# -*- coding: utf-8 -*-
"""
Sora 激活与手机号绑定 HTTP 逻辑。
优先对齐当前官方前端 bundle 暴露的 onboarding/me 接口，旧 project_y 接口仅作回退。
全部使用 curl_cffi 移动端指纹请求 sora.chatgpt.com / auth.openai.com。
供「开始绑定手机」任务调用，参数均显式传入（不依赖 config）。
"""
import re
import random
import string
import uuid
import time
import os
from urllib.parse import parse_qs, urlencode, urlparse

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi import CurlMime
    CURL_CFFI_AVAILABLE = True
except ImportError:
    curl_requests = None
    CurlMime = None
    CURL_CFFI_AVAILABLE = False

import requests

try:
    from protocol_sentinel import build_sentinel_token
except Exception:
    try:
        from protocol.protocol_sentinel import build_sentinel_token
    except Exception:
        build_sentinel_token = None

SORA_ORIGIN = "https://sora.chatgpt.com"
SORA_LEGACY_ORIGIN = "https://sora.com"
CHATGPT_ORIGIN = "https://chatgpt.com"
AUTH_ORIGIN = "https://auth.openai.com"
CHATGPT_BACKEND_API_ORIGIN = f"{CHATGPT_ORIGIN}/backend-api"
CHATGPT_SECURITY_SETTINGS_URL = f"{CHATGPT_ORIGIN}/security-settings"
CHATGPT_MFA_RECENT_AUTH_MAX_AGE_SEC = 240
CHATGPT_WEB_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"
# 移动端 client_id / redirect_uri（与 sora-phone-bind 一致，用于 RT 换 AT）
MOBILE_CLIENT_ID = "app_LlGpXReQgckcGGUo2JrYvtJK"
MOBILE_REDIRECT_URI = "com.openai.chat://auth0.openai.com/ios/com.openai.chat/callback"

MOBILE_FINGERPRINTS = ["safari17_2_ios", "safari18_0_ios"]
MOBILE_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
]
WEB_FINGERPRINT = "chrome131"
WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_BROWSER_CDP_URLS = (
    "http://127.0.0.1:9222",
    "http://127.0.0.1:9224",
)

SORA_HEADERS_BASE = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Origin": SORA_ORIGIN,
    "Pragma": "no-cache",
    "Referer": f"{SORA_ORIGIN}/",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "Sec-Ch-Ua-Mobile": "?1",
    "Sec-Ch-Ua-Platform": '"iOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Content-Type": "application/json",
}

DEFAULT_TIMEOUT = 30
USERNAME_RETRY_CODES = {
    "username_taken",
    "username_rejected",
    "username_invalid",
    "username_required",
    "reserved_username",
}


def _log(log_fn, message: str) -> None:
    if callable(log_fn):
        try:
            log_fn(message)
        except Exception:
            pass


def _make_plain_session(proxy_url: str = None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def _make_web_session(proxy_url: str = None):
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    if CURL_CFFI_AVAILABLE and curl_requests:
        session = curl_requests.Session(impersonate=WEB_FINGERPRINT)
        if proxies:
            session.proxies = proxies
        return session
    return _make_plain_session(proxy_url=proxy_url)


def _candidate_origins() -> tuple[str, ...]:
    if SORA_LEGACY_ORIGIN == SORA_ORIGIN:
        return (SORA_ORIGIN,)
    return (SORA_ORIGIN, SORA_LEGACY_ORIGIN)


def _candidate_sora_web_origins(preferred_origin: str = "") -> tuple[str, ...]:
    seen = []
    for value in ((preferred_origin or "").strip(), SORA_ORIGIN, SORA_LEGACY_ORIGIN):
        origin = (value or "").rstrip("/")
        if origin and origin not in seen:
            seen.append(origin)
    return tuple(seen)


def _candidate_browser_cdp_urls(cdp_urls=None) -> tuple[str, ...]:
    raw_values = []
    if isinstance(cdp_urls, str):
        raw_values.extend(cdp_urls.split(","))
    elif cdp_urls:
        try:
            raw_values.extend(list(cdp_urls))
        except Exception:
            pass
    env_value = (
        os.getenv("SORA_BROWSER_CDP_URLS")
        or os.getenv("SORA_BROWSER_CDP_URL")
        or ""
    ).strip()
    if env_value:
        raw_values.extend(env_value.split(","))
    raw_values.extend(DEFAULT_BROWSER_CDP_URLS)
    seen = []
    for value in raw_values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def _response_preview(resp, limit: int = 240) -> str:
    try:
        text = (resp.text or "").strip().replace("\n", " ")
    except Exception:
        text = ""
    return text[:limit]


def _strip_nullish(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            cleaned = _strip_nullish(item)
            if cleaned is None:
                continue
            out[key] = cleaned
        return out
    if isinstance(value, list):
        return [_strip_nullish(item) for item in value]
    return value


def _decode_jwt_payload(token: str) -> dict:
    value = (token or "").strip()
    if not value:
        return {}
    parts = value.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1]
        padding = (-len(payload)) % 4
        if padding:
            payload += "=" * padding
        import base64
        import json
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def _extract_error(resp) -> tuple[str, str, str]:
    code = ""
    message = ""
    try:
        data = resp.json()
    except Exception:
        return code, message, _response_preview(resp)
    if isinstance(data, dict):
        err = data.get("error") or {}
        if isinstance(err, dict):
            code = (err.get("code") or "").strip()
            message = (err.get("message") or "").strip()
    return code, message, _response_preview(resp)


def _build_sentinel_header(device_id: str, flow: str, proxy_url: str = None, log_fn=None) -> str:
    if not build_sentinel_token:
        return ""
    try:
        session = _make_plain_session(proxy_url=proxy_url)
        return build_sentinel_token(session, device_id, flow=flow) or ""
    except Exception as exc:
        _log(log_fn, f"[sora] sentinel {flow} 异常: {exc}")
        return ""


def _session_get(url: str, headers: dict, proxy_url: str = None, timeout: int = DEFAULT_TIMEOUT):
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    if CURL_CFFI_AVAILABLE and curl_requests:
        return curl_requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            impersonate=random.choice(MOBILE_FINGERPRINTS),
        )
    session = _make_plain_session(proxy_url=proxy_url)
    return session.get(url, headers=headers, timeout=timeout, verify=False)


def _session_post(
    url: str,
    headers: dict,
    json: dict = None,
    data: dict = None,
    proxy_url: str = None,
    timeout: int = DEFAULT_TIMEOUT,
    allow_redirects: bool = True,
):
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    if CURL_CFFI_AVAILABLE and curl_requests:
        return curl_requests.post(
            url,
            headers=headers,
            json=json,
            data=data,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=allow_redirects,
            impersonate=random.choice(MOBILE_FINGERPRINTS),
        )
    session = _make_plain_session(proxy_url=proxy_url)
    kwargs = {"headers": headers, "timeout": timeout, "verify": False, "allow_redirects": allow_redirects}
    if data is not None:
        kwargs["data"] = data
    else:
        kwargs["json"] = json or {}
    return session.post(url, **kwargs)


def _session_multipart_post(
    url: str,
    headers: dict,
    *,
    data: dict = None,
    file_field_name: str,
    filename: str,
    file_bytes: bytes = None,
    file_path: str = None,
    content_type: str = None,
    proxy_url: str = None,
    timeout: int = DEFAULT_TIMEOUT,
    allow_redirects: bool = True,
):
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    if CURL_CFFI_AVAILABLE and curl_requests and CurlMime is not None:
        multipart_parts = [
            {
                "name": file_field_name,
                "filename": filename,
                "content_type": content_type,
                **({"local_path": file_path} if file_path else {"data": file_bytes or b""}),
            }
        ]
        return curl_requests.post(
            url,
            headers=headers,
            data=data or {},
            multipart=CurlMime.from_list(multipart_parts),
            proxies=proxies,
            timeout=timeout,
            allow_redirects=allow_redirects,
            impersonate=random.choice(MOBILE_FINGERPRINTS),
        )
    session = _make_plain_session(proxy_url=proxy_url)
    file_tuple = (filename, file_bytes if file_bytes is not None else open(file_path, "rb"), content_type)
    try:
        return session.post(
            url,
            headers=headers,
            data=data or {},
            files={file_field_name: file_tuple},
            timeout=timeout,
            verify=False,
            allow_redirects=allow_redirects,
        )
    finally:
        if file_bytes is None and hasattr(file_tuple[1], "close"):
            try:
                file_tuple[1].close()
            except Exception:
                pass


def _web_session_get(
    url: str,
    headers: dict,
    proxy_url: str = None,
    timeout: int = DEFAULT_TIMEOUT,
    allow_redirects: bool = True,
    web_session=None,
):
    if web_session is not None:
        return web_session.get(
            url,
            headers=headers,
            timeout=timeout,
            verify=False,
            allow_redirects=allow_redirects,
        )
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    if CURL_CFFI_AVAILABLE and curl_requests:
        return curl_requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=allow_redirects,
            impersonate=WEB_FINGERPRINT,
        )
    session = _make_plain_session(proxy_url=proxy_url)
    return session.get(url, headers=headers, timeout=timeout, verify=False, allow_redirects=allow_redirects)


def _web_session_post(
    url: str,
    headers: dict,
    data: dict = None,
    proxy_url: str = None,
    timeout: int = DEFAULT_TIMEOUT,
    allow_redirects: bool = True,
    web_session=None,
):
    if web_session is not None:
        return web_session.post(
            url,
            headers=headers,
            data=data or {},
            timeout=timeout,
            verify=False,
            allow_redirects=allow_redirects,
        )
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    if CURL_CFFI_AVAILABLE and curl_requests:
        return curl_requests.post(
            url,
            headers=headers,
            data=data,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=allow_redirects,
            impersonate=WEB_FINGERPRINT,
        )
    session = _make_plain_session(proxy_url=proxy_url)
    return session.post(
        url,
        headers=headers,
        data=data or {},
        timeout=timeout,
        verify=False,
        allow_redirects=allow_redirects,
    )


