# ==============================================================================
# MEDIGUARD CARE - TRIAGE TICKET MODEL
# Database Table: triage_tickets
# Core model managing the Closed-Loop Clinical Resolution Workflow & SLAs
# ==============================================================================

from datetime import datetime
from app.core.datetime_utils import utc_now
from sqlalchemy import Column, String, Integer, DateTime, JSON, ForeignKey, Text
from .patient import Base


class TriageTicket(Base):
    __tablename__ = "triage_tickets"

    ticket_id = Column(String(50), primary_key=True, index=True)  # TCK-2026-0042
    patient_id = Column(String(50), ForeignKey("patients.id"), nullable=False, index=True)
    call_session_id = Column(String(50), ForeignKey("call_sessions.session_id"), nullable=False)

    # Mức độ ưu tiên lâm sàng: RED, ORANGE, YELLOW, GREEN
    priority_level = Column(String(20), nullable=False, index=True)

    # Danh sách quy tắc đã kích hoạt (Lý do báo động)
    triggered_rules = Column(JSON, nullable=False)
    clinical_summary = Column(Text, nullable=False)

    # Phân công trách nhiệm
    assigned_to_user_id = Column(String(100), nullable=True)  # ID/Tên Điều dưỡng hoặc Bác sĩ trực
    assigned_role = Column(String(50), default="Điều dưỡng theo dõi")

    # Trạng thái quy trình khép kín: OPEN, ACKNOWLEDGED, IN_PROGRESS, RESOLVED, ESCALATED
    status = Column(String(30), default="OPEN", index=True)

    # Đếm ngược thời hạn xử lý (SLA Tracking)
    sla_deadline = Column(DateTime, nullable=False, index=True)
    is_sla_breached = Column(Integer, default=0)  # 1 nếu quá hạn chưa xử lý

    # Kết quả xử lý lâm sàng (Closed-Loop Resolution)
    resolution_action_type = Column(String(50), nullable=True)  # call_patient, adjust_meds, schedule_early, code_red, resolve_safe
    resolution_notes = Column(Text, nullable=True)  # Ghi chú bắt buộc của bác sĩ/điều dưỡng
    resolved_by_user_id = Column(String(100), nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
