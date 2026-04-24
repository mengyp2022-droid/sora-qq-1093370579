# -*- coding: utf-8 -*-
"""
Sora API 调用接口：
- rt -> at
- bootstrap
- me
- ensure activate
- 通用请求（限制 /backend/* 路径）
- 账号池自动轮换（额度耗尽自动切换下一个可用账号）
- API Key 请求自动注入去水印 header
"""
import json
import mimetypes
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
import requests

from app.database import get_db, init_db
from app.registration_env import inject_registration_modules
from app.services.otp_resolver import build_otp_fetcher
from app.services.sora_api_key import (
    SORA_API_KEY_SCOPE_IMAGE,
    SORA_API_KEY_SCOPE_TEXT,
    get_sora_api_caller,
    sora_api_key_scope_allows,
    sora_api_key_scope_label,
)

router = APIRouter(prefix="/api/sora-api", tags=["sora-api"])

_NF2_WEB_SESSION_CACHE: dict[int, dict] = {}
_NF2_WEB_SESSION_TTL_SECONDS = 20 * 60
_TASK_FAMILY_VIDEO_GEN = "video_gen"
_TASK_FAMILY_NF2 = "nf2"


def _normalize_task_family(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"nf2", "sora_app", "sora_app_nf2"}:
        return _TASK_FAMILY_NF2
    return _TASK_FAMILY_VIDEO_GEN


def _wants_legacy_text_video(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return normalized in {"video_gen", "legacy", "old", "old_chain"}


def _close_nf2_web_session(web_session) -> None:
    if web_session is None:
        return
    try:
        web_session.close()
    except Exception:
        pass


def _drop_nf2_web_session(account_id: Optional[int]) -> None:
    if account_id in (None, ""):
        return
    try:
        key = int(account_id)
    except Exception:
        return
    entry = _NF2_WEB_SESSION_CACHE.pop(key, None)
    if isinstance(entry, dict):
        _close_nf2_web_session(entry.get("web_session"))


def _store_nf2_web_session(
    account_id: Optional[int],
    web_session,
    *,
    access_token: str = "",
    proxy_url: str = "",
    web_origin: str = "",
) -> None:
    if account_id in (None, "") or web_session is None:
        return
    try:
        key = int(account_id)
    except Exception:
        return
    previous = _NF2_WEB_SESSION_CACHE.get(key)
    previous_session = previous.get("web_session") if isinstance(previous, dict) else None
    if previous_session is not None and previous_session is not web_session:
        _close_nf2_web_session(previous_session)
    _NF2_WEB_SESSION_CACHE[key] = {
        "web_session": web_session,
        "access_token": (access_token or "").strip(),
        "proxy_url": (proxy_url or "").strip(),
        "web_origin": (web_origin or "").strip(),
        "updated_at": time.time(),
    }


def _get_nf2_web_session(account_id: Optional[int]) -> Optional[dict]:
    if account_id in (None, ""):
        return None
    try:
        key = int(account_id)
    except Exception:
        return None
    entry = _NF2_WEB_SESSION_CACHE.get(key)
    if not isinstance(entry, dict):
        return None
    updated_at = float(entry.get("updated_at") or 0.0)
    if not updated_at or (time.time() - updated_at) > _NF2_WEB_SESSION_TTL_SECONDS:
        _drop_nf2_web_session(key)
        return None
    if entry.get("web_session") is None:
        _NF2_WEB_SESSION_CACHE.pop(key, None)
        return None
    return entry


def _touch_nf2_web_session(account_id: Optional[int], data: dict) -> None:
    if account_id in (None, ""):
        return
    if not isinstance(data, dict):
        return
    web_session = data.get("web_session")
    if web_session is None:
        return
    _store_nf2_web_session(
        account_id,
        web_session,
        access_token=(data.get("access_token") or "").strip(),
        proxy_url=(data.get("proxy_url") or "").strip(),
        web_origin=(data.get("web_origin") or "").strip(),
    )


def _import_sora_phone():
    inject_registration_modules()
    import protocol_sora_phone as sora_phone
    return sora_phone


def _import_protocol_register():
    inject_registration_modules()
    import protocol_register as pr
    return pr


def _load_account(account_id: int) -> Optional[dict]:
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT id, email, password, refresh_token, access_token, proxy, has_sora,
                      COALESCE(sora_enabled, 1) AS sora_enabled,
                      COALESCE(sora_quota_exhausted, 0) AS sora_quota_exhausted,
                      COALESCE(sora_quota_note, '') AS sora_quota_note,
                      COALESCE(sora_quota_updated_at, '') AS sora_quota_updated_at
               FROM accounts WHERE id = ?""",
            (account_id,),
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1] or "",
        "password": row[2] or "",
        "refresh_token": (row[3] or "").strip(),
        "access_token": (row[4] or "").strip(),
        "proxy": (row[5] or "").strip(),
        "has_sora": bool(row[6]),
        "sora_enabled": bool(row[7]),
        "sora_quota_exhausted": bool(row[8]),
        "sora_quota_note": row[9] or "",
        "sora_quota_updated_at": row[10] or "",
    }


def _load_email_mailbox(email: str) -> Optional[dict]:
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
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT key, value FROM system_settings
               WHERE key IN ('email_api_url', 'email_api_key')"""
        )
        rows = c.fetchall()
    settings = {k: (v or "").strip() for k, v in rows}
    base_url = (settings.get("email_api_url") or "https://gapi.hotmail007.com").rstrip("/")
    client_key = settings.get("email_api_key") or ""
    if not client_key:
        return None
    account_str = f"{mailbox['email']}:{mailbox['password']}:{mailbox['token']}:{mailbox['uuid']}"
    return build_otp_fetcher(base_url, client_key, account_str, timeout_sec=120, interval_sec=5)


def _pick_next_available_account(exclude_ids: list = None) -> dict | None:
    """Round-robin 从可用 Sora 账号池中挑选下一个账号。
    返回 account dict 或 None（无可用账号）。
    """
    exclude = set(exclude_ids or [])
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        # 读取上次使用的游标
        c.execute("SELECT value FROM system_settings WHERE key = 'sora_auto_rotate_cursor'")
        row = c.fetchone()
        try:
            cursor = int((row[0] if row else "0") or "0")
        except Exception:
            cursor = 0

        c.execute(
            """SELECT id, email, refresh_token, access_token, proxy, has_sora,
                      COALESCE(sora_enabled, 1) AS sora_enabled,
                      COALESCE(sora_quota_exhausted, 0) AS sora_quota_exhausted,
                      COALESCE(sora_quota_note, '') AS sora_quota_note,
                      COALESCE(sora_quota_updated_at, '') AS sora_quota_updated_at
               FROM accounts
               WHERE has_sora = 1
                 AND COALESCE(sora_enabled, 1) = 1
                 AND COALESCE(sora_quota_exhausted, 0) = 0
                 AND (COALESCE(refresh_token, '') != '' OR COALESCE(access_token, '') != '')
               ORDER BY id ASC"""
        )
        rows = c.fetchall()
        if not rows:
            return None

        # 从 cursor 之后的第一个可用账号开始
        pick = None
        for r in rows:
            if int(r[0]) > cursor and int(r[0]) not in exclude:
                pick = r
                break
        # 没找到则回绕到最早的可用账号
        if pick is None:
            for r in rows:
                if int(r[0]) not in exclude:
                    pick = r
                    break
        if pick is None:
            return None

        # 更新游标
        c.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
            ("sora_auto_rotate_cursor", str(int(pick[0]))),
        )

    return {
        "id": pick[0],
        "email": pick[1] or "",
        "refresh_token": (pick[2] or "").strip(),
        "access_token": (pick[3] or "").strip(),
        "proxy": (pick[4] or "").strip(),
        "has_sora": bool(pick[5]),
        "sora_enabled": bool(pick[6]),
        "sora_quota_exhausted": bool(pick[7]),
        "sora_quota_note": pick[8] or "",
        "sora_quota_updated_at": pick[9] or "",
    }


def _save_account_tokens(account_id: int, access_token: str = "", refresh_token: str = "", id_token: str = "") -> None:
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        if access_token:
            c.execute("UPDATE accounts SET access_token = ? WHERE id = ?", (access_token, account_id))
        if refresh_token:
            c.execute("UPDATE accounts SET refresh_token = ? WHERE id = ?", (refresh_token, account_id))
        if id_token:
            c.execute("UPDATE accounts SET id_token = ? WHERE id = ?", (id_token, account_id))


def _mark_account_sora(account_id: int) -> None:
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """UPDATE accounts
               SET has_sora = 1,
                   status = CASE
                       WHEN COALESCE(status, '') = 'Registered' THEN 'Registered+Sora'
                       ELSE status
                   END
               WHERE id = ?""",
            (account_id,),
        )


def _mark_account_quota_exhausted(account_id: int, note: str = "") -> None:
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """UPDATE accounts
               SET sora_quota_exhausted = 1,
                   sora_quota_note = ?,
                   sora_last_error = ?,
                   sora_quota_updated_at = datetime('now')
               WHERE id = ?""",
            ((note or "quota_exceeded").strip(), (note or "quota_exceeded").strip(), account_id),
        )


def _clear_account_quota_exhausted(account_id: int) -> None:
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """UPDATE accounts
               SET sora_quota_exhausted = 0,
                   sora_quota_note = '',
                   sora_last_error = '',
                   sora_quota_updated_at = datetime('now')
               WHERE id = ?""",
            (account_id,),
        )


def _mark_account_last_error(account_id: int, message: str = "") -> None:
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """UPDATE accounts
               SET sora_last_error = ?,
                   sora_quota_updated_at = datetime('now')
               WHERE id = ?""",
            ((message or "").strip()[:500], account_id),
        )


def _remember_media_asset(
    media_id: str,
    account_id: int,
    payload: Optional[dict] = None,
    api_key_id: Optional[int] = None,
) -> None:
    asset_id = (media_id or "").strip()
    if not asset_id or not account_id:
        return
    data = payload or {}
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO sora_media_assets
               (media_id, account_id, api_key_id, media_type, filename, mime_type, width, height, source_url, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
               ON CONFLICT(media_id) DO UPDATE SET
                   account_id = excluded.account_id,
                   api_key_id = COALESCE(excluded.api_key_id, sora_media_assets.api_key_id),
                   media_type = COALESCE(excluded.media_type, sora_media_assets.media_type),
                   filename = COALESCE(excluded.filename, sora_media_assets.filename),
                   mime_type = COALESCE(excluded.mime_type, sora_media_assets.mime_type),
                   width = COALESCE(excluded.width, sora_media_assets.width),
                   height = COALESCE(excluded.height, sora_media_assets.height),
                   source_url = COALESCE(excluded.source_url, sora_media_assets.source_url),
                   updated_at = datetime('now')""",
            (
                asset_id,
                int(account_id),
                int(api_key_id) if api_key_id is not None else None,
                (data.get("type") or "").strip() or None,
                (data.get("filename") or "").strip() or None,
                (data.get("mime_type") or "").strip() or None,
                int(data.get("width")) if str(data.get("width") or "").isdigit() else None,
                int(data.get("height")) if str(data.get("height") or "").isdigit() else None,
                (data.get("url") or "").strip() or None,
            ),
        )


def _load_media_asset(media_id: str) -> Optional[dict]:
    asset_id = (media_id or "").strip()
    if not asset_id:
        return None
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT media_id, account_id, api_key_id, media_type, filename, mime_type, width, height, source_url, created_at, updated_at
               FROM sora_media_assets
               WHERE media_id = ?
               LIMIT 1""",
            (asset_id,),
        )
        row = c.fetchone()
    if not row:
        return None
    return {
        "media_id": row[0] or "",
        "account_id": int(row[1] or 0),
        "api_key_id": row[2],
        "media_type": row[3] or "",
        "filename": row[4] or "",
        "mime_type": row[5] or "",
        "width": row[6],
        "height": row[7],
        "url": row[8] or "",
        "created_at": row[9] or "",
        "updated_at": row[10] or "",
    }


