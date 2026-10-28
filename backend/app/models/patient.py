# ==============================================================================
# MEDIGUARD CARE - PATIENT MODEL
# Database Table: patients
# Compliant with Decree 13/2023/ND-CP on Personal Data Protection & Consent
# ==============================================================================

from datetime import date, datetime
from app.core.datetime_utils import utc_now
from sqlalchemy import Column, String, Integer, Date, DateTime, Boolean, Text
from app.database import Base


class Patient(Base):
    __tablename__ = "patients"

    id = Column(String(50), primary_key=True, index=True)  # Mã BN: PT-2026-0891
    full_name = Column(String(100), nullable=False, index=True)
    date_of_birth = Column(Date, nullable=True)
    age = Column(Integer, nullable=False)
    gender = Column(String(10), nullable=False)  # Nam, Nữ, Khác
    phone_number = Column(String(20), nullable=False, index=True)
    emergency_contact_phone = Column(String(20), nullable=True)
    emergency_contact_name = Column(String(100), nullable=True)
    address = Column(String(255), nullable=True)

    # Thông tin chuyên môn & phẫu thuật
    icd10_code = Column(String(20), nullable=False)
    procedure_name = Column(String(255), nullable=False)
    surgical_department = Column(String(100), nullable=False)
    primary_surgeon_name = Column(String(100), nullable=False)
    assigned_coordinator_nurse = Column(String(100), nullable=True)

    # Lịch trình & Giao thức
    admission_date = Column(Date, nullable=False)
    discharge_date = Column(Date, nullable=False)
    protocol_id = Column(String(50), nullable=False)  # e.g. general_surgery_postop_7d
    current_day_offset = Column(String(20), default="D+1")  # D+1, D+3, D+7

    # Tuân thủ Nghị định 13/2023/NĐ-CP (Consent & Bảo vệ dữ liệu cá nhân)
    consent_voice_monitoring = Column(Boolean, default=True)
    consent_recorded_at = Column(DateTime, default=utc_now)
    preferred_call_time_window = Column(String(50), default="09:00-11:00")  # Khung giờ ưu tiên

    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