def _build_headers(access_token: str, device_id: str = None, origin: str = None) -> dict:
    base_origin = (origin or SORA_ORIGIN).rstrip("/")
    h = dict(SORA_HEADERS_BASE)
    h["Origin"] = base_origin
    h["Referer"] = f"{base_origin}/"
    h["Authorization"] = f"Bearer {access_token}"
    h["User-Agent"] = random.choice(MOBILE_USER_AGENTS)
    h["oai-device-id"] = device_id or str(uuid.uuid4())
    return h


def _build_sora_web_headers(
    access_token: str,
    device_id: str = None,
    referer: str = "",
    origin: str = "",
) -> dict:
    base_origin = (origin or SORA_ORIGIN).rstrip("/")
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Origin": base_origin,
        "Referer": referer or f"{base_origin}/explore",
        "User-Agent": WEB_USER_AGENT,
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Not_A Brand";v="24", "Chromium";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "oai-device-id": device_id or str(uuid.uuid4()),
    }


def _build_web_headers() -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": WEB_USER_AGENT,
    }


def _web_session_json_post(
    url: str,
    headers: dict,
    json: dict = None,
    proxy_url: str = None,
    timeout: int = DEFAULT_TIMEOUT,
    allow_redirects: bool = True,
    web_session=None,
):
    if web_session is not None:
        return web_session.post(
            url,
            headers=headers,
            json=json or {},
            timeout=timeout,
            verify=False,
            allow_redirects=allow_redirects,
        )
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    if CURL_CFFI_AVAILABLE and curl_requests:
        return curl_requests.post(
            url,
            headers=headers,
            json=json or {},
            proxies=proxies,
            timeout=timeout,
            allow_redirects=allow_redirects,
            impersonate=WEB_FINGERPRINT,
        )
    session = _make_plain_session(proxy_url=proxy_url)
    return session.post(
        url,
        headers=headers,
        json=json or {},
        timeout=timeout,
        verify=False,
        allow_redirects=allow_redirects,
    )


def _build_html_headers(referer: str = "") -> dict:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": WEB_USER_AGENT,
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _build_chatgpt_backend_headers(access_token: str, device_id: str = None, referer: str = "") -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Origin": CHATGPT_ORIGIN,
        "Referer": referer or f"{CHATGPT_SECURITY_SETTINGS_URL}?action=enable&factor=sms",
        "User-Agent": WEB_USER_AGENT,
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Not_A Brand";v="24", "Chromium";v="131"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "oai-device-id": device_id or str(uuid.uuid4()),
    }


def _load_register_helpers():
    try:
        import protocol_register as pr
        return pr
    except Exception:
        try:
            import protocol.protocol_register as pr
            return pr
        except Exception:
            return None


def _collect_response_urls(resp) -> list[str]:
    urls = []
    seen = set()

    def _push(value: str):
        candidate = (value or "").strip()
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        urls.append(candidate)

    try:
        _push(str(getattr(resp, "url", "") or ""))
    except Exception:
        pass
    try:
        for item in getattr(resp, "history", []) or []:
            _push(str(getattr(item, "url", "") or ""))
            try:
                _push((item.headers.get("Location") or item.headers.get("location") or "").strip())
            except Exception:
                pass
    except Exception:
        pass
    try:
        _push((resp.headers.get("Location") or resp.headers.get("location") or "").strip())
    except Exception:
        pass
    return urls


def _copy_session_cookies(src_session, dst_session) -> None:
    if src_session is None or dst_session is None:
        return
    try:
        cookies = list(getattr(src_session, "cookies", None) or [])
    except Exception:
        cookies = []
    for cookie in cookies:
        try:
            dst_session.cookies.set(
                cookie.name,
                cookie.value,
                domain=getattr(cookie, "domain", None),
                path=getattr(cookie, "path", "/") or "/",
            )
            continue
        except Exception:
            pass
        try:
            dst_session.cookies.set(cookie.name, cookie.value)
        except Exception:
            pass


def _copy_browser_cookie_dicts(cookies: list[dict], dst_session) -> int:
    if dst_session is None:
        return 0
    copied = 0
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        if not name:
            continue
        value = cookie.get("value") or ""
        kwargs = {
            "domain": cookie.get("domain") or None,
            "path": cookie.get("path") or "/",
        }
        secure = cookie.get("secure")
        if secure is not None:
            kwargs["secure"] = bool(secure)
        expires = cookie.get("expires")
        try:
            expires_value = float(expires)
        except Exception:
            expires_value = None
        if expires_value and expires_value > 0:
            kwargs["expires"] = int(expires_value)
        try:
            dst_session.cookies.set(name, value, **kwargs)
            copied += 1
            continue
        except Exception:
            pass
        try:
            dst_session.cookies.set(name, value)
            copied += 1
        except Exception:
            pass
    return copied


def _extract_api_error(resp) -> tuple[str, str, str]:
    code, message, preview = _extract_error(resp)
    if code or message:
        return code, message, preview
    try:
        data = resp.json()
    except Exception:
        return code, message, preview
    if not isinstance(data, dict):
        return code, message, preview
    detail = data.get("detail")
    if isinstance(detail, dict):
        code = (detail.get("code") or detail.get("type") or code or "").strip()
        message = (detail.get("message") or detail.get("detail") or message or "").strip()
    elif isinstance(detail, str) and detail.strip():
        raw_detail = detail.strip()
        try:
            import json
            nested = json.loads(raw_detail)
        except Exception:
            nested = None
        if isinstance(nested, dict):
            nested_err = nested.get("error") or {}
            if isinstance(nested_err, dict):
                code = (nested_err.get("code") or nested_err.get("type") or code or "").strip()
                message = (nested_err.get("message") or message or "").strip()
            else:
                message = raw_detail[:200]
        else:
            message = raw_detail[:200]
    else:
        code = (data.get("code") or data.get("type") or code or "").strip()
        message = (data.get("message") or message or "").strip()
    return code, message, preview


