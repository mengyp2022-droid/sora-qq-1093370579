# -*- coding: utf-8 -*-
"""
协议版 ChatGPT 注册（严格按 protocol_keygen 一套）
入口：register_one_protocol(email, password, jwt_token, get_otp_fn, user_info, **kwargs)。
流程（keygen 单流程）：GET /oauth/authorize(screen_hint=signup) -> POST authorize/continue(sentinel) -> GET create-account/password -> POST user/register(sentinel) -> send_otp -> 邮局取验证码 -> validate_otp -> create_account -> callback -> 取 code 换 AT/RT 或 8.6 登录取 code 换 RT -> 返回 tokens 供 runner 写入账号列表。
邮箱/代理/OAuth Client ID 等均从配置（Web 系统设置）获取。
"""

import base64
import hashlib
import json
import os
import random
import re
import secrets
import time
import uuid
from urllib.parse import urlparse, parse_qs, urlencode
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import (
    cfg,
    HTTP_TIMEOUT,
    get_proxy_url_for_session,
)
from utils import get_user_agent

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    curl_requests = None
    CURL_CFFI_AVAILABLE = False

try:
    from protocol_sentinel import build_sentinel_token, build_sentinel_token_pow_only
except Exception:
    try:
        from protocol.protocol_sentinel import build_sentinel_token, build_sentinel_token_pow_only
    except Exception:
        build_sentinel_token = None
        build_sentinel_token_pow_only = None

CHATGPT_ORIGIN = "https://chatgpt.com"
AUTH_ORIGIN = "https://auth.openai.com"

# OAuth Code 换 Token（Codex / ChatGPT），运行时从 cfg.oauth 读（Web 下为系统设置）
OAUTH_ISSUER = AUTH_ORIGIN


def _format_error_status(prefix: str, payload) -> str:
    """把协议错误转成可供 runner 判定/打标的稳定字符串。"""
    code = ""
    message = ""
    if isinstance(payload, dict):
        err = payload.get("error") or {}
        if isinstance(err, dict):
            code = (err.get("code") or "").strip()
            message = (err.get("message") or "").strip()
    parts = [prefix]
    if code:
        parts.append(code)
    if message:
        parts.append(message)
    return ": ".join(parts)


def _get_oauth_client_id() -> str:
    return (getattr(getattr(cfg, "oauth", None), "client_id", None) or "").strip()


def _get_oauth_redirect_uri() -> str:
    return (getattr(getattr(cfg, "oauth", None), "redirect_uri", None) or "").strip() or f"{CHATGPT_ORIGIN}/"


def _has_cookie(session, name: str) -> bool:
    """兼容 requests 与 curl_cffi：判断 session 是否含有名为 name 的 cookie。"""
    try:
        if getattr(session.cookies, "get", None):
            if session.cookies.get(name):
                return True
        for c in getattr(session, "cookies", []):
            if getattr(c, "name", None) == name:
                return True
    except Exception:
        pass
    return False

# 密码规则：OpenAI 要求最少 12 位
PASSWORD_MIN_LENGTH = 12


class RetryException(Exception):
    """需换 IP/会话重试时抛出；主循环捕获后重新开始。"""
    pass


class RegistrationCancelled(Exception):
    """用户请求停止注册时抛出。"""
    pass


# 与 keygen 一致：使用 requests，TLS 指纹与 keygen 相同，便于过 CF
KEYGEN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


def _make_trace_headers():
    """与参考 chatgpt_register.py 一致：traceparent + datadog 头。"""
    trace_id = random.randint(10**17, 10**18 - 1)
    parent_id = random.randint(10**17, 10**18 - 1)
    tp = f"00-{uuid.uuid4().hex}-{format(parent_id, '016x')}-01"
    return {
        "traceparent": tp, "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum", "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(trace_id), "x-datadog-parent-id": str(parent_id),
    }


def _mask_proxy_for_log(proxy: str) -> str:
    """日志用：隐藏代理 URL 中的密码部分。"""
    if not proxy or "@" not in proxy:
        return proxy or "(无)"
    try:
        # protocol://user:pass@host -> protocol://user:****@host
        pre, at_part = proxy.rsplit("@", 1)
        if ":" in pre:
            scheme_rest = pre.split("//", 1)
            if len(scheme_rest) == 2 and ":" in scheme_rest[1]:
                user, _ = scheme_rest[1].split(":", 1)
                pre = f"{scheme_rest[0]}//{user}:****"
        return f"{pre}@{at_part}"
    except Exception:
        return proxy[:50] + "..." if len(proxy or "") > 50 else (proxy or "(无)")


def _make_session(device_id: str = None):
    """与 keygen 一致：使用 requests.Session()（TLS 指纹同 keygen，利于过 CF）。"""
    proxy = get_proxy_url_for_session()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    print(f"[*] 代理: {_mask_proxy_for_log(proxy)}", flush=True)
    print("[*] Using requests (keygen 同款)", flush=True)
    if device_id is None:
        device_id = str(uuid.uuid4())

    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if proxies:
        session.proxies = proxies
    session.headers.update({
        "User-Agent": KEYGEN_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        session.cookies.set("oai-did", device_id, domain=".auth.openai.com")
        session.cookies.set("oai-did", device_id, domain="auth.openai.com")
    except Exception:
        pass
    return session


# -------------------- 注册流程步骤（keygen 单流程） --------------------

def _ensure_password_page(session, state: str = None) -> None:
    """0b 后 GET create-account/password，建立密码页会话后再 POST user/register（keygen 无此步，当前服务端可能依赖）。"""
    url = f"{AUTH_ORIGIN}/create-account/password"
    if state:
        url = f"{url}?state={state}"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "referer": f"{AUTH_ORIGIN}/create-account",
    }
    session.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True, verify=False)


