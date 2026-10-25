"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息（多账号版）。

一个「账号」= data/states/<账号ID>.json 里的一份登录态，各账号的自动化完全隔离。

发送逻辑参考 douyin-cloud-streak（MIT），要点：
- 点击联系人后校验右侧会话确实切换（防止限流时错发给上一个人）；
- 列表点击失败时用搜索框兜底；
- 检测“操作频繁 / 安全验证”等提示，命中即停本轮；
- 发送前清空输入框，发送后校验输入框已清空。
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime
from typing import Callable

from .config import (
    account_effective,
    account_photo_path,
    account_time_text,
    get_account,
    load_config,
    state_path,
)
from .lock import ProcessLock

logger = logging.getLogger("douyin-spark")

CHAT_URL = "https://www.douyin.com/chat"


def _launch_args() -> list[str]:
    """浏览器启动参数；可通过 DS_PROXY 环境变量走代理（云端被风控时可用）。"""
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


RATE_LIMIT_KEYWORDS = [
    "操作频繁",
    "操作太频繁",
    "发送过于频繁",
    "请稍后再试",
    "稍后再试",
    "安全验证",
    "滑动验证",
    "验证码",
    "验证中心",
    "人机验证",
    "网络异常",
    "请勿频繁",
]

LOGIN_TEXTS = ["扫码登录", "验证码登录", "登录后查看", "登录后即可"]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_label(cfg: dict, account_id: str) -> str:
    """日志里统一用「显示名（账号ID）」，多账号运行时能一眼分清是谁。"""
    account = get_account(cfg, account_id)
    label = (account or {}).get("label") or account_id
    if label == account_id:
        return f"[{account_id}]"
    return f"[{label} ({account_id})]"


def _screenshot(page, account_id: str) -> None:
    try:
        path = account_photo_path(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), timeout=5000)
        logger.info("已保存页面截图: %s", path)
    except Exception:
        pass


def check_login(page) -> tuple[bool, str]:
    """返回 (是否已登录, 说明)。宁可误报掉线，也不要带着过期登录态硬跑。"""
    url = page.url
    if "login" in url.lower() or "passport" in url.lower():
        return False, f"页面已跳转到登录页（{url}）"

    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return False, "页面出现扫码登录二维码，登录态已过期"
    except Exception:
        pass

    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return False, f"页面出现登录提示「{text}」"
        except Exception:
            continue

    cookies = page.context.cookies()
    if not any(c["name"].startswith("sessionid") for c in cookies):
        return False, "未检测到 sessionid Cookie"
    return True, "ok"


def detect_rate_limit(page) -> str | None:
    for kw in RATE_LIMIT_KEYWORDS:
        try:
            loc = page.get_by_text(kw, exact=False)
            for i in range(loc.count()):
                if loc.nth(i).bounding_box():
                    return kw
        except Exception:
            continue
    return None


def _find_contact(page, name: str):
    """优先按全文精确匹配联系人标题，避免误点其他会话里的消息预览。"""
    exact = page.get_by_text(name, exact=True)
    if exact.count():
        return exact.first
    return page.locator(".conversationConversationItemtitle").filter(has_text=name).first


def verify_in_conversation(page, name: str) -> bool:
    """右侧会话顶部标题区域（x>300 且 y<100）出现目标昵称才算切换成功，防止错发。"""
    for exact in (True, False):
        try:
            loc = page.get_by_text(name, exact=exact)
            for i in range(loc.count()):
                try:
                    box = loc.nth(i).bounding_box()
                except Exception:
                    continue
                if box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                    return True
        except Exception:
            continue
    return False


def search_and_open(page, name: str) -> bool:
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count() == 0:
            return False
        box.click()
        box.fill(name)
        time.sleep(4)
        # 优先直接点搜索结果里的「发消息」按钮，最可靠
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(4)
            return True
        # 否则点精确匹配的结果卡片，再找「发消息」入口
        candidate = page.get_by_text(name, exact=True).first
        if candidate.count() == 0:
            candidate = page.get_by_text(name, exact=False).first
        if candidate.count() == 0:
            return False
        candidate.click(force=True)
        time.sleep(3)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(3)
        return True
    except Exception as e:
        logger.info("搜索打开 %s 失败: %s", name, e)
        return False


