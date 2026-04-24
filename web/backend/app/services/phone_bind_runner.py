# -*- coding: utf-8 -*-
"""
开始绑定手机：只从账号管理取已具备 Sora 资格的账号
（Registered+Sora、has_sora=1、sora_enabled=1、phone_bound=0 且有 RT/AT），
从手机号管理取可用号码，执行 Sora 激活 + enroll/start -> 轮询验证码 -> enroll/finish，
更新 accounts.phone_bound、phone_numbers.used_count。
"""
import re
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from app.database import get_db, init_db
from app.services import hero_sms
from app.services.otp_resolver import build_otp_fetcher

# 绑定任务状态（与 register 类似，独立 stop 标志）
_phone_bind_running = False
_phone_bind_task_id = None
_phone_bind_heartbeat = None
_phone_bind_stop = False
_phone_bind_lock = threading.Lock()

PHONE_CODE_POLL_INTERVAL = 5
PHONE_CODE_MAX_RETRIES = 60
PHONE_BIND_PREFERRED_COUNTRY = 52
PHONE_BIND_PREFERRED_PHONE_PREFIXES = ("66",)


def is_phone_bind_stop_requested() -> bool:
    with _phone_bind_lock:
        return _phone_bind_stop


def set_phone_bind_stop(value: bool) -> None:
    with _phone_bind_lock:
        global _phone_bind_stop
        _phone_bind_stop = value


def set_phone_bind_task_started(task_id: str) -> bool:
    """返回 False 表示已在运行。"""
    with _phone_bind_lock:
        global _phone_bind_running, _phone_bind_task_id, _phone_bind_heartbeat, _phone_bind_stop
        if _phone_bind_running:
            return False
        _phone_bind_running = True
        _phone_bind_task_id = task_id
        _phone_bind_heartbeat = datetime.utcnow().isoformat() + "Z"
        _phone_bind_stop = False
        return True


def get_phone_bind_status() -> dict:
    with _phone_bind_lock:
        return {
            "running": _phone_bind_running,
            "task_id": _phone_bind_task_id,
            "heartbeat": _phone_bind_heartbeat,
        }


# 接码平台未返回到期时间时，默认有效期（分钟），与 sms_api 一致
_PHONE_DEFAULT_EXPIRE_MINUTES = 20


def _get_settings():
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT key, value FROM system_settings WHERE key IN ("
            "'sms_api_url', 'sms_api_key', 'proxy_url', 'sms_openai_service', 'sms_max_price', 'phone_bind_limit',"
            "'email_api_url', 'email_api_key')"
        )
        rows = c.fetchall()
    out = {}
    for k, v in rows:
        out[k] = (v or "").strip()
    out.setdefault("sms_api_url", "https://hero-sms.com/stubs/handler_api.php")
    return out


