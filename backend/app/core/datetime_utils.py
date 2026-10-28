from datetime import datetime, date, timezone

def utc_now() -> datetime:
    """Trả về datetime UTC dạng naive, an toàn tuyệt đối với SQLite và Python 3.12+."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

def utc_now_iso() -> str:
    """Trả về chuỗi ISO format của thời điểm UTC hiện tại."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
