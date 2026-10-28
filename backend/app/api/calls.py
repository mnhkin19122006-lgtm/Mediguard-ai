import json
import logging
from datetime import datetime, date
from app.core.datetime_utils import utc_now, utc_now_iso
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import get_db
from app.models.user import User
from app.models.schedule import Schedule
from app.models.medication import Medication
from app.models.interaction_log import InteractionLog
from app.models.call_report import (
    AICallReport,
    CallStatus,
    MedicationAdherence,
    FollowupPriority,
)
from app.core.call_ai_service import (
    DEFAULT_APPROVED_QUESTIONS,
    analyze_transcript_and_generate_report,
)
from app.core.notifier import send_telegram_alert
from app.core.zalo_service import zalo_service
from app.api.websocket import broadcast_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calls", tags=["AI Calls"])

# Lưu bộ câu hỏi đã duyệt trong bộ nhớ / runtime (có thể cập nhật qua API)
_RUNTIME_APPROVED_QUESTIONS = list(DEFAULT_APPROVED_QUESTIONS)


# Schemas Pydantic
class TriggerCallRequest(BaseModel):
    elder_id: int
    schedule_id: Optional[int] = None
    call_type: Optional[str] = "TỰ ĐỘNG THEO LỊCH"


class DialogueTurn(BaseModel):
    speaker: str  # "AI" hoặc "PATIENT"
    text: str
    timestamp: Optional[str] = None


class SubmitCallResultRequest(BaseModel):
    elder_id: int
    schedule_id: Optional[int] = None
    call_status: str = CallStatus.COMPLETED  # COMPLETED | NO_ANSWER | BUSY
    duration_seconds: int = 0
    questions_asked: Optional[List[Dict[str, Any]]] = None
    transcript: List[DialogueTurn] = []
    scheduled_time: Optional[str] = None


class ResolveCallbackRequest(BaseModel):
    staff_notes: str
    handled_by_name: Optional[str] = "Nhân viên y tế trực"
    handled_by_user_id: Optional[int] = None


class UpdateQuestionsRequest(BaseModel):
    questions: List[Dict[str, Any]]


# ----------------------------------------------------------------------
# 1. BỘ CÂU HỎI LÂM SÀNG ĐÃ ĐƯỢC DUYỆT
# ----------------------------------------------------------------------
@router.get("/questions")
def get_approved_questions():
    """Lấy danh sách các câu hỏi lâm sàng đã được phê duyệt để AI sử dụng."""
    return _RUNTIME_APPROVED_QUESTIONS


@router.post("/questions")
def update_approved_questions(req: UpdateQuestionsRequest):
    """Cập nhật hoặc tùy biến bộ câu hỏi đã được duyệt bởi nhân viên y tế."""
    global _RUNTIME_APPROVED_QUESTIONS
    _RUNTIME_APPROVED_QUESTIONS = req.questions
    return {"message": "Đã cập nhật bộ câu hỏi lâm sàng phê duyệt", "total": len(_RUNTIME_APPROVED_QUESTIONS)}