def _type_and_send(page, input_box, msg_text: str) -> bool:
    """把文字输入输入框并按 Enter 发送，返回文字是否成功进入输入框。"""
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
            logger.warning("文字未进入输入框，当前内容: %r", cur[:30])
            return False
        page.keyboard.press("Enter")
        return True
    except Exception as e:
        logger.info("输入/发送异常: %s", str(e)[:100])
        return False


def _wait_input_cleared(input_box, msg_text: str, wait: float = 8) -> bool:
    """消息发出后输入框应不再包含发送文字，以此确认真正发出。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1)
        try:
            cur = input_box.inner_text() or ""
            if msg_text not in cur:
                return True
        except Exception:
            pass
    return False


def send_to_contact(page, name: str, msg_text: str, dry_run: bool) -> tuple[bool, str]:
    switched = False
    for attempt in range(5):
        try:
            target = _find_contact(page, name)
            if target.count():
                # 目标可能在列表下方（未滚入视口），先滚到可见再点，避免 force 点到空白
                try:
                    target.scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    pass
                target.click(force=True, timeout=10000)
                time.sleep(random.uniform(2, 4))
                if verify_in_conversation(page, name):
                    switched = True
                    break
            else:
                # 目标可能因列表懒加载尚未渲染，滚动侧边栏继续找
                try:
                    page.mouse.move(200, 350)
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                time.sleep(1.5)
        except Exception as e:
            logger.info("点击联系人 %s 异常: %s", name, str(e)[:100])
        time.sleep(random.uniform(1, 2))

    if not switched and search_and_open(page, name):
        time.sleep(random.uniform(1, 3))
        switched = verify_in_conversation(page, name)

    if not switched:
        return False, "未能切换到该好友会话（名字不在聊天列表，或页面结构变化）"

    if detect_rate_limit(page):
        return False, "检测到「操作频繁 / 安全验证」提示"

    input_box = page.locator('div[contenteditable="true"]').first
    try:
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return False, "找不到聊天输入框"
        input_box.wait_for(state="visible", timeout=8001)
    except Exception:
        return False, "找不到聊天输入框"

    if dry_run:
        return True, "dry-run"

    try:
        if detect_rate_limit(page):
            return False, "发送前检测到验证提示"
        if not _type_and_send(page, input_box, msg_text):
            return False, "文字未能输入到输入框"
        if _wait_input_cleared(input_box, msg_text, wait=8):
            return True, "ok"
        logger.warning("未检测到消息发出，重试一次：%s", name)
        if detect_rate_limit(page):
            return False, "重试时检测到验证提示"
        if not _type_and_send(page, input_box, msg_text):
            return False, "重试时文字未能输入"
        if _wait_input_cleared(input_box, msg_text, wait=8):
            return True, "ok"
        return False, "发送后输入框未清空，消息可能未发出"
    except Exception as e:
        logger.info("向 %s 发送异常: %s", name, e)
        return False, f"发送异常: {e}"


# --------------------------------------------------------------------------- #
# 打开某个账号的私信页（所有浏览器操作都走这里）
# --------------------------------------------------------------------------- #

def _with_chat_page(account_id: str, cfg: dict, job: Callable, *, ready_wait: int = 8,
                    list_wait: int = 30, goto_timeout: int = 60000) -> dict:
    """在账号 account_id 的登录态下打开私信页并执行 job(page, context)。

    job 返回的 dict 会合并进结果；函数负责补齐 logged_out / error。
    所有账号共用一个跨进程文件锁：任意时刻只有一个 Chromium 在跑（严格串行排队）。
    """
    tag = log_label(cfg, account_id)
    result: dict = {"ok": False, "error": None, "logged_out": False}
    path = state_path(account_id)
    if not path.exists():
        result["error"] = f"账号 {account_id} 尚未上传登录态文件"
        return result

    browser = None
    lock = ProcessLock()
    lock.acquire()
    # 惰性导入：playwright 较重（~20-30MB），发送/获取时才加载，降低空闲常驻内存
    from playwright.sync_api import sync_playwright
    try:
        p = sync_playwright().start()
        try:
            browser = p.chromium.launch(
                headless=True,
                args=_launch_args(),
            )
            context = browser.new_context(
                storage_state=str(path),
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()

            goto_ok = False
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=goto_timeout, wait_until="domcontentloaded")
                    goto_ok = True
                    break
                except Exception as e:
                    logger.info("%s 第 %s 次打开页面失败: %s", tag, attempt + 1, str(e)[:80])
                    time.sleep(5)
            if not goto_ok:
                result["error"] = "无法打开抖音私信页面"
                return result

            time.sleep(ready_wait)
            # 等聊天列表渲染出来再操作，避免第一个好友（列表还没加载完）点不上
            try:
                page.wait_for_selector(".conversationConversationItemtitle", timeout=list_wait * 1000)
            except Exception:
                logger.warning("%s 聊天列表加载超时，仍继续尝试", tag)

            logged, why = check_login(page)
            if not logged:
                result["logged_out"] = True
                result["error"] = why
                _screenshot(page, account_id)
                return result

            out = job(page, context) or {}
            result.update(out)
            result["logged_out"] = bool(result.get("logged_out"))
            return result
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("%s 运行异常: %s", tag, e)
        result["error"] = f"运行异常: {e}"
        return result
    finally:
        lock.release()


# --------------------------------------------------------------------------- #
# 读取聊天列表
# --------------------------------------------------------------------------- #

_EXTRACT_JS = """
    () => {
        const out = [];
        const seen = new Set();
        document.querySelectorAll('.conversationConversationItemtitle').forEach(t => {
            const name = (t.textContent || '').trim();
            if (!name || seen.has(name)) return;
            seen.add(name);
            const wrap = t.parentElement;
            const s = wrap ? wrap.querySelector('.commonStreaknormalText') : null;
            out.push({ name: name, streak: s ? (s.textContent || '').trim() : '' });
        });
        return out;
    }
