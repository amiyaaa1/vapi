import asyncio
import http.client
import logging
import os
import random
import re
import socket
import time
from typing import Tuple
from urllib.parse import quote

import httpx
from playwright.async_api import async_playwright

from . import config

log = logging.getLogger("registrator.csrf")

REGISTER_URL = "https://dashboard.vapi.ai/register?redirect=%2Fsignup"
BILLING_URL = "https://dashboard.vapi.ai/settings/billing"
SITEKEY = os.getenv("VAPI_TURNSTILE_SITEKEY", "0x4AAAAAAAa7ZSD7onoZcTuC")
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
]
USER_AGENT_FALLBACK = USER_AGENTS[0]
TURNSTILE_TIMEOUT = float(os.getenv("TURNSTILE_TIMEOUT", "120"))
TURNSTILE_SOLVER_URL = os.getenv("TURNSTILE_SOLVER_URL", "http://127.0.0.1:5000").rstrip("/")
TURNSTILE_SOLVER_POLL_INTERVAL = float(os.getenv("TURNSTILE_SOLVER_POLL_INTERVAL", "2"))
TURNSTILE_SOLVER_ATTEMPTS = int(os.getenv("TURNSTILE_SOLVER_ATTEMPTS", os.getenv("TURNSTILE_SOLVER_RETRIES", "2")))
WARP_CONTAINER_NAME = os.getenv("WARP_CONTAINER_NAME", "vapi-gateway-warp")
WARP_RESTART_COOLDOWN_SECONDS = float(os.getenv("WARP_RESTART_COOLDOWN_SECONDS", "75"))
WARP_RESTART_WAIT_SECONDS = float(os.getenv("WARP_RESTART_WAIT_SECONDS", "12"))
WARP_RESTART_STATE_FILE = os.getenv("WARP_RESTART_STATE_FILE", "/data/warp-restart-last")


def _env_true(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 10):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _docker_post(path: str) -> tuple[int, str]:
    docker_socket = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
    conn = _UnixHTTPConnection(docker_socket, timeout=float(os.getenv("DOCKER_API_TIMEOUT", "10")))
    try:
        conn.request("POST", path, body=b"", headers={"Host": "docker"})
        response = conn.getresponse()
        body = response.read().decode("utf-8", "replace")
        return response.status, body
    finally:
        conn.close()


def _warp_restartable_solver_error(error: str) -> bool:
    lowered = str(error or "").lower()
    return any(part in lowered for part in (
        "timeout",
        "timed out",
        "readtimeout",
        "connecttimeout",
        "pooltimeout",
        "timed out waiting for turnstile",
        "timed out waiting for turnstile/csrf",
        "solver task creation failed",
        "captcha_not_ready",
        "could not solve",
        "couldn't solve",
        "workers could not solve",
        "turnstile token",
    ))


def _restart_warp_container_sync(reason: str) -> bool:
    if not _env_true("WARP_RESTART_ON_TURNSTILE_TIMEOUT", True):
        return False
    if not WARP_CONTAINER_NAME:
        log.warning("WARP restart skipped: WARP_CONTAINER_NAME is empty")
        return False
    docker_socket = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
    if not os.path.exists(docker_socket):
        log.warning(f"WARP restart skipped: docker socket not found: {docker_socket}")
        return False

    now = time.time()
    try:
        with open(WARP_RESTART_STATE_FILE, "r", encoding="utf-8") as file:
            last = float((file.read() or "0").strip() or "0")
    except Exception:
        last = 0
    if last and now - last < WARP_RESTART_COOLDOWN_SECONDS:
        log.warning(
            f"WARP restart skipped by cooldown: reason={reason} "
            f"remaining={int(WARP_RESTART_COOLDOWN_SECONDS - (now - last))}s"
        )
        return False

    path = f"/containers/{quote(WARP_CONTAINER_NAME, safe='')}/restart?t=5"
    status, body = _docker_post(path)
    if status not in (200, 204):
        raise RuntimeError(f"Docker restart {WARP_CONTAINER_NAME} failed: status={status} body={body[:200]}")

    try:
        os.makedirs(os.path.dirname(WARP_RESTART_STATE_FILE), exist_ok=True)
        with open(WARP_RESTART_STATE_FILE, "w", encoding="utf-8") as file:
            file.write(str(now))
    except Exception:
        pass

    log.warning(f"WARP restarted for Turnstile solver issue: container={WARP_CONTAINER_NAME} reason={reason}")
    return True


