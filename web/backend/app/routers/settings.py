from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.routers.auth import get_current_user
from app.database import get_db, init_db
from app.security import get_password_hash

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _clamp_retry(value: str) -> str:
    try:
        n = int((value or "").strip())
        return str(max(1, min(5, n)))
    except (ValueError, TypeError):
        return "2"


class LoginUpdateBody(BaseModel):
    admin_username: str = ""
    admin_password: str = ""


class SettingsBody(BaseModel):
    sms_api_url: str = ""
    sms_api_key: str = ""
    sms_openai_service: str = "openai"
    sms_max_price: str = "0.55"
    thread_count: str = "1"
    retry_count: str = "2"
    proxy_url: str = ""
    proxy_api_url: str = ""
    bank_card_api_url: str = ""
    bank_card_api_key: str = ""
    bank_card_api_platform: str = "寻汇"
    email_api_url: str = ""
    email_api_key: str = ""
    email_api_default_type: str = "outlook"
    captcha_api_url: str = ""
    captcha_api_key: str = ""
    card_use_limit: str = "1"
    phone_bind_limit: str = "1"
    oauth_client_id: str = ""
    oauth_redirect_uri: str = ""
    admin_username: str = ""
    admin_password: str = ""


@router.get("")
def get_settings(username: str = Depends(get_current_user)):
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT key, value FROM system_settings")
        rows = c.fetchall()
    out = {}
    for k, v in rows:
        out[k] = v or ""
    return out


@router.put("")
def update_settings(body: SettingsBody, username: str = Depends(get_current_user)):
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        for key, value in [
            ("sms_api_url", body.sms_api_url),
            ("sms_api_key", body.sms_api_key),
            ("sms_openai_service", (body.sms_openai_service or "dr").strip()),
            ("sms_max_price", (body.sms_max_price or "0.55").strip()),
            ("thread_count", body.thread_count),
            ("retry_count", _clamp_retry(body.retry_count)),
            ("proxy_url", body.proxy_url),
            ("proxy_api_url", body.proxy_api_url),
            ("bank_card_api_url", body.bank_card_api_url),
            ("bank_card_api_key", body.bank_card_api_key),
            ("bank_card_api_platform", (body.bank_card_api_platform or "寻汇").strip()),
            ("email_api_url", body.email_api_url),
            ("email_api_key", body.email_api_key),
            ("email_api_default_type", (body.email_api_default_type or "outlook").strip()),
            ("captcha_api_url", body.captcha_api_url),
            ("captcha_api_key", body.captcha_api_key),
            ("card_use_limit", body.card_use_limit),
            ("phone_bind_limit", body.phone_bind_limit),
            ("oauth_client_id", (body.oauth_client_id or "").strip()),
            ("oauth_redirect_uri", (body.oauth_redirect_uri or "").strip()),
        ]:
            c.execute(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                (key, value or "")
            )
        if body.admin_username and body.admin_password:
            hash_val = get_password_hash(body.admin_password)
            c.execute(
                "INSERT INTO admin_users (username, password_hash, updated_at) VALUES (?, ?, datetime('now')) ON CONFLICT(username) DO UPDATE SET password_hash=?, updated_at=datetime('now')",
                (body.admin_username, hash_val, hash_val)
            )
    return {"ok": True}


@router.put("/login")
def update_login(body: LoginUpdateBody, username: str = Depends(get_current_user)):
    """仅修改登录账号与密码，与系统设置分离"""
    if not body.admin_username or not body.admin_password:
        raise HTTPException(status_code=400, detail="账号与密码均不能为空")
    hash_val = get_password_hash(body.admin_password)
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO admin_users (username, password_hash, updated_at) VALUES (?, ?, datetime('now')) ON CONFLICT(username) DO UPDATE SET password_hash=?, updated_at=datetime('now')",
            (body.admin_username.strip(), hash_val, hash_val)
        )
    return {"ok": True}