def _keygen_step0_oauth_and_continue(session, email: str, device_id: str, code_verifier: str, code_challenge: str, _step) -> str:
    """
    keygen 可注册方案：GET /oauth/authorize (screen_hint=signup) + POST authorize/continue 带 sentinel。
    代理从 config 的 get_proxy_url_for_session 已注入到 session。
    """
    client_id = _get_oauth_client_id()
    if not client_id:
        _step("[*] keygen 需配置 OAuth Client ID，跳过 Sentinel 流程")
        return ""
    redirect_uri = _get_oauth_redirect_uri()
    state = secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email offline_access",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "screen_hint": "signup",
        "prompt": "login",
    }
    authorize_url = f"{AUTH_ORIGIN}/oauth/authorize?{urlencode(params)}"
    _step("[*] keygen 0a GET /oauth/authorize (screen_hint=signup)")
    # 与 keygen NAVIGATE_HEADERS 完全一致（含 user-agent、sec-ch-ua，无 Referer，verify=False）
    nav_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    try:
        r = session.get(authorize_url, headers=nav_headers, timeout=HTTP_TIMEOUT, allow_redirects=True, verify=False)
    except Exception as e:
        print(f"[x] keygen 0a 失败: {e}", flush=True)
        return ""
    if not _has_cookie(session, "login_session"):
        _step("[*] keygen 0a 未获得 login_session")
        try:
            preview = (getattr(r, "text", None) or "")[:300]
            if preview:
                print(f"    响应预览: {preview}", flush=True)
                if "just a moment" in preview.lower() or "cloudflare" in preview.lower():
                    print("[x] 0a 被 Cloudflare 拦截，请换代理或稍后用下一账号重试", flush=True)
        except Exception:
            pass
        return ""
    if not build_sentinel_token:
        _step("[*] keygen 需 protocol_sentinel，跳过 Sentinel 流程")
        return ""
    sentinel_token = build_sentinel_token(session, device_id, flow="authorize_continue")
    if not sentinel_token:
        _step("[*] keygen 获取 sentinel token 失败")
        return ""
    _step("[*] keygen 0b POST authorize/continue + sentinel")
    # 与 keygen 一致：COMMON_HEADERS + referer + oai-device-id + datadog + openai-sentinel-token
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": AUTH_ORIGIN,
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "referer": f"{AUTH_ORIGIN}/create-account",
        "oai-device-id": device_id,
        "openai-sentinel-token": sentinel_token,
    }
    headers.update(_make_trace_headers())
    try:
        r = session.post(
            f"{AUTH_ORIGIN}/api/accounts/authorize/continue",
            json={"username": {"kind": "email", "value": email}, "screen_hint": "signup"},
            headers=headers,
            timeout=HTTP_TIMEOUT,
            verify=False,
        )
    except Exception as e:
        print(f"[x] keygen 0b 失败: {e}", flush=True)
        return ""
    if r.status_code != 200:
        _step(f"[*] keygen 0b 返回 {r.status_code}")
        return ""
    return state


def _register_with_sentinel(session, email: str, password: str, device_id: str, _step) -> tuple:
    """keygen 方案：POST user/register 带 openai-sentinel-token。先试完整 token(flow=authorize_continue)，否则 PoW 仅串。"""
    url = f"{AUTH_ORIGIN}/api/accounts/user/register"
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": AUTH_ORIGIN,
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "referer": f"{AUTH_ORIGIN}/create-account/password",
        "oai-device-id": device_id,
    }
    headers.update(_make_trace_headers())
    sentinel_val = None
    if build_sentinel_token:
        sentinel_val = build_sentinel_token(session, device_id, flow="authorize_continue")
    if not sentinel_val and build_sentinel_token_pow_only:
        sentinel_val = build_sentinel_token_pow_only(device_id)
    if sentinel_val:
        headers["openai-sentinel-token"] = sentinel_val
    r = session.post(url, json={"username": email, "password": password}, headers=headers, timeout=HTTP_TIMEOUT, verify=False)
    try:
        data = r.json()
    except Exception:
        data = {"text": (r.text or "")[:500]}
    if r.status_code == 409:
        err = data.get("error") or {}
        err_code = err.get("code") if isinstance(err, dict) else None
        if err_code == "invalid_state" or (isinstance(err, dict) and "invalid" in str(err).lower()):
            raise RetryException("Step register returned 409 invalid_state")
    if r.status_code == 400:
        err = data.get("error") or {}
        err_code = err.get("code") if isinstance(err, dict) else None
        if err_code == "invalid_auth_step":
            raise RetryException("Step register returned 400 invalid_auth_step")
    return r.status_code, data


def _send_otp(session):
    # keygen step3: NAVIGATE_HEADERS + referer, verify=False
    url = f"{AUTH_ORIGIN}/api/accounts/email-otp/send"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "referer": f"{AUTH_ORIGIN}/create-account/password",
    }
    r = session.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True, verify=False)
    try:
        data = r.json()
    except Exception:
        data = {"final_url": str(r.url), "status": r.status_code}
    return r.status_code, data


def _validate_otp(session, code: str):
    url = f"{AUTH_ORIGIN}/api/accounts/email-otp/validate"
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": AUTH_ORIGIN,
        "referer": f"{AUTH_ORIGIN}/email-verification",
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    headers.update(_make_trace_headers())
    r = session.post(url, json={"code": code}, headers=headers, timeout=HTTP_TIMEOUT, verify=False)
    try:
        data = r.json()
    except Exception:
        data = {"text": (r.text or "")[:500]}
    return r.status_code, data


def _create_account(session, name: str, birthdate: str):
    url = f"{AUTH_ORIGIN}/api/accounts/create_account"
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": AUTH_ORIGIN,
        "referer": f"{AUTH_ORIGIN}/about-you",
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    headers.update(_make_trace_headers())
    r = session.post(url, json={"name": name, "birthdate": birthdate}, headers=headers, timeout=HTTP_TIMEOUT, verify=False)
    try:
        data = r.json()
    except Exception:
        data = {"text": (r.text or "")[:500]}
    return r.status_code, data