def _log(task_id: str, level: str, message: str):
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO run_logs (task_id, level, message, created_at) VALUES (?, ?, ?, ?)",
                (task_id, level, (message or "")[:500], datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
    except Exception:
        pass


def fetch_accounts_to_bind(limit: int = 50, exclude_ids=None):
    """账号管理：仅挑已具备 Sora 资格且未绑手机、仍可进入轮换池的账号。"""
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        where = [
            "phone_bound = 0",
            "COALESCE(status, '') = 'Registered+Sora'",
            "COALESCE(has_sora, 0) = 1",
            "COALESCE(sora_enabled, 1) = 1",
            """(
                   (refresh_token IS NOT NULL AND refresh_token != '')
                   OR (access_token IS NOT NULL AND access_token != '')
                 )""",
        ]
        params = []
        exclude_ids = [int(x) for x in (exclude_ids or []) if str(x).strip()]
        if exclude_ids:
            where.append("id NOT IN ({})".format(",".join(["?"] * len(exclude_ids))))
            params.extend(exclude_ids)
        params.append(limit)
        c.execute(
            f"""SELECT id, email, password, refresh_token, access_token, proxy FROM accounts
               WHERE {' AND '.join(where)}
               ORDER BY id ASC LIMIT ?""",
            tuple(params),
        )
        return c.fetchall()


def _load_email_mailbox(email: str):
    account_email = (email or "").strip()
    if not account_email:
        return None
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT email, password, uuid, token
               FROM emails
               WHERE LOWER(TRIM(email)) = LOWER(TRIM(?))
               LIMIT 1""",
            (account_email,),
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "email": row[0] or "",
        "password": row[1] or "",
        "uuid": row[2] or "",
        "token": row[3] or "",
    }


def _build_account_otp_fetcher(email: str):
    mailbox = _load_email_mailbox(email)
    if not mailbox:
        return None
    settings = _get_settings()
    base_url = (settings.get("email_api_url") or "https://gapi.hotmail007.com").rstrip("/")
    client_key = settings.get("email_api_key") or ""
    if not client_key:
        return None
    account_str = f"{mailbox['email']}:{mailbox['password']}:{mailbox['token']}:{mailbox['uuid']}"
    return build_otp_fetcher(base_url, client_key, account_str, timeout_sec=120, interval_sec=5, stop_check=is_phone_bind_stop_requested)


def fetch_phones_available(limit: int = 50):
    """手机号管理：仅挑当前已验证更稳定的泰国号。"""
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT id, phone, activation_id, max_use_count, used_count FROM phone_numbers
               WHERE activation_id IS NOT NULL AND used_count < max_use_count
                 AND (
                   COALESCE(remark, '') LIKE ?
                   OR REPLACE(REPLACE(COALESCE(phone, ''), '+', ''), ' ', '') LIKE ?
                 )
               ORDER BY id ASC LIMIT ?""",
            (f"%country={PHONE_BIND_PREFERRED_COUNTRY}%", f"{PHONE_BIND_PREFERRED_PHONE_PREFIXES[0]}%", limit),
        )
        return c.fetchall()


