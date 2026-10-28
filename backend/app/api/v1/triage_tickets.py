# ==============================================================================
# MEDIGUARD CARE - TRIAGE TICKETS API
# Real-time Triage Queue, SLA tracking, and Closed-Loop Action Resolution
# ==============================================================================

from typing import List, Optional
from datetime import datetime, timedelta
from app.core.datetime_utils import utc_now
from fastapi import APIRouter, HTTPException, Query
from ...schemas.triage_schema import (
    TriageTicketResponse,
    ActionResolutionRequest,
    TriageLevel,
    TicketStatus
)
from ...services.clinical_triage.ticket_dispatcher import ticket_dispatcher

router = APIRouter()

# Bộ nhớ giả lập Triage Tickets
MOCK_TICKETS = [
    {
        "ticket_id": "TCK-2026-0042",
        "patient_id": "PT-2026-0422",
        "patient_name": "Vũ Đình Khang",
        "patient_phone": "0983 892 114",
        "procedure": "Nong và đặt Stent động mạch vành LAD",
        "discharge_day": "D+1",
        "priority_level": TriageLevel.RED,
        "status": TicketStatus.OPEN,
        "triggered_rules": [
            "RED_FLAG: Đau thắt ngực cấp tính > 10 phút lan vai sau đặt Stent",
            "RED_FLAG: Khó thở đột ngột kèm vã mồ hôi lạnh"
        ],
        "clinical_summary": "BÁO ĐỘNG ĐỎ: Đau tức ngực tái phát sau can thiệp Stent > 10m kèm khó thở và vã mồ hôi. Nguy cơ tắc Stent cấp tính. Kích hoạt Code Red ngay lập tức!",
        "evidence_quotes": [
            "Tôi thấy tức nghẹn ngực trái lan ra vai khoảng 20 phút nay rồi, thở hơi dốc và thấy vã mồ hôi hột ở trán..."
        ],
        "sla_deadline": utc_now() + timedelta(minutes=8),
        "sla_remaining_minutes": 8,
        "assigned_to": "ĐD. Đặng Thanh Tâm",
        "created_at": utc_now() - timedelta(minutes=7)
    },
    {
        "ticket_id": "TCK-2026-0041",
        "patient_id": "PT-2026-0891",
        "patient_name": "Nguyễn Văn Tuấn",
        "patient_phone": "0912 345 678",
        "procedure": "Phẫu thuật Cắt túi mật nội soi",
        "discharge_day": "D+3",
        "priority_level": TriageLevel.ORANGE,
        "status": TicketStatus.IN_PROGRESS,
        "triggered_rules": [
            "ORANGE_FLAG: Đau vết mổ tăng dần từ ngày thứ 3 (VAS 4 -> 6)",
            "ORANGE_FLAG: Xuất hiện sốt nhẹ 37.8°C"
        ],
        "clinical_summary": "Cảnh báo mức Cam: Triệu chứng đau tăng so với D+1 kèm sốt nhẹ 37.8°C mới xuất hiện. Cần điều dưỡng gọi lại đánh giá nhiễm trùng vết mổ trong 4 giờ.",
        "evidence_quotes": [
            "Từ sáng đến giờ vết mổ hạ sườn phải hơi đau nhói hơn hôm qua cô ạ, người thấy mệt và hơi âm ấm đầu đo được khoảng 37.8 độ."
        ],
        "sla_deadline": utc_now() + timedelta(minutes=145),
        "sla_remaining_minutes": 145,
        "assigned_to": "ĐD. CKI Nguyễn Thu Hằng",
        "created_at": utc_now() - timedelta(minutes=35)
    }
]


@router.get("/triage-tickets", response_model=List[TriageTicketResponse])
async def list_triage_tickets(priority: Optional[TriageLevel] = Query(None)):
    """
    Lấy danh sách các ca bệnh trong hàng đợi Triage Queue thời gian thực
    """
    tickets = MOCK_TICKETS
    if priority:
        tickets = [t for t in tickets if t["priority_level"] == priority]
    return tickets


@router.post("/triage-tickets/{ticket_id}/resolve")
async def resolve_ticket(ticket_id: str, request: ActionResolutionRequest):
    """
    Bắt buộc nhân viên y tế xác nhận kết quả xử lý lâm sàng (Closed-Loop Resolution)
    trước khi đóng phiếu cảnh báo.
    """
    ticket = next((t for t in MOCK_TICKETS if t["ticket_id"] == ticket_id), None)
    if not ticket:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu cảnh báo")

    ticket["status"] = TicketStatus.RESOLVED if request.action_type != "code_red" else TicketStatus.ESCALATED
    ticket["resolved_at"] = datetime.utcnow()
    ticket["resolution_notes"] = f"[{request.staff_name}] {request.action_type}: {request.resolution_notes}"

    # Bắn sự kiện cập nhật trạng thái tới tất cả client
    await ticket_dispatcher.broadcast_ticket_event("TICKET_RESOLVED", ticket)

    return {
        "success": True,
        "ticket_id": ticket_id,
        "status": ticket["status"],
        "resolved_at": ticket["resolved_at"]
    }
