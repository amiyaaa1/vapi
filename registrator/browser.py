import asyncio
import random
import shutil
import subprocess
import tempfile
from pathlib import Path
from playwright.async_api import async_playwright, Browser, Page
from . import config

_port_counter = config.DEBUG_PORT_BASE


def _next_port() -> int:
    global _port_counter
    p = _port_counter
    _port_counter += 1
    return p


async def launch_browser(proxy_url: str = "", port: int = 0) -> tuple[Browser, Page, subprocess.Popen, Path]:
    """
    启动 CloakBrowser 并用 Playwright 连接。
    返回 (browser, page, process, temp_dir)
    """
    port = port or _next_port()
    seed = random.randint(10000, 99999)
    temp_dir = Path(tempfile.mkdtemp(prefix="cloak_"))

    args = [
        config.CHROME_PATH,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={temp_dir}",
        f"--fingerprint={seed}",
        "--fingerprint-platform=windows",
        '--fingerprint-gpu-vendor=Google Inc. (NVIDIA)',
        '--fingerprint-gpu-renderer=ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)',
        "--fingerprint-noise=false",
        "--fingerprint-storage-quota=100000",
        "--disable-blink-features=AutomationControlled",
        "--window-position=-2560,0",
        "--window-size=1920,1080",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--no-first-run",
        "--disable-default-apps",
    ]
    if proxy_url:
        args.append(f"--proxy-server={proxy_url}")

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await asyncio.sleep(3)

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    ctx = browser.contexts[0]
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.set_viewport_size({"width": 1920, "height": 947})

    return browser, page, proc, temp_dir


async def close_browser(browser: Browser, proc: subprocess.Popen, temp_dir: Path):
    """关闭浏览器、杀进程、清理临时目录"""
    try:
        await browser.close()
    except Exception:
        pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass
