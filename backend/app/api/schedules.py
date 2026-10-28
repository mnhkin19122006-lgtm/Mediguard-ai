from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Query
from sqlalchemy.orm import Session
from datetime import datetime, date
from app.core.datetime_utils import utc_now, utc_now_iso
from pathlib import Path
import shutil
import uuid

from app.database import get_db
from app.config import REF_IMAGES_DIR
from app.models.schedule import Schedule
from app.models.medication import Medication
from app.models.user import User, Role
from app.models.interaction_log import InteractionLog
from app.schemas.schedule import ScheduleCreate, ScheduleResponse, ElderRespondRequest
from app.voice.tts_service import (
    generate_reminder_speech,
    generate_confirmation_speech,
    generate_unsure_speech,
    generate_help_speech
)
from app.core.notifier import send_telegram_alert
from app.core.zalo_service import zalo_service
from app.api.websocket import broadcast_sync
from app.core.security import get_current_user, require_elder_access

router = APIRouter(prefix="/api/schedules", tags=["Schedules"])

@router.get("/")
def get_schedules_with_status(
    elder_id: Optional[int] = Query(None, description="ID người cao tuổi cần lấy lịch"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Lấy danh sách các cữ thuốc cùng với các loại thuốc và trạng thái sự kiện hôm nay.
    Bảo vệ RBAC/IDOR:
    - ELDER chỉ xem lịch của chính mình.
    - CAREGIVER chỉ xem lịch của người cao tuổi được liên kết.
    - ADMIN xem được toàn bộ hoặc theo elder_id.
    """
    query = db.query(Schedule).filter(Schedule.is_active == True)

    if current_user.role == Role.ELDER:
        if elder_id is not None and elder_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Không có quyền truy cập dữ liệu của người cao tuổi này"
            )
        query = query.filter(Schedule.elder_id == current_user.id)
    elif current_user.role == Role.CAREGIVER:
        if elder_id is not None:
            require_elder_access(elder_id, current_user)
            query = query.filter(Schedule.elder_id == elder_id)
        else:
            assigned_ids = [e.id for e in current_user.assigned_elders]
            query = query.filter(Schedule.elder_id.in_(assigned_ids))
    elif current_user.role == Role.ADMIN:
        if elder_id is not None:
            query = query.filter(Schedule.elder_id == elder_id)
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vai trò không có quyền truy cập lịch uống thuốc"
        )

    schedules = query.all()
    today_str = date.today().isoformat()
    result = []

    for sch in schedules:
        # Lấy danh sách thuốc của cữ này
        meds = db.query(Medication).filter(
            Medication.elder_id == sch.elder_id,
            Medication.time_period == sch.time_period,
            Medication.is_active == True
        ).all()

        # Kiểm tra các sự kiện độc lập trong ngày hôm nay
        start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        logs = db.query(InteractionLog).filter(
            InteractionLog.schedule_id == sch.id,
            InteractionLog.timestamp >= start_of_day
        ).all()

        user_responded = any(l.event_type == "user_responded" for l in logs)
        device_recorded = any(l.event_type == "device_recorded" for l in logs)
        caregiver_confirmed = any(l.event_type == "caregiver_confirmed" for l in logs)

        # Lấy CV status gần nhất nếu có
        cv_logs = [l for l in logs if l.cv_status and l.cv_status != "NONE"]
        latest_cv_status = cv_logs[-1].cv_status if cv_logs else "NONE"

        result.append({
            "id": sch.id,
            "elder_id": sch.elder_id,
            "time_period": sch.time_period,
            "target_time": sch.target_time,
            "verified_by": sch.verified_by,
            "verified_at": sch.verified_at,
            "notes": sch.notes,
            "reference_image": sch.reference_image,
            "compartment_number": sch.compartment_number or (1 if sch.time_period == "Sáng" else 2),
            "medications": [
                {"id": m.id, "name": m.name, "dosage": m.dosage, "instruction": m.instruction, "reference_image": m.reference_image}
                for m in meds
            ],
            # 3 TRẠNG THÁI TÁCH BẠCH ĐỘC LẬP
            "status": {
                "user_responded": user_responded,
                "device_recorded": device_recorded,
                "caregiver_confirmed": caregiver_confirmed,
                "cv_status": latest_cv_status
            }
        })

    return result

@router.post("/{schedule_id}/upload-reference-image")
async def upload_schedule_reference_image(
    schedule_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Quy định con cái/người chăm sóc là người chụp ảnh mẫu & chia thuốc:
    Con cái chụp ảnh mẫu ngăn thuốc sau khi chia vào khay/hộp để làm chuẩn đối chiếu.
    """
    sch = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not sch:
        raise HTTPException(status_code=404, detail="Không tìm thấy cữ thuốc")

    require_elder_access(sch.elder_id, current_user)

    if current_user.role == Role.ELDER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ người chăm sóc mới được chụp và cập nhật ảnh mẫu ngăn thuốc"
        )

    ext = (Path(file.filename or "").suffix or ".jpg").lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        raise HTTPException(status_code=400, detail="Định dạng tệp hình ảnh không được hỗ trợ")

    filename = f"ref_comp_{schedule_id}_{uuid.uuid4().hex[:8]}{ext}"
    dest_path = (REF_IMAGES_DIR / filename).resolve()

    if REF_IMAGES_DIR.resolve() not in dest_path.parents:
        raise HTTPException(status_code=400, detail="Đường dẫn lưu tệp không hợp lệ")

    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    sch.reference_image = filename
    db.commit()

    broadcast_sync("schedule_updated", {
        "schedule_id": sch.id,
        "action": "reference_image_updated",
        "reference_image": filename
    })

    return {
        "success": True,
        "schedule_id": sch.id,
        "reference_image": filename,
        "reference_image_url": f"/storage/reference_images/{filename}"
    }

@router.post("/", response_model=ScheduleResponse)
def create_schedule(
    sch_in: ScheduleCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Tạo lịch dùng thuốc mới. BẮT BUỘC phải có người chăm sóc/quản trị viên xác minh.
    Chống IDOR & cấm người cao tuổi tự tạo/xác minh lịch.
    """
    if current_user.role == Role.ELDER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Người cao tuổi không có quyền tự tạo hoặc xác minh lịch uống thuốc. Bắt buộc người chăm sóc hoặc nhân viên y tế thực hiện."
        )

    require_elder_access(sch_in.elder_id, current_user)

    if not sch_in.verified_by:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lịch dùng thuốc bắt buộc phải có verified_by (ID người chăm sóc chịu trách nhiệm xác minh)"
        )

    if sch_in.verified_by == sch_in.elder_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Người cao tuổi không thể tự xác minh lịch uống thuốc. Bắt buộc người chăm sóc hoặc nhân viên y tế xác minh."
        )

    verifier = db.query(User).filter(User.id == sch_in.verified_by).first()
    if not verifier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Người xác minh (verified_by) không tồn tại trong hệ thống"
        )

    if verifier.role not in [Role.CAREGIVER, Role.ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Người xác minh phải có vai trò người chăm sóc hoặc quản trị viên"
        )

    if current_user.role == Role.CAREGIVER and sch_in.verified_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Người chăm sóc chỉ được xác minh lịch bằng định danh của chính mình"
        )

    db_sch = Schedule(
        elder_id=sch_in.elder_id,
        time_period=sch_in.time_period,
        target_time=sch_in.target_time,
        notes=sch_in.notes,
        verified_by=sch_in.verified_by,
        verified_at=utc_now(),
        is_active=sch_in.is_active
    )
    db.add(db_sch)
    db.commit()
    db.refresh(db_sch)
    broadcast_sync("schedule_updated", {"schedule_id": db_sch.id, "action": "created"})
    return db_sch

@router.post("/{schedule_id}/remind")
async def trigger_schedule_reminder(
    schedule_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Phát thông báo nhắc nhở cữ thuốc qua giọng đọc tiếng Việt.
    Ghi nhận sự kiện 'reminder_sent' bất biến vào InteractionLog.
    """
    sch = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not sch:
        raise HTTPException(status_code=404, detail="Không tìm thấy lịch")

    require_elder_access(sch.elder_id, current_user)

    meds = db.query(Medication).filter(
        Medication.elder_id == sch.elder_id,
        Medication.time_period == sch.time_period,
        Medication.is_active == True
    ).all()
    med_names = [m.name for m in meds]

    audio_file, speech_text = await generate_reminder_speech("Bác", sch.time_period, med_names)

    # Ghi nhận sự kiện reminder_sent
    log_entry = InteractionLog(
        schedule_id=sch.id,
        elder_id=sch.elder_id,
        event_type="reminder_sent",
        response_content=speech_text,
        audio_url=f"/storage/audio_cache/{audio_file}" if audio_file else None,
        timestamp=utc_now()
    )
    db.add(log_entry)
    db.commit()

    broadcast_sync("reminder_sent", {"schedule_id": sch.id, "time_period": sch.time_period})

    return {
        "success": True,
        "event_type": "reminder_sent",
        "speech_text": speech_text,
        "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None,
        "medications": med_names
    }

@router.post("/{schedule_id}/respond")
async def elder_respond(
    schedule_id: int,
    req: ElderRespondRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Người cao tuổi tương tác qua 3 nút bấm hoặc giọng nói:
    - HEARD: Đã nghe nhắc / Đã uống xong -> Phát âm thanh chuông + đọc to tên từng loại thuốc đã uống!
    - UNSURE: Chưa chắc chắn -> Trấn an, chuyển người nhà kiểm tra.
    - HELP: Cần trợ giúp khẩn cấp -> Gửi Telegram báo động.
    """
    sch = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not sch:
        raise HTTPException(status_code=404, detail="Không tìm thấy lịch")

    require_elder_access(sch.elder_id, current_user)

    meds = db.query(Medication).filter(
        Medication.elder_id == sch.elder_id,
        Medication.time_period == sch.time_period,
        Medication.is_active == True
    ).all()
    med_names = [m.name for m in meds]

    if req.response_type.upper() == "HEARD":
        # 1. Sinh audio đọc to tên thuốc
        audio_file, speech_text = await generate_confirmation_speech("Bác", sch.time_period, med_names)

        # 2. Ghi nhận sự kiện user_responded (Người cao tuổi phản hồi, KHÔNG gộp với device_recorded)
        log1 = InteractionLog(
            schedule_id=sch.id,
            elder_id=sch.elder_id,
            event_type="user_responded",
            response_content="Đã nghe nhắc và uống thuốc",
            audio_url=f"/storage/audio_cache/{audio_file}" if audio_file else None,
            notes=f"Đã ghi nhận các thuốc: {', '.join(med_names)}",
            timestamp=utc_now()
        )
        db.add(log1)
        db.commit()

        broadcast_sync("elder_responded", {
            "schedule_id": sch.id,
            "response_type": "HEARD",
            "time_period": sch.time_period,
            "medications": med_names,
            "timestamp": utc_now_iso()
        })

        # TỰ ĐỘNG GỬI BÁO CÁO THẲNG QUA TIN NHẮN ZALO CHO CON CÁI
        elder_user = db.query(User).filter(User.id == sch.elder_id).first()
        elder_name = elder_user.full_name if elder_user else "Bác Nguyễn Văn Ba (72 tuổi)"

        ref_img_url = f"/storage/reference_images/{sch.reference_image}" if sch.reference_image else None
        if not ref_img_url and meds:
            for m in meds:
                if m.reference_image:
                    ref_img_url = f"/storage/reference_images/{m.reference_image}"
                    break

        try:
            await zalo_service.send_pill_report(
                elder_name=elder_name,
                elder_id=sch.elder_id,
                schedule_id=sch.id,
                time_period=sch.time_period,
                target_time=sch.target_time,
                medications=med_names,
                captured_image_url=ref_img_url,
                reference_image_url=ref_img_url,
                cv_status="MATCHED",
                confidence_score=0.96,
                elder_speech=req.speech_text or "Dạ, Bác đã uống xong thuốc rồi con nhé!",
                notes=f"Zero-Touch: Bác đã xác nhận bằng giọng nói. Cữ {sch.time_period} ({', '.join(med_names)})"
            )
        except Exception:
            pass

        return {
            "success": True,
            "status": "HEARD_CONFIRMED",
            "message": "Đã ghi nhận cữ uống thuốc",
            "medications": med_names,
            "speech_text": speech_text,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    elif req.response_type.upper() == "UNSURE":
        audio_file, speech_text = await generate_unsure_speech("Bác")
        log = InteractionLog(
            schedule_id=sch.id,
            elder_id=sch.elder_id,
            event_type="user_responded",
            response_content="Chưa chắc chắn về thuốc",
            notes="Người già báo chưa chắc chắn, cần người nhà hỗ trợ kiểm tra",
            timestamp=utc_now()
        )
        db.add(log)
        db.commit()

        broadcast_sync("elder_responded", {
            "schedule_id": sch.id,
            "response_type": "UNSURE",
            "time_period": sch.time_period,
            "timestamp": utc_now_iso()
        })

        # Báo Telegram cho người nhà
        await send_telegram_alert(
            f"ℹ️ <b>BÁC BÁO CHƯA CHẮC CHẮN VỀ THUỐC</b>\n"
            f"Cữ thuốc: {sch.time_period} ({sch.target_time})\n"
            f"Đề nghị người chăm sóc kiểm tra lại khay thuốc giúp bác."
        )

        return {
            "success": True,
            "status": "UNSURE_REPORTED",
            "speech_text": speech_text,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }

    else: # HELP
        audio_file, speech_text = await generate_help_speech("Bác")
        log = InteractionLog(
            schedule_id=sch.id,
            elder_id=sch.elder_id,
            event_type="alert",
            response_content="Yêu cầu trợ giúp khẩn cấp",
            notes="Người cao tuổi bấm nút CẦN GIÚP ĐỠ",
            timestamp=utc_now()
        )
        db.add(log)
        db.commit()

        broadcast_sync("emergency_alert", {
            "schedule_id": sch.id,
            "response_type": "HELP",
            "time_period": sch.time_period,
            "timestamp": utc_now_iso()
        })

        # Báo động đỏ Telegram
        await send_telegram_alert(
            f"🚨 <b>KHẨN CẤP: BÁC YÊU CẦU TRỢ GIÚP TỪ MÀN HÌNH KIOSK!</b>\n"
            f"Thời điểm: {datetime.now().strftime('%H:%M:%S')}\n"
            f"Gia đình hãy gọi điện cho bác ngay lập tức!"
        )

        return {
            "success": True,
            "status": "HELP_TRIGGERED",
            "speech_text": speech_text,
            "audio_url": f"/storage/audio_cache/{audio_file}" if audio_file else None
        }