def _remember_video_task(
    task_id: str,
    account_id: int,
    api_key_id: Optional[int] = None,
    task_family: str = _TASK_FAMILY_VIDEO_GEN,
    raw_status: str = "",
    normalized_status: str = "",
    is_active: Optional[bool] = True,
) -> None:
    task = (task_id or "").strip()
    if not task or not account_id:
        return
    raw_value = (raw_status or "").strip() or None
    normalized_value = _normalize_video_status(normalized_status or raw_status) or None
    task_family_value = _normalize_task_family(task_family)
    active_value = None if is_active is None else (1 if is_active else 0)
    succeeded_value = "now" if normalized_value == "succeeded" else ""
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO sora_video_tasks (task_id, account_id, api_key_id, task_family, raw_status, normalized_status, is_active, lease_expires_at, succeeded_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, CASE WHEN ? = 'now' THEN datetime('now') ELSE NULL END, datetime('now'), datetime('now'))
               ON CONFLICT(task_id) DO UPDATE SET
                   account_id = excluded.account_id,
                   api_key_id = COALESCE(excluded.api_key_id, sora_video_tasks.api_key_id),
                   task_family = COALESCE(excluded.task_family, sora_video_tasks.task_family),
                   raw_status = COALESCE(excluded.raw_status, sora_video_tasks.raw_status),
                   normalized_status = COALESCE(excluded.normalized_status, sora_video_tasks.normalized_status),
                   is_active = COALESCE(excluded.is_active, sora_video_tasks.is_active),
                   lease_expires_at = NULL,
                   succeeded_at = CASE
                       WHEN sora_video_tasks.succeeded_at IS NOT NULL THEN sora_video_tasks.succeeded_at
                       WHEN COALESCE(excluded.normalized_status, sora_video_tasks.normalized_status) = 'succeeded' THEN datetime('now')
                       ELSE sora_video_tasks.succeeded_at
                   END,
                   updated_at = datetime('now')""",
            (
                task,
                int(account_id),
                int(api_key_id) if api_key_id is not None else None,
                task_family_value,
                raw_value,
                normalized_value,
                active_value,
                succeeded_value,
            ),
        )


def _claim_reserved_video_task(
    reservation_task_id: str,
    task_id: str,
    account_id: int,
    api_key_id: Optional[int] = None,
    task_family: str = _TASK_FAMILY_VIDEO_GEN,
    raw_status: str = "",
    normalized_status: str = "",
    is_active: Optional[bool] = True,
) -> None:
    reservation = (reservation_task_id or "").strip()
    task = (task_id or "").strip()
    if not task or not account_id:
        return
    raw_value = (raw_status or "").strip() or None
    normalized_value = _normalize_video_status(normalized_status or raw_status) or None
    task_family_value = _normalize_task_family(task_family)
    active_value = None if is_active is None else (1 if is_active else 0)
    succeeded_value = "now" if normalized_value == "succeeded" else ""
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        if reservation:
            c.execute("DELETE FROM sora_video_tasks WHERE task_id = ?", (reservation,))
        c.execute(
            """INSERT INTO sora_video_tasks (task_id, account_id, api_key_id, task_family, raw_status, normalized_status, is_active, lease_expires_at, succeeded_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, CASE WHEN ? = 'now' THEN datetime('now') ELSE NULL END, datetime('now'), datetime('now'))
               ON CONFLICT(task_id) DO UPDATE SET
                   account_id = excluded.account_id,
                   api_key_id = COALESCE(excluded.api_key_id, sora_video_tasks.api_key_id),
                   task_family = COALESCE(excluded.task_family, sora_video_tasks.task_family),
                   raw_status = COALESCE(excluded.raw_status, sora_video_tasks.raw_status),
                   normalized_status = COALESCE(excluded.normalized_status, sora_video_tasks.normalized_status),
                   is_active = COALESCE(excluded.is_active, sora_video_tasks.is_active),
                   lease_expires_at = NULL,
                   succeeded_at = CASE
                       WHEN sora_video_tasks.succeeded_at IS NOT NULL THEN sora_video_tasks.succeeded_at
                       WHEN COALESCE(excluded.normalized_status, sora_video_tasks.normalized_status) = 'succeeded' THEN datetime('now')
                       ELSE sora_video_tasks.succeeded_at
                   END,
                   updated_at = datetime('now')""",
            (
                task,
                int(account_id),
                int(api_key_id) if api_key_id is not None else None,
                task_family_value,
                raw_value,
                normalized_value,
                active_value,
                succeeded_value,
            ),
        )


def _release_video_task_reservation(reservation_task_id: str) -> None:
    reservation = (reservation_task_id or "").strip()
    if not reservation:
        return
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM sora_video_tasks WHERE task_id = ?", (reservation,))


def _sync_video_task_result(
    task_id: str,
    account_id: int,
    result: Optional[dict],
    api_key_id: Optional[int] = None,
    default_active: Optional[bool] = None,
    task_family: str = "",
) -> None:
    task = (task_id or "").strip()
    if not task or not account_id:
        return
    raw_status = ((result or {}).get("status") or "").strip()
    normalized_status = _normalize_video_status((result or {}).get("normalized_status") or raw_status)
    if normalized_status:
        is_active = normalized_status not in _VIDEO_TERMINAL_STATUSES
    else:
        is_active = default_active
    _remember_video_task(
        task,
        account_id,
        api_key_id=api_key_id,
        task_family=task_family or (result or {}).get("task_family") or _TASK_FAMILY_VIDEO_GEN,
        raw_status=raw_status,
        normalized_status=normalized_status,
        is_active=is_active,
    )


def _reserve_pool_video_account(
    api_key_id: Optional[int] = None,
    exclude_ids: list = None,
    task_family: str = _TASK_FAMILY_VIDEO_GEN,
) -> Optional[dict]:
    exclude = {int(x) for x in (exclude_ids or []) if int(x)}
    task_family_value = _normalize_task_family(task_family)
    init_db()
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        c = conn.cursor()
        c.execute(
            "DELETE FROM sora_video_tasks WHERE task_id LIKE ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= datetime('now')",
            (f"{_VIDEO_RESERVATION_PREFIX}%",),
        )
        c.execute("SELECT value FROM system_settings WHERE key = 'sora_auto_rotate_cursor'")
        row = c.fetchone()
        try:
            cursor = int((row[0] if row else "0") or "0")
        except Exception:
            cursor = 0
        c.execute(
            """SELECT a.id, a.email, a.refresh_token, a.access_token, a.proxy, a.has_sora,
                      COALESCE(a.sora_enabled, 1) AS sora_enabled,
                      COALESCE(a.sora_quota_exhausted, 0) AS sora_quota_exhausted,
                      COALESCE(a.sora_quota_note, '') AS sora_quota_note,
                      COALESCE(a.sora_quota_updated_at, '') AS sora_quota_updated_at,
                      COALESCE(t.active_count, 0) AS active_count
               FROM accounts a
               LEFT JOIN (
                   SELECT account_id, COUNT(*) AS active_count
                   FROM sora_video_tasks
                   WHERE is_active = 1
                     AND (lease_expires_at IS NULL OR lease_expires_at > datetime('now'))
                   GROUP BY account_id
               ) t ON t.account_id = a.id
               WHERE a.has_sora = 1
                 AND COALESCE(a.sora_enabled, 1) = 1
                 AND COALESCE(a.sora_quota_exhausted, 0) = 0
                 AND (COALESCE(a.refresh_token, '') != '' OR COALESCE(a.access_token, '') != '')
               ORDER BY a.id ASC"""
        )
        rows = [r for r in c.fetchall() if int(r["id"]) not in exclude]
        if not rows:
            return None
        min_active = min(int(r["active_count"] or 0) for r in rows)
        candidates = [r for r in rows if int(r["active_count"] or 0) == min_active]
        pick = None
        for r in candidates:
            if int(r["id"]) > cursor:
                pick = r
                break
        if pick is None:
            pick = candidates[0]
        c.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
            ("sora_auto_rotate_cursor", str(int(pick["id"]))),
        )
        reservation_task_id = f"{_VIDEO_RESERVATION_PREFIX}{uuid.uuid4().hex}"
        c.execute(
            """INSERT INTO sora_video_tasks
               (task_id, account_id, api_key_id, task_family, raw_status, normalized_status, is_active, lease_expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now', ?), datetime('now'), datetime('now'))""",
            (
                reservation_task_id,
                int(pick["id"]),
                int(api_key_id) if api_key_id is not None else None,
                task_family_value,
                "reserving",
                "running",
                f"+{_VIDEO_RESERVATION_SECONDS} seconds",
            ),
        )
    return {
        "id": int(pick["id"]),
        "email": pick["email"] or "",
        "refresh_token": (pick["refresh_token"] or "").strip(),
        "access_token": (pick["access_token"] or "").strip(),
        "proxy": (pick["proxy"] or "").strip(),
        "has_sora": bool(pick["has_sora"]),
        "sora_enabled": bool(pick["sora_enabled"]),
        "sora_quota_exhausted": bool(pick["sora_quota_exhausted"]),
        "sora_quota_note": pick["sora_quota_note"] or "",
        "sora_quota_updated_at": pick["sora_quota_updated_at"] or "",
        "active_task_count": int(pick["active_count"] or 0) + 1,
        "reservation_task_id": reservation_task_id,
    }


def _lookup_video_task_meta(task_id: str) -> Optional[dict]:
    task = (task_id or "").strip()
    if not task:
        return None
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT account_id, COALESCE(task_family, ?) FROM sora_video_tasks WHERE task_id = ? LIMIT 1",
            (_TASK_FAMILY_VIDEO_GEN, task),
        )
        row = c.fetchone()
    if not row:
        return None
    try:
        account_id = int(row[0])
    except Exception:
        account_id = None
    return {
        "account_id": account_id,
        "task_family": _normalize_task_family(row[1] or _TASK_FAMILY_VIDEO_GEN),
    }


def _lookup_video_task_account(task_id: str) -> Optional[int]:
    meta = _lookup_video_task_meta(task_id)
    if not meta:
        return None
    try:
        return int(meta.get("account_id"))
    except Exception:
        return None


def _extract_quota_reason(status_code: int, payload: Any, raw_text: str = "") -> str:
    code = ""
    parts = []
    if raw_text:
        parts.append(raw_text)
    if payload is not None:
        try:
            parts.append(json.dumps(payload, ensure_ascii=False))
        except Exception:
            parts.append(str(payload))
        if isinstance(payload, dict):
            err = payload.get("error") or {}
            if isinstance(err, dict):
                code = (err.get("code") or "").strip().lower()
    merged = " ".join(parts).lower()
    if code == "too_many_concurrent_tasks":
        return ""
    if not merged and status_code not in (402, 429):
        return ""

    keywords = [
        "insufficient_quota",
        "quota_exceeded",
        "billing_hard_limit_reached",
        "out of credits",
        "insufficient credits",
        "rate_limit_exceeded",
        "usage limit",
        "credit balance",
    ]
    if code in keywords:
        return code
    if status_code == 402:
        return f"http_{status_code}"
    if status_code == 429 and not any(k in merged for k in keywords):
        return ""
    for k in keywords:
        if k in merged:
            return k
    return ""


def _extract_sora_error_code(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error") or {}
    if isinstance(error, dict):
        return (error.get("code") or "").strip().lower()
    return ""


def _is_too_many_concurrent_tasks_result(result: Optional[dict]) -> bool:
    payload = (result or {}).get("data")
    code = _extract_sora_error_code(payload)
    if code == "too_many_concurrent_tasks":
        return True
    text = ""
    try:
        text = json.dumps(payload, ensure_ascii=False).lower()
    except Exception:
        text = str(payload or "").lower()
    return "too_many_concurrent_tasks" in text


def _extract_busy_reason(payload: Any, raw_text: str = "") -> str:
    code = ""
    message = ""
    if isinstance(payload, dict):
        err = payload.get("error") or {}
        if isinstance(err, dict):
            code = (err.get("code") or "").strip().lower()
            message = (err.get("message") or "").strip().lower()
    merged = " ".join(
        part for part in [raw_text.lower() if raw_text else "", message] if part
    )
    if code == "too_many_concurrent_tasks":
        return code
    if "generations in progress" in merged:
        return "too_many_concurrent_tasks"
    return ""


class SoraUpstreamTransportError(RuntimeError):
    pass


_TRANSPORT_ERROR_HINTS = (
    "connect tunnel failed",
    "proxyerror",
    "proxy error",
    "connection refused",
    "connection reset",
    "failed to connect",
    "curl: (7)",
    "curl: (35)",
    "curl: (56)",
)
_TRANSPORT_RETRY_COUNT = 2


def _extract_transport_error_message(exc: Exception) -> str:
    if exc is None:
        return ""
    message = (str(exc) or exc.__class__.__name__ or "").strip()
    if not message:
        return ""
    lowered = message.lower()
    if isinstance(exc, (requests.exceptions.ProxyError, requests.exceptions.ConnectionError)):
        return message[:400]
    if any(token in lowered for token in _TRANSPORT_ERROR_HINTS):
        return message[:400]
    module_name = (exc.__class__.__module__ or "").lower()
    class_name = (exc.__class__.__name__ or "").lower()
    if "curl_cffi" in module_name and class_name in {"proxyerror", "connectionerror"}:
        return message[:400]
    return ""


def _raise_transport_http_error(detail: str, all_accounts: bool = False) -> None:
    prefix = "所有可用账号代理或网络异常" if all_accounts else "Sora 上游代理或网络异常"
    suffix = f"：{detail}" if detail else ""
    raise HTTPException(status_code=503, detail=f"{prefix}{suffix}")


def _run_transport_safe_request(fn):
    last_detail = ""
    for attempt in range(max(1, _TRANSPORT_RETRY_COUNT + 1)):
        try:
            return fn()
        except Exception as exc:
            detail = _extract_transport_error_message(exc)
            if not detail:
                raise
            last_detail = detail
            if attempt >= _TRANSPORT_RETRY_COUNT:
                raise SoraUpstreamTransportError(last_detail) from exc
            time.sleep(0.35 * attempt)
    raise SoraUpstreamTransportError(last_detail or "unknown transport error")


class SoraTokenBody(BaseModel):
    account_id: Optional[int] = None
    access_token: str = ""
    refresh_token: str = ""
    proxy_url: str = ""


class SoraRequestBody(SoraTokenBody):
    method: str = "GET"
    path: str = "/backend/me"
    payload: Dict[str, Any] = Field(default_factory=dict)


class SoraVideoGenCreateBody(SoraTokenBody):
    prompt: str
    auto_rotate: bool = False
    task_family: str = ""
    operation: str = "simple_compose"
    n_variants: int = 4
    n_frames: int = 300
    resolution: int = 360
    orientation: str = "wide"
    model: str = ""
    style_id: str = ""
    audio_caption: str = ""
    audio_transcript: str = ""
    video_caption: str = ""
    seed: Optional[int] = None
    source_image_media_id: str = ""
    extra_payload: Dict[str, Any] = Field(default_factory=dict)


class SoraVideoTaskBody(SoraTokenBody):
    task_id: str


class SoraVideoListBody(SoraTokenBody):
    limit: int = 20
    last_id: str = ""
    task_type_filter: str = "videos"


class SoraVideoGenCreateAndWaitBody(SoraVideoGenCreateBody):
    poll_interval_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    timeout_seconds: int = Field(default=900, ge=5, le=7200)


class SoraVideoGenNfCreateBody(SoraTokenBody):
    prompt: str
    auto_rotate: bool = False
    n_variants: int = 1
    n_frames: int = 300
    resolution: int = 360
    orientation: str = "portrait"
    model: str = "sy_8"
    style_id: str = ""
    audio_caption: str = ""
    audio_transcript: str = ""
    video_caption: str = ""
    seed: Optional[int] = None
    extra_payload: Dict[str, Any] = Field(default_factory=dict)


class SoraDraftBody(SoraTokenBody):
    draft_id: str


class SoraStitchBody(SoraTokenBody):
    generation_ids: list[str] = Field(default_factory=list)
    for_download: bool = False


_VIDEO_SUCCESS_STATUSES = {"succeeded"}
_VIDEO_FAILURE_STATUSES = {"failed", "cancelled", "rejected", "expired", "error"}
_VIDEO_TERMINAL_STATUSES = _VIDEO_SUCCESS_STATUSES | _VIDEO_FAILURE_STATUSES
_VIDEO_RETRYABLE_POLL_STATUS_CODES = {404, 409, 425}
_VIDEO_RESERVATION_PREFIX = "lease_"
_VIDEO_RESERVATION_SECONDS = 180
_VIDEO_URL_KEYS = {
    "url",
    "src",
    "uri",
    "download_url",
    "downloadurl",
    "signed_url",
    "signedurl",
    "stream_url",
    "streamurl",
    "video_url",
    "videourl",
    "playback_url",
    "playbackurl",
}
_VIDEO_URL_EXTENSIONS = (".mp4", ".mov", ".webm", ".m3u8")


def _normalize_video_status(status: str) -> str:
    value = (status or "").strip().lower()
    if not value:
        return ""
    aliases = {
        "complete": "succeeded",
        "completed": "succeeded",
        "done": "succeeded",
        "success": "succeeded",
        "succeed": "succeeded",
        "succeeded": "succeeded",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "in_progress": "running",
        "inprogress": "running",
        "processing": "running",
    }
    return aliases.get(value, value)


def _find_string_field(payload: Any, keys: tuple[str, ...], depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _find_string_field(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload[:50]:
            found = _find_string_field(item, keys, depth + 1)
            if found:
                return found
    return ""


def _video_url_priority(url: str) -> tuple[int, int, int, int]:
    value = (url or "").strip()
    lowered = value.lower()
    decoded = unquote(lowered)
    base = decoded.split("?", 1)[0]
    manifest_penalty = 1 if base.endswith(".m3u8") else 0
    extension_rank = 0
    if base.endswith(".mov"):
        extension_rank = 1
    elif base.endswith(".webm"):
        extension_rank = 2
    elif base.endswith(".m3u8"):
        extension_rank = 3
    quality_rank = 50
    quality_checks = (
        (0, ("no_watermark", "downloadable", "/src.mp4", "/source.mp4", "/source_wm.mp4", "original")),
        (1, ("/hd.mp4", "_hd.mp4", "/high.mp4", "_high.mp4")),
        (2, ("/md.mp4", "_md.mp4", "/medium.mp4", "_medium.mp4")),
        (3, ("/ld.mp4", "_ld.mp4", "/low.mp4", "_low.mp4")),
        (4, ("watermark", "_wm.mp4", "/wm.mp4")),
    )
    for rank, needles in quality_checks:
        if any(needle in decoded for needle in needles):
            quality_rank = rank
            break
    watermark_penalty = 0
    if (
        "watermark" in decoded
        or "_wm." in base
        or "/wm." in base
        or "/wm/" in base
    ) and "no_watermark" not in decoded:
        watermark_penalty = 1
    return (manifest_penalty, quality_rank, watermark_penalty, extension_rank)


def _merge_video_urls(*groups: Any) -> list[str]:
    ranked: list[tuple[tuple[int, int, int, int], int, str]] = []
    seen: set[str] = set()
    sequence = 0
    for group in groups:
        if isinstance(group, str):
            candidates = [group]
        elif isinstance(group, (list, tuple, set)):
            candidates = list(group)
        else:
            candidates = []
        for item in candidates:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            ranked.append((_video_url_priority(value), sequence, value))
            sequence += 1
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def _collect_video_urls(payload: Any, depth: int = 0, urls: Optional[list[str]] = None) -> list[str]:
    if urls is None:
        urls = []
    if depth > 6:
        return urls
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered_key = str(key or "").strip().lower()
            if isinstance(value, str):
                candidate = value.strip()
                lower_candidate = candidate.lower()
                if candidate.startswith(("http://", "https://")):
                    base_url = lower_candidate.split("?", 1)[0]
                    if lowered_key in _VIDEO_URL_KEYS or base_url.endswith(_VIDEO_URL_EXTENSIONS):
                        if candidate not in urls:
                            urls.append(candidate)
            elif isinstance(value, (dict, list)):
                _collect_video_urls(value, depth + 1, urls)
    elif isinstance(payload, list):
        for item in payload[:50]:
            _collect_video_urls(item, depth + 1, urls)
    return urls


def _decorate_video_task_result(result: dict, task_id: str = "") -> dict:
    payload = result.get("data")
    raw_status = _find_string_field(payload, ("status", "state"))
    normalized_status = _normalize_video_status(raw_status)
    resolved_task_id = (task_id or _find_string_field(payload, ("task_id", "id"))).strip()
    video_urls = _merge_video_urls(_collect_video_urls(payload))
    return {
        **result,
        "task_family": _TASK_FAMILY_VIDEO_GEN,
        "task_id": resolved_task_id,
        "status": raw_status,
        "normalized_status": normalized_status,
        "is_terminal": normalized_status in _VIDEO_TERMINAL_STATUSES,
        "is_success": normalized_status in _VIDEO_SUCCESS_STATUSES,
        "video_urls": video_urls,
    }


def _find_dict_matching(payload: Any, predicate, depth: int = 0):
    if depth > 8:
        return None
    if isinstance(payload, dict):
        try:
            if predicate(payload):
                return payload
        except Exception:
            pass
        for value in payload.values():
            found = _find_dict_matching(value, predicate, depth + 1)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload[:80]:
            found = _find_dict_matching(item, predicate, depth + 1)
            if found is not None:
                return found
    return None


def _extract_nf2_task_id(payload: Any) -> str:
    if isinstance(payload, dict):
        task = payload.get("task")
        if isinstance(task, dict):
            task_id = (task.get("id") or task.get("task_id") or "").strip()
            if task_id:
                return task_id
        top_id = (payload.get("task_id") or "").strip()
        if top_id:
            return top_id
        kind = (payload.get("kind") or "").strip().lower()
        status = (payload.get("status") or payload.get("state") or "").strip()
        top_level_id = (payload.get("id") or "").strip()
        if top_level_id and status and kind != "sora_draft":
            return top_level_id
    nested = _find_dict_matching(
        payload,
        lambda item: isinstance(item.get("id"), str)
        and isinstance(item.get("status"), str)
        and (item.get("kind") or "").strip().lower() != "sora_draft",
    )
    if isinstance(nested, dict):
        return (nested.get("id") or "").strip()
    return _find_string_field(payload, ("task_id",))


def _extract_nf2_draft_id(payload: Any) -> str:
    if isinstance(payload, dict):
        draft = payload.get("draft")
        if isinstance(draft, dict):
            draft_id = (draft.get("id") or "").strip()
            if draft_id:
                return draft_id
        kind = (payload.get("kind") or "").strip().lower()
        top_level_id = (payload.get("id") or "").strip()
        if kind == "sora_draft" and top_level_id:
            return top_level_id
    nested = _find_dict_matching(
        payload,
        lambda item: ((item.get("kind") or "").strip().lower() == "sora_draft") and isinstance(item.get("id"), str),
    )
    if isinstance(nested, dict):
        return (nested.get("id") or "").strip()
    return ""


def _extract_nf2_download_urls(payload: Any) -> dict:
    watermark_url = _find_string_field(payload, ("watermark",))
    no_watermark_url = _find_string_field(payload, ("no_watermark",))
    downloadable_url = _find_string_field(payload, ("downloadable_url",))
    urls = _merge_video_urls(
        [no_watermark_url, downloadable_url, watermark_url],
        _collect_video_urls(payload),
    )
    return {
        "watermark_url": watermark_url,
        "no_watermark_url": no_watermark_url,
        "downloadable_url": downloadable_url,
        "media_urls": urls,
        "video_urls": urls,
    }


def _decorate_nf2_result(result: dict, task_id: str = "") -> dict:
    payload = result.get("data")
    raw_status = _find_string_field(payload, ("status", "state"))
    normalized_status = _normalize_video_status(raw_status)
    resolved_task_id = (task_id or _extract_nf2_task_id(payload)).strip()
    draft_id = _extract_nf2_draft_id(payload)
    download_info = _extract_nf2_download_urls(payload)
    return {
        **result,
        "task_family": _TASK_FAMILY_NF2,
        "task_id": resolved_task_id,
        "draft_id": draft_id,
        "status": raw_status,
        "normalized_status": normalized_status,
        "is_terminal": normalized_status in _VIDEO_TERMINAL_STATUSES,
        "is_success": normalized_status in _VIDEO_SUCCESS_STATUSES,
        **download_info,
    }


def _merge_nf2_lookup_result(task_result: dict, draft_result: dict) -> dict:
    merged = {**task_result}
    for key in ("draft_id", "no_watermark_url", "downloadable_url", "watermark_url"):
        if draft_result.get(key):
            merged[key] = draft_result.get(key)
    merged_urls = _merge_video_urls(
        [
            draft_result.get("no_watermark_url"),
            draft_result.get("downloadable_url"),
            draft_result.get("watermark_url"),
        ],
        draft_result.get("video_urls") or [],
        [
            task_result.get("no_watermark_url"),
            task_result.get("downloadable_url"),
            task_result.get("watermark_url"),
        ],
        task_result.get("video_urls") or [],
    )
    if merged_urls:
        merged["video_urls"] = merged_urls
        merged["media_urls"] = merged_urls
    return merged


def _is_pool_api_key_caller(caller: dict) -> bool:
    return (caller.get("auth_type") or "") == "api_key" and caller.get("account_id") is None


def _require_api_key_video_scope(caller: dict, capability: str) -> None:
    if (caller.get("auth_type") or "") != "api_key":
        return
    scope = caller.get("api_key_scope") or SORA_API_KEY_SCOPE_TEXT
    if sora_api_key_scope_allows(scope, capability):
        return
    target = "文生视频" if capability == SORA_API_KEY_SCOPE_TEXT else "图生视频"
    raise HTTPException(
        status_code=403,
        detail=f"当前 API Key 类型是「{sora_api_key_scope_label(scope)}」，不能调用{target}接口",
    )


def _require_api_key_any_video_scope(caller: dict) -> None:
    if (caller.get("auth_type") or "") != "api_key":
        return
    scope = caller.get("api_key_scope") or SORA_API_KEY_SCOPE_TEXT
    if scope in (SORA_API_KEY_SCOPE_TEXT, SORA_API_KEY_SCOPE_IMAGE) or sora_api_key_scope_allows(scope, SORA_API_KEY_SCOPE_TEXT):
        return
    raise HTTPException(status_code=403, detail="当前 API Key 不能调用视频接口")


def _payload_is_image_to_video(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    media_id = (payload.get("source_image_media_id") or "").strip()
    if media_id:
        return True
    if bool(payload.get("is_storyboard")) and isinstance(payload.get("inpaint_items"), list) and payload.get("inpaint_items"):
        return True
    for item in payload.get("inpaint_items") or []:
        if not isinstance(item, dict):
            continue
        if (item.get("upload_media_id") or item.get("uploaded_file_id") or item.get("generation_id") or "").strip():
            return True
    return False


def _resolve_tokens(
    body: SoraTokenBody,
    allow_refresh: bool = True,
    prefer_refresh_token_for_sora: bool = False,
    default_account_id: Optional[int] = None,
    locked_account_id: Optional[int] = None,
    allow_direct_tokens: bool = True,
    allow_pool_rotation: bool = False,
    exclude_account_ids: list = None,
) -> dict:
    account = None
    request_account_id = body.account_id
    if locked_account_id is not None:
        if request_account_id is not None and int(request_account_id) != int(locked_account_id):
            raise HTTPException(status_code=403, detail="API Key 仅允许访问绑定账号")
        request_account_id = locked_account_id
    elif request_account_id is None:
        request_account_id = default_account_id

    access_token = (body.access_token or "").strip() if allow_direct_tokens else ""
    refresh_token = (body.refresh_token or "").strip() if allow_direct_tokens else ""
    id_token = ""
    proxy_url = (body.proxy_url or "").strip() if allow_direct_tokens else ""

    # 池模式自动选账号
    if request_account_id is None and allow_pool_rotation and not access_token and not refresh_token:
        picked = _pick_next_available_account(exclude_ids=exclude_account_ids)
        if not picked:
            raise HTTPException(status_code=404, detail="账号池中无可用 Sora 账号（请检查 token/额度/启停状态）")
        account = picked
        request_account_id = picked["id"]
        access_token = picked["access_token"]
        refresh_token = picked["refresh_token"]
        proxy_url = picked["proxy"]
    elif request_account_id is not None:
        account = _load_account(int(request_account_id))
        if not account:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not account["sora_enabled"]:
            raise HTTPException(status_code=403, detail="该账号已停用，请在账号管理中启用后再调用")
        if account["sora_quota_exhausted"]:
            # 池模式下遇到额度不足不直接报错，而是跳过该账号
            if allow_pool_rotation:
                excl = list(exclude_account_ids or []) + [int(request_account_id)]
                picked = _pick_next_available_account(exclude_ids=excl)
                if not picked:
                    raise HTTPException(status_code=429, detail="所有账号额度已耗尽，请添加新账号或重置额度")
                account = picked
                request_account_id = picked["id"]
                access_token = picked["access_token"]
                refresh_token = picked["refresh_token"]
                proxy_url = picked["proxy"]
            else:
                note = account["sora_quota_note"] or "quota_exceeded"
                when = account["sora_quota_updated_at"] or ""
                suffix = f"（{when}）" if when else ""
                raise HTTPException(status_code=429, detail=f"该账号已标记额度不足{suffix}：{note}，请切换账号或重置额度状态")
        if not access_token:
            access_token = account["access_token"]
        if not refresh_token:
            refresh_token = account["refresh_token"]
        if not proxy_url:
            proxy_url = account["proxy"]

    if refresh_token and allow_refresh and (prefer_refresh_token_for_sora or not access_token):
        sora_phone = _import_sora_phone()
        out = sora_phone.rt_to_at_mobile(refresh_token, proxy_url=proxy_url)
        new_access_token = (out.get("access_token") or "").strip()
        new_rt = (out.get("refresh_token") or "").strip()
        new_id_token = (out.get("id_token") or "").strip()
        if new_access_token:
            access_token = new_access_token
        if new_rt:
            refresh_token = new_rt
        if new_id_token:
            id_token = new_id_token
        if request_account_id is not None and (new_access_token or new_rt or new_id_token):
            _save_account_tokens(
                int(request_account_id),
                access_token=new_access_token,
                refresh_token=new_rt,
                id_token=new_id_token,
            )

    return {
        "account": account,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "proxy_url": proxy_url,
    }


def _ensure_nf2_access_token(
    data: dict,
    account_id: Optional[int] = None,
    force_web_login: bool = False,
) -> dict:
    sora_phone = _import_sora_phone()
    current = dict(data or {})
    access_token = (current.get("access_token") or "").strip()
    web_session = current.get("web_session")
    account = current.get("account")
    if account is None and account_id is not None:
        account = _load_account(int(account_id))
        current["account"] = account
    if (
        web_session is not None
        and access_token
        and sora_phone.is_chatgpt_web_access_token(access_token)
        and not force_web_login
    ):
        return current
    if account is not None and force_web_login:
        _drop_nf2_web_session(account.get("id"))
        current["web_session"] = None
    if account is not None and not force_web_login:
        cached = _get_nf2_web_session(account.get("id"))
        if cached:
            cached_session = cached.get("web_session")
            cached_access_token = ""
            try:
                session_state = sora_phone._read_sora_web_session(cached_session)
            except Exception:
                session_state = {}
            if isinstance(session_state, dict):
                cached_access_token = (session_state.get("access_token") or "").strip()
            if not cached_access_token:
                cached_access_token = (cached.get("access_token") or "").strip()
            if cached_access_token and sora_phone.is_chatgpt_web_access_token(cached_access_token):
                cached_origin = (session_state.get("base_origin") or cached.get("web_origin") or "").strip()
                probe = sora_phone.sora_probe_nf2_session(
                    cached_access_token,
                    web_session=cached_session,
                    preferred_origin=cached_origin,
                )
                if probe.get("ok"):
                    current["access_token"] = cached_access_token
                    current["web_session"] = cached_session
                    current["web_origin"] = (probe.get("base_origin") or cached_origin or "").strip()
                    _save_account_tokens(int(account["id"]), access_token=cached_access_token)
                    _store_nf2_web_session(
                        int(account["id"]),
                        cached_session,
                        access_token=cached_access_token,
                        proxy_url=(current.get("proxy_url") or cached.get("proxy_url") or "").strip(),
                        web_origin=(current.get("web_origin") or "").strip(),
                    )
                    return current
            _drop_nf2_web_session(account.get("id"))
            current["web_session"] = None
    if access_token and sora_phone.is_chatgpt_web_access_token(access_token) and not account:
        return current
    if not account:
        return current
    browser_auth = sora_phone.sora_import_browser_web_session(
        expected_email=account.get("email") or "",
        preferred_origin=current.get("web_origin") or "",
    )
    browser_access_token = (browser_auth.get("access_token") or "").strip() if isinstance(browser_auth, dict) else ""
    browser_web_session = browser_auth.get("web_session") if isinstance(browser_auth, dict) else None
    if browser_access_token and browser_web_session is not None:
        current["access_token"] = browser_access_token
        current["web_session"] = browser_web_session
        current["web_origin"] = (browser_auth.get("base_origin") or "").strip()
        _store_nf2_web_session(
            int(account["id"]),
            browser_web_session,
            access_token=browser_access_token,
            proxy_url=(current.get("proxy_url") or "").strip(),
            web_origin=(current.get("web_origin") or "").strip(),
        )
        _save_account_tokens(int(account["id"]), access_token=browser_access_token)
        return current
    if not (account.get("email") or "").strip() or not (account.get("password") or "").strip():
        return current
    otp_fetcher = _build_account_otp_fetcher(account.get("email") or "")
    web_auth = sora_phone.sora_chatgpt_web_login(
        account.get("email") or "",
        account.get("password") or "",
        get_otp_fn=otp_fetcher,
        proxy_url=current.get("proxy_url") or "",
        return_web_session=True,
    )
    new_access_token = (web_auth.get("access_token") or "").strip()
    new_web_session = web_auth.get("web_session") if isinstance(web_auth, dict) else None
    if not new_access_token:
        if new_web_session is not None:
            _close_nf2_web_session(new_web_session)
        return current
    new_origin = (web_auth.get("base_origin") or "").strip()
    if new_web_session is not None:
        probe = sora_phone.sora_probe_nf2_session(
            new_access_token,
            web_session=new_web_session,
            preferred_origin=new_origin,
        )
        if probe.get("ok"):
            new_origin = (probe.get("base_origin") or new_origin or "").strip()
        else:
            _close_nf2_web_session(new_web_session)
            return current
    current["access_token"] = new_access_token
    current["web_session"] = new_web_session
    current["web_origin"] = new_origin
    if new_web_session is not None:
        _store_nf2_web_session(
            int(account["id"]),
            new_web_session,
            access_token=new_access_token,
            proxy_url=(current.get("proxy_url") or "").strip(),
            web_origin=(current.get("web_origin") or "").strip(),
        )
    _save_account_tokens(int(account["id"]), access_token=new_access_token)
    return current


def _candidate_nf2_origins(data: dict) -> list[str]:
    sora_phone = _import_sora_phone()
    seen = []
    for value in (
        (data.get("web_origin") or "").strip(),
        (getattr(sora_phone, "SORA_ORIGIN", "") or "").strip(),
        (getattr(sora_phone, "SORA_LEGACY_ORIGIN", "") or "").strip(),
    ):
        origin = (value or "").rstrip("/")
        if origin and origin not in seen:
            seen.append(origin)
    return seen


def _run_nf2_request_with_origin_fallback(data: dict, request_fn):
    origins = _candidate_nf2_origins(data or {})
    last_resp = None
    last_exc = None
    for index, origin in enumerate(origins):
        try:
            resp = _run_transport_safe_request(lambda: request_fn(data, origin))
        except SoraUpstreamTransportError as exc:
            last_exc = exc
            if index + 1 < len(origins):
                continue
            raise
        last_resp = resp
        status_code = int(getattr(resp, "status_code", 0) or 0)
        if index + 1 < len(origins) and status_code in (401, 403, 404):
            continue
        data["web_origin"] = origin
        return resp
    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise SoraUpstreamTransportError("nf2 request failed without response")


def _run_nf2_session_request(
    body: SoraTokenBody,
    *,
    default_account_id: Optional[int] = None,
    locked_account_id: Optional[int] = None,
    allow_pool_rotation: bool = False,
    request_fn,
    decorate_fn=None,
) -> dict:
    data = _resolve_tokens(
        body,
        allow_refresh=False,
        default_account_id=default_account_id,
        locked_account_id=locked_account_id,
        allow_pool_rotation=allow_pool_rotation,
    )
    resolved_account_id = (data.get("account") or {}).get("id")
    if resolved_account_id is None:
        resolved_account_id = default_account_id

    final_result = None
    for attempt in range(2):
        data = _ensure_nf2_access_token(
            data,
            account_id=resolved_account_id,
            force_web_login=bool(attempt),
        )
        if not (data.get("access_token") or "").strip():
            raise HTTPException(status_code=400, detail="缺少可用的 ChatGPT Web access_token")
        if resolved_account_id and data.get("web_session") is None:
            if attempt == 0:
                data["access_token"] = ""
                continue
            raise HTTPException(status_code=400, detail="无法建立可用的 ChatGPT/Sora Web session")
        try:
            resp = _run_nf2_request_with_origin_fallback(data, request_fn)
        except SoraUpstreamTransportError as exc:
            if data["account"] is not None:
                _mark_account_last_error(data["account"]["id"], f"transport error: {exc}")
            _raise_transport_http_error(str(exc), all_accounts=False)
        result = _build_account_result(resp, _parse_response_payload(resp), data)
        if int(resp.status_code or 0) == 401 and resolved_account_id and attempt == 0:
            _drop_nf2_web_session(int(resolved_account_id))
            data["access_token"] = ""
            data["web_session"] = None
            final_result = result
            continue
        if resolved_account_id and data.get("web_session") is not None:
            _touch_nf2_web_session(int(resolved_account_id), data)
        final_result = result
        break

    if callable(decorate_fn):
        return decorate_fn(final_result or {})
    return final_result or {}


def _sora_caller_rules(caller: dict) -> dict:
    auth_type = caller.get("auth_type") or "admin"
    if auth_type == "api_key":
        bound_account_id = caller.get("account_id")
        # account_id 为 None 表示池模式（创建 Key 时 account_id=0）
        if bound_account_id is None:
            return {
                "default_account_id": None,
                "locked_account_id": None,
                "allow_direct_tokens": False,
                "allow_pool_rotation": True,
                "inject_watermark_free": True,
            }
        return {
            "default_account_id": int(bound_account_id),
            "locked_account_id": int(bound_account_id),
            "allow_direct_tokens": False,
            "allow_pool_rotation": False,
            "inject_watermark_free": True,
        }
    return {
        "default_account_id": None,
        "locked_account_id": None,
        "allow_direct_tokens": True,
        "allow_pool_rotation": False,
        "inject_watermark_free": False,
    }


def _locked_sora_caller_rules(caller: dict, account_id: int) -> dict:
    rules = dict(_sora_caller_rules(caller))
    rules["default_account_id"] = int(account_id)
    rules["locked_account_id"] = int(account_id)
    rules["allow_pool_rotation"] = False
    return rules


@router.post("/rt-to-at")
def rt_to_at(body: SoraTokenBody, caller: dict = Depends(get_sora_api_caller)):
    rules = _sora_caller_rules(caller)
    resolve_keys = {k: v for k, v in rules.items() if k not in ("inject_watermark_free",)}
    data = _resolve_tokens(body, allow_refresh=True, **resolve_keys)
    if not data["access_token"]:
        if data["account"] is not None:
            _mark_account_last_error(data["account"]["id"], "RT->AT failed")
        raise HTTPException(status_code=502, detail="RT 换 AT 失败，请检查 refresh_token/代理")
    return {
        "ok": True,
        "account_id": data["account"]["id"] if data["account"] else None,
        "email": data["account"]["email"] if data["account"] else "",
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "id_token": data["id_token"],
    }


@router.post("/bootstrap")
def sora_bootstrap(body: SoraTokenBody, caller: dict = Depends(get_sora_api_caller)):
    rules = _sora_caller_rules(caller)
    resolve_keys = {k: v for k, v in rules.items() if k not in ("inject_watermark_free",)}
    data = _resolve_tokens(body, allow_refresh=True, prefer_refresh_token_for_sora=True, **resolve_keys)
    at = data["access_token"]
    if not at:
        raise HTTPException(status_code=400, detail="缺少 access_token（或 refresh_token）")
    sora_phone = _import_sora_phone()
    ok = sora_phone.sora_bootstrap(at, proxy_url=data["proxy_url"])
    if not ok:
        if data["account"] is not None:
            _mark_account_last_error(data["account"]["id"], "Sora bootstrap failed")
        raise HTTPException(status_code=502, detail="Sora bootstrap 失败")
    if data["account"] is not None:
        _clear_account_quota_exhausted(data["account"]["id"])
    return {"ok": True, "used_account_id": data["account"]["id"] if data["account"] else None}


@router.post("/me")
def sora_me(body: SoraTokenBody, caller: dict = Depends(get_sora_api_caller)):
    rules = _sora_caller_rules(caller)
    resolve_keys = {k: v for k, v in rules.items() if k not in ("inject_watermark_free",)}
    data = _resolve_tokens(body, allow_refresh=True, prefer_refresh_token_for_sora=True, **resolve_keys)
    at = data["access_token"]
    if not at:
        raise HTTPException(status_code=400, detail="缺少 access_token（或 refresh_token）")
    sora_phone = _import_sora_phone()
    me = sora_phone.sora_me(at, proxy_url=data["proxy_url"])
    if not me:
        if data["account"] is not None:
            _mark_account_last_error(data["account"]["id"], "Sora me failed")
        raise HTTPException(status_code=502, detail="Sora me 请求失败")
    if data["account"] is not None:
        _clear_account_quota_exhausted(data["account"]["id"])
    return {
        "ok": True,
        "account_id": data["account"]["id"] if data["account"] else None,
        "email": data["account"]["email"] if data["account"] else "",
        "used_account_id": data["account"]["id"] if data["account"] else None,
        "me": me,
    }


@router.post("/activate")
def sora_activate(body: SoraTokenBody, caller: dict = Depends(get_sora_api_caller)):
    rules = _sora_caller_rules(caller)
    resolve_keys = {k: v for k, v in rules.items() if k not in ("inject_watermark_free",)}
    data = _resolve_tokens(body, allow_refresh=True, prefer_refresh_token_for_sora=True, **resolve_keys)
    account = data["account"]
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
    }
    logs = []

    def _step(msg: str) -> None:
        text = (msg or "").strip()
        if text:
            logs.append(text[:500])

    sora_phone = _import_sora_phone()
    ok = False
    if account is not None:
        pr = _import_protocol_register()
        get_otp_fn = _build_account_otp_fetcher(account["email"])
        ok = pr.activate_sora(
            tokens,
            account["email"],
            proxy_url=data["proxy_url"],
            step_log_fn=_step,
            account_password=(account.get("password") or "").strip(),
            get_otp_fn=get_otp_fn,
        )
    else:
        at = tokens["access_token"]
        if not at:
            raise HTTPException(status_code=400, detail="缺少 access_token（或 refresh_token）")
        ok = sora_phone.sora_ensure_activated(at, proxy_url=data["proxy_url"], log_fn=_step)

    if not ok:
        detail = logs[-1] if logs else "Sora 激活失败"
        if account is not None:
            _mark_account_last_error(account["id"], detail)
        raise HTTPException(status_code=502, detail=detail)

    if account is not None:
        _save_account_tokens(
            account["id"],
            access_token=(tokens.get("access_token") or "").strip(),
            refresh_token=(tokens.get("refresh_token") or "").strip(),
            id_token=(tokens.get("id_token") or "").strip(),
        )

    at = (tokens.get("access_token") or "").strip()
    me = sora_phone.sora_me(at, proxy_url=data["proxy_url"]) if at else {}
    if account is not None:
        _clear_account_quota_exhausted(account["id"])
        _mark_account_sora(account["id"])
    return {
        "ok": True,
        "account_id": account["id"] if account else None,
        "email": account["email"] if account else "",
        "used_account_id": account["id"] if account else None,
        "username": (me or {}).get("username") or "",
        "me": me or {},
    }


# 最大自动重试次数（池模式下额度耗尽时自动切换账号重试）
_MAX_POOL_RETRIES = 5


def _validate_sora_request(body: SoraRequestBody) -> tuple[str, str]:
    method = (body.method or "GET").strip().upper()
    path = (body.path or "").strip()
    if method not in ("GET", "POST"):
        raise HTTPException(status_code=400, detail="method 仅支持 GET/POST")
    if not path.startswith("/backend/"):
        raise HTTPException(status_code=400, detail="path 仅允许 /backend/*")
    return method, path


def _is_video_gen_create_request(method: str, path: str) -> bool:
    return method == "POST" and path.rstrip("/") == "/backend/video_gen"


def _do_sora_request(body: SoraRequestBody, data: dict, inject_watermark_free: bool = False):
    """执行单次 Sora 后端请求，返回 (response, payload, quota_reason)。"""
    at = data["access_token"]
    method, path = _validate_sora_request(body)

    sora_phone = _import_sora_phone()
    url = f"{sora_phone.SORA_ORIGIN}{path}"
    device_id = None
    if method == "POST":
        device_id = sora_phone.uuid.uuid4()
    headers = sora_phone._build_headers(at, device_id=str(device_id) if device_id else None)

    # API Key 调用注入去水印 header
    if inject_watermark_free:
        headers["x-sora-watermark"] = "disabled"

    if _is_video_gen_create_request(method, path):
        sentinel = sora_phone._build_sentinel_header(
            headers.get("oai-device-id") or str(device_id),
            "sora_create_task",
            proxy_url=data["proxy_url"],
        )
        if sentinel:
            headers["openai-sentinel-token"] = sentinel

    if method == "GET":
        r = sora_phone._session_get(url, headers=headers, proxy_url=data["proxy_url"])
    else:
        payload = body.payload or {}
        if _is_video_gen_create_request(method, path):
            payload = sora_phone._strip_nullish(payload)
        r = sora_phone._session_post(
            url,
            headers=headers,
            json=payload,
            proxy_url=data["proxy_url"],
        )
    try:
        payload = r.json()
    except Exception:
        payload = {"text": (r.text or "")[:1000]}

    quota_reason = _extract_quota_reason(r.status_code, payload, r.text or "")
    return r, payload, quota_reason


def _run_sora_request(body: SoraRequestBody, caller: dict, rules_override: Optional[dict] = None) -> dict:
    rules = dict(rules_override) if rules_override is not None else _sora_caller_rules(caller)
    inject_watermark_free = rules.pop("inject_watermark_free", False)
    allow_pool = rules.get("allow_pool_rotation", False)
    data = _resolve_tokens(body, allow_refresh=True, prefer_refresh_token_for_sora=True, **rules)
    at = data["access_token"]
    if not at:
        raise HTTPException(status_code=400, detail="缺少 access_token（或 refresh_token）")

    _validate_sora_request(body)

    try:
        r, payload, quota_reason = _run_transport_safe_request(
            lambda: _do_sora_request(body, data, inject_watermark_free=inject_watermark_free)
        )
    except SoraUpstreamTransportError as exc:
        if data["account"] is not None:
            _mark_account_last_error(data["account"]["id"], f"transport error: {exc}")
        _raise_transport_http_error(str(exc), all_accounts=False)

    if quota_reason:
        tried_ids = []
        if data["account"] is not None:
            _mark_account_quota_exhausted(data["account"]["id"], quota_reason)
            tried_ids.append(data["account"]["id"])

        if allow_pool:
            for _ in range(_MAX_POOL_RETRIES):
                next_account = _pick_next_available_account(exclude_ids=tried_ids)
                if not next_account:
                    break
                try:
                    next_data = _resolve_tokens(
                        SoraTokenBody(account_id=next_account["id"]),
                        allow_refresh=True,
                        prefer_refresh_token_for_sora=True,
                        default_account_id=next_account["id"],
                        locked_account_id=next_account["id"],
                        allow_direct_tokens=False,
                    )
                except Exception:
                    tried_ids.append(next_account["id"])
                    continue
                if not next_data["access_token"]:
                    tried_ids.append(next_account["id"])
                    continue
                try:
                    r2, payload2, quota_reason2 = _run_transport_safe_request(
                        lambda: _do_sora_request(body, next_data, inject_watermark_free=inject_watermark_free)
                    )
                except SoraUpstreamTransportError as exc:
                    _mark_account_last_error(next_account["id"], f"transport error: {exc}")
                    tried_ids.append(next_account["id"])
                    continue
                if quota_reason2:
                    _mark_account_quota_exhausted(next_account["id"], quota_reason2)
                    tried_ids.append(next_account["id"])
                    continue
                data = next_data
                r, payload, quota_reason = r2, payload2, quota_reason2
                break

        if quota_reason:
            raise HTTPException(
                status_code=429,
                detail=f"{'所有' if allow_pool else ''}账号额度不足，已自动标记不可用：{quota_reason}"
            )

    if 200 <= r.status_code < 300:
        if data["account"] is not None:
            _clear_account_quota_exhausted(data["account"]["id"])
    else:
        if data["account"] is not None:
            _mark_account_last_error(data["account"]["id"], f"HTTP {r.status_code}")
    return {
        "ok": 200 <= r.status_code < 300,
        "status_code": r.status_code,
        "data": payload,
        "used_account_id": data["account"]["id"] if data["account"] else None,
        "used_email": data["account"]["email"] if data["account"] else "",
    }


def _parse_response_payload(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"text": (resp.text or "")[:1000]}


def _build_account_result(resp, payload: Any, data: dict) -> dict:
    if 200 <= resp.status_code < 300:
        if data["account"] is not None:
            _clear_account_quota_exhausted(data["account"]["id"])
    else:
        if data["account"] is not None:
            _mark_account_last_error(data["account"]["id"], f"HTTP {resp.status_code}")
    return {
        "ok": 200 <= resp.status_code < 300,
        "status_code": resp.status_code,
        "data": payload,
        "used_account_id": data["account"]["id"] if data["account"] else None,
        "used_email": data["account"]["email"] if data["account"] else "",
    }


def _run_nf2_create_request(data: dict, body: SoraVideoGenNfCreateBody, payload: dict) -> tuple[dict, str]:
    sora_phone = _import_sora_phone()
    def _request(current: dict, origin: str):
        device_id = str(sora_phone.uuid.uuid4())
        headers = sora_phone._build_sora_web_headers(
            current["access_token"],
            device_id=device_id,
            origin=origin,
        )
        sentinel = sora_phone._build_sentinel_header(
            device_id,
            "sora_2_create_task",
            proxy_url=current["proxy_url"],
        )
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        path = "/backend/nf/bulk_create" if int((payload or {}).get("nsamples") or 1) > 1 else "/backend/nf/create"
        return sora_phone._web_session_json_post(
            f"{origin}{path}",
            headers=headers,
            json=sora_phone._strip_nullish(payload),
            proxy_url=current["proxy_url"],
            web_session=current.get("web_session"),
        )

    try:
        resp = _run_nf2_request_with_origin_fallback(data, _request)
    except SoraUpstreamTransportError as exc:
        if data["account"] is not None:
            _mark_account_last_error(data["account"]["id"], f"transport error: {exc}")
        _raise_transport_http_error(str(exc), all_accounts=False)
    parsed = _parse_response_payload(resp)
    quota_reason = _extract_quota_reason(resp.status_code, parsed, getattr(resp, "text", "") or "")
    return _build_account_result(resp, parsed, data), quota_reason


def _run_nf2_task_lookup(task_id: str, body: SoraTokenBody, caller: dict) -> dict:
    sora_phone = _import_sora_phone()
    account_id = body.account_id or _lookup_video_task_account(task_id)
    rules_override = _locked_sora_caller_rules(caller, account_id) if account_id and _is_pool_api_key_caller(caller) else None
    decorated = _run_nf2_session_request(
        body,
        default_account_id=account_id,
        locked_account_id=account_id if account_id and rules_override else None,
        allow_pool_rotation=False,
        request_fn=lambda data, origin: sora_phone.sora_nf2_get_task(
            data["access_token"],
            task_id,
            proxy_url=data["proxy_url"],
            web_session=data.get("web_session"),
            base_origin=origin,
        ),
        decorate_fn=lambda result: _decorate_nf2_result(result, task_id=task_id),
    )
    draft_id = (decorated.get("draft_id") or "").strip()
    if decorated.get("ok") and decorated.get("is_success") and draft_id:
        draft_result = _run_nf2_session_request(
            SoraDraftBody(
                account_id=body.account_id,
                access_token=body.access_token,
                refresh_token=body.refresh_token,
                proxy_url=body.proxy_url,
                draft_id=draft_id,
            ),
            default_account_id=account_id,
            locked_account_id=account_id if account_id and rules_override else None,
            allow_pool_rotation=False,
            request_fn=lambda data, origin: sora_phone.sora_nf2_get_draft(
                data["access_token"],
                draft_id,
                proxy_url=data["proxy_url"],
                web_session=data.get("web_session"),
                base_origin=origin,
            ),
            decorate_fn=_decorate_nf2_result,
        )
        if draft_result.get("ok"):
            decorated = _merge_nf2_lookup_result(decorated, draft_result)
    if account_id:
        _sync_video_task_result(
            task_id,
            int(account_id),
            decorated,
            caller.get("api_key_id"),
            default_active=None,
            task_family=_TASK_FAMILY_NF2,
        )
    return decorated


@router.post("/request")
def sora_request(body: SoraRequestBody, caller: dict = Depends(get_sora_api_caller)):
    _, path = _validate_sora_request(body)
    if path.startswith("/backend/video_gen"):
        if path.rstrip("/") == "/backend/video_gen" and (body.method or "GET").strip().upper() == "POST":
            _require_api_key_video_scope(
                caller,
                SORA_API_KEY_SCOPE_IMAGE if _payload_is_image_to_video(body.payload or {}) else SORA_API_KEY_SCOPE_TEXT,
            )
        else:
            _require_api_key_any_video_scope(caller)
    return _run_sora_request(body, caller)


def _build_video_gen_list_path(limit: int = 20, last_id: str = "", task_type_filter: str = "videos") -> str:
    params = {
        "limit": max(1, min(int(limit or 20), 100)),
    }
    if (last_id or "").strip():
        params["last_id"] = (last_id or "").strip()
    if (task_type_filter or "").strip():
        params["task_type_filters"] = (task_type_filter or "").strip()
    return f"/backend/video_gen?{urlencode(params)}"


def _run_video_task_lookup(task_id: str, body: SoraTokenBody, caller: dict) -> dict:
    meta = _lookup_video_task_meta(task_id)
    task_family = _normalize_task_family((meta or {}).get("task_family") or "")
    if task_family == _TASK_FAMILY_NF2:
        return _run_nf2_task_lookup(task_id, body, caller)
    account_id = body.account_id or ((meta or {}).get("account_id") if meta else None)
    rules_override = _locked_sora_caller_rules(caller, account_id) if account_id and _is_pool_api_key_caller(caller) else None
    result = _run_sora_request(
        SoraRequestBody(
            account_id=account_id,
            access_token=body.access_token,
            refresh_token=body.refresh_token,
            proxy_url=body.proxy_url,
            method="GET",
            path=f"/backend/video_gen/{task_id}",
            payload={},
        ),
        caller,
        rules_override=rules_override,
    )
    decorated = _decorate_video_task_result(result, task_id=task_id)
    if account_id:
        _sync_video_task_result(
            task_id,
            int(account_id),
            decorated,
            caller.get("api_key_id"),
            default_active=None,
            task_family=_TASK_FAMILY_VIDEO_GEN,
        )
    return decorated


@router.post("/video-gen/create")
def sora_video_gen_create(body: SoraVideoGenCreateBody, caller: dict = Depends(get_sora_api_caller)):
    sora_phone = _import_sora_phone()
    source_image_media_id = (body.source_image_media_id or "").strip()
    source_asset = _load_media_asset(source_image_media_id) if source_image_media_id else None
    forced_account_id = None
    wants_legacy_text_video = _wants_legacy_text_video(body.task_family)

    if not source_image_media_id and not wants_legacy_text_video:
        return sora_video_gen_nf_create(
            SoraVideoGenNfCreateBody(
                account_id=body.account_id,
                access_token=body.access_token,
                refresh_token=body.refresh_token,
                proxy_url=body.proxy_url,
                prompt=body.prompt,
                auto_rotate=body.auto_rotate,
                n_variants=body.n_variants,
                n_frames=body.n_frames,
                resolution=body.resolution,
                orientation=body.orientation,
                model=(body.model or "sy_8").strip() or "sy_8",
                style_id=body.style_id,
                audio_caption=body.audio_caption,
                audio_transcript=body.audio_transcript,
                video_caption=body.video_caption,
                seed=body.seed,
                extra_payload=body.extra_payload,
            ),
            caller,
        )

    if source_image_media_id:
        _require_api_key_video_scope(caller, SORA_API_KEY_SCOPE_IMAGE)
        if source_asset:
            forced_account_id = int(source_asset["account_id"])
            if body.account_id is not None and int(body.account_id) != forced_account_id:
                raise HTTPException(status_code=403, detail="source_image_media_id 绑定的账号与当前请求账号不一致")
        elif body.account_id is None and not (body.access_token or "").strip() and not (body.refresh_token or "").strip():
            raise HTTPException(status_code=400, detail="source_image_media_id 未在本地记录，请先调用 /api/sora-api/video-gen/upload-image 或 /create-with-image")
        payload = sora_phone.sora_build_image_video_payload(
            body.prompt,
            source_image_media_id,
            operation=body.operation,
            n_variants=body.n_variants,
            n_frames=body.n_frames,
            resolution=body.resolution,
            orientation=body.orientation,
            model=(body.model or "").strip() or None,
            seed=body.seed,
        )
    else:
        payload = sora_phone.sora_build_simple_video_payload(
            body.prompt,
            operation=body.operation,
            n_variants=body.n_variants,
            n_frames=body.n_frames,
            resolution=body.resolution,
            orientation=body.orientation,
            model=(body.model or "").strip() or None,
            seed=body.seed,
        )
    if body.extra_payload:
        payload.update(body.extra_payload)
    admin_auto_rotate = bool(body.auto_rotate) and body.account_id is None and not (body.access_token or "").strip() and not (body.refresh_token or "").strip()
    pool_dispatch = not forced_account_id and (_is_pool_api_key_caller(caller) or admin_auto_rotate) and body.account_id is None
    tried_ids = []
    result = None
    reservation_task_id = ""
    fixed_account_id = forced_account_id or (int(body.account_id) if body.account_id is not None else None)

    while True:
        request_body = SoraRequestBody(
            account_id=fixed_account_id,
            access_token=body.access_token,
            refresh_token=body.refresh_token,
            proxy_url=body.proxy_url,
            method="POST",
            path="/backend/video_gen",
            payload=payload,
        )
        rules_override = _locked_sora_caller_rules(caller, fixed_account_id) if fixed_account_id else None
        reserved_account_id = None
        reserved_email = ""

        if pool_dispatch:
            reserved = _reserve_pool_video_account(
                caller.get("api_key_id"),
                exclude_ids=tried_ids,
                task_family=_TASK_FAMILY_VIDEO_GEN,
            )
            if not reserved:
                if result is None:
                    raise HTTPException(status_code=404, detail="账号池中无可用 Sora 账号（请检查 token/额度/启停状态）")
                break
            reservation_task_id = reserved["reservation_task_id"]
            reserved_account_id = int(reserved["id"])
            reserved_email = reserved["email"] or ""
            request_body.account_id = reserved_account_id
            request_body.access_token = ""
            request_body.refresh_token = ""
            request_body.proxy_url = ""
            rules_override = _locked_sora_caller_rules(caller, reserved_account_id)

        try:
            result = _run_sora_request(request_body, caller, rules_override=rules_override)
        except HTTPException as exc:
            if reservation_task_id:
                _release_video_task_reservation(reservation_task_id)
            if pool_dispatch and reserved_account_id is not None and exc.status_code in (429, 503):
                tried_ids.append(reserved_account_id)
                result = {
                    "ok": False,
                    "status_code": exc.status_code,
                    "data": {
                        "error": {
                            "code": "quota_exceeded" if exc.status_code == 429 else "upstream_transport_error",
                            "message": str(exc.detail),
                        }
                    },
                    "used_account_id": reserved_account_id,
                    "used_email": reserved_email,
                }
                reservation_task_id = ""
                continue
            raise

        task_id = ((result.get("data") or {}).get("id") or "").strip()
        decorated = _decorate_video_task_result({
            **result,
            "task_id": task_id,
            "request_payload": payload,
            "source_image_media_id": source_image_media_id,
        }, task_id=task_id)

        used_account_id = decorated.get("used_account_id")
        busy_reason = _extract_busy_reason(decorated.get("data"), "")
        should_retry_pool = pool_dispatch and used_account_id and (busy_reason or _is_too_many_concurrent_tasks_result(decorated))

        if reservation_task_id:
            if task_id and used_account_id:
                _claim_reserved_video_task(
                    reservation_task_id,
                    task_id,
                    int(used_account_id),
                    api_key_id=caller.get("api_key_id"),
                    task_family=_TASK_FAMILY_VIDEO_GEN,
                    raw_status=decorated.get("status") or "",
                    normalized_status=decorated.get("normalized_status") or "",
                    is_active=not bool(decorated.get("is_terminal")),
                )
            else:
                _release_video_task_reservation(reservation_task_id)
            reservation_task_id = ""
        elif task_id and used_account_id:
            _sync_video_task_result(
                task_id,
                int(used_account_id),
                decorated,
                caller.get("api_key_id"),
                default_active=True,
                task_family=_TASK_FAMILY_VIDEO_GEN,
            )

        if should_retry_pool:
            tried_ids.append(int(used_account_id))
            continue
        return decorated

    if result is None:
        raise HTTPException(status_code=500, detail="视频任务创建失败")
    task_id = ((result.get("data") or {}).get("id") or "").strip()
    decorated = _decorate_video_task_result({
        **result,
        "task_id": task_id,
        "request_payload": payload,
        "source_image_media_id": source_image_media_id,
    }, task_id=task_id)
    if task_id and decorated.get("used_account_id"):
        _sync_video_task_result(
            task_id,
            int(decorated["used_account_id"]),
            decorated,
            caller.get("api_key_id"),
            default_active=True,
            task_family=_TASK_FAMILY_VIDEO_GEN,
        )
    return decorated


@router.post("/video-gen/create-and-wait")
def sora_video_gen_create_and_wait(body: SoraVideoGenCreateAndWaitBody, caller: dict = Depends(get_sora_api_caller)):
    create_result = sora_video_gen_create(
        SoraVideoGenCreateBody(
            account_id=body.account_id,
            access_token=body.access_token,
            refresh_token=body.refresh_token,
            proxy_url=body.proxy_url,
            prompt=body.prompt,
            auto_rotate=body.auto_rotate,
            task_family=body.task_family,
            operation=body.operation,
            n_variants=body.n_variants,
            n_frames=body.n_frames,
            resolution=body.resolution,
            orientation=body.orientation,
            model=body.model,
            style_id=body.style_id,
            audio_caption=body.audio_caption,
            audio_transcript=body.audio_transcript,
            video_caption=body.video_caption,
            seed=body.seed,
            source_image_media_id=body.source_image_media_id,
            extra_payload=body.extra_payload,
        ),
        caller,
    )
    task_id = (create_result.get("task_id") or "").strip()
    if not create_result.get("ok") or not task_id:
        return {
            "ok": False,
            "timed_out": False,
            "task_id": task_id,
            "task_family": create_result.get("task_family") or "",
            "status": create_result.get("status") or "",
            "normalized_status": create_result.get("normalized_status") or "",
            "is_terminal": bool(create_result.get("is_terminal")),
            "is_success": bool(create_result.get("is_success")),
            "video_urls": create_result.get("video_urls") or [],
            "used_account_id": create_result.get("used_account_id"),
            "used_email": create_result.get("used_email") or "",
            "poll_attempts": 0,
            "elapsed_seconds": 0.0,
            "message": "视频任务创建失败",
            "create_result": create_result,
            "final_result": create_result,
        }

    started_at = time.time()
    poll_attempts = 0
    final_result = None
    timed_out = False
    poll_interval_seconds = float(body.poll_interval_seconds or 5.0)
    timeout_seconds = int(body.timeout_seconds or 900)

    while True:
        poll_attempts += 1
        final_result = _run_video_task_lookup(task_id, body, caller)
        if final_result.get("is_terminal"):
            break
        if (not final_result.get("ok")) and int(final_result.get("status_code") or 0) not in _VIDEO_RETRYABLE_POLL_STATUS_CODES:
            break
        if (time.time() - started_at) >= timeout_seconds:
            timed_out = True
            break
        time.sleep(poll_interval_seconds)

    elapsed_seconds = round(max(0.0, time.time() - started_at), 2)
    normalized_status = (final_result or {}).get("normalized_status") or ""
    ok = bool(final_result and final_result.get("is_success"))
    if ok:
        message = "视频任务已成功出片（succeeded）"
    elif timed_out:
        current_status = normalized_status or (final_result or {}).get("status") or "unknown"
        message = f"轮询超时，当前状态：{current_status}"
    elif final_result and not final_result.get("ok"):
        code = final_result.get("status_code")
        message = f"轮询查询失败，HTTP {code}"
    else:
        current_status = normalized_status or (final_result or {}).get("status") or "unknown"
        message = f"视频任务已结束，状态：{current_status}"

    return {
        "ok": ok,
        "timed_out": timed_out,
        "task_id": task_id,
        "task_family": (final_result or {}).get("task_family") or create_result.get("task_family") or "",
        "status": (final_result or {}).get("status") or "",
        "normalized_status": normalized_status,
        "is_terminal": bool((final_result or {}).get("is_terminal")),
        "is_success": bool((final_result or {}).get("is_success")),
        "video_urls": (final_result or {}).get("video_urls") or [],
        "used_account_id": create_result.get("used_account_id"),
        "used_email": create_result.get("used_email") or "",
        "poll_attempts": poll_attempts,
        "elapsed_seconds": elapsed_seconds,
        "message": message,
        "create_result": create_result,
        "final_result": final_result,
    }


@router.post("/video-gen-nf/create")
def sora_video_gen_nf_create(body: SoraVideoGenNfCreateBody, caller: dict = Depends(get_sora_api_caller)):
    sora_phone = _import_sora_phone()
    _require_api_key_video_scope(caller, SORA_API_KEY_SCOPE_TEXT)
    payload = sora_phone.sora_build_nf2_video_payload(
        body.prompt,
        n_variants=body.n_variants,
        n_frames=body.n_frames,
        resolution=body.resolution,
        orientation=body.orientation,
        model=body.model,
        style_id=body.style_id,
        audio_caption=body.audio_caption,
        audio_transcript=body.audio_transcript,
        video_caption=body.video_caption,
        seed=body.seed,
    )
    if body.extra_payload:
        payload.update(body.extra_payload)
        payload = sora_phone._strip_nullish(payload)

    admin_auto_rotate = bool(body.auto_rotate) and body.account_id is None and not (body.access_token or "").strip() and not (body.refresh_token or "").strip()
    pool_dispatch = (_is_pool_api_key_caller(caller) or admin_auto_rotate) and body.account_id is None
    tried_ids = []
    result = None
    reservation_task_id = ""
    fixed_account_id = int(body.account_id) if body.account_id is not None else None

    while True:
        reserved_account_id = None
        reserved_email = ""
        request_account_id = fixed_account_id
        if pool_dispatch:
            reserved = _reserve_pool_video_account(
                caller.get("api_key_id"),
                exclude_ids=tried_ids,
                task_family=_TASK_FAMILY_NF2,
            )
            if not reserved:
                if result is None:
                    raise HTTPException(status_code=404, detail="账号池中无可用 Sora 账号（请检查 token/额度/启停状态）")
                break
            reservation_task_id = reserved["reservation_task_id"]
            reserved_account_id = int(reserved["id"])
            reserved_email = reserved["email"] or ""
            request_account_id = reserved_account_id

        data = _resolve_tokens(
            SoraTokenBody(
                account_id=request_account_id,
                access_token=body.access_token if reserved_account_id is None else "",
                refresh_token=body.refresh_token if reserved_account_id is None else "",
                proxy_url=body.proxy_url if reserved_account_id is None else "",
            ),
            allow_refresh=False,
            default_account_id=request_account_id,
            locked_account_id=request_account_id if request_account_id and (reserved_account_id is not None or caller.get("account_id") is not None) else None,
            allow_direct_tokens=reserved_account_id is None,
            allow_pool_rotation=False,
        )
        quota_reason = ""
        for auth_attempt in range(2):
            data = _ensure_nf2_access_token(
                data,
                account_id=request_account_id,
                force_web_login=bool(auth_attempt),
            )
            if not (data.get("access_token") or "").strip():
                break
            if request_account_id and data.get("web_session") is None:
                if auth_attempt == 0:
                    data["access_token"] = ""
                    continue
                result = {
                    "ok": False,
                    "status_code": 400,
                    "data": {"error": {"code": "missing_web_session", "message": "无法建立可用的 ChatGPT/Sora Web session"}},
                    "used_account_id": request_account_id,
                    "used_email": reserved_email or ((data.get("account") or {}).get("email") or ""),
                }
                break
            result, quota_reason = _run_nf2_create_request(data, body, payload)
            if int(result.get("status_code") or 0) == 401 and request_account_id and auth_attempt == 0:
                _drop_nf2_web_session(int(request_account_id))
                data["access_token"] = ""
                data["web_session"] = None
                continue
            if request_account_id and data.get("web_session") is not None:
                _touch_nf2_web_session(int(request_account_id), data)
            break

        if not (data.get("access_token") or "").strip():
            if reservation_task_id:
                _release_video_task_reservation(reservation_task_id)
                reservation_task_id = ""
            if pool_dispatch and request_account_id:
                tried_ids.append(int(request_account_id))
                result = {
                    "ok": False,
                    "status_code": 400,
                    "data": {"error": {"code": "missing_web_access_token", "message": "缺少可用的 ChatGPT Web access_token"}},
                    "used_account_id": request_account_id,
                    "used_email": reserved_email or ((data.get("account") or {}).get("email") or ""),
                }
                continue
            raise HTTPException(status_code=400, detail="缺少可用的 ChatGPT Web access_token")

        if quota_reason:
            if data["account"] is not None:
                _mark_account_quota_exhausted(data["account"]["id"], quota_reason)
            if reservation_task_id:
                _release_video_task_reservation(reservation_task_id)
                reservation_task_id = ""
            if pool_dispatch and request_account_id:
                tried_ids.append(int(request_account_id))
                continue
            raise HTTPException(status_code=429, detail=f"账号额度不足，已自动标记不可用：{quota_reason}")

        decorated = _decorate_nf2_result({
            **result,
            "request_payload": payload,
        })
        task_id = (decorated.get("task_id") or "").strip()
        used_account_id = decorated.get("used_account_id")
        busy_reason = _extract_busy_reason(decorated.get("data"), "")
        should_retry_pool = pool_dispatch and used_account_id and (
            busy_reason
            or _is_too_many_concurrent_tasks_result(decorated)
            or int(decorated.get("status_code") or 0) == 401
        )

        if reservation_task_id:
            if task_id and used_account_id:
                _claim_reserved_video_task(
                    reservation_task_id,
                    task_id,
                    int(used_account_id),
                    api_key_id=caller.get("api_key_id"),
                    task_family=_TASK_FAMILY_NF2,
                    raw_status=decorated.get("status") or "",
                    normalized_status=decorated.get("normalized_status") or "",
                    is_active=not bool(decorated.get("is_terminal")),
                )
            else:
                _release_video_task_reservation(reservation_task_id)
            reservation_task_id = ""
        elif task_id and used_account_id:
            _sync_video_task_result(
                task_id,
                int(used_account_id),
                decorated,
                caller.get("api_key_id"),
                default_active=True,
                task_family=_TASK_FAMILY_NF2,
            )

        if should_retry_pool:
            tried_ids.append(int(used_account_id))
            continue
        return decorated

    if result is None:
        raise HTTPException(status_code=500, detail="NF2 视频任务创建失败")
    return _decorate_nf2_result({**result, "request_payload": payload})


@router.post("/video-gen-nf/get")
def sora_video_gen_nf_get(body: SoraVideoTaskBody, caller: dict = Depends(get_sora_api_caller)):
    _require_api_key_any_video_scope(caller)
    task_id = (body.task_id or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="缺少 task_id")
    return _run_nf2_task_lookup(task_id, body, caller)


@router.post("/video-gen-nf/pending")
def sora_video_gen_nf_pending(body: SoraTokenBody, caller: dict = Depends(get_sora_api_caller)):
    _require_api_key_any_video_scope(caller)
    sora_phone = _import_sora_phone()
    default_account_id = caller.get("account_id") if (caller.get("auth_type") or "") == "api_key" else None
    return _run_nf2_session_request(
        body,
        default_account_id=default_account_id,
        locked_account_id=default_account_id,
        allow_pool_rotation=_is_pool_api_key_caller(caller),
        request_fn=lambda data, origin: sora_phone.sora_nf2_get_pending(
            data["access_token"],
            proxy_url=data["proxy_url"],
            web_session=data.get("web_session"),
            base_origin=origin,
        ),
    )


@router.post("/video-gen-nf/draft/get")
def sora_video_gen_nf_draft_get(body: SoraDraftBody, caller: dict = Depends(get_sora_api_caller)):
    _require_api_key_any_video_scope(caller)
    sora_phone = _import_sora_phone()
    draft_id = (body.draft_id or "").strip()
    if not draft_id:
        raise HTTPException(status_code=400, detail="缺少 draft_id")
    default_account_id = caller.get("account_id") if (caller.get("auth_type") or "") == "api_key" else None
    return _run_nf2_session_request(
        body,
        default_account_id=default_account_id,
        locked_account_id=default_account_id,
        allow_pool_rotation=_is_pool_api_key_caller(caller),
        request_fn=lambda data, origin: sora_phone.sora_nf2_get_draft(
            data["access_token"],
            draft_id,
            proxy_url=data["proxy_url"],
            web_session=data.get("web_session"),
            base_origin=origin,
        ),
        decorate_fn=_decorate_nf2_result,
    )


@router.post("/video-gen-nf/stitch")
def sora_video_gen_nf_stitch(body: SoraStitchBody, caller: dict = Depends(get_sora_api_caller)):
    _require_api_key_any_video_scope(caller)
    sora_phone = _import_sora_phone()
    generation_ids = [str(item).strip() for item in (body.generation_ids or []) if str(item).strip()]
    if not generation_ids:
        raise HTTPException(status_code=400, detail="缺少 generation_ids")
    default_account_id = caller.get("account_id") if (caller.get("auth_type") or "") == "api_key" else None
    return _run_nf2_session_request(
        body,
        default_account_id=default_account_id,
        locked_account_id=default_account_id,
        allow_pool_rotation=_is_pool_api_key_caller(caller),
        request_fn=lambda data, origin: sora_phone.sora_nf2_stitch(
            data["access_token"],
            generation_ids,
            for_download=bool(body.for_download),
            proxy_url=data["proxy_url"],
            web_session=data.get("web_session"),
            base_origin=origin,
        ),
        decorate_fn=_decorate_nf2_result,
    )


def _upload_image_bytes_with_retry(
    *,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    account_id: Optional[int],
    auto_rotate: bool,
    access_token: str,
    refresh_token: str,
    proxy_url: str,
    caller: dict,
    exclude_account_ids: Optional[list[int]] = None,
) -> dict:
    _require_api_key_video_scope(caller, SORA_API_KEY_SCOPE_IMAGE)
    token_body = SoraTokenBody(
        account_id=account_id,
        access_token=access_token,
        refresh_token=refresh_token,
        proxy_url=proxy_url,
    )
    rules = dict(_sora_caller_rules(caller))
    if bool(auto_rotate) and account_id is None and not access_token.strip() and not refresh_token.strip():
        rules["allow_pool_rotation"] = True
    resolve_keys = {k: v for k, v in rules.items() if k not in ("inject_watermark_free",)}
    sora_phone = _import_sora_phone()
    allow_pool_upload = bool(resolve_keys.get("allow_pool_rotation")) and account_id is None and not access_token.strip() and not refresh_token.strip()
    tried_ids = [int(x) for x in (exclude_account_ids or []) if int(x)]
    last_transport_error = ""

    while True:
        try:
            data = _resolve_tokens(
                token_body,
                allow_refresh=True,
                prefer_refresh_token_for_sora=True,
                exclude_account_ids=tried_ids,
                **resolve_keys,
            )
        except HTTPException:
            if allow_pool_upload and last_transport_error:
                _raise_transport_http_error(last_transport_error, all_accounts=True)
            raise

        at = data["access_token"]
        if not at:
            raise HTTPException(status_code=400, detail="缺少 access_token（或 refresh_token）")

        try:
            resp = _run_transport_safe_request(
                lambda: sora_phone.sora_upload_media(
                    at,
                    filename=filename,
                    content_type=content_type,
                    file_bytes=file_bytes,
                    media_type="image",
                    proxy_url=data["proxy_url"],
                )
            )
        except SoraUpstreamTransportError as exc:
            used_account = data["account"]
            last_transport_error = str(exc)
            if used_account is not None:
                _mark_account_last_error(used_account["id"], f"image upload transport error: {exc}")
            if allow_pool_upload and used_account is not None:
                tried_ids.append(int(used_account["id"]))
                if len(tried_ids) <= _MAX_POOL_RETRIES:
                    continue
                _raise_transport_http_error(last_transport_error, all_accounts=True)
            _raise_transport_http_error(last_transport_error, all_accounts=False)

        try:
            payload = resp.json()
        except Exception:
            payload = {"text": (resp.text or "")[:1000]}

        ok = 200 <= resp.status_code < 300
        used_account = data["account"]
        if ok and used_account is not None:
            _clear_account_quota_exhausted(used_account["id"])
        elif used_account is not None:
            _mark_account_last_error(used_account["id"], f"image upload HTTP {resp.status_code}")

        media_id = ((payload or {}).get("id") or "").strip() if isinstance(payload, dict) else ""
        if ok and media_id and used_account is not None:
            _remember_media_asset(media_id, int(used_account["id"]), payload if isinstance(payload, dict) else {}, caller.get("api_key_id"))

        return {
            "ok": ok,
            "status_code": resp.status_code,
            "media_id": media_id,
            "media": payload,
            "used_account_id": used_account["id"] if used_account else None,
            "used_email": used_account["email"] if used_account else "",
            "source_image_media_id": media_id,
        }


@router.post("/video-gen/upload-image")
async def sora_video_gen_upload_image(
    file: UploadFile = File(...),
    account_id: Optional[int] = Form(default=None),
    auto_rotate: bool = Form(default=False),
    access_token: str = Form(default=""),
    refresh_token: str = Form(default=""),
    proxy_url: str = Form(default=""),
    caller: dict = Depends(get_sora_api_caller),
):
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="缺少图片文件名")
    content_type = (file.content_type or mimetypes.guess_type(filename)[0] or "").strip().lower() or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="仅支持图片文件上传")
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="上传文件为空")

    return _upload_image_bytes_with_retry(
        filename=filename,
        content_type=content_type,
        file_bytes=file_bytes,
        account_id=account_id,
        auto_rotate=auto_rotate,
        access_token=access_token,
        refresh_token=refresh_token,
        proxy_url=proxy_url,
        caller=caller,
        exclude_account_ids=None,
    )


