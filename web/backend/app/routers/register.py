"""
开启注册：POST /api/register/start 启动调度，GET /api/register/status 查询状态与心跳。
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.registration_state import set_stop_requested, is_stop_requested
from app.routers.auth import get_current_user
from app.database import get_db, init_db
from app.services.registration_runner import (
    _get_registration_settings,
    fetch_unregistered_emails,
    run_one_task,
)


def _log_run(task_id: str, level: str, message: str):
    try:
        with get_db() as conn:
            c = conn.cursor()
            created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c.execute(
                "INSERT INTO run_logs (task_id, level, message, created_at) VALUES (?, ?, ?, ?)",
                (task_id, level, message, created),
            )
    except Exception:
        pass

router = APIRouter(prefix="/api/register", tags=["register"])

_registration_running = False
_registration_heartbeat: str | None = None
_registration_lock = threading.Lock()


def _run_registration_loop():
    """后台线程：按 thread_count 并发取未注册邮箱并执行，写心跳到 system_settings。"""
    global _registration_running, _registration_heartbeat
    task_id = f"register_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    settings = _get_registration_settings()
    thread_count = max(1, min(32, int(settings.get("thread_count") or "1")))
    init_db()

    def _email_key(row) -> str:
        if not row or len(row) < 2 or not row[1]:
            return ""
        return row[1].strip().lower()

    def _update_heartbeat():
        global _registration_heartbeat
        with _registration_lock:
            _registration_heartbeat = datetime.utcnow().isoformat() + "Z"
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES ('last_registration_heartbeat', ?)",
                (_registration_heartbeat,),
            )

    try:
        _log_run(task_id, "info", "注册任务已启动")
        attempted_ids_this_run = set()  # 本 run 内已提交处理过的邮箱 ID，不再重复拉取
        attempted_emails_this_run = set()  # 兜底按邮箱去重，避免大小写或重复数据导致重复跑
        while True:
            if is_stop_requested():
                _log_run(task_id, "info", "已请求停止，立即结束")
                break
            raw_batch = fetch_unregistered_emails(limit=thread_count)
            batch = [
                row for row in raw_batch
                if row
                and row[0] not in attempted_ids_this_run
                and _email_key(row) not in attempted_emails_this_run
            ]
            if not batch:
                if raw_batch:
                    _log_run(task_id, "info", "本轮剩余邮箱均已在当前任务处理过，结束当前注册任务")
                else:
                    _log_run(task_id, "info", "注册任务结束，无更多未注册邮箱")
                break
            for row in batch:
                attempted_ids_this_run.add(row[0])
                email_key = _email_key(row)
                if email_key:
                    attempted_emails_this_run.add(email_key)
            _update_heartbeat()
            _log_run(task_id, "info", f"本批开始注册 共 {len(batch)} 条")
            ex = ThreadPoolExecutor(max_workers=min(len(batch), thread_count))
            futures = {
                ex.submit(run_one_task, task_id, settings, email_row=row): row
                for row in batch
            }
            stopped_early = False
            done_iterator = as_completed(futures, timeout=1.0)
            num_futures = len(futures)
            num_done = 0
            try:
                while num_done < num_futures:
                    try:
                        fut = next(done_iterator)
                    except FuturesTimeoutError:
                        if is_stop_requested():
                            _log_run(task_id, "info", "已请求停止，立即结束当前批次")
                            stopped_early = True
                            break
                        _update_heartbeat()
                        continue
                    except StopIteration:
                        break
                    if is_stop_requested():
                        _log_run(task_id, "info", "已请求停止，立即结束当前批次")
                        stopped_early = True
                        break
                    num_done += 1
                    try:
                        fut.result()
                    except Exception:
                        pass
                    _update_heartbeat()
            finally:
                ex.shutdown(wait=not stopped_early)
            if stopped_early:
                break
    finally:
        try:
            _log_run(task_id, "info", "注册调度已退出")
        except Exception:
            pass
        with _registration_lock:
            _registration_running = False
        set_stop_requested(False)


@router.post("/start")
def start_registration(username: str = Depends(get_current_user)):
    """启动一次注册任务（后台调度直到无未注册邮箱）。若已在运行则返回 409。"""
    global _registration_running
    with _registration_lock:
        if _registration_running:
            return {"ok": False, "message": "注册任务已在运行中"}
        _registration_running = True
    set_stop_requested(False)
    t = threading.Thread(target=_run_registration_loop, daemon=True)
    t.start()
    return {"ok": True, "message": "已启动注册任务"}


@router.post("/stop")
def stop_registration(username: str = Depends(get_current_user)):
    """请求停止注册任务（调度与进行中的任务都会尽快退出）。"""
    global _registration_running
    with _registration_lock:
        if not _registration_running:
            return {"ok": False, "message": "当前无运行中的注册任务"}
        _registration_running = False
    set_stop_requested(True)
    return {"ok": True, "message": "已请求停止，正在立即结束"}


def _parse_heartbeat_time(s: str | None):
    """解析 ISO 心跳时间，失败返回 None。"""
    if not s or not isinstance(s, str):
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


# 心跳超过此分钟数仍视为任务已死，返回 running: false
_STATUS_HEARTBEAT_DEAD_MINUTES = 5


@router.get("/status")
def get_registration_status(username: str = Depends(get_current_user)):
    """返回是否运行中、最近心跳时间、last_run_success/fail。running 来自本进程的 _registration_running；若心跳超过 5 分钟则强制视为已停止。"""
    with _registration_lock:
        running = _registration_running
        heartbeat = _registration_heartbeat
    init_db()
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT key, value FROM system_settings WHERE key IN ('last_run_success', 'last_run_fail', 'last_registration_heartbeat')"
        )
        rows = c.fetchall()
    kv = {r[0]: r[1] for r in rows}
    last_heartbeat = heartbeat or kv.get("last_registration_heartbeat")
    # 若认为在运行但心跳超时，视为已停止（避免重启/异常后一直显示正在注册）
    if running and last_heartbeat:
        ht = _parse_heartbeat_time(last_heartbeat)
        if ht:
            now = datetime.now(timezone.utc)
            if ht.tzinfo is None:
                ht = ht.replace(tzinfo=timezone.utc)
            if (now - ht).total_seconds() > _STATUS_HEARTBEAT_DEAD_MINUTES * 60:
                running = False
    return {
        "running": running,
        "last_heartbeat": last_heartbeat,
        "last_run_success": int(kv.get("last_run_success") or 0),
        "last_run_fail": int(kv.get("last_run_fail") or 0),
    }
