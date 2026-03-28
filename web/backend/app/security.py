import bcrypt
from passlib.context import CryptContext

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain: str, hashed: str) -> bool:
    hash_value = (hashed or "").strip()
    if not hash_value:
        return False
    try:
        return pwd_ctx.verify(plain, hash_value)
    except Exception:
        pass
    try:
        return bcrypt.checkpw((plain or "").encode("utf-8"), hash_value.encode("utf-8"))
    except Exception:
        return False


def get_password_hash(password: str) -> str:
    secret = (password or "").encode("utf-8")
    try:
        return pwd_ctx.hash(password)
    except Exception:
        return bcrypt.hashpw(secret, bcrypt.gensalt()).decode("utf-8")