# ----------------------------------------------------------------------
# 2. TỰ ĐỘNG GỌI ĐIỆN HỎI THĂM BỆNH NHÂN (KÍCH HOẠT / THỰC HIỆN)
# ----------------------------------------------------------------------
@router.post("/trigger")
def trigger_ai_call(req: TriggerCallRequest, db: Session = Depends(get_db)):
    """
    Kích hoạt cuộc gọi hỏi thăm tự động đến bệnh nhân:
    - Thu thập thông tin cữ thuốc đã được phê duyệt của bệnh nhân.
    - Soạn thảo câu hỏi cá nhân hóa cho cữ thuốc.
    - Phát tín hiệu cuộc gọi đến thiết bị Kiosk / Tablet của Bác qua WebSocket.
    """
    elder = db.query(User).filter(User.id == req.elder_id).first()
    if not elder:
        raise HTTPException(status_code=404, detail="Không tìm thấy thông tin bệnh nhân")

    # Lấy thông tin cữ thuốc nếu có
    sch = None
    med_names = []
    if req.schedule_id:
        sch = db.query(Schedule).filter(Schedule.id == req.schedule_id).first()
    if not sch:
        # Lấy cữ thuốc gần nhất hôm nay
        sch = db.query(Schedule).filter(Schedule.elder_id == req.elder_id, Schedule.is_active == True).first()

    if sch:
        meds = db.query(Medication).filter(
            Medication.elder_id == req.elder_id,
            Medication.time_period == sch.time_period,
            Medication.is_active == True,
        ).all()
        med_names = [m.name for m in meds]

    # Cá nhân hóa câu hỏi mở đầu với tên thuốc thực tế
    meds_str = ", ".join(med_names) if med_names else "thuốc chỉ định"
    time_str = sch.time_period if sch else "hôm nay"

    customized_questions = []
    for q in _RUNTIME_APPROVED_QUESTIONS:
        q_copy = dict(q)
        if q["id"] == "q1_adherence":
            q_copy["question"] = (
                f"Kính chào {elder.full_name}! Cháu là Trợ lý Y tế AI gọi thăm sức khỏe Bác theo lịch cữ {time_str}. "
                f"Các loại thuốc gồm {meds_str}, Bác đã uống đầy đủ chưa ạ?"
            )
        customized_questions.append(q_copy)

    # Broadcast WebSocket cuộc gọi đến
    call_payload = {
        "elder_id": elder.id,
        "elder_name": elder.full_name,
        "elder_phone": elder.phone_number or "0901234567",
        "schedule_id": sch.id if sch else None,
        "scheduled_time": sch.target_time if sch else datetime.now().strftime("%H:%M"),
        "time_period": sch.time_period if sch else "Sáng",
        "medications": med_names,
        "approved_questions": customized_questions,
        "timestamp": utc_now_iso(),
        "call_type": req.call_type,
    }

    broadcast_sync("incoming_call", call_payload)

    return {
        "status": "ringing",
        "message": f"Đã phát lệnh gọi điện tự động tới {elder.full_name}",
        "call_payload": call_payload,
    }