async def _restart_warp_after_solver_issue(reason: str) -> bool:
    try:
        restarted = await asyncio.to_thread(_restart_warp_container_sync, reason)
        if restarted and WARP_RESTART_WAIT_SECONDS > 0:
            await asyncio.sleep(WARP_RESTART_WAIT_SECONDS)
        return restarted
    except Exception as e:
        log.warning(f"WARP restart failed after Turnstile solver issue: {e}")
        return False


def _chrome_version_from_user_agent(user_agent: str) -> str:
    match = re.search(r"(?:Chrome|Chromium)/([0-9.]+)", user_agent or "")
    return match.group(1) if match else ""


def _turnstile_fingerprint(
    user_agent: str = "",
    sec_ch_ua: str = "",
    browser_name: str = "",
    browser_version: str = "",
) -> dict:
    user_agent = user_agent or USER_AGENT_FALLBACK
    browser_version = browser_version or _chrome_version_from_user_agent(user_agent)
    major_version = (browser_version.split(".", 1)[0] if browser_version else "139") or "139"
    if not sec_ch_ua:
        sec_ch_ua = f'"Not;A=Brand";v="99", "Google Chrome";v="{major_version}", "Chromium";v="{major_version}"'
    return {
        "user_agent": user_agent,
        "sec_ch_ua": sec_ch_ua,
        "browser_name": browser_name or "Google Chrome",
        "browser_version": browser_version,
    }


async def _get_token_from_nexos_solver(proxy_url: str = "", page_url: str = REGISTER_URL) -> Tuple[str, dict]:
    if not TURNSTILE_SOLVER_URL or os.getenv("TURNSTILE_SOLVER_DISABLED", "0") in ("1", "true", "TRUE", "yes", "YES"):
        raise RuntimeError("Nexos Turnstile solver is disabled")

    timeout = httpx.Timeout(30.0, connect=5.0)
    params = {"url": page_url, "sitekey": SITEKEY}
    if proxy_url:
        params["proxy"] = proxy_url

    async with httpx.AsyncClient(timeout=timeout) as client:
        task_id = ""
        last_error = ""
        for _ in range(30):
            try:
                response = await client.get(f"{TURNSTILE_SOLVER_URL}/turnstile", params=params)
                data = response.json()
                task_id = data.get("taskId") or data.get("task_id") or ""
                if task_id:
                    break
                last_error = str(data)
            except Exception as e:
                last_error = str(e)
            await asyncio.sleep(2)

        if not task_id:
            raise RuntimeError(f"Nexos solver task creation failed: {last_error}")

        deadline = asyncio.get_event_loop().time() + TURNSTILE_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            response = await client.get(f"{TURNSTILE_SOLVER_URL}/result", params={"id": task_id})
            data = response.json()
            if data.get("status") == "ready":
                solution = data.get("solution") or {}
                token = solution.get("token") or data.get("value") or ""
                user_agent = solution.get("userAgent") or solution.get("user_agent") or ""
                if token:
                    return token, _turnstile_fingerprint(
                        user_agent=user_agent,
                        sec_ch_ua=solution.get("secChUa") or solution.get("sec_ch_ua") or "",
                        browser_name=solution.get("browserName") or solution.get("browser_name") or "",
                        browser_version=str(solution.get("browserVersion") or solution.get("browser_version") or ""),
                    )

            if data.get("errorId") == 1 and data.get("errorCode") != "CAPTCHA_NOT_READY":
                raise RuntimeError(f"Nexos solver failed: {data.get('errorDescription') or data}")

            await asyncio.sleep(TURNSTILE_SOLVER_POLL_INTERVAL)

        raise RuntimeError("Nexos solver timed out waiting for Turnstile token")


