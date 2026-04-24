from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.routers.auth import get_current_user
from app.database import get_db, init_db
import csv
import io
from datetime import datetime

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountSoraStateBody(BaseModel):
    sora_enabled: Optional[bool] = None
    reset_quota: bool = False


class AccountSoraQuotaRecheckBody(BaseModel):
    account_id: Optional[int] = None
    limit: int = 10
    auto_cancel: bool = True
    prompt: str = "A calm abstract light gradient slowly drifting."
    n_frames: int = 60
    resolution: int = 360
    orientation: str = "wide"


def _load_quota_recheck_candidates(account_id: Optional[int] = None, limit: int = 10) -> list[dict]:
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        if account_id is not None:
            c.execute(
                """SELECT id, email, status,
                          COALESCE(refresh_token, '') AS refresh_token,
                          COALESCE(access_token, '') AS access_token,
                          COALESCE(proxy, '') AS proxy,
                          COALESCE(has_sora, 0) AS has_sora,
                          COALESCE(sora_enabled, 1) AS sora_enabled,
                          COALESCE(sora_quota_exhausted, 0) AS sora_quota_exhausted,
                          COALESCE(sora_quota_note, '') AS sora_quota_note,
                          COALESCE(sora_quota_updated_at, '') AS sora_quota_updated_at
                   FROM accounts
                   WHERE id = ?
                   LIMIT 1""",
                (int(account_id),),
            )
        else:
            c.execute(
                """SELECT id, email, status,
                          COALESCE(refresh_token, '') AS refresh_token,
                          COALESCE(access_token, '') AS access_token,
                          COALESCE(proxy, '') AS proxy,
                          COALESCE(has_sora, 0) AS has_sora,
                          COALESCE(sora_enabled, 1) AS sora_enabled,
                          COALESCE(sora_quota_exhausted, 0) AS sora_quota_exhausted,
                          COALESCE(sora_quota_note, '') AS sora_quota_note,
                          COALESCE(sora_quota_updated_at, '') AS sora_quota_updated_at
                   FROM accounts
                   WHERE has_sora = 1
                     AND COALESCE(sora_enabled, 1) = 1
                     AND COALESCE(sora_quota_exhausted, 0) = 1
                     AND (COALESCE(refresh_token, '') != '' OR COALESCE(access_token, '') != '')
                   ORDER BY
                       CASE WHEN COALESCE(sora_quota_updated_at, '') = '' THEN 1 ELSE 0 END,
                       sora_quota_updated_at ASC,
                       id ASC
                   LIMIT ?""",
                (max(1, min(int(limit or 10), 50)),),
            )
        rows = c.fetchall()

    items = []
    for row in rows:
        items.append({
            "id": int(row[0]),
            "email": row[1] or "",
            "status": row[2] or "",
            "refresh_token": (row[3] or "").strip(),
            "access_token": (row[4] or "").strip(),
            "proxy": (row[5] or "").strip(),
            "has_sora": bool(row[6]),
            "sora_enabled": bool(row[7]),
            "sora_quota_exhausted": bool(row[8]),
            "sora_quota_note": row[9] or "",
            "sora_quota_updated_at": row[10] or "",
        })
    return items


def _probe_account_sora_quota(account: dict, body: AccountSoraQuotaRecheckBody) -> dict:
    from app.routers import sora_api as sora_router

    account_id = int(account["id"])
    email = account.get("email") or ""
    result = {
        "account_id": account_id,
        "email": email,
        "status": account.get("status") or "",
        "quota_note": account.get("sora_quota_note") or "",
        "quota_updated_at": account.get("sora_quota_updated_at") or "",
        "result": "",
        "message": "",
        "recovered_to_pool": False,
        "task_id": "",
        "create_status_code": 0,
        "cancel_status_code": 0,
        "cancel_ok": False,
    }

    if not account.get("has_sora"):
        result["result"] = "skipped_no_sora"
        result["message"] = "账号未开通 Sora"
        return result
    if not account.get("sora_enabled"):
        result["result"] = "skipped_disabled"
        result["message"] = "账号已停用"
        return result
    if not account.get("sora_quota_exhausted"):
        result["result"] = "already_available"
        result["message"] = "账号当前未标记额度不足，已经在轮换池内"
        result["recovered_to_pool"] = True
        return result
    if not ((account.get("refresh_token") or "").strip() or (account.get("access_token") or "").strip()):
        result["result"] = "skipped_no_token"
        result["message"] = "账号没有可用 token，无法复检"
        return result

    sora_phone = sora_router._import_sora_phone()
    access_token = (account.get("access_token") or "").strip()
    refresh_token = (account.get("refresh_token") or "").strip()
    proxy_url = (account.get("proxy") or "").strip()
    refresh_error = ""

    if refresh_token:
        try:
            token_out = sora_phone.rt_to_at_mobile(refresh_token, proxy_url=proxy_url)
            new_access_token = (token_out.get("access_token") or "").strip()
            new_refresh_token = (token_out.get("refresh_token") or "").strip()
            new_id_token = (token_out.get("id_token") or "").strip()
            if new_access_token:
                access_token = new_access_token
            if new_refresh_token:
                refresh_token = new_refresh_token
            if new_access_token or new_refresh_token or new_id_token:
                sora_router._save_account_tokens(
                    account_id,
                    access_token=new_access_token,
                    refresh_token=new_refresh_token,
                    id_token=new_id_token,
                )
        except Exception as exc:
            refresh_error = str(exc or "").strip()[:300]

    if not access_token:
        detail = refresh_error or "缺少 access_token"
        sora_router._mark_account_last_error(account_id, f"quota recheck auth failed: {detail}")
        result["result"] = "auth_failed"
        result["message"] = f"换取 access_token 失败：{detail}"
        return result

    prompt = (body.prompt or "").strip() or "A calm abstract light gradient slowly drifting."
    orientation = (body.orientation or "wide").strip().lower()
    if orientation not in ("wide", "tall", "square"):
        orientation = "wide"
    payload = sora_phone.sora_build_simple_video_payload(
        prompt,
        n_variants=1,
        n_frames=max(60, int(body.n_frames or 60)),
        resolution=max(360, int(body.resolution or 360)),
        orientation=orientation,
        model=None,
        seed=None,
    )
    request_body = sora_router.SoraRequestBody(
        access_token=access_token,
        proxy_url=proxy_url,
        method="POST",
        path="/backend/video_gen",
        payload=payload,
    )
    request_data = {
        "account": account,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "proxy_url": proxy_url,
    }

    try:
        response, response_payload, quota_reason = sora_router._do_sora_request(
            request_body,
            request_data,
            inject_watermark_free=False,
        )
    except Exception as exc:
        detail = str(exc or "").strip()[:300]
        sora_router._mark_account_last_error(account_id, f"quota recheck failed: {detail}")
        result["result"] = "probe_failed"
        result["message"] = f"探针请求异常：{detail}"
        return result

    result["create_status_code"] = int(response.status_code or 0)
    decorated = sora_router._decorate_video_task_result(
        {
            "ok": 200 <= response.status_code < 300,
            "status_code": response.status_code,
            "data": response_payload,
            "used_account_id": account_id,
            "used_email": email,
        }
    )
    task_id = (decorated.get("task_id") or "").strip()
    result["task_id"] = task_id
    busy_reason = sora_router._extract_busy_reason(response_payload, response.text or "")

    if quota_reason:
        sora_router._mark_account_quota_exhausted(account_id, quota_reason)
        result["result"] = "still_exhausted"
        result["message"] = f"账号仍然额度不足：{quota_reason}"
        return result

    if busy_reason or sora_router._is_too_many_concurrent_tasks_result(decorated):
        sora_router._clear_account_quota_exhausted(account_id)
        result["result"] = "recovered_busy"
        result["message"] = "额度已恢复，但账号当前并发繁忙，已重新回池"
        result["recovered_to_pool"] = True
        return result

    if 200 <= response.status_code < 300 and task_id:
        if body.auto_cancel:
            cancel_request = sora_router.SoraRequestBody(
                access_token=access_token,
                proxy_url=proxy_url,
                method="POST",
                path=f"/backend/video_gen/{task_id}/cancel",
                payload={},
            )
            try:
                cancel_response, cancel_payload, _ = sora_router._do_sora_request(
                    cancel_request,
                    request_data,
                    inject_watermark_free=False,
                )
                result["cancel_status_code"] = int(cancel_response.status_code or 0)
                result["cancel_ok"] = 200 <= cancel_response.status_code < 300
                cancel_decorated = sora_router._decorate_video_task_result(
                    {
                        "ok": 200 <= cancel_response.status_code < 300,
                        "status_code": cancel_response.status_code,
                        "data": cancel_payload,
                        "used_account_id": account_id,
                        "used_email": email,
                    },
                    task_id=task_id,
                )
                sora_router._sync_video_task_result(
                    task_id,
                    account_id,
                    cancel_decorated,
                    default_active=not bool(result["cancel_ok"]),
                )
            except Exception as exc:
                detail = str(exc or "").strip()[:300]
                sora_router._remember_video_task(
                    task_id,
                    account_id,
                    raw_status=decorated.get("status") or "",
                    normalized_status=decorated.get("normalized_status") or "",
                    is_active=True,
                )
                result["message"] = f"探针创建成功，但自动取消失败：{detail}"
        else:
            sora_router._remember_video_task(
                task_id,
                account_id,
                raw_status=decorated.get("status") or "",
                normalized_status=decorated.get("normalized_status") or "",
                is_active=True,
            )

        sora_router._clear_account_quota_exhausted(account_id)
        result["result"] = "recovered"
        base_message = "探针创建成功，额度已恢复并重新回池"
        if result["message"]:
            base_message += f"；{result['message']}"
        result["message"] = base_message
        result["recovered_to_pool"] = True
        if body.auto_cancel and not result["cancel_ok"] and result["cancel_status_code"]:
            result["message"] += f"；取消返回 HTTP {result['cancel_status_code']}"
        return result

    detail = f"探针返回 HTTP {response.status_code}"
    if refresh_error:
        detail += f"，RT->AT 报错：{refresh_error}"
    sora_router._mark_account_last_error(account_id, detail)
    result["result"] = "probe_failed"
    result["message"] = detail
    return result