def _callback(session, url: str):
    if not url or not url.startswith("http"):
        return None, None
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    }
    r_first = session.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=False)
    body_first = (r_first.text or "")[:50000]
    location = r_first.headers.get("Location") or r_first.headers.get("location") or ""
    if r_first.status_code in (301, 302, 303, 307, 308) and location:
        r = session.get(location, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
        body = (r.text or "")[:50000]
        final_url = str(r.url)
    else:
        r = r_first
        body = body_first
        final_url = str(r.url)
    if not body and body_first:
        body = body_first
    return r.status_code, {"final_url": final_url, "body": body, "first_location": location}


def _generate_code_verifier() -> str:
    """PKCE code_verifier，43~128 字符。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _generate_code_challenge(verifier: str) -> str:
    """PKCE S256 code_challenge。"""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _parse_code_from_url(final_url: str) -> str:
    """从 callback 最终 URL 的 query 或 fragment 中解析 OAuth code。"""
    if not final_url or not isinstance(final_url, str):
        return ""
    try:
        parsed = urlparse(final_url)
        for part in (parsed.query, parsed.fragment):
            if not part:
                continue
            params = parse_qs(part, keep_blank_values=False)
            for key in ("code",):
                vals = params.get(key)
                if vals and isinstance(vals[0], str) and vals[0].strip():
                    return vals[0].strip()
    except Exception:
        pass
    return ""


def _parse_code_from_body(body: str) -> str:
    """从 callback 响应体（HTML/JSON）中解析 OAuth code。"""
    if not body or not isinstance(body, str):
        return ""
    try:
        stripped = body.strip()
        if stripped.startswith("{"):
            data = json.loads(body)
            if isinstance(data, dict):
                c = data.get("code") or data.get("authorization_code")
                if isinstance(c, str) and len(c.strip()) > 5:
                    return c.strip()
        m = re.search(r"[\?&]code=([^&\s\"'<>]+)", body)
        if m and m.group(1) and len(m.group(1).strip()) > 5:
            return m.group(1).strip()
        m = re.search(r"[\"']code[\"']\s*:\s*[\"']([^\"']{10,})[\"']", body, re.I)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


def _parse_tokens_from_body(body: str) -> dict:
    """从 callback 响应体（HTML/JSON）中解析 refresh_token、access_token、id_token。"""
    out = {"refresh_token": "", "access_token": "", "id_token": ""}
    if not body or not isinstance(body, str):
        return out
    try:
        stripped = body.strip()
        if stripped.startswith("{"):
            data = json.loads(body)
            if isinstance(data, dict):
                for key in ("refresh_token", "refresh_token_secret"):
                    v = data.get(key)
                    if isinstance(v, str) and len(v.strip()) > 10:
                        out["refresh_token"] = v.strip()
                        break
                for key in ("access_token", "token"):
                    v = data.get(key)
                    if isinstance(v, str) and len(v.strip()) > 10:
                        out["access_token"] = v.strip()
                        break
                v = data.get("id_token")
                if isinstance(v, str) and len(v.strip()) > 10:
                    out["id_token"] = v.strip()
                for nest in ("session", "credentials", "auth"):
                    obj = data.get(nest)
                    if isinstance(obj, dict):
                        if not out["refresh_token"]:
                            v = obj.get("refresh_token") or obj.get("refresh_token_secret")
                            if isinstance(v, str) and len(v.strip()) > 10:
                                out["refresh_token"] = v.strip()
                        if not out["access_token"]:
                            v = obj.get("access_token") or obj.get("token")
                            if isinstance(v, str) and len(v.strip()) > 10:
                                out["access_token"] = v.strip()
                        if not out["id_token"]:
                            v = obj.get("id_token")
                            if isinstance(v, str) and len(v.strip()) > 10:
                                out["id_token"] = v.strip()
        for key_rt in ("refresh_token", "refresh_token_secret"):
            m = re.search(r"[\"']" + re.escape(key_rt) + r"[\"']\s*:\s*[\"']([^\"']{15,})[\"']", body, re.I)
            if m and not out["refresh_token"]:
                out["refresh_token"] = m.group(1).strip()
                break
        for key_at in ("access_token", "token"):
            m = re.search(r"[\"']" + re.escape(key_at) + r"[\"']\s*:\s*[\"']([^\"']{15,})[\"']", body, re.I)
            if m and not out["access_token"]:
                out["access_token"] = m.group(1).strip()
                break
        if not out["id_token"]:
            m = re.search(r"[\"']id_token[\"']\s*:\s*[\"']([^\"']{15,})[\"']", body, re.I)
            if m:
                out["id_token"] = m.group(1).strip()
        if not out["refresh_token"]:
            m = re.search(r'"refresh_token"\s*:\s*"([A-Za-z0-9_\-\.]{50,800})"', body)
            if m:
                out["refresh_token"] = m.group(1).strip()
        if not out["access_token"]:
            m = re.search(r'"access_token"\s*:\s*"([A-Za-z0-9_\-\.]{50,1200})"', body)
            if m:
                out["access_token"] = m.group(1).strip()
        if not out["id_token"]:
            m = re.search(r'"id_token"\s*:\s*"([A-Za-z0-9_\-\.]{50,1600})"', body)
            if m:
                out["id_token"] = m.group(1).strip()
        if not out["refresh_token"] and "refresh_token" in body:
            m = re.search(r"refresh_token[=:]\s*[\"']?([A-Za-z0-9_\-\.]{50,800})[\"']?", body, re.I)
            if m:
                out["refresh_token"] = m.group(1).strip()
    except Exception:
        pass
    return out


def codex_exchange_code(session, code: str, code_verifier: str, redirect_uri: str = None):
    """
    用 authorization code 换取 Codex/ChatGPT tokens。与 keygen 一致：重试 1 次、Content-Type form、verify=False。
    POST https://auth.openai.com/oauth/token
    redirect_uri 需与拿 code 时一致；不传则用系统设置或 chatgpt.com/。
    返回含 access_token、refresh_token 等的 dict，失败返回 None。
    """
    client_id = _get_oauth_client_id()
    if not client_id:
        return None
    uri = (redirect_uri or "").strip() or _get_oauth_redirect_uri()
    resp = None
    for attempt in range(2):
        try:
            resp = session.post(
                f"{OAUTH_ISSUER}/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": uri,
                    "client_id": client_id,
                    "code_verifier": code_verifier,
                },
                verify=False,
                timeout=60,
            )
            break
        except Exception as e:
            if attempt == 0:
                print("  Token 交换超时，重试...", flush=True)
                time.sleep(2)
                continue
            print(f"  Token 交换失败: {e}", flush=True)
            return None
    if resp and resp.status_code == 200:
        data = resp.json()
        print("  Codex Token 获取成功！", flush=True)
        print(f"    Access Token 长度: {len(data.get('access_token', ''))}", flush=True)
        print(f"    Refresh Token: {'有' if data.get('refresh_token') else '无'}", flush=True)
        print(f"    ID Token: {'有' if data.get('id_token') else '无'}", flush=True)
        return data
    if resp:
        print(f"  Token 交换失败: {resp.status_code}", flush=True)
        print(f"  响应: {(resp.text or '')[:300]}", flush=True)
    return None


def _decode_oai_session_cookie(session) -> dict:
    """从 oai-client-auth-session cookie 解码 JSON（尝试各 segment）。"""
    val = ""
    try:
        val = (session.cookies.get("oai-client-auth-session") or "") if hasattr(session, "cookies") else ""
    except Exception:
        pass
    if not val:
        for c in getattr(session, "cookies", []):
            if getattr(c, "name", None) == "oai-client-auth-session":
                val = getattr(c, "value", "") or ""
                break
    if not val:
        return {}
    for i, part in enumerate(val.split(".")[:3]):
        if not part:
            continue
        pad = 4 - len(part) % 4
        if pad != 4:
            part = part + ("=" * pad)
        try:
            raw = base64.urlsafe_b64decode(part)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            continue
    return {}


def _follow_consent_to_code(session, start_url: str, _step, max_depth: int = 15) -> str:
    """跟随 consent 重定向链，从 302 Location 或 ConnectionError（重定向到 localhost）中解析 code。与 keygen _follow_and_extract_code 一致。"""
    url = start_url
    if not url or not url.startswith("http"):
        url = f"{AUTH_ORIGIN}{start_url}" if start_url.startswith("/") else ""
    if not url:
        return ""
    nav_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    for _ in range(max_depth):
        try:
            r = session.get(url, headers=nav_headers, timeout=min(HTTP_TIMEOUT, 30), allow_redirects=False, verify=False)
        except requests.exceptions.ConnectionError as e:
            err_str = str(e)
            m = re.search(r"(https?://(?:localhost|127\.0\.0\.1)[^\s\'\"<>]*)", err_str)
            if m:
                return _parse_code_from_url(m.group(1))
            return ""
        except Exception as e:
            err_str = str(e)
            if "localhost" in err_str or "1455" in err_str or "127.0.0.1" in err_str:
                m = re.search(r"(https?://(?:localhost|127\.0\.0\.1)[^\s\'\"<>]*)", err_str)
                if m:
                    return _parse_code_from_url(m.group(1))
            return ""
        if r.status_code in (301, 302, 303, 307, 308):
            loc = (r.headers.get("Location") or r.headers.get("location") or "").strip()
            if not loc:
                return ""
            code = _parse_code_from_url(loc)
            if code:
                return code
            url = loc if loc.startswith("http") else f"{AUTH_ORIGIN}{loc}"
            continue
        if r.status_code == 200:
            code = _parse_code_from_url(r.url)
            if code:
                return code
            body = (r.text or "")[:200000]
            code = _parse_code_from_body(body)
            if code:
                return code
            next_url = ""
            for pat in (
                r'"continue_url"\s*:\s*"([^"]+)"',
                r'"redirect_url"\s*:\s*"([^"]+)"',
                r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
                r'location\.replace\(\s*["\']([^"\']+)["\']\s*\)',
            ):
                m = re.search(pat, body, re.I)
                if m and m.group(1):
                    next_url = m.group(1).replace("\\u0026", "&").replace("\\/", "/").strip()
                    break
            if not next_url:
                hrefs = re.findall(r'href=["\']([^"\']+)["\']', body, re.I)
                for h in hrefs:
                    h2 = (h or "").replace("\\u0026", "&").replace("\\/", "/").strip()
                    if not h2:
                        continue
                    if "code=" in h2:
                        next_url = h2
                        break
                    if "/oauth/" in h2 or "/auth/callback" in h2 or "localhost:1455" in h2:
                        next_url = h2
                        break
            if next_url:
                code = _parse_code_from_url(next_url)
                if code:
                    return code
                url = next_url if next_url.startswith("http") else f"{AUTH_ORIGIN}{next_url}"
                continue
            _step(f"[*] 8.6 consent 200 但未解析到 code/next_url: {str(r.url)[:120]}")
            try:
                snippet = re.sub(r"\\s+", " ", body)[:220]
                if snippet:
                    _step(f"[*] 8.6 consent body 片段: {snippet}")
            except Exception:
                pass
        return ""
    return ""


def _normalize_otp_code(raw) -> str:
    digits = re.sub(r"\D", "", str(raw or "").strip())
    return digits[:6] if len(digits) >= 6 else ""


def _request_login_email_otp(session, device_id: str, _step) -> bool:
    """
    登录 8.6 邮箱验证码兜底触发：先 POST 再 GET /api/accounts/email-otp/send。
    返回是否至少一次拿到 2xx。
    """
    ok = False
    url = f"{AUTH_ORIGIN}/api/accounts/email-otp/send"

    api_headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": AUTH_ORIGIN,
        "referer": f"{AUTH_ORIGIN}/email-verification",
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "oai-device-id": device_id,
    }
    api_headers.update(_make_trace_headers())
    try:
        r = session.post(url, json={}, headers=api_headers, timeout=min(HTTP_TIMEOUT, 30), verify=False)
        _step(f"[*] 8.6 触发登录验证码发送 POST {r.status_code}")
        if 200 <= r.status_code < 300:
            ok = True
    except Exception as e:
        _step(f"[*] 8.6 触发登录验证码发送 POST 异常: {e}")

    nav_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "referer": f"{AUTH_ORIGIN}/email-verification",
    }
    try:
        r = session.get(url, headers=nav_headers, timeout=min(HTTP_TIMEOUT, 30), allow_redirects=True, verify=False)
        _step(f"[*] 8.6 触发登录验证码发送 GET {r.status_code}")
        if 200 <= r.status_code < 300:
            ok = True
    except Exception as e:
        _step(f"[*] 8.6 触发登录验证码发送 GET 异常: {e}")
    return ok


def _poll_fresh_login_otp(get_otp_fn, _step, excluded_codes=None, attempts: int = 2) -> str:
    excluded = set()
    for x in (excluded_codes or []):
        x_norm = _normalize_otp_code(x)
        if x_norm:
            excluded.add(x_norm)
    rounds = max(1, attempts)
    for i in range(rounds):
        code = _normalize_otp_code(get_otp_fn() if get_otp_fn else "")
        if code and code not in excluded:
            return code
        if code and code in excluded:
            _step("[*] 8.6 收到旧验证码，继续等待新码...")
        else:
            _step("[*] 8.6 暂未取到新验证码")
        if code:
            excluded.add(code)
        if i < rounds - 1:
            time.sleep(2)
    return ""


def _oauth_login_get_tokens(email: str, password: str, get_otp_fn, _step, prev_used_codes=None) -> dict:
    """
    严格按 keygen perform_codex_oauth_login_http：注册成功后用新 session 走 OAuth 登录，
    GET authorize -> POST authorize/continue -> POST password/verify -> [email-otp] -> consent -> code 换 AT/RT。
    """
    client_id = _get_oauth_client_id()
    if not client_id:
        return {}
    _step("[*] 8.6 登录取 RT（keygen 同款：新 session GET authorize -> ... -> code 换 token）")
    device_id = str(uuid.uuid4())
    session = _make_session(device_id)
    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)
    state = secrets.token_urlsafe(32)
    redirect_uri = (_get_oauth_redirect_uri() or "").strip() or "http://localhost:1455/auth/callback"
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email offline_access",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    authorize_url = f"{AUTH_ORIGIN}/oauth/authorize?{urlencode(params)}"
    nav_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    try:
        r = session.get(authorize_url, headers=nav_headers, timeout=HTTP_TIMEOUT, allow_redirects=True, verify=False)
    except Exception as e:
        _step(f"[*] 8.6 authorize 请求失败: {e}")
        return {}
    if not _has_cookie(session, "login_session"):
        _step("[*] 8.6 未获得 login_session，可能需 sentinel")
    api_headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": AUTH_ORIGIN,
        "user-agent": KEYGEN_USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "referer": f"{AUTH_ORIGIN}/log-in",
        "oai-device-id": device_id,
    }
    api_headers.update(_make_trace_headers())
    if build_sentinel_token:
        sentinel_ac = build_sentinel_token(session, device_id, flow="authorize_continue")
        if sentinel_ac:
            api_headers["openai-sentinel-token"] = sentinel_ac
    try:
        r = session.post(
            f"{AUTH_ORIGIN}/api/accounts/authorize/continue",
            json={"username": {"kind": "email", "value": email}},
            headers=api_headers,
            timeout=HTTP_TIMEOUT,
            verify=False,
        )
    except Exception as e:
        _step(f"[*] 8.6 authorize/continue 失败: {e}")
        return {}
    if r.status_code != 200:
        _step(f"[*] 8.6 authorize/continue {r.status_code}（若 403 可能需 sentinel）")
        try:
            _step(f"[*] 8.6 响应: {(r.text or '')[:200]}")
        except Exception:
            pass
        return {}
    api_headers["referer"] = f"{AUTH_ORIGIN}/log-in/password"
    api_headers.update(_make_trace_headers())
    if build_sentinel_token:
        sentinel_pw = build_sentinel_token(session, device_id, flow="password_verify")
        if sentinel_pw:
            api_headers["openai-sentinel-token"] = sentinel_pw
    try:
        r = session.post(
            f"{AUTH_ORIGIN}/api/accounts/password/verify",
            json={"password": password},
            headers=api_headers,
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
            verify=False,
        )
    except Exception as e:
        _step(f"[*] 8.6 password/verify 失败: {e}")
        return {}
    if r.status_code != 200:
        _step(f"[*] 8.6 password/verify {r.status_code}（若 403 可能需 sentinel）")
        try:
            _step(f"[*] 8.6 响应: {(r.text or '')[:200]}")
        except Exception:
            pass
        return {}
    try:
        data = r.json()
        continue_url = (data.get("continue_url") or "").strip()
        page_type = (data.get("page") or {}).get("type", "")
    except Exception:
        continue_url = ""
        page_type = ""
    if not continue_url:
        _step("[*] 8.6 password/verify 200 但无 continue_url")
        return {}
    _step(f"[*] 8.6 continue_url: {continue_url[:80]}...")
    if page_type == "email_otp_verification" or "email-verification" in continue_url:
        excluded_codes = set(prev_used_codes or [])
        # password/verify 进入邮箱验证页后，服务端通常已自动发码。
        # 先等现有新码，拿不到再主动 resend，避免刚发出的旧码被我们自己立刻作废。
        code_otp = _poll_fresh_login_otp(get_otp_fn, _step, excluded_codes=excluded_codes, attempts=1)
        if not code_otp:
            _request_login_email_otp(session, device_id, _step)
            code_otp = _poll_fresh_login_otp(get_otp_fn, _step, excluded_codes=excluded_codes, attempts=2)
        if not code_otp:
            _step("[*] 8.6 需要邮箱验证码但未提供 get_otp_fn 或未取到")
            return {}
        api_headers["referer"] = f"{AUTH_ORIGIN}/email-verification"
        api_headers.update(_make_trace_headers())
        try:
            r = session.post(
                f"{AUTH_ORIGIN}/api/accounts/email-otp/validate",
                json={"code": code_otp},
                headers=api_headers,
                timeout=HTTP_TIMEOUT,
                verify=False,
            )
        except Exception:
            return {}
        if r.status_code != 200:
            _step(f"[*] 8.6 email-otp/validate {r.status_code}")
            # 401 常见于验证码过期/拿到旧码，尝试再拉一枚新码重试一次
            if r.status_code in (400, 401) and get_otp_fn:
                excluded_codes.add(code_otp)
                code_otp_2 = _poll_fresh_login_otp(get_otp_fn, _step, excluded_codes=excluded_codes, attempts=1)
                if not code_otp_2:
                    _request_login_email_otp(session, device_id, _step)
                    code_otp_2 = _poll_fresh_login_otp(get_otp_fn, _step, excluded_codes=excluded_codes, attempts=2)
                if code_otp_2:
                    api_headers.update(_make_trace_headers())
                    try:
                        r = session.post(
                            f"{AUTH_ORIGIN}/api/accounts/email-otp/validate",
                            json={"code": code_otp_2},
                            headers=api_headers,
                            timeout=HTTP_TIMEOUT,
                            verify=False,
                        )
                        _step(f"[*] 8.6 email-otp/validate 重试 {r.status_code}")
                    except Exception:
                        return {}
            if r.status_code != 200:
                return {}
        try:
            data = r.json()
            continue_url = (data.get("continue_url") or "").strip()
        except Exception:
            pass
    if continue_url and (
        "/about-you" in continue_url
        or page_type in ("about_you", "about-you")
    ):
        _step("[*] 8.6 检测到 about-you，补提交资料后继续取 code")
        yy = random.randint(1988, 2000)
        mm = random.randint(1, 12)
        dd = random.randint(1, 28)
        status_create, data_create = _create_account(session, "User", f"{yy}-{str(mm).zfill(2)}-{str(dd).zfill(2)}")
        if status_create not in (200, 201, 204):
            _step(f"[*] 8.6 about-you 提交失败: {status_create}")
            return {}
        try:
            next_url = (
                (data_create.get("continue_url") or "").strip()
                or (data_create.get("url") or "").strip()
                or (data_create.get("redirect_url") or "").strip()
            )
            if next_url:
                continue_url = next_url
                _step(f"[*] 8.6 about-you -> continue_url: {continue_url[:80]}...")
        except Exception:
            pass
    if not continue_url:
        return {}
    consent_url = continue_url if continue_url.startswith("http") else f"{AUTH_ORIGIN}{continue_url}"
    auth_code = _follow_consent_to_code(session, consent_url, _step)
    if not auth_code:
        _step("[*] 8.6 直接 GET consent 未拿到 code，尝试 workspace/select...")
        session_data = _decode_oai_session_cookie(session)
        workspaces = (session_data or {}).get("workspaces") or []
        workspace_id = workspaces[0].get("id") if workspaces else None
        if workspace_id:
            api_headers["referer"] = consent_url
            api_headers.update(_make_trace_headers())
            try:
                r = session.post(
                    f"{AUTH_ORIGIN}/api/accounts/workspace/select",
                    json={"workspace_id": workspace_id},
                    headers=api_headers,
                    timeout=HTTP_TIMEOUT,
                    allow_redirects=False,
                    verify=False,
                )
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = (r.headers.get("Location") or r.headers.get("location") or "").strip()
                    auth_code = _parse_code_from_url(loc)
                    if not auth_code and loc:
                        auth_code = _follow_consent_to_code(
                            session, loc if loc.startswith("http") else f"{AUTH_ORIGIN}{loc}", _step
                        )
                elif r.status_code == 200:
                    try:
                        ws_data = r.json()
                        ws_next = (ws_data.get("continue_url") or "").strip()
                        if ws_next:
                            auth_code = _follow_consent_to_code(
                                session,
                                ws_next if ws_next.startswith("http") else f"{AUTH_ORIGIN}{ws_next}",
                                _step,
                            )
                        if not auth_code:
                            orgs = (ws_data.get("data") or {}).get("orgs") or []
                            if orgs:
                                org_id = orgs[0].get("id")
                                proj = (orgs[0].get("projects") or [{}])[0].get("id") if orgs[0].get("projects") else None
                                body = {"org_id": org_id}
                                if proj:
                                    body["project_id"] = proj
                                api_headers["referer"] = consent_url
                                api_headers.update(_make_trace_headers())
                                r2 = session.post(
                                    f"{AUTH_ORIGIN}/api/accounts/organization/select",
                                    json=body,
                                    headers=api_headers,
                                    timeout=HTTP_TIMEOUT,
                                    allow_redirects=False,
                                    verify=False,
                                )
                                if r2.status_code in (301, 302, 303, 307, 308):
                                    loc2 = (r2.headers.get("Location") or r2.headers.get("location") or "").strip()
                                    auth_code = _parse_code_from_url(loc2) or _follow_consent_to_code(
                                        session, loc2 if loc2.startswith("http") else f"{AUTH_ORIGIN}{loc2}", _step
                                    )
                                elif r2.status_code == 200:
                                    try:
                                        next_url = (r2.json().get("continue_url") or "").strip()
                                        if next_url:
                                            auth_code = _follow_consent_to_code(
                                                session,
                                                next_url if next_url.startswith("http") else f"{AUTH_ORIGIN}{next_url}",
                                                _step,
                                            )
                                    except Exception:
                                        pass
                    except Exception as e:
                        _step(f"[*] 8.6 workspace 响应解析异常: {e}")
            except Exception as e:
                _step(f"[*] 8.6 workspace/select 请求异常: {e}")
        else:
            _step("[*] 8.6 无 workspace_id（cookie 无 workspaces）")
    if not auth_code:
        _step("[*] 8.6 [4d] 备用: GET consent allow_redirects=True 以从最终 URL 或 ConnectionError 取 code")
        nav_headers_4d = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": KEYGEN_USER_AGENT,
            "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        }
        try:
            r = session.get(consent_url, headers=nav_headers_4d, timeout=min(HTTP_TIMEOUT, 30), allow_redirects=True, verify=False)
            auth_code = _parse_code_from_url(r.url)
            if not auth_code and getattr(r, "history", None):
                for h in r.history:
                    loc = (h.headers.get("Location") or h.headers.get("location") or "").strip()
                    auth_code = _parse_code_from_url(loc)
                    if auth_code:
                        break
        except requests.exceptions.ConnectionError as e:
            m = re.search(r"(https?://(?:localhost|127\.0\.0\.1)[^\s\'\"<>]*)", str(e))
            if m:
                auth_code = _parse_code_from_url(m.group(1))
        except Exception:
            pass
    if not auth_code:
        _step("[*] 8.6 跟随 consent 未解析到 code")
        return {}
    _step("[*] 8.6 已从 consent 拿到 code，换取 token...")
    login_redirect_uri = (_get_oauth_redirect_uri() or "").strip() or "http://localhost:1455/auth/callback"
    exchange = codex_exchange_code(session, auth_code, code_verifier, redirect_uri=login_redirect_uri)
    if not exchange:
        _step(f"[*] 8.6 code 换 token 失败，请确认 OAuth redirect_uri 与系统设置一致: {login_redirect_uri[:50]}...")
        return {}
    if not exchange.get("refresh_token"):
        _step("[*] 8.6 换 token 成功但响应无 refresh_token")
    try:
        import protocol_sora_phone as sora_phone
    except Exception:
        try:
            import protocol.protocol_sora_phone as sora_phone
        except Exception:
            sora_phone = None
    if sora_phone:
        try:
            web_auth = sora_phone.sora_chatgpt_web_login_from_authenticated_session(
                session,
                email=email,
                password=password,
                get_otp_fn=get_otp_fn,
                log_fn=_step,
            )
        except Exception as e:
            _step(f"[*] 8.7 复用登录 session 建立 Sora Web session 异常: {e}")
            web_auth = {}
        web_at = (web_auth.get("access_token") or "").strip() if isinstance(web_auth, dict) else ""
        if web_at:
            api_access_token = (exchange.get("access_token") or "").strip()
            if api_access_token:
                exchange["api_access_token"] = api_access_token
            exchange["access_token"] = web_at
            if isinstance(web_auth.get("session"), dict):
                exchange["sora_session"] = dict(web_auth.get("session") or {})
            _step("[*] 8.7 已复用登录 session 建立 Sora Web session")
    return dict(exchange)


def decode_jwt_payload(token: str) -> dict:
    """解析 JWT token 的 payload 部分。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _parse_tokens_from_url(final_url: str) -> dict:
    """从 callback 最终 URL 的 query 或 fragment 中解析 refresh_token、access_token、id_token。"""
    out = {"refresh_token": "", "access_token": "", "id_token": ""}
    if not final_url or not isinstance(final_url, str):
        return out
    try:
        parsed = urlparse(final_url)
        for part in (parsed.query, parsed.fragment):
            if not part:
                continue
            params = parse_qs(part, keep_blank_values=False)
            for key_rt in ("refresh_token", "refresh_token_secret"):
                vals = params.get(key_rt) or params.get(key_rt.replace("_", "."))
                if vals and isinstance(vals[0], str) and len(vals[0].strip()) > 10:
                    out["refresh_token"] = vals[0].strip()
                    break
            for key_at in ("access_token", "token"):
                vals = params.get(key_at) or params.get(key_at.replace("_", "."))
                if vals and isinstance(vals[0], str) and len(vals[0].strip()) > 10:
                    out["access_token"] = vals[0].strip()
                    break
            vals = params.get("id_token") or params.get("id.token")
            if vals and isinstance(vals[0], str) and len(vals[0].strip()) > 10:
                out["id_token"] = vals[0].strip()
    except Exception:
        pass
    return out


def _parse_refresh_token_from_url(final_url: str) -> str:
    """从 callback 最终 URL 的 query 或 fragment 中解析 refresh_token（兼容旧逻辑）。"""
    return _parse_tokens_from_url(final_url).get("refresh_token", "") or ""


def _get_access_token_from_response(data: dict) -> str:
    """从 create_account 等接口的 JSON 响应中提取 access_token（含 page 等嵌套）。"""
    if not data or not isinstance(data, dict):
        return ""
    for key in ("access_token", "token"):
        v = data.get(key)
        if isinstance(v, str) and len(v.strip()) > 10:
            return v.strip()
    for nest in ("session", "credentials", "auth", "token", "page"):
        obj = data.get(nest)
        if isinstance(obj, dict):
            v = obj.get("access_token") or obj.get("token")
            if isinstance(v, str) and len(v.strip()) > 10:
                return v.strip()
    return ""


def _get_refresh_token_from_response(data: dict) -> str:
    """从 create_account 等接口的 JSON 响应中提取 refresh_token（含 page 等嵌套）。"""
    if not data or not isinstance(data, dict):
        return ""
    for key in ("refresh_token", "refresh_token_secret"):
        v = data.get(key)
        if isinstance(v, str) and len(v.strip()) > 10:
            return v.strip()
    for nest in ("session", "credentials", "auth", "token", "page"):
        obj = data.get(nest)
        if isinstance(obj, dict):
            v = obj.get("refresh_token") or obj.get("refresh_token_secret")
            if isinstance(v, str) and len(v.strip()) > 10:
                return v.strip()
    return ""


# -------------------- 入口 --------------------

def register_one_protocol(email: str, password: str, jwt_token: str, get_otp_fn, user_info: dict, **kwargs):
    """
    协议注册入口。
    入参：email, password, jwt_token, get_otp_fn(), user_info(name/year/month/day), step_log_fn, stop_check 等。
    返回：(email, password, success: bool[, status_extra[, tokens]])。
    """
    step_log_fn = kwargs.pop("step_log_fn", None)
    stop_check = kwargs.pop("stop_check", None)

    def _step(msg: str):
        if stop_check and callable(stop_check) and stop_check():
            raise RegistrationCancelled()
        if msg:
            print(msg, flush=True)
            if step_log_fn:
                try:
                    step_log_fn(msg.strip())
                except Exception:
                    pass

    _step(f"[*] register_one_protocol start {email}")
    pwd = (password or "").strip()
    if len(pwd) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password length must be >= {PASSWORD_MIN_LENGTH}, got {len(pwd)}. Set password in email row or use runner which auto-generates.")
    password = pwd
    name = user_info.get("name", "User")
    year = user_info.get("year", "1990")
    month = user_info.get("month", "01")
    day = user_info.get("day", "01")
    birthdate = f"{year}-{month}-{day}"

    device_id = str(uuid.uuid4())
    session = _make_session(device_id)
    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)
    if not _get_oauth_client_id():
        _step("[*] 未配置 OAuth Client ID，请在系统设置中填写")
        return email, password, False
    if not build_sentinel_token:
        _step("[*] Sentinel 未加载，请确保 protocol_sentinel 可用")
        return email, password, False
    try:
        _step("[*] 0. GET authorize + POST authorize/continue (sentinel)")
        try:
            session.cookies.set("oai-did", device_id, domain=".auth.openai.com")
            session.cookies.set("oai-did", device_id, domain="auth.openai.com")
        except Exception:
            pass
        time.sleep(random.uniform(0.2, 0.5))
        auth_state = _keygen_step0_oauth_and_continue(
            session, email, device_id, code_verifier, code_challenge, _step
        )
        if not auth_state:
            return email, password, False, "0a_no_session", None
        time.sleep(random.uniform(0.5, 1.0))
        _step("[*] 1. GET create-account/password")
        _ensure_password_page(session, auth_state)
        time.sleep(random.uniform(0.5, 1.0))
        _step("[*] 2. Register (user/register + sentinel)")
        status_reg, data_reg = _register_with_sentinel(session, email, password, device_id, _step)
        if status_reg not in (200, 201, 204):
            print(f"[x] 4. Register failed: status={status_reg} data={data_reg}", flush=True)
            if status_reg == 400 and isinstance(data_reg, dict):
                err = data_reg.get("error") or {}
                if err.get("code") == "bad_request" or "register username" in str(err.get("message", "")).lower():
                    print("[x] 若该邮箱已注册过，请换未注册邮箱重试", flush=True)
            return email, password, False, _format_error_status("register_failed", data_reg)
        print("[ok] 4. Register OK", flush=True)
        _step("[*] 3. Send OTP")
        status_otp, data_otp = _send_otp(session)
        if status_otp not in (200, 201, 204) and (not isinstance(data_otp, dict) or data_otp.get("error")):
            print(f"[x] 5. Send OTP failed: status={status_otp} data={data_otp}", flush=True)
            return email, password, False, _format_error_status("send_otp_failed", data_otp)

        _step("[*] Waiting for email OTP...")
        if stop_check and callable(stop_check) and stop_check():
            return email, password, False
        code = get_otp_fn()
        if not code or len(str(code).strip()) < 4:
            print("[x] No OTP received or invalid", flush=True)
            return email, password, False, "otp_missing"
        # 规范为纯 6 位数字，避免空格/换行导致 wrong_email_otp_code
        code = re.sub(r"\D", "", str(code).strip())
        if len(code) < 6:
            print("[x] OTP too short after normalizing", flush=True)
            return email, password, False, "otp_invalid"
        code = code[:6]

        _step("[*] 6. Validate OTP")
        status_val, data_val = _validate_otp(session, code)
        if status_val not in (200, 201, 204):
            err = (data_val.get("error") or {}) if isinstance(data_val, dict) else {}
            err_code = err.get("code") if isinstance(err, dict) else ""
            if status_val == 401 and err_code == "wrong_email_otp_code":
                print("[x] 验证码错误或过期；正在重试一次获取新验证码...", flush=True)
                time.sleep(3)
                code2 = get_otp_fn()
                if code2 and len(re.sub(r"\D", "", str(code2).strip())) >= 6:
                    code = re.sub(r"\D", "", str(code2).strip())[:6]
                    status_val, data_val = _validate_otp(session, code)
            if status_val not in (200, 201, 204):
                print(f"[x] 6. Validate OTP failed: status={status_val} data={data_val}", flush=True)
                if status_val == 401 and (err_code == "wrong_email_otp_code" or "wrong" in str(err).lower()):
                    print("[x] 请确认验证码来自本邮箱最新一封 OpenAI 邮件且未过期；多任务并发时易拿错邮箱", flush=True)
                return email, password, False, _format_error_status("validate_otp_failed", data_val)

        _step("[*] 7. Create account")
        status_create, data_create = _create_account(session, name, birthdate)
        if status_create not in (200, 201, 204):
            print(f"[x] 7. Create account failed: status={status_create} data={data_create}", flush=True)
            return email, password, False, _format_error_status("create_account_failed", data_create)

        callback_url = None
        if isinstance(data_create, dict):
            callback_url = data_create.get("continue_url") or data_create.get("url") or data_create.get("redirect_url")

        _step("[*] 8. Callback")
        if callback_url:
            _, _ = _callback(session, callback_url)

        print("[ok] Protocol registration success", flush=True)
        has_client_id = bool(_get_oauth_client_id())
        if not has_client_id:
            _step("[*] 未配置 OAuth Client ID，跳过登录取 RT；请在系统设置中填写")
            tokens = {}
        else:
            _step("[*] 8. 按 keygen 仅通过登录取 RT（新 session GET authorize -> ... -> code 换 token）")
            tokens = _oauth_login_get_tokens(
                email,
                password,
                get_otp_fn,
                _step,
                prev_used_codes={code},
            )
            if tokens.get("refresh_token") or tokens.get("access_token"):
                _step("[*] 8.6 登录取 code 已拿到 AT/RT")
            else:
                _step("[*] 8. 登录取 RT 未拿到 token（可能 403/sentinel 或 consent 未返回 code）")
        if tokens.get("refresh_token") or tokens.get("access_token"):
            _step(f"[*] 最终: RT={'有' if tokens.get('refresh_token') else '无'}, AT={'有' if tokens.get('access_token') else '无'}")
        return email, password, True, None, (tokens if tokens else None)
    except RegistrationCancelled:
        print("[*] 注册已停止", flush=True)
        return email, password, False
    except RetryException:
        raise
    except (requests.RequestException, ValueError) as e:
        print(f"[x] {e}", flush=True)
        return email, password, False
    except Exception as e:
        print(f"[x] Unexpected error: {e}", flush=True)
        return email, password, False
    finally:
        try:
            session.close()
        except Exception:
            pass


