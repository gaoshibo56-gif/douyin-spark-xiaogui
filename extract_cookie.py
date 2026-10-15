"""在本地电脑（有界面的 Windows/macOS）运行：打开浏览器扫码登录抖音，导出登录态。

用法：
    pip install -r requirements.txt
    playwright install chromium
    python extract_cookie.py          # 输出 data/state.json
    python extract_cookie.py 3        # 输出 data/state_3.json（多账号扫描时各自编号，避免覆盖）

生成 data/state_<编号>.json 后，到网页端「设置 -> 上传登录态」上传到对应实例即可。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def _resolve_out_path() -> Path:
    """多账号扫描时，传一个编号参数（如 3）输出到 data/state_3.json，互不覆盖。"""
    base = Path(__file__).resolve().parent / "data"
    tag = (sys.argv[1].strip() if len(sys.argv) > 1 else "").strip()
    return base / (f"state_{tag}.json" if tag else "state.json")


OUT_PATH = _resolve_out_path()


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"登录态将保存到: {OUT_PATH}")
    print("正在打开浏览器，请在弹出的窗口里用手机抖音 App 扫码登录…")

    with sync_playwright() as p:
        # 抖音是可直接访问的站点，绕过系统代理（否则本机代理没转发会报 ERR_PROXY_CONNECTION_FAILED）
        browser = p.chromium.launch(headless=False, args=["--no-proxy-server"])
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)

        deadline = time.time() + 300
        while time.time() < deadline:
            cookies = context.cookies()
            if any(c["name"].startswith("sessionid") for c in cookies):
                time.sleep(2)
                context.storage_state(path=str(OUT_PATH))
                print(f"\n登录态已保存到: {OUT_PATH}")
                browser.close()
                return
            time.sleep(2)

        print("\n超时：5 分钟内未完成扫码登录，请重新运行。")
        browser.close()
        sys.exit(1)


if __name__ == "__main__":
    main()