@router.get("")
def list_accounts(
    username: str = Depends(get_current_user),
    status: str = Query(None),
    has_sora: bool = Query(None),
    has_plus: bool = Query(None),
    phone_bound: bool = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        where = []
        params = []
        if status:
            where.append("status = ?")
            params.append(status)
        if has_sora is not None:
            where.append("has_sora = ?")
            params.append(1 if has_sora else 0)
        if has_plus is not None:
            where.append("has_plus = ?")
            params.append(1 if has_plus else 0)
        if phone_bound is not None:
            where.append("phone_bound = ?")
            params.append(1 if phone_bound else 0)
        where_sql = " AND ".join(where) if where else "1=1"
        c.execute(
            f"SELECT COUNT(*) FROM accounts WHERE {where_sql}",
            params
        )
        total = c.fetchone()[0]
        offset = (page - 1) * page_size
        c.execute(
            f"""SELECT id, email, password, status, registered_at,
                   has_sora, has_plus, phone_bound, proxy, refresh_token, access_token, id_token, created_at,
                   COALESCE(sora_enabled, 1) AS sora_enabled,
                   COALESCE(sora_quota_exhausted, 0) AS sora_quota_exhausted,
                   COALESCE(sora_quota_note, '') AS sora_quota_note,
                   COALESCE(sora_quota_updated_at, '') AS sora_quota_updated_at,
                   COALESCE(sora_last_error, '') AS sora_last_error
            FROM accounts WHERE {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
            params + [page_size, offset]
        )
        rows = c.fetchall()
    items = []
    for r in rows:
        items.append({
            "id": r[0],
            "email": r[1],
            "password": r[2],
            "status": r[3],
            "registered_at": r[4],
            "has_sora": bool(r[5]),
            "has_plus": bool(r[6]),
            "phone_bound": bool(r[7]),
            "proxy": r[8],
            "refresh_token": (r[9] or "")[:20] + "..." if r[9] else "",
            "access_token": (r[10] or "")[:20] + "..." if r[10] else "",
            "id_token": (r[11] or "")[:20] + "..." if r[11] else "",
            "created_at": r[12],
            "sora_enabled": bool(r[13]),
            "sora_quota_exhausted": bool(r[14]),
            "sora_quota_note": r[15] or "",
            "sora_quota_updated_at": r[16] or "",
            "sora_last_error": r[17] or "",
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.post("/sora-quota/recheck")
def recheck_sora_quota(
    body: AccountSoraQuotaRecheckBody,
    username: str = Depends(get_current_user),
):
    limit = max(1, min(int(body.limit or 10), 50))
    candidates = _load_quota_recheck_candidates(account_id=body.account_id, limit=limit)
    if body.account_id is not None and not candidates:
        raise HTTPException(status_code=404, detail="账号不存在")

    if not candidates:
        return {
            "ok": True,
            "message": "当前没有被标记额度不足的账号",
            "checked_count": 0,
            "recovered_count": 0,
            "still_exhausted_count": 0,
            "failed_count": 0,
            "busy_count": 0,
            "items": [],
        }

    items = [_probe_account_sora_quota(account, body) for account in candidates]
    recovered_count = sum(1 for item in items if item.get("result") == "recovered")
    busy_count = sum(1 for item in items if item.get("result") == "recovered_busy")
    still_exhausted_count = sum(1 for item in items if item.get("result") == "still_exhausted")
    failed_count = sum(
        1
        for item in items
        if item.get("result") in ("probe_failed", "auth_failed", "skipped_no_token", "skipped_disabled", "skipped_no_sora")
    )
    message = (
        f"已复检 {len(items)} 个账号，恢复 {recovered_count + busy_count} 个，"
        f"仍然额度不足 {still_exhausted_count} 个，失败/跳过 {failed_count} 个"
    )
    return {
        "ok": True,
        "message": message,
        "checked_count": len(items),
        "recovered_count": recovered_count,
        "busy_count": busy_count,
        "still_exhausted_count": still_exhausted_count,
        "failed_count": failed_count,
        "items": items,
    }


@router.get("/next-sora-available")
def next_sora_available_account(username: str = Depends(get_current_user)):
    """
    手动轮换：返回下一个可用 Sora 账号（仅可用且有 token 的账号）。
    """
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM system_settings WHERE key = 'sora_manual_rotate_cursor'")
        row = c.fetchone()
        try:
            cursor = int((row[0] if row else "0") or "0")
        except Exception:
            cursor = 0

        c.execute(
            """SELECT id, email, status,
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
            raise HTTPException(status_code=404, detail="暂无可用 Sora 账号（请检查 token/额度/启停状态）")

        pick = None
        for r in rows:
            if int(r[0]) > cursor:
                pick = r
                break
        if pick is None:
            pick = rows[0]

        c.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
            ("sora_manual_rotate_cursor", str(int(pick[0]))),
        )

    return {
        "id": int(pick[0]),
        "email": pick[1] or "",
        "status": pick[2] or "",
        "sora_enabled": bool(pick[3]),
        "sora_quota_exhausted": bool(pick[4]),
        "sora_quota_note": pick[5] or "",
        "sora_quota_updated_at": pick[6] or "",
    }


@router.get("/{account_id:int}")
def get_account_detail(account_id: int, username: str = Depends(get_current_user)):
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT id, email, status, registered_at,
                      has_sora, has_plus, phone_bound,
                      COALESCE(refresh_token, '') AS refresh_token,
                      COALESCE(access_token, '') AS access_token,
                      COALESCE(id_token, '') AS id_token,
                      COALESCE(sora_enabled, 1) AS sora_enabled,
                      COALESCE(sora_quota_exhausted, 0) AS sora_quota_exhausted,
                      COALESCE(sora_quota_note, '') AS sora_quota_note,
                      COALESCE(sora_quota_updated_at, '') AS sora_quota_updated_at,
                      COALESCE(sora_last_error, '') AS sora_last_error
               FROM accounts
               WHERE id = ?""",
            (account_id,),
        )
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {
        "id": row[0],
        "email": row[1] or "",
        "status": row[2] or "",
        "registered_at": row[3] or "",
        "has_sora": bool(row[4]),
        "has_plus": bool(row[5]),
        "phone_bound": bool(row[6]),
        "has_token": bool((row[7] or "").strip() or (row[8] or "").strip()),
        "has_id_token": bool((row[9] or "").strip()),
        "sora_enabled": bool(row[10]),
        "sora_quota_exhausted": bool(row[11]),
        "sora_quota_note": row[12] or "",
        "sora_quota_updated_at": row[13] or "",
        "sora_last_error": row[14] or "",
    }


@router.post("/{account_id:int}/sora-state")
def update_account_sora_state(
    account_id: int,
    body: AccountSoraStateBody,
    username: str = Depends(get_current_user),
):
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM accounts WHERE id = ?", (account_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail="账号不存在")

        if body.sora_enabled is not None:
            c.execute(
                "UPDATE accounts SET sora_enabled = ? WHERE id = ?",
                (1 if body.sora_enabled else 0, account_id),
            )

        if body.reset_quota:
            c.execute(
                """UPDATE accounts
                   SET sora_quota_exhausted = 0,
                       sora_quota_note = '',
                       sora_last_error = '',
                       sora_quota_updated_at = datetime('now')
                   WHERE id = ?""",
                (account_id,),
            )

        c.execute(
            """SELECT id, email,
                      COALESCE(sora_enabled, 1),
                      COALESCE(sora_quota_exhausted, 0),
                      COALESCE(sora_quota_note, ''),
                      COALESCE(sora_quota_updated_at, ''),
                      COALESCE(sora_last_error, '')
               FROM accounts
               WHERE id = ?""",
            (account_id,),
        )
        row = c.fetchone()

    return {
        "id": row[0],
        "email": row[1] or "",
        "sora_enabled": bool(row[2]),
        "sora_quota_exhausted": bool(row[3]),
        "sora_quota_note": row[4] or "",
        "sora_quota_updated_at": row[5] or "",
        "sora_last_error": row[6] or "",
    }


@router.get("/export")
def export_accounts(
    username: str = Depends(get_current_user),
    status: str = Query(None),
    has_sora: bool = Query(None),
    has_plus: bool = Query(None),
):
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        where = []
        params = []
        if status:
            where.append("status = ?")
            params.append(status)
        if has_sora is not None:
            where.append("has_sora = ?")
            params.append(1 if has_sora else 0)
        if has_plus is not None:
            where.append("has_plus = ?")
            params.append(1 if has_plus else 0)
        where_sql = " AND ".join(where) if where else "1=1"
        c.execute(
            f"""SELECT email, password, status, registered_at, has_sora, has_plus, phone_bound, proxy, refresh_token, access_token, id_token,
                       COALESCE(sora_enabled, 1), COALESCE(sora_quota_exhausted, 0), COALESCE(sora_quota_note, '')
            FROM accounts WHERE {where_sql} ORDER BY id DESC""",
            params
        )
        rows = c.fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "password", "status", "registered_at", "has_sora", "has_plus", "phone_bound", "proxy", "refresh_token", "access_token", "id_token", "sora_enabled", "sora_quota_exhausted", "sora_quota_note"])
    for r in rows:
        writer.writerow([
            r[0], r[1], r[2], r[3],
            "Y" if r[4] else "N", "Y" if r[5] else "N", "Y" if r[6] else "N",
            r[7] or "", r[8] or "", r[9] or "", r[10] or "",
            "Y" if r[11] else "N", "Y" if r[12] else "N", r[13] or ""
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.get("/export-sora2")
def export_sora2_accounts(
    username: str = Depends(get_current_user),
    has_sora: bool = Query(True, description="是否只导出 has_sora=1 的账号"),
    require_token: bool = Query(False, description="是否要求 refresh_token/access_token 至少有一个"),
    only_available: bool = Query(False, description="是否仅导出未停用且未标记额度不足账号"),
    format: str = Query("txt", description="导出格式: txt 或 csv"),
    separator: str = Query("----", description="txt 模式下字段分隔符"),
):
    """
    导出 Sora2 可用账号：
    - txt: email----password----refresh_token----access_token----id_token（无表头）
    - csv: 带表头
    """
    fmt = (format or "txt").strip().lower()
    if fmt not in ("txt", "csv"):
        fmt = "txt"
    sep = separator if isinstance(separator, str) and separator else "----"

    init_db()
    with get_db() as conn:
        c = conn.cursor()
        where = []
        params = []
        if has_sora is not None:
            where.append("has_sora = ?")
            params.append(1 if has_sora else 0)
        if require_token:
            where.append("(COALESCE(refresh_token, '') != '' OR COALESCE(access_token, '') != '')")
        if only_available:
            where.append("COALESCE(sora_enabled, 1) = 1")
            where.append("COALESCE(sora_quota_exhausted, 0) = 0")
        where_sql = " AND ".join(where) if where else "1=1"
        c.execute(
            f"""SELECT email, password, refresh_token, access_token, id_token, status, registered_at
                FROM accounts
                WHERE {where_sql}
                ORDER BY id DESC""",
            params
        )
        rows = c.fetchall()

    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["email", "password", "refresh_token", "access_token", "id_token", "status", "registered_at"])
        for r in rows:
            writer.writerow([r[0] or "", r[1] or "", r[2] or "", r[3] or "", r[4] or "", r[5] or "", r[6] or ""])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=sora2-accounts-{now}.csv"}
        )

    lines = []
    for r in rows:
        lines.append(sep.join([
            (r[0] or "").strip(),
            (r[1] or "").strip(),
            (r[2] or "").strip(),
            (r[3] or "").strip(),
            (r[4] or "").strip(),
        ]))
    body = "\n".join(lines)
    return StreamingResponse(
        iter([body]),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=sora2-accounts-{now}.txt"}
    )
