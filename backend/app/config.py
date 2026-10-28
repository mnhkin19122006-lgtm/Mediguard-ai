# ==============================================================================
# MEDIGUARD CARE & MEDIGUARD AI - UNIFIED CONFIGURATION
# Compliant with Vietnamese Decree 13/2023/ND-CP (Personal Data Protection)
# Supports both Clinical Voice AI Triage and Medication Adherence Engine
# ==============================================================================

import os
from pathlib import Path
from typing import List
from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Base Paths
BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent
STORAGE_DIR = BASE_DIR / "storage"
AUDIO_CACHE_DIR = STORAGE_DIR / "audio_cache"
REF_IMAGES_DIR = STORAGE_DIR / "reference_images"
CAPTURED_IMAGES_DIR = STORAGE_DIR / "captured_images"

REFERENCE_IMG_DIR = REF_IMAGES_DIR
UPLOAD_IMG_DIR = STORAGE_DIR / "uploaded_checks"

# Load .env configuration
load_dotenv(ROOT_DIR / ".env")
load_dotenv(BASE_DIR / ".env")

# Ensure directories exist
for _p in (STORAGE_DIR, AUDIO_CACHE_DIR, REF_IMAGES_DIR, CAPTURED_IMAGES_DIR, UPLOAD_IMG_DIR):
    _p.mkdir(parents=True, exist_ok=True)

# Legacy Config Exports (Used by legacy routes, database, and background workers)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'mediguard.db'}")
SECRET_KEY = os.getenv("SECRET_KEY", "mediguard-secret-key-super-secure-change-in-prod-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
CAREGIVER_PIN = os.getenv("CAREGIVER_PIN", "1234")
NO_RESPONSE_MINUTES = int(os.getenv("NO_RESPONSE_MINUTES", "15"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DEFAULT_VIETNAMESE_VOICE = os.getenv("MEDIGUARD_VOICE", "vi-VN-Google")
DEFAULT_VOICE_RATE = os.getenv("MEDIGUARD_VOICE_RATE", "+0%")


class Settings(BaseSettings):
    PROJECT_NAME: str = "MediGuard Care Backend"
    API_V1_STR: str = "/api/v1"
    DEBUG: bool = True
    ENVIRONMENT: str = "development"

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, v):
        if isinstance(v, str):
            if v.lower() in ("release", "prod", "production", "0", "false", "no"):
                return False
            if v.lower() in ("debug", "dev", "development", "1", "true", "yes"):
                return True
        return bool(v)

    # Host & Port
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # CORS
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]

    # Database & Redis
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'mediguard.db'}")
    REDIS_URL: str = "redis://localhost:6379/0"

    # AI & LLM Providers
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "mock-gemini-key")
    GEMINI_FAST_MODEL: str = os.getenv("GEMINI_FAST_MODEL", "gemini-3.5-flash-lite")
    GEMINI_PRO_MODEL: str = "gemini-2.5-pro"
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "mock-openai-key")
    DEFAULT_LLM_TEMPERATURE: float = 0.0  # Zero-hallucination deterministic temperature

    # Telephony Gateway (Stringee / OmiCall)
    TELEPHONY_PROVIDER: str = "stringee"  # "stringee" or "omicall"
    STRINGEE_API_KEY_SID: str = os.getenv("STRINGEE_API_KEY_SID", "mock-sid")
    STRINGEE_API_KEY_SECRET: str = os.getenv("STRINGEE_API_KEY_SECRET", "mock-secret")
    HOTLINE_EMERGENCY_TRANSFER: str = "19001115"
    VOICE_BRANDNAME: str = "MEDIGUARD_HOSPITAL"

    # Zalo Notification Service (ZNS)
    ZALO_OA_ID: str = os.getenv("ZALO_OA_ID", "mock-zalo-oa")
    ZALO_APP_SECRET: str = os.getenv("ZALO_APP_SECRET", "mock-zalo-secret")

    # MinIO / S3 Storage (Audio Call Recordings with AES-256)
    S3_ENDPOINT: str = "localhost:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET_NAME: str = "mediguard-call-recordings"
    S3_USE_SSL: bool = False

    # SLA Default Minutes (Based on Clinical Board Policies)
    SLA_RED_FLAG_MINUTES: int = 15      # Emergency Code Red
    SLA_ORANGE_FLAG_MINUTES: int = 240   # Clinical Review (4 hours)
    SLA_YELLOW_FLAG_MINUTES: int = 720   # Information clarification (12 hours)
    SLA_GREEN_FLAG_MINUTES: int = 1440   # Routine check (24 hours)

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="allow"
    )


settings = Settings()
