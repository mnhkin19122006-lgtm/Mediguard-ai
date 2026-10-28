import os
import re
import json
import asyncio
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List, Dict, Any, cast
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.datetime_utils import utc_now, utc_now_iso
from app.config import DEFAULT_VIETNAMESE_VOICE, DEFAULT_VOICE_RATE, STORAGE_DIR
from app.models.user import User, Role
from app.models.schedule import Schedule
from app.models.medication import Medication
from app.models.interaction_log import InteractionLog
from app.models.symptom_log import SymptomLog
from app.voice.tts_service import text_to_speech, get_cached_tts
from app.core.rule_engine import evaluate_safety_and_intent
from app.core.notifier import send_telegram_alert
from app.core.security import get_current_user
from app.api.websocket import broadcast_sync
from app.core.llm_service import (
    get_smart_llm_reply,
    get_realtime_weather_summary,
    detect_search_need,
    get_conversation_history,
    save_conversation_turn,
    clear_conversation_history
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["Voice"])

PREFERENCES_FILE = STORAGE_DIR / "companion_preferences.json"
SESSIONS_FILE = STORAGE_DIR / "companion_sessions.json"


def _load_json_data(file_path: Path) -> Dict[str, Any]:
    """Đọc dữ liệu JSON an toàn, không văng lỗi khi file thiếu hoặc hỏng định dạng."""
    if not file_path.exists():
        return {}
    try:
        content = file_path.read_text(encoding="utf-8").strip()
        if not content:
            return {}
        return json.loads(content)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Không thể đọc file cấu hình %s (%s). Khởi tạo mặc định.", file_path.name, type(e).__name__)
        return {}


def _save_json_data(file_path: Path, data: Dict[str, Any]) -> None:
    """Ghi dữ liệu JSON nguyên tử qua file tạm để chống hỏng dữ liệu khi ghi đồng thời."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_name(f"{file_path.stem}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, file_path)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        logger.error("Lỗi khi ghi file dữ liệu %s: %s", file_path.name, type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Lỗi lưu trữ dữ liệu hệ thống"
        )


class SpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    rate: Optional[str] = None


class VoiceProcessRequest(BaseModel):
    user_speech: str
    voice: Optional[str] = None
    rate: Optional[str] = None
    elder_id: Optional[int] = None
    session_id: Optional[str] = None


class CompanionPreferencesPayload(BaseModel):
    elder_id: int
    preferred_voice: Optional[str] = DEFAULT_VIETNAMESE_VOICE
    speech_rate: Optional[str] = DEFAULT_VOICE_RATE
    daily_greeting_enabled: Optional[bool] = True
    reminder_tone: Optional[str] = "gentle"
    favorite_topics: Optional[List[str]] = Field(default_factory=list)
    custom_notes: Optional[str] = None


class CompanionPreferencesResponse(BaseModel):
    success: bool
    elder_id: int
    preferences: Dict[str, Any]


# ==============================================================================
# ENDPOINT BẢO MẬT COMPANION PREFERENCES & SESSIONS
# ==============================================================================

@router.get("/companion/preferences", response_model=CompanionPreferencesResponse)
def get_companion_preferences(
    elder_id: int = Query(..., description="ID người cao tuổi cần xem sở thích"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Lấy thông tin cài đặt sở thích trò chuyện đồng hành của người cao tuổi.
    Bảo vệ phân quyền: Người chăm sóc chỉ được xem người cao tuổi được phân quyền.
    """
    if not current_user.can_access_elder(elder_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Không có quyền truy cập dữ liệu của người cao tuổi này"
        )
    elder = db.query(User).filter(User.id == elder_id, User.role == Role.ELDER).first()
    if not elder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy người cao tuổi")

    all_prefs = _load_json_data(PREFERENCES_FILE)
    elder_prefs = all_prefs.get(str(elder_id), {
        "preferred_voice": DEFAULT_VIETNAMESE_VOICE,
        "speech_rate": DEFAULT_VOICE_RATE,
        "daily_greeting_enabled": True,
        "reminder_tone": "gentle",
        "favorite_topics": [],
        "custom_notes": ""
    })
    return {
        "success": True,
        "elder_id": elder_id,
        "preferences": elder_prefs
    }