# ----------------------------------------------------------------------
# 3. TIẾP NHẬN KẾT QUẢ & TỰ ĐỘNG TẠO BÁO CÁO CÓ CẤU TRÚC
# ----------------------------------------------------------------------
@router.post("/submit-result")
async def submit_call_result(req: SubmitCallResultRequest, db: Session = Depends(get_db)):
    """
    Tiếp nhận nội dung cuộc gọi vừa hoàn tất (hoặc không nghe máy):
    1. Chuyển nội dung đối thoại thành bản ghi lâm sàng có cấu trúc bằng AI.
    2. Phát hiện trường hợp cần nhân viên y tế liên hệ lại.
    3. Lưu trữ vào cơ sở dữ liệu và ghi nhật ký kiểm toán bất biến.
    4. Bắn thông báo khẩn qua WebSocket, Zalo, Telegram nếu cần can thiệp.
    """
    elder = db.query(User).filter(User.id == req.elder_id).first()
    if not elder:
        raise HTTPException(status_code=404, detail="Không tìm thấy người cao tuổi")

    transcript_dicts = [t.dict() for t in req.transcript]

    # Phân tích nội dung và sinh báo cáo có cấu trúc
    report_data = analyze_transcript_and_generate_report(
        elder_name=elder.full_name,
        elder_phone=elder.phone_number or "0901234567",
        call_status=req.call_status,
        duration_seconds=req.duration_seconds,
        transcript=transcript_dicts,
        scheduled_time=req.scheduled_time or datetime.now().strftime("%H:%M"),
    )

    # Tạo bản ghi AICallReport trong cơ sở dữ liệu
    call_report = AICallReport(
        elder_id=elder.id,
        schedule_id=req.schedule_id,
        call_type="TỰ ĐỘNG THEO LỊCH",
        scheduled_time=req.scheduled_time or datetime.now().strftime("%H:%M"),
        call_time=utc_now(),
        call_status=req.call_status,
        duration_seconds=req.duration_seconds,
        questions_asked=json.dumps(req.questions_asked or _RUNTIME_APPROVED_QUESTIONS, ensure_ascii=False),
        transcript=json.dumps(transcript_dicts, ensure_ascii=False),
        medication_adherence=report_data["medication_adherence"],
        adherence_notes=report_data["adherence_notes"],
        reported_symptoms=json.dumps(report_data["reported_symptoms"], ensure_ascii=False),
        patient_mood=report_data["patient_mood"],
        summary_report=report_data["summary_report"],
        requires_followup=report_data["requires_followup"],
        followup_reason=report_data["followup_reason"],
        followup_priority=report_data["followup_priority"],
        handled_by_staff=False,
    )
    db.add(call_report)
    db.commit()
    db.refresh(call_report)

    # Ghi nhận vào chuỗi sự kiện kiểm toán bất biến InteractionLog
    event_type = "user_responded" if req.call_status == CallStatus.COMPLETED else "alert"
    audit_log = InteractionLog(
        schedule_id=req.schedule_id,
        elder_id=elder.id,
        event_type=event_type,
        response_content="Đã trả lời cuộc gọi AI" if req.call_status == CallStatus.COMPLETED else "Không nghe máy",
        device_action="AI_VOICE_CALL",
        notes=f"[Báo cáo cuộc gọi AI #{call_report.id}] {report_data['summary_report'][:200]}",
        timestamp=utc_now(),
    )
    db.add(audit_log)
    db.commit()

    # Broadcast sự kiện báo cáo cuộc gọi mới
    serialized_report = {
        "id": call_report.id,
        "elder_id": elder.id,
        "elder_name": elder.full_name,
        "elder_phone": elder.phone_number or "0901234567",
        "call_status": call_report.call_status,
        "duration_seconds": call_report.duration_seconds,
        "medication_adherence": call_report.medication_adherence,
        "adherence_notes": call_report.adherence_notes,
        "reported_symptoms": report_data["reported_symptoms"],
        "patient_mood": call_report.patient_mood,
        "summary_report": call_report.summary_report,
        "requires_followup": call_report.requires_followup,
        "followup_priority": call_report.followup_priority,
        "followup_reason": call_report.followup_reason,
        "handled_by_staff": call_report.handled_by_staff,
        "created_at": call_report.created_at.isoformat(),
        "transcript": transcript_dicts,
    }

    broadcast_sync("call_report_created", serialized_report)

    # 4. NẾU CẦN NHÂN VIÊN Y TẾ LIÊN HỆ LẠI -> PHÁT CẢNH BÁO TỨC THÌ
    if call_report.requires_followup:
        broadcast_sync("urgent_callback_alert", {
            "report_id": call_report.id,
            "elder_name": elder.full_name,
            "priority": call_report.followup_priority,
            "reason": call_report.followup_reason,
            "message": f"🚨 Bệnh nhân {elder.full_name} cần liên hệ lại: {call_report.followup_reason}",
        })

        # Gửi cảnh báo Telegram cho nhân viên y tế / người thân
        prio_icon = "🔴" if call_report.followup_priority == FollowupPriority.CRITICAL else "🟡"
        telegram_msg = (
            f"{prio_icon} <b>THÔNG BÁO: BỆNH NHÂN CẦN LIÊN HỆ LẠI!</b>\n"
            f"• Bệnh nhân: <b>{elder.full_name}</b> (SĐT: {elder.phone_number or '0901234567'})\n"
            f"• Mức độ ưu tiên: <b>{call_report.followup_priority}</b>\n"
            f"• Lý do: <i>{call_report.followup_reason}</i>\n"
            f"• Tóm tắt cuộc gọi: {call_report.summary_report[:250]}...\n"
            f"👉 <i>Đề nghị nhân viên y tế kiểm tra và thực hiện cuộc gọi lại!</i>"
        )
        try:
            await send_telegram_alert(telegram_msg)
        except Exception as e:
            logger.warning(f"Lỗi gửi telegram cảnh báo cuộc gọi: {e}")

    return {
        "status": "success",
        "report_id": call_report.id,
        "report": serialized_report,
    }


