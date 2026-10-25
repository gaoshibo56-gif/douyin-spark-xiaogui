#!/usr/bin/env python3
"""GitHub Actions（免服务器）运行入口。

不启动 Web 服务，只读取 ci/ 下的配置与登录态，串行给每个启用账号发送；
适合 GitHub Actions 定时任务，每月免费额度（私有仓库）2000 分钟，对个人自用绰绰有余。

本机自测：
    python ci/run.py --dry

配置文件：ci/config.json
登录态目录：ci/state/ （每个账号一份 <账号ID>.json）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_DIR = ROOT / "ci"
DEFAULT_CONFIG_PATH = CI_DIR / "config.json"
DEFAULT_STATE_DIR = CI_DIR / "state"


def log(message: str = "") -> None:
    print(message, flush=True)


# -------------------------------------------------------------------------- #
# 配置读取
# -------------------------------------------------------------------------- #

def load_ci_config(path: Path) -> dict:
    if not path.exists():
        print(
            f"\n[错误] 找不到 {path}\n"
            "请先在本机运行：python tools/prepare_ci.py\n"
            "（它会把本机 data/ 里已配置好的账号与登录态整理到 ci/ 目录）\n",
            flush=True,
        )
        raise SystemExit(1)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"\n[错误] {path} 不是合法 JSON：{exc}\n", flush=True)
        raise SystemExit(1)
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict) or not data["accounts"]:
        print(f"\n[错误] {path} 格式不正确：需要包含非空的 accounts 字段\n", flush=True)
        raise SystemExit(1)
    return data


def to_core_config(ci_cfg: dict) -> dict:
    """把 ci/config.json 转换成 core 使用的 config.json 结构。"""
    schedule = ci_cfg.get("schedule") or {}
    defaults = ci_cfg.get("defaults") or {}
    accounts = {}
    for account_id, raw in (ci_cfg.get("accounts") or {}).items():
        raw = raw or {}
        accounts[account_id] = {
            "label": raw.get("label") or account_id,
            "enabled": raw.get("enabled", True),
            "offset_minutes": int(raw.get("offset_minutes", 0) or 0),
            "jitter_minutes": raw.get("jitter_minutes"),
            "send_gap_min": raw.get("send_gap_min"),
            "send_gap_max": raw.get("send_gap_max"),
            "max_friends_per_run": raw.get("max_friends_per_run"),
            "friends": [str(x).strip() for x in (raw.get("friends") or []) if str(x).strip()],
            "messages": [str(x).strip() for x in (raw.get("messages") or []) if str(x).strip()],
            "original_filename": raw.get("state_file") or f"{account_id}.json",
        }
    return {
        "schedule_time": str(schedule.get("base_time") or "21:00"),
        "jitter_minutes": int(schedule.get("jitter_minutes", 0) or 0),
        "send_gap_min": int(defaults.get("send_gap_min", 6) or 6),
        "send_gap_max": int(defaults.get("send_gap_max", 12) or 12),
        "max_friends_per_run": int(defaults.get("max_friends_per_run", 20) or 20),
        "messages": [str(x) for x in (defaults.get("messages") or ["🔥 续火花"]) if str(x).strip()] or ["🔥 续火花"],
        "accounts": accounts,
    }


# -------------------------------------------------------------------------- #
# 登录态准备（支持明文 / 加密 / 环境变量 base64）
# -------------------------------------------------------------------------- #

def _openssl() -> str | None:
    found = shutil.which("openssl")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\Git\usr\bin\openssl.exe"),
        Path(r"C:\Program Files (x86)\Git\usr\bin\openssl.exe"),
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.extend(
            sorted(Path(local).glob("GitHubDesktop/app-*/resources/app/git/usr/bin/openssl.exe"))
        )
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def decrypt_state(src: Path, dst: Path, passphrase: str) -> bool:
    openssl = _openssl()
    if not openssl:
        log("    未找到 openssl，无法解密（Windows 上建议安装 Git for Windows）")
        return False
    env = dict(os.environ, DS_STATE_PASSPHRASE=passphrase)
    try:
        subprocess.run(
            [
                openssl,
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-in",
                str(src),
                "-out",
                str(dst),
                "-pass",
                "env:DS_STATE_PASSPHRASE",
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return True
    except subprocess.CalledProcessError:
        log("    解密失败：口令不正确或文件已损坏")
        return False


def prepare_states(
    ci_cfg: dict,
    state_dir: Path,
    target_dir: Path,
    *,
    allow_public: bool,
    visibility: str,
) -> dict[str, str]:
    """把每个账号的登录态落到 DATA_DIR/states/<账号ID>.json。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    passphrase = (os.environ.get("DS_STATE_PASSPHRASE") or "").strip()
    sources: dict[str, str] = {}
    plaintext_accounts: list[str] = []

    for account_id, raw in (ci_cfg.get("accounts") or {}).items():
        raw = raw or {}
        name = raw.get("state_file") or f"{account_id}.json"
        plain = state_dir / name
        encrypted = state_dir / f"{name}.enc"
        dst = target_dir / f"{account_id}.json"

        # 1）环境变量里的 base64（适合体积较小的登录态）
        env_key = "DS_STATE_B64_" + "".join(ch if ch.isalnum() else "_" for ch in account_id).upper()
        b64 = (os.environ.get(env_key) or "").strip()
        if b64:
            import base64

            try:
                dst.write_bytes(base64.b64decode(b64))
                sources[account_id] = f"{env_key}(base64)"
                continue
            except Exception:
                log(f"  [{account_id}] {env_key} 解码失败，忽略")

        # 2）加密文件优先
        if encrypted.exists():
            if not passphrase:
                raise SystemExit(
                    f"\n[错误] 账号 {account_id} 的登录态为加密文件 {encrypted.name}，"
                    "但未提供口令。请在仓库 Secrets 中设置 DS_STATE_PASSPHRASE\n"
                )
            if decrypt_state(encrypted, dst, passphrase):
                sources[account_id] = f"{encrypted.name}(加密)"
                continue
            raise SystemExit(f"\n[错误] 账号 {account_id} 的登录态解密失败\n")

        # 3）明文文件
        if plain.exists():
            shutil.copyfile(plain, dst)
            sources[account_id] = f"{plain.name}(明文)"
            plaintext_accounts.append(account_id)
            continue

        raise SystemExit(
            f"\n[错误] 账号 {account_id} 缺少登录态文件：{plain.name}（或 {encrypted.name}）\n"
            f"请把它放进 {state_dir}，或运行 python tools/prepare_ci.py 重新准备\n"
        )

    if plaintext_accounts and visibility == "public" and not allow_public:
        raise SystemExit(
            "\n[错误] 检测到仓库是 public，但登录态使用明文文件，继续运行会把 Cookie 暴露在公开仓库里。\n"
            "解决办法（任选其一）：\n"
            "  1. 把仓库改成 Private（推荐，保护账号安全）\n"
            "  2. 使用加密方式：python tools/prepare_ci.py --encrypt，并在 Secrets 中设置 DS_STATE_PASSPHRASE\n"
            "  3. 明确要这么跑：给 ci/run.py 加 --allow-public\n"
        )
    return sources


