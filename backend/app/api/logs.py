from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from datetime import datetime
from app.core.datetime_utils import utc_now

from app.database import get_db
from app.models.interaction_log import InteractionLog
from app.models.schedule import Schedule
from app.models.user import User, Role
from app.schemas.log import InteractionLogResponse, CaregiverConfirmRequest
from app.api.websocket import broadcast_sync
from app.core.security import get_current_user, require_elder_access

router = APIRouter(prefix="/api/logs", tags=["Logs"])

@router.get("/", response_model=List[InteractionLogResponse])
def get_interaction_logs(
    elder_id: Optional[int] = Query(None, description="ID người cao tuổi cần xem nhật ký"),
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Lấy toàn bộ nhật ký chuỗi sự kiện kiểm toán bất biến (Audit Trail).
    Bảo vệ RBAC/IDOR:
    - ELDER chỉ xem nhật ký của chính mình.
    - CAREGIVER chỉ xem nhật ký của người cao tuổi đã liên kết.
    - ADMIN xem được toàn bộ hoặc theo elder_id.
    """
    query = db.query(InteractionLog)

    if current_user.role == Role.ELDER:
        if elder_id is not None and elder_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Không có quyền truy cập dữ liệu của người cao tuổi này"
            )
        query = query.filter(InteractionLog.elder_id == current_user.id)
    elif current_user.role == Role.CAREGIVER:
        if elder_id is not None:
            require_elder_access(elder_id, current_user)
            query = query.filter(InteractionLog.elder_id == elder_id)
        else:
            assigned_ids = [e.id for e in current_user.assigned_elders]
            query = query.filter(InteractionLog.elder_id.in_(assigned_ids))
    elif current_user.role == Role.ADMIN:
        if elder_id is not None:
            query = query.filter(InteractionLog.elder_id == elder_id)
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vai trò không có quyền xem nhật ký"
        )

    return query.order_by(InteractionLog.timestamp.desc()).limit(limit).all()

@router.post("/{target_id}/caregiver-confirm")
def caregiver_confirm(
    target_id: int,
    req: CaregiverConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Người chăm sóc xác nhận: 'Tôi đã kiểm tra & xác nhận an toàn'.
    Ghi nhận sự kiện 'caregiver_confirmed' độc lập vào cơ sở dữ liệu.
    Chống giả mạo caregiver_id và chống IDOR chéo người cao tuổi.
    """
    if current_user.role not in [Role.CAREGIVER, Role.ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ người chăm sóc hoặc quản trị viên mới có quyền xác nhận nhật ký an toàn"
        )

    sch = db.query(Schedule).filter(Schedule.id == target_id).first()
    if sch:
        elder_id = sch.elder_id
        schedule_id = sch.id
    else:
        log_item = db.query(InteractionLog).filter(InteractionLog.id == target_id).first()
        if log_item:
            elder_id = log_item.elder_id
            schedule_id = log_item.schedule_id
        else:
            raise HTTPException(status_code=404, detail="Không tìm thấy lịch hoặc bản ghi nhật ký phù hợp")

    require_elder_access(elder_id, current_user)

    # Đảm bảo confirmed_by luôn là ID người dùng thực tế đang gửi request
    confirmed_by_id = current_user.id
    if req.caregiver_id and req.caregiver_id != current_user.id and current_user.role != Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Người chăm sóc chỉ được xác nhận bằng định danh của chính mình"
        )

    log_entry = InteractionLog(
        schedule_id=schedule_id,
        elder_id=elder_id,
        event_type="caregiver_confirmed",
        confirmed_by=confirmed_by_id,
        notes=req.notes or "Người nhà đã đối chiếu và xác nhận an toàn",
        timestamp=utc_now()
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)

    broadcast_sync("caregiver_confirmed", {
        "schedule_id": schedule_id,
        "elder_id": elder_id,
        "confirmed_by": confirmed_by_id,
        "notes": log_entry.notes,
        "timestamp": log_entry.timestamp.isoformat()
    })

    return {
        "success": True,
        "event_type": "caregiver_confirmed",
        "message": "Đã ghi nhận người chăm sóc xác nhận thành công",
        "log_id": log_entry.id
    }
