"""定时调度（多账号分时版）。

「分时发送」= 每天在基准时间（config.schedule_time）之上，为每个账号加上自己的
offset_minutes 偏移，各账号分别生成一个独立的定时任务，到点各自串行执行：

    账号A offset=0   -> 21:00 (+抖动) 发送
    账号B offset=20  -> 21:20 (+抖动) 发送
    账号C offset=45  -> 21:45 (+抖动) 发送

即使两个账号的时间意外重叠，core.lock 的跨进程文件锁也会让它们排队串行，
不会同时拉起两个 Chromium（内存峰值保持最低）。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import account_effective, account_time_text, get_account, load_config

logger = logging.getLogger("douyin-spark")
TZ = "Asia/Shanghai"

_scheduler: BackgroundScheduler | None = None
_run_func: Callable[[str], None] | None = None

# 任务ID用 "|" 分隔（账号名里可能带冒号），账号ID统一从 job.args[0] 取，避免解析歧义
DAILY_PREFIX = "daily|"
RETRY_PREFIX = "retry|"


def _job_id(account_id: str) -> str:
    return f"{DAILY_PREFIX}{account_id}"


def _retry_job_id(account_id: str) -> str:
    return f"{RETRY_PREFIX}{account_id}"


def _job_account(job) -> str | None:
    """从任务对象里取账号ID。"""
    args = getattr(job, "args", None) or ()
    if args and isinstance(args[0], str):
        return args[0]
    for prefix in (DAILY_PREFIX, RETRY_PREFIX):
        if job.id.startswith(prefix):
            return job.id[len(prefix):]
    return None


def _account_job(account_id: str) -> None:
    """到点：按该账号自己的抖动窗口随机延迟后触发发送。"""
    cfg = load_config()
    account = get_account(cfg, account_id)
    if account is None or not account.get("enabled", True):
        logger.info("[%s] 账号已停用或不存在，跳过本次定时发送", account_id)
        return

    eff = account_effective(account, cfg)
    jitter = max(0, int(eff.get("jitter_minutes") or 0))
    if jitter:
        delay = random.uniform(0, jitter * 60)
        logger.info("[%s] 随机延迟 %.0f 秒后开始发送（抖动窗口 %s 分钟）", account_id, delay, jitter)
        time.sleep(delay)
    if _run_func:
        _run_func(account_id)


def configure(run_func: Callable[[str], None]) -> None:
    global _scheduler, _run_func
    _run_func = run_func
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.start()
    apply_schedule()


def apply_schedule() -> None:
    """按当前配置重建所有账号的定时任务。"""
    if _scheduler is None:
        return

    cfg = load_config()
    base = str(cfg.get("schedule_time", "21:00"))
    accounts = cfg.get("accounts") or {}

    # 先清掉旧的每账号任务（含已删除的账号）
    for job in _scheduler.get_jobs():
        if job.id.startswith(DAILY_PREFIX) and job.id not in {_job_id(a) for a in accounts}:
            try:
                _scheduler.remove_job(job.id)
            except Exception:
                pass

    try:
        base_hh, base_mm = (int(x) for x in base.split(":"))
    except Exception:
        base_hh, base_mm = 21, 0

    enabled = 0
    for account_id, raw in accounts.items():
        account = get_account(cfg, account_id) or {}
        if not account.get("enabled", True):
            if _scheduler.get_job(_job_id(account_id)):
                try:
                    _scheduler.remove_job(_job_id(account_id))
                except Exception:
                    pass
            continue

        offset = int(account.get("offset_minutes", 0) or 0)
        total = base_hh * 60 + base_mm + offset
        total %= 24 * 60
        hour, minute = divmod(total, 60)

        _scheduler.add_job(
            _account_job,
            CronTrigger(hour=hour, minute=minute, timezone=TZ),
            args=[account_id],
            id=_job_id(account_id),
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        enabled += 1
        logger.info(
            "定时任务已更新：%s（%s）每天 %02d:%02d %s",
            account_id,
            account.get("label") or account_id,
            hour,
            minute,
            TZ,
        )

    if not enabled:
        logger.info("当前没有启用中的账号，定时发送不会执行（基准时间 %s %s）", base, TZ)


def next_run_times() -> dict:
    """账号ID -> 下次执行时间（ISO 字符串）。"""
    if _scheduler is None:
        return {}
    out: dict[str, str] = {}
    for job in _scheduler.get_jobs():
        if not job.id.startswith(DAILY_PREFIX) or not job.next_run_time:
            continue
        account_id = _job_account(job)
        if account_id:
            out[account_id] = job.next_run_time.isoformat()
    return out


def next_run_time() -> str | None:
    """全部账号中最早的下次执行时间（兼容旧接口）。"""
    times = sorted(next_run_times().values())
    return times[0] if times else None


def schedule_retry(account_id: str, run_func: Callable[[], None], delay_minutes: int = 45) -> None:
    """安排某个账号的当日补发。"""
    if _scheduler is None:
        return
    job_id = _retry_job_id(account_id)
    if _scheduler.get_job(job_id):
        return
    run_at = datetime.now() + timedelta(minutes=delay_minutes)
    _scheduler.add_job(
        run_func,
        DateTrigger(run_date=run_at, timezone=TZ),
        id=job_id,
        replace_existing=True,
    )
    logger.info("[%s] 已安排 %s 分钟后自动补发本次失败的好友", account_id, delay_minutes)


def cancel_retry(account_id: str | None = None) -> None:
    if _scheduler is None:
        return
    if account_id is None:
        for job in _scheduler.get_jobs():
            if job.id.startswith(RETRY_PREFIX):
                try:
                    _scheduler.remove_job(job.id)
                except Exception:
                    pass
        logger.info("已取消全部待执行的补发任务")
        return
    job_id = _retry_job_id(account_id)
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info("[%s] 已取消待执行的补发任务", account_id)


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def describe(cfg: dict) -> list[dict]:
    """给前端用的时间表：账号ID -> 展示用的发送时刻与下次执行时间。"""
    account_effective  # noqa: B018 - 保持与 config 的显式依赖关系
    times = next_run_times()
    out = []
    for account_id, raw in (cfg.get("accounts") or {}).items():
        account = get_account(cfg, account_id) or {}
        out.append({
            "account": account_id,
            "label": account.get("label") or account_id,
            "enabled": bool(account.get("enabled", True)),
            "offset_minutes": int(account.get("offset_minutes", 0) or 0),
            "send_time": account_time_text(cfg, account),
            "next_run": times.get(account_id),
        })
    return out
