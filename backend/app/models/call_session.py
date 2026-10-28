# ==============================================================================
# MEDIGUARD CARE - CALL SESSION MODEL
# Database Table: call_sessions
# ==============================================================================

from datetime import datetime
from app.core.datetime_utils import utc_now
from sqlalchemy import Column, String, Integer, DateTime, Boolean, JSON, ForeignKey, Text
from .patient import Base


class CallSession(Base):
    __tablename__ = "call_sessions"

    session_id = Column(String(50), primary_key=True, index=True)
    patient_id = Column(String(50), ForeignKey("patients.id"), nullable=False, index=True)
    protocol_day = Column(String(20), nullable=False)  # D+1, D+3, D+7

    # Thời gian & Trạng thái
    scheduled_time = Column(DateTime, nullable=False)
    actual_start_time = Column(DateTime, nullable=True)
    actual_end_time = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, default=0)

    # Trạng thái cuộc gọi
    call_status = Column(
        String(30),
        default="SCHEDULED"  # SCHEDULED, DIALING, COMPLETED, NO_ANSWER, BUSY, FAILED, TRANSFERRED_HOTLINE
    )
    retry_count = Column(Integer, default=0)

    # Tích hợp Tổng đài (Stringee / OmiCall)
    telephony_provider = Column(String(30), default="stringee")
    telephony_call_id = Column(String(100), nullable=True)
    recording_s3_url = Column(String(255), nullable=True)  # MinIO / S3 audio file URL (AES-256)

    # Dữ liệu hội thoại thô
    full_transcript_json = Column(JSON, nullable=True)  # List of {speaker: 'ai'|'patient', text, timestamp, audio_offset_ms}

    # Kênh Zalo ZNS báo trước
    zns_pre_call_sent = Column(Boolean, default=False)
    zns_pre_call_sent_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=utc_now)
