"""Douyin Spark Keeper：多账号抖音续火花 Web 服务入口。

支持一次上传多个账号的登录态文件，每个账号有独立的好友名单、发送时间偏移与
好友间隔；定时任务按账号分别触发（分时发送），浏览器任务之间由文件锁串行排队。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _load_env() -> None:
    env_path = Path(os.environ.get("ENV_FILE") or (BASE_DIR / ".env"))
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


# 必须在导入 core 之前加载环境变量：core.config 的 DATA_DIR 在导入时读取，
# 这样 .env / ENV_FILE 里的 DATA_DIR 也能生效（否则只能靠真实环境变量）。
_load_env()

from core import automation, scheduler
from core.config import (
    DATA_DIR,
    account_effective,
    accounts_from_states,
    delete_account,
    get_account,
    is_valid_state,
    load_config,
    now_iso,
    parse_state_bytes,
    resolve_account,
    sanitize_account_id,
    save_config,
    state_path,
    unique_account_id,
    upsert_account,
)
from core.runtime import (
    account_log_file,
    load_account_history,
    load_runtime,
    recent_logs,
    record_contacts,
    record_run,
    set_queue,
    set_running,
    setup_logging,
    update_runtime,
)

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
logger = setup_logging()

run_lock = threading.Lock()
config_guard = threading.RLock()
run_queue: list[str] = []
queue_thread: threading.Thread | None = None
queue_guard = threading.Lock()
fetching_accounts: set[str] = set()
fetch_guard = threading.Lock()
upload_job: dict = {"running": False, "total": 0, "done": 0, "results": []}
upload_guard = threading.Lock()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _check_auth(token: str) -> None:
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="访问令牌不正确")


def _enabled_account_ids(cfg: dict | None = None) -> list[str]:
    cfg = cfg or load_config()
    return [a for a, item in (cfg.get("accounts") or {}).items() if (item or {}).get("enabled", True)]


def _set_fetching(account_id: str, value: bool) -> None:
    with fetch_guard:
        if value:
            fetching_accounts.add(account_id)
        else:
            fetching_accounts.discard(account_id)


def _set_upload_state(**fields) -> None:
    with upload_guard:
        upload_job.update(fields)


def _account_info(cfg: dict, account_id: str, rt: dict, next_runs: dict) -> dict:
    account = get_account(cfg, account_id) or {}
    eff = account_effective(account, cfg)
    last = (rt.get("last_runs") or {}).get(account_id)
    path = state_path(account_id)
    with fetch_guard:
        fetching = account_id in fetching_accounts
    return {
        "account": account_id,
        "label": account.get("label") or account_id,
        "enabled": bool(account.get("enabled", True)),
        "offset_minutes": int(account.get("offset_minutes", 0) or 0),
        "send_time": eff["base_time"],
        "next_run": next_runs.get(account_id),
        "friends": list(account.get("friends") or []),
        "friends_count": len(account.get("friends") or []),
        "messages": list(account.get("messages") or []),
        "gap_min": eff["gap_min"],
        "gap_max": eff["gap_max"],
        "jitter_minutes": eff["jitter_minutes"],
        "jitter_override": account.get("jitter_minutes"),
        "gap_min_override": account.get("send_gap_min"),
        "gap_max_override": account.get("send_gap_max"),
        "max_friends_per_run": eff["max_friends_per_run"],
        "max_friends_override": account.get("max_friends_per_run"),
        "original_filename": account.get("original_filename") or "",
        "uploaded_at": account.get("uploaded_at"),
        "state_file_exists": path.exists(),
        "state_size": path.stat().st_size if path.exists() else 0,
        "session_status": (last or {}).get("_status") or _status_of(rt, account_id, last),
        "last_run": last,
        "running": account_id in (rt.get("running_accounts") or []),
        "fetching": fetching,
        "contacts": (rt.get("contacts") or {}).get(account_id, {}).get("names", []),
        "contacts_at": (rt.get("contacts") or {}).get(account_id, {}).get("at"),
        "contacts_error": (rt.get("contacts") or {}).get(account_id, {}).get("error"),
    }


def _status_of(rt: dict, account_id: str, last: dict | None) -> str:
    if not last:
        if not state_path(account_id).exists():
            return "unknown"
        return "ready"
    if last.get("logged_out"):
        return "expired"
    if last.get("ok") and not last.get("failed"):
        return "ok"
    if last.get("ok"):
        return "partial"
    if not last.get("failed"):
        return "ok"
    return "failed"


def _all_accounts(rt: dict, next_runs: dict) -> list[dict]:
    """注册表 + data/states 里存在但未登记的登录态文件，一并返回。

    多个线程（定时任务 / 上传 / 轮询接口）都会读改写 config.json，
    所以整段「读-补登记-写」用一把进程内的锁串行化，避免互相覆盖。
    """
    with config_guard:
        cfg = load_config()
        before = set((cfg.get("accounts") or {}).keys())
        cfg = accounts_from_states(cfg)
        if set((cfg.get("accounts") or {}).keys()) != before:
            save_config(cfg)
    return [_account_info(cfg, account_id, rt, next_runs) for account_id in (cfg.get("accounts") or {})]


# --------------------------------------------------------------------------- #
# 运行队列（一次可以排队多个账号，浏览器任务之间严格串行）
#
# 分时发送由 core/scheduler.py 负责：每个账号在「基准时间 + 自己的 offset_minutes」
# 各自触发一次定时任务。手动点「立即发送」时则不再叠加偏移，立刻按队列顺序执行。
# 无论哪种方式，core/lock 的文件锁都保证同一时刻只有一个 Chromium 在跑。
# --------------------------------------------------------------------------- #

def _retry_run(account_id: str, names: list[str]) -> None:
    if not run_lock.acquire(blocking=False):
        logger.info("[%s] 补发时已有任务在运行，跳过本次补发", account_id)
        return
    try:
        set_running(True, account_id)
        result = automation.run_send(account_id, dry_run=False, only_names=names)
        record_run(result)
        logger.info("[%s] 补发完成：成功 %s 人，失败 %s 人", account_id, len(result.get("ok", [])), len(result.get("failed", [])))
    except Exception as e:
        logger.error("[%s] 补发异常: %s", account_id, e)
    finally:
        set_running(False, account_id)
        run_lock.release()


def _enqueue_accounts(account_ids: list[str], dry: bool) -> None:
    """把多个账号排入队列；同一时刻只有一个账号真正在跑浏览器。"""
    global queue_thread
    if dry:
        threading.Thread(target=_dry_run_many, args=(account_ids,), daemon=True).start()
        return
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行，请稍后再试")
    with queue_guard:
        run_queue.extend(account_ids)
        set_queue(list(run_queue))
        if queue_thread is None or not queue_thread.is_alive():
            queue_thread = threading.Thread(target=_queue_runner, daemon=True)
            queue_thread.start()
    run_lock.release()


def _queue_runner() -> None:
    """串行处理队列：同一时刻只有一个账号在跑浏览器。"""
    while True:
        with queue_guard:
            if not run_queue:
                break
            account_id = run_queue.pop(0)
        acquired = run_lock.acquire(blocking=False)
        if not acquired:
            # 有别的任务在跑，放回队列稍后重试
            with queue_guard:
                run_queue.insert(0, account_id)
            time.sleep(3)
            continue
        try:
            _run_one(account_id)
        finally:
            run_lock.release()


def _run_one(account_id: str) -> None:
    """执行一个账号的发送。手动/队列触发时立即执行，不再叠加分时偏移
    （偏移只用于每天的定时安排，见 core/scheduler.py）。"""
    cfg = load_config()
    account = get_account(cfg, account_id)
    if account is None:
        logger.warning("账号 %s 不存在，跳过", account_id)
        return
    set_running(True, account_id)
    try:
        result = automation.run_send(account_id, dry_run=False)
        record_run(result)
        logger.info(
            "[%s] 本次发送完成：成功 %s 人，失败 %s 人",
            account_id,
            len(result.get("ok", [])),
            len(result.get("failed", [])),
        )
        _maybe_schedule_retry(account_id, result)
    except Exception as e:
        logger.error("[%s] 运行异常: %s", account_id, e)
    finally:
        set_running(False, account_id)


def _maybe_schedule_retry(account_id: str, result: dict) -> None:
    from core.runtime import mark_retry_done, retry_done_today

    today = datetime.now().date().isoformat()
    failed_names = [
        f["name"]
        for f in result.get("failed", [])
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"] != "_system"
    ]
    if failed_names and not result.get("logged_out"):
        if retry_done_today(account_id, today):
            return
        mark_retry_done(account_id, today)
        scheduler.schedule_retry(account_id, lambda a=account_id, n=failed_names: _retry_run(a, n))
    elif not failed_names:
        scheduler.cancel_retry(account_id)


def _dry_run_many(account_ids: list[str]) -> None:
    """干跑：不真实发送，但同样串行，避免同时拉起多个浏览器。"""
    if not run_lock.acquire(blocking=False):
        logger.info("已有任务在运行，干跑测试未执行")
        return
    try:
        for account_id in account_ids:
            try:
                set_running(True, account_id)
                result = automation.run_send(account_id, dry_run=True)
                record_run(result)
                logger.info(
                    "[%s] 干跑完成：可发送 %s 人，失败 %s 人",
                    account_id,
                    len(result.get("ok", [])),
                    len(result.get("failed", [])),
                )
            except Exception as e:
                logger.error("[%s] 干跑异常: %s", account_id, e)
            finally:
                set_running(False, account_id)
    finally:
        run_lock.release()
        set_queue([])


def _start_fetch_contacts(account_id: str) -> None:
    def worker() -> None:
        _set_fetching(account_id, True)
        try:
            result = automation.fetch_chat_contacts(account_id)
            record_contacts(result)
            if result.get("error"):
                logger.warning("[%s] 获取聊天列表失败：%s", account_id, result["error"])
            else:
                logger.info("[%s] 获取聊天列表成功：%s 个联系人", account_id, len(result.get("names", [])))
        finally:
            _set_fetching(account_id, False)

    threading.Thread(target=worker, daemon=True).start()


# --------------------------------------------------------------------------- #
# 上传处理
# --------------------------------------------------------------------------- #

def _slug_from_filename(filename: str) -> str:
    stem = Path(filename or "").stem
    stem = re.sub(r"^state[_\-\s]*", "", stem, flags=re.IGNORECASE)
    return sanitize_account_id(stem) or "account"


def _process_uploads(items: list[tuple[str, bytes]]) -> list[dict]:
    """保存一批登录态文件并登记账号。items = [(文件名, 原始字节), ...]"""
    results: list[dict] = []
    with config_guard:
        cfg = load_config()
        for filename, raw in items:
            entry = {"filename": filename, "ok": False, "account": None, "message": ""}
            if len(raw) > MAX_UPLOAD_BYTES:
                entry["message"] = "文件过大（超过 10MB）"
                results.append(entry)
                continue
            try:
                data = parse_state_bytes(raw)
            except Exception:
                data = None
            if not is_valid_state(data):
                entry["message"] = "缺少 cookies 字段，请确认是 Playwright 导出的登录态文件"
                results.append(entry)
                continue

            try:
                slug = _slug_from_filename(filename)
                account_id = unique_account_id(cfg, slug)
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                path = state_path(account_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                # 重新序列化，去掉可能存在的 BOM，保证 Playwright 能正常读取
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                upsert_account(
                    cfg,
                    account_id,
                    {
                        "label": Path(filename).stem or account_id,
                        "original_filename": filename,
                        "uploaded_at": now_iso(),
                    },
                    create=True,
                )
                save_config(cfg)
                entry.update({"ok": True, "account": account_id, "size": len(raw)})
                entry["message"] = f"已保存为账号「{account_id}」（登录态 {len(raw)} 字节）"
                logger.info("[%s] 登录态已上传：%s（%s 字节）", account_id, filename, len(raw))
            except Exception as e:
                entry["message"] = f"保存失败: {e}"
            results.append(entry)
    return results


def _start_upload(items: list[tuple[str, bytes]]) -> None:
    def worker() -> None:
        _set_upload_state(running=True, total=len(items), done=0, results=[])
        try:
            results = _process_uploads(items)
            _set_upload_state(running=False, done=len(items), results=results)
            scheduler.apply_schedule()
        except Exception as e:
            logger.error("上传处理异常: %s", e)
            _set_upload_state(running=False, results=[{"filename": "?", "ok": False, "message": f"处理异常: {e}"}])

    threading.Thread(target=worker, daemon=True).start()


# --------------------------------------------------------------------------- #
# FastAPI 应用
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        scheduler.configure(lambda account_id: _enqueue_accounts([account_id], dry=False))
        cfg = load_config()
        if accounts_from_states(cfg) is not cfg:
            save_config(cfg)
    except Exception as e:  # pragma: no cover
        logger.warning("调度器启动失败: %s", e)
    yield
    scheduler.shutdown()


app = FastAPI(title="Douyin Spark Keeper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ConfigBody(BaseModel):
    config: dict


class RunBody(BaseModel):
    dry: bool = False
    accounts: list[str] | None = None


class AccountPatch(BaseModel):
    account: dict


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def api_status(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    next_runs = scheduler.next_run_times()
    accounts = _all_accounts(rt, next_runs)
    uploading = upload_job
    return {
        "running": bool(rt.get("running", False)),
        "running_accounts": rt.get("running_accounts") or [],
        "next_run": scheduler.next_run_time(),
        "auth_required": bool(AUTH_TOKEN),
        "version": "0.2.0",
        "accounts": accounts,
        "account_count": len(accounts),
        "enabled_count": len([a for a in accounts if a["enabled"]]),
        "session_status": _overall_status(accounts),
        "last_run": rt.get("last_run"),
        "history_count": len(rt.get("history", [])),
        "contacts_fetching": sorted(fetching_accounts),
        "upload": {
            "running": bool(uploading.get("running")),
            "total": int(uploading.get("total") or 0),
            "done": int(uploading.get("done") or 0),
            "results": uploading.get("results") or [],
        },
    }


def _overall_status(accounts: list[dict]) -> str:
    if not accounts:
        return "unknown"
    order = {"expired": 4, "failed": 3, "partial": 2, "ok": 1, "ready": 0, "unknown": 0}
    return max((a.get("session_status", "unknown") for a in accounts), key=lambda s: order.get(s, 0))


@app.get("/api/config")
def api_config(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    cfg = load_config()
    return {
        "schedule_time": cfg.get("schedule_time", "21:00"),
        "jitter_minutes": cfg.get("jitter_minutes", 30),
        "send_gap_min": cfg.get("send_gap_min", 6),
        "send_gap_max": cfg.get("send_gap_max", 12),
        "max_friends_per_run": cfg.get("max_friends_per_run", 20),
        "messages": cfg.get("messages", []),
        "accounts": cfg.get("accounts", {}),
    }


@app.put("/api/config")
def api_config_save(body: ConfigBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """保存全局设置。请求体没带 accounts 时保留现有账号，避免误清空。"""
    _check_auth(token)
    incoming = dict(body.config or {})
    if "accounts" not in incoming:
        incoming["accounts"] = load_config().get("accounts") or {}
    try:
        cfg = save_config(incoming)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule()
    return {"ok": True, "config": cfg}


# ----------------------------- 账号 ----------------------------- #

@app.get("/api/accounts")
def api_accounts(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    return {"accounts": _all_accounts(rt, scheduler.next_run_times())}


@app.put("/api/accounts/{account_id}")
def api_account_update(
    account_id: str,
    body: AccountPatch,
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    cfg = load_config()
    real_id = resolve_account(cfg, account_id)
    if real_id is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    patch = body.account or {}
    messages = patch.get("messages")
    if isinstance(messages, list):
        patch["messages"] = [str(m).strip() for m in messages if str(m).strip()]
    try:
        upsert_account(cfg, real_id, patch)
        saved = save_config(cfg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule()
    return {"ok": True, "account": get_account(saved, real_id)}


@app.delete("/api/accounts/{account_id}")
def api_account_delete(account_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    cfg = load_config()
    real_id = resolve_account(cfg, account_id)
    if real_id is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    delete_account(cfg, real_id)
    save_config(cfg)
    scheduler.apply_schedule()
    scheduler.cancel_retry(real_id)
    logger.info("已删除账号 %s（含登录态文件）", real_id)
    return {"ok": True}


@app.post("/api/accounts/{account_id}/enabled")
def api_account_enabled(
    account_id: str,
    enabled: bool = Form(...),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    cfg = load_config()
    real_id = resolve_account(cfg, account_id)
    if real_id is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    upsert_account(cfg, real_id, {"enabled": bool(enabled)})
    save_config(cfg)
    scheduler.apply_schedule()
    return {"ok": True, "enabled": bool(enabled)}


@app.get("/api/accounts/{account_id}/history")
def api_account_history(account_id: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    cfg = load_config()
    real_id = resolve_account(cfg, account_id)
    if real_id is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    return load_account_history(real_id)


@app.get("/api/accounts/{account_id}/logs")
def api_account_logs(
    account_id: str,
    n: int = 300,
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    cfg = load_config()
    real_id = resolve_account(cfg, account_id)
    if real_id is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    limit = max(10, min(n, 800))
    lines = account_log_file(real_id, limit) or recent_logs(limit, real_id)
    return {"account": real_id, "logs": "\n".join(lines)}


@app.get("/api/contacts")
def api_contacts(
    account: str = "",
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    rt = load_runtime()
    cfg = load_config()
    real_id = resolve_account(cfg, account) if account else None
    if account and real_id is None:
        raise HTTPException(status_code=404, detail=f"账号 {account} 不存在")
    contacts = rt.get("contacts") or {}
    with fetch_guard:
        fetching = sorted(fetching_accounts)
    if real_id:
        item = contacts.get(real_id, {})
        return {
            "account": real_id,
            "contacts": item.get("names", []),
            "contacts_at": item.get("at"),
            "contacts_error": item.get("error"),
            "fetching": real_id in fetching_accounts,
        }
    return {
        "account": None,
        "contacts": {key: value.get("names", []) for key, value in contacts.items()},
        "contacts_at": {key: value.get("at") for key, value in contacts.items()},
        "contacts_error": {key: value.get("error") for key, value in contacts.items()},
        "fetching": fetching,
    }


@app.post("/api/contacts/fetch")
def api_contacts_fetch(
    account: str = Form(...),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    cfg = load_config()
    real_id = resolve_account(cfg, account)
    if real_id is None:
        raise HTTPException(status_code=404, detail=f"账号 {account} 不存在")
    if not state_path(real_id).exists():
        raise HTTPException(status_code=400, detail=f"账号 {real_id} 尚未上传登录态文件")
    with fetch_guard:
        already = real_id in fetching_accounts
    if not already:
        _start_fetch_contacts(real_id)
    return {"ok": True, "started": True, "account": real_id}


@app.post("/api/run")
def api_run(body: RunBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    cfg = load_config()
    accounts = [a for a in (body.accounts or []) if a]
    if accounts:
        resolved = []
        for account_id in accounts:
            real_id = resolve_account(cfg, account_id)
            if real_id is None:
                raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
            resolved.append(real_id)
    else:
        resolved = _enabled_account_ids(cfg)
        if not resolved:
            raise HTTPException(status_code=400, detail="没有启用的账号，请先上传登录态并启用账号")
    try:
        _enqueue_accounts(resolved, bool(body.dry))
    except HTTPException:
        raise
    return {"ok": True, "started": True, "accounts": resolved}


# ----------------------------- 上传 ----------------------------- #

@app.post("/api/upload-states")
async def api_upload_states(
    files: list[UploadFile] = File(...),
    ids: str = Form(default=""),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    """一次上传多个账号的登录态文件；ids 可选（逗号分隔，与文件顺序对应）。"""
    _check_auth(token)
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")
    if upload_job.get("running"):
        raise HTTPException(status_code=409, detail="上一批文件仍在处理，请稍候")

    id_list = [sanitize_account_id(x) for x in str(ids).split(",")] if ids else []
    items: list[tuple[str, bytes]] = []
    for index, upload in enumerate(files):
        raw = await upload.read()
        filename = upload.filename or f"state_{index + 1}.json"
        if index < len(id_list) and id_list[index]:
            filename = f"{id_list[index]}.json"
        items.append((filename, raw))

    _start_upload(items)
    return {"ok": True, "started": True, "count": len(items)}


@app.post("/api/upload-state")
async def api_upload_state(
    file: UploadFile = File(...),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    """兼容旧接口：单个登录态文件，默认登记为「默认账号」。"""
    _check_auth(token)
    raw = await file.read()
    results = _process_uploads([(file.filename or "state.json", raw)])
    entry = results[0] if results else {"ok": False, "message": "未处理"}
    if not entry.get("ok"):
        raise HTTPException(status_code=400, detail=entry.get("message") or "上传失败")
    scheduler.apply_schedule()
    return {"ok": True, "account": entry["account"], "size": entry.get("size", len(raw))}


# ----------------------------- 日志 ----------------------------- #

@app.get("/api/logs")
def api_logs(
    n: int = 300,
    account: str = "",
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    cfg = load_config()
    real_id = resolve_account(cfg, account) if account else None
    limit = max(10, min(n, 800))
    if real_id:
        lines = account_log_file(real_id, limit) or recent_logs(limit, real_id)
    else:
        lines = recent_logs(limit)
    return {"logs": "\n".join(lines), "account": real_id}


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run(app, host=host, port=port, log_level="info")
