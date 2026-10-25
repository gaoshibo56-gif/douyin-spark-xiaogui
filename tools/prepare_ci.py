#!/usr/bin/env python3
"""把本机 data/ 里已配置好的账号，整理成 GitHub Actions 需要的 ci/ 目录。

用法（本机运行一次）：
    python tools/prepare_ci.py            # 明文登录态（仓库务必设为 Private）
    python tools/prepare_ci.py --encrypt  # 加密登录态（更安全，需设置 DS_STATE_PASSPHRASE 密码）

生成的 ci/config.json 和 ci/state/ 需要提交到 GitHub 仓库。
"""

from __future__ import annotations

import base64
import getpass
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CI_DIR = ROOT / "ci"
STATES_DIR = DATA_DIR / "states"
CI_STATE_DIR = CI_DIR / "state"
CI_CONFIG_PATH = CI_DIR / "config.json"


def log(message: str = "") -> None:
    print(message, flush=True)


def _openssl() -> str | None:
    import shutil as _shutil

    found = _shutil.which("openssl")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\Git\usr\bin\openssl.exe"),
        Path(r"C:\Program Files (x86)\Git\usr\bin\openssl.exe"),
    ]
    local = __import__("os").environ.get("LOCALAPPDATA")
    if local:
        candidates.extend(
            sorted(Path(local).glob("GitHubDesktop/app-*/resources/app/git/usr/bin/openssl.exe"))
        )
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def _encrypt(src: Path, dst: Path, passphrase: str) -> bool:
    openssl = _openssl()
    if not openssl:
        log("未找到 openssl，无法加密")
        return False
    env = dict(__import__("os").environ, DS_STATE_PASSPHRASE=passphrase)
    try:
        subprocess.run(
            [
                openssl,
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
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
        log("加密失败")
        return False


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="整理本机 data/ 为 GitHub Actions 部署目录 ci/")
    parser.add_argument("--encrypt", action="store_true", help="登录态文件加密后再写入 ci/state/")
    parser.add_argument("--passphrase", default="", help="加密口令（不给则交互式输入）")
    args = parser.parse_args()

    if not DATA_DIR.exists():
        log(f"\n[错误] 找不到 {DATA_DIR}，请先在本机把项目跑起来并配置好账号。\n")
        return 1

    config_path = DATA_DIR / "config.json"
    if not config_path.exists():
        log(f"\n[错误] 找不到 {config_path}\n请先在网页上完成账号配置（上传登录态、勾选好友、设定好友间隔）。\n")
        return 1

    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    accounts = cfg.get("accounts") or {}
    if not accounts:
        log("\n[错误] 本机 config.json 里没有账号，请先在网页上添加账号。\n")
        return 1

    CI_DIR.mkdir(parents=True, exist_ok=True)
    CI_STATE_DIR.mkdir(parents=True, exist_ok=True)

    # 1）复制登录态
    log("\n整理登录态文件 -> ci/state/")
    ci_accounts = {}
    for account_id, acc in accounts.items():
        acc = acc or {}
        state_name = acc.get("original_filename") or f"{account_id}.json"
        src = STATES_DIR / state_name
        if not src.exists():
            log(f"  [警告] 账号 {account_id} 缺少登录态文件：{src}，已跳过")
            continue

        ci_accounts[account_id] = {
            "label": acc.get("label") or account_id,
            "enabled": acc.get("enabled", True),
            "offset_minutes": acc.get("offset_minutes", 0),
            "state_file": state_name,
            "friends": acc.get("friends") or [],
            "messages": acc.get("messages") or [],
            "send_gap_min": acc.get("send_gap_min"),
            "send_gap_max": acc.get("send_gap_max"),
            "max_friends_per_run": acc.get("max_friends_per_run"),
        }

        plain_dst = CI_STATE_DIR / state_name
        if args.encrypt:
            passphrase = args.passphrase.strip() or getpass.getpass("请输入加密口令：")
            enc_dst = CI_STATE_DIR / f"{state_name}.enc"
            if not _encrypt(src, enc_dst, passphrase):
                return 1
            # 加密时不保留明文
            log(f"  加密写入 {enc_dst.name}")
        else:
            shutil.copyfile(src, plain_dst)
            log(f"  复制 {plain_dst.name}（明文，仓库务必设为 Private）")

    if not ci_accounts:
        log("\n[错误] 没有可用的账号登录态，终止。\n")
        return 1

    # 2）写 ci/config.json
    schedule = {
        "base_time": cfg.get("schedule_time", "21:00"),
        "jitter_minutes": cfg.get("jitter_minutes", 30),
        "max_stagger_minutes": 60,
    }
    defaults = {
        "send_gap_min": cfg.get("send_gap_min", 6),
        "send_gap_max": cfg.get("send_gap_max", 12),
        "max_friends_per_run": cfg.get("max_friends_per_run", 20),
        "messages": cfg.get("messages") or ["🔥 续火花"],
    }
    ci_cfg = {"schedule": schedule, "defaults": defaults, "accounts": ci_accounts}
    CI_CONFIG_PATH.write_text(json.dumps(ci_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\n已写入 {CI_CONFIG_PATH}")

    # 3）后续步骤提示
    log("\n下一步：在 GitHub 仓库中：")
    log("  1. 把仓库设为 Private（保护登录态安全）")
    log("  2. 提交 ci/、.github/workflows/、tools/ 这几个目录")
    if args.encrypt:
        log("  3. 在 Settings -> Secrets and variables -> Actions 添加 Secret：DS_STATE_PASSPHRASE = 你的加密口令")
    log("  4. 确认仓库为 Public 时：不要提交明文登录态文件，改用 --encrypt")
    log("  5. Actions 页面手动跑一次：Run workflow -> Run workflow")
    log("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
