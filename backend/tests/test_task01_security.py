import pytest
import io
import uuid
from pathlib import Path
from datetime import datetime, date
from app.core.datetime_utils import utc_now

from fastapi.testclient import TestClient
from main import app
from app.database import SessionLocal
from app.models.user import User, Role
from app.models.medication import Medication
from app.models.schedule import Schedule
from app.models.interaction_log import InteractionLog
from app.models.symptom_log import SymptomLog
from app.config import STORAGE_DIR, REF_IMAGES_DIR, CAPTURED_IMAGES_DIR
from app.core.security import get_password_hash, create_access_token

client = TestClient(app)


@pytest.fixture
def sec_env():
    """
    Khởi tạo môi trường dữ liệu cách ly cho kiểm thử bảo mật & phân quyền:
    - 2 Người cao tuổi (elder_1, elder_2)
    - 2 Người chăm sóc (cg_1 liên kết elder_1, cg_2 liên kết elder_2)
    - 1 Quản trị viên (admin)
    - 1 Thuốc và 1 Lịch dùng thuốc mẫu cho elder_1 và elder_2
    - Tệp hình ảnh và âm thanh mẫu kiểm thử lưu trữ an toàn
    """
    db = SessionLocal()

    # Dọn dẹp dữ liệu kiểm thử cũ nếu có
    usernames = ["sec_elder_1", "sec_elder_2", "sec_cg_1", "sec_cg_2", "sec_admin"]
    existing_users = db.query(User).filter(User.username.in_(usernames)).all()
    user_ids = [u.id for u in existing_users]
    if user_ids:
        db.query(SymptomLog).filter(SymptomLog.elder_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(Schedule).filter((Schedule.elder_id.in_(user_ids)) | (Schedule.verified_by.in_(user_ids))).delete(synchronize_session=False)
        db.query(Medication).filter(Medication.elder_id.in_(user_ids)).delete(synchronize_session=False)
        for u in existing_users:
            db.delete(u)
        db.commit()

    e1 = User(
        username="sec_elder_1",
        full_name="Bác An (Elder 1)",
        hashed_password=get_password_hash("pass123"),
        role=Role.ELDER
    )
    e2 = User(
        username="sec_elder_2",
        full_name="Bác Bình (Elder 2)",
        hashed_password=get_password_hash("pass123"),
        role=Role.ELDER
    )
    cg1 = User(
        username="sec_cg_1",
        full_name="Chăm sóc Viên 1",
        hashed_password=get_password_hash("pass123"),
        role=Role.CAREGIVER
    )
    cg2 = User(
        username="sec_cg_2",
        full_name="Chăm sóc Viên 2",
        hashed_password=get_password_hash("pass123"),
        role=Role.CAREGIVER
    )
    adm = User(
        username="sec_admin",
        full_name="Quản Trị Viên",
        hashed_password=get_password_hash("pass123"),
        role=Role.ADMIN
    )

    db.add_all([e1, e2, cg1, cg2, adm])
    db.commit()
    db.refresh(e1)
    db.refresh(e2)
    db.refresh(cg1)
    db.refresh(cg2)
    db.refresh(adm)

    # Phân quyền: cg1 -> elder 1, cg2 -> elder 2
    cg1.assigned_elders.append(e1)
    cg2.assigned_elders.append(e2)
    db.commit()

    # Tạo tệp ảnh mẫu thực tế trên đĩa để kiểm thử storage
    REF_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    img_filename_1 = f"sec_ref_med_{e1.id}_{uuid.uuid4().hex[:8]}.jpg"
    test_img_path = REF_IMAGES_DIR / img_filename_1
    test_img_path.write_bytes(b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01test_image_data")

    # Tạo thuốc cho elder 1 & elder 2
    med1 = Medication(
        elder_id=e1.id,
        name="Thuốc Tim Mạch Elder 1",
        dosage="1 viên",
        instruction="Uống sáng",
        time_period="Sáng",
        reference_image=img_filename_1,
        is_active=True
    )
    med2 = Medication(
        elder_id=e2.id,
        name="Thuốc Tiểu Đường Elder 2",
        dosage="1 viên",
        instruction="Uống trưa",
        time_period="Trưa",
        is_active=True
    )
    db.add_all([med1, med2])
    db.commit()
    db.refresh(med1)
    db.refresh(med2)

    # Tạo lịch cho elder 1 & elder 2
    sch1 = Schedule(
        elder_id=e1.id,
        time_period="Sáng",
        target_time="07:30",
        verified_by=cg1.id,
        reference_image=img_filename_1,
        is_active=True
    )
    sch2 = Schedule(
        elder_id=e2.id,
        time_period="Trưa",
        target_time="12:00",
        verified_by=cg2.id,
        is_active=True
    )
    db.add_all([sch1, sch2])
    db.commit()
    db.refresh(sch1)
    db.refresh(sch2)

    # Tạo triệu chứng và log cho elder 1
    symp1 = SymptomLog(
        elder_id=e1.id,
        symptom_name="Chóng mặt",
        severity="Trung bình",
        reported_date=date.today().isoformat(),
        reported_time="08:00",
        caregiver_confirmed=False
    )
    log1 = InteractionLog(
        schedule_id=sch1.id,
        elder_id=e1.id,
        event_type="user_responded",
        response_content="Đã uống thuốc",
        timestamp=utc_now()
    )
    db.add_all([symp1, log1])
    db.commit()
    db.refresh(symp1)
    db.refresh(log1)

    # Sinh JWT tokens động
    token_e1 = create_access_token({"sub": e1.username, "user_id": e1.id, "role": e1.role})
    token_e2 = create_access_token({"sub": e2.username, "user_id": e2.id, "role": e2.role})
    token_cg1 = create_access_token({"sub": cg1.username, "user_id": cg1.id, "role": cg1.role})
    token_cg2 = create_access_token({"sub": cg2.username, "user_id": cg2.id, "role": cg2.role})
    token_adm = create_access_token({"sub": adm.username, "user_id": adm.id, "role": adm.role})

    yield {
        "e1": e1, "e2": e2,
        "cg1": cg1, "cg2": cg2,
        "adm": adm,
        "token_e1": token_e1, "token_e2": token_e2,
        "token_cg1": token_cg1, "token_cg2": token_cg2,
        "token_adm": token_adm,
        "med1": med1, "med2": med2,
        "sch1": sch1, "sch2": sch2,
        "symp1": symp1, "log1": log1,
        "img_filename_1": img_filename_1,
        "test_img_path": test_img_path
    }

    # Dọn dẹp sau test
    try:
        test_img_path.unlink(missing_ok=True)
    except Exception:
        pass

    db_clean = SessionLocal()
    existing_clean_users = db_clean.query(User).filter(User.username.in_(usernames)).all()
    clean_ids = [u.id for u in existing_clean_users]
    if clean_ids:
        db_clean.query(SymptomLog).filter(SymptomLog.elder_id.in_(clean_ids)).delete(synchronize_session=False)
        db_clean.query(Schedule).filter((Schedule.elder_id.in_(clean_ids)) | (Schedule.verified_by.in_(clean_ids))).delete(synchronize_session=False)
        db_clean.query(Medication).filter(Medication.elder_id.in_(clean_ids)).delete(synchronize_session=False)
        for u in existing_clean_users:
            db_clean.delete(u)
        db_clean.commit()
    db_clean.close()
    db.close()


# ==============================================================================
# NHÓM 1: KIỂM THỬ TỪ CHỐI KHI CHƯA XÁC THỰC (401 UNAUTHORIZED)
# ==============================================================================

def test_unauthenticated_requests_fail_with_401(sec_env):
    """Mọi endpoint y tế và quản lý cấu hình bắt buộc phải xác thực người dùng"""
    e1_id = sec_env["e1"].id
    sch1_id = sec_env["sch1"].id
    med1_id = sec_env["med1"].id
    symp1_id = sec_env["symp1"].id

    endpoints = [
        ("GET", f"/api/schedules/?elder_id={e1_id}"),
        ("POST", "/api/schedules/"),
        ("POST", f"/api/schedules/{sch1_id}/upload-reference-image"),
        ("POST", f"/api/schedules/{sch1_id}/remind"),
        ("POST", f"/api/schedules/{sch1_id}/respond"),
        ("GET", f"/api/medications/?elder_id={e1_id}"),
        ("POST", "/api/medications/"),
        ("POST", f"/api/medications/{med1_id}/upload-reference-image"),
        ("DELETE", f"/api/medications/{med1_id}"),
        ("GET", f"/api/logs/?elder_id={e1_id}"),
        ("POST", f"/api/logs/{sch1_id}/caregiver-confirm"),
        ("GET", f"/api/symptoms/?elder_id={e1_id}"),
        ("POST", "/api/symptoms/report"),
        ("POST", f"/api/symptoms/{symp1_id}/confirm"),
        ("GET", "/api/ai/llm-config"),
        ("POST", "/api/ai/llm-config"),
        ("GET", f"/storage/reference_images/{sec_env['img_filename_1']}"),
        ("GET", f"/api/storage/reference_images/{sec_env['img_filename_1']}"),
    ]

    for method, path in endpoints:
        if method == "GET":
            res = client.get(path)
        elif method == "POST":
            res = client.post(path, json={})
        elif method == "DELETE":
            res = client.delete(path)
        assert res.status_code == 401, f"Lỗ hổng: {method} {path} trả về {res.status_code} thay vì 401!"


# ==============================================================================
# NHÓM 2: KIỂM THỬ PHÂN QUYỀN VAI TRÒ (WRONG ROLE -> 403 FORBIDDEN)
# ==============================================================================

def test_elder_cannot_create_schedule(sec_env):
    """Người cao tuổi KHÔNG có quyền tự tạo lịch uống thuốc"""
    res = client.post(
        "/api/schedules/",
        json={
            "elder_id": sec_env["e1"].id,
            "time_period": "Tối",
            "target_time": "19:00",
            "verified_by": sec_env["cg1"].id
        },
        headers={"Authorization": f"Bearer {sec_env['token_e1']}"}
    )
    assert res.status_code == 403
    assert "tự tạo" in res.json()["detail"].lower() or "quyền" in res.json()["detail"].lower()


def test_elder_cannot_confirm_logs(sec_env):
    """Người cao tuổi KHÔNG thể tự xác nhận an toàn nhật ký điều trị"""
    res = client.post(
        f"/api/logs/{sec_env['sch1'].id}/caregiver-confirm",
        json={"caregiver_id": sec_env["e1"].id, "notes": "Tự duyệt"},
        headers={"Authorization": f"Bearer {sec_env['token_e1']}"}
    )
    assert res.status_code == 403


def test_elder_cannot_confirm_symptoms(sec_env):
    """Người cao tuổi KHÔNG thể tự xác nhận xử lý triệu chứng"""
    res = client.post(
        f"/api/symptoms/{sec_env['symp1'].id}/confirm",
        json={"notes": "Tự duyệt triệu chứng"},
        headers={"Authorization": f"Bearer {sec_env['token_e1']}"}
    )
    assert res.status_code == 403


def test_elder_cannot_access_or_update_llm_config(sec_env):
    """Người cao tuổi KHÔNG được quyền xem hoặc thay đổi cấu hình API LLM"""
    res_get = client.get(
        "/api/ai/llm-config",
        headers={"Authorization": f"Bearer {sec_env['token_e1']}"}
    )
    assert res_get.status_code == 403

    res_post = client.post(
        "/api/ai/llm-config",
        json={"gemini_api_key": "AIzaSyFakeElderKeyTesting12345"},
        headers={"Authorization": f"Bearer {sec_env['token_e1']}"}
    )
    assert res_post.status_code == 403


# ==============================================================================
# NHÓM 3: CHỐNG TRUY CẬP CHÉO IDOR GIỮA CÁC NGƯỜI CAO TUỔI (CROSS-ELDER IDOR)
# ==============================================================================

def test_elder_cannot_access_another_elder_data(sec_env):
    """Elder 1 KHÔNG được xem hoặc chỉnh sửa dữ liệu của Elder 2"""
    e2_id = sec_env["e2"].id
    token_e1 = sec_env["token_e1"]

    # Xem trộm lịch
    r1 = client.get(f"/api/schedules/?elder_id={e2_id}", headers={"Authorization": f"Bearer {token_e1}"})
    assert r1.status_code == 403

    # Xem trộm thuốc
    r2 = client.get(f"/api/medications/?elder_id={e2_id}", headers={"Authorization": f"Bearer {token_e1}"})
    assert r2.status_code == 403

    # Xem trộm nhật ký
    r3 = client.get(f"/api/logs/?elder_id={e2_id}", headers={"Authorization": f"Bearer {token_e1}"})
    assert r3.status_code == 403

    # Xem trộm triệu chứng
    r4 = client.get(f"/api/symptoms/?elder_id={e2_id}", headers={"Authorization": f"Bearer {token_e1}"})
    assert r4.status_code == 403

    # Báo cáo triệu chứng giả mạo cho Elder 2
    r5 = client.post(
        "/api/symptoms/report",
        json={"elder_id": e2_id, "symptom_name": "Đau ngực giả mạo"},
        headers={"Authorization": f"Bearer {token_e1}"}
    )
    assert r5.status_code == 403

    # Phản hồi lịch uống thuốc của Elder 2
    r6 = client.post(
        f"/api/schedules/{sec_env['sch2'].id}/respond",
        json={"response_type": "HEARD"},
        headers={"Authorization": f"Bearer {token_e1}"}
    )
    assert r6.status_code == 403

    # Xóa thuốc của Elder 2
    r7 = client.delete(
        f"/api/medications/{sec_env['med2'].id}",
        headers={"Authorization": f"Bearer {token_e1}"}
    )
    assert r7.status_code == 403


# ==============================================================================
# NHÓM 4: NGƯỜI CHĂM SÓC KHÔNG ĐƯỢC LIÊN KẾT (UNLINKED CAREGIVER -> 403)
# ==============================================================================

def test_unlinked_caregiver_cannot_access_unassigned_elder(sec_env):
    """Caregiver 2 (chỉ quản lý Elder 2) KHÔNG thể truy cập dữ liệu của Elder 1"""
    e1_id = sec_env["e1"].id
    token_cg2 = sec_env["token_cg2"]

    # Xem lịch của Elder 1
    r1 = client.get(f"/api/schedules/?elder_id={e1_id}", headers={"Authorization": f"Bearer {token_cg2}"})
    assert r1.status_code == 403

    # Xem thuốc của Elder 1
    r2 = client.get(f"/api/medications/?elder_id={e1_id}", headers={"Authorization": f"Bearer {token_cg2}"})
    assert r2.status_code == 403

    # Xem nhật ký của Elder 1
    r3 = client.get(f"/api/logs/?elder_id={e1_id}", headers={"Authorization": f"Bearer {token_cg2}"})
    assert r3.status_code == 403

    # Xem triệu chứng của Elder 1
    r4 = client.get(f"/api/symptoms/?elder_id={e1_id}", headers={"Authorization": f"Bearer {token_cg2}"})
    assert r4.status_code == 403

    # Tạo thuốc cho Elder 1
    r5 = client.post(
        "/api/medications/",
        json={"elder_id": e1_id, "name": "Thuốc trái phép", "dosage": "1", "instruction": "uống", "time_period": "Sáng"},
        headers={"Authorization": f"Bearer {token_cg2}"}
    )
    assert r5.status_code == 403

    # Xác nhận log của Elder 1
    r6 = client.post(
        f"/api/logs/{sec_env['sch1'].id}/caregiver-confirm",
        json={"caregiver_id": sec_env["cg2"].id, "notes": "Xác nhận trái phép"},
        headers={"Authorization": f"Bearer {token_cg2}"}
    )
    assert r6.status_code == 403

    # Xác nhận triệu chứng của Elder 1
    r7 = client.post(
        f"/api/symptoms/{sec_env['symp1'].id}/confirm",
        json={"caregiver_id": sec_env["cg2"].id},
        headers={"Authorization": f"Bearer {token_cg2}"}
    )
    assert r7.status_code == 403


# ==============================================================================
# NHÓM 5: THAO TÁC HỢP LỆ CỦA NGƯỜI CHĂM SÓC ĐƯỢC LIÊN KẾT & NGƯỜI CAO TUỔI (200 OK)
# ==============================================================================

def test_linked_caregiver_and_elder_legitimate_access(sec_env):
    """Caregiver 1 và Elder 1 thao tác thành công trên dữ liệu của Elder 1"""
    e1_id = sec_env["e1"].id
    token_cg1 = sec_env["token_cg1"]
    token_e1 = sec_env["token_e1"]

    # Caregiver 1 đọc lịch, thuốc, log, triệu chứng của Elder 1
    assert client.get(f"/api/schedules/?elder_id={e1_id}", headers={"Authorization": f"Bearer {token_cg1}"}).status_code == 200
    assert client.get(f"/api/medications/?elder_id={e1_id}", headers={"Authorization": f"Bearer {token_cg1}"}).status_code == 200
    assert client.get(f"/api/logs/?elder_id={e1_id}", headers={"Authorization": f"Bearer {token_cg1}"}).status_code == 200
    assert client.get(f"/api/symptoms/?elder_id={e1_id}", headers={"Authorization": f"Bearer {token_cg1}"}).status_code == 200

    # Elder 1 đọc dữ liệu của chính mình
    assert client.get("/api/schedules/", headers={"Authorization": f"Bearer {token_e1}"}).status_code == 200
    assert client.get("/api/medications/", headers={"Authorization": f"Bearer {token_e1}"}).status_code == 200
    assert client.get("/api/logs/", headers={"Authorization": f"Bearer {token_e1}"}).status_code == 200
    assert client.get("/api/symptoms/", headers={"Authorization": f"Bearer {token_e1}"}).status_code == 200

    # Caregiver 1 xác nhận nhật ký an toàn cho Elder 1
    res_confirm = client.post(
        f"/api/logs/{sec_env['sch1'].id}/caregiver-confirm",
        json={"caregiver_id": sec_env["cg1"].id, "notes": "Kiểm tra đúng thuốc"},
        headers={"Authorization": f"Bearer {token_cg1}"}
    )
    assert res_confirm.status_code == 200
    assert res_confirm.json()["success"] is True

    # Elder 1 phản hồi lịch uống thuốc
    res_respond = client.post(
        f"/api/schedules/{sec_env['sch1'].id}/respond",
        json={"response_type": "HEARD"},
        headers={"Authorization": f"Bearer {token_e1}"}
    )
    assert res_respond.status_code == 200


# ==============================================================================
# NHÓM 6: QUYỀN QUẢN TRỊ VIÊN (ADMIN PRIVILEGES -> 200 OK)
# ==============================================================================

def test_admin_access_and_llm_config(sec_env):
    """Quản trị viên có toàn quyền xem dữ liệu và cấu hình hệ thống LLM"""
    token_adm = sec_env["token_adm"]

    # Admin xem lịch và thuốc của bất kỳ ai
    r_sch = client.get(f"/api/schedules/?elder_id={sec_env['e1'].id}", headers={"Authorization": f"Bearer {token_adm}"})
    assert r_sch.status_code == 200

    # Admin xem cấu hình LLM
    r_cfg = client.get("/api/ai/llm-config", headers={"Authorization": f"Bearer {token_adm}"})
    assert r_cfg.status_code == 200
    cfg_data = r_cfg.json()
    assert "is_activated" in cfg_data

    # API Key không được trả về dạng thô (phải bị che)
    if cfg_data.get("gemini_masked"):
        assert not cfg_data["gemini_masked"].startswith("AIzaSy") or "..." in cfg_data["gemini_masked"]

    # Admin cập nhật cấu hình LLM
    r_up = client.post(
        "/api/ai/llm-config",
        json={"gemini_api_key": "AIzaSySecretTestingKeyForValidation999"},
        headers={"Authorization": f"Bearer {token_adm}"}
    )
    assert r_up.status_code == 200
    up_data = r_up.json()
    assert up_data["success"] is True
    assert "AIzaSySecretTestingKeyForValidation999" not in str(up_data)


# ==============================================================================
# NHÓM 7: KIỂM THỬ BẢO VỆ TỆP LƯU TRỮ VÀ CHỐNG PATH TRAVERSAL
# ==============================================================================

def test_storage_root_sensitive_json_files_not_exposed(sec_env):
    """Các tệp cấu hình JSON trong thư mục gốc /storage không được truy cập trực tiếp"""
    r1 = client.get("/storage/companion_preferences.json")
    assert r1.status_code in [401, 404]

    r2 = client.get("/storage/companion_sessions.json")
    assert r2.status_code in [401, 404]

    r3 = client.get("/storage/zalo_messages.json")
    assert r3.status_code in [401, 404]


def test_storage_path_traversal_blocked(sec_env):
    """Chặn triệt để tấn công Path Traversal trên endpoint tải tệp"""
    token_cg1 = sec_env["token_cg1"]

    traversal_payloads = [
        "..%2f..%2fmediguard.db",
        "../../main.py",
        "..\\..\\app\\config.py",
        "%2e%2e%2f%2e%2e%2f.env",
        "/etc/passwd",
        "C:\\Windows\\win.ini"
    ]

    for payload in traversal_payloads:
        res = client.get(
            f"/storage/reference_images/{payload}",
            headers={"Authorization": f"Bearer {token_cg1}"}
        )
        assert res.status_code in [400, 404], f"Lỗ hổng Path Traversal không bị chặn với payload {payload}"


def test_storage_authorization_and_token_query_streaming(sec_env):
    """
    Tải tệp hình ảnh/âm thanh yêu cầu phân quyền sở hữu:
    - Caregiver 2 không được tải ảnh thuốc của Elder 1 (403)
    - Caregiver 1 tải được ảnh thuốc của Elder 1 bằng Header Bearer (200)
    - Elder 1 tải được ảnh thuốc bằng Query Parameter '?token=...' (200)
    """
    img_name = sec_env["img_filename_1"]
    token_cg1 = sec_env["token_cg1"]
    token_cg2 = sec_env["token_cg2"]
    token_e1 = sec_env["token_e1"]

    # 1. Caregiver 2 không liên kết -> 403 Forbidden
    res_cg2 = client.get(
        f"/storage/reference_images/{img_name}",
        headers={"Authorization": f"Bearer {token_cg2}"}
    )
    assert res_cg2.status_code == 403

    # 2. Caregiver 1 liên kết -> 200 OK
    res_cg1 = client.get(
        f"/storage/reference_images/{img_name}",
        headers={"Authorization": f"Bearer {token_cg1}"}
    )
    assert res_cg1.status_code == 200
    assert len(res_cg1.content) > 0

    # 3. Elder 1 tải qua ?token=... (hỗ trợ hiển thị <img src="..." /> trên giao diện) -> 200 OK
    res_query = client.get(f"/storage/reference_images/{img_name}?token={token_e1}")
    assert res_query.status_code == 200
    assert len(res_query.content) > 0


# ==============================================================================
# NHÓM 8: KIỂM THỬ AN TOÀN TỰ XÁC MINH (SELF-VERIFICATION SAFETY INVARIANT)
# ==============================================================================

def test_schedule_creation_rejects_elder_self_verification(sec_env):
    """Tạo lịch uống thuốc với verified_by = elder_id phải bị từ chối bằng 400 Bad Request"""
    res = client.post(
        "/api/schedules/",
        json={
            "elder_id": sec_env["e1"].id,
            "time_period": "Trưa",
            "target_time": "11:30",
            "verified_by": sec_env["e1"].id  # VI PHẠM TỰ XÁC MINH
        },
        headers={"Authorization": f"Bearer {sec_env['token_cg1']}"}
    )
    assert res.status_code == 400
    assert "tự xác minh" in res.json()["detail"].lower()
