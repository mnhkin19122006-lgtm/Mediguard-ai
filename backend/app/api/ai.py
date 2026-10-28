import os
import re
import asyncio
import logging
from datetime import datetime, date
from app.core.datetime_utils import utc_now, utc_now_iso
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.symptom_log import SymptomLog
from app.models.schedule import Schedule
from app.models.medication import Medication
from app.models.user import User, Role
from app.core.security import require_caregiver_or_admin, security_scheme
from app.config import SECRET_KEY, ALGORITHM
from jose import jwt
from fastapi.security import HTTPAuthorizationCredentials
from app.models.interaction_log import InteractionLog
from app.api.drugs import search_drugs, DRUG_DATABASE
from app.core.rule_engine import evaluate_safety_and_intent, SYMPTOM_PATTERNS, FORBIDDEN_PATTERNS
from app.core.notifier import send_telegram_alert
from app.core.zalo_service import zalo_service
from app.api.websocket import broadcast_sync
from app.voice.tts_service import text_to_speech, get_cached_tts
from app.config import DEFAULT_VIETNAMESE_VOICE, DEFAULT_VOICE_RATE
from app.core.llm_service import (
    get_smart_llm_reply,
    get_gemini_key,
    get_openai_key,
    set_llm_config,
    get_realtime_weather_summary,
    get_smart_contextual_fallback,
    get_agent_info,
    detect_search_need,
    get_conversation_history,
    save_conversation_turn
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["AI"])


async def resolve_audio_file(reply_text: str, voice: str = None, rate: str = None) -> str | None:
    """
    Trả về file âm thanh giọng Chị Google (Google Translate TTS).
    Nếu đã có trong cache -> trả về filename ngay lập tức (< 1ms).
    Nếu chưa có -> sinh trong nền hoặc chờ tối đa 1.2s,
    đảm bảo API luôn phản hồi siêu tốc (< 100ms khi cache hit, tối đa 1.2s khi cache miss).
    """
    if not reply_text or not reply_text.strip():
        return None
    actual_voice = voice if (voice and "nam" not in voice.lower()) else DEFAULT_VIETNAMESE_VOICE
    cached = get_cached_tts(reply_text, voice=actual_voice, rate=rate)
    if cached:
        return cached

    tts_task = asyncio.create_task(
        text_to_speech(reply_text, voice=actual_voice, rate=rate or DEFAULT_VOICE_RATE)
    )
    try:
        audio_file = await asyncio.wait_for(asyncio.shield(tts_task), timeout=1.2)
        if audio_file:
            return audio_file
    except asyncio.TimeoutError:
        logger.info("TTS đang được sinh ngầm trong nền, trả về văn bản phản hồi siêu tốc trước.")
    except Exception as e:
        logger.warning(f"Lỗi sinh audio Chị Google: {e}")
    return None