async def _optimized_route(route):
    req = route.request
    url = req.url
    resource_type = req.resource_type
    allowed_types = {"document", "script", "xhr", "fetch"}
    allowed_domains = (
        "dashboard.vapi.ai",
        "api.vapi.ai",
        "challenges.cloudflare.com",
        "static.cloudflareinsights.com",
        "cloudflare.com",
    )
    if resource_type in allowed_types or any(domain in url for domain in allowed_domains):
        await route.continue_()
    else:
        await route.abort()


async def _inject_turnstile(page):
    script = f"""
    document.querySelectorAll('.cf-turnstile, [data-sitekey], input[name="cf-turnstile-response"]').forEach(el => el.remove());
    const div = document.createElement('div');
    div.className = 'cf-turnstile';
    div.setAttribute('data-sitekey', '{SITEKEY}');
    div.style.cssText = 'position:fixed;top:20px;left:20px;z-index:9999;background:white;padding:15px;';
    document.body.appendChild(div);

    const oldScript = document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]');
    if (!oldScript) {{
        const s = document.createElement('script');
        s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
        s.async = true;
        s.defer = true;
        document.head.appendChild(s);
    }}
    """
    await page.evaluate(script)


async def _try_click_turnstile(page):
    selectors = [
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="turnstile"]',
        ".cf-turnstile",
        "[data-sitekey]",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0:
                await locator.click(timeout=1000, force=True)
                return
        except Exception:
            continue


async def get_turnstile_token(proxy_url: str = "", page_url: str = REGISTER_URL) -> Tuple[str, dict]:
    solver_error: Exception | None = None
    attempts = max(1, TURNSTILE_SOLVER_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        try:
            return await _get_token_from_nexos_solver(proxy_url, page_url)
        except Exception as e:
            solver_error = e
            error_text = f"{type(e).__name__}: {e}"
            if attempt < attempts and _warp_restartable_solver_error(error_text):
                restarted = await _restart_warp_after_solver_issue(error_text[:180])
                if restarted:
                    log.warning(f"Retrying Turnstile solver after WARP restart: attempt={attempt + 1}/{attempts}")
                    continue
            break

    if os.getenv("TURNSTILE_SOLVER_FALLBACK", "0") not in ("1", "true", "TRUE", "yes", "YES"):
        raise RuntimeError(f"Nexos Turnstile solver unavailable: {solver_error}")

    playwright = await async_playwright().start()
    browser = None
    try:
        user_agent = random.choice(USER_AGENTS)
        browser_version = _chrome_version_from_user_agent(user_agent)
        browser_major = (browser_version.split(".", 1)[0] if browser_version else "139") or "139"
        sec_ch_ua = f'"Not;A=Brand";v="99", "Google Chrome";v="{browser_major}", "Chromium";v="{browser_major}"'
        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ]
        proxy = {"server": proxy_url} if proxy_url else None
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=config.CHROME_PATH or None,
            args=launch_args,
            proxy=proxy,
        )
        context = await browser.new_context(
            viewport={"width": 500, "height": 160},
            user_agent=user_agent,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": sec_ch_ua,
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = await context.new_page()
        await page.route("**/*", _optimized_route)
        await page.goto(page_url, wait_until="domcontentloaded", timeout=120000)
        await _inject_turnstile(page)

        deadline = asyncio.get_event_loop().time() + TURNSTILE_TIMEOUT
        attempt = 0
        while asyncio.get_event_loop().time() < deadline:
            try:
                token = await page.locator('input[name="cf-turnstile-response"]').input_value(timeout=1000)
            except Exception:
                token = ""
            if token and len(token) > 20:
                actual_user_agent = await page.evaluate("navigator.userAgent || ''")
                return token, _turnstile_fingerprint(
                    user_agent=actual_user_agent or user_agent,
                    sec_ch_ua=sec_ch_ua,
                    browser_name="Google Chrome",
                    browser_version=browser_version,
                )
            if attempt > 2 and attempt % 3 == 0:
                await _try_click_turnstile(page)
            attempt += 1
            await asyncio.sleep(1.5)
        raise RuntimeError("Timed out waiting for Turnstile/CSRF token")
    finally:
        if browser:
            await browser.close()
        await playwright.stop()


async def get_signup_csrf_token(proxy_url: str = "") -> Tuple[str, dict]:
    return await get_turnstile_token(proxy_url, REGISTER_URL)