@router.post("/video-gen/create-with-image")
async def sora_video_gen_create_with_image(
    prompt: str = Form(...),
    file: UploadFile = File(...),
    account_id: Optional[int] = Form(default=None),
    auto_rotate: bool = Form(default=False),
    access_token: str = Form(default=""),
    refresh_token: str = Form(default=""),
    proxy_url: str = Form(default=""),
    operation: str = Form(default="simple_compose"),
    n_variants: int = Form(default=1),
    n_frames: int = Form(default=300),
    resolution: int = Form(default=360),
    orientation: str = Form(default="wide"),
    model: str = Form(default=""),
    seed: Optional[int] = Form(default=None),
    caller: dict = Depends(get_sora_api_caller),
):
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="缺少图片文件名")
    content_type = (file.content_type or mimetypes.guess_type(filename)[0] or "").strip().lower() or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="仅支持图片文件上传")
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="上传文件为空")

    allow_pool_retry = bool(auto_rotate) and account_id is None and not access_token.strip() and not refresh_token.strip()
    tried_ids: list[int] = []
    last_result: Optional[dict] = None

    while True:
        try:
            upload_result = _upload_image_bytes_with_retry(
                filename=filename,
                content_type=content_type,
                file_bytes=file_bytes,
                account_id=account_id,
                auto_rotate=auto_rotate,
                access_token=access_token,
                refresh_token=refresh_token,
                proxy_url=proxy_url,
                caller=caller,
                exclude_account_ids=tried_ids,
            )
        except HTTPException as exc:
            if allow_pool_retry and last_result is not None and exc.status_code in (404, 503):
                return last_result
            raise

        if not upload_result.get("ok"):
            return upload_result

        used_account_id = upload_result.get("used_account_id")
        try:
            create_result = sora_video_gen_create(
                SoraVideoGenCreateBody(
                    account_id=used_account_id,
                    prompt=prompt,
                    auto_rotate=False,
                    operation=operation,
                    n_variants=n_variants,
                    n_frames=n_frames,
                    resolution=resolution,
                    orientation=orientation,
                    model=model,
                    seed=seed,
                    source_image_media_id=upload_result.get("media_id") or "",
                    extra_payload={},
                ),
                caller,
            )
        except HTTPException as exc:
            if allow_pool_retry and used_account_id and exc.status_code in (429, 503):
                last_result = {
                    "ok": False,
                    "status_code": exc.status_code,
                    "data": {"error": {"code": "upstream_transport_error" if exc.status_code == 503 else "quota_exceeded", "message": str(exc.detail)}},
                    "used_account_id": used_account_id,
                    "used_email": upload_result.get("used_email") or "",
                    "task_id": "",
                    "request_payload": {},
                    "source_image_media_id": upload_result.get("media_id") or "",
                    "status": "",
                    "normalized_status": "",
                    "is_terminal": False,
                    "is_success": False,
                    "video_urls": [],
                    "uploaded_media": upload_result.get("media") or {},
                }
                tried_ids.append(int(used_account_id))
                if len(tried_ids) <= _MAX_POOL_RETRIES:
                    continue
                return last_result
            raise

        create_result["uploaded_media"] = upload_result.get("media") or {}
        create_result["source_image_media_id"] = upload_result.get("media_id") or ""
        if allow_pool_retry and used_account_id:
            busy_reason = _extract_busy_reason(create_result.get("data"), "")
            if busy_reason or _is_too_many_concurrent_tasks_result(create_result):
                last_result = create_result
                tried_ids.append(int(used_account_id))
                if len(tried_ids) <= _MAX_POOL_RETRIES:
                    continue
        return create_result


@router.post("/video-gen/list")
def sora_video_gen_list(body: SoraVideoListBody, caller: dict = Depends(get_sora_api_caller)):
    _require_api_key_any_video_scope(caller)
    return _run_sora_request(
        SoraRequestBody(
            account_id=body.account_id,
            access_token=body.access_token,
            refresh_token=body.refresh_token,
            proxy_url=body.proxy_url,
            method="GET",
            path=_build_video_gen_list_path(
                limit=body.limit,
                last_id=body.last_id,
                task_type_filter=body.task_type_filter,
            ),
            payload={},
        ),
        caller,
    )


@router.post("/video-gen/get")
def sora_video_gen_get(body: SoraVideoTaskBody, caller: dict = Depends(get_sora_api_caller)):
    _require_api_key_any_video_scope(caller)
    task_id = (body.task_id or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="缺少 task_id")
    return _run_video_task_lookup(task_id, body, caller)


@router.post("/video-gen/cancel")
def sora_video_gen_cancel(body: SoraVideoTaskBody, caller: dict = Depends(get_sora_api_caller)):
    _require_api_key_any_video_scope(caller)
    task_id = (body.task_id or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="缺少 task_id")
    account_id = body.account_id or _lookup_video_task_account(task_id)
    rules_override = _locked_sora_caller_rules(caller, account_id) if account_id and _is_pool_api_key_caller(caller) else None
    result = _run_sora_request(
        SoraRequestBody(
            account_id=account_id,
            access_token=body.access_token,
            refresh_token=body.refresh_token,
            proxy_url=body.proxy_url,
            method="POST",
            path=f"/backend/video_gen/{task_id}/cancel",
            payload={},
        ),
        caller,
        rules_override=rules_override,
    )
    decorated = _decorate_video_task_result(result, task_id=task_id)
    if account_id:
        _sync_video_task_result(task_id, int(account_id), decorated, caller.get("api_key_id"), default_active=None)
    return decorated


@router.post("/video-gen/archive")
def sora_video_gen_archive(body: SoraVideoTaskBody, caller: dict = Depends(get_sora_api_caller)):
    _require_api_key_any_video_scope(caller)
    task_id = (body.task_id or "").strip()
    if not task_id:
        raise HTTPException(status_code=400, detail="缺少 task_id")
    account_id = body.account_id or _lookup_video_task_account(task_id)
    rules_override = _locked_sora_caller_rules(caller, account_id) if account_id and _is_pool_api_key_caller(caller) else None
    result = _run_sora_request(
        SoraRequestBody(
            account_id=account_id,
            access_token=body.access_token,
            refresh_token=body.refresh_token,
            proxy_url=body.proxy_url,
            method="POST",
            path=f"/backend/video_gen/{task_id}/archive",
            payload={},
        ),
        caller,
        rules_override=rules_override,
    )
    decorated = _decorate_video_task_result(result, task_id=task_id)
    if account_id:
        _sync_video_task_result(task_id, int(account_id), decorated, caller.get("api_key_id"), default_active=None)
    return decorated
