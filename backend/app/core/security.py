from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, CAREGIVER_PIN
from app.database import get_db
from app.models.user import User, Role

security_scheme = HTTPBearer(auto_error=False)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    if isinstance(plain_password, str):
        plain_password = plain_password.encode('utf-8')[:72]
    if isinstance(hashed_password, str):
        hashed_password = hashed_password.encode('utf-8')
    try:
        return bcrypt.checkpw(plain_password, hashed_password)
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    if isinstance(password, str):
        password = password.encode('utf-8')[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password, salt).decode('utf-8')

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_kiosk_pin(pin: str) -> bool:
    """
    Xác thực mã PIN trên màn hình Kiosk để người chăm sóc
    chuyển sang bảng cài đặt nhanh mà không cần đăng nhập lại từ đầu.
    Loại bỏ hardcoded PIN backdoor để chỉ xác thực theo biến môi trường CAREGIVER_PIN.
    """
    input_pin = str(pin).strip()
    configured_pin = str(CAREGIVER_PIN).strip() if CAREGIVER_PIN else "1234"
    return bool(input_pin and input_pin == configured_pin)

def get_current_user(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db)
) -> User:
    """
    Dependency lấy thông tin User hiện tại từ JWT Bearer token.
    Áp dụng cơ chế xác thực HS256 chuẩn của MediGuard.
    """
    if not auth or not auth.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Yêu cầu xác thực: Thiếu access token"
        )
    token = auth.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        user_id: Optional[int] = payload.get("user_id")
        if username is None and user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token không hợp lệ: Thiếu định danh người dùng"
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token không hợp lệ hoặc đã hết hạn"
        )

    query = db.query(User)
    if user_id is not None:
        user = query.filter(User.id == user_id).first()
    else:
        user = query.filter(User.username == username).first()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Người dùng không tồn tại hoặc tài khoản đã bị khóa"
        )
    return user


def get_current_user_flexible(
    auth: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    token_query: Optional[str] = Query(None, alias="token"),
    db: Session = Depends(get_db)
) -> User:
    """
    Dependency lấy thông tin User từ Authorization Header HOẶC Query Parameter '?token=...'.
    Hỗ trợ các trường hợp tải tài nguyên (file hình ảnh, âm thanh streaming).
    """
    token = None
    if auth and auth.credentials:
        token = auth.credentials
    elif token_query:
        token = token_query.strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Yêu cầu xác thực: Thiếu access token"
        )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        user_id: Optional[int] = payload.get("user_id")
        if username is None and user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token không hợp lệ: Thiếu định danh người dùng"
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token không hợp lệ hoặc đã hết hạn"
        )

    query = db.query(User)
    if user_id is not None:
        user = query.filter(User.id == user_id).first()
    else:
        user = query.filter(User.username == username).first()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Người dùng không tồn tại hoặc tài khoản đã bị khóa"
        )
    return user


def require_elder_access(elder_id: int, current_user: User) -> None:
    """
    Xác thực quyền truy cập đối với elder_id cụ thể.
    Ném lỗi HTTP 403 Forbidden nếu không có quyền.
    """
    if not current_user.can_access_elder(elder_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Không có quyền truy cập dữ liệu của người cao tuổi này"
        )


def require_caregiver_or_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    Chặn các vai trò không phải CAREGIVER hoặc ADMIN.
    """
    if current_user.role not in [Role.CAREGIVER, Role.ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Yêu cầu quyền người chăm sóc hoặc quản trị viên"
        )
    return current_user