def _fetch_numbers_from_api(task_id: str, max_try: int = 3) -> int:
    """无可用手机号时从接码 API 拉取泰国号并写入 phone_numbers，返回成功写入条数。"""
    settings = _get_settings()
    base = settings.get("sms_api_url") or "https://hero-sms.com/stubs/handler_api.php"
    key = settings.get("sms_api_key") or ""
    if not key:
        _log(task_id, "warning", "未配置接码 API KEY，无法自动拉取手机号")
        return 0
    service = (settings.get("sms_openai_service") or "openai").strip() or "openai"
    try:
        max_price = float(settings.get("sms_max_price") or "0")
    except (TypeError, ValueError):
        max_price = 0
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM system_settings WHERE key = ?", ("phone_bind_limit",))
        row = c.fetchone()
        limit = int(row[0]) if row and row[0] else 1
    country = PHONE_BIND_PREFERRED_COUNTRY
    inserted = 0
    for _ in range(max_try):
        result = hero_sms.get_number_auto(base, key, service, country, max_price=max_price)
        if not result:
            break
        if result.get("error"):
            _log(task_id, "warning", f"自动拉取泰国手机号失败: {str(result['error'])[:200]}")
            break
        expired_at = result.get("expired_at")
        if not (expired_at and str(expired_at).strip()):
            default_end = (datetime.utcnow() + timedelta(minutes=_PHONE_DEFAULT_EXPIRE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
            expired_at = default_end
        else:
            raw = str(expired_at).strip()
            if "T" in raw:
                raw = raw.replace("Z", "").split(".")[0].replace("T", " ")
            expired_at = raw
        with get_db() as conn:
            c = conn.cursor()
            country_code = result.get("country")
            remark = "Hero-SMS(自动)"
            if country_code not in (None, "", 0):
                remark = f"Hero-SMS(自动 country={country_code})"
            c.execute(
                "INSERT INTO phone_numbers (phone, activation_id, max_use_count, remark, expired_at) VALUES (?, ?, ?, ?, ?)",
                (result["phone_number"], result["activation_id"], limit, remark, expired_at),
            )
            inserted += 1
            suffix = f" country={country_code}" if country_code not in (None, "", 0) else ""
            _log(task_id, "info", "自动拉取泰国手机号: " + str(result["phone_number"]) + suffix)
    return inserted


def _ensure_phone_inventory(task_id: str, needed: int) -> list:
    """确保本轮至少拿到 needed 条可用泰国号，拿不到则返回当前能拿到的全部。"""
    needed = max(0, int(needed or 0))
    phones = fetch_phones_available(limit=max(needed, 1))
    while len(phones) < needed:
        missing = needed - len(phones)
        _log(task_id, "info", f"无足够泰国手机号，尝试自动补拉 {missing} 条")
        inserted = _fetch_numbers_from_api(task_id, max_try=max(missing, 1))
        if inserted <= 0:
            break
        phones = fetch_phones_available(limit=max(needed, 1))
    return phones


def run_one_phone_bind(task_id: str, account_id: int, email: str, account_password: str, refresh_token: str, access_token: str, account_proxy: str,
                       phone_id: int, phone: str, activation_id: int,
                       sms_base: str, sms_key: str, proxy_url: str) -> bool:
    """
    单条绑定：拿 AT -> Sora 激活 -> enroll/start -> 轮询验证码 -> enroll/finish -> 更新 DB。
    返回 True 表示成功。
    """
    from app.registration_env import inject_registration_modules
    inject_registration_modules()

    import protocol_sora_phone as sora_phone

    def log(msg):
        _log(task_id, "info", msg)

    def close_enroll_ctx(ctx):
        if not isinstance(ctx, dict):
            return
        sess = ctx.get("web_session")
        if sess is None:
            return
        try:
            sess.close()
        except Exception:
            pass

    get_otp_fn = _build_account_otp_fetcher(email)
    if get_otp_fn:
        seed_current_otps = getattr(get_otp_fn, "seed_current_otps", None)
        if callable(seed_current_otps):
            try:
                seeded = seed_current_otps(folders=["junkemail", "inbox"])
            except Exception:
                seeded = set()
            if seeded:
                log(f"[绑定] {email} 预排除旧 OTP: {','.join(sorted(seeded))}")

    at = (access_token or "").strip()
    if not at and (refresh_token or "").strip():
        log(f"[绑定] {email} RT 换 AT...")
        out = sora_phone.rt_to_at_mobile(refresh_token.strip(), proxy_url=proxy_url or account_proxy, log_fn=log)
        at = (out.get("access_token") or "").strip()
        new_rt = (out.get("refresh_token") or "").strip()
        new_id_token = (out.get("id_token") or "").strip()
        if at or new_rt or new_id_token:
            try:
                with get_db() as conn:
                    c = conn.cursor()
                    if at:
                        c.execute("UPDATE accounts SET access_token = ? WHERE id = ?", (at, account_id))
                    if new_rt:
                        c.execute("UPDATE accounts SET refresh_token = ? WHERE id = ?", (new_rt, account_id))
                    if new_id_token:
                        c.execute("UPDATE accounts SET id_token = ? WHERE id = ?", (new_id_token, account_id))
            except Exception:
                pass
    if not at:
        log(f"[绑定] {email} 无 AT，跳过")
        return False

    if is_phone_bind_stop_requested():
        return False

    log(f"[绑定] {email} Sora 激活...")
    if not sora_phone.sora_ensure_activated(at, proxy_url=proxy_url or account_proxy, log_fn=log):
        log(f"[绑定] {email} Sora 激活失败")
        return False

    if is_phone_bind_stop_requested():
        return False

    log(f"[绑定] {email} 发送验证码 -> {phone}")
    ok, err, enroll_ctx = sora_phone.sora_phone_enroll_start(
        at,
        phone,
        proxy_url=proxy_url or account_proxy,
        log_fn=log,
        login_email=email,
        login_password=(account_password or "").strip(),
        get_otp_fn=get_otp_fn,
    )
    if not ok:
        if err == "phone_used":
            log(f"[绑定] 手机号已被使用: {phone}")
        elif err == "reauth_failed":
            log(f"[绑定] {email} recent reauth 失败")
        elif err == "sms_unavailable":
            log(f"[绑定] {email} 当前未开放 SMS MFA")
        elif err == "invalid_request":
            log(f"[绑定] {email} 手机号参数被上游拒绝: {phone}")
        return False

    code = None
    for i in range(PHONE_CODE_MAX_RETRIES):
        if is_phone_bind_stop_requested():
            close_enroll_ctx(enroll_ctx)
            return False
        out = hero_sms.get_status_v2(sms_base, sms_key, activation_id)
        if out and out.get("code"):
            raw = out.get("code")
            m = re.search(r"\d{6}", str(raw))
            if m:
                code = m.group()
                break
        import time
        time.sleep(PHONE_CODE_POLL_INTERVAL)

    if not code:
        log(f"[绑定] {email} 获取验证码超时")
        close_enroll_ctx(enroll_ctx)
        return False

    log(f"[绑定] {email} 提交验证码...")
    if not sora_phone.sora_phone_enroll_finish(
        at,
        phone,
        code,
        proxy_url=proxy_url or account_proxy,
        log_fn=log,
        context=enroll_ctx,
    ):
        log(f"[绑定] {email} 验证码提交失败")
        close_enroll_ctx(enroll_ctx)
        return False

    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE accounts SET phone_bound = 1 WHERE id = ?", (account_id,))
        c.execute("UPDATE phone_numbers SET used_count = used_count + 1 WHERE id = ?", (phone_id,))
    close_enroll_ctx(enroll_ctx)
    log(f"[绑定] 成功 {email} -> {phone}")
    return True


def run_phone_bind_loop(task_id: str, max_count: int = None):
    """后台循环：取待绑定账号与可用手机号，按 max_count 并发执行直到达到目标成功数或停止。"""
    global _phone_bind_running, _phone_bind_heartbeat, _phone_bind_stop
    settings = _get_settings()
    sms_base = settings.get("sms_api_url") or "https://hero-sms.com/stubs/handler_api.php"
    sms_key = settings.get("sms_api_key") or ""
    proxy_url = settings.get("proxy_url") or ""
    if not sms_key:
        _log(task_id, "error", "请先在系统设置中配置手机号接码 API KEY")
        with _phone_bind_lock:
            _phone_bind_running = False
        return

    processed = 0
    success_count = 0
    skipped_account_ids = set()
    try:
        while True:
            if is_phone_bind_stop_requested():
                _log(task_id, "info", "已请求停止绑定")
                break
            if max_count is not None and success_count >= max_count:
                _log(task_id, "info", f"已达到目标绑定数量 {max_count}")
                break

            remaining_target = max_count - success_count if max_count is not None else 1
            batch_size = max(1, int(remaining_target))

            accounts = fetch_accounts_to_bind(limit=max(batch_size * 2, batch_size), exclude_ids=skipped_account_ids)
            if not accounts:
                if skipped_account_ids:
                    _log(task_id, "info", "无更多可尝试账号（本轮失败账号已跳过）")
                else:
                    _log(task_id, "info", "无待绑定账号（需满足 Registered+Sora、has_sora=1、sora_enabled=1、phone_bound=0 且有 RT/AT）")
                break

            batch_size = min(batch_size, len(accounts))
            phones = _ensure_phone_inventory(task_id, batch_size)
            if not phones:
                _log(task_id, "info", "无可用泰国手机号（已尝试自动拉取仍无）")
                break

            batch_size = min(batch_size, len(phones))
            accounts = accounts[:batch_size]
            phones = phones[:batch_size]

            with _phone_bind_lock:
                _phone_bind_heartbeat = datetime.utcnow().isoformat() + "Z"

            if batch_size > 1:
                _log(task_id, "info", f"开始并发绑定，本轮并发 {batch_size} 条，目标剩余 {remaining_target}")

            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                future_map = {}
                for acc, ph in zip(accounts, phones):
                    account_id, email, account_password, rt, at, account_proxy = acc[0], acc[1], acc[2] or "", acc[3], acc[4], acc[5] or ""
                    phone_id, phone, act_id = ph[0], ph[1], ph[2]
                    future = executor.submit(
                        run_one_phone_bind,
                        task_id,
                        account_id, email, account_password, rt or "", at or "", account_proxy,
                        phone_id, phone, act_id,
                        sms_base, sms_key, proxy_url,
                    )
                    future_map[future] = (account_id, email, phone)

                for future in as_completed(future_map):
                    account_id, email, phone = future_map[future]
                    processed += 1
                    try:
                        ok = bool(future.result())
                    except Exception as exc:
                        ok = False
                        _log(task_id, "error", f"单条绑定线程异常 {email} -> {phone}: {exc}")
                    if ok:
                        success_count += 1
                    else:
                        skipped_account_ids.add(account_id)
                        _log(task_id, "warning", f"单条绑定失败，跳过账号继续下一条: {email} -> {phone}")
    finally:
        with _phone_bind_lock:
            _phone_bind_running = False
        _log(task_id, "info", f"绑定任务结束 处理 {processed} 条 成功 {success_count} 条")