def get_optional_current_user(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Dependency trích xuất User hiện tại nếu có Authorization Bearer token hợp lệ."""
    if not auth or not auth.credentials:
        return None
    try:
        payload = jwt.decode(auth.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        username = payload.get("sub")
        if user_id is not None:
            return db.query(User).filter(User.id == user_id, User.is_active == True).first()
        if username is not None:
            return db.query(User).filter(User.username == username, User.is_active == True).first()
    except Exception:
        return None
    return None


class FastAIChatRequest(BaseModel):
    elder_id: Optional[int] = None
    caller_id: Optional[int] = None
    user_id: Optional[int] = None
    message: str
    voice: Optional[str] = DEFAULT_VIETNAMESE_VOICE
    rate: Optional[str] = DEFAULT_VOICE_RATE
    need_audio: Optional[bool] = False
    session_id: Optional[str] = None


# Danh mục triệu chứng mở rộng để nhận diện chuẩn xác
EXTENDED_SYMPTOMS = [
    ("chóng mặt", "Chóng mặt, hoa mắt, choáng váng", "Khẩn cấp"),
    ("hoa mắt", "Hoa mắt, chóng mặt", "Khẩn cấp"),
    ("choáng váng", "Choáng váng, xây xẩm mặt mày", "Khẩn cấp"),
    ("đau đầu", "Đau đầu, nhức đầu", "Trung bình"),
    ("nhức đầu", "Nhức đầu, đau nửa đầu", "Trung bình"),
    ("buồn nôn", "Buồn nôn, nôn mửa, cồn cào", "Khẩn cấp"),
    ("mắc ói", "Buồn nôn, cồn cào khó chịu", "Khẩn cấp"),
    ("đau bụng", "Đau quặn bụng, đau dạ dày", "Khẩn cấp"),
    ("quặn bụng", "Đau quặn thắt vùng bụng", "Khẩn cấp"),
    ("khó thở", "Khó thở, hụt hơi, thở dốc", "Khẩn cấp"),
    ("hụt hơi", "Hụt hơi, khó thở, thở dốc", "Khẩn cấp"),
    ("tức ngực", "Tức ngực, đau thắt ngực", "Khẩn cấp"),
    ("đau ngực", "Đau thắt ngực, nhói tim", "Khẩn cấp"),
    ("nhói tim", "Đau nhói tim, thắt ngực", "Khẩn cấp"),
    ("mệt mỏi", "Mệt mỏi nhiều trong người", "Trung bình"),
    ("mệt quá", "Rất mệt, kiệt sức", "Khẩn cấp"),
    ("thấy mệt", "Cảm giác mệt mỏi trong người", "Trung bình"),
    ("mệt trong người", "Cảm giác mệt mỏi bất thường", "Trung bình"),
    ("bủn rủn", "Tay chân bủn rủn, kiệt sức", "Trung bình"),
    ("kiệt sức", "Kiệt sức, đuối sức", "Khẩn cấp"),
    ("mệt", "Mệt mỏi trong người", "Trung bình"),
    ("sốt", "Bị sốt, người nóng ran", "Trung bình"),
    ("nóng sốt", "Sốt cao, thân nhiệt nóng ran", "Trung bình"),
    ("tim đập nhanh", "Hồi hộp, tim đập nhanh, đánh trống ngực", "Khẩn cấp"),
    ("uống nhầm", "Nghi ngờ đã uống nhầm thuốc", "Khẩn cấp"),
    ("đau lưng", "Đau nhức lưng, cột sống", "Trung bình"),
    ("nhức mỏi", "Nhức mỏi toàn thân, đau cơ", "Trung bình"),
    ("run tay", "Run tay chân, bủn rủn", "Trung bình"),
    ("ngứa", "Nổi mẩn ngứa, dị ứng da", "Trung bình"),
    ("tiêu chảy", "Tiêu chảy, rối loạn tiêu hóa", "Khẩn cấp"),
    ("đi ngoài", "Đi ngoài nhiều lần, rối loạn tiêu hóa", "Khẩn cấp"),
    ("ho nhiều", "Ho dai dẳng, viêm họng", "Trung bình"),
    ("bị ho", "Bị ho, ngứa rát cổ họng", "Trung bình"),
    ("mất ngủ", "Mất ngủ, khó ngủ kéo dài", "Trung bình"),
]


def clean_person_name(raw_name: Optional[str]) -> str:
    """
    Chuẩn hóa họ tên: loại bỏ phần chú thích trong ngoặc đơn (ví dụ: '(72 tuổi)', '(Con trai)')
    và loại bỏ các tiền tố xưng hô thừa nếu đã có sẵn trong full_name.
    """
    if not raw_name:
        return ""
    cleaned = re.sub(r'\(.*?\)', '', raw_name).strip()
    for prefix in ["Bác ", "Ông ", "Bà ", "Cụ ", "Anh ", "Chị ", "Cô ", "Chú ",
                   "bác ", "ông ", "bà ", "cụ ", "anh ", "chị ", "cô ", "chú "]:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    if cleaned in ["Bác", "Ông", "Bà", "Cụ", "Anh", "Chị", "Cô", "Chú",
                   "bác", "ông", "bà", "cụ", "anh", "chị", "cô", "chú"]:
        cleaned = ""
    return cleaned


def detect_drug_lookup(text: str) -> Optional[dict]:
    """
    Nhận diện câu hỏi tra cứu thông tin dược lý / công dụng / tác dụng phụ của thuốc.
    Phân biệt rõ với câu hỏi lịch uống thuốc cá nhân hoặc đơn thuốc của người bệnh.
    """
    text_clean = text.lower().strip()
    text_norm = re.sub(r'[?!.,;:]+$', '', text_clean).strip()

    # 1. Bỏ qua nếu là câu hỏi về đơn thuốc cá nhân hoặc lịch uống thuốc của người bệnh
    personal_prescription_triggers = [
        "trong đơn", "đơn thuốc của", "thuốc của tôi", "thuốc của bác", "thuốc của cụ",
        "tôi đang uống", "bác đang uống", "cụ đang uống",
        "tôi có đang uống", "bác có đang uống", "tôi có phải uống", "bác có phải uống",
        "mấy giờ uống", "khi nào uống", "lịch uống", "cữ sáng", "cữ trưa", "cữ chiều", "cữ tối",
        "hôm nay uống", "hôm nay tôi uống", "hôm nay bác uống", "đã uống", "uống rồi",
        "kế hoạch hôm nay", "việc cần làm hôm nay", "nhiệm vụ hôm nay"
    ]
    if any(p in text_norm for p in personal_prescription_triggers):
        return None

    # 2. Bỏ qua nếu là câu hỏi tư vấn cách uống / thời điểm / tương tác đồ ăn thức uống chung (Section 9/10)
    ingestion_lifestyle_triggers = [
        "thời điểm nào tốt nhất", "uống vào lúc nào tốt nhất", "uống lúc nào tốt nhất",
        "uống cùng lúc với sữa", "uống với sữa", "uống với nước gì", "uống nước gì",
        "uống bao nhiêu nước", "uống trước hay sau ăn", "lúc đói hay lúc no"
    ]
    if any(t in text_norm for t in ingestion_lifestyle_triggers):
        return None

    # 3. Nhận diện khía cạnh câu hỏi (aspect)
    aspect = None
    if re.search(r'tác dụng phụ|tác dụng không mong muốn|phản ứng phụ|phản ứng không mong muốn', text_norm):
        aspect = "side_effects"
    elif re.search(r'chống chỉ định|ai không nên (?:uống|dùng)|ai không được (?:uống|dùng)|không dùng cho ai', text_norm):
        aspect = "contraindications"
    elif re.search(r'tương tác|kỵ với|kiêng gì|kỵ gì', text_norm):
        aspect = "interactions"
    elif re.search(r'hoạt chất|thành phần', text_norm):
        aspect = "ingredient"
    elif re.search(r'dùng để làm gì|dùng làm gì|dùng để trị gì|dùng trị gì|dùng để chữa gì|dùng chữa gì|trị bệnh gì|chữa bệnh gì|trị cái gì|chữa cái gì|có tác dụng gì|tác dụng gì|công dụng gì|có công dụng gì|công dụng của|tác dụng của', text_norm):
        aspect = "indication"
    elif re.search(r'là thuốc gì|thông tin về thuốc|thông tin thuốc|tra cứu thuốc|tìm hiểu về thuốc|cho tôi biết về thuốc|cho tôi biết thuốc|cho bác biết về thuốc|cho bác biết thuốc', text_norm):
        aspect = "general"

    if not aspect:
        return None

    # 4. Bóc tách tên thuốc mục tiêu (drug name)
    cleaned = text_norm
    prefixes = [
        r'^(?:dạ\s+)?(?:thưa\s+cháu\s+)?(?:cháu\s+ơi\s+)?',
        r'^cho\s+(?:tôi|bác|cháu|em|mình)\s+(?:hỏi\s+)?(?:biết\s+)?(?:thông\s+tin\s+)?(?:về\s+)?',
        r'^(?:hãy\s+)?cho\s+(?:tôi|bác|cháu|em)\s+hỏi\s+(?:về\s+)?',
        r'^làm\s+ơn\s+cho\s+(?:tôi|bác)\s+biết\s+(?:về\s+)?',
        r'^tra\s+cứu\s+(?:thông\s+tin\s+)?(?:về\s+)?',
        r'^tìm\s+hiểu\s+(?:về\s+)?',
        r'^thông\s+tin\s+(?:về\s+)?',
        r'^bác\s+muốn\s+(?:hỏi|biết)\s+(?:về\s+)?',
        r'^tôi\s+muốn\s+(?:hỏi|biết)\s+(?:về\s+)?',
    ]
    for p in prefixes:
        cleaned = re.sub(p, '', cleaned).strip()

    # Mẫu A: (công dụng|tác dụng|tác dụng phụ|thành phần|hoạt chất) của (thuốc )?X
    m_of = re.search(r'(?:công dụng|tác dụng|tác dụng phụ|thành phần|hoạt chất)\s+của\s+(?:thuốc\s+|viên\s+)?(.+)', cleaned)
    if m_of:
        raw_drug = m_of.group(1).strip()
        raw_drug = re.sub(r'\s+(?:là gì|như thế nào|thế nào|hả cháu|nhỉ|vậy).*$', '', raw_drug).strip()
        return {"drug_name": raw_drug, "aspect": aspect}

    # Mẫu B: (thuốc |viên )?X + hậu tố câu hỏi
    suffixes = [
        r'\s+(?:dùng\s+để\s+làm\s+gì|dùng\s+làm\s+gì|dùng\s+để\s+trị\s+gì|dùng\s+trị\s+gì|trị\s+bệnh\s+gì|chữa\s+bệnh\s+gì).*$',
        r'\s+(?:có\s+tác\s+dụng\s+gì|tác\s+dụng\s+gì|công\s+dụng\s+gì|có\s+công\s+dụng\s+gì).*$',
        r'\s+(?:có\s+tác\s+dụng\s+phụ\s+gì|tác\s+dụng\s+phụ\s+gì|tác\s+dụng\s+phụ\s+là\s+gì).*$',
        r'\s+(?:chống\s+chỉ\s+định\s+gì|chống\s+chỉ\s+định\s+như\s+thế\s+nào).*$',
        r'\s+(?:là\s+thuốc\s+gì|là\s+gì).*$',
        r'\s+(?:kỵ\s+với\s+gì|kiêng\s+gì).*$',
    ]
    for s in suffixes:
        if re.search(s, cleaned):
            raw_drug = re.sub(s, '', cleaned).strip()
            raw_drug = re.sub(r'^(?:thuốc|viên|loại\s+thuốc)\s+', '', raw_drug).strip()
            if raw_drug:
                return {"drug_name": raw_drug, "aspect": aspect}

    # Mẫu C: (về )?(thuốc |viên )X
    m_thuoc = re.match(r'^(?:về\s+)?(?:thuốc|viên)\s+([a-zA-Z0-9\sà-ỹÀ-Ỹ\-\_\.]+)', cleaned)
    if m_thuoc:
        raw_drug = m_thuoc.group(1).strip()
        raw_drug = re.sub(r'\s+(?:là gì|như thế nào|thế nào|hả cháu|nhỉ|vậy).*$', '', raw_drug).strip()
        return {"drug_name": raw_drug, "aspect": aspect}

    # Mẫu D: Nếu câu hỏi có dạng "cho tôi biết thuốc X"
    if cleaned and aspect in ["general", "indication"]:
        clean_name = re.sub(r'^(?:thuốc|viên|về\s+thuốc|về\s+viên)\s+', '', cleaned).strip()
        clean_name = re.sub(r'\s+(?:là gì|như thế nào|thế nào|hả cháu|nhỉ|vậy).*$', '', clean_name).strip()
        if clean_name and len(clean_name) >= 2:
            return {"drug_name": clean_name, "aspect": aspect}

    return None


def resolve_chat_user_context(
    db: Session,
    caller_id: Optional[int] = None,
    elder_id: Optional[int] = None,
    user_text: str = "",
    current_user: Optional[User] = None
) -> dict:
    """
    Xác định vai trò, xưng hô và quan hệ người cao tuổi được quản lý cho luồng chat/voice.
    
    Quy tắc nghiệp vụ:
    1. Phân biệt đúng ELDER và CAREGIVER/ADMIN theo vai trò (Role) thực tế trong database.
    2. ELDER: xưng 'cháu', gọi 'Bác [Tên]' (hoặc 'Bác' nếu không có tên).
    3. CAREGIVER / ADMIN: xưng hô trung tính, lịch sự ('bạn [Tên]' hoặc 'bạn'), TUYỆT ĐỐI không gọi là 'Bác'.
    4. CAREGIVER: chỉ gán người cao tuổi quản lý (managed_elder) khi quan hệ đã được xác minh qua can_access_elder().
       - Không suy đoán danh tính người gọi từ elder_id.
       - Không mặc định elder_id = 1, không tự chọn assigned_elders[0] khi chưa xác minh.
       - Không cho phép truy cập hồ sơ chỉ vì elder_id bằng ID của người gọi.
    5. Không tìm thấy tài khoản hoặc role không xác định: dùng lời chào trung tính ('bạn'), không gán người cao tuổi nào.
    """
    caller = None
    if current_user:
        caller = current_user
    elif caller_id:
        caller = db.query(User).filter(User.id == caller_id, User.is_active == True).first()
    else:
        caller = None

    if not caller or caller.role not in [Role.ELDER, Role.CAREGIVER, Role.ADMIN]:
        # Không suy đoán danh tính người gọi: caller vẫn là None, không có quyền truy cập hồ sơ thuốc
        # Nhưng nhận diện đúng thiết bị kiosk của elder cho các tác vụ ghi nhận (báo triệu chứng, xác nhận uống thuốc, cứu hộ)
        device_elder = None
        if elder_id:
            cand = db.query(User).filter(User.id == elder_id, User.is_active == True).first()
            if cand and cand.role == Role.ELDER:
                device_elder = cand

        text_lower = (user_text or "").lower()
        is_speaking_as_elder = any(w in text_lower for w in ["bác", "ông", "bà", "cụ", "cháu ơi", "cháu à", "chào cháu"])

        display_name = "bạn"
        managed_name = None
        target_id = None
        if device_elder:
            cleaned_e = clean_person_name(device_elder.full_name)
            managed_name = f"Bác {cleaned_e}" if cleaned_e else "Bác"
            target_id = device_elder.id
            if is_speaking_as_elder:
                display_name = managed_name

        return {
            "user": None,
            "role": None,
            "is_elder": bool(device_elder and is_speaking_as_elder),
            "caller_display_name": display_name,
            "managed_elder": device_elder,
            "managed_elder_name": managed_name,
            "target_elder_id": target_id,
            "access_denied": True if elder_id is not None else False,
            "invalid_elder_target": False
        }

    cleaned = clean_person_name(caller.full_name)

    # 1. Người cao tuổi (ELDER)
    if caller.role == Role.ELDER:
        display_name = f"Bác {cleaned}" if cleaned else "Bác"
        # Người cao tuổi chỉ được truy cập dữ liệu của chính mình
        if elder_id is not None and elder_id != caller.id:
            return {
                "user": caller,
                "role": Role.ELDER,
                "is_elder": True,
                "caller_display_name": display_name,
                "managed_elder": None,
                "managed_elder_name": None,
                "target_elder_id": None,
                "access_denied": True,
                "invalid_elder_target": False
            }
        return {
            "user": caller,
            "role": Role.ELDER,
            "is_elder": True,
            "caller_display_name": display_name,
            "managed_elder": caller,
            "managed_elder_name": display_name,
            "target_elder_id": caller.id,
            "access_denied": False,
            "invalid_elder_target": False
        }

    # 2. Người chăm sóc (CAREGIVER) hoặc Quản trị viên (ADMIN)
    caller_display = f"bạn {cleaned}" if cleaned else "bạn"
    managed_elder = None
    access_denied = False
    invalid_elder_target = False

    accessible_elders = []
    if caller.role == Role.ADMIN:
        accessible_elders = db.query(User).filter(User.role == Role.ELDER, User.is_active == True).all()
    elif hasattr(caller, "assigned_elders") and caller.assigned_elders:
        for e in caller.assigned_elders:
            if caller.can_access_elder(e.id):
                accessible_elders.append(e)

    # Nếu caller truyền elder_id cụ thể
    if elder_id is not None:
        if elder_id == caller.id:
            # Caregiver gửi ID của chính mình -> không phải hồ sơ người cao tuổi
            invalid_elder_target = True
            # Kiểm tra xem trong lời nói có nhắc đích danh tên người cao tuổi nào được phân quyền không
            text_lower = (user_text or "").lower()
            for elder_candidate in accessible_elders:
                cand_clean = clean_person_name(elder_candidate.full_name).lower()
                first_name = cand_clean.split()[-1] if cand_clean else ""
                if (cand_clean and cand_clean in text_lower) or (first_name and len(first_name) >= 2 and first_name in text_lower):
                    managed_elder = elder_candidate
                    invalid_elder_target = False
                    break
        else:
            # Truy vấn elder_id cụ thể khác caller.id
            if caller.can_access_elder(elder_id):
                target_user = db.query(User).filter(User.id == elder_id, User.role == Role.ELDER).first()
                if target_user:
                    managed_elder = target_user
                else:
                    access_denied = True
            else:
                access_denied = True
    else:
        # Không truyền elder_id:
        # Tuyệt đối không tự ý chọn mặc định (không gán accessible_elders[0])
        # Chỉ nhận diện nếu lời nói có nhắc tên người cao tuổi được phân quyền
        text_lower = (user_text or "").lower()
        for elder_candidate in accessible_elders:
            cand_clean = clean_person_name(elder_candidate.full_name).lower()
            first_name = cand_clean.split()[-1] if cand_clean else ""
            if (cand_clean and cand_clean in text_lower) or (first_name and len(first_name) >= 2 and first_name in text_lower):
                managed_elder = elder_candidate
                break

    managed_elder_name = None
    target_elder_id = None
    if managed_elder:
        m_clean = clean_person_name(managed_elder.full_name)
        managed_elder_name = f"Bác {m_clean}" if m_clean else "Bác"
        target_elder_id = managed_elder.id

    return {
        "user": caller,
        "role": caller.role,
        "is_elder": False,
        "caller_display_name": caller_display,
        "managed_elder": managed_elder,
        "managed_elder_name": managed_elder_name,
        "target_elder_id": target_elder_id,
        "access_denied": access_denied,
        "invalid_elder_target": invalid_elder_target
    }


@router.post("/fast-reply")
async def fast_ai_reply(
    req: FastAIChatRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user)
):
    """
    API phản hồi AI siêu tốc (< 100ms) chuẩn người Việt dành riêng cho Người cao tuổi & Người chăm sóc:
    1. Chào hỏi lễ phép, ấm áp, thưa gửi kính cẩn với Người cao tuổi (xưng 'cháu', gọi 'Bác [Tên]').
    2. Xưng hô trung tính, lịch sự, tự nhiên với Người chăm sóc (xưng 'tôi/em', gọi 'bạn [Tên]'), không gọi là 'Bác'.
    3. Nhận diện chính xác 100% mọi triệu chứng mệt mỏi, đau nhức và lưu theo ngày.
    4. Trả lời giờ giấc, ngày tháng thật theo thời gian thực.
    5. Trích xuất chính xác đơn thuốc đã được phân quyền để hướng dẫn cụ thể từng cữ.
    6. Hướng dẫn cách uống thuốc, ăn uống đúng cách, động viên tinh thần.
    """
    user_text = req.message.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Thiếu câu hỏi")

    text_clean = user_text.lower()
    user_context = resolve_chat_user_context(
        db=db,
        caller_id=req.caller_id or req.user_id,
        elder_id=req.elder_id,
        user_text=user_text,
        current_user=current_user
    )
    caller = user_context["user"]
    is_elder = user_context["is_elder"]
    caller_display_name = user_context["caller_display_name"]
    managed_elder = user_context["managed_elder"]
    managed_elder_name = user_context["managed_elder_name"]
    target_elder_id = user_context["target_elder_id"]

    elder_display_name = caller_display_name if is_elder else (managed_elder_name or "Bác")
    elder_name = caller_display_name if is_elder else (managed_elder_name or caller_display_name)

    now = datetime.now()
    today_str = now.date().isoformat()
    now_time = now.strftime("%H:%M")
    agent_info = get_agent_info(req.voice)

    # ==================================================================
    # 0. BÁO ĐỘNG ĐỎ CỨU HỘ KHẨN CẤP (EMERGENCY / TÉ NGÃ / KÊU CỨU)
    # ==================================================================
    emergency_keywords = [
        "cứu tôi", "cứu với", "cứu cháu", "cứu bác", "cứu tôi với", 
        "cấp cứu", "khẩn cấp", "bị ngã", "té ngã", "ngã rồi", 
        "ngã cầu thang", "tai nạn", "chảy máu nhiều", "ngất xỉu", "đột quỵ", "bất tỉnh",
        "cứu bà", "cứu ông"
    ]
    if any(k in text_clean for k in emergency_keywords):
        log_elder_id = target_elder_id or (caller.id if (caller and caller.role == Role.ELDER) else None)
        if log_elder_id:
            symptom_log = SymptomLog(
                elder_id=log_elder_id,
                symptom_name="YÊU CẦU CỨU HỘ KHẨN CẤP (Té ngã / Kêu cứu)",
                severity="Khẩn cấp",
                description=f"{caller_display_name} kêu cứu khẩn cấp qua AI: \"{user_text}\"",
                reported_date=today_str,
                reported_time=now_time,
                caregiver_notified=True,
                caregiver_confirmed=False,
                action_taken=f"AI đã kích hoạt BÁO ĐỘNG ĐỎ ngày {today_str} và gọi khẩn tới gia đình.",
                timestamp=utc_now()
            )
            db.add(symptom_log)

            audit = InteractionLog(
                elder_id=log_elder_id,
                event_type="alert",
                response_content=user_text,
                notes=f"AI Voice phát hiện BÁO ĐỘNG ĐỎ ngày {today_str}: {user_text}",
                timestamp=utc_now()
            )
            db.add(audit)
            db.commit()

            broadcast_sync("symptom_alert", {
                "event": "emergency_triggered",
                "symptom": symptom_log.to_dict(),
                "message": f"🚨🚨 BÁO ĐỘNG ĐỎ: {elder_name} đang kêu cứu khẩn cấp lúc {now_time}!",
                "timestamp": utc_now_iso()
            })

            telegram_msg = (
                f"🚨🚨 <b>BÁO ĐỘNG ĐỎ CỨU HỘ KHẨN CẤP!</b>\n\n"
                f"👤 <b>Đối tượng:</b> {elder_name}\n"
                f"📅 <b>Thời gian:</b> {today_str} lúc {now_time}\n"
                f"⚠️ <b>Tình trạng:</b> YÊU CẦU CỨU HỘ KHẨN CẤP (Té ngã / Kêu cứu)\n"
                f"💬 <b>Câu kêu cứu:</b> \"{user_text}\"\n\n"
                f"👉 <i>ĐỀ NGHỊ GIA ĐÌNH VÀ CỨU HỘ LIÊN HỆ KIỂM TRA NGAY LẬP TỨC!</i>"
            )
            asyncio.create_task(send_telegram_alert(telegram_msg))

            try:
                asyncio.create_task(zalo_service.send_text_message(
                    elder_id=log_elder_id,
                    text=f"🚨🚨 [BÁO ĐỘNG ĐỎ] {elder_name} kêu cứu: '{user_text}' lúc {now_time}. Gia đình hãy gọi ngay cho Bác!"
                ))
            except Exception:
                pass

        if is_elder:
            reply_text = (
                "Dạ thưa Bác! Cháu đã phát tín hiệu báo động đỏ khẩn cấp tới toàn thể con cháu và gia đình rồi ạ! "
                "Bác hãy cố gắng giữ bình tĩnh, ngồi hoặc nằm yên tại chỗ, không cử động mạnh. "
                "Gia đình và người thân đang gọi điện cho Bác ngay lập tức đây ạ!"
            )
        else:
            reply_text = (
                f"Dạ chào {caller_display_name}! Hệ thống đã phát tín hiệu báo động đỏ khẩn cấp tới gia đình và đội ngũ hỗ trợ rồi ạ! "
                "Bạn hãy cố gắng giữ bình tĩnh, hỗ trợ người thân tại chỗ và không di chuyển mạnh. "
                "Đội ngũ hỗ trợ đang liên hệ ngay lập tức đây ạ!"
            )
        audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
        return {
            "success": True,
            "reply": reply_text,
            "intent": "EMERGENCY",
            "agent_name": agent_info["name"],
            "agent_role": agent_info["title"],
            "is_emergency": True,
            "symptom_detected": True,
            "symptom_name": "YÊU CẦU CỨU HỘ KHẨN CẤP (Té ngã / Kêu cứu)",
            "severity": "Khẩn cấp",
            "reported_date": today_str,
            "reported_time": now_time,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    # ==================================================================
    # 1. KIỂM TRA HÀNH VI NGUY HIỂM (UỐNG BÙ, TĂNG LIỀU, BỎ CỮ)
    # ==================================================================
    for pattern, reason in FORBIDDEN_PATTERNS:
        if pattern in text_clean:
            asyncio.create_task(send_telegram_alert(
                f"⚠️ <b>CẢNH BÁO AN TOÀN DÙNG THUỐC!</b>\n"
                f"👤 <b>Người hỏi:</b> {caller_display_name}\n"
                f"Nội dung: \"{user_text}\"\n"
                f"Lý do: {reason}"
            ))

            if is_elder:
                reply_text = (
                    f"Dạ thưa Bác, Bác tuyệt đối đừng tự ý thay đổi liều lượng thuốc nhé ạ. {reason} "
                    f"Cháu đang gửi thông báo cho người nhà để kiểm tra và gọi điện hỗ trợ Bác ngay đây ạ!"
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, bạn tuyệt đối đừng tự ý thay đổi liều lượng thuốc nhé ạ. {reason} "
                    "Hệ thống đang ghi nhận thông tin để kiểm tra và hỗ trợ bạn ngay đây ạ!"
                )
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "DANGEROUS_REQUEST",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

    # ==================================================================
    # 2. KIỂM TRA TRIỆU CHỨNG SỨC KHỎE -> LƯU THEO NGÀY & BÁO NGƯỜI NHÀ
    # ==================================================================
    detected_symptom = None
    severity = "Trung bình"
    for pattern, desc, sev in EXTENDED_SYMPTOMS:
        if pattern in text_clean:
            detected_symptom = desc
            severity = sev
            break

    if detected_symptom:
        log_elder_id = target_elder_id or (caller.id if (caller and caller.role == Role.ELDER) else None)
        if log_elder_id:
            symptom_log = SymptomLog(
                elder_id=log_elder_id,
                symptom_name=detected_symptom,
                severity=severity,
                description=f"{caller_display_name} trao đổi với AI: \"{user_text}\"",
                reported_date=today_str,
                reported_time=now_time,
                caregiver_notified=True,
                caregiver_confirmed=False,
                action_taken=f"AI đã ghi nhận vào sổ theo dõi ngày {today_str} và phát tín hiệu khẩn tới gia đình.",
                timestamp=utc_now()
            )
            db.add(symptom_log)

            audit = InteractionLog(
                elder_id=log_elder_id,
                event_type="alert",
                response_content=user_text,
                notes=f"AI Voice phát hiện triệu chứng ngày {today_str}: {detected_symptom}",
                timestamp=utc_now()
            )
            db.add(audit)
            db.commit()

            broadcast_sync("symptom_alert", {
                "event": "symptom_reported",
                "symptom": symptom_log.to_dict(),
                "message": f"🚨 CẢNH BÁO: {elder_name} vừa báo triệu chứng [{detected_symptom}] lúc {now_time}!",
                "timestamp": utc_now_iso()
            })

            telegram_msg = (
                f"🚨 <b>CẢNH BÁO SỨC KHỎE!</b>\n\n"
                f"👤 <b>Đối tượng:</b> {elder_name}\n"
                f"📅 <b>Ngày phát sinh:</b> {today_str} lúc {now_time}\n"
                f"⚠️ <b>Triệu chứng:</b> {detected_symptom} (Mức độ: {severity})\n"
                f"💬 <b>Nội dung:</b> \"{user_text}\"\n\n"
                f"👉 <i>Đề nghị kiểm tra và thăm hỏi ngay!</i>"
            )
            asyncio.create_task(send_telegram_alert(telegram_msg))

            try:
                asyncio.create_task(zalo_service.send_text_message(
                    elder_id=log_elder_id,
                    text=f"🚨 [CẢNH BÁO SỨC KHỎE] {elder_name} vừa báo triệu chứng '{detected_symptom}' lúc {now_time}. Gia đình hãy mở MediGuard AI để kiểm tra ngay!"
                ))
            except Exception:
                pass

        if is_elder:
            reply_text = (
                f"Dạ thưa Bác, cháu đã lưu triệu chứng '{detected_symptom}' vào sổ sức khỏe ngày hôm nay, "
                f"đồng thời đã gửi thông báo khẩn cấp tới con cháu trong gia đình rồi ạ. "
                f"Bác vui lòng ngồi nghỉ ngơi tại chỗ, uống một ngụm nước ấm và giữ bình tĩnh, con cháu sẽ gọi điện cho Bác ngay đây ạ!"
            )
        else:
            if managed_elder_name:
                reply_text = (
                    f"Dạ chào {caller_display_name}, hệ thống đã lưu triệu chứng '{detected_symptom}' của {managed_elder_name} vào sổ theo dõi ngày hôm nay và phát thông báo rồi ạ. "
                    f"Bạn hãy theo dõi sát tình trạng sức khỏe của {managed_elder_name} nhé ạ!"
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, hệ thống đã lưu triệu chứng '{detected_symptom}' vào sổ theo dõi sức khỏe ngày hôm nay rồi ạ. "
                    "Bạn hãy chú ý theo dõi và nghỉ ngơi nhé ạ!"
                )

        # Kiểm tra nếu câu nói vừa có triệu chứng vừa có nội dung tâm sự/hỏi han
        chat_triggers = [
            "kể chuyện", "chuyện", "tâm sự", "nói chuyện", "nhớ", "buồn", "cô đơn",
            "cháu ơi", "hỏi", "đỡ đau", "cho vui", "làng quê", "cổ tích", "câu đố",
            "thế nào", "tại sao", "sao lại", "ở quê", "bầu bạn"
        ]
        has_chat = any(w in text_clean for w in chat_triggers) or len(user_text.split()) >= 8
        if has_chat:
            if is_elder:
                chat_instruction = (
                    f"Bác vừa báo triệu chứng [{detected_symptom}]. Cháu đã ghi nhận vào sổ theo dõi và gửi thông báo khẩn cấp tới gia đình. "
                    f"Đồng thời Bác vừa nói: \"{user_text}\". Hãy mở đầu bằng việc ân cần dặn Bác ngồi nghỉ ngơi uống nước ấm vì gia đình đang gọi điện cho Bác, "
                    f"sau đó trả lời phần trò chuyện, tâm sự, kể chuyện của Bác thật ấm áp, tình cảm trong 2 đến 3 câu ngắn."
                )
            else:
                chat_instruction = (
                    f"Người dùng ({caller_display_name}) vừa báo triệu chứng [{detected_symptom}]. "
                    f"Đồng thời người dùng nói: \"{user_text}\". Hãy trả lời tư vấn ngắn gọn, lịch sự, ân cần trong 2 đến 3 câu."
                )
            history_id = target_elder_id or (caller.id if caller else None)
            history = get_conversation_history(history_id, req.session_id) if history_id else []
            llm_name = caller_display_name if is_elder else "bạn"
            comp_reply = await get_smart_llm_reply(
                chat_instruction,
                elder_name=llm_name,
                voice=req.voice,
                history=history
            )
            if comp_reply and len(comp_reply) >= 15:
                if is_elder:
                    reply_text = (
                        f"Dạ thưa Bác, cháu đã lưu triệu chứng '{detected_symptom}' vào sổ sức khỏe ngày hôm nay và phát tín hiệu khẩn cấp tới gia đình rồi ạ. "
                        f"Bác nhớ ngồi nghỉ ngơi tại chỗ, uống một ngụm nước ấm nhé. {comp_reply}"
                    )
                else:
                    reply_text = (
                        f"Dạ chào {caller_display_name}, hệ thống đã ghi nhận triệu chứng '{detected_symptom}'. {comp_reply}"
                    )
            if history_id:
                save_conversation_turn(history_id, user_text, reply_text, req.session_id)
        audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
        return {
            "success": True,
            "reply": reply_text,
            "intent": "REPORT_SYMPTOM",
            "agent_name": agent_info["name"],
            "agent_role": agent_info["title"],
            "is_emergency": True,
            "symptom_detected": True,
            "symptom_name": detected_symptom,
            "reported_date": today_str,
            "reported_time": now_time,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    # ==================================================================
    # 3. XÁC NHẬN ĐÃ UỐNG THUỐC (CONFIRM INTAKE)
    # ==================================================================
    intake_keywords = [
        "uống rồi", "đã uống", "uống xong", "bác đã uống", "tôi đã uống", 
        "ông đã uống", "bà đã uống", "uống hết rồi", "xong cữ rồi", "uống xong rồi", 
        "bác uống rồi", "uống thuốc rồi", "đã dùng thuốc", "uống rồi nhé", "tao uống rồi", 
        "vừa uống thuốc", "uống hết thuốc", "đã uống cữ", "uống rồi con", "uống rồi cháu",
        "đã nghe", "nghe rồi"
    ]
    if any(k in text_clean for k in intake_keywords) or ("uống" in text_clean and any(r in text_clean for r in ["rồi", "xong", "hết"])):
        log_elder_id = target_elder_id or (caller.id if (caller and caller.role == Role.ELDER) else None)
        active_sch = None
        if log_elder_id:
            active_sch = db.query(Schedule).filter(Schedule.elder_id == log_elder_id, Schedule.is_active == True).first()
            log_entry = InteractionLog(
                schedule_id=active_sch.id if active_sch else None,
                elder_id=log_elder_id,
                event_type="user_responded",
                response_content=user_text,
                notes="Xác nhận uống thuốc thành công qua trợ lý giọng nói siêu tốc",
                timestamp=utc_now()
            )
            db.add(log_entry)
            db.commit()

            broadcast_sync("user_responded", {
                "elder_id": log_elder_id,
                "response": "Đã uống thuốc",
                "timestamp": utc_now_iso()
            })

        if is_elder:
            reply_text = (
                "Dạ thưa Bác, cháu đã ghi nhận Bác vừa uống xong thuốc cữ này rồi ạ! "
                "Cháu đã cập nhật nhật ký và gửi thông báo an tâm cho con cháu trong gia đình. "
                "Bác nhớ uống thêm một chút nước ấm và nghỉ ngơi thật thoải mái nhé ạ!"
            )
        else:
            if managed_elder_name:
                reply_text = (
                    f"Dạ chào {caller_display_name}, hệ thống đã ghi nhận {managed_elder_name} đã uống xong cữ thuốc này rồi ạ! "
                    "Thông tin đã được cập nhật vào nhật ký sức khỏe."
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, hệ thống đã ghi nhận xác nhận đã uống thuốc cữ này rồi ạ!"
                )
        audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
        return {
            "success": True,
            "reply": reply_text,
            "intent": "CONFIRM_INTAKE",
            "agent_name": agent_info["name"],
            "agent_role": agent_info["title"],
            "is_emergency": False,
            "symptom_detected": False,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    # ==================================================================
    # 4. CHÀO HỎI & XƯNG HÔ THÂN MẬT (GREETINGS - CHỈ KHI CHÀO ĐƠN THUẦN)
    # ==================================================================
    greeting_keywords = [
        "chào cháu", "xin chào", "bác chào cháu", "chào em", 
        "chào bạn", "hello", "hi", "bác đây", "ông đây", "bà đây"
    ]
    is_pure_greeting = (
        text_clean in greeting_keywords or 
        text_clean in ["chào", "alo", "ơi"] or
        (len(text_clean) <= 15 and any(text_clean.startswith(k) or text_clean == k for k in greeting_keywords))
    ) and not any(w in text_clean for w in [
        "thời tiết", "mưa", "nắng", "mấy giờ", "thuốc", "uống", "đau", "mệt", "sốt", 
        "buồn", "sao", "thế nào", "như thế nào", "gì", "kể chuyện", "chuyện vui", "ăn gì"
    ])
    if is_pure_greeting:
        if is_elder:
            reply_text = (
                f"Dạ, cháu chào {caller_display_name} ạ! Cháu là trợ lý sức khỏe MediGuard luôn túc trực bên Bác đây ạ. "
                "Hôm nay Bác cảm thấy trong người khỏe không ạ? Bác có cần cháu hỗ trợ gì về lịch uống thuốc không ạ?"
            )
        else:
            if managed_elder_name:
                reply_text = (
                    f"Dạ, xin chào {caller_display_name}! Tôi là trợ lý sức khỏe MediGuard AI. "
                    f"Bạn có cần hỗ trợ gì về việc theo dõi sức khỏe và lịch thuốc cho {managed_elder_name} hôm nay không ạ?"
                )
            else:
                reply_text = (
                    f"Dạ, xin chào {caller_display_name}! Tôi là trợ lý sức khỏe MediGuard AI. "
                    "Bạn cần hỗ trợ thông tin gì về sức khỏe và chăm sóc người thân hôm nay ạ?"
                )
        audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
        return {
            "success": True,
            "reply": reply_text,
            "intent": "GREETING",
            "agent_name": agent_info["name"],
            "agent_role": agent_info["title"],
            "is_emergency": False,
            "symptom_detected": False,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    # ==================================================================
    # 5. DANH TÍNH & CHỨC NĂNG ỨNG DỤNG (IDENTITY)
    # ==================================================================
    identity_keywords = [
        "cháu là ai", "cháu tên gì", "tên cháu là gì", "tên là gì", "bạn là ai", 
        "đây là app gì", "đây là ứng dụng gì", "cháu làm được gì", "giúp bác cái gì", 
        "chức năng gì", "hướng dẫn bác", "cháu là gì", "ứng dụng này làm gì", "máy này làm gì"
    ]
    if any(k in text_clean for k in identity_keywords):
        if is_elder:
            reply_text = (
                f"Dạ thưa {caller_display_name}, cháu là Trợ lý Y tế Thông minh MediGuard AI dành riêng cho Bác ạ! "
                "Cháu có thể nhắc Bác uống thuốc đúng giờ, đọc chi tiết các cữ thuốc trong ngày, "
                "hỗ trợ kiểm tra viên thuốc bằng camera, lưu triệu chứng mệt mỏi và báo ngay cho con cháu khi Bác cần hỗ trợ ạ."
            )
        else:
            if managed_elder_name:
                reply_text = (
                    f"Dạ chào {caller_display_name}, tôi là Trợ lý Y tế Thông minh MediGuard AI đồng hành chăm sóc sức khỏe cho {managed_elder_name}. "
                    "Tôi có thể hỗ trợ bạn theo dõi lịch uống thuốc, nhận cảnh báo triệu chứng khẩn cấp và tra cứu thông tin y tế an toàn ạ."
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, tôi là Trợ lý Y tế Thông minh MediGuard AI. "
                    "Tôi có thể hỗ trợ theo dõi lịch uống thuốc, nhận cảnh báo sức khỏe và tra cứu thông tin dùng thuốc an toàn cho người thân ạ."
                )
        audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
        return {
            "success": True,
            "reply": reply_text,
            "intent": "IDENTITY",
            "agent_name": agent_info["name"],
            "agent_role": agent_info["title"],
            "is_emergency": False,
            "symptom_detected": False,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    # ==================================================================
    # 6. HỎI THỜI GIAN, NGÀY THÁNG, THỨ MẤY (TIME & DATE)
    # ==================================================================
    time_keywords = [
        "mấy giờ rồi", "bây giờ là mấy giờ", "mấy giờ", "ngày mấy", 
        "hôm nay ngày mấy", "thứ mấy", "hôm nay thứ mấy", "năm nay là năm bao nhiêu"
    ]
    if any(k in text_clean for k in time_keywords) and not ("uống" in text_clean or "thuốc" in text_clean):
        days_vn = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
        weekday_name = days_vn[now.weekday()]
        if is_elder:
            reply_text = (
                f"Dạ thưa {caller_display_name}, bây giờ là {now.hour} giờ {now.minute} phút, {weekday_name} "
                f"ngày {now.day} tháng {now.month} năm {now.year} ạ. "
                f"Bác có muốn cháu kiểm tra xem đã đến cữ uống thuốc tiếp theo chưa ạ?"
            )
        else:
            reply_text = (
                f"Dạ chào {caller_display_name}, bây giờ là {now.hour} giờ {now.minute} phút, {weekday_name} "
                f"ngày {now.day} tháng {now.month} năm {now.year} ạ."
            )
        audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
        return {
            "success": True,
            "reply": reply_text,
            "intent": "INQUIRE_TIME",
            "agent_name": agent_info["name"],
            "agent_role": agent_info["title"],
            "is_emergency": False,
            "symptom_detected": False,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    # ==================================================================
    # 7. HỎI LỊCH UỐNG THUỐC, KẾ HOẠCH HÔM NAY & TẤT CẢ LOẠI THUỐC ĐANG UỐNG
    # ==================================================================
    routine_keywords = [
        "hôm nay tôi cần phải làm gì", "hôm nay tôi phải làm gì", "hôm nay cần làm gì",
        "hôm nay làm gì", "tôi cần phải làm gì", "tôi cần làm gì", "cần phải làm gì",
        "phải làm gì", "cần làm gì", "làm gì hôm nay", "việc hôm nay", "lịch hôm nay",
        "nhiệm vụ hôm nay", "kế hoạch hôm nay", "hôm nay có việc gì", "hôm nay tôi làm gì",
        "làm những gì", "cần làm những gì", "phải làm những gì", "việc cần làm",
        "việc cần làm hôm nay", "hôm nay có gì", "cần làm cái gì", "hôm nay làm việc gì"
    ]
    is_routine_inquiry = any(k in text_clean for k in routine_keywords)

    schedule_keywords = [
        "mấy giờ uống thuốc", "khi nào uống thuốc", "lịch uống thuốc", "giờ uống thuốc", 
        "cữ sáng", "cữ trưa", "cữ chiều", "cữ tối", "hôm nay uống mấy cữ", 
        "uống thuốc lúc mấy giờ", "mấy giờ uống", "hôm nay uống gì", "nhắc thuốc", 
        "lịch thuốc", "những thuốc gì", "thuốc gì", "mấy viên", "uống thế nào", "cữ này",
        "tất cả loại thuốc", "tất cả các loại thuốc", "các loại thuốc", "thuốc đang uống",
        "danh sách thuốc", "đơn thuốc", "thuốc của tôi", "thuốc của bác", "tôi đang uống",
        "bác đang uống", "kể tên thuốc", "uống những thuốc nào", "có những thuốc nào",
        "toàn bộ thuốc", "tất cả thuốc"
    ]
    is_med_inquiry = (
        is_routine_inquiry
        or any(k in text_clean for k in schedule_keywords)
        or ("thuốc" in text_clean and any(w in text_clean for w in ["tất cả", "đang uống", "các loại", "danh sách", "toàn bộ", "những loại", "kể tên", "những gì", "uống gì"]))
        or ("uống" in text_clean and any(w in text_clean for w in ["tất cả", "loại nào", "những thuốc", "thuốc gì"]))
        or "đơn thuốc" in text_clean
    )
    if is_med_inquiry:
        # 1. Từ chối truy cập nếu không có quyền (unauthorized / IDOR / chưa xác thực nhưng gửi elder_id)
        if user_context.get("access_denied"):
            reply_text = f"Dạ chào {caller_display_name}, bạn không có quyền truy cập thông tin y tế của người cao tuổi này ạ."
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "INQUIRE_DAILY_ROUTINE" if is_routine_inquiry else "INQUIRE_MEDICATION",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

        # 2. Caregiver gửi ID của chính mình thay vì ID của người cao tuổi
        if user_context.get("invalid_elder_target"):
            reply_text = (
                f"Dạ chào {caller_display_name}, mã hồ sơ bạn gửi là tài khoản của người chăm sóc, "
                f"không phải hồ sơ của người cao tuổi. Vui lòng chọn đúng người cao tuổi cần tra cứu ạ."
            )
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "INQUIRE_DAILY_ROUTINE" if is_routine_inquiry else "INQUIRE_MEDICATION",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

        # 3. Request thiếu xác thực người dùng
        if not caller:
            reply_text = (
                f"Dạ chào {caller_display_name}, bạn vui lòng đăng nhập để truy cập thông tin "
                f"lịch trình và đơn thuốc ạ."
            )
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "INQUIRE_DAILY_ROUTINE" if is_routine_inquiry else "INQUIRE_MEDICATION",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

        # 4. Người chăm sóc chưa chọn hoặc chưa được liên kết với hồ sơ người cao tuổi
        if caller.role in [Role.CAREGIVER, Role.ADMIN] and not target_elder_id:
            if is_routine_inquiry:
                reply_text = (
                    f"Dạ chào {caller_display_name}, hiện tại bạn chưa chọn hoặc chưa được liên kết với "
                    f"hồ sơ người cao tuổi nào để xem kế hoạch chăm sóc ạ."
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, hiện tại bạn chưa chọn hồ sơ người cao tuổi cần tra cứu "
                    f"hoặc chưa liên kết với người cao tuổi nào trong hệ thống ạ."
                )
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "INQUIRE_DAILY_ROUTINE" if is_routine_inquiry else "INQUIRE_MEDICATION",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

        schedules = []
        meds = []
        if target_elder_id:
            schedules = db.query(Schedule).filter(
                Schedule.elder_id == target_elder_id,
                Schedule.is_active == True
            ).all()
            meds = db.query(Medication).filter(
                Medication.elder_id == target_elder_id,
                Medication.is_active == True
            ).all()

        if is_routine_inquiry:
            steps_list = [
                "Bác hãy nghỉ ngơi thoải mái, tránh xúc động hay làm việc nặng." if is_elder else "Nghỉ ngơi thoải mái, tránh làm việc nặng.",
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

            if is_elder:
                reply_text = (
                    f"Dạ thưa {caller_display_name}, hôm nay việc quan trọng của Bác là: "
                    f"Buổi sáng Bác nhớ uống một ly nước ấm và đo lại huyết áp. "
                    f"Về phần thuốc hôm nay, Bác cần uống {med_summary}. "
                    f"Bác ăn uống thanh đạm, nghỉ ngơi thư thái và vận động nhẹ nhàng nhé Bác! "
                    f"Cháu luôn ở bên Bác và đã gửi thông báo an tâm cho người thân của Bác rồi ạ."
                )
            else:
                if managed_elder_name:
                    reply_text = (
                        f"Dạ chào {caller_display_name}, kế hoạch chăm sóc hôm nay cho {managed_elder_name} gồm có: "
                        f"Nhắc {managed_elder_name} uống một ly nước ấm và đo lại huyết áp buổi sáng. "
                        f"Về phần thuốc hôm nay cần uống: {med_summary}."
                    )
                else:
                    reply_text = (
                        f"Dạ chào {caller_display_name}, hiện tại chưa có thông tin lịch trình hoặc kế hoạch chăm sóc nào trong hệ thống ạ."
                    )
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "INQUIRE_DAILY_ROUTINE",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "steps": steps_list,
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

        if meds:
            # Nhóm thuốc theo cữ rõ ràng cho người cao tuổi
            by_period = {}
            for m in meds:
                p = (m.time_period or "Hàng ngày").capitalize()
                cat_suffix = f" ({m.category})" if getattr(m, 'category', None) else ""
                by_period.setdefault(p, []).append(f"{m.name} liều {m.dosage or '1 viên'}{cat_suffix}")

            period_summaries = []
            for p, items in by_period.items():
                period_summaries.append(f"cữ {p.lower()} gồm có {', '.join(items)}")

            summary_str = "; ".join(period_summaries)
            if is_elder:
                reply_text = (
                    f"Dạ thưa {caller_display_name}, hiện tại trong danh mục thuốc Bác đang uống gồm có {len(meds)} loại: {summary_str}. "
                    f"Bác nhớ uống đúng giờ theo chuông nhắc và uống cùng nước lọc ấm nhé ạ!"
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, danh mục thuốc hiện tại của {managed_elder_name or 'người thân'} gồm có {len(meds)} loại: {summary_str}."
                )
        elif schedules:
            sch_summaries = [f"cữ {s.time_period.lower()} lúc {s.target_time} tại khay số 0{s.compartment_number or 1}" for s in schedules]
            if is_elder:
                reply_text = (
                    f"Dạ thưa {caller_display_name}, lịch uống thuốc hôm nay của Bác gồm có: {'; '.join(sch_summaries)}. "
                    f"Bác nhớ uống đúng giờ và uống kèm nước ấm nhé ạ!"
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, lịch uống thuốc hôm nay của {managed_elder_name or 'người thân'} gồm có: {'; '.join(sch_summaries)}."
                )
        else:
            if is_elder:
                reply_text = (
                    f"Dạ thưa {caller_display_name}, hiện tại trong sổ theo dõi của Bác chưa có loại thuốc nào được ghi nhận. "
                    "Bác có thể bảo con cháu cập nhật thêm đơn thuốc vào ứng dụng cho Bác nhé ạ!"
                )
            else:
                if managed_elder_name:
                    reply_text = (
                        f"Dạ chào {caller_display_name}, hiện tại trong sổ theo dõi của {managed_elder_name} chưa có loại thuốc nào được ghi nhận ạ."
                    )
                else:
                    reply_text = (
                        f"Dạ chào {caller_display_name}, hiện tại bạn chưa có người cao tuổi được liên kết hoặc chưa có thông tin thuốc nào được ghi nhận trong hệ thống ạ."
                    )

        audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
        return {
            "success": True,
            "reply": reply_text,
            "intent": "INQUIRE_MEDICATION",
            "agent_name": agent_info["name"],
            "agent_role": agent_info["title"],
            "is_emergency": False,
            "symptom_detected": False,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    # ==================================================================
    # 8. TRA CỨU KIẾN THỨC THUỐC (DRUG LOOKUP) & HỎI ĐƠN THUỐC CÁ NHÂN
    # ==================================================================
    drug_lookup_info = detect_drug_lookup(user_text)
    if drug_lookup_info:
        raw_drug_query = drug_lookup_info["drug_name"]
        aspect = drug_lookup_info["aspect"]
        clean_query = raw_drug_query.lower().strip()
        drug_name_display = raw_drug_query.strip().title()

        try:
            # 8.1 Nhóm sản phẩm chung "thuốc bổ phế" (chưa phải tên một thuốc cụ thể)
            if "bổ phế" in clean_query:
                if is_elder:
                    reply_text = (
                        f"Dạ thưa {caller_display_name}, 'thuốc bổ phế' là tên gọi chung cho nhóm sản phẩm đông dược hoặc siro thảo dược "
                        "có tác dụng nhuận phế, bổ phổi, làm ấm đường hô hấp và giảm các triệu chứng ho, đau rát họng (như siro Bổ phế Nam Hà, Bổ phế Bảo Thanh...). "
                        "Tuy nhiên, đây là nhóm sản phẩm chung chứ chưa phải tên một loại thuốc cụ thể. "
                        "Để cháu hỗ trợ Bác chính xác nhất, Bác có thể cho cháu biết thêm tên đầy đủ của sản phẩm, nhà sản xuất, hoặc nhờ người nhà chụp ảnh bao bì/vỏ hộp gửi lên ứng dụng giúp Bác nhé ạ!"
                    )
                else:
                    reply_text = (
                        f"Dạ chào {caller_display_name}, 'thuốc bổ phế' là tên gọi chung cho nhóm sản phẩm siro hoặc đông dược thảo dược "
                        "có tác dụng nhuận phế, bổ phổi, làm ấm đường hô hấp và hỗ trợ giảm ho, long đờm (như Bổ phế Nam Hà, Bổ phế Bảo Thanh...). "
                        "Vì đây là tên nhóm sản phẩm chứ chưa phải một loại thuốc cụ thể, bạn vui lòng cung cấp tên đầy đủ của sản phẩm, nhà sản xuất, "
                        "hoặc chụp ảnh bao bì để hệ thống tra cứu thông tin chi tiết và chính xác nhé ạ."
                    )
                audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
                return {
                    "success": True,
                    "reply": reply_text,
                    "intent": "DRUG_LOOKUP",
                    "agent_name": agent_info["name"],
                    "agent_role": agent_info["title"],
                    "is_emergency": False,
                    "symptom_detected": False,
                    "drug_info": None,
                    "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
                }

            # 8.2 Tra cứu thuốc trong DRUG_DATABASE qua search_drugs()
            search_res = search_drugs(raw_drug_query)
            candidates = search_res.get("results", []) if search_res else []
            matched_drug = None
            if candidates:
                first = candidates[0]
                if any(d["id"] == first.get("id") for d in DRUG_DATABASE):
                    matched_drug = first

            if matched_drug:
                if aspect == "side_effects":
                    side_effects_text = matched_drug.get("side_effects", "Chưa có ghi nhận tác dụng phụ điển hình.")
                    if is_elder:
                        reply_text = (
                            f"Dạ thưa {caller_display_name}, theo thông tin dược lý chuẩn hóa, tác dụng phụ có thể gặp của thuốc {matched_drug['name']} gồm có: "
                            f"{side_effects_text} "
                            "Nếu Bác gặp các dấu hiệu bất thường này, Bác hãy báo ngay cho người nhà hoặc bác sĩ để được kiểm tra kịp thời nhé ạ!"
                        )
                    else:
                        reply_text = (
                            f"Dạ chào {caller_display_name}, các tác dụng phụ có thể gặp của thuốc {matched_drug['name']} gồm: "
                            f"{side_effects_text} "
                            "Nếu người bệnh xuất hiện các triệu chứng bất thường, hãy tham khảo ý kiến bác sĩ điều trị ạ."
                        )
                elif aspect == "contraindications":
                    contra_text = matched_drug.get("contraindications", "Chưa có thông tin chống chỉ định cụ thể.")
                    if is_elder:
                        reply_text = (
                            f"Dạ thưa {caller_display_name}, thuốc {matched_drug['name']} có các chống chỉ định sau: {contra_text} "
                            "Bác tuyệt đối không tự ý dùng nếu thuộc các trường hợp này nhé ạ!"
                        )
                    else:
                        reply_text = (
                            f"Dạ chào {caller_display_name}, các chống chỉ định của thuốc {matched_drug['name']} gồm: {contra_text} "
                            "Tuyệt đối không sử dụng cho các đối tượng này ạ."
                        )
                elif aspect == "interactions":
                    inter_text = matched_drug.get("interactions", "Chưa có thông tin tương tác thuốc cụ thể.")
                    if is_elder:
                        reply_text = (
                            f"Dạ thưa {caller_display_name}, về tương tác và kiêng kỵ của thuốc {matched_drug['name']}: {inter_text} "
                            "Bác chú ý lưu ý để uống thuốc an toàn nhé ạ!"
                        )
                    else:
                        reply_text = (
                            f"Dạ chào {caller_display_name}, tương tác thuốc của {matched_drug['name']}: {inter_text}."
                        )
                elif aspect == "ingredient":
                    ing_text = matched_drug.get("active_ingredient", "Chưa có thông tin hoạt chất.")
                    if is_elder:
                        reply_text = (
                            f"Dạ thưa {caller_display_name}, thuốc {matched_drug['name']} có hoạt chất chính là: {ing_text}."
                        )
                    else:
                        reply_text = (
                            f"Dạ chào {caller_display_name}, hoạt chất chính của thuốc {matched_drug['name']} là: {ing_text}."
                        )
                else:  # indication hoặc general
                    indication_text = matched_drug.get("indication", "Chưa có thông tin chỉ định chi tiết.")
                    category_text = matched_drug.get("category", "Thuốc điều trị")
                    active_ing = matched_drug.get("active_ingredient", "")
                    ing_part = f" (hoạt chất {active_ing})" if active_ing else ""
                    if is_elder:
                        reply_text = (
                            f"Dạ thưa {caller_display_name}, thuốc {matched_drug['name']}{ing_part} thuộc nhóm {category_text}. "
                            f"Công dụng chính: {indication_text} "
                            "Lưu ý: Thông tin mang tính tham khảo y tế chuẩn hóa, Bác chỉ dùng thuốc theo đúng chỉ định và hướng dẫn của bác sĩ điều trị nhé ạ!"
                        )
                    else:
                        reply_text = (
                            f"Dạ chào {caller_display_name}, thuốc {matched_drug['name']}{ing_part} thuộc nhóm {category_text}. "
                            f"Công dụng chính: {indication_text} "
                            "Lưu ý: Thông tin mang tính tham khảo y tế, cần tuân thủ chỉ định của bác sĩ chuyên khoa ạ."
                        )

                audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
                return {
                    "success": True,
                    "reply": reply_text,
                    "intent": "DRUG_LOOKUP",
                    "agent_name": agent_info["name"],
                    "agent_role": agent_info["title"],
                    "is_emergency": False,
                    "symptom_detected": False,
                    "drug_info": matched_drug,
                    "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
                }

            # 8.3 Trường hợp thuốc không có trong DRUG_DATABASE (không tự bịa đặt bản ghi / công dụng / liều lượng)
            else:
                if is_elder:
                    reply_text = (
                        f"Dạ thưa {caller_display_name}, hiện tại trong cẩm nang thuốc chuẩn hóa của hệ thống chưa có thông tin về '{drug_name_display}' nên chưa thể xác minh thông tin. "
                        "Để đảm bảo an toàn và hỗ trợ Bác chính xác nhất, Bác vui lòng cung cấp tên thuốc chính xác hoặc thành phần trên bao bì "
                        "(hoặc nhờ người nhà chụp ảnh bao bì/vỉ thuốc gửi lên ứng dụng) giúp Bác nhé ạ!"
                    )
                else:
                    reply_text = (
                        f"Dạ chào {caller_display_name}, hiện tại trong cơ sở dữ liệu thuốc chuẩn hóa của hệ thống chưa có thông tin về '{drug_name_display}' nên chưa thể xác minh thông tin. "
                        "Bạn vui lòng cung cấp tên thuốc chính xác hoặc thành phần trên bao bì "
                        "(hoặc chụp ảnh bao bì sản phẩm) để hệ thống hỗ trợ tra cứu chính xác nhé ạ."
                    )
                audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
                return {
                    "success": True,
                    "reply": reply_text,
                    "intent": "DRUG_LOOKUP",
                    "agent_name": agent_info["name"],
                    "agent_role": agent_info["title"],
                    "is_emergency": False,
                    "symptom_detected": False,
                    "drug_info": None,
                    "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
                }

        except Exception as e:
            logger.error("Lỗi khi tra cứu thông tin thuốc: %s", str(e), exc_info=False)
            if is_elder:
                reply_text = (
                    f"Dạ thưa {caller_display_name}, hệ thống tra cứu thông tin thuốc hiện đang gặp gián đoạn tạm thời nên chưa thể tra cứu thông tin cho Bác được ạ. "
                    "Bác vui lòng thử lại sau ít phút hoặc nhờ người nhà kiểm tra trực tiếp trên vỏ hộp thuốc giúp Bác nhé ạ!"
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, hệ thống tra cứu thông tin thuốc hiện đang gặp sự cố kỹ thuật. "
                    "Bạn vui lòng thử lại sau hoặc tham khảo trực tiếp tờ hướng dẫn sử dụng và ý kiến chuyên môn nhé ạ."
                )
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "DRUG_LOOKUP",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "drug_info": None,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

    # ==================================================================
    # 8.5 HỎI VỀ LOẠI THUỐC CỤ THỂ TRONG ĐƠN CỦA NGƯỜI BỆNH (PERSONAL PRESCRIPTION)
    # Chỉ kích hoạt khi thực sự hỏi về thuốc trong đơn, không chặn câu hỏi tư vấn ăn uống, sinh hoạt.
    # ==================================================================
    common_drug_terms = [
        "huyết áp", "tiểu đường", "tim mạch", "dạ dày", "bổ não", "xương khớp", 
        "giảm đau", "panadol", "paracetamol", "amlodipine", "metformin", 
        "aspirin", "omeprazole", "losartan", "atorvastatin", "ginkgo"
    ]
    is_consultation_inquiry = (
        any(w in text_clean for w in [
            "ăn uống", "sinh hoạt", "chế độ", "dinh dưỡng", "kiêng gì", "chăm sóc", "tập gì", "khỏe hơn", "lời khuyên",
            "thời điểm nào tốt nhất", "uống vào lúc nào tốt nhất", "uống lúc nào tốt nhất", "thời điểm nào", "lúc nào tốt nhất",
            "uống cùng lúc với sữa", "uống với sữa", "uống với nước gì", "uống trước hay sau ăn", "lúc đói hay lúc no"
        ])
        or (any(w in text_clean for w in ["thế nào", "như thế nào", "làm sao"]) and not any(k in text_clean for k in ["thuốc", "uống", "đơn"]))
    )
    matched_term = next((term for term in common_drug_terms if term in text_clean), None)
    if matched_term and not is_consultation_inquiry:
        # 1. Từ chối truy cập nếu không có quyền (unauthorized / IDOR / chưa xác thực nhưng gửi elder_id)
        if user_context.get("access_denied"):
            reply_text = f"Dạ chào {caller_display_name}, bạn không có quyền truy cập thông tin đơn thuốc của người cao tuổi này ạ."
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "INQUIRE_SPECIFIC_DRUG",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

        # 2. Caregiver gửi ID của chính mình thay vì ID của người cao tuổi
        if user_context.get("invalid_elder_target"):
            reply_text = (
                f"Dạ chào {caller_display_name}, mã hồ sơ bạn gửi là tài khoản của người chăm sóc, "
                f"không phải hồ sơ của người cao tuổi. Vui lòng chọn đúng người cao tuổi cần tra cứu đơn thuốc ạ."
            )
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "INQUIRE_SPECIFIC_DRUG",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

        # 3. Request thiếu xác thực người dùng
        if not caller:
            reply_text = f"Dạ chào {caller_display_name}, bạn vui lòng đăng nhập để tra cứu thông tin đơn thuốc ạ."
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "INQUIRE_SPECIFIC_DRUG",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

        # 4. Người chăm sóc chưa chọn hoặc chưa được liên kết với hồ sơ người cao tuổi
        if caller.role in [Role.CAREGIVER, Role.ADMIN] and not target_elder_id:
            reply_text = f"Dạ chào {caller_display_name}, hiện tại bạn chưa chọn hồ sơ người cao tuổi để tra cứu thông tin đơn thuốc này ạ."
            audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
            return {
                "success": True,
                "reply": reply_text,
                "intent": "INQUIRE_SPECIFIC_DRUG",
                "agent_name": agent_info["name"],
                "agent_role": agent_info["title"],
                "is_emergency": False,
                "symptom_detected": False,
                "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
            }

        meds = []
        if target_elder_id:
            meds = db.query(Medication).filter(
                Medication.elder_id == target_elder_id,
                Medication.is_active == True
            ).all()
        target_med = next(
            (m for m in meds if matched_term in m.name.lower() or (getattr(m, 'category', None) and matched_term in m.category.lower())),
            None
        )
        if target_med:
            cat_desc = f"thuộc nhóm {target_med.category}" if getattr(target_med, 'category', None) else "hỗ trợ điều trị sức khỏe"
            if is_elder:
                reply_text = (
                    f"Dạ thưa Bác, trong đơn thuốc của Bác có {target_med.name} liều {target_med.dosage}, "
                    f"uống vào cữ {target_med.time_period.lower()} ({target_med.instruction}). "
                    f"Thuốc này {cat_desc}. Bác nhớ uống đúng liều đã ghi trên vỉ thuốc nhé ạ!"
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, trong đơn thuốc của {managed_elder_name or 'người thân'} có {target_med.name} liều {target_med.dosage}, "
                    f"uống vào cữ {target_med.time_period.lower()} ({target_med.instruction}). {cat_desc}."
                )
        else:
            if is_elder:
                reply_text = (
                    f"Dạ thưa Bác, về thuốc liên quan đến {matched_term}, Bác hãy kiểm tra kỹ trên vỉ thuốc "
                    f"hoặc hỏi người nhà xem có trong đơn bác sĩ đã chỉ định chưa nhé. Bác tuyệt đối không tự ý uống thêm thuốc ngoài đơn ạ!"
                )
            else:
                reply_text = (
                    f"Dạ chào {caller_display_name}, về thuốc liên quan đến {matched_term}, bạn hãy kiểm tra lại đơn thuốc đã được bác sĩ chỉ định hoặc hỏi ý kiến chuyên môn nhé ạ."
                )
        audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
        return {
            "success": True,
            "reply": reply_text,
            "intent": "INQUIRE_SPECIFIC_DRUG",
            "agent_name": agent_info["name"],
            "agent_role": agent_info["title"],
            "is_emergency": False,
            "symptom_detected": False,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    # ==================================================================
    # 9. HƯỚNG DẪN CÁCH UỐNG THUỐC, NƯỚC UỐNG, TRƯỚC/SAU ĂN
    # ==================================================================
    ingestion_terms = [
        "uống nước gì", "uống với nước gì", "uống thuốc với nước gì", "với nước gì", "uống bao nhiêu nước", 
        "nước gì", "uống nước nào", "uống sữa", "nước chè", "uống trà", "cà phê", "nước ngọt", 
        "trước ăn", "sau ăn", "lúc đói", "lúc no", "kiêng ăn", "kiêng gì"
    ]
    if any(k in text_clean for k in ingestion_terms):
        if is_elder:
            reply_text = (
                "Dạ thưa Bác, thuốc uống tốt nhất là cùng một ly nước lọc ấm đầy khoảng 150 đến 200ml. "
                "Bác tránh dùng thuốc chung với sữa, trà, cà phê hay nước ngọt vì có thể làm giảm tác dụng của thuốc. "
                "Với các thuốc dặn uống sau ăn, Bác nhớ ăn no rồi hãy uống để bảo vệ dạ dày nhé ạ!"
            )
        else:
            reply_text = (
                f"Dạ chào {caller_display_name}, thuốc uống tốt nhất là dùng cùng một ly nước lọc ấm đầy khoảng 150 đến 200ml. "
                "Nên tránh dùng thuốc chung với sữa, trà, cà phê hay nước ngọt vì có thể làm giảm tác dụng của thuốc. "
                "Đối với các thuốc được dặn uống sau ăn, nên dùng sau khi ăn no để bảo vệ dạ dày ạ."
            )
        audio_file = await resolve_audio_file(reply_text, voice=req.voice, rate=req.rate)
        return {
            "success": True,
            "reply": reply_text,
            "intent": "INGESTION_ADVICE",
            "agent_name": agent_info["name"],
            "agent_role": agent_info["title"],
            "is_emergency": False,
            "symptom_detected": False,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    # ==================================================================
    # 10. TRÍ TUỆ NHÂN TẠO ĐA NĂNG & BẦU BẠN TÂM SỰ (AI LLM & COMPANIONSHIP)
    # Tự động suy nghĩ và phản hồi linh hoạt mọi câu hỏi mở rộng:
    # thời tiết thực tế, tâm sự, bầu bạn, dinh dưỡng, chuyện vui, hỏi thăm.
    # ==================================================================
    med_list = []
    if target_elder_id:
        med_list = db.query(Medication).filter(Medication.elder_id == target_elder_id, Medication.is_active == True).all()
    med_context = [f"{m.name} ({m.dosage or '1 viên'}, cữ {m.time_period})" for m in med_list]

    days_vn = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
    weekday_name = days_vn[now.weekday()]
    time_context = f"{now.hour} giờ {now.minute} phút, {weekday_name} ngày {now.day}/{now.month}/{now.year}"
    weather_context = get_realtime_weather_summary()
    agent_info = get_agent_info(req.voice)

    # 1. Nạp lịch sử hội thoại và kiểm tra nhu cầu tra cứu Internet
    history_id = target_elder_id or (caller.id if caller else None)
    history = get_conversation_history(history_id, req.session_id) if history_id else []
    needs_search = detect_search_need(user_text)

    # 2. Gọi Trí Tuệ Nhân Tạo LLM (Gemini / OpenAI) hoặc Bộ não ngữ cảnh lão khoa
    llm_name = caller_display_name if is_elder else "bạn"
    smart_reply = await get_smart_llm_reply(
        user_text,
        elder_name=llm_name,
        medications=med_context,
        time_context=time_context,
        weather_context=weather_context,
        voice=req.voice,
        use_grounding=needs_search,
        history=history,
        return_metadata=True
    )

    final_reply = ""
    grounding_used = False
    sources = []
    is_llm_online = False

    if isinstance(smart_reply, dict):
        final_reply = smart_reply.get("text") or ""
        grounding_used = smart_reply.get("grounding_used", False)
        sources = smart_reply.get("sources", [])
        is_llm_online = smart_reply.get("model") not in ["contextual_fallback", "offline_fallback", None]
    elif smart_reply:
        final_reply = str(smart_reply)
        is_llm_online = True

    if not final_reply:
        final_reply = get_smart_contextual_fallback(
            user_text=user_text,
            elder_name=llm_name,
            medications=med_context,
            time_context=time_context,
            weather_context=weather_context,
            voice=req.voice,
            history=history,
            search_needed=needs_search
        )

    # 3. Lưu lượt hội thoại vào bộ nhớ phiên
    if history_id:
        save_conversation_turn(history_id, user_text, final_reply, req.session_id)

    audio_file = await resolve_audio_file(final_reply, voice=req.voice, rate=req.rate)
    return {
        "success": True,
        "reply": final_reply,
        "intent": "AI_LLM_CONSULTATION" if is_llm_online else "COMPANION_CHAT",
        "agent_name": agent_info["name"],
        "agent_role": agent_info["title"],
        "is_emergency": False,
        "symptom_detected": False,
        "grounding_used": grounding_used,
        "sources": sources,
        "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
    }


class LLMConfigPayload(BaseModel):
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_base: Optional[str] = None


@router.get("/llm-config")
def get_current_llm_config(current_user: User = Depends(require_caregiver_or_admin)):
    """
    Xem trạng thái cấu hình Trí tuệ Nhân tạo MediGuard AI.
    Yêu cầu quyền Người chăm sóc hoặc Quản trị viên.
    Tuyệt đối không trả về API Key thô.
    """
    g_key = get_gemini_key()
    o_key = get_openai_key()
    return {
        "is_activated": True,
        "status": "READY_OUT_OF_BOX",
        "license_name": "MediGuard AI Pro Edition (Kích hoạt sẵn theo thiết bị)",
        "has_gemini": bool(g_key),
        "has_openai": bool(o_key),
        "gemini_masked": f"{g_key[:6]}...{g_key[-4:]}" if g_key and len(g_key) > 10 else ("Đã cấu hình riêng" if g_key else "Tích hợp sẵn"),
        "openai_masked": f"{o_key[:6]}...{o_key[-4:]}" if o_key and len(o_key) > 10 else ("Đã cấu hình riêng" if o_key else "Tích hợp sẵn"),
        "active_provider": "Google Gemini Cloud" if g_key else ("OpenAI / Groq" if o_key else "Trí Tuệ Nhân Tạo MediGuard AI (Tự động kích hoạt)"),
        "capabilities": [
            "Bầu bạn & trò chuyện đa năng bằng giọng nói",
            "Cập nhật thời tiết trực tuyến theo thời gian thực",
            "Tự động ghi nhận cữ uống thuốc vào nhật ký",
            "Nhận diện triệu chứng & kích hoạt cảnh báo người nhà",
            "Tra cứu đơn thuốc và hướng dẫn uống an toàn"
        ]
    }


@router.post("/llm-config")
def update_llm_config(
    payload: LLMConfigPayload,
    current_user: User = Depends(require_caregiver_or_admin)
):
    """
    Cập nhật khóa Google Gemini hoặc OpenAI API Key để kích hoạt LLM cho Bác.
    Yêu cầu quyền Người chăm sóc hoặc Quản trị viên.
    Không ghi log hoặc phản hồi secret/key nhạy cảm.
    """
    cleaned_gemini = payload.gemini_api_key.strip() if payload.gemini_api_key else None
    cleaned_openai = payload.openai_api_key.strip() if payload.openai_api_key else None
    cleaned_base = payload.openai_base.strip() if payload.openai_base else None

    set_llm_config(
        gemini_key=cleaned_gemini,
        openai_key=cleaned_openai,
        openai_base=cleaned_base
    )
    logger.info("Cấu hình khóa LLM đã được cập nhật bởi user_id=%s role=%s", current_user.id, current_user.role)

    g_key = get_gemini_key()
    o_key = get_openai_key()
    return {
        "success": True,
        "message": "Đã lưu và cập nhật khóa API LLM thành công!",
        "has_gemini": bool(g_key),
        "has_openai": bool(o_key),
        "active_provider": "Google Gemini" if g_key else ("OpenAI / Groq" if o_key else "Cục bộ")
    }

