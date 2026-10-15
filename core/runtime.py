"""运行状态与日志（多账号版）。

- 全局状态：data/runtime.json（最近一次运行汇总 + 每个账号的最近结果）
- 每个账号的历史：data/runtime/<账号ID>.json（最近 30 次）
- 日志：data/logs/app.log 全量；data/logs/<账号ID>.log 里只放该账号的行
  （日志文本统一带 [账号ID] 标记，RingHandler 按标记分流）
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from pathlib import Path

from .config import DATA_DIR, account_log_path, read_json

RUNTIME_PATH = DATA_DIR / "runtime.json"
ACCOUNT_RUNTIME_DIR = DATA_DIR / "runtime"
LOG_DIR = DATA_DIR / "logs"

HISTORY_LIMIT = 30
_ACCOUNT_RE = re.compile(r"\[([^\[\]]+?)\]\s")

_lock = threading.Lock()
_ring: deque[str] = deque(maxlen=800)


def _default() -> dict:
    return {
        "session_status": "unknown",
        "running": False,
        "running_accounts": [],
        "last_run": None,
        "last_runs": {},        # 账号ID -> 最近一次结果
        "retry_dates": {},      # 账号ID -> 已补发过的日期 YYYY-MM-DD
        "history": [],
    }


# --------------------------------------------------------------------------- #
# 全局 runtime
# --------------------------------------------------------------------------- #

def load_runtime() -> dict:
    rt = _default()
    if RUNTIME_PATH.exists():
        data = read_json(RUNTIME_PATH)
        if isinstance(data, dict):
            rt.update(data)
    for key in ("last_runs", "retry_dates"):
        if not isinstance(rt.get(key), dict):
            rt[key] = {}
    if not isinstance(rt.get("running_accounts"), list):
        rt["running_accounts"] = []
    return rt


def _save(rt: dict) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_PATH.write_text(json.dumps(rt, ensure_ascii=False, indent=2), encoding="utf-8")


def update_runtime(**fields) -> None:
    rt = load_runtime()
    rt.update(fields)
    _save(rt)


def _status_from(result: dict) -> str:
    if result.get("logged_out"):
        return "expired"
    if result.get("ok") and not result.get("failed"):
        return "ok"
    if result.get("ok"):
        return "partial"
    if not result.get("failed"):
        return "ok"
    return "failed"


def set_running(value: bool, account_id: str | None = None, queue: list[str] | None = None) -> None:
    """更新运行标记。account_id 为空表示整体状态。"""
    rt = load_runtime()
    if account_id is None:
        rt["running"] = bool(value)
        if not value:
            rt["running_accounts"] = []
        elif queue is not None:
            rt["running_accounts"] = list(queue)
    else:
        running = set(rt.get("running_accounts") or [])
        if value:
            running.add(account_id)
        else:
            running.discard(account_id)
        rt["running_accounts"] = sorted(running)
        rt["running"] = bool(running)
    _save(rt)


def set_queue(account_ids: list[str]) -> None:
    """队列状态：非空时 running 一直为 True（含两个账号之间的空档）。"""
    rt = load_runtime()
    rt["running_accounts"] = list(account_ids)
    rt["running"] = bool(account_ids)
    _save(rt)


def queue_active() -> bool:
    rt = load_runtime()
    return bool(rt.get("running")) and bool(rt.get("running_accounts"))


# --------------------------------------------------------------------------- #
# 单账号历史
# --------------------------------------------------------------------------- #

def _account_path(account_id: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(account_id))
    return ACCOUNT_RUNTIME_DIR / f"{safe}.json"


def load_account_history(account_id: str) -> dict:
    data = {"account": account_id, "history": [], "last_run": None}
    path = _account_path(account_id)
    if path.exists():
        loaded = read_json(path)
        if isinstance(loaded, dict):
            data.update(loaded)
    if not isinstance(data.get("history"), list):
        data["history"] = []
    return data


def _save_account_history(account_id: str, data: dict) -> None:
    with _lock:
        ACCOUNT_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _account_path(account_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# --------------------------------------------------------------------------- #
# 记录运行结果
# --------------------------------------------------------------------------- #

def record_run(result: dict) -> None:
    """记录一次运行结果。支持单账号结果与多账号汇总结果。"""
    rt = load_runtime()

    if result.get("multiple"):
        runs = [r for r in result.get("runs", []) if isinstance(r, dict)]
        for run in runs:
            _record_one(rt, run, save=False)
        overall = dict(result)
        overall.pop("runs", None)
        rt["last_run"] = overall
        rt["session_status"] = _status_from(overall)
        running = set(rt.get("running_accounts") or []) - {r.get("account") for r in runs}
        rt["running_accounts"] = sorted(running)
        rt["running"] = bool(running)
        history = rt.get("history", [])
        history.insert(0, overall)
        rt["history"] = history[:HISTORY_LIMIT]
        _save(rt)
        return

    _record_one(rt, result, save=True)


def _record_one(rt: dict, result: dict, *, save: bool) -> None:
    account_id = result.get("account")
    if not account_id:
        rt["last_run"] = result
        rt["session_status"] = _status_from(result)
        if save:
            _save(rt)
        return

    rt.setdefault("last_runs", {})[account_id] = result
    history = load_account_history(account_id)
    history["last_run"] = result
    runs = history.get("history", [])
    runs.insert(0, result)
    history["history"] = runs[:HISTORY_LIMIT]
    _save_account_history(account_id, history)

    # 队列里还有别的账号时保持整体 running=True，避免前端以为任务已结束
    remaining = [a for a in (rt.get("running_accounts") or []) if a != account_id]
    if remaining:
        rt["running_accounts"] = remaining
        rt["running"] = True
    else:
        rt["running_accounts"] = []
        rt["running"] = False

    rt["last_run"] = result
    rt["session_status"] = _status_from(result)
    if save:
        _save(rt)


def mark_retry_done(account_id: str, day: str) -> None:
    rt = load_runtime()
    dates = rt.setdefault("retry_dates", {})
    dates[account_id] = day
    _save(rt)


def retry_done_today(account_id: str, day: str) -> bool:
    rt = load_runtime()
    return (rt.get("retry_dates") or {}).get(account_id) == day


# --------------------------------------------------------------------------- #
# 联系人（按账号存进账号配置由 app 层负责，这里保存读取结果快照）
# --------------------------------------------------------------------------- #

def record_contacts(data: dict) -> None:
    account_id = data.get("account")
    if not account_id:
        return
    rt = load_runtime()
    contacts = rt.setdefault("contacts", {})
    if not isinstance(contacts, dict):
        contacts = {}
        rt["contacts"] = contacts
    contacts[account_id] = {
        "names": data.get("names", []),
        "at": data.get("at"),
        "error": data.get("error"),
    }
    _save(rt)


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

def _account_from_record(record: logging.LogRecord) -> str | None:
    try:
        message = record.getMessage()
    except Exception:
        return None
    match = _ACCOUNT_RE.search(message)
    if not match:
        return None
    return match.group(1)


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _ring.append(self.format(record))
        except Exception:
            pass


class PerAccountFileHandler(logging.Handler):
    """把带 [账号ID] 标记的日志行额外写进 data/logs/<账号ID>.log。"""

    def __init__(self) -> None:
        super().__init__()
        self._files: dict[str, logging.FileHandler] = {}
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        account_id = _account_from_record(record)
        if not account_id:
            return
        try:
            handler = self._handler(account_id)
            handler.emit(record)
        except Exception:
            pass

    def _handler(self, account_id: str) -> logging.FileHandler:
        with self._guard:
            handler = self._files.get(account_id)
            if handler is None:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                handler = logging.FileHandler(account_log_path(account_id), encoding="utf-8")
                handler.setFormatter(self.formatter)
                self._files[account_id] = handler
            elif handler.formatter is None:
                handler.setFormatter(self.formatter)
            return handler


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("douyin-spark")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    rh = RingHandler()
    rh.setFormatter(fmt)
    logger.addHandler(rh)

    ph = PerAccountFileHandler()
    ph.setFormatter(fmt)
    logger.addHandler(ph)
    return logger


def recent_logs(n: int = 300, account_id: str | None = None) -> list[str]:
    lines = list(_ring)
    if account_id:
        lines = [line for line in lines if f"[{account_id}]" in line]
    return lines[-n:]


def account_log_file(account_id: str, n: int = 300) -> list[str]:
    """从该账号的日志文件尾部读取 n 行（进程重启后仍能看到历史）。"""
    path = account_log_path(account_id)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in f.readlines()[-n:]]
    except Exception:
        return []