SORA_ORIGIN = "https://sora.chatgpt.com"
# 旧版 project_y 用户名接口仅保留给兼容日志；实际激活逻辑委托 protocol_sora_phone.sora_ensure_activated。
SORA_USERNAME_SET_URL = f"{SORA_ORIGIN}/backend/project_y/profile/username/set"


def _sora_username_from_email(email: str, max_len: int = 20) -> str:
    """从邮箱生成 Sora 用户名：取 @ 前部分，只保留字母数字下划线。"""
    if not email or "@" not in email:
        return "user" + str(random.randint(1000, 9999))
    local = email.split("@", 1)[0].strip().lower()
    safe = "".join(c for c in local if c.isalnum() or c == "_")
    if not safe:
        safe = "user"
    safe = (safe[:max_len]) if len(safe) > max_len else safe
    return safe if len(safe) >= 3 else f"user{random.randint(100, 999)}"


def activate_sora(tokens, email: str, **kwargs):
    """
    注册成功后激活 Sora。
    优先走 protocol_sora_phone 里的新版 onboarding/me 链路，失败时由其内部回退旧接口。
    tokens 可为空；若无 AT/RT，会回退到 ChatGPT Web 登录补 access_token。
    kwargs: proxy_url, username（可选覆盖）, step_log_fn。
    返回 True 表示设置成功，False 表示未调或失败。
    """
    if isinstance(tokens, dict):
        tokens = tokens
    else:
        tokens = {}
    at = (tokens.get("access_token") or "").strip()
    rt = (tokens.get("refresh_token") or "").strip()
    if not rt and isinstance(tokens.get("session"), dict):
        rt = ((tokens.get("session") or {}).get("refresh_token") or "").strip()
    proxy_url = (kwargs.get("proxy_url") or "").strip()
    step_log = kwargs.get("step_log_fn")
    try:
        import protocol_sora_phone as sora_phone
    except Exception:
        try:
            import protocol.protocol_sora_phone as sora_phone
        except Exception as exc:
            if callable(step_log):
                try:
                    step_log(f"[*] Sora helper 导入失败: {exc}")
                except Exception:
                    pass
            return False
    if rt:
        try:
            mobile = sora_phone.rt_to_at_mobile(rt, proxy_url=proxy_url or None, log_fn=step_log)
            mobile_at = (mobile.get("access_token") or "").strip()
            mobile_rt = (mobile.get("refresh_token") or "").strip()
            if mobile_at:
                at = mobile_at
                tokens["access_token"] = mobile_at
                if callable(step_log):
                    try:
                        step_log("[*] Sora 使用移动端 RT->AT 成功")
                    except Exception:
                        pass
            if mobile_rt:
                rt = mobile_rt
                tokens["refresh_token"] = mobile_rt
        except Exception as exc:
            if callable(step_log):
                try:
                    step_log(f"[*] Sora 移动端 RT->AT 异常: {exc}")
                except Exception:
                    pass
    username = (kwargs.get("username") or "").strip() or _sora_username_from_email(email or "")
    account_password = (kwargs.get("account_password") or "").strip()
    get_otp_fn = kwargs.get("get_otp_fn")
    if not at and not rt and not account_password:
        if callable(step_log):
            try:
                step_log("[*] Sora 缺少 AT/RT 且无账号密码，无法走 Web session 回退")
            except Exception:
                pass
        return False
    try:
        ok = False
        if at:
            ok = bool(
                sora_phone.sora_ensure_activated(
                    at,
                    proxy_url=proxy_url or None,
                    log_fn=step_log,
                    username=username,
                )
            )
            if ok:
                return True
        if account_password:
            web_auth = sora_phone.sora_chatgpt_web_login(
                email=email,
                password=account_password,
                get_otp_fn=get_otp_fn,
                proxy_url=proxy_url or None,
                log_fn=step_log,
            )
            web_at = (web_auth.get("access_token") or "").strip() if isinstance(web_auth, dict) else ""
            if web_at:
                at = web_at
                tokens["access_token"] = web_at
                if isinstance(web_auth.get("session"), dict):
                    tokens["sora_session"] = dict(web_auth.get("session") or {})
                if callable(step_log):
                    try:
                        step_log("[*] Sora 已切换到 ChatGPT Web session access_token")
                    except Exception:
                        pass
                return bool(
                    sora_phone.sora_ensure_activated(
                        at,
                        proxy_url=proxy_url or None,
                        log_fn=step_log,
                        username=username,
                    )
                )
        return False
    except Exception as e:
        if callable(step_log):
            try:
                step_log(f"[*] Sora 激活异常: {e}")
            except Exception:
                pass
        return False
