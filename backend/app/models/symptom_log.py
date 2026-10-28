# ==============================================================================
# MEDIGUARD CARE & MEDIGUARD AI - SYMPTOM LOG MODEL
# Database Table: symptom_logs (Elder Care Daily Tracking)
# and clinical_postop_logs (Post-Op Clinical Voicebot Tracking)
# ==============================================================================

from datetime import datetime
from app.core.datetime_utils import utc_now
from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean, JSON, ForeignKey
from app.database import Base


class SymptomLog(Base):
    """
    Nhật ký triệu chứng hàng ngày cho người cao tuổi (Elderly Care)
    """
    __tablename__ = "symptom_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    elder_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    symptom_name = Column(String, nullable=False)
    severity = Column(String, default="Nhẹ")
    description = Column(String, nullable=True)
    reported_date = Column(String, nullable=False, index=True)
    reported_time = Column(String, nullable=True)
    caregiver_notified = Column(Boolean, default=False)
    caregiver_confirmed = Column(Boolean, default=False)
    confirmed_by = Column(Integer, nullable=True)
    action_taken = Column(String, nullable=True)
    timestamp = Column(DateTime, default=utc_now)

    def to_dict(self):
        return {
            "id": self.id,
            "elder_id": self.elder_id,
            "symptom_name": self.symptom_name,
            "severity": self.severity,
            "description": self.description,
            "reported_date": self.reported_date,
            "reported_time": self.reported_time,
            "caregiver_notified": self.caregiver_notified,
            "caregiver_confirmed": self.caregiver_confirmed,
            "confirmed_by": self.confirmed_by,
            "action_taken": self.action_taken,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


class ClinicalPostOpLog(Base):
    """
    Nhật ký triệu chứng bóc tách sau cuộc gọi theo dõi hậu phẫu (B2B SaaS Clinical Triage)
    """
    __tablename__ = "clinical_postop_logs"

    id = Column(String(50), primary_key=True, index=True)
    patient_id = Column(String(50), nullable=False, index=True)
    call_session_id = Column(String(50), nullable=False)
    protocol_day = Column(String(20), nullable=False)  # D+1, D+3, D+7

    pain_vas_score = Column(Integer, nullable=True)
    pain_trend = Column(String(30), nullable=True)
    wound_condition = Column(String(50), nullable=True)
    temperature_celsius = Column(Float, nullable=True)
    fever_status = Column(String(50), nullable=True)
    bowel_movement = Column(String(50), nullable=True)
    medication_adherence = Column(String(50), nullable=True)

    structured_payload = Column(JSON, nullable=False)
    evidence_quotes_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=utc_now)
