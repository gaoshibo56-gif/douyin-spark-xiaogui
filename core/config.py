"""配置读写（多账号版）。

结构说明：一个「账号」= 一份 Playwright 登录态文件 + 一套独立配置。
    data/states/<账号ID>.json   登录态（storage_state）
    data/config.json            {
                                  "schedule_time": "21:00",     # 每天基准发送时间
                                  "jitter_minutes": 30,         # 基准时间后的随机抖动窗口
                                  "accounts": { "<账号ID>": {...} }
                                }
账号配置项：
    label                     显示名（默认取上传的文件名）
    enabled                   是否参与定时发送
    offset_minutes            相对基准时间的偏移分钟数（分时发送的核心）
    jitter_minutes            本账号独立的抖动窗口（None 表示沿用全局值）
    send_gap_min/max          本账号好友之间的随机间隔（秒）
    max_friends_per_run       本账号每次最多发送人数（0 = 不限）
    friends                   好友名单
    messages                  文案模板（未配置则沿用全局 messages）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve_data_dir() -> Path:
    """数据目录：优先取 DATA_DIR 环境变量，多实例共用一个代码目录时各自指定。"""
    raw = os.environ.get("DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return BASE_DIR / "data"


DATA_DIR = _resolve_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
STATES_DIR = DATA_DIR / "states"
LEGACY_STATE_PATH = DATA_DIR / "state.json"
PHOTOS_DIR = DATA_DIR / "photos"
LOG_DIR = DATA_DIR / "logs"

DEFAULT_CONFIG = {
    "schedule_time": "21:00",   # 每天基准发送时间 HH:MM（服务器时区 Asia/Shanghai），各账号在此之上加偏移
    "jitter_minutes": 30,       # 全局抖动窗口：实际在 [基准时间+偏移, +抖动] 内随机开始
    "send_gap_min": 6,          # 相邻两个好友之间的最小间隔（秒）
    "send_gap_max": 12,         # 相邻两个好友之间的最大间隔（秒）
    "max_friends_per_run": 20,  # 每次最多发送的好友数（0 表示不限制）
    "friends": [],              # 旧版单账号好友列表（仅自动迁移时使用）
    "messages": ["🔥 续火花", "晚安，明天见", "今天也要开心哦"],
    "accounts": {},             # 账号ID -> 账号配置
}

DEFAULT_ACCOUNT = {
    "label": "",
    "enabled": True,
    "offset_minutes": 0,
    "jitter_minutes": None,       # None = 沿用全局 jitter_minutes
    "send_gap_min": None,         # None = 沿用全局
    "send_gap_max": None,         # None = 沿用全局
    "max_friends_per_run": None,  # None = 沿用全局
    "friends": [],
    "messages": [],               # 空 = 沿用全局 messages
    "original_filename": "",
    "uploaded_at": None,
}

_lock = threading.Lock()
_migrated = False

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{1,2})$")


# --------------------------------------------------------------------------- #
# 账号ID / 文件路径
# --------------------------------------------------------------------------- #

def sanitize_account_id(raw: str) -> str:
    """把用户提供的名字转成安全的账号ID（允许中文，去掉路径分隔符等危险字符）。"""
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._")
    if len(text) > 60:
        text = text[:60]
    return text


def state_path(account_id: str) -> Path:
    return STATES_DIR / f"{sanitize_account_id(account_id)}.json"


def account_photo_path(account_id: str) -> Path:
    return PHOTOS_DIR / f"{sanitize_account_id(account_id)}.png"


def account_log_path(account_id: str) -> Path:
    return LOG_DIR / f"{sanitize_account_id(account_id)}.log"


# --------------------------------------------------------------------------- #
# 账号配置
# --------------------------------------------------------------------------- #

def _to_int(value, default: int, low: int | None = None, high: int | None = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    if low is not None:
        out = max(low, out)
    if high is not None:
        out = min(high, out)
    return out


def _to_optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_account(acc: dict | None, key: str = "") -> dict:
    merged = dict(DEFAULT_ACCOUNT)
    if isinstance(acc, dict):
        merged.update(acc)

    merged["label"] = str(merged.get("label") or key or "")
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["original_filename"] = str(merged.get("original_filename") or "")
    merged["friends"] = [str(x).strip() for x in (merged.get("friends") or []) if str(x).strip()]
    merged["messages"] = [str(x) for x in (merged.get("messages") or []) if str(x).strip()]

    merged["offset_minutes"] = _to_int(merged.get("offset_minutes"), 0, low=-1440, high=1440)

    for name in ("send_gap_min", "send_gap_max", "max_friends_per_run", "jitter_minutes"):
        merged[name] = _to_optional_int(merged.get(name))
    if merged["send_gap_min"] is not None:
        merged["send_gap_min"] = max(1, merged["send_gap_min"])
    if merged["send_gap_max"] is not None:
        merged["send_gap_max"] = max(1, merged["send_gap_max"])
    if (
        merged["send_gap_min"] is not None
        and merged["send_gap_max"] is not None
        and merged["send_gap_max"] < merged["send_gap_min"]
    ):
        merged["send_gap_max"] = merged["send_gap_min"]
    return merged


def get_account(cfg: dict, account_id: str) -> dict | None:
    """取账号配置（含默认值填充）。账号不存在返回 None。"""
    accounts = cfg.get("accounts") or {}
    if account_id not in accounts:
        return None
    return _normalize_account(accounts.get(account_id), account_id)


def resolve_account(cfg: dict, account_id: str) -> str | None:
    """宽松匹配账号ID（允许大小写差异），返回真实 ID 或 None。"""
    accounts = cfg.get("accounts") or {}
    if account_id in accounts:
        return account_id
    lowered = str(account_id or "").strip().lower()
    for key in accounts:
        if key.lower() == lowered:
            return key
    return None


def account_effective(account: dict, cfg: dict) -> dict:
    """把账号配置与全局默认值合并成运行时实际参数。"""
    gap_min = account.get("send_gap_min")
    gap_max = account.get("send_gap_max")
    gap_min = int(cfg.get("send_gap_min", 6) or 6) if gap_min is None else int(gap_min)
    gap_max = int(cfg.get("send_gap_max", 12) or 12) if gap_max is None else int(gap_max)
    gap_min = max(1, gap_min)
    gap_max = max(gap_min, gap_max)

    max_n = account.get("max_friends_per_run")
    max_n = int(cfg.get("max_friends_per_run", 20) or 0) if max_n is None else int(max_n)

    jitter = account.get("jitter_minutes")
    jitter = int(cfg.get("jitter_minutes", 30) or 0) if jitter is None else int(jitter)

    messages = account.get("messages") or cfg.get("messages") or ["🔥"]
    offset = int(account.get("offset_minutes", 0) or 0)

    return {
        "gap_min": gap_min,
        "gap_max": gap_max,
        "max_friends_per_run": max_n,
        "jitter_minutes": max(0, jitter),
        "messages": [str(m) for m in messages if str(m).strip()] or ["🔥"],
        "friends": list(account.get("friends") or []),
        "offset_minutes": offset,
        "enabled": bool(account.get("enabled", True)),
        "label": account.get("label") or "",
        "base_time": _format_minutes(_parse_schedule(cfg.get("schedule_time", "21:00")) + offset),
    }


def account_time_text(cfg: dict, account: dict) -> str:
    """该账号的基准发送时刻（HH:MM，已含分时偏移），供界面展示与日志使用。"""
    try:
        base = _parse_schedule(cfg.get("schedule_time", "21:00"))
    except ValueError:
        base = 21 * 60
    return _format_minutes(base + int((account or {}).get("offset_minutes", 0) or 0))


def _parse_schedule(value: str) -> int:
    match = _HHMM_RE.match(str(value).strip())
    if not match:
        raise ValueError("schedule_time 必须是 HH:MM 格式")
    hh, mm = int(match.group(1)), int(match.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("schedule_time 必须是 HH:MM 格式")
    return hh * 60 + mm


def _format_minutes(total: int) -> str:
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    global _migrated
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        data = read_json(CONFIG_PATH)
        if data:
            cfg.update(data)
    if not isinstance(cfg.get("accounts"), dict):
        cfg["accounts"] = {}
    cfg["accounts"] = {
        key: _normalize_account(value, key)
        for key, value in cfg["accounts"].items()
        if isinstance(key, str) and key.strip()
    }
    if not _migrated:
        _migrated = True
        try:
            if migrate_legacy(cfg):
                cfg = load_config()  # 迁移完成后重新加载一次
        except Exception:
            pass
    return cfg


def save_config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_CONFIG)
    if cfg:
        merged.update(cfg)

    merged["messages"] = [str(x) for x in merged.get("messages", []) if str(x).strip()]
    if not merged["messages"]:
        merged["messages"] = ["🔥"]

    merged["schedule_time"] = _format_minutes(_parse_schedule(merged.get("schedule_time", "21:00")))

    for key in ("jitter_minutes", "send_gap_min", "send_gap_max", "max_friends_per_run"):
        try:
            merged[key] = max(0, int(merged.get(key, DEFAULT_CONFIG[key])))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")
    if merged["send_gap_max"] < merged["send_gap_min"]:
        merged["send_gap_max"] = merged["send_gap_min"]

    accounts_in = merged.get("accounts") or {}
    if not isinstance(accounts_in, dict):
        raise ValueError("accounts 必须是对象")
    merged["accounts"] = {
        str(key): _normalize_account(value, str(key))
        for key, value in accounts_in.items()
        if str(key).strip()
    }
    merged.pop("friends", None)  # 旧版单账号字段不再写回文件

    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


# --------------------------------------------------------------------------- #
# 账号增删改
# --------------------------------------------------------------------------- #

def unique_account_id(cfg: dict, base: str) -> str:
    """生成不与现有账号冲突的 ID（重名自动加序号）。"""
    candidate = sanitize_account_id(base) or f"account_{int(time.time())}"
    accounts = cfg.get("accounts") or {}
    if candidate not in accounts:
        return candidate
    index = 2
    while f"{candidate}_{index}" in accounts:
        index += 1
    return f"{candidate}_{index}"


def upsert_account(cfg: dict, account_id: str, patch: dict, *, create: bool = False) -> dict:
    """更新账号配置（只覆盖 patch 中出现的字段）。不存在且 create=False 时抛 KeyError。"""
    accounts = cfg.setdefault("accounts", {})
    if account_id not in accounts:
        if not create:
            raise KeyError(account_id)
        accounts[account_id] = dict(DEFAULT_ACCOUNT)
    current = _normalize_account(accounts.get(account_id), account_id)

    allowed = set(DEFAULT_ACCOUNT.keys())
    for key, value in (patch or {}).items():
        if key in allowed:
            current[key] = value
    accounts[account_id] = _normalize_account(current, account_id)
    return accounts[account_id]


def delete_account(cfg: dict, account_id: str) -> None:
    (cfg.get("accounts") or {}).pop(account_id, None)
    for path in (state_path(account_id), account_photo_path(account_id), account_log_path(account_id)):
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


def discover_state_files() -> list[str]:
    """列出 data/states/ 下所有登录态文件名（不含扩展名）。"""
    if not STATES_DIR.exists():
        return []
    return sorted(p.stem for p in STATES_DIR.glob("*.json") if p.is_file())


def accounts_from_states(cfg: dict) -> dict:
    """把 data/states/ 里存在、但配置里没有的登录态文件补登记成账号。"""
    existing = cfg.get("accounts") or {}
    for account_id in discover_state_files():
        if account_id in existing:
            continue
        try:
            upsert_account(
                cfg,
                account_id,
                {"label": account_id, "original_filename": f"{account_id}.json"},
                create=True,
            )
        except Exception:
            continue
    return cfg


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_state_bytes(raw: bytes) -> dict | None:
    """解析登录态文件字节。兼容 Windows 记事本 / 浏览器下载可能带上的 UTF-8 BOM。"""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def is_valid_state(data: dict | None) -> bool:
    return bool(isinstance(data, dict) and isinstance(data.get("cookies"), list) and data["cookies"])


def read_json(path: Path) -> dict | None:
    """读取 JSON 文件（兼容 BOM），失败返回 None。"""
    try:
        return parse_state_bytes(path.read_bytes())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 旧版单账号数据迁移
# --------------------------------------------------------------------------- #

def migrate_legacy(cfg: dict) -> bool:
    """把旧版 data/state.json（+ 顶层 friends）迁移成 data/states/默认账号.json。

    仅在 data/states/ 目录尚不存在且旧 state.json 存在时执行一次。返回是否迁移。
    """
    if STATES_DIR.exists():
        return False
    if not LEGACY_STATE_PATH.exists():
        return False

    try:
        raw = LEGACY_STATE_PATH.read_bytes()
        info = parse_state_bytes(raw)
    except Exception:
        return False
    if not is_valid_state(info):
        return False

    account_id = unique_account_id(cfg, "默认账号")
    try:
        STATES_DIR.mkdir(parents=True, exist_ok=True)
        # 重新序列化，去掉可能存在的 BOM，保证 Playwright 能正常读取
        state_path(account_id).write_text(
            json.dumps(info, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        return False

    cfg.setdefault("accounts", {})[account_id] = _normalize_account(
        {
            "label": "默认账号",
            "original_filename": "state.json",
            "uploaded_at": now_iso(),
            "enabled": True,
            "offset_minutes": 0,
            "friends": list(cfg.get("friends") or []),
            "messages": [],
        },
        account_id,
    )

    try:
        save_config(cfg)
        backup = LEGACY_STATE_PATH.with_name("state.json.bak")
        if not backup.exists():
            shutil.copyfile(LEGACY_STATE_PATH, backup)
        LEGACY_STATE_PATH.unlink()
    except Exception:
        return False
    return True