@router.post("/companion/preferences", response_model=CompanionPreferencesResponse)
def update_companion_preferences(
    payload: CompanionPreferencesPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Cập nhật sở thích trò chuyện đồng hành cho người cao tuổi.
    Bảo vệ phân quyền: Người chăm sóc chỉ được cập nhật người cao tuổi được phân quyền.
    """
    elder_id = payload.elder_id
    if not current_user.can_access_elder(elder_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Không có quyền truy cập dữ liệu của người cao tuổi này"
        )
    elder = db.query(User).filter(User.id == elder_id, User.role == Role.ELDER).first()
    if not elder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy người cao tuổi")

    all_prefs = _load_json_data(PREFERENCES_FILE)
    updated_dict = {
        "preferred_voice": payload.preferred_voice or DEFAULT_VIETNAMESE_VOICE,
        "speech_rate": payload.speech_rate or DEFAULT_VOICE_RATE,
        "daily_greeting_enabled": payload.daily_greeting_enabled if payload.daily_greeting_enabled is not None else True,
        "reminder_tone": payload.reminder_tone or "gentle",
        "favorite_topics": payload.favorite_topics or [],
        "custom_notes": payload.custom_notes or "",
        "updated_at": utc_now_iso()
    }
    all_prefs[str(elder_id)] = updated_dict
    _save_json_data(PREFERENCES_FILE, all_prefs)

    return {
        "success": True,
        "elder_id": elder_id,
        "preferences": updated_dict
    }


@router.delete("/companion/preferences")
def delete_companion_preferences(
    elder_id: int = Query(..., description="ID người cao tuổi cần xóa sở thích"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Xóa/đặt lại sở thích đồng hành của người cao tuổi về mặc định.
    Bảo vệ phân quyền: Người chăm sóc chỉ được thao tác với người cao tuổi được phân quyền.
    """
    if not current_user.can_access_elder(elder_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Không có quyền truy cập dữ liệu của người cao tuổi này"
        )
    elder = db.query(User).filter(User.id == elder_id, User.role == Role.ELDER).first()
    if not elder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy người cao tuổi")

    all_prefs = _load_json_data(PREFERENCES_FILE)
    if str(elder_id) in all_prefs:
        del all_prefs[str(elder_id)]
        _save_json_data(PREFERENCES_FILE, all_prefs)

    return {
        "success": True,
        "elder_id": elder_id,
        "message": "Đã đặt lại sở thích đồng hành về mặc định thành công"
    }


@router.delete("/companion/session/{elder_id}")
def clear_companion_session(
    elder_id: int,
    session_id: Optional[str] = Query(None, description="Mã phiên cần xóa (bỏ trống để xóa toàn bộ)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Xóa phiên làm việc/trạng thái hội thoại đồng hành hiện tại của người cao tuổi.
    Bảo vệ phân quyền: Người chăm sóc chỉ được thao tác với người cao tuổi được phân quyền.
    """
    if not current_user.can_access_elder(elder_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Không có quyền truy cập dữ liệu của người cao tuổi này"
        )
    elder = db.query(User).filter(User.id == elder_id, User.role == Role.ELDER).first()
    if not elder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy người cao tuổi")

    clear_conversation_history(elder_id, session_id=session_id)

    return {
        "success": True,
        "elder_id": elder_id,
        "session_cleared": True,
        "session_id": session_id,
        "message": "Đã làm mới phiên hội thoại đồng hành của người cao tuổi"
    }


# ==============================================================================
# ENDPOINT PHÁT ÂM THANH & XỬ LÝ GIỌNG NÓI
# ==============================================================================

@router.post("/speak")
async def speak_text(req: SpeakRequest):
    """
    Tạo giọng đọc tiếng Việt chuẩn Edge-TTS từ văn bản bất kỳ.
    Có cache theo hash nội dung để phản hồi tức thời (< 10ms).
    Nếu chưa có trong cache thì sinh nhanh hoặc sinh nền, không bao giờ ném lỗi 500.
    """
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="Thiếu nội dung văn bản")
    voice = req.voice or DEFAULT_VIETNAMESE_VOICE
    rate = req.rate or DEFAULT_VOICE_RATE
    clean_text = req.text.strip()

    # 1. Trả về ngay nếu đã có trong cache (< 5ms)
    cached = get_cached_tts(clean_text, voice=voice, rate=rate)
    if cached:
        return {
            "success": True,
            "text": req.text,
            "audio_url": f"/storage/audio_cache/{cached}"
        }

    # 2. Thử sinh nhanh bằng Chị Google trong tối đa 1.5s
    tts_task = asyncio.create_task(text_to_speech(clean_text, voice=voice, rate=rate))
    try:
        audio_file = await asyncio.wait_for(asyncio.shield(tts_task), timeout=1.5)
        if audio_file:
            return {
                "success": True,
                "text": req.text,
                "audio_url": f"/storage/audio_cache/{audio_file}"
            }
    except asyncio.TimeoutError:
        logger.info("TTS trong /speak đang được sinh ngầm trong nền...")
    except Exception as e:
        logger.warning("Lỗi sinh âm thanh giọng Chị Google trong /speak: %s", e)

    return {
        "success": True,
        "text": req.text,
        "audio_url": None
    }


@router.post("/process")
async def process_voice(req: VoiceProcessRequest, db: Session = Depends(get_db)):
    """
    Tiếp nhận câu nói tiếng Việt của người già, phân tích an toàn,
    sinh câu thoại phản hồi bằng giọng nói tiếng Việt Edge-TTS,
    đồng thời lưu vết vào Nhật ký kiểm toán y tế (InteractionLog).
    """
    if not req.user_speech or not req.user_speech.strip():
        raise HTTPException(status_code=400, detail="Thiếu câu nói của người dùng")

    elder_id: int = req.elder_id if req.elder_id is not None else 1
    active_sch = None
    try:
        active_sch = db.query(Schedule).filter(Schedule.is_active == True).first()
        if req.elder_id is None and active_sch and active_sch.elder_id is not None:
            elder_id = cast(int, active_sch.elder_id)
    except Exception as db_err:
        logger.warning("Lỗi truy vấn schedule active: %s", db_err)
        db.rollback()

    elder = db.query(User).filter(User.id == elder_id).first()
    raw_name = str(elder.full_name) if elder and elder.full_name else f"#{elder_id}"
    cleaned_name = re.sub(r'\(.*?\)', '', raw_name).strip()
    for prefix in ["Bác ", "Ông ", "Bà ", "Cụ "]:
        if cleaned_name.startswith(prefix):
            cleaned_name = cleaned_name[len(prefix):].strip()
    elder_display_name = f"Bác {cleaned_name}" if cleaned_name else "Bác"

    analysis = evaluate_safety_and_intent(req.user_speech)
    reply_text = analysis.get("reply_speech") or "Cháu đã ghi nhận thông tin của bác rồi ạ!"
    final_intent = analysis.get("intent", "GENERAL_TALK")
    steps_list = None

    voice = req.voice or DEFAULT_VIETNAMESE_VOICE
    rate = req.rate or DEFAULT_VOICE_RATE

    # Nếu người dùng báo uống thuốc thành công, tự động ghi nhận vào InteractionLog
    if analysis.get("intent") == "CONFIRM_INTAKE":
        try:
            recent_thresh = utc_now() - timedelta(seconds=5)
            recent_confirm = db.query(InteractionLog).filter(
                InteractionLog.elder_id == elder_id,
                InteractionLog.event_type == "user_responded",
                InteractionLog.response_content == req.user_speech,
                InteractionLog.timestamp >= recent_thresh
            ).first()

            if not recent_confirm:
                log_entry = InteractionLog(
                    schedule_id=active_sch.id if active_sch else None,
                    elder_id=elder_id,
                    event_type="user_responded",
                    response_content=req.user_speech,
                    notes="Xác nhận uống thuốc bằng đối thoại giọng nói AI",
                    audio_url=None,
                    timestamp=utc_now()
                )
                db.add(log_entry)
                db.commit()
        except Exception as e:
            db.rollback()
            logger.warning("Lỗi ghi log voice intake: %s", e)

    # Nếu phát hiện yêu cầu nguy hiểm hoặc triệu chứng cấp tính -> gửi cảnh báo khẩn và lưu vết theo ngày
    elif analysis.get("action") in ["EMERGENCY_ALERT", "REJECT_AND_NOTIFY_CAREGIVER"]:
        today_str = date.today().isoformat()
        now_time = datetime.now().strftime("%H:%M")

        # Deduplication check on retry within 5 seconds
        recent_log = None
        try:
            recent_threshold = utc_now() - timedelta(seconds=5)
            recent_log = db.query(InteractionLog).filter(
                InteractionLog.elder_id == elder_id,
                InteractionLog.event_type == "alert",
                InteractionLog.response_content == req.user_speech,
                InteractionLog.timestamp >= recent_threshold
            ).first()
        except Exception:
            db.rollback()

        if not recent_log:
            # Kiểm tra nếu câu nói của Bác vừa có triệu chứng vừa có nội dung tâm sự/hỏi han
            chat_triggers = [
                "kể chuyện", "chuyện", "tâm sự", "nói chuyện", "nhớ", "buồn", "cô đơn",
                "cháu ơi", "hỏi", "đỡ đau", "cho vui", "làng quê", "cổ tích", "câu đố",
                "thế nào", "tại sao", "sao lại", "ở quê", "bầu bạn"
            ]
            has_chat = any(w in req.user_speech.lower() for w in chat_triggers) or len(req.user_speech.split()) >= 8
            if has_chat and analysis.get("intent") == "REPORT_SYMPTOM":
                history = get_conversation_history(elder_id, req.session_id)
                comp_reply = await get_smart_llm_reply(
                    f"Bác vừa báo triệu chứng [{analysis.get('pattern_matched', 'Triệu chứng cấp tính')}]. Cháu đã ghi nhận và báo gia đình. Đồng thời Bác vừa nói: \"{req.user_speech}\". Hãy mở đầu bằng lời dặn Bác ngồi nghỉ ngơi vì người nhà đang gọi điện cho Bác, sau đó trả lời phần trò chuyện, tâm sự của Bác thật ấm áp, tình cảm trong 2-3 câu.",
                    elder_name="Bác",
                    voice=voice,
                    history=history
                )
                if comp_reply and len(comp_reply) >= 15:
                    reply_text = f"Dạ thưa Bác, cháu đã ghi nhận triệu chứng vào sổ theo dõi và báo ngay cho gia đình rồi ạ. Bác nhớ ngồi nghỉ ngơi tại chỗ nhé. {comp_reply}"
                    save_conversation_turn(elder_id, req.user_speech, reply_text, req.session_id)

            # 1. Gửi Telegram alert an toàn độc lập với DB transaction
            alert_msg = (
                f"🚨 <b>CẢNH BÁO TỪ GIỌNG NÓI NGƯỜI CAO TUỔI!</b>\n"
                f"• Nội dung bác nói: \"{req.user_speech}\"\n"
                f"• Ngày ghi nhận: {today_str} lúc {now_time}\n"
                f"• Phân loại: {analysis.get('warning_reason', 'Cảnh báo an toàn')}\n"
                f"• Phản hồi AI: {reply_text}\n"
                f"👉 <i>Đề nghị gia đình chủ động liên hệ kiểm tra tình hình sức khỏe của bác!</i>"
            )
            telegram_ok = False
            try:
                telegram_ok = await asyncio.wait_for(send_telegram_alert(alert_msg), timeout=4.0)
            except Exception as tele_exc:
                logger.error("Ngoại lệ khi gửi cảnh báo Telegram: %s", tele_exc)
                telegram_ok = False

            # 2. Lưu log nghiệp vụ (SymptomLog và InteractionLog) ĐỘC LẬP với kết quả Telegram
            try:
                if analysis.get("intent") == "REPORT_SYMPTOM":
                    action_text = (
                        "AI đã trấn an bác và gửi tín hiệu báo động tới điện thoại người nhà qua Telegram thành công."
                        if telegram_ok else
                        "AI đã trấn an bác. Tín hiệu cảnh báo Telegram chưa gửi được tới người nhà."
                    )
                    symp = SymptomLog(
                        elder_id=elder_id,
                        symptom_name=analysis.get("pattern_matched", "Triệu chứng cấp tính").capitalize(),
                        severity="Khẩn cấp",
                        description=f"Giọng nói ghi nhận: \"{req.user_speech}\"",
                        reported_date=today_str,
                        reported_time=now_time,
                        caregiver_notified=telegram_ok,
                        caregiver_confirmed=False,
                        action_taken=action_text,
                        timestamp=utc_now()
                    )
                    db.add(symp)
                    db.commit()
                    db.refresh(symp)

                    try:
                        broadcast_sync("symptom_alert", {
                            "event": "symptom_reported",
                            "symptom": symp.to_dict(),
                            "message": f"🚨 CẢNH BÁO SỨC KHỎE: Bác vừa báo triệu chứng [{symp.symptom_name}] vào lúc {now_time} hôm nay!",
                            "timestamp": utc_now_iso()
                        })
                    except Exception as bc_err:
                        logger.warning("Lỗi broadcast websocket: %s", bc_err)

                tele_status_desc = "Telegram đã gửi" if telegram_ok else "Telegram gửi thất bại"
                log_entry = InteractionLog(
                    schedule_id=active_sch.id if active_sch else None,
                    elder_id=elder_id,
                    event_type="alert",
                    response_content=req.user_speech,
                    notes=f"Cảnh báo giọng nói ({today_str}): {analysis.get('warning_reason')} [{tele_status_desc}]",
                    audio_url=None,
                    timestamp=utc_now()
                )
                db.add(log_entry)
                db.commit()
            except Exception as db_err:
                logger.error("Lỗi lưu log nghiệp vụ DB: %s", db_err)
                db.rollback()

    # Phân loại hỏi về thuốc trong ngày hoặc kế hoạch sinh hoạt hôm nay
    elif analysis.get("intent") == "INQUIRE_MEDICATION":
        text_lower = req.user_speech.lower()
        is_routine = any(k in text_lower for k in [
            "làm gì", "cần làm", "phải làm", "việc hôm nay", "hôm nay có việc gì",
            "kế hoạch", "nhiệm vụ", "hôm nay thế nào"
        ])

        meds = db.query(Medication).filter(
            Medication.elder_id == elder_id,
            Medication.is_active == True
        ).all()
        schedules = db.query(Schedule).filter(
            Schedule.elder_id == elder_id,
            Schedule.is_active == True
        ).all()

        if is_routine:
            final_intent = "INQUIRE_DAILY_ROUTINE"
            steps_list = [
                "Bác hãy nghỉ ngơi thoải mái, tránh xúc động hay làm việc nặng.",
                "Uống một ly nước lọc ấm và đo lại huyết áp buổi sáng."
            ]
            if meds:
                by_period = {}
                for m in meds:
                    p = (m.time_period or "Hàng ngày").capitalize()
                    by_period.setdefault(p, []).append(f"{m.name} ({m.dosage or '1 viên'})")
                med_parts = []
                for p, items in by_period.items():
                    med_parts.append(f"cữ {p.lower()}: {', '.join(items)}")
                    steps_list.append(f"Uống thuốc cữ {p.lower()}: {', '.join(items)}")
                med_summary = "; ".join(med_parts)
            elif schedules:
                med_parts = [f"cữ {s.time_period.lower()} lúc {s.target_time}" for s in schedules]
                for s in schedules:
                    steps_list.append(f"Uống thuốc cữ {s.time_period.lower()} lúc {s.target_time}")
                med_summary = "; ".join(med_parts)
            else:
                med_summary = "hiện chưa có cữ thuốc nào cần uống"
                steps_list.append("Ăn uống thanh đạm, đủ chất và giữ ấm cơ thể.")

            reply_text = (
                f"Dạ thưa {elder_display_name}, về kế hoạch hôm nay: "
                f"Về phần thuốc hôm nay, Bác cần uống {med_summary}. "
                f"Bác nghỉ ngơi thoải mái, tránh làm việc nặng và vận động nhẹ nhàng thư thái Bác nhé! "
                f"Cháu luôn ở bên Bác để hỗ trợ nhắc nhở cữ thuốc nhé ạ."
            )
        else:
            final_intent = "INQUIRE_MEDICATION"
            if meds:
                by_period = {}
                for m in meds:
                    p = (m.time_period or "Hàng ngày").capitalize()
                    by_period.setdefault(p, []).append(f"{m.name} ({m.dosage or '1 viên'})")
                summary_str = "; ".join(f"cữ {p.lower()} gồm có {', '.join(items)}" for p, items in by_period.items())
                reply_text = (
                    f"Dạ thưa {elder_display_name}, hiện tại trong danh mục thuốc Bác đang uống gồm có {len(meds)} loại: {summary_str}. "
                    f"Bác nhớ uống đúng giờ và uống cùng nước lọc ấm nhé ạ!"
                )
            elif schedules:
                sch_summaries = [f"cữ {s.time_period.lower()} lúc {s.target_time}" for s in schedules]
                reply_text = (
                    f"Dạ thưa {elder_display_name}, lịch uống thuốc hôm nay của Bác gồm có: {'; '.join(sch_summaries)}. "
                    f"Bác nhớ uống đúng giờ và uống kèm nước ấm nhé ạ!"
                )
            else:
                reply_text = (
                    f"Dạ thưa {elder_display_name}, hiện tại trong sổ theo dõi của Bác chưa có loại thuốc nào được ghi nhận. "
                    "Bác có thể nhờ con cháu cập nhật thêm đơn thuốc vào ứng dụng cho Bác nhé ạ!"
                )
        save_conversation_turn(elder_id, req.user_speech, reply_text, req.session_id)

    # Chào hỏi hoặc hỏi thông tin nhận dạng
    elif analysis.get("intent") in ["GREETING", "IDENTITY"]:
        final_intent = str(analysis.get("intent") or "GREETING")
        reply_text = str(analysis.get("reply_speech") or reply_text or "Dạ cháu chào bác ạ!")
        save_conversation_turn(elder_id, req.user_speech, reply_text, req.session_id)

    # Mọi câu hỏi ngoài luồng, chào hỏi, tâm sự, kiến thức hoặc không khớp intent nghiệp vụ -> chuyển đến LLM
    else:
        history = get_conversation_history(elder_id, req.session_id)
        needs_search = detect_search_need(req.user_speech)
        meds = db.query(Medication).filter(
            Medication.elder_id == elder_id,
            Medication.is_active == True
        ).all()
        med_names: list[str] = [str(m.name) for m in meds if m.name]

        llm_reply = await get_smart_llm_reply(
            req.user_speech,
            elder_name=elder_display_name,
            medications=med_names,
            time_context=f"{datetime.now().hour} giờ {datetime.now().minute} phút",
            weather_context=get_realtime_weather_summary(),
            voice=voice,
            use_grounding=needs_search,
            history=history
        )
        if llm_reply and len(llm_reply) >= 8:
            reply_text = llm_reply
        save_conversation_turn(elder_id, req.user_speech, reply_text, req.session_id)

    # Sinh âm thanh giọng đọc Chị Google cho phản hồi
    reply_speech_str = str(reply_text or "Cháu đã ghi nhận thông tin rồi ạ!")
    audio_file = get_cached_tts(reply_speech_str, voice=voice, rate=rate)
    if not audio_file:
        tts_task = asyncio.create_task(text_to_speech(reply_speech_str, voice=voice, rate=rate))
        try:
            audio_file = await asyncio.wait_for(asyncio.shield(tts_task), timeout=1.2)
        except asyncio.TimeoutError:
            logger.info("TTS trong /process đang tạo ngầm trong nền...")
        except Exception as tts_err:
            logger.warning("Timeout/lỗi khi sinh giọng Chị Google trong /process: %s", tts_err)

    return {
        "success": True,
        "is_safe": analysis.get("is_safe", True),
        "intent": final_intent,
        "action": analysis.get("action", "SAFE_GENERAL_REPLY"),
        "warning_reason": analysis.get("warning_reason"),
        "reply_speech": reply_speech_str,
        "reply": reply_speech_str,
        "steps": steps_list,
        "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
    }