def _normalize_phone_number(phone_number: str) -> str:
    raw = (phone_number or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits:
        return ""
    return f"+{digits}"


def _chatgpt_pwd_auth_age_seconds(access_token: str):
    payload = _decode_jwt_payload(access_token)
    raw = payload.get("pwd_auth_time")
    try:
        value = float(raw)
    except Exception:
        return None
    if value <= 0:
        return None
    if value > 10_000_000_000:
        value = value / 1000.0
    return max(0, int(time.time() - value))


def _chatgpt_needs_recent_auth(access_token: str) -> bool:
    age = _chatgpt_pwd_auth_age_seconds(access_token)
    return age is None or age > CHATGPT_MFA_RECENT_AUTH_MAX_AGE_SEC


def _warm_chatgpt_security_page(web_session, log_fn=None) -> str:
    page_url = f"{CHATGPT_SECURITY_SETTINGS_URL}?action=enable&factor=sms"
    try:
        resp = web_session.get(
            page_url,
            headers=_build_html_headers(referer=CHATGPT_ORIGIN),
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        return str(getattr(resp, "url", "") or page_url)
    except Exception as exc:
        _log(log_fn, f"[phone_bind] security-settings 预热异常: {exc}")
        return page_url


def _read_sora_web_session(web_session, log_fn=None, preferred_origin: str = "") -> dict:
    for origin in _candidate_sora_web_origins(preferred_origin):
        sora_session_resp = web_session.get(
            f"{origin}/api/auth/session",
            headers={**_build_web_headers(), "Referer": f"{origin}/"},
            timeout=DEFAULT_TIMEOUT,
        )
        if sora_session_resp.status_code != 200:
            _log(log_fn, f"[sora] sora /api/auth/session HTTP {sora_session_resp.status_code} {_response_preview(sora_session_resp, 140)}")
            continue
        try:
            sora_session = sora_session_resp.json()
        except Exception:
            _log(log_fn, f"[sora] sora /api/auth/session 非 JSON: {_response_preview(sora_session_resp, 140)}")
            continue
        if not isinstance(sora_session, dict) or not sora_session or sora_session == {"WARNING_BANNER": sora_session.get("WARNING_BANNER")}:
            _log(log_fn, f"[sora] sora /api/auth/session 返回空会话: {_response_preview(sora_session_resp, 140)}")
            continue
        access_token = (sora_session.get("accessToken") or "").strip()
        if access_token:
            payload = _decode_jwt_payload(access_token)
            client_id = (payload.get("client_id") or "").strip()
            _log(log_fn, f"[sora] Web session 已建立 origin={origin} client_id={client_id or '-'}")
        return {"access_token": access_token, "session": sora_session, "base_origin": origin}
    return {}


def _read_chatgpt_web_session(web_session, log_fn=None) -> dict:
    chatgpt_session_resp = web_session.get(
        f"{CHATGPT_ORIGIN}/api/auth/session",
        headers={**_build_web_headers(), "Referer": f"{CHATGPT_ORIGIN}/"},
        timeout=DEFAULT_TIMEOUT,
    )
    if chatgpt_session_resp.status_code != 200:
        _log(log_fn, f"[phone_bind] chatgpt /api/auth/session HTTP {chatgpt_session_resp.status_code} {_response_preview(chatgpt_session_resp, 140)}")
        return {}
    try:
        session_data = chatgpt_session_resp.json()
    except Exception:
        _log(log_fn, f"[phone_bind] chatgpt /api/auth/session 非 JSON: {_response_preview(chatgpt_session_resp, 140)}")
        return {}
    if not isinstance(session_data, dict) or not session_data:
        _log(log_fn, f"[phone_bind] chatgpt /api/auth/session 返回空会话: {_response_preview(chatgpt_session_resp, 140)}")
        return {}
    access_token = (session_data.get("accessToken") or "").strip()
    if access_token:
        payload = _decode_jwt_payload(access_token)
        client_id = (payload.get("client_id") or "").strip()
        age = _chatgpt_pwd_auth_age_seconds(access_token)
        _log(log_fn, f"[phone_bind] ChatGPT Web session 已建立 client_id={client_id or '-'} pwd_auth_age={age if age is not None else '-'}s")
    return {"access_token": access_token, "session": session_data}


def sora_probe_nf2_session(
    access_token: str,
    *,
    proxy_url: str = None,
    web_session=None,
    preferred_origin: str = "",
    log_fn=None,
) -> dict:
    token = (access_token or "").strip()
    if not token:
        return {}
    last_status = 0
    last_preview = ""
    for origin in _candidate_sora_web_origins(preferred_origin):
        try:
            resp = sora_nf2_get_pending(
                token,
                proxy_url=proxy_url,
                web_session=web_session,
                base_origin=origin,
            )
        except Exception as exc:
            _log(log_fn, f"[sora] NF2 probe 请求异常 origin={origin}: {exc}")
            continue
        status_code = int(getattr(resp, "status_code", 0) or 0)
        preview = _response_preview(resp, 160)
        if status_code == 200:
            return {
                "ok": True,
                "status_code": status_code,
                "base_origin": origin,
                "preview": preview,
            }
        last_status = status_code
        last_preview = preview
        _log(log_fn, f"[sora] NF2 probe HTTP {status_code} origin={origin} {preview or '-'}")
    return {
        "ok": False,
        "status_code": last_status,
        "base_origin": "",
        "preview": last_preview,
    }


def sora_import_browser_web_session(
    *,
    expected_email: str = "",
    preferred_origin: str = "",
    cdp_urls=None,
    log_fn=None,
) -> dict:
    expected = (expected_email or "").strip().lower()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _log(log_fn, f"[sora] 浏览器 session fallback 不可用: {exc}")
        return {}

    for cdp_url in _candidate_browser_cdp_urls(cdp_urls):
        browser = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(cdp_url)
                contexts = list(getattr(browser, "contexts", []) or [])
                if not contexts:
                    _log(log_fn, f"[sora] CDP {cdp_url} 无可用浏览器上下文")
                    continue
                for context in contexts:
                    pages = list(getattr(context, "pages", []) or [])
                    page = None
                    for candidate in pages:
                        url = str(getattr(candidate, "url", "") or "")
                        if any(origin in url for origin in (SORA_ORIGIN, SORA_LEGACY_ORIGIN, CHATGPT_ORIGIN)):
                            page = candidate
                            break
                    if page is None:
                        page = pages[0] if pages else context.new_page()
                    for origin in _candidate_sora_web_origins(preferred_origin):
                        target_url = f"{origin}/explore"
                        try:
                            page.goto(target_url, wait_until="domcontentloaded", timeout=120000)
                        except Exception as exc:
                            _log(log_fn, f"[sora] CDP 导航失败 {target_url}: {exc}")
                            continue
                        try:
                            session_payload = page.evaluate(
                                """
                                async () => {
                                  const resp = await fetch('/api/auth/session', { credentials: 'include' });
                                  const text = await resp.text();
                                  let data = null;
                                  try {
                                    data = JSON.parse(text);
                                  } catch (err) {}
                                  return {
                                    status: resp.status,
                                    data,
                                    text_preview: text.slice(0, 400),
                                  };
                                }
                                """
                            )
                        except Exception as exc:
                            _log(log_fn, f"[sora] CDP 读取 /api/auth/session 失败 origin={origin}: {exc}")
                            continue
                        if not isinstance(session_payload, dict):
                            continue
                        session_data = session_payload.get("data") or {}
                        if int(session_payload.get("status") or 0) != 200 or not isinstance(session_data, dict):
                            continue
                        access_token = (session_data.get("accessToken") or "").strip()
                        session_email = (
                            ((session_data.get("user") or {}).get("email") or "")
                            or ((session_data.get("profile") or {}).get("email") or "")
                        ).strip().lower()
                        if expected and session_email and session_email != expected:
                            _log(log_fn, f"[sora] CDP 登录邮箱不匹配 browser={session_email} expected={expected}")
                            continue
                        if not access_token or not is_chatgpt_web_access_token(access_token):
                            continue
                        try:
                            cookies = context.cookies([SORA_ORIGIN, SORA_LEGACY_ORIGIN, CHATGPT_ORIGIN, AUTH_ORIGIN])
                        except Exception as exc:
                            _log(log_fn, f"[sora] CDP 读取 cookies 失败 origin={origin}: {exc}")
                            continue
                        web_session = _make_web_session()
                        copied = _copy_browser_cookie_dicts(cookies, web_session)
                        if copied <= 0:
                            try:
                                web_session.close()
                            except Exception:
                                pass
                            continue
                        session_state = _read_sora_web_session(web_session, preferred_origin=origin, log_fn=log_fn)
                        effective_token = (session_state.get("access_token") or access_token).strip()
                        if not effective_token:
                            try:
                                web_session.close()
                            except Exception:
                                pass
                            continue
                        nf2_probe = sora_probe_nf2_session(
                            effective_token,
                            web_session=web_session,
                            preferred_origin=(session_state.get("base_origin") or origin),
                            log_fn=log_fn,
                        )
                        if not nf2_probe.get("ok"):
                            try:
                                web_session.close()
                            except Exception:
                                pass
                            continue
                        return {
                            "access_token": effective_token,
                            "session": session_state.get("session") or session_data,
                            "web_session": web_session,
                            "base_origin": (nf2_probe.get("base_origin") or session_state.get("base_origin") or origin).strip(),
                            "email": session_email,
                            "cookie_count": copied,
                            "cdp_url": cdp_url,
                            "source": "browser_cdp",
                        }
        except Exception as exc:
            _log(log_fn, f"[sora] CDP 连接失败 {cdp_url}: {exc}")
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
    return {}


def _complete_chatgpt_provider_flow(
    web_session,
    authorize_url: str,
    *,
    referer: str = "",
    login_email: str = "",
    login_password: str = "",
    get_otp_fn=None,
    proxy_url: str = None,
    log_fn=None,
    log_prefix: str = "chatgpt",
    session_reader=None,
) -> dict:
    pr = _load_register_helpers()
    if not pr or web_session is None:
        return {}
    parsed = urlparse(authorize_url or "")
    params = parse_qs(parsed.query, keep_blank_values=False)
    redirect_uri = ((params.get("redirect_uri") or [""])[0] or "").strip()
    state = ((params.get("state") or [""])[0] or "").strip()
    device_id = ((params.get("device_id") or [""])[0] or "").strip()
    if not redirect_uri or not state or not device_id:
        _log(log_fn, f"[sora] {log_prefix} authorize 缺少 redirect_uri/state/device_id: {(authorize_url or '')[:180]}")
        return {}

    auth_start = web_session.get(
        authorize_url,
        headers=_build_html_headers(referer=referer or CHATGPT_ORIGIN),
        timeout=DEFAULT_TIMEOUT,
        allow_redirects=True,
    )
    _log(log_fn, f"[sora] {log_prefix} provider authorize -> {str(auth_start.url)[:160]}")

    callback_url = ""
    auth_code = ""
    for candidate in _collect_response_urls(auth_start):
        if "api/auth/callback/openai" in candidate:
            callback_url = candidate
            break
        auth_code = pr._parse_code_from_url(candidate)
        if auth_code:
            break

    continue_url = ""
    page_type = ""
    consent_url = str(getattr(auth_start, "url", "") or "").strip() or authorize_url

    if not callback_url and not auth_code:
        if not login_email:
            _log(log_fn, f"[sora] {log_prefix} 缺少登录邮箱，无法继续 provider flow")
            return {}
        api_headers = {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": AUTH_ORIGIN,
            "user-agent": pr.KEYGEN_USER_AGENT,
            "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "referer": f"{AUTH_ORIGIN}/log-in",
            "oai-device-id": device_id,
        }
        api_headers.update(pr._make_trace_headers())
        sentinel_auth = _build_sentinel_header(device_id, flow="authorize_continue", proxy_url=proxy_url, log_fn=log_fn)
        if sentinel_auth:
            api_headers["openai-sentinel-token"] = sentinel_auth
        continue_resp = web_session.post(
            f"{AUTH_ORIGIN}/api/accounts/authorize/continue",
            headers=api_headers,
            json={"username": {"kind": "email", "value": login_email}},
            timeout=DEFAULT_TIMEOUT,
        )
        if continue_resp.status_code != 200:
            _log(log_fn, f"[sora] {log_prefix} authorize/continue HTTP {continue_resp.status_code} {_response_preview(continue_resp, 140)}")
            return {}
        try:
            continue_data = continue_resp.json()
        except Exception:
            continue_data = {}
        continue_url = (continue_data.get("continue_url") or "").strip()
        page_type = ((continue_data.get("page") or {}).get("type") or "").strip()

        needs_password = (
            not continue_url
            or page_type in ("password", "password_verification")
            or "/log-in/password" in continue_url
        )
        if needs_password:
            if not login_password:
                _log(log_fn, f"[sora] {log_prefix} 已到 password 阶段但缺少账号密码")
                return {}
            api_headers["referer"] = f"{AUTH_ORIGIN}/log-in/password"
            api_headers.update(pr._make_trace_headers())
            sentinel_pw = _build_sentinel_header(device_id, flow="password_verify", proxy_url=proxy_url, log_fn=log_fn)
            if sentinel_pw:
                api_headers["openai-sentinel-token"] = sentinel_pw
            password_resp = web_session.post(
                f"{AUTH_ORIGIN}/api/accounts/password/verify",
                headers=api_headers,
                json={"password": login_password},
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=False,
            )
            if password_resp.status_code != 200:
                _log(log_fn, f"[sora] {log_prefix} password/verify HTTP {password_resp.status_code} {_response_preview(password_resp, 140)}")
                return {}
            try:
                password_data = password_resp.json()
            except Exception:
                password_data = {}
            continue_url = (password_data.get("continue_url") or continue_url).strip()
            page_type = ((password_data.get("page") or {}).get("type") or page_type).strip()

    if page_type == "email_otp_verification" or "email-verification" in continue_url:
        otp_code = ""
        if callable(get_otp_fn):
            try:
                otp_code = pr._normalize_otp_code(get_otp_fn() or "")
            except Exception:
                otp_code = ""
        if not otp_code:
            pr._request_login_email_otp(web_session, device_id, lambda msg: _log(log_fn, msg))
            if callable(get_otp_fn):
                try:
                    otp_code = pr._normalize_otp_code(get_otp_fn() or "")
                except Exception:
                    otp_code = ""
        if not otp_code:
            _log(log_fn, f"[sora] {log_prefix} 登录需要邮箱验证码，但未拿到新 OTP")
            return {}
        api_headers = {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": AUTH_ORIGIN,
            "referer": f"{AUTH_ORIGIN}/email-verification",
            "user-agent": pr.KEYGEN_USER_AGENT,
            "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
        }
        otp_resp = None
        for attempt in range(2):
            api_headers.update(pr._make_trace_headers())
            otp_resp = web_session.post(
                f"{AUTH_ORIGIN}/api/accounts/email-otp/validate",
                headers=api_headers,
                json={"code": otp_code},
                timeout=DEFAULT_TIMEOUT,
            )
            if otp_resp.status_code == 200:
                break
            _log(log_fn, f"[sora] {log_prefix} email-otp/validate HTTP {otp_resp.status_code} {_response_preview(otp_resp, 140)}")
            if attempt == 0 and otp_resp.status_code in (400, 401):
                pr._request_login_email_otp(web_session, device_id, lambda msg: _log(log_fn, msg))
                if callable(get_otp_fn):
                    try:
                        otp_code = pr._normalize_otp_code(get_otp_fn() or "")
                    except Exception:
                        otp_code = ""
                if not otp_code:
                    break
                continue
            return {}
        if not otp_resp or otp_resp.status_code != 200:
            return {}
        try:
            otp_data = otp_resp.json()
        except Exception:
            otp_data = {}
        continue_url = (otp_data.get("continue_url") or continue_url).strip()
        page_type = ((otp_data.get("page") or {}).get("type") or page_type).strip()

    if continue_url and ("/about-you" in continue_url or page_type in ("about_you", "about-you")):
        status_create, data_create = pr._create_account(web_session, "User", "1992-09-19")
        if status_create not in (200, 201, 204):
            _log(log_fn, f"[sora] {log_prefix} about-you 提交失败: {status_create}")
            return {}
        continue_url = (
            (data_create.get("continue_url") or "").strip()
            or (data_create.get("url") or "").strip()
            or (data_create.get("redirect_url") or "").strip()
            or continue_url
        )

    if not callback_url and auth_code:
        callback_url = f"{redirect_uri}?{urlencode({'code': auth_code, 'state': state})}"
    if not callback_url:
        if continue_url and "api/auth/callback/openai" in continue_url:
            callback_url = continue_url
        else:
            consent_url = continue_url if continue_url.startswith("http") else (f"{AUTH_ORIGIN}{continue_url}" if continue_url else consent_url)
            auth_code = pr._follow_consent_to_code(web_session, consent_url, lambda msg: _log(log_fn, msg))
            if not auth_code:
                session_data = pr._decode_oai_session_cookie(web_session)
                workspaces = (session_data or {}).get("workspaces") or []
                workspace_id = workspaces[0].get("id") if workspaces else None
                if workspace_id:
                    api_headers = {
                        "accept": "application/json",
                        "accept-language": "en-US,en;q=0.9",
                        "content-type": "application/json",
                        "origin": AUTH_ORIGIN,
                        "user-agent": pr.KEYGEN_USER_AGENT,
                        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
                        "sec-ch-ua-mobile": "?0",
                        "sec-ch-ua-platform": '"Windows"',
                        "sec-fetch-dest": "empty",
                        "sec-fetch-mode": "cors",
                        "sec-fetch-site": "same-origin",
                        "referer": consent_url,
                        "oai-device-id": device_id,
                    }
                    api_headers.update(pr._make_trace_headers())
                    ws_resp = web_session.post(
                        f"{AUTH_ORIGIN}/api/accounts/workspace/select",
                        headers=api_headers,
                        json={"workspace_id": workspace_id},
                        timeout=DEFAULT_TIMEOUT,
                        allow_redirects=False,
                    )
                    if ws_resp.status_code in (301, 302, 303, 307, 308):
                        loc = (ws_resp.headers.get("Location") or ws_resp.headers.get("location") or "").strip()
                        auth_code = pr._parse_code_from_url(loc)
                        if not auth_code and loc:
                            auth_code = pr._follow_consent_to_code(
                                web_session,
                                loc if loc.startswith("http") else f"{AUTH_ORIGIN}{loc}",
                                lambda msg: _log(log_fn, msg),
                            )
            if not auth_code:
                _log(log_fn, f"[sora] {log_prefix} provider 跟随 consent 后仍未拿到 code")
                return {}
            callback_url = f"{redirect_uri}?{urlencode({'code': auth_code, 'state': state})}"

    callback_resp = web_session.get(
        callback_url,
        headers=_build_html_headers(referer=CHATGPT_ORIGIN),
        timeout=DEFAULT_TIMEOUT,
        allow_redirects=True,
    )
    _log(log_fn, f"[sora] {log_prefix} callback -> {str(callback_resp.url)[:160]}")
    reader = session_reader or _read_sora_web_session
    if not callable(reader):
        return {}
    return reader(web_session, log_fn=log_fn)


def sora_chatgpt_web_login_from_authenticated_session(
    web_session,
    email: str = "",
    password: str = "",
    get_otp_fn=None,
    log_fn=None,
) -> dict:
    """
    复用已在 auth.openai.com 完成登录的 session，建立 ChatGPT/Sora Web session。
    优先沿着 provider flow 继续，避免 fresh 注册后再触发一轮完整邮箱 OTP。
    """
    if web_session is None:
        return {}

    login_url = f"{CHATGPT_ORIGIN}/auth/login?next=/sora/login?next=%2Fauth%2Flogin_with"
    proxy_url = None
    try:
        proxies = getattr(web_session, "proxies", None) or {}
        proxy_url = (proxies.get("https") or proxies.get("http") or "").strip() or None
    except Exception:
        proxy_url = None
    browser_session = _make_web_session(proxy_url=proxy_url)
    _copy_session_cookies(web_session, browser_session)
    try:
        page_resp = browser_session.get(
            login_url,
            headers=_build_html_headers(),
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        csrf_resp = browser_session.get(
            f"{CHATGPT_ORIGIN}/api/auth/csrf",
            headers={**_build_web_headers(), "Referer": str(page_resp.url)},
            timeout=DEFAULT_TIMEOUT,
        )
        csrf_token = ""
        if csrf_resp.status_code == 200:
            try:
                csrf_token = (csrf_resp.json().get("csrfToken") or "").strip()
            except Exception:
                csrf_token = ""
        if not csrf_token:
            _log(log_fn, f"[sora] 复用 session 获取 chatgpt csrf 失败 HTTP {csrf_resp.status_code} {_response_preview(csrf_resp, 120)}")
            return {}

        signin_resp = browser_session.post(
            f"{CHATGPT_ORIGIN}/api/auth/signin/openai",
            headers={
                **_build_web_headers(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": str(page_resp.url),
            },
            data={
                "csrfToken": csrf_token,
                "callbackUrl": f"{CHATGPT_ORIGIN}/",
            },
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=False,
        )
        authorize_url = (signin_resp.headers.get("Location") or signin_resp.headers.get("location") or "").strip()
        if signin_resp.status_code not in (301, 302, 303, 307, 308) or not authorize_url:
            _log(log_fn, f"[sora] 复用 session chatgpt signin/openai HTTP {signin_resp.status_code} {_response_preview(signin_resp, 120)}")
            return {}

        return _complete_chatgpt_provider_flow(
            browser_session,
            authorize_url,
            referer=str(page_resp.url),
            login_email=(email or "").strip(),
            login_password=(password or "").strip(),
            get_otp_fn=get_otp_fn,
            proxy_url=proxy_url,
            log_fn=log_fn,
            log_prefix="复用 session",
        )
    except Exception as exc:
        _log(log_fn, f"[sora] 复用登录 session 建立 Web session 异常: {exc}")
        return {}
    finally:
        try:
            browser_session.close()
        except Exception:
            pass


def sora_chatgpt_web_login(
    email: str,
    password: str,
    get_otp_fn=None,
    proxy_url: str = None,
    log_fn=None,
    return_web_session: bool = False,
) -> dict:
    """
    通过 ChatGPT next-auth + auth.openai.com 登录，建立可供 Sora 使用的 Web session。
    返回 {"access_token": str, "session": dict}；失败返回空 dict。
    """
    login_email = (email or "").strip()
    login_password = (password or "").strip()
    if not login_email or not login_password:
        return {}
    pr = _load_register_helpers()
    if not pr:
        _log(log_fn, "[sora] 无法导入 protocol_register，跳过 ChatGPT Web 登录补链")
        return {}
    if callable(get_otp_fn):
        seed_current_otps = getattr(get_otp_fn, "seed_current_otps", None)
        if callable(seed_current_otps):
            try:
                seeded = seed_current_otps(folders=["junkemail", "inbox"])
            except Exception:
                seeded = set()
            if seeded:
                _log(log_fn, f"[sora] chatgpt 预排除旧 OTP: {','.join(sorted(seeded))}")

    login_url = f"{CHATGPT_ORIGIN}/auth/login?next=/sora/login?next=%2Fauth%2Flogin_with"
    web_session = _make_web_session(proxy_url=proxy_url)
    try:
        page_resp = None
        csrf_token = ""
        for attempt in range(2):
            page_resp = web_session.get(
                login_url,
                headers=_build_html_headers(),
                timeout=DEFAULT_TIMEOUT,
            )
            csrf_resp = web_session.get(
                f"{CHATGPT_ORIGIN}/api/auth/csrf",
                headers={**_build_web_headers(), "Referer": str(page_resp.url)},
                timeout=DEFAULT_TIMEOUT,
            )
            if csrf_resp.status_code == 200:
                try:
                    csrf_token = (csrf_resp.json().get("csrfToken") or "").strip()
                except Exception:
                    csrf_token = ""
                if csrf_token:
                    break
                _log(log_fn, f"[sora] chatgpt /api/auth/csrf 200 但无 csrfToken {_response_preview(csrf_resp, 120)}")
            else:
                _log(log_fn, f"[sora] chatgpt /api/auth/csrf HTTP {csrf_resp.status_code} {_response_preview(csrf_resp, 120)}")
            if attempt == 0:
                time.sleep(2)
        if not csrf_token:
            return {}

        signin_resp = web_session.post(
            f"{CHATGPT_ORIGIN}/api/auth/signin/openai",
            headers={
                **_build_web_headers(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": str(page_resp.url),
            },
            data={
                "csrfToken": csrf_token,
                "callbackUrl": f"{CHATGPT_ORIGIN}/",
            },
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=False,
        )
        authorize_url = (signin_resp.headers.get("Location") or signin_resp.headers.get("location") or "").strip()
        if signin_resp.status_code not in (301, 302, 303, 307, 308) or not authorize_url:
            _log(log_fn, f"[sora] chatgpt signin/openai HTTP {signin_resp.status_code} {_response_preview(signin_resp, 120)}")
            return {}

        web_auth = _complete_chatgpt_provider_flow(
            web_session,
            authorize_url,
            referer=str(page_resp.url),
            login_email=login_email,
            login_password=login_password,
            get_otp_fn=get_otp_fn,
            proxy_url=proxy_url,
            log_fn=log_fn,
            log_prefix="chatgpt",
        )
        if (
            return_web_session
            and isinstance(web_auth, dict)
            and (web_auth.get("access_token") or "").strip()
        ):
            web_auth = dict(web_auth)
            web_auth["web_session"] = web_session
            web_session = None
        return web_auth
    except Exception as exc:
        _log(log_fn, f"[sora] ChatGPT Web 登录异常: {exc}")
        return {}
    finally:
        if web_session is not None:
            try:
                web_session.close()
            except Exception:
                pass


def chatgpt_mfa_info(access_token: str, proxy_url: str = None, log_fn=None, web_session=None) -> dict:
    own_session = web_session is None
    session = web_session or _make_web_session(proxy_url=proxy_url)
    referer = _warm_chatgpt_security_page(session, log_fn=log_fn)
    try:
        resp = session.get(
            f"{CHATGPT_BACKEND_API_ORIGIN}/accounts/mfa_info",
            headers=_build_chatgpt_backend_headers(access_token, referer=referer),
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code != 200:
            code, message, preview = _extract_api_error(resp)
            _log(log_fn, f"[phone_bind] mfa_info HTTP {resp.status_code} code={code or '-'} msg={message or preview or '-'}")
            return {}
        data = resp.json() if hasattr(resp, "json") and callable(resp.json) else {}
        if isinstance(data, dict):
            _log(
                log_fn,
                f"[phone_bind] mfa_info mfa_enabled_v2={data.get('mfa_enabled_v2')} show_sms={data.get('show_sms')} show_passkey={data.get('show_passkey')}",
            )
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        _log(log_fn, f"[phone_bind] mfa_info 异常: {exc}")
        return {}
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def _chatgpt_mfa_enroll_once(web_session, access_token: str, phone_number: str, *, channel: str = "sms", log_fn=None) -> tuple:
    referer = _warm_chatgpt_security_page(web_session, log_fn=log_fn)
    try:
        resp = web_session.post(
            f"{CHATGPT_BACKEND_API_ORIGIN}/accounts/mfa/enroll",
            headers=_build_chatgpt_backend_headers(access_token, referer=referer),
            json={
                "factor_type": "sms",
                "phone_number": phone_number,
                "phone_verification_channel": (channel or "sms").strip() or "sms",
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        _log(log_fn, f"[phone_bind] mfa/enroll 异常: {exc}")
        return False, {}, "other"

    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            data = {}
        if isinstance(data, dict) and isinstance(data.get("session_id"), str) and data.get("session_id"):
            return True, data, ""
        _log(log_fn, f"[phone_bind] mfa/enroll 返回异常结构: {_response_preview(resp, 180)}")
        return False, data if isinstance(data, dict) else {}, "other"

    code, message, preview = _extract_api_error(resp)
    text = " ".join(x for x in (code, message, preview) if x).lower()
    _log(log_fn, f"[phone_bind] mfa/enroll HTTP {resp.status_code} code={code or '-'} msg={message or preview or '-'}")
    if "recent_auth_required" in text or "re-authenticate" in text or "reauth" in text:
        return False, {}, "recent_auth_required"
    if "invalid_request" in text:
        return False, {}, "invalid_request"
    if "already" in text and ("phone" in text or "factor" in text):
        return False, {}, "phone_used"
    return False, {}, "other"


def _chatgpt_mfa_activate_enrollment(web_session, access_token: str, session_id: str, code: str, log_fn=None) -> bool:
    referer = _warm_chatgpt_security_page(web_session, log_fn=log_fn)
    try:
        resp = web_session.post(
            f"{CHATGPT_BACKEND_API_ORIGIN}/accounts/mfa/user/activate_enrollment",
            headers=_build_chatgpt_backend_headers(access_token, referer=referer),
            json={
                "code": code,
                "factor_type": "sms",
                "session_id": session_id,
            },
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code == 200:
            return True
        code_text, message, preview = _extract_api_error(resp)
        _log(log_fn, f"[phone_bind] activate_enrollment HTTP {resp.status_code} code={code_text or '-'} msg={message or preview or '-'}")
        return False
    except Exception as exc:
        _log(log_fn, f"[phone_bind] activate_enrollment 异常: {exc}")
        return False


def chatgpt_open_recent_auth_session_for_mfa(
    email: str,
    password: str,
    get_otp_fn=None,
    proxy_url: str = None,
    log_fn=None,
) -> dict:
    login_email = (email or "").strip()
    login_password = (password or "").strip()
    if not login_email or not login_password:
        return {}
    if callable(get_otp_fn):
        seed_current_otps = getattr(get_otp_fn, "seed_current_otps", None)
        if callable(seed_current_otps):
            try:
                seeded = seed_current_otps(folders=["junkemail", "inbox"])
            except Exception:
                seeded = set()
            if seeded:
                _log(log_fn, f"[phone_bind] reauth 预排除旧 OTP: {','.join(sorted(seeded))}")

    web_session = _make_web_session(proxy_url=proxy_url)
    callback_url = f"{CHATGPT_SECURITY_SETTINGS_URL}?action=enable&factor=sms"
    web_auth = {}
    try:
        page_resp = web_session.get(
            callback_url,
            headers=_build_html_headers(referer=CHATGPT_ORIGIN),
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        csrf_resp = web_session.get(
            f"{CHATGPT_ORIGIN}/api/auth/csrf",
            headers={**_build_web_headers(), "Referer": str(page_resp.url)},
            timeout=DEFAULT_TIMEOUT,
        )
        csrf_token = ""
        if csrf_resp.status_code == 200:
            try:
                csrf_token = (csrf_resp.json().get("csrfToken") or "").strip()
            except Exception:
                csrf_token = ""
        if not csrf_token:
            _log(log_fn, f"[phone_bind] reauth 获取 csrf 失败 HTTP {csrf_resp.status_code} {_response_preview(csrf_resp, 120)}")
            return {}

        device_id = str(uuid.uuid4())
        signin_resp = web_session.post(
            f"{CHATGPT_ORIGIN}/api/auth/signin/openai?{urlencode({'reauth': 'password', 'max_age': '0', 'login_hint': login_email, 'ext-oai-did': device_id})}",
            headers={
                **_build_web_headers(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": str(page_resp.url),
            },
            data={
                "csrfToken": csrf_token,
                "callbackUrl": callback_url,
            },
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=False,
        )
        authorize_url = (signin_resp.headers.get("Location") or signin_resp.headers.get("location") or "").strip()
        if signin_resp.status_code not in (301, 302, 303, 307, 308) or not authorize_url:
            _log(log_fn, f"[phone_bind] reauth signin/openai HTTP {signin_resp.status_code} {_response_preview(signin_resp, 120)}")
            return {}

        web_auth = _complete_chatgpt_provider_flow(
            web_session,
            authorize_url,
            referer=str(page_resp.url),
            login_email=login_email,
            login_password=login_password,
            get_otp_fn=get_otp_fn,
            proxy_url=proxy_url,
            log_fn=log_fn,
            log_prefix="reauth",
            session_reader=_read_chatgpt_web_session,
        )
        access_token = (web_auth.get("access_token") or "").strip() if isinstance(web_auth, dict) else ""
        if not access_token:
            return {}
        mfa_info = chatgpt_mfa_info(access_token, proxy_url=proxy_url, log_fn=log_fn, web_session=web_session)
        return {
            "access_token": access_token,
            "session": web_auth.get("session") or {},
            "mfa_info": mfa_info or {},
            "web_session": web_session,
        }
    except Exception as exc:
        _log(log_fn, f"[phone_bind] reauth 建立 recent-auth session 异常: {exc}")
        return {}
    finally:
        if not isinstance(web_auth, dict) or not (web_auth.get("access_token") or "").strip():
            try:
                web_session.close()
            except Exception:
                pass


def sora_probe_web_auth(access_token: str = "", proxy_url: str = None, log_fn=None) -> dict:
    """
    探测当前 Sora Web 会话入口，帮助定位「Bearer token 不可用」与「Web session 未建立」。
    返回示例：
    {
      "session_state": "null" | "present" | "error",
      "provider_client_id": "...",
      "provider_redirect_uri": "...",
      "provider_audience": "...",
      "token_client_id": "...",
    }
    """
    out = {
        "session_state": "",
        "provider_client_id": "",
        "provider_redirect_uri": "",
        "provider_audience": "",
        "provider_scope": "",
        "token_client_id": "",
    }

    token_payload = _decode_jwt_payload(access_token)
    token_client_id = (token_payload.get("client_id") or "").strip()
    if token_client_id:
        out["token_client_id"] = token_client_id

    try:
        web_session = _make_web_session(proxy_url=proxy_url)
        session_resp = web_session.get(
            f"{SORA_ORIGIN}/api/auth/session",
            headers=_build_web_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        if session_resp.status_code == 200:
            body = (session_resp.text or "").strip()
            if body == "null":
                out["session_state"] = "null"
            elif body:
                out["session_state"] = "present"
            else:
                out["session_state"] = "empty"
        else:
            out["session_state"] = f"http_{session_resp.status_code}"
            _log(log_fn, f"[sora] web /api/auth/session HTTP {session_resp.status_code} {_response_preview(session_resp, 120)}")
    except Exception as exc:
        out["session_state"] = "error"
        _log(log_fn, f"[sora] web /api/auth/session 异常: {exc}")

    try:
        web_session = locals().get("web_session") or _make_web_session(proxy_url=proxy_url)
        csrf_resp = web_session.get(
            f"{SORA_ORIGIN}/api/auth/csrf",
            headers=_build_web_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        csrf_token = ""
        if csrf_resp.status_code == 200:
            try:
                csrf_token = (csrf_resp.json().get("csrfToken") or "").strip()
            except Exception:
                csrf_token = ""
            if not csrf_token:
                _log(log_fn, f"[sora] web /api/auth/csrf 200 但无 csrfToken {_response_preview(csrf_resp, 120)}")
        else:
            _log(log_fn, f"[sora] web /api/auth/csrf HTTP {csrf_resp.status_code} {_response_preview(csrf_resp, 120)}")

        if csrf_token:
            signin_resp = web_session.post(
                f"{SORA_ORIGIN}/api/auth/signin/openai",
                headers={
                    **_build_web_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "csrfToken": csrf_token,
                    "callbackUrl": f"{SORA_ORIGIN}/",
                },
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=False,
            )
            loc = (signin_resp.headers.get("Location") or signin_resp.headers.get("location") or "").strip()
            if signin_resp.status_code in (301, 302, 303, 307, 308) and loc:
                parsed = urlparse(loc)
                params = parse_qs(parsed.query, keep_blank_values=False)
                out["provider_client_id"] = ((params.get("client_id") or [""])[0] or "").strip()
                out["provider_redirect_uri"] = ((params.get("redirect_uri") or [""])[0] or "").strip()
                out["provider_audience"] = ((params.get("audience") or [""])[0] or "").strip()
                out["provider_scope"] = ((params.get("scope") or [""])[0] or "").strip()
            else:
                _log(
                    log_fn,
                    f"[sora] web signin/openai HTTP {signin_resp.status_code} {_response_preview(signin_resp, 120)}",
                )
    except Exception as exc:
        _log(log_fn, f"[sora] web signin/openai 探测异常: {exc}")
    finally:
        try:
            web_session.close()
        except Exception:
            pass

    provider_client_id = out["provider_client_id"]
    if provider_client_id:
        _log(
            log_fn,
            f"[sora] web auth session={out['session_state'] or '-'} provider_client_id={provider_client_id} redirect_uri={out['provider_redirect_uri'] or '-'}",
        )
    if token_client_id and provider_client_id and token_client_id != provider_client_id:
        _log(
            log_fn,
            f"[sora] 当前 AT client_id={token_client_id} 与 Sora web provider client_id={provider_client_id} 不一致",
        )
    return out


def rt_to_at_mobile(refresh_token: str, proxy_url: str = None, log_fn=None) -> dict:
    """
    RT 换 AT（移动端 client_id/redirect_uri）。返回 {"access_token": str, "refresh_token": str|None, "id_token": str|None}，失败抛异常或返回空。
    """
    rt = (refresh_token or "").strip()
    if not rt:
        _log(log_fn, "[phone_bind] RT 为空")
        return {}
    for attempt in range(2):
        try:
            r = _session_post(
                f"{AUTH_ORIGIN}/oauth/token",
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={
                    "client_id": MOBILE_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "redirect_uri": MOBILE_REDIRECT_URI,
                    "refresh_token": rt,
                },
                proxy_url=proxy_url,
                timeout=30,
            )
            if r.status_code == 200:
                d = r.json()
                at = (d.get("access_token") or "").strip()
                if at:
                    return {
                        "access_token": at,
                        "refresh_token": d.get("refresh_token"),
                        "id_token": d.get("id_token"),
                    }
            if log_fn and attempt == 0:
                code, message, preview = _extract_error(r)
                _log(log_fn, f"[phone_bind] RT 换 AT HTTP {r.status_code} code={code or '-'} msg={message or preview or '-'}")
        except Exception as e:
            _log(log_fn, f"[phone_bind] RT 换 AT 异常: {e}")
            if attempt == 0:
                time.sleep(2)
                continue
    return {}


def _legacy_sora_bootstrap(access_token: str, proxy_url: str = None, log_fn=None) -> bool:
    """兼容旧版 GET backend/m/bootstrap。"""
    for origin in _candidate_origins():
        try:
            r = _session_get(
                f"{origin}/backend/m/bootstrap",
                headers=_build_headers(access_token, origin=origin),
                proxy_url=proxy_url,
            )
            if r.status_code == 200:
                return True
            code, message, preview = _extract_error(r)
            _log(log_fn, f"[sora] legacy bootstrap {origin} HTTP {r.status_code} code={code or '-'} msg={message or preview or '-'}")
        except Exception as e:
            _log(log_fn, f"[sora] legacy bootstrap {origin} 异常: {e}")
    return False


def sora_me(access_token: str, proxy_url: str = None, log_fn=None) -> dict:
    """GET backend/me 获取当前用户信息。返回 dict，含 username 等；失败返回 {}."""
    try:
        r = _session_get(
            f"{SORA_ORIGIN}/backend/me",
            headers=_build_headers(access_token),
            proxy_url=proxy_url,
        )
        if r.status_code == 200:
            return r.json() if hasattr(r, "json") and callable(r.json) else {}
        code, message, preview = _extract_error(r)
        _log(log_fn, f"[sora] me HTTP {r.status_code} code={code or '-'} msg={message or preview or '-'}")
        return {}
    except Exception as e:
        _log(log_fn, f"[sora] me 异常: {e}")
        return {}


def _normalize_username(username: str) -> str:
    value = "".join(c for c in (username or "").strip().lower() if c.isalnum() or c == "_")
    if not value:
        return ""
    if not value[0].isalnum():
        value = "user_" + value
    return value[:20]


def _normalize_video_orientation(orientation: str) -> str:
    value = (orientation or "wide").strip().lower()
    aliases = {
        "landscape": "wide",
        "16:9": "wide",
        "wide": "wide",
        "portrait": "tall",
        "9:16": "tall",
        "tall": "tall",
        "square": "square",
        "1:1": "square",
    }
    return aliases.get(value, "wide")


def _video_dimensions(resolution: int = 360, orientation: str = "wide") -> tuple[int, int]:
    base = int(resolution or 360)
    if base <= 0:
        base = 360
    direction = _normalize_video_orientation(orientation)
    if direction == "square":
        return base, base
    long_edge = int(round(base * 16 / 9))
    if direction == "tall":
        return base, long_edge
    return long_edge, base


def sora_build_simple_video_payload(
    prompt: str,
    *,
    operation: str = "simple_compose",
    n_variants: int = 4,
    n_frames: int = 300,
    resolution: int = 360,
    orientation: str = "wide",
    model: str = None,
    seed: int = None,
) -> dict:
    width, height = _video_dimensions(resolution=resolution, orientation=orientation)
    payload = {
        "type": "video_gen",
        "operation": (operation or "simple_compose").strip() or "simple_compose",
        "prompt": (prompt or "").strip(),
        "n_variants": int(n_variants or 4),
        "n_frames": int(n_frames or 300),
        "width": width,
        "height": height,
        "inpaint_items": [],
        "is_storyboard": False,
        "model": (model or "").strip() or None,
        "seed": seed,
    }
    return _strip_nullish(payload)


def is_chatgpt_web_access_token(access_token: str) -> bool:
    payload = _decode_jwt_payload(access_token)
    return (payload.get("client_id") or "").strip() == CHATGPT_WEB_CLIENT_ID


def _normalize_nf2_orientation(orientation: str) -> str:
    value = (orientation or "portrait").strip().lower()
    aliases = {
        "portrait": "portrait",
        "tall": "portrait",
        "9:16": "portrait",
        "wide": "landscape",
        "landscape": "landscape",
        "16:9": "landscape",
        "square": "landscape",
        "1:1": "landscape",
    }
    return aliases.get(value, "portrait")


def _nf2_size_from_resolution(resolution: int = 360) -> str:
    try:
        value = int(resolution or 360)
    except Exception:
        value = 360
    return "large" if value >= 720 else "small"


def sora_build_nf2_video_payload(
    prompt: str,
    *,
    n_variants: int = 1,
    n_frames: int = 300,
    resolution: int = 360,
    orientation: str = "portrait",
    model: str = "sy_8",
    style_id: str = "",
    audio_caption: str = "",
    audio_transcript: str = "",
    video_caption: str = "",
    seed: int = None,
) -> dict:
    payload = {
        "kind": "video",
        "prompt": (prompt or "").strip(),
        "title": None,
        "orientation": _normalize_nf2_orientation(orientation),
        "size": _nf2_size_from_resolution(resolution),
        "n_frames": int(n_frames or 300),
        "inpaint_items": [],
        "remix_target_id": None,
        "reroll_target_id": None,
        "project_config": None,
        "trim_config": None,
        "metadata": None,
        "cameo_ids": None,
        "cameo_replacements": None,
        "model": (model or "sy_8").strip() or "sy_8",
        "style_id": (style_id or "").strip() or None,
        "audio_caption": (audio_caption or "").strip() or None,
        "audio_transcript": (audio_transcript or "").strip() or None,
        "video_caption": (video_caption or "").strip() or None,
        "storyboard_id": None,
        "seed": seed,
    }
    try:
        n = int(n_variants or 1)
    except Exception:
        n = 1
    if n > 1:
        payload["nsamples"] = n
    return _strip_nullish(payload)


def sora_build_image_video_payload(
    prompt: str,
    upload_media_id: str,
    *,
    operation: str = "simple_compose",
    n_variants: int = 1,
    n_frames: int = 300,
    resolution: int = 360,
    orientation: str = "wide",
    model: str = None,
    seed: int = None,
) -> dict:
    payload = sora_build_simple_video_payload(
        prompt,
        operation=operation,
        n_variants=n_variants,
        n_frames=n_frames,
        resolution=resolution,
        orientation=orientation,
        model=model,
        seed=seed,
    )
    payload["is_storyboard"] = True
    payload["inpaint_items"] = [
        {
            "type": "image",
            "upload_media_id": (upload_media_id or "").strip(),
            "frame_index": 0,
            "x": 0,
            "y": 0,
            "width": int(payload.get("width") or 0),
            "height": int(payload.get("height") or 0),
        }
    ]
    return _strip_nullish(payload)


def sora_video_gen_create(
    access_token: str,
    prompt: str,
    *,
    operation: str = "simple_compose",
    n_variants: int = 4,
    n_frames: int = 300,
    resolution: int = 360,
    orientation: str = "wide",
    model: str = None,
    seed: int = None,
    proxy_url: str = None,
    log_fn=None,
):
    device_id = str(uuid.uuid4())
    headers = _build_headers(access_token, device_id=device_id)
    sentinel = _build_sentinel_header(device_id, "sora_create_task", proxy_url=proxy_url, log_fn=log_fn)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    payload = sora_build_simple_video_payload(
        prompt,
        operation=operation,
        n_variants=n_variants,
        n_frames=n_frames,
        resolution=resolution,
        orientation=orientation,
        model=model,
        seed=seed,
    )
    return _session_post(
        f"{SORA_ORIGIN}/backend/video_gen",
        headers=headers,
        json=payload,
        proxy_url=proxy_url,
    )


def sora_nf2_create(
    access_token: str,
    prompt: str,
    *,
    n_variants: int = 1,
    n_frames: int = 300,
    resolution: int = 360,
    orientation: str = "portrait",
    model: str = "sy_8",
    style_id: str = "",
    audio_caption: str = "",
    audio_transcript: str = "",
    video_caption: str = "",
    seed: int = None,
    proxy_url: str = None,
    log_fn=None,
    web_session=None,
    base_origin: str = None,
):
    origin = (base_origin or SORA_ORIGIN).rstrip("/")
    device_id = str(uuid.uuid4())
    headers = _build_sora_web_headers(access_token, device_id=device_id, origin=origin)
    sentinel = _build_sentinel_header(device_id, "sora_2_create_task", proxy_url=proxy_url, log_fn=log_fn)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    payload = sora_build_nf2_video_payload(
        prompt,
        n_variants=n_variants,
        n_frames=n_frames,
        resolution=resolution,
        orientation=orientation,
        model=model,
        style_id=style_id,
        audio_caption=audio_caption,
        audio_transcript=audio_transcript,
        video_caption=video_caption,
        seed=seed,
    )
    path = "/backend/nf/bulk_create" if int(payload.get("nsamples") or 1) > 1 else "/backend/nf/create"
    return _web_session_json_post(
        f"{origin}{path}",
        headers=headers,
        json=payload,
        proxy_url=proxy_url,
        web_session=web_session,
    )


def sora_nf2_get_task(access_token: str, task_id: str, proxy_url: str = None, web_session=None, base_origin: str = None):
    task = (task_id or "").strip()
    origin = (base_origin or SORA_ORIGIN).rstrip("/")
    return _web_session_get(
        f"{origin}/backend/nf/tasks/{task}/v2",
        headers=_build_sora_web_headers(access_token, origin=origin),
        proxy_url=proxy_url,
        web_session=web_session,
    )


def sora_nf2_get_pending(access_token: str, proxy_url: str = None, web_session=None, base_origin: str = None):
    origin = (base_origin or SORA_ORIGIN).rstrip("/")
    return _web_session_get(
        f"{origin}/backend/nf/pending/v2",
        headers=_build_sora_web_headers(access_token, origin=origin),
        proxy_url=proxy_url,
        web_session=web_session,
    )


def sora_nf2_get_draft(access_token: str, draft_id: str, proxy_url: str = None, web_session=None, base_origin: str = None):
    draft = (draft_id or "").strip()
    origin = (base_origin or SORA_ORIGIN).rstrip("/")
    return _web_session_get(
        f"{origin}/backend/project_y/profile/drafts/v2/{draft}",
        headers=_build_sora_web_headers(access_token, origin=origin),
        proxy_url=proxy_url,
        web_session=web_session,
    )


def sora_nf2_stitch(
    access_token: str,
    generation_ids: list[str],
    *,
    for_download: bool = False,
    proxy_url: str = None,
    web_session=None,
    base_origin: str = None,
):
    origin = (base_origin or SORA_ORIGIN).rstrip("/")
    query = "?for_download=true" if for_download else ""
    payload = {"generation_ids": [str(item).strip() for item in (generation_ids or []) if str(item).strip()]}
    return _web_session_json_post(
        f"{origin}/backend/editor/stitch{query}",
        headers=_build_sora_web_headers(access_token, origin=origin),
        json=payload,
        proxy_url=proxy_url,
        web_session=web_session,
    )


def sora_upload_media(
    access_token: str,
    *,
    filename: str,
    content_type: str,
    file_bytes: bytes = None,
    file_path: str = None,
    media_type: str = "image",
    proxy_url: str = None,
):
    headers = _build_headers(access_token, device_id=str(uuid.uuid4()))
    headers.pop("Content-Type", None)
    return _session_multipart_post(
        f"{SORA_ORIGIN}/backend/uploads",
        headers=headers,
        data={
            "file_name": (filename or "").strip(),
            "media_type": (media_type or "image").strip() or "image",
        },
        file_field_name="file",
        filename=(filename or "upload.bin").strip() or "upload.bin",
        file_bytes=file_bytes,
        file_path=file_path,
        content_type=(content_type or "application/octet-stream").strip() or "application/octet-stream",
        proxy_url=proxy_url,
    )


def _random_username(prefix: str = "user") -> str:
    prefix = _normalize_username(prefix) or "user"
    prefix = prefix[:11]
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    return _normalize_username(f"{prefix}_{suffix}")


def sora_create_account(access_token: str, birth_date: str = None, proxy_url: str = None, log_fn=None) -> bool:
    """按当前官方前端链路创建 Sora onboarding 账号。"""
    device_id = str(uuid.uuid4())
    headers = _build_headers(access_token, device_id=device_id)
    sentinel = _build_sentinel_header(device_id, "sora_create_account", proxy_url=proxy_url, log_fn=log_fn)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    payload = {"birth_date": birth_date or None}
    try:
        r = _session_post(
            f"{SORA_ORIGIN}/backend/me/onboarding/create_account",
            headers=headers,
            json=payload,
            proxy_url=proxy_url,
        )
        if r.status_code in (200, 201, 204):
            return True
        code, message, preview = _extract_error(r)
        if code == "account_already_created":
            return True
        _log(log_fn, f"[sora] create_account HTTP {r.status_code} code={code or '-'} msg={message or preview or '-'}")
        return False
    except Exception as exc:
        _log(log_fn, f"[sora] create_account 异常: {exc}")
        return False


def _update_me(access_token: str, payload: dict, proxy_url: str = None, log_fn=None) -> tuple[bool, str]:
    try:
        r = _session_post(
            f"{SORA_ORIGIN}/backend/me",
            headers=_build_headers(access_token),
            json=payload,
            proxy_url=proxy_url,
        )
        if r.status_code == 200:
            return True, ""
        code, message, preview = _extract_error(r)
        _log(log_fn, f"[sora] update_me HTTP {r.status_code} code={code or '-'} msg={message or preview or '-'}")
        return False, code
    except Exception as exc:
        _log(log_fn, f"[sora] update_me 异常: {exc}")
        return False, ""


def sora_username_check(access_token: str, username: str, proxy_url: str = None, log_fn=None) -> bool:
    """保留旧版用户名检查接口；当前激活流程不再依赖它。"""
    for origin in _candidate_origins():
        try:
            r = _session_post(
                f"{origin}/backend/project_y/profile/username/check",
                headers=_build_headers(access_token, origin=origin),
                json={"username": username},
                proxy_url=proxy_url,
            )
            if r.status_code == 200:
                d = r.json() if hasattr(r, "json") and callable(r.json) else {}
                return d.get("available", False)
            code, message, preview = _extract_error(r)
            _log(log_fn, f"[sora] legacy username/check {origin} HTTP {r.status_code} code={code or '-'} msg={message or preview or '-'}")
        except Exception as exc:
            _log(log_fn, f"[sora] legacy username/check {origin} 异常: {exc}")
    return False


def _legacy_sora_username_set(access_token: str, username: str, proxy_url: str = None, log_fn=None) -> bool:
    for origin in _candidate_origins():
        try:
            r = _session_post(
                f"{origin}/backend/project_y/profile/username/set",
                headers=_build_headers(access_token, origin=origin),
                json={"username": username},
                proxy_url=proxy_url,
            )
            if r.status_code == 200:
                return True
            code, message, preview = _extract_error(r)
            _log(log_fn, f"[sora] legacy username/set {origin} HTTP {r.status_code} code={code or '-'} msg={message or preview or '-'}")
        except Exception as exc:
            _log(log_fn, f"[sora] legacy username/set {origin} 异常: {exc}")
    return False


def sora_username_set(access_token: str, username: str, proxy_url: str = None, log_fn=None) -> bool:
    """按当前前端链路 POST /backend/me 设置用户名，失败时回退旧接口。"""
    normalized = _normalize_username(username) or _random_username()
    ok, code = _update_me(access_token, {"username": normalized}, proxy_url=proxy_url, log_fn=log_fn)
    if ok:
        return True
    if code in USERNAME_RETRY_CODES:
        return False
    return _legacy_sora_username_set(access_token, normalized, proxy_url=proxy_url, log_fn=log_fn)


def sora_bootstrap(access_token: str, proxy_url: str = None, log_fn=None) -> bool:
    """
    兼容旧调用名。
    现版本优先尝试 onboarding create_account；若当前环境仍是旧接口，再回退 legacy bootstrap。
    """
    if sora_create_account(access_token, proxy_url=proxy_url, log_fn=log_fn):
        return True
    return _legacy_sora_bootstrap(access_token, proxy_url=proxy_url, log_fn=log_fn)


def sora_ensure_activated(
    access_token: str,
    proxy_url: str = None,
    log_fn=None,
    username: str = None,
    birth_date: str = None,
) -> bool:
    """
    确保 Sora 已激活（有 username）。
    新链路：GET /backend/me -> POST /backend/me/onboarding/create_account -> POST /backend/me(username)
    若新链路失败，再回退旧版 bootstrap + project_y username/set。
    返回 True 表示已激活或激活成功。
    """
    me = sora_me(access_token, proxy_url, log_fn)
    if me and me.get("username"):
        _log(log_fn, f"[sora] 已激活 username={me.get('username')}")
        return True
    sora_probe_web_auth(access_token=access_token, proxy_url=proxy_url, log_fn=log_fn)

    if sora_create_account(access_token, birth_date=birth_date, proxy_url=proxy_url, log_fn=log_fn):
        me = sora_me(access_token, proxy_url, log_fn)
        if me and me.get("username"):
            _log(log_fn, f"[sora] create_account 后已激活 username={me.get('username')}")
            return True

    preferred = _normalize_username(username)
    candidates = []
    if preferred:
        candidates.append(preferred)
    for _ in range(5):
        candidates.append(_random_username(prefix=preferred or "user"))

    for uname in candidates:
        ok, code = _update_me(access_token, {"username": uname}, proxy_url=proxy_url, log_fn=log_fn)
        if ok:
            _log(log_fn, f"[sora] 设置用户名成功: {uname}")
            return True
        if code and code not in USERNAME_RETRY_CODES:
            break

    _log(log_fn, "[sora] 新版 onboarding/me 流程失败，回退 legacy project_y 接口")
    _legacy_sora_bootstrap(access_token, proxy_url, log_fn)
    for uname in candidates:
        if sora_username_check(access_token, uname, proxy_url, log_fn):
            if _legacy_sora_username_set(access_token, uname, proxy_url, log_fn):
                _log(log_fn, f"[sora] legacy 设置用户名成功: {uname}")
                return True
    return False


def _legacy_sora_phone_enroll_start(access_token: str, phone_number: str, proxy_url: str = None, log_fn=None) -> tuple:
    try:
        r = _session_post(
            f"{SORA_ORIGIN}/backend/project_y/phone_number/enroll/start",
            headers=_build_headers(access_token),
            json={"phone_number": phone_number, "verification_expiry_window_ms": None},
            proxy_url=proxy_url,
        )
        if r.status_code == 200:
            return True, None
        text = (r.text or "").lower()
        if "already verified" in text or "phone number already" in text:
            return False, "phone_used"
        _log(log_fn, f"[phone_bind] enroll/start HTTP {r.status_code} {_response_preview(r, 150)}")
        return False, "other"
    except Exception as e:
        _log(log_fn, f"[phone_bind] enroll/start 异常: {e}")
        return False, "other"


def _legacy_sora_phone_enroll_finish(access_token: str, phone_number: str, verification_code: str, proxy_url: str = None, log_fn=None) -> bool:
    code = re.sub(r"\D", "", (verification_code or "").strip())[:6]
    if not code:
        return False
    try:
        r = _session_post(
            f"{SORA_ORIGIN}/backend/project_y/phone_number/enroll/finish",
            headers=_build_headers(access_token),
            json={"phone_number": phone_number, "verification_code": code},
            proxy_url=proxy_url,
        )
        ok = r.status_code == 200
        if not ok:
            _log(log_fn, f"[phone_bind] enroll/finish HTTP {r.status_code} {_response_preview(r, 150)}")
        return ok
    except Exception as e:
        _log(log_fn, f"[phone_bind] enroll/finish 异常: {e}")
        return False


def sora_phone_enroll_start(
    access_token: str,
    phone_number: str,
    proxy_url: str = None,
    log_fn=None,
    login_email: str = "",
    login_password: str = "",
    get_otp_fn=None,
) -> tuple:
    """
    优先走 ChatGPT MFA 手机绑定：
    - GET /backend-api/accounts/mfa_info
    - POST /backend-api/accounts/mfa/enroll
    recent_auth_required 时走 reauth=password + max_age=0。

    返回:
    - (True, None, context)
    - (False, "phone_used"|"reauth_failed"|"sms_unavailable"|"other", None)
    """
    normalized_phone = _normalize_phone_number(phone_number)
    if not normalized_phone:
        _log(log_fn, f"[phone_bind] 非法手机号: {phone_number}")
        return False, "other", None

    current_at = (access_token or "").strip()
    current_age = _chatgpt_pwd_auth_age_seconds(current_at)
    if current_age is not None:
        _log(log_fn, f"[phone_bind] 当前 access_token pwd_auth_age={current_age}s")

    def _close_session(obj) -> None:
        if obj is None:
            return
        try:
            obj.close()
        except Exception:
            pass

    def _try_enroll_with_session(web_session, token: str):
        mfa_info = chatgpt_mfa_info(token, proxy_url=proxy_url, log_fn=log_fn, web_session=web_session)
        if not mfa_info:
            return False, "other", None
        if mfa_info.get("show_sms") is False:
            _log(log_fn, "[phone_bind] 当前账号未开放 SMS MFA 入口")
            return False, "sms_unavailable", None
        ok, data, err = _chatgpt_mfa_enroll_once(
            web_session,
            token,
            normalized_phone,
            channel="sms",
            log_fn=log_fn,
        )
        if not ok:
            return False, err or "other", None
        factor = data.get("factor") if isinstance(data, dict) else {}
        return True, None, {
            "web_session": web_session,
            "access_token": token,
            "session_id": (data.get("session_id") or "").strip(),
            "factor_id": (factor.get("id") or "").strip() if isinstance(factor, dict) else "",
            "factor_type": "sms",
            "phone_number": normalized_phone,
        }

    should_try_direct = bool(current_at) and not _chatgpt_needs_recent_auth(current_at)
    if should_try_direct:
        direct_session = _make_web_session(proxy_url=proxy_url)
        try:
            ok, err, context = _try_enroll_with_session(direct_session, current_at)
            if ok:
                return True, None, context
            _close_session(direct_session)
            if err == "phone_used":
                return False, "phone_used", None
            if err == "sms_unavailable":
                return False, "sms_unavailable", None
        except Exception as exc:
            _log(log_fn, f"[phone_bind] 直连 MFA enroll 异常: {exc}")
            _close_session(direct_session)

    if (login_email or "").strip() and (login_password or "").strip():
        reauth = chatgpt_open_recent_auth_session_for_mfa(
            email=login_email,
            password=login_password,
            get_otp_fn=get_otp_fn,
            proxy_url=proxy_url,
            log_fn=log_fn,
        )
        reauth_session = reauth.get("web_session") if isinstance(reauth, dict) else None
        reauth_at = (reauth.get("access_token") or "").strip() if isinstance(reauth, dict) else ""
        if not reauth_session or not reauth_at:
            _log(log_fn, "[phone_bind] recent-auth session 建立失败")
            return False, "reauth_failed", None
        ok, err, context = _try_enroll_with_session(reauth_session, reauth_at)
        if ok:
            return True, None, context
        _close_session(reauth_session)
        if err == "phone_used":
            return False, "phone_used", None
        if err == "sms_unavailable":
            return False, "sms_unavailable", None
        return False, err or "other", None

    _log(log_fn, "[phone_bind] 无账号密码，回退 legacy project_y 手机绑定")
    ok, err = _legacy_sora_phone_enroll_start(access_token, normalized_phone, proxy_url=proxy_url, log_fn=log_fn)
    return ok, err, None


def sora_phone_enroll_finish(
    access_token: str,
    phone_number: str,
    verification_code: str,
    proxy_url: str = None,
    log_fn=None,
    context: dict = None,
) -> bool:
    """优先提交 ChatGPT MFA 验证码；无上下文时回退 legacy project_y。"""
    code = re.sub(r"\D", "", (verification_code or "").strip())[:6]
    if not code:
        return False
    ctx = context if isinstance(context, dict) else {}
    web_session = ctx.get("web_session")
    session_id = (ctx.get("session_id") or "").strip()
    effective_at = (ctx.get("access_token") or access_token or "").strip()
    if web_session is not None and session_id and effective_at:
        try:
            return _chatgpt_mfa_activate_enrollment(
                web_session,
                effective_at,
                session_id,
                code,
                log_fn=log_fn,
            )
        finally:
            try:
                web_session.close()
            except Exception:
                pass
    return _legacy_sora_phone_enroll_finish(
        access_token,
        _normalize_phone_number(phone_number) or phone_number,
        code,
        proxy_url=proxy_url,
        log_fn=log_fn,
    )