# -------------------------------------------------------------------------- #
# 账号顺序（分时）与执行
# -------------------------------------------------------------------------- #

def ordered_accounts(ci_cfg: dict, only: str) -> list[tuple[str, dict]]:
    items = []
    for account_id, raw in (ci_cfg.get("accounts") or {}).items():
        raw = raw or {}
        if not raw.get("enabled", True):
            continue
        if only and account_id != only:
            continue
        items.append((account_id, raw))
    items.sort(key=lambda kv: (int((kv[1] or {}).get("offset_minutes", 0) or 0), kv[0]))
    return items


def write_summary(path: Path, results: list[dict], ci_cfg: dict, dry: bool) -> None:
    lines = ["# 抖音续火花运行结果", ""]
    lines.append(f"- 时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"- 模式：{'干跑测试' if dry else '真实发送'}")
    lines.append("")
    lines.append("| 账号 | 成功 | 失败 | 备注 |")
    lines.append("|---|---|---|---|")
    for result in results:
        notes: list[str] = []
        if result.get("logged_out"):
            notes.append("登录态过期")
        if result.get("rate_limited"):
            notes.append("疑似限流")
        if result.get("skipped"):
            notes.append("未配置好友")
        ok_n = len(result.get("ok") or [])
        bad = [f for f in (result.get("failed") or []) if (f or {}).get("name") != "_system"]
        lines.append(
            f"| {result.get('label') or result.get('account')} | {ok_n} | {len(bad)} | {'、'.join(notes) or '-'} |"
        )
    lines.append("")
    details = []
    for result in results:
        for f in (result.get("failed") or []):
            details.append(
                f"- **{result.get('label') or result.get('account')}** · {f.get('name')}：{f.get('reason')}"
            )
    if details:
        lines += ["## 失败明细", ""] + details + [""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Actions / 免服务器运行入口")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--data-dir", default=os.environ.get("CI_DATA_DIR") or str(ROOT / ".ci-data"))
    parser.add_argument("--only", default="", help="只运行指定账号ID")
    parser.add_argument("--dry", action="store_true", help="干跑，不真实发送")
    parser.add_argument("--no-stagger", action="store_true", help="忽略偏移，账号之间不等待")
    parser.add_argument("--allow-public", action="store_true", help="允许 public 仓库使用明文登录态")
    parser.add_argument("--summary", default="", help="把结果写入该 markdown 文件（Actions 步骤摘要）")
    parser.add_argument("--fail-on-error", dest="fail_on_error", action="store_true", default=True)
    parser.add_argument("--no-fail-on-error", dest="fail_on_error", action="store_false")
    args = parser.parse_args()

    ci_cfg = load_ci_config(Path(args.config))
    data_dir = Path(args.data_dir).resolve()
    states_dir = data_dir / "states"
    data_dir.mkdir(parents=True, exist_ok=True)
    states_dir.mkdir(parents=True, exist_ok=True)

    # 必须在 import core 之前设置：core.config 在导入时就读 DATA_DIR
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ.setdefault("LOCK_FILE", str(data_dir / ".send.lock"))

    visibility = (os.environ.get("DS_REPO_VISIBILITY") or "").strip().lower()
    log("=" * 60)
    log("抖音续火花 · 云端运行")
    log(f"数据目录   : {data_dir}")
    log(f"仓库可见性 : {visibility or '未知'}")

    sources = prepare_states(ci_cfg, Path(args.state_dir), states_dir,
                             allow_public=args.allow_public, visibility=visibility)
    for account_id, src in sources.items():
        log(f"  登录态 {account_id}: {src}")

    core_cfg = to_core_config(ci_cfg)
    (data_dir / "config.json").write_text(
        json.dumps(core_cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 现在才导入 core（此时 DATA_DIR 已就位）
    from core import automation

    items = ordered_accounts(ci_cfg, args.only.strip())
    if not items:
        log("\n没有需要运行的账号（可能都已停用，或 --only 没匹配上）")
        return 0

    schedule = ci_cfg.get("schedule") or {}
    max_stagger = int(schedule.get("max_stagger_minutes", 60) or 0)
    stagger = not args.no_stagger and max_stagger > 0

    log("")
    log(f"待运行账号 {len(items)} 个，分时={'开' if stagger else '关'}（上限 {max_stagger} 分钟）")
    for account_id, raw in items:
        log(
            f"  - {raw.get('label') or account_id}: 偏移 {int(raw.get('offset_minutes', 0) or 0)} 分钟，"
            f"好友 {len(raw.get('friends') or [])} 人"
        )

    started = time.time()
    results: list[dict] = []
    for account_id, raw in items:
        offset = int(raw.get("offset_minutes", 0) or 0)
        if stagger:
            target = min(offset, max_stagger) * 60
            wait = target - (time.time() - started)
            if wait > 1:
                log(f"\n[{account_id}] 分时等待 {wait / 60:.1f} 分钟后开始…")
                time.sleep(wait)
        log(f"\n===== 开始账号 {raw.get('label') or account_id} ({account_id}) =====")
        try:
            result = automation.run_send(account_id, dry_run=args.dry)
        except Exception as exc:
            log(f"[{account_id}] 运行异常: {exc}")
            result = {
                "account": account_id,
                "label": raw.get("label") or account_id,
                "ok": [],
                "failed": [{"name": "_system", "reason": f"运行异常: {exc}"}],
                "logged_out": False,
                "rate_limited": False,
                "dry_run": args.dry,
            }
        results.append(result)
        ok_n = len(result.get("ok") or [])
        bad_n = len([f for f in (result.get("failed") or []) if (f or {}).get("name") != "_system"])
        sys_n = len([f for f in (result.get("failed") or []) if (f or {}).get("name") == "_system"])
        log(
            f"[{account_id}] 结果：成功 {ok_n}，失败 {bad_n}"
            + (f"，系统错误 {sys_n}" if sys_n else "")
            + ("，登录态过期" if result.get("logged_out") else "")
            + ("，疑似限流" if result.get("rate_limited") else "")
        )

    # 汇总
    log("\n" + "=" * 60)
    log("汇总")
    total_ok = total_failed = 0
    for result in results:
        ok_n = len(result.get("ok") or [])
        bad = [f for f in (result.get("failed") or []) if (f or {}).get("name") != "_system"]
        sys_bad = [f for f in (result.get("failed") or []) if (f or {}).get("name") == "_system"]
        total_ok += ok_n
        total_failed += len(bad) + len(sys_bad)
        note = ""
        if result.get("logged_out"):
            note += "，登录态过期"
        if result.get("rate_limited"):
            note += "，疑似限流"
        log(f"  {result.get('label') or result.get('account')}: 成功 {ok_n}，失败 {len(bad)}{note}")

    (data_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.summary:
        write_summary(Path(args.summary), results, ci_cfg, args.dry)

    has_error = any(
        (result.get("failed") or []) or result.get("logged_out") for result in results
    )
    log("")
    log(f"总计：成功 {total_ok} 人，失败 {total_failed} 人")
    if has_error and args.fail_on_error:
        log("存在失败项，退出码 1（Actions 会标红并给你发邮件通知）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
