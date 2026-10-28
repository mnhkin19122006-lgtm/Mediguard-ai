"""
MediGuard AI - Scheduler Service (Hệ thống lập lịch & Giám sát an toàn)
1. Tự động kích hoạt thông báo nhắc thuốc theo giờ đã định.
2. Bộ giám sát quá hạn (Timeout Watcher): Nếu sau NO_RESPONSE_MINUTES mà bác chưa
   bấm phản hồi thì tự động gửi cảnh báo khẩn cấp cho người thân qua Telegram.
"""

from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from app.core.datetime_utils import utc_now, utc_now_iso
import asyncio
import logging
from app.config import NO_RESPONSE_MINUTES
from app.database import SessionLocal
from app.models.schedule import Schedule
from app.models.interaction_log import InteractionLog
from app.core.notifier import send_telegram_alert

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()

def check_unresponsive_elders():
    """
    Watcher chạy định kỳ mỗi phút:
    Tìm các lời nhắc đã phát > NO_RESPONSE_MINUTES mà người cao tuổi chưa có phản hồi.
    """
    db = SessionLocal()
    try:
        cutoff_time = utc_now() - timedelta(minutes=NO_RESPONSE_MINUTES)
        
        # 1. Lấy tất cả sự kiện 'reminder_sent' đã phát trước thời điểm cutoff
        recent_reminders = db.query(InteractionLog).filter(
            InteractionLog.event_type == "reminder_sent",
            InteractionLog.timestamp <= cutoff_time,
            InteractionLog.timestamp >= utc_now() - timedelta(hours=3) # Trong vòng 3 tiếng gần nhất
        ).all()

        for reminder in recent_reminders:
            # 2. Kiểm tra xem sau lần nhắc này đã có user_responded hoặc caregiver_confirmed chưa
            has_response = db.query(InteractionLog).filter(
                InteractionLog.schedule_id == reminder.schedule_id,
                InteractionLog.event_type.in_(["user_responded", "caregiver_confirmed"]),
                InteractionLog.timestamp >= reminder.timestamp
            ).first()

            if not has_response:
                # 3. Kiểm tra xem hệ thống đã gửi cảnh báo 'alert' cho cữ này chưa (tránh spam)
                alert_already_sent = db.query(InteractionLog).filter(
                    InteractionLog.schedule_id == reminder.schedule_id,
                    InteractionLog.event_type == "alert",
                    InteractionLog.timestamp >= reminder.timestamp
                ).first()

                if not alert_already_sent:
                    # Ghi nhận sự kiện cảnh báo bất biến vào DB
                    alert_log = InteractionLog(
                        schedule_id=reminder.schedule_id,
                        elder_id=reminder.elder_id,
                        event_type="alert",
                        notes=f"Cảnh báo: Đã quá {NO_RESPONSE_MINUTES} phút kể từ lời nhắc mà bác chưa phản hồi.",
                        timestamp=utc_now()
                    )
                    db.add(alert_log)
                    db.commit()

                    msg = (
                        f"⚠️ <b>NGƯỜI THÂN CHƯA PHẢN HỒI LỊCH THUỐC!</b>\n"
                        f"• Thời điểm nhắc: {reminder.timestamp.strftime('%H:%M:%S')}\n"
                        f"• Thời gian chờ: Đã hơn {NO_RESPONSE_MINUTES} phút\n"
                        f"• Trạng thái: Chưa nhận được phản hồi qua màn hình Kiosk.\n"
                        f"👉 <i>Đề nghị gia đình chủ động gọi điện kiểm tra sức khỏe của bác!</i>"
                    )
                    # Chạy gửi telegram không chặn
                    try:
                        asyncio.run(send_telegram_alert(msg))
                    except Exception as ex:
                        logger.error(f"Lỗi gửi cảnh báo telegram trong watcher: {ex}")

    except Exception as e:
        logger.error(f"Lỗi trong check_unresponsive_elders: {e}")
    finally:
        db.close()


def check_and_trigger_scheduled_ai_calls():
    """
    Tự động gọi điện hỏi thăm bệnh nhân theo lịch:
    Chạy định kỳ mỗi phút, đối chiếu target_time của cữ thuốc với giờ hiện tại.
    Nếu khớp giờ và hôm nay chưa gọi, hệ thống tự động phát tín hiệu cuộc gọi AI.
    """
    from datetime import date
    from app.models.user import User
    from app.models.medication import Medication
    from app.models.call_report import AICallReport
    from app.core.call_ai_service import DEFAULT_APPROVED_QUESTIONS
    from app.api.websocket import broadcast_sync

    db = SessionLocal()
    try:
        now_time = datetime.now().strftime("%H:%M")
        today_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)

        # Tìm các lịch cữ thuốc khớp giờ hiện tại
        matching_schedules = db.query(Schedule).filter(
            Schedule.target_time == now_time,
            Schedule.is_active == True
        ).all()

        for sch in matching_schedules:
            # Kiểm tra xem hôm nay đã có cuộc gọi cho cữ này chưa
            already_called = db.query(AICallReport).filter(
                AICallReport.elder_id == sch.elder_id,
                AICallReport.schedule_id == sch.id,
                AICallReport.call_time >= today_start
            ).first()

            if not already_called:
                elder = db.query(User).filter(User.id == sch.elder_id).first()
                if not elder:
                    continue

                meds = db.query(Medication).filter(
                    Medication.elder_id == sch.elder_id,
                    Medication.time_period == sch.time_period,
                    Medication.is_active == True
                ).all()
                med_names = [m.name for m in meds]

                # Chuẩn bị câu hỏi duyệt cá nhân hóa
                custom_questions = []
                for q in DEFAULT_APPROVED_QUESTIONS:
                    q_c = dict(q)
                    if q["id"] == "q1_adherence":
                        med_str = ", ".join(med_names) if med_names else "thuốc chỉ định"
                        q_c["question"] = (
                            f"Kính chào {elder.full_name}! Cháu là Trợ lý Y tế AI gọi thăm sức khỏe Bác theo lịch cữ {sch.time_period}. "
                            f"Các loại thuốc gồm {med_str}, Bác đã uống đầy đủ chưa ạ?"
                        )
                    custom_questions.append(q_c)

                # Phát tín hiệu cuộc gọi tự động đến Kiosk của bệnh nhân
                call_payload = {
                    "elder_id": elder.id,
                    "elder_name": elder.full_name,
                    "elder_phone": elder.phone_number or "0901234567",
                    "schedule_id": sch.id,
                    "scheduled_time": sch.target_time,
                    "time_period": sch.time_period,
                    "medications": med_names,
                    "approved_questions": custom_questions,
                    "timestamp": utc_now_iso(),
                    "call_type": "TỰ ĐỘNG THEO LỊCH",
                }

                broadcast_sync("incoming_call", call_payload)
                logger.info(f"[Scheduler] Đã kích hoạt cuộc gọi AI tự động tới {elder.full_name} lúc {now_time}")
    except Exception as e:
        logger.error(f"Lỗi trong check_and_trigger_scheduled_ai_calls: {e}")
    finally:
        db.close()


def start_scheduler():
    """
    Khởi động Scheduler chạy nền.
    """
    if not scheduler.running:
        scheduler.add_job(check_unresponsive_elders, 'interval', minutes=1, id="unresponsive_watcher")
        scheduler.add_job(check_and_trigger_scheduled_ai_calls, 'interval', minutes=1, id="ai_calls_scheduler")
        scheduler.start()
        logger.info("Scheduler Service đã khởi chạy thành công với tính năng Gọi Tự Động AI.")


def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)

