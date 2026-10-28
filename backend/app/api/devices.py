from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from datetime import datetime
from app.core.datetime_utils import utc_now
from app.database import get_db
from app.models.device_event import DeviceEvent
from app.models.interaction_log import InteractionLog

router = APIRouter(prefix="/api/devices", tags=["Devices"])

class DeviceEventPayload(BaseModel):
    device_id: str
    elder_id: int
    schedule_id: Optional[int] = None
    event_name: str # "COMPARTMENT_OPENED", "BUTTON_PRESSED"
    compartment_index: Optional[int] = None
    raw_payload: Optional[str] = None

@router.post("/event")
def receive_device_event(payload: DeviceEventPayload, db: Session = Depends(get_db)):
    """
    Tiếp nhận sự kiện từ hộp thuốc thông minh ESP32 (MVP 3).
    Ghi nhận DeviceEvent và InteractionLog (device_recorded).
    """
    dev_event = DeviceEvent(
        device_id=payload.device_id,
        elder_id=payload.elder_id,
        event_name=payload.event_name,
        compartment_index=payload.compartment_index,
        raw_payload=payload.raw_payload,
        created_at=utc_now()
    )
    db.add(dev_event)

    # Ghi nhận sự kiện thiết bị vào nhật ký kiểm toán
    log = InteractionLog(
        schedule_id=payload.schedule_id,
        elder_id=payload.elder_id,
        event_type="device_recorded",
        device_action=f"IOT_{payload.event_name}",
        notes=f"Hộp thuốc {payload.device_id} ghi nhận sự kiện mở ngăn số {payload.compartment_index}",
        timestamp=utc_now()
    )
    db.add(log)
    db.commit()

    return {"success": True, "message": "Đã tiếp nhận sự kiện thiết bị IoT"}
