"""GitHub Actions 云端运行脚本：以加载登录态的方式给多个账号分时发送消息。

在 Actions runner 上运行，流程：
1. 读取 ci/config.json（全局基准时间 + 各账号配置）
2. 读取 ci/state/<账号ID>.json（或加密后的 .enc，需 DS_STATE_PASSPHRASE 解密）
3. 按账号 offset_minutes 偏移，依次（串行）给每个账号的好友发送消息
4. 输出步骤摘要，便于查看每个账号的成功/失败情况

环境变量：
    DS_STATE_PASSPHRASE  若使用了 --encrypt 加密，需在此提供解密口令
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CI_DIR = BASE_DIR / "ci"
CI_CONFIG = CI_DIR / "config.json"
CI_STATE_DIR = CI_DIR / "state"

CHAT_URL = "https://www.douyin.com/chat"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load_config() -> dict:
    cfg = json.loads(CI_CONFIG.read_text(encoding="utf-8"))
    return cfg


def _load_state(account_id: str) -> dict | None:
    """读取登录态，优先 .json，其次 .enc（需解密）。"""
    plain = CI_STATE_DIR / f"{account_id}.json"
    enc = CI_STATE_DIR / f"{account_id}.enc"

    if plain.exists():
        return json.loads(plain.read_text(encoding="utf-8-sig"))

    if enc.exists():
        passphrase = os.environ.get("DS_STATE_PASSPHRASE", "").strip()
        if not passphrase:
            _log(f"⚠️ [{account_id}] 登录态已加密但未设置 DS_STATE_PASSPHRASE，跳过")
            return None
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            _log("❌ 缺少 cryptography 依赖，无法解密登录态")
            return None
        key = _derive_key(passphrase)
        fernet = Fernet(key)
        data = fernet.decrypt(enc.read_bytes())
        return json.loads(data.decode("utf-8-sig"))

    _log(f"⚠️ [{account_id}] 未找到登录态文件，跳过")
    return None


def _derive_key(passphrase: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    import base64

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"douyin-spark-ci",
        iterations=100_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def _launch_args() -> list[str]:
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]
    proxy = os.environ.get("DS_PROXY", "").strip()
    if proxy:
        args.append(f"--proxy-server={proxy}")
    return args


def _run_account(account_id: str, account_cfg: dict, global_cfg: dict) -> dict:
    """给单个账号发送，返回结果 dict。"""
    result = {
        "account": account_id,
        "label": account_cfg.get("label") or account_id,
        "ok": [],
        "failed": [],
        "logged_out": False,
        "rate_limited": False,
        "skipped": False,
        "error": None,
    }

    state = _load_state(account_id)
    if state is None:
        result["failed"].append({"name": "_system", "reason": "无法加载登录态"})
        return result

    # 获取好友列表
    friends = list(account_cfg.get("friends") or [])
    max_n = account_cfg.get("max_friends_per_run")
    if max_n is None:
        max_n = global_cfg.get("max_friends_per_run", 20)
    try:
        max_n = int(max_n or 0)
    except (TypeError, ValueError):
        max_n = 20
    targets = friends[:max_n] if max_n > 0 else friends

    messages = account_cfg.get("messages") or global_cfg.get("messages") or ["🔥"]
    messages = [m for m in messages if str(m).strip()] or ["🔥"]

    gap_min = account_cfg.get("send_gap_min") or global_cfg.get("send_gap_min", 6)
    gap_max = account_cfg.get("send_gap_max") or global_cfg.get("send_gap_max", 12)
    try:
        gap_min = int(gap_min)
    except (TypeError, ValueError):
        gap_min = 6
    try:
        gap_max = int(gap_max)
    except (TypeError, ValueError):
        gap_max = 12
    gap_max = max(gap_min, gap_max)

    if not targets:
        result["skipped"] = True
        _log(f"[{account_id}] 未配置好友，跳过")
        return result

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_launch_args())
        try:
            context = browser.new_context(
                storage_state=json.dumps(state, ensure_ascii=False),
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()

            _log(f"[{account_id}] 正在打开抖音私信页…")
            page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
            time.sleep(8)

            # 简单登录态校验
            url = page.url
            if "login" in url.lower() or "passport" in url.lower():
                result["logged_out"] = True
                result["error"] = "页面已跳转到登录页，登录态已过期"
                return result

            try:
                qr = page.locator("#animate_qrcode_container")
                if qr.count() and qr.first.is_visible():
                    result["logged_out"] = True
                    result["error"] = "页面出现扫码登录二维码，登录态已过期"
                    return result
            except Exception:
                pass

            _log(f"[{account_id}] 登录态正常，待发送好友 {len(targets)} 人")

            for name in targets:
                msg = random.choice(messages)
                ok, why = _send_to_contact(page, name, msg)
                if ok:
                    result["ok"].append(name)
                    _log(f"[{account_id}] ✅ 已发送给 {name}")
                else:
                    result["failed"].append({"name": name, "reason": why})
                    _log(f"[{account_id}] ❌ 发送给 {name} 失败：{why}")
                    if any(kw in why for kw in ("操作频繁", "安全验证", "验证码", "滑动验证")):
                        result["rate_limited"] = True
                        _log(f"[{account_id}] ⚠️ 疑似触发限流，停止本轮")
                        break
                time.sleep(random.uniform(gap_min, gap_max))

        finally:
            try:
                browser.close()
            except Exception:
                pass

    return result


def _send_to_contact(page, name: str, msg_text: str) -> tuple[bool, str]:
    """在当前页面找到联系人并发送消息。"""
    switched = False
    for attempt in range(5):
        try:
            target = page.get_by_text(name, exact=True).first
            if target.count():
                try:
                    target.scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    pass
                target.click(force=True, timeout=10000)
                time.sleep(random.uniform(2, 4))
                # 校验右侧会话标题
                try:
                    loc = page.get_by_text(name, exact=False)
                    for i in range(loc.count()):
                        box = loc.nth(i).bounding_box()
                        if box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                            switched = True
                            break
                except Exception:
                    pass
                if switched:
                    break
            else:
                try:
                    page.mouse.move(200, 350)
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                time.sleep(1.5)
        except Exception as e:
            _log(f"点击联系人 {name} 异常: {str(e)[:100]}")
        time.sleep(random.uniform(1, 2))

    if not switched:
        return False, "未能切换到该好友会话"

    input_box = page.locator('div[contenteditable="true"]').first
    try:
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return False, "找不到聊天输入框"
        input_box.wait_for(state="visible", timeout=8001)
    except Exception:
        return False, "找不到聊天输入框"

    try:
        input_box.click()
        time.sleep(0.4)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        time.sleep(0.3)
        page.keyboard.type(msg_text, delay=100)
        time.sleep(0.8)
        cur = input_box.inner_text() or ""
        if msg_text not in cur:
            return False, "文字未能输入到输入框"
        page.keyboard.press("Enter")

        deadline = time.time() + 8
        while time.time() < deadline:
            time.sleep(1)
            try:
                cur = input_box.inner_text() or ""
                if msg_text not in cur:
                    return True, "ok"
            except Exception:
                pass
        # 重试一次
        if not _type_and_send(page, input_box, msg_text):
            return False, "重试时文字未能输入"
        deadline = time.time() + 8
        while time.time() < deadline:
            time.sleep(1)
            try:
                cur = input_box.inner_text() or ""
                if msg_text not in cur:
                    return True, "ok"
            except Exception:
                pass
        return False, "发送后输入框未清空，消息可能未发出"
    except Exception as e:
        return False, f"发送异常: {e}"


def _type_and_send(page, input_box, msg_text: str) -> bool:
    try:
        input_box.click()
        time.sleep(0.4)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        time.sleep(0.3)
        page.keyboard.type(msg_text, delay=100)
        time.sleep(0.8)
        cur = input_box.inner_text() or ""
        if msg_text not in cur:
            return False
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def main() -> None:
    if not CI_CONFIG.exists():
        print("❌ 未找到 ci/config.json，请先在本地运行 tools/prepare_ci.py 生成。")
        sys.exit(1)

    cfg = _load_config()
    base_time = cfg.get("schedule_time", "21:00")
    accounts = cfg.get("accounts") or {}

    enabled = {k: v for k, v in accounts.items() if v.get("enabled", True)}
    if not enabled:
        _log("没有启用的账号，退出。")
        return

    _log(f"共 {len(enabled)} 个启用账号，基准时间 {base_time}（Asia/Shanghai）")

    all_runs = []
    for account_id, acc in enabled.items():
        offset = int(acc.get("offset_minutes", 0) or 0)
        # 分时：按偏移分钟睡眠，再发送（Actions 单次任务，按顺序串行即可）
        if offset > 0:
            _log(f"[{account_id}] 偏移 {offset} 分钟，等待 {offset * 60} 秒…")
            time.sleep(offset * 60)

        run = _run_account(account_id, acc, cfg)
        all_runs.append(run)

    # 输出汇总（GitHub Actions 步骤摘要）
    total_ok = sum(len(r["ok"]) for r in all_runs)
    total_failed = sum(len(r["failed"]) for r in all_runs)
    expired = [r["label"] or r["account"] for r in all_runs if r["logged_out"]]
    rate_limited = [r["label"] or r["account"] for r in all_runs if r["rate_limited"]]

    summary = f"""## 抖音续火花 · 本次运行结果

- 账号数：{len(all_runs)}
- 成功发送：{total_ok} 人
- 发送失败：{total_failed} 人
"""
    if expired:
        summary += f"\n⚠️ 登录态已过期：{', '.join(expired)}"
    if rate_limited:
        summary += f"\n⚠️ 疑似触发限流：{', '.join(rate_limited)}"

    summary_path = Path(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"))
    summary_path.write_text(summary, encoding="utf-8")

    _log(f"\n✅ 全部完成：成功 {total_ok} 人，失败 {total_failed} 人")
    if expired:
        _log(f"⚠️ 登录态已过期账号：{', '.join(expired)}，请本地重新扫码后替换 ci/state/ 并重新提交")


if __name__ == "__main__":
    main()
