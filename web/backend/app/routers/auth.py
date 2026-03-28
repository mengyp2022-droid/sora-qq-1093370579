from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from app.config import settings
from app.database import get_db, init_db
from app.security import get_password_hash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
security = HTTPBearer(auto_error=False)


def _check_admin(username: str, password: str) -> bool:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT password_hash FROM admin_users WHERE username = ?", (username,))
        row = c.fetchone()
        if row and row[0]:
            return verify_password(password, row[0])
    # 无 DB 记录时：先认默认 admin/admin123，再认配置
    if username == "admin" and password == "admin123":
        return True
    return username == settings.admin_username and password == settings.admin_password


def create_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=24)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=["HS256"])
        username = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Invalid token")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_optional_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """不抛错，无凭证或无效时返回 None。用于 debug 等可选鉴权接口。"""
    if not credentials or not credentials.credentials:
        return None
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=["HS256"])
        return payload.get("sub") or None
    except JWTError:
        return None


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    username: str


@router.post("/login", response_model=LoginOut)
def login(data: LoginIn):
    init_db()
    if not _check_admin(data.username, data.password):
        raise HTTPException(status_code=401, detail="Wrong username or password")
    token = create_token(data.username)
    return LoginOut(token=token, username=data.username)


@router.get("/me")
def me(username: str = Depends(get_current_user)):
    return {"username": username}