# ----------------------------------------------------------------------
# 4. DANH SÁCH BÁO CÁO CUỘC GỌI CÓ CẤU TRÚC
# ----------------------------------------------------------------------
@router.get("/reports")
def get_call_reports(
    elder_id: Optional[int] = None,
    requires_followup: Optional[bool] = None,
    handled: Optional[bool] = None,
    limit: int = Query(50, le=100),
    db: Session = Depends(get_db),
):
    """Lấy danh sách các báo cáo cuộc gọi có cấu trúc với bộ lọc linh hoạt."""
    query = db.query(AICallReport)
    if elder_id:
        query = query.filter(AICallReport.elder_id == elder_id)
    if requires_followup is not None:
        query = query.filter(AICallReport.requires_followup == requires_followup)
    if handled is not None:
        query = query.filter(AICallReport.handled_by_staff == handled)

    reports = query.order_by(desc(AICallReport.call_time)).limit(limit).all()

    result = []
    for r in reports:
        elder = db.query(User).filter(User.id == r.elder_id).first()
        result.append({
            "id": r.id,
            "elder_id": r.elder_id,
            "elder_name": elder.full_name if elder else f"Bệnh nhân #{r.elder_id}",
            "elder_phone": elder.phone_number if elder else "0901234567",
            "schedule_id": r.schedule_id,
            "call_type": r.call_type,
            "scheduled_time": r.scheduled_time,
            "call_time": r.call_time.isoformat() if r.call_time else None,
            "call_status": r.call_status,
            "duration_seconds": r.duration_seconds,
            "medication_adherence": r.medication_adherence,
            "adherence_notes": r.adherence_notes,
            "reported_symptoms": json.loads(r.reported_symptoms) if r.reported_symptoms else [],
            "patient_mood": r.patient_mood,
            "summary_report": r.summary_report,
            "requires_followup": r.requires_followup,
            "followup_priority": r.followup_priority,
            "followup_reason": r.followup_reason,
            "handled_by_staff": r.handled_by_staff,
            "handled_by_name": r.handled_by_name,
            "handled_at": r.handled_at.isoformat() if r.handled_at else None,
            "staff_notes": r.staff_notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "transcript": json.loads(r.transcript) if r.transcript else [],
        })
    return result