"""


def _normalize_page_result(out) -> dict:
    """把 _with_chat_page 的返回值统一成 dict。

    正常返回是 dict；兼容旧写法返回 (ok, error) 元组的情况；字符串按错误信息处理。
    """
    if isinstance(out, dict):
        out.setdefault("ok", False)
        out.setdefault("error", None)
        out.setdefault("logged_out", False)
        return out
    if isinstance(out, tuple):
        ok = bool(out[0]) if out else False
        error = out[1] if len(out) > 1 else None
        return {"ok": ok, "error": None if ok else error, "logged_out": False}
    if isinstance(out, str):
        return {"ok": False, "error": out, "logged_out": False}
    return {"ok": False, "error": None, "logged_out": False}


def fetch_chat_contacts(account_id: str) -> dict:
    """读取某个账号私信页左侧聊天列表的联系人（含火花天数），供网页端勾选。"""
    cfg = load_config()
    tag = log_label(cfg, account_id)
    result = {"account": account_id, "at": _now(), "names": [], "error": None}

    def job(page, context):
        collected: list[dict] = []
        for attempt in range(3):
            try:
                page.wait_for_selector(".conversationConversationItemtitle", timeout=45000)
            except Exception:
                logger.info("%s 第 %s 次等待联系人列表超时", tag, attempt + 1)

            stable = 0
            for _ in range(20):
                data = page.evaluate(_EXTRACT_JS) or []
                new_items = [x for x in data if x not in collected]
                if new_items:
                    collected.extend(new_items)
                    stable = 0
                else:
                    stable += 1
                    if stable >= 2:
                        break
                try:
                    page.mouse.move(200, 350)
                    page.mouse.wheel(0, 800)
                except Exception:
                    pass
                page.wait_for_timeout(1200)

            if collected:
                break
            try:
                page.reload(wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(12000)
            except Exception:
                pass

        logger.info("%s 已读取聊天列表联系人 %s 个", tag, len(collected))
        return {"ok": True, "names": collected, "error": None}

    out = _normalize_page_result(_with_chat_page(account_id, cfg, job, ready_wait=10, list_wait=45,
                                                 goto_timeout=90000))
    result["names"] = out.get("names", [])
    result["error"] = out.get("error")
    return result


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #

def run_send(account_id: str, dry_run: bool = False, only_names: list[str] | None = None) -> dict:
    """给指定账号的好友发送消息，返回本轮结果（含账号ID，便于汇总展示）。"""
    cfg = load_config()
    account = get_account(cfg, account_id)
    tag = log_label(cfg, account_id)

    result = {
        "account": account_id,
        "label": (account or {}).get("label") or account_id,
        "at": _now(),
        "dry_run": bool(dry_run),
        "ok": [],
        "failed": [],
        "logged_out": False,
        "rate_limited": False,
        "skipped": False,
    }

    if account is None:
        result["failed"].append({"name": "_system", "reason": f"账号 {account_id} 不存在"})
        return result

    eff = account_effective(account, cfg)
    friends = eff["friends"]
    if only_names is not None:
        friends = [f for f in friends if f in only_names]
    max_n = int(eff["max_friends_per_run"] or 0)
    targets = friends[:max_n] if max_n > 0 else friends
    messages = eff["messages"]

    if not targets:
        logger.info("%s 未配置任何好友，跳过发送", tag)
        result["skipped"] = True
        return result

    def job(page, context):
        logger.info("%s 待发送好友 %s 人，dry_run=%s（发送时刻 %s）",
                    tag, len(targets), dry_run, account_time_text(cfg, account))
        for name in targets:
            msg = random.choice(messages)
            ok, why = send_to_contact(page, name, msg, dry_run)
            if ok:
                result["ok"].append(name)
                logger.info("%s 已发送给 %s：%s", tag, name, msg if not dry_run else "(干跑，未真实发送)")
            else:
                result["failed"].append({"name": name, "reason": why})
                logger.warning("%s 发送给 %s 失败：%s", tag, name, why)
                if detect_rate_limit(page):
                    result["rate_limited"] = True
                    logger.warning("%s 疑似触发限流，停止本轮", tag)
                    break
            time.sleep(random.uniform(eff["gap_min"], eff["gap_max"]))
        return {}

    out = _normalize_page_result(_with_chat_page(account_id, cfg, job))
    if out.get("logged_out"):
        result["logged_out"] = True
    if out.get("error") and not result["failed"] and not result["ok"]:
        result["failed"].append({"name": "_system", "reason": out["error"]})
    return result


def run_send_all(account_ids: list[str], dry_run: bool = False) -> dict:
    """依次（串行）给多个账号发送；账号之间的分时由调度器/上层控制。"""
    runs = []
    for account_id in account_ids:
        try:
            runs.append(run_send(account_id, dry_run=dry_run))
        except Exception as e:  # 单个账号异常不影响其它账号
            logger.error("账号 %s 运行异常: %s", account_id, e)
            runs.append({
                "account": account_id,
                "label": account_id,
                "at": _now(),
                "dry_run": bool(dry_run),
                "ok": [],
                "failed": [{"name": "_system", "reason": f"运行异常: {e}"}],
                "logged_out": False,
                "rate_limited": False,
            })
    return {
        "at": _now(),
        "dry_run": bool(dry_run),
        "multiple": True,
        "runs": runs,
        "ok": [n for r in runs for n in r.get("ok", [])],
        "failed": [
            {"name": f"{r.get('label') or r.get('account')} · {f.get('name')}", "reason": f.get("reason")}
            for r in runs
            for f in r.get("failed", [])
        ],
        "logged_out": any(r.get("logged_out") for r in runs),
        "rate_limited": any(r.get("rate_limited") for r in runs),
    }
