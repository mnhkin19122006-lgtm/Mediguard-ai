import asyncio
import logging
from datetime import datetime, date
from app.core.datetime_utils import utc_now, utc_now_iso
from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.symptom_log import SymptomLog
from app.models.user import User, Role
from app.models.interaction_log import InteractionLog
from app.api.websocket import broadcast_sync
from app.core.notifier import send_telegram_alert
from app.core.zalo_service import zalo_service
from app.core.security import get_current_user, require_elder_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/symptoms", tags=["Symptoms"])


class SymptomReportRequest(BaseModel):
    elder_id: int
    symptom_name: str
    severity: Optional[str] = "Trung bình"
    description: Optional[str] = ""
    action_taken: Optional[str] = ""
    source: Optional[str] = None  # 'voice' | 'text' | 'quick'


SOURCE_LABELS = {
    "voice": "Giọng nói",
    "text": "Văn bản",
    "quick": "Chọn nhanh",
}


class SymptomConfirmRequest(BaseModel):
    caregiver_id: Optional[int] = None
    notes: Optional[str] = "Người chăm sóc đã gọi điện thăm hỏi và kiểm tra bác an toàn"


@router.get("/")
def get_symptoms(
    elder_id: Optional[int] = Query(None, description="Lọc theo mã người cao tuổi"),
    target_date: Optional[str] = Query(None, alias="date", description="Lọc theo ngày (YYYY-MM-DD)"),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Lấy danh sách nhật ký triệu chứng y tế theo ngày.
    Bảo vệ RBAC/IDOR:
    - ELDER chỉ xem triệu chứng của chính mình.
    - CAREGIVER chỉ xem triệu chứng của người cao tuổi đã liên kết.
    - ADMIN xem được toàn bộ hoặc theo elder_id.
    """
    query = db.query(SymptomLog)

    if current_user.role == Role.ELDER:
        if elder_id is not None and elder_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Không có quyền truy cập dữ liệu của người cao tuổi này"
            )
        query = query.filter(SymptomLog.elder_id == current_user.id)
    elif current_user.role == Role.CAREGIVER:
        if elder_id is not None:
            require_elder_access(elder_id, current_user)
            query = query.filter(SymptomLog.elder_id == elder_id)
        else:
            assigned_ids = [e.id for e in current_user.assigned_elders]
            query = query.filter(SymptomLog.elder_id.in_(assigned_ids))
    elif current_user.role == Role.ADMIN:
        if elder_id is not None:
            query = query.filter(SymptomLog.elder_id == elder_id)
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vai trò không có quyền xem triệu chứng"
        )

    if target_date:
        query = query.filter(SymptomLog.reported_date == target_date)

    items = query.order_by(SymptomLog.reported_date.desc(), SymptomLog.reported_time.desc()).limit(limit).all()

    today_str = date.today().isoformat()
    by_date: Dict[str, List[dict]] = {}
    today_count = 0
    unhandled_count = 0

    symptoms_list = []
    for item in items:
        data = item.to_dict()
        symptoms_list.append(data)
        d = item.reported_date or "Chưa rõ ngày"
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(data)

        if item.reported_date == today_str:
            today_count += 1
        if not item.caregiver_confirmed:
            unhandled_count += 1

    return {
        "success": True,
        "total": len(items),
        "today_count": today_count,
        "unhandled_count": unhandled_count,
        "by_date": by_date,
        "symptoms": symptoms_list
    }


@router.post("/report")
async def report_symptom(
    req: SymptomReportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Ghi nhận triệu chứng y tế của Bác (Hỗ trợ cả Voice thu âm & Text văn bản):
    1. Xác thực và kiểm tra quyền truy cập đối với elder_id.
    2. Tự động nhận diện mức độ khẩn cấp (tức ngực, khó thở, uống nhầm, ngất...).
    3. Lưu vết theo ngày vào cơ sở dữ liệu.
    4. Tự động báo động khẩn cấp tới điện thoại Người nhà (Telegram, Zalo, WebSocket).
    5. Trả về thông điệp phản hồi giọng nói ấm áp, trấn an người cao tuổi.
    """
    require_elder_access(req.elder_id, current_user)

    now = datetime.now()
    today_str = date.today().isoformat()
    time_str = now.strftime("%H:%M")

    elder = db.query(User).filter(User.id == req.elder_id).first()
    elder_name = elder.full_name if elder else f"Bác #{req.elder_id}"

    raw_name = req.symptom_name.strip() if req.symptom_name else "Triệu chứng bất thường"
    raw_desc = req.description.strip() if req.description else ""
    combined_text = f"{raw_name} {raw_desc}".lower()

    # Nhận diện tự động các dấu hiệu khẩn cấp đe dọa sức khỏe
    EMERGENCY_KEYWORDS = [
        "tức ngực", "thắt ngực", "khó thở", "hụt hơi", "thở dốc", "uống nhầm",
        "ngất", "bất tỉnh", "co giật", "liệt", "méo miệng", "nôn ra máu",
        "đau nhói tim", "choáng váng nặng", "hôn mê"
    ]
    is_emergency = any(kw in combined_text for kw in EMERGENCY_KEYWORDS)

    severity = req.severity or "Trung bình"
    if is_emergency:
        severity = "Khẩn cấp"

    symptom_title = raw_name
    final_desc = raw_desc if raw_desc else f"Bác thông báo triệu chứng: {raw_name}"

    if len(raw_name) > 40:
        matched_kw = None
        for kw in EMERGENCY_KEYWORDS + ["chóng mặt", "đau đầu", "buồn nôn", "đau bụng", "mệt mỏi", "sốt", "đau lưng", "khớp", "ngứa"]:
            if kw in combined_text:
                matched_kw = kw.title()
                break
        if matched_kw:
            symptom_title = matched_kw
        else:
            symptom_title = raw_name[:36] + "..."
        if not raw_desc:
            final_desc = raw_name

    action_text = req.action_taken or (
        "AI đã nhắc bác ngồi nghỉ ngơi tại chỗ, uống nước ấm và tự động phát cảnh báo khẩn cấp tới điện thoại người nhà."
    )
    source_label = SOURCE_LABELS.get((req.source or "").lower())
    if source_label:
        action_text = f"Nguồn ghi nhận: {source_label}. {action_text}"

    symptom_entry = SymptomLog(
        elder_id=req.elder_id,
        symptom_name=symptom_title,
        severity=severity,
        description=final_desc,
        reported_date=today_str,
        reported_time=time_str,
        caregiver_notified=True,
        caregiver_confirmed=False,
        action_taken=action_text,
        timestamp=utc_now()
    )
    db.add(symptom_entry)

    audit_log = InteractionLog(
        schedule_id=None,
        elder_id=req.elder_id,
        event_type="alert",
        response_content=raw_name,
        notes=f"Cảnh báo triệu chứng theo ngày ({today_str}): {symptom_title} - Mức độ: {severity}",
        timestamp=utc_now()
    )
    db.add(audit_log)
    db.commit()

    symptom_dict = symptom_entry.to_dict()

    # 1. Phát sóng sự kiện thời gian thực qua WebSocket (Zero-latency)
    broadcast_sync("symptom_alert", {
        "event": "symptom_reported",
        "symptom": symptom_dict,
        "message": f"🚨 CẢNH BÁO SỨC KHỎE: {elder_name} vừa báo triệu chứng [{symptom_title}] vào lúc {time_str} hôm nay!",
        "timestamp": now.isoformat()
    })

    # 2. Gửi cảnh báo Telegram bất đồng bộ (không làm chậm phản hồi HTTP)
    telegram_text = (
        f"🚨 <b>CẢNH BÁO SỨC KHỎE NGƯỜI CAO TUỔI THEO NGÀY!</b>\n\n"
        f"👤 <b>Đối tượng:</b> {elder_name}\n"
        f"📅 <b>Ngày phát sinh:</b> {today_str} lúc {time_str}\n"
        f"🩺 <b>Triệu chứng:</b> {symptom_title}\n"
        f"⚡ <b>Mức độ:</b> {severity}\n"
        f"📝 <b>Mô tả chi tiết:</b> \"{final_desc}\"\n\n"
        f"👉 <i>Đề nghị người chăm sóc / con cháu chủ động gọi điện kiểm tra tình trạng của Bác ngay!</i>"
    )
    asyncio.create_task(send_telegram_alert(telegram_text))

    # 3. Gửi tin nhắn Zalo bất đồng bộ
    try:
        asyncio.create_task(zalo_service.send_text_message(
            elder_id=req.elder_id,
            text=f"🚨 [CẢNH BÁO SỨC KHỎE NGÀY {today_str}] {elder_name} vừa báo triệu chứng '{symptom_title}' lúc {time_str}. Con cháu hãy mở ứng dụng để kiểm tra ngay!"
        ))
    except Exception as e:
        logger.warning(f"Lỗi gửi Zalo alert: {e}")

    # Tạo câu trả lời giọng nói ân cần, tùy biến theo mức độ nguy hiểm
    if "uống nhầm" in combined_text:
        reply_speech = (
            "Dạ thưa Bác, cháu đã báo động khẩn cấp tới con cháu về việc nghi uống nhầm thuốc rồi ạ! "
            "Bác hãy giữ nguyên các vỉ thuốc tại chỗ, ngồi nghỉ ngơi bình tĩnh, "
            "người nhà và bác sĩ sẽ gọi điện hướng dẫn Bác xử lý ngay lập tức ạ!"
        )
    elif "tức ngực" in combined_text or "khó thở" in combined_text:
        reply_speech = (
            f"Dạ thưa Bác, cháu đã ghi nhận triệu chứng {symptom_title} và phát chuông báo động đỏ tới con cháu rồi ạ. "
            "Bác hãy ngồi tựa lưng nghỉ ngơi, nới lỏng cổ áo và hít thở thật chậm đều nhé. "
            "Con cháu đang gọi điện cho Bác ngay đây ạ!"
        )
    else:
        reply_speech = (
            f"Dạ thưa Bác, cháu đã lưu triệu chứng '{symptom_title}' vào sổ theo dõi sức khỏe ngày hôm nay, "
            f"đồng thời đã gửi thông báo khẩn cấp tới điện thoại của con cháu rồi ạ. "
            f"Bác vui lòng ngồi nghỉ ngơi tại chỗ, uống một ngụm nước ấm và giữ bình tĩnh, "
            f"người nhà sẽ gọi điện cho Bác ngay đây ạ!"
        )

    return {
        "success": True,
        "symptom": symptom_dict,
        "reply_speech": reply_speech,
        "caregiver_notified": True,
        "reported_date": today_str,
        "reported_time": time_str
    }


@router.post("/{symptom_id}/confirm")
def confirm_symptom(
    symptom_id: int,
    req: SymptomConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Người chăm sóc xác nhận: 'Tôi đã kiểm tra & xử lý an toàn cho bác'.
    Kiểm tra quyền truy cập và ràng buộc người dùng thực hiện.
    """
    if current_user.role not in [Role.CAREGIVER, Role.ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ người chăm sóc hoặc quản trị viên mới có quyền xác nhận xử lý triệu chứng"
        )

    symptom = db.query(SymptomLog).filter(SymptomLog.id == symptom_id).first()
    if not symptom:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi triệu chứng")

    require_elder_access(symptom.elder_id, current_user)

    confirmed_by_id = current_user.id
    if req.caregiver_id and req.caregiver_id != current_user.id and current_user.role != Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Người chăm sóc chỉ được xác nhận bằng định danh của chính mình"
        )

    symptom.caregiver_confirmed = True
    symptom.confirmed_by = confirmed_by_id
    if req.notes:
        symptom.action_taken = (symptom.action_taken or "") + f" | Xác nhận người chăm sóc: {req.notes}"
    db.commit()
    db.refresh(symptom)

    symptom_dict = symptom.to_dict()

    # Báo qua WebSocket để tất cả màn hình người chăm sóc và Kiosk cập nhật
    broadcast_sync("symptom_confirmed", {
        "symptom_id": symptom_id,
        "elder_id": symptom.elder_id,
        "confirmed_by": confirmed_by_id,
        "notes": req.notes,
        "timestamp": utc_now_iso()
    })

    return {
        "success": True,
        "message": "Đã xác nhận xử lý triệu chứng thành công",
        "symptom": symptom_dict
    }