# ----------------------------------------------------------------------
# 5. DANH SÁCH BỆNH NHÂN CẦN NHÂN VIÊN Y TẾ LIÊN HỆ LẠI (TRIAGE QUEUE)
# ----------------------------------------------------------------------
@router.get("/callbacks")
def get_pending_callbacks(elder_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    Lấy danh sách các trường hợp phát hiện CẦN NHÂN VIÊN LIÊN HỆ LẠI:
    - Bệnh nhân không nghe máy
    - Bệnh nhân yêu cầu hỗ trợ
    - Câu trả lời bất thường (chưa uống thuốc, triệu chứng nguy hiểm)
    Sắp xếp ưu tiên: CRITICAL -> HIGH -> MEDIUM -> LOW.
    """
    query = db.query(AICallReport).filter(
        AICallReport.requires_followup == True,
        AICallReport.handled_by_staff == False,
    )
    if elder_id:
        query = query.filter(AICallReport.elder_id == elder_id)

    reports = query.order_by(desc(AICallReport.call_time)).all()

    # Sắp xếp theo thứ tự ưu tiên lâm sàng
    priority_order = {
        FollowupPriority.CRITICAL: 1,
        FollowupPriority.HIGH: 2,
        FollowupPriority.MEDIUM: 3,
        FollowupPriority.LOW: 4,
        FollowupPriority.NONE: 5,
    }

    sorted_reports = sorted(reports, key=lambda r: priority_order.get(r.followup_priority, 99))

    result = []
    for r in sorted_reports:
        elder = db.query(User).filter(User.id == r.elder_id).first()
        result.append({
            "id": r.id,
            "elder_id": r.elder_id,
            "elder_name": elder.full_name if elder else f"Bệnh nhân #{r.elder_id}",
            "elder_phone": elder.phone_number if elder else "0901234567",
            "call_time": r.call_time.isoformat() if r.call_time else None,
            "call_status": r.call_status,
            "followup_priority": r.followup_priority,
            "followup_reason": r.followup_reason,
            "medication_adherence": r.medication_adherence,
            "reported_symptoms": json.loads(r.reported_symptoms) if r.reported_symptoms else [],
            "summary_report": r.summary_report,
            "transcript": json.loads(r.transcript) if r.transcript else [],
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return result


# ----------------------------------------------------------------------
# 6. NHÂN VIÊN Y TẾ XÁC NHẬN ĐÃ LIÊN HỆ LẠI & GHI CHÚ CHUYÊN MÔN
# ----------------------------------------------------------------------
@router.post("/resolve-callback/{report_id}")
def resolve_callback(
    report_id: int,
    req: ResolveCallbackRequest,
    db: Session = Depends(get_db),
):
    """
    Nhân viên y tế sau khi gọi lại bệnh nhân bấm xác nhận đã xử lý,
    nhập ghi chú lâm sàng và lưu vào hồ sơ an toàn y tế.
    """
    report = db.query(AICallReport).filter(AICallReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Không tìm thấy báo cáo cuộc gọi")

    report.handled_by_staff = True
    report.handled_at = utc_now()
    report.handled_by_name = req.handled_by_name or "Nhân viên y tế trực"
    report.handled_by_user_id = req.handled_by_user_id
    report.staff_notes = req.staff_notes

    # Ghi nhận vào chuỗi sự kiện kiểm toán bất biến
    elder = db.query(User).filter(User.id == report.elder_id).first()
    audit_log = InteractionLog(
        schedule_id=report.schedule_id,
        elder_id=report.elder_id,
        event_type="caregiver_confirmed",
        confirmed_by=req.handled_by_user_id or 2,
        response_content="Nhân viên y tế đã gọi lại hỗ trợ",
        notes=f"Đã xử lý can thiệp cho cuộc gọi #{report.id}: {req.staff_notes}",
        timestamp=utc_now(),
    )
    db.add(audit_log)
    db.commit()

    broadcast_sync("callback_resolved", {
        "report_id": report.id,
        "elder_name": elder.full_name if elder else "Bệnh nhân",
        "handled_by": report.handled_by_name,
        "staff_notes": report.staff_notes,
    })

    return {
        "status": "success",
        "message": "Đã ghi nhận nhân viên y tế đã liên hệ lại thành công",
        "report_id": report.id,
    }


# ----------------------------------------------------------------------
# 7. THỐNG KÊ TỔNG ĐÀI AI CHO BẢNG ĐIỀU KHIỂN
# ----------------------------------------------------------------------
@router.get("/stats")
def get_call_stats(elder_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Tổng hợp KPI hiệu suất tổng đài AI hỏi thăm cho nhân viên y tế."""
    query = db.query(AICallReport)
    if elder_id:
        query = query.filter(AICallReport.elder_id == elder_id)

    total_calls = query.count()
    completed_calls = query.filter(AICallReport.call_status == CallStatus.COMPLETED).count()
    no_answer_calls = query.filter(AICallReport.call_status == CallStatus.NO_ANSWER).count()

    pending_callbacks = query.filter(
        AICallReport.requires_followup == True,
        AICallReport.handled_by_staff == False,
    ).count()

    resolved_callbacks = query.filter(
        AICallReport.requires_followup == True,
        AICallReport.handled_by_staff == True,
    ).count()

    taken_calls = query.filter(
        AICallReport.call_status == CallStatus.COMPLETED,
        AICallReport.medication_adherence == MedicationAdherence.TAKEN,
    ).count()

    adherence_rate = round((taken_calls / completed_calls * 100), 1) if completed_calls > 0 else 100.0

    return {
        "total_calls": total_calls,
        "completed_calls": completed_calls,
        "no_answer_calls": no_answer_calls,
        "pending_callbacks": pending_callbacks,
        "resolved_callbacks": resolved_callbacks,
        "adherence_rate": adherence_rate,
    }
