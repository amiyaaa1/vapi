"""
纯协议注册流程（基于请求录制分析）:
1. POST /auth/signup → 注册（curl_cffi impersonate 绕 5s 盾，csrf_token 空串）
2. 等邮件 → 提取验证链接
3. GET verify_url (allow_redirects=False) → Location fragment 取 Supabase JWT
4. GET /org/dashboard (Supabase JWT) → orgId
5. GET /user/auth (Supabase JWT) → user JWT
6. GET /org/{orgId}/auth (user JWT) → org JWT
7. GET /token (org JWT) → private key
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import time
import uuid
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, unquote, urlsplit, urlunsplit
import httpx
from curl_cffi import requests as cffi_requests
from playwright.async_api import async_playwright
from .csrf import (
    BILLING_URL,
    TURNSTILE_SOLVER_POLL_INTERVAL,
    TURNSTILE_SOLVER_URL,
    _recycle_turnstile_solver_pool,
    _restart_warp_after_solver_issue,
    _restart_warp_after_billing_issue,
    _turnstile_solver_proxy,
    _warp_restartable_solver_error,
    get_signup_csrf_token,
)
from .email_client import MoeMailClient, extract_verify_link
from . import config

log = logging.getLogger("registrator")

SITEKEY = "0x4AAAAAAAa7ZSD7onoZcTuC"
DASHBOARD_VERSION = os.getenv("DASHBOARD_VERSION", "670f2f3f21685ccb9be46866fdab17542cd08e28")
_DASHBOARD_VERSION_REFRESHED = False
STRIPE_PK = "pk_live_51NvVHqCRkod4mKy3BF9IHbOHhM3dGiYOPThym9Son9DdkS0DIyQKWkModLfDdPHO6hmEmqmzKrInZwA52PfMzrzX00MFliNTGB"
STRIPE_SOLVER_URL = os.getenv("STRIPE_SOLVER_URL", TURNSTILE_SOLVER_URL).rstrip("/")
STRIPE_SOLVER_TIMEOUT = float(os.getenv("STRIPE_SOLVER_TIMEOUT", os.getenv("TURNSTILE_TIMEOUT", "120")))
DODGEBALL_PUBLIC_KEY = os.getenv("DODGEBALL_PUBLIC_KEY", "364218e31251444ca8851a2dea555f6a")
DODGEBALL_API_URL = os.getenv("DODGEBALL_API_URL", "https://api.dodgeballhq.com")
ORG_DASHBOARD_ATTEMPTS = int(os.getenv("ORG_DASHBOARD_ATTEMPTS", "8"))
ORG_DASHBOARD_RETRY_SECONDS = float(os.getenv("ORG_DASHBOARD_RETRY_SECONDS", "2.0"))
SIGNUP_BROWSER_TURNSTILE_TIMEOUT = float(os.getenv("SIGNUP_BROWSER_TURNSTILE_TIMEOUT", "18"))
SIGNUP_SOLVER_BROWSER_TIMEOUT = float(os.getenv("SIGNUP_SOLVER_BROWSER_TIMEOUT", os.getenv("TURNSTILE_TIMEOUT", "120")))
SIGNUP_SOLVER_BROWSER_ATTEMPTS = int(os.getenv("SIGNUP_SOLVER_BROWSER_ATTEMPTS", "2"))


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except Exception:
        return default


class _MockHTTPResponse:
    def __init__(self, status_code: int, payload: dict, headers: dict | None = None):
        self.status_code = int(status_code)
        self._payload = payload if isinstance(payload, dict) else {"value": payload}
        self.text = json.dumps(self._payload, ensure_ascii=False)
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        return self._payload


def _billing_test_mode_enabled() -> bool:
    # Billing-only safe test mode: mock Stripe PM and Vapi add-card responses.
    # It is intentionally opt-in so normal live behavior is unchanged.
    return _env_bool("BILLING_TEST_MODE", False) or _env_bool("BILLING_BILLING_TEST_MODE", False)


def _billing_mock_stripe_pm_enabled() -> bool:
    return _env_bool("BILLING_MOCK_STRIPE_PM", _billing_test_mode_enabled())


def _billing_mock_add_card_enabled() -> bool:
    return _env_bool("BILLING_MOCK_ADD_CARD", _billing_test_mode_enabled())


def _mock_payment_method_id(card_tail: str = "") -> str:
    tail = re.sub(r"\D", "", str(card_tail or ""))[-4:] or "0000"
    return f"pm_mock_{tail}_{uuid.uuid4().hex[:18]}"


def _mock_card_brand(card_number: str = "") -> str:
    number = re.sub(r"\D", "", str(card_number or ""))
    if number.startswith("4"):
        return "visa"
    try:
        prefix4 = int((number + "0000")[:4])
    except Exception:
        prefix4 = 0
    if number[:2] in {str(i) for i in range(51, 56)} or 2221 <= prefix4 <= 2720:
        return "mastercard"
    if number.startswith(("34", "37")):
        return "amex"
    return "unknown"


def _mock_stripe_payment_method_payload(email_addr: str = "", card_number: str = "", pm_id: str = "") -> dict:
    number = re.sub(r"\D", "", str(card_number or ""))
    tail = number[-4:] if number else "0000"
    brand = _mock_card_brand(number)
    return {
        "id": pm_id or _mock_payment_method_id(tail),
        "object": "payment_method",
        "allow_redisplay": "unspecified",
        "billing_details": {"address": {}, "email": email_addr or None, "name": None, "phone": None},
        "card": {
            "brand": brand,
            "checks": {"address_line1_check": None, "address_postal_code_check": None, "cvc_check": "unchecked"},
            "country": "US",
            "display_brand": brand,
            "exp_month": 12,
            "exp_year": datetime.now(timezone.utc).year + 5,
            "funding": "credit",
            "generated_from": None,
            "last4": tail,
            "networks": {"available": [], "preferred": None},
            "three_d_secure_usage": {"supported": True},
            "wallet": None,
        },
        "created": int(time.time()),
        "customer": None,
        "livemode": False,
        "metadata": {},
        "type": "card",
    }


def _mock_add_card_response(pm_id: str = "") -> tuple[int, dict]:
    status = _env_int("BILLING_MOCK_ADD_CARD_STATUS", 201)
    if _env_bool("BILLING_MOCK_ADD_CARD_DECLINE", False):
        status = 400
    if status not in (200, 201):
        message = os.getenv("BILLING_MOCK_ADD_CARD_ERROR", "Couldn't Attach Payment Method. Stripe Error: Your card was declined.")
        return status, {"message": message, "error": "Bad Request", "statusCode": status, "mock": True}
    pm_id = pm_id or _mock_payment_method_id()
    return status, {"stripePaymentMethodId": pm_id, "paymentMethodId": pm_id, "mock": True}




def _extract_payment_method_id_from_add_card_body(raw: str = "") -> str:
    try:
        payload = json.loads(raw or "{}")
        if isinstance(payload, dict):
            return str(payload.get("paymentMethodId") or payload.get("stripePaymentMethodId") or "")
    except Exception:
        pass
    try:
        for key, value in parse_qsl(raw or "", keep_blank_values=True):
            if key in ("paymentMethodId", "stripePaymentMethodId"):
                return str(value or "")
    except Exception:
        pass
    return ""

def _classify_add_card_failure(status: int | str | None, body: str | dict | None) -> dict:
    if isinstance(body, dict):
        payload = body
        body_text = json.dumps(body, ensure_ascii=False)
    else:
        body_text = str(body or "")
        try:
            payload = json.loads(body_text) if body_text else {}
        except Exception:
            payload = {}
    lowered = body_text.lower()
    category = "unknown"
    if "invalid csrf" in lowered or "csrf" in lowered:
        category = "csrf_or_session"
    elif "card was declined" in lowered or "card_declined" in lowered or "decline_code" in lowered:
        category = "card_declined"
    elif "couldn't attach payment method" in lowered or "couldn’t attach payment method" in lowered:
        category = "attach_rejected"
    elif str(status) in ("401", "403"):
        category = "auth_or_session"
    elif str(status) == "429":
        category = "rate_limited"
    elif str(status).startswith("5"):
        category = "server_error"
    message = ""
    error = ""
    status_code = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or "")
        error = str(payload.get("error") or "")
        status_code = payload.get("statusCode")
    return {
        "status": status,
        "category": category,
        "message": (message or body_text)[:300],
        "error": error[:120],
        "statusCode": status_code,
    }


def _stripe_pm_page_html() -> str:
    return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Stripe PaymentMethod</title>
    <style>
      body { margin: 24px; font-family: Arial, sans-serif; background: #fff; }
      .field { width: 420px; min-height: 44px; margin: 12px 0; padding: 12px; border: 1px solid #d0d5dd; border-radius: 6px; background: #fff; }
    </style>
  </head>
  <body>
    <div id="card-number" class="field"></div>
    <div id="card-expiry" class="field"></div>
    <div id="card-cvc" class="field"></div>
  </body>
</html>
"""


async def _open_stripe_pm_page(context, page):
    async def fulfill(route):
        await route.fulfill(status=200, content_type="text/html", body=_stripe_pm_page_html())

    await context.route("https://dashboard.vapi.ai/settings/billing**", fulfill)
    await _goto_with_abort_tolerance(page, "https://dashboard.vapi.ai/settings/billing", wait_until="domcontentloaded", timeout=60000, label="stripe pm page")


def _request_id() -> str:
    return f"dash_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"


def _new_client_context() -> dict:
    return {
        "device_fingerprint_token": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "verification_id": str(uuid.uuid4()),
        "dodgeball_source_token": "",
        "browser_storage_state": None,
    }


def _dashboard_headers(
    user_agent: str = "",
    authorization: str = "",
    referer: str = "https://dashboard.vapi.ai/",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
    include_device_fingerprint: bool = False,
    include_session_headers: bool = False,
) -> dict:
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://dashboard.vapi.ai",
        "Referer": referer,
        "User-Agent": user_agent or "Mozilla/5.0",
        "X-Client-Source": "dashboard",
        "X-Client-Platform": "web",
        "X-DASHBOARD-VERSION": DASHBOARD_VERSION,
        "X-Request-ID": _request_id(),
    }
    headers.update(_fingerprint_extra_headers(browser_fingerprint))
    if include_device_fingerprint:
        ctx = client_ctx or {}
        headers["x-device-fingerprint-token"] = ctx.get("device_fingerprint_token") or str(uuid.uuid4())
    if include_session_headers:
        ctx = client_ctx or {}
        # 真实 dashboard 当前并不会在普通 API 请求上稳定发送这两个头。
        # 仅保留显式开关，避免默认暴露 "undefined" 这类自动化痕迹。
        headers["x-session-id"] = ctx.get("session_id") or "undefined"
        headers["x-verification-id"] = ctx.get("verification_id") or "undefined"
    if authorization:
        headers["Authorization"] = authorization
    return headers


def _refresh_dashboard_version(proxies=None, user_agent: str = ""):
    """从当前 dashboard bundle 提取实时 X-DASHBOARD-VERSION，避免协议段使用旧 hash。"""
    global DASHBOARD_VERSION, _DASHBOARD_VERSION_REFRESHED
    if _DASHBOARD_VERSION_REFRESHED or os.getenv("DASHBOARD_VERSION"):
        return DASHBOARD_VERSION

    _DASHBOARD_VERSION_REFRESHED = True
    try:
        r = cffi_requests.get(
            "https://dashboard.vapi.ai/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": user_agent or "Mozilla/5.0",
            },
            proxies=proxies,
            impersonate="chrome",
            timeout=20,
        )
        if r.status_code != 200:
            return DASHBOARD_VERSION
        scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', r.text)
        for src in scripts:
            if "/js/" not in src:
                continue
            if src.startswith("//"):
                js_url = "https:" + src
            elif src.startswith("/"):
                js_url = "https://dashboard.vapi.ai" + src
            elif src.startswith("http"):
                js_url = src
            else:
                js_url = "https://dashboard.vapi.ai/" + src.lstrip("./")
            js = cffi_requests.get(
                js_url,
                headers={
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://dashboard.vapi.ai/",
                    "User-Agent": user_agent or "Mozilla/5.0",
                },
                proxies=proxies,
                impersonate="chrome",
                timeout=30,
            )
            if js.status_code != 200:
                continue
            match = re.search(r"daily-\d{4}-\d{2}-\d{2}-(?:\d{4}|\d{2}-\d{2})", js.text)
            if match:
                DASHBOARD_VERSION = match.group(0)
                log.info(f"当前 dashboard version: {DASHBOARD_VERSION}")
                return DASHBOARD_VERSION
    except Exception as e:
        log.debug(f"刷新 dashboard version 失败，沿用默认值: {e}")
    return DASHBOARD_VERSION


def _make_session(proxy_url: str = ""):
    """创建带代理的 curl_cffi session"""
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    return proxies


def _proxy_value_from_text(value: str, default_proxy: str = "") -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered in ("", "default", "source", "input"):
        return default_proxy
    if lowered in ("0", "none", "no", "off", "direct", "direct://"):
        return ""
    return text


def _billing_proxy_sequence() -> list[str]:
    raw = os.getenv("BILLING_BIND_PROXY_SEQUENCE", os.getenv("BILLING_PROXY_SEQUENCE", "")).strip()
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[;,\n]+", raw) if part.strip()]




def _billing_proxy_sequence_state_path() -> Path:
    return Path(os.getenv("BILLING_PROXY_SEQUENCE_STATE", "/data/billing-proxy-sequence-state.json"))


def _init_billing_proxy_index_for_registration(client_ctx: dict | None) -> None:
    if not isinstance(client_ctx, dict):
        return
    if not _env_bool("BILLING_PROXY_SEQUENCE_ROTATE_PER_ACCOUNT", True):
        return
    if "billing_proxy_index" in client_ctx:
        return
    sequence = _billing_proxy_sequence()
    if not sequence:
        return
    path = _billing_proxy_sequence_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            file.seek(0)
            raw = file.read().strip()
            try:
                state = json.loads(raw) if raw else {}
            except Exception:
                state = {}
            cursor = int(state.get("cursor", 0) or 0)
            client_ctx["billing_proxy_index"] = cursor % len(sequence)
            state.update({
                "cursor": cursor + 1,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "lastProxyIndex": client_ctx["billing_proxy_index"],
                "sequenceSize": len(sequence),
            })
            file.seek(0)
            file.truncate()
            file.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            log.info(
                f"billing proxy sequence per-account: cursor={cursor}->{cursor + 1} "
                f"index={client_ctx['billing_proxy_index']}"
            )
    except Exception as e:
        log.warning(f"billing proxy sequence state unavailable: {e}")


def _billing_stage_proxy(proxy_url: str = "", client_ctx: dict | None = None) -> str:
    """绑卡阶段代理覆盖：支持固定代理和 retry 序列，direct 表示直连。"""
    ctx = client_ctx if isinstance(client_ctx, dict) else None
    def _force_cloak_warp(value: str) -> str:
        selected = str(value or "")
        engine = os.getenv("BILLING_BROWSER_ENGINE", "playwright").strip().lower()
        if engine not in ("cloak", "cloakbrowser") or not _env_bool("BILLING_CLOAK_FORCE_WARP", True):
            return selected
        if selected and selected.strip().lower() not in ("0", "none", "no", "off", "direct", "direct://"):
            return selected
        for name in ("BILLING_BIND_PROXY", "WARP_PROXY_URL", "SOCKS5_PROXY"):
            candidate = str(os.getenv(name) or "").strip()
            if candidate and candidate.lower() not in ("0", "none", "no", "off", "direct", "direct://"):
                return candidate
        return ""

    if ctx and "billing_proxy_override" in ctx:
        return _force_cloak_warp(_proxy_value_from_text(str(ctx.get("billing_proxy_override") or ""), proxy_url))

    sequence = _billing_proxy_sequence()
    if sequence:
        idx = int((ctx or {}).get("billing_proxy_index", 0) or 0)
        return _force_cloak_warp(_proxy_value_from_text(sequence[idx % len(sequence)], proxy_url))

    override = os.getenv("BILLING_BIND_PROXY", os.getenv("BILLING_BIND_PROXY_URL", "")).strip()
    if not override:
        return _force_cloak_warp(proxy_url)
    return _force_cloak_warp(_proxy_value_from_text(override, proxy_url))


def _mask_id(value: str) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:7]}...{value[-6:]}"


def _summarize_json_error(response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text[:300]

    if not isinstance(payload, dict):
        return str(payload)[:300]

    error = payload.get("error")
    parts = []
    if isinstance(error, dict):
        for key in ("message", "code", "decline_code", "type"):
            value = error.get(key)
            if value:
                parts.append(f"{key}={value}")
    for key in ("message", "error", "statusCode"):
        value = payload.get(key)
        if value and not isinstance(value, (dict, list)):
            parts.append(f"{key}={value}")
    return "; ".join(parts)[:300] or json.dumps(payload, ensure_ascii=False)[:300]


def _hash_value(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _sensitive_name(name: str) -> bool:
    lowered = str(name or "").lower()
    return any(part in lowered for part in (
        "authorization",
        "cookie",
        "token",
        "csrf",
        "secret",
        "password",
        "cvc",
        "number",
        "card",
        "key",
        "paymentmethod",
        "guid",
        "muid",
        "sid",
    ))


def _safe_value(name: str, value) -> object:
    text = "" if value is None else str(value)
    if not _sensitive_name(name):
        return text

    item = {
        "redacted": True,
        "length": len(text),
        "sha256": _hash_value(text),
    }
    if name.lower() in ("authorization", "authorization".lower()) and text.lower().startswith("bearer "):
        token = text.split(" ", 1)[1]
        payload = _decode_jwt_payload(token)
        item.update({
            "scheme": "Bearer",
            "tokenLength": len(token),
            "tokenSha256": _hash_value(token),
            "jwtClaims": {
                key: payload.get(key)
                for key in ("iss", "sub", "aud", "exp", "iat", "role", "email", "orgId", "organizationId")
                if payload.get(key) is not None
            },
        })
    elif text:
        item["prefix"] = text[:8]
        item["suffix"] = text[-6:] if len(text) > 6 else text
    return item


def _safe_headers(headers: dict) -> dict:
    return {name: _safe_value(name, value) for name, value in sorted((headers or {}).items())}


def _env_string(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip()


def _env_disabled_text(value: str) -> bool:
    return str(value or "").strip().lower() in ("", "0", "false", "no", "off", "none", "null")


def _billing_dodgeball_refresh_enabled(label: str = "") -> bool:
    specific = ""
    normalized = str(label or "").strip().upper().replace("-", "_")
    if normalized:
        specific = _env_string(f"BILLING_REFRESH_DODGEBALL_{normalized}", "")
    if specific:
        return not _env_disabled_text(specific)
    value = os.getenv("BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD")
    if str(label or "") == "before-add-card":
        # 新增风控样本显示，点击保存前主动调用 getSourceToken() 会多出一轮
        # sourceToken 请求；最后稳定成功样本主要依赖页面自然产生的 Dodgeball token。
        # 因此默认不主动刷新，保留 BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD=1 作为回滚/A-B 开关。
        if value is None:
            return False
        return not _env_disabled_text(value)
    return _env_bool("BILLING_REFRESH_DODGEBALL", False)


def _safe_client_context(client_ctx: dict | None) -> dict:
    ctx = client_ctx or {}
    storage_state = ctx.get("browser_storage_state") if isinstance(ctx, dict) else None
    return {
        "deviceFingerprintToken": _safe_value("x-device-fingerprint-token", ctx.get("device_fingerprint_token", "")),
        "dodgeballSourceTokenSha256": _hash_value(ctx.get("dodgeball_source_token", "")) if ctx.get("dodgeball_source_token") else "",
        "hasBrowserStorageState": bool(storage_state),
        "storageCookieCount": len((storage_state or {}).get("cookies") or []) if isinstance(storage_state, dict) else 0,
        "storageOriginCount": len((storage_state or {}).get("origins") or []) if isinstance(storage_state, dict) else 0,
    }


def _write_billing_debug(kind: str, payload: dict) -> str:
    if not _env_bool("BILLING_ADD_CARD_DEBUG", True):
        return ""
    try:
        debug_dir = Path("/data/browser-bind-debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        status = "na"
        response = payload.get("response") if isinstance(payload, dict) else None
        if isinstance(response, dict) and response.get("status") is not None:
            status = str(response.get("status"))
        path = debug_dir / f"{kind}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}-{status}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return str(path)
    except Exception as e:
        log.debug(f"写入 billing debug 失败: {e}")
        return ""


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query.append((key, json.dumps(_safe_value(key, value), ensure_ascii=False) if _sensitive_name(key) else value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except Exception:
        return url


def _safe_post_data(text: str) -> object:
    if not text:
        return ""
    try:
        payload = json.loads(text)
        return _safe_payload(payload)
    except Exception:
        pass
    try:
        pairs = parse_qsl(text, keep_blank_values=True)
        if pairs:
            return {key: _safe_value(key, value) for key, value in pairs}
    except Exception:
        pass
    return _safe_value("body", text)


def _safe_payload(value) -> object:
    if isinstance(value, dict):
        return {key: _safe_payload(value[key]) if not _sensitive_name(key) else _safe_value(key, value[key]) for key in value}
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    return value


def _network_interesting(url: str) -> bool:
    return any(part in url for part in (
        "dashboard.vapi.ai",
        "api.vapi.ai",
        "api.stripe.com",
        "js.stripe.com",
        "m.stripe.network",
        "r.stripe.com",
        "q.stripe.com",
        "api.dodgeballhq.com",
    ))




def _redacted_sha(value) -> str:
    if isinstance(value, dict):
        return str(value.get("sha256") or "")
    return ""


def _billing_rstripe_event_names(event: dict) -> list[str]:
    post = event.get("postData") if isinstance(event, dict) else None
    if not isinstance(post, dict):
        return []
    raw = post.get("events") or ""
    if isinstance(raw, (dict, list)):
        raw = json.dumps(raw, ensure_ascii=False)
    text = str(raw or "")
    if not text:
        return []
    names = re.findall(r'"event_name"\s*:\s*"([^"]+)"', text)
    if names:
        return names
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item.get("event_name") or "") for item in parsed if isinstance(item, dict) and item.get("event_name")]
    except Exception:
        pass
    return []


def _billing_network_anti_fraud_summary(network_log: dict | None) -> dict:
    """Summarize fraud-token ordering for future billing samples without exposing tokens."""
    if not isinstance(network_log, dict):
        return {}
    events = [ev for ev in (network_log.get("events") or []) if isinstance(ev, dict)]
    add_req = None
    stripe_req = None
    source_req_indices: list[int] = []
    source_resp_statuses: list[object] = []
    rstripe_names: list[str] = []
    for idx, ev in enumerate(events):
        url = str(ev.get("url") or "")
        typ = ev.get("type")
        if typ == "request" and "api.stripe.com/v1/payment_methods" in url:
            stripe_req = ev | {"_idx": idx}
        elif typ == "request" and "/stripe/add-card" in url:
            add_req = ev | {"_idx": idx}
        elif "api.dodgeballhq.com/v1/sourceToken" in url:
            if typ == "request":
                source_req_indices.append(idx)
            elif typ == "response":
                source_resp_statuses.append(ev.get("status"))
        elif typ == "request" and "r.stripe.com/b" in url:
            rstripe_names.extend(_billing_rstripe_event_names(ev))

    stripe_post = stripe_req.get("postData") if isinstance(stripe_req, dict) and isinstance(stripe_req.get("postData"), dict) else {}
    add_headers = add_req.get("headers") if isinstance(add_req, dict) and isinstance(add_req.get("headers"), dict) else {}
    add_idx = add_req.get("_idx") if isinstance(add_req, dict) else None
    stripe_idx = stripe_req.get("_idx") if isinstance(stripe_req, dict) else None
    source_before_add = sum(1 for idx in source_req_indices if isinstance(add_idx, int) and idx < add_idx)
    source_between = sum(1 for idx in source_req_indices if isinstance(stripe_idx, int) and isinstance(add_idx, int) and stripe_idx < idx < add_idx)
    human_keys = sorted(k for k in stripe_post if str(k).startswith("radar_options[human_security") or str(k).startswith("radar_options[px"))
    refresh = network_log.get("dodgeballRefresh") if isinstance(network_log.get("dodgeballRefresh"), list) else []
    before_refresh = [r for r in refresh if isinstance(r, dict) and r.get("label") == "before-add-card"]
    last_refresh = next((r for r in reversed(refresh) if isinstance(r, dict) and r.get("tokenSha256")), {})
    add_device_sha = _redacted_sha(add_headers.get("x-device-fingerprint-token"))
    refresh_token_sha = str((last_refresh or {}).get("tokenSha256") or "")
    refresh_device_sha = str((last_refresh or {}).get("deviceTokenSha256") or "")
    pua = str(stripe_post.get("payment_user_agent") or "")
    return {
        "sourceTokenRequestCount": len(source_req_indices),
        "sourceTokenResponseStatuses": source_resp_statuses,
        "sourceTokenBeforeAddCard": source_before_add,
        "sourceTokenBetweenStripePmAndAddCard": source_between,
        "dodgeballRefreshLabels": [r.get("label") for r in refresh if isinstance(r, dict)],
        "beforeAddCardRefreshActiveGenerated": any(bool(r.get("activeGenerated")) for r in before_refresh),
        "addCardDeviceFingerprintSha256": add_device_sha,
        "lastRefreshTokenSha256": refresh_token_sha,
        "lastRefreshDeviceTokenSha256": refresh_device_sha,
        "deviceFingerprintMatchesRefreshToken": bool(add_device_sha and refresh_token_sha and add_device_sha == refresh_token_sha),
        "deviceFingerprintMatchesRefreshDeviceToken": bool(add_device_sha and refresh_device_sha and add_device_sha == refresh_device_sha),
        "stripePaymentUserAgent": pua,
        "stripeKnownGoodPua": "ab68db42e2" in pua,
        "stripeHasHcaptchaToken": "radar_options[hcaptcha_token]" in stripe_post,
        "stripeHumanSecurityKeys": human_keys[:12],
        "stripeTimeOnPage": stripe_post.get("time_on_page") or "",
        "rStripeEventCount": len(rstripe_names),
        "rStripeHasCaptchaEvent": any("captcha" in name for name in rstripe_names),
        "rStripeHasConsumeTokenEvent": any("consume_token" in name for name in rstripe_names),
        "rStripeEventSample": list(dict.fromkeys(rstripe_names))[:12],
    }

def _fingerprint_user_agent(browser_fingerprint, fallback: str = "") -> str:
    if isinstance(browser_fingerprint, dict):
        return str(
            browser_fingerprint.get("user_agent")
            or browser_fingerprint.get("userAgent")
            or fallback
            or ""
        )
    if browser_fingerprint:
        return str(browser_fingerprint)
    return fallback or ""


def _fingerprint_extra_headers(browser_fingerprint) -> dict:
    accept_language = os.getenv("BILLING_ACCEPT_LANGUAGE", "en-US,en;q=0.9")
    headers = {"Accept-Language": accept_language}
    if os.getenv("BILLING_BROWSER_ENGINE", "playwright").strip().lower() in ("cloak", "cloakbrowser") and _env_bool("BILLING_CLOAK_NATIVE_UA", True):
        return headers
    if isinstance(browser_fingerprint, dict):
        sec_ch_ua = browser_fingerprint.get("sec_ch_ua") or browser_fingerprint.get("secChUa") or ""
        if sec_ch_ua:
            platform = (
                browser_fingerprint.get("sec_ch_ua_platform")
                or browser_fingerprint.get("secChUaPlatform")
                or browser_fingerprint.get("ua_platform")
                or browser_fingerprint.get("uaPlatform")
                or "Windows"
            )
            mobile = browser_fingerprint.get("sec_ch_ua_mobile") or browser_fingerprint.get("secChUaMobile") or "?0"
            platform_text = str(platform)
            if not (platform_text.startswith('"') and platform_text.endswith('"')):
                platform_text = json.dumps(platform_text)
            headers["sec-ch-ua"] = str(sec_ch_ua)
            headers["sec-ch-ua-mobile"] = str(mobile)
            headers["sec-ch-ua-platform"] = platform_text
    return headers


def _chrome_version_from_fingerprint(browser_fingerprint: dict | None = None, user_agent: str = "") -> str:
    if isinstance(browser_fingerprint, dict):
        version = str(browser_fingerprint.get("browser_version") or browser_fingerprint.get("browserVersion") or "").strip()
        if version:
            return version
    match = re.search(r"(?:Chrome|Chromium)/([0-9.]+)", str(user_agent or _fingerprint_user_agent(browser_fingerprint, "") or ""))
    if match:
        return match.group(1)
    return "148.0.7778.96"

def _billing_user_agent_override() -> str:
    raw = (
        os.getenv("BILLING_BROWSER_USER_AGENT")
        or os.getenv("BILLING_UA_OVERRIDE")
        or os.getenv("BILLING_DEFAULT_UA")
        or ""
    ).strip()
    if not raw or raw.lower() in ("0", "false", "no", "off", "none"):
        return ""
    if raw.startswith("Mozilla/"):
        return raw
    # 允许只传 Chrome 完整版本号，例如 148.0.7778.72。
    if re.fullmatch(r"\d+(?:\.\d+){1,3}", raw):
        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{raw} Safari/537.36"
    return raw


def _apply_billing_user_agent_override(browser_fingerprint: dict | None) -> dict | None:
    ua = _billing_user_agent_override()
    if not ua:
        return browser_fingerprint
    fp = dict(browser_fingerprint or {})
    version = _chrome_version_from_fingerprint(None, ua)
    major = (version.split(".", 1)[0] if version else "148") or "148"
    fp["user_agent"] = ua
    fp["userAgent"] = ua
    fp["browser_version"] = version
    fp["browserVersion"] = version
    fp["browser_name"] = fp.get("browser_name") or "Chrome"
    fp["sec_ch_ua"] = f'"Not;A=Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'
    fp["secChUa"] = fp["sec_ch_ua"]
    fp.setdefault("sec_ch_ua_mobile", "?0")
    fp.setdefault("secChUaMobile", "?0")
    return fp


def _billing_locale() -> str:
    return os.getenv("BILLING_LOCALE", "en-US").strip() or "en-US"


def _billing_timezone_id() -> str:
    # WARP 当前出口实测在 Germany/Dreieich；默认先让 JS timezone 不再暴露 UTC。
    return os.getenv("BILLING_TIMEZONE_ID", "Europe/Berlin").strip() or "Europe/Berlin"


def _billing_default_profile() -> dict:
    return {
        "viewport": {"width": 1365, "height": 900},
        "screen": {"width": 1365, "height": 900},
        "navigatorPlatform": "Win32",
        "uaPlatform": "Windows",
        "uaPlatformVersion": "10.0.0",
        "language": "en-US",
        "languages": ["en-US", "en"],
        "hardwareConcurrency": 8,
        "deviceMemory": 8,
        "maxTouchPoints": 0,
        "canvasSeed": 0,
        "audioNoise": 0.0,
        "webglVendor": "Google Inc. (NVIDIA)",
        "webglRenderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    }


def _billing_random_profile() -> dict:
    profiles = [
        ({"width": 1365, "height": 900}, {"width": 1365, "height": 900}, 8, 8, "Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ({"width": 1366, "height": 768}, {"width": 1366, "height": 768}, 8, 8, "Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ({"width": 1440, "height": 900}, {"width": 1440, "height": 900}, 8, 8, "Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ({"width": 1536, "height": 864}, {"width": 1536, "height": 864}, 12, 8, "Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ({"width": 1600, "height": 900}, {"width": 1600, "height": 900}, 16, 8, "Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ({"width": 1920, "height": 1080}, {"width": 1920, "height": 1080}, 12, 8, "Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ]
    viewport, screen, cores, memory, vendor, renderer = random.choice(profiles)
    profile = _billing_default_profile()
    profile.update({
        "viewport": dict(viewport),
        "screen": dict(screen),
        "hardwareConcurrency": cores,
        "deviceMemory": memory,
        "canvasSeed": random.randint(1, 2147483647),
        "audioNoise": random.choice([0.0000003, -0.0000003, 0.0000005, -0.0000005]),
        "webglVendor": vendor,
        "webglRenderer": renderer,
    })
    return profile


def _billing_profile_from_fingerprint(browser_fingerprint: dict | None = None) -> dict:
    if isinstance(browser_fingerprint, dict) and isinstance(browser_fingerprint.get("__billing_profile"), dict):
        profile = _billing_default_profile()
        profile.update(browser_fingerprint.get("__billing_profile") or {})
        if not isinstance(profile.get("viewport"), dict):
            profile["viewport"] = _billing_default_profile()["viewport"]
        if not isinstance(profile.get("screen"), dict):
            profile["screen"] = _billing_default_profile()["screen"]
        return profile
    return _billing_default_profile()


def _with_billing_profile(browser_fingerprint: dict | None, client_ctx: dict | None = None) -> dict:
    fp = dict(browser_fingerprint or {})
    ctx = client_ctx if isinstance(client_ctx, dict) else None
    force_retry_random = bool(ctx and ctx.get("billing_force_random_profile")) and _env_bool("BILLING_RETRY_FORCE_RANDOM_FINGERPRINT", True)
    if not force_retry_random and not _env_bool("BILLING_RANDOMIZE_FINGERPRINT", True):
        profile = _billing_default_profile()
    else:
        profile = (ctx or {}).get("billing_fingerprint_profile") if ctx else None
        if not isinstance(profile, dict):
            profile = _billing_random_profile()
            if ctx is not None:
                ctx["billing_fingerprint_profile"] = profile
    fp["__billing_profile"] = profile
    fp.setdefault("sec_ch_ua_platform", profile.get("uaPlatform", "Windows"))
    fp.setdefault("secChUaPlatform", profile.get("uaPlatform", "Windows"))
    fp = _apply_billing_user_agent_override(fp) or fp
    return fp


def _billing_profile_summary(browser_fingerprint: dict | None = None) -> dict:
    profile = _billing_profile_from_fingerprint(browser_fingerprint)
    return {
        "viewport": profile.get("viewport"),
        "screen": profile.get("screen"),
        "hardwareConcurrency": profile.get("hardwareConcurrency"),
        "deviceMemory": profile.get("deviceMemory"),
        "canvasSeed": profile.get("canvasSeed"),
        "audioNoise": profile.get("audioNoise"),
        "webglVendor": profile.get("webglVendor"),
        "webglRenderer": profile.get("webglRenderer"),
    }


def _billing_fingerprint_init_script(browser_fingerprint: dict | None = None, user_agent: str = "") -> str:
    if os.getenv("BILLING_BROWSER_ENGINE", "playwright").strip().lower() in ("cloak", "cloakbrowser") and _env_bool("BILLING_CLOAK_NATIVE_FINGERPRINT", True) and not _env_bool("BILLING_CLOAK_TOP_FRAME_PATCH", False):
        return _billing_cloak_noop_init_script()
    full_version = _chrome_version_from_fingerprint(browser_fingerprint, user_agent)
    major_version = (full_version.split(".", 1)[0] or "148") if full_version else "148"
    profile = _billing_profile_from_fingerprint(browser_fingerprint)
    script = r"""
(() => {
  const defineGetter = (obj, prop, value) => {
    try {
      // Dodgeball/anti-fraud collectors probe navigator fields by temporarily
      // assigning to them. A getter without a setter throws in strict mode
      // (observed: "Cannot set property platform ... which has only a getter")
      // and poisons the sourceToken fingerprint. Keep the value stable while
      // making assignment probes no-op instead of errors.
      Object.defineProperty(obj, prop, { get: () => value, set: () => true, configurable: true });
    } catch (_) {}
  };

  defineGetter(Navigator.prototype, 'webdriver', undefined);
  defineGetter(Navigator.prototype, 'platform', 'Win32');
  defineGetter(Navigator.prototype, 'languages', ['en-US', 'en']);
  defineGetter(Navigator.prototype, 'language', 'en-US');
  defineGetter(Navigator.prototype, 'hardwareConcurrency', 8);
  defineGetter(Navigator.prototype, 'deviceMemory', 8);
  defineGetter(Navigator.prototype, 'maxTouchPoints', 0);

  try {
    const brands = [
      { brand: 'Not;A=Brand', version: '99' },
      { brand: 'Google Chrome', version: '148' },
      { brand: 'Chromium', version: '148' },
    ];
    const fullVersion = '148.0.7778.96';
    const highEntropy = {
      architecture: 'x86',
      bitness: '64',
      brands,
      fullVersionList: [
        { brand: 'Not;A=Brand', version: '99.0.0.0' },
        { brand: 'Google Chrome', version: fullVersion },
        { brand: 'Chromium', version: fullVersion },
      ],
      mobile: false,
      model: '',
      platform: 'Windows',
      platformVersion: '10.0.0',
      uaFullVersion: fullVersion,
      wow64: false,
    };
    const uaData = {
      brands,
      mobile: false,
      platform: 'Windows',
      getHighEntropyValues: async (hints) => {
        const out = {};
        for (const hint of hints || []) {
          if (Object.prototype.hasOwnProperty.call(highEntropy, hint)) out[hint] = highEntropy[hint];
        }
        return out;
      },
      toJSON: () => ({ brands, mobile: false, platform: 'Windows' }),
    };
    Object.defineProperty(Navigator.prototype, 'userAgentData', {
      get: () => uaData,
      set: () => true,
      configurable: true,
    });
  } catch (_) {}

  try {
    if (!window.chrome) {
      Object.defineProperty(window, 'chrome', {
        value: { runtime: {}, app: {}, csi: () => ({}), loadTimes: () => ({}) },
        configurable: true,
      });
    } else if (!window.chrome.runtime) {
      Object.defineProperty(window.chrome, 'runtime', { value: {}, configurable: true });
    }
  } catch (_) {}

  try {
    const permissionsProto = navigator.permissions && Object.getPrototypeOf(navigator.permissions);
    const originalQuery = permissionsProto && permissionsProto.query;
    if (permissionsProto && originalQuery && !originalQuery.__vapiFpPatched) {
      const patchedQuery = function(parameters) {
        if (parameters && parameters.name === 'notifications') {
          return Promise.resolve({ state: Notification.permission });
        }
        return originalQuery.apply(this, arguments);
      };
      Object.defineProperty(patchedQuery, '__vapiFpPatched', { value: true });
      permissionsProto.query = patchedQuery;
    }
  } catch (_) {}

  try {
    const uaDataProto = Object.getPrototypeOf(navigator.userAgentData);
    defineGetter(uaDataProto, 'platform', 'Windows');
    defineGetter(uaDataProto, 'mobile', false);
  } catch (_) {}

  const patchWebGL = (proto) => {
    if (!proto || proto.__vapiFpPatched) return;
    const originalGetParameter = proto.getParameter;
    const originalGetExtension = proto.getExtension;
    Object.defineProperty(proto, '__vapiFpPatched', { value: true });
    proto.getExtension = function(name) {
      if (name === 'WEBGL_debug_renderer_info') {
        return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
      }
      return originalGetExtension.apply(this, arguments);
    };
    proto.getParameter = function(parameter) {
      if (parameter === 37445) return 'Google Inc. (NVIDIA)';
      if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return originalGetParameter.apply(this, arguments);
    };
  };
  patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  try {
    const seed = 0;
    if (seed) {
      const perturb = (canvas) => {
        try {
          const w = canvas && canvas.width, h = canvas && canvas.height;
          if (!w || !h) return;
          const ctx = canvas.getContext && canvas.getContext('2d');
          if (!ctx || !ctx.getImageData || !ctx.putImageData) return;
          const x = Math.abs(seed) % Math.max(1, w);
          const y = Math.floor(Math.abs(seed) / 97) % Math.max(1, h);
          const img = ctx.getImageData(x, y, 1, 1);
          img.data[0] = (img.data[0] + (seed & 7)) & 255;
          img.data[1] = (img.data[1] + ((seed >> 3) & 7)) & 255;
          img.data[2] = (img.data[2] + ((seed >> 6) & 7)) & 255;
          ctx.putImageData(img, x, y);
        } catch (_) {}
      };
      const proto = window.HTMLCanvasElement && HTMLCanvasElement.prototype;
      if (proto && !proto.__vapiCanvasPatched) {
        Object.defineProperty(proto, '__vapiCanvasPatched', { value: true });
        const origToDataURL = proto.toDataURL;
        const origToBlob = proto.toBlob;
        proto.toDataURL = function() { perturb(this); return origToDataURL.apply(this, arguments); };
        if (origToBlob) proto.toBlob = function() { perturb(this); return origToBlob.apply(this, arguments); };
      }
    }
  } catch (_) {}

  try {
    const noise = 0;
    if (noise) {
      const proto = window.AudioBuffer && AudioBuffer.prototype;
      if (proto && proto.getChannelData && !proto.__vapiAudioPatched) {
        Object.defineProperty(proto, '__vapiAudioPatched', { value: true });
        const orig = proto.getChannelData;
        proto.getChannelData = function() {
          const data = orig.apply(this, arguments);
          try { if (data && data.length > 128) data[127] = data[127] + noise; } catch (_) {}
          return data;
        };
      }
    }
  } catch (_) {}
})();
"""
    replacements = {
        "defineGetter(Navigator.prototype, 'platform', 'Win32');":
            f"defineGetter(Navigator.prototype, 'platform', {json.dumps(str(profile.get('navigatorPlatform') or 'Win32'))});",
        "defineGetter(Navigator.prototype, 'languages', ['en-US', 'en']);":
            f"defineGetter(Navigator.prototype, 'languages', {json.dumps(profile.get('languages') or ['en-US', 'en'])});",
        "defineGetter(Navigator.prototype, 'language', 'en-US');":
            f"defineGetter(Navigator.prototype, 'language', {json.dumps(str(profile.get('language') or 'en-US'))});",
        "defineGetter(Navigator.prototype, 'hardwareConcurrency', 8);":
            f"defineGetter(Navigator.prototype, 'hardwareConcurrency', {int(profile.get('hardwareConcurrency') or 8)});",
        "defineGetter(Navigator.prototype, 'deviceMemory', 8);":
            f"defineGetter(Navigator.prototype, 'deviceMemory', {int(profile.get('deviceMemory') or 8)});",
        "defineGetter(Navigator.prototype, 'maxTouchPoints', 0);":
            f"defineGetter(Navigator.prototype, 'maxTouchPoints', {int(profile.get('maxTouchPoints') or 0)});",
        "platform: 'Windows'": f"platform: {json.dumps(str(profile.get('uaPlatform') or 'Windows'))}",
        "platformVersion: '10.0.0'": f"platformVersion: {json.dumps(str(profile.get('uaPlatformVersion') or '10.0.0'))}",
        "defineGetter(uaDataProto, 'platform', 'Windows');":
            f"defineGetter(uaDataProto, 'platform', {json.dumps(str(profile.get('uaPlatform') or 'Windows'))});",
        "if (parameter === 37445) return 'Google Inc. (NVIDIA)';":
            f"if (parameter === 37445) return {json.dumps(str(profile.get('webglVendor') or 'Google Inc. (NVIDIA)'))};",
        "if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)';":
            f"if (parameter === 37446) return {json.dumps(str(profile.get('webglRenderer') or 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)'))};",
    }
    replacements["const seed = 0;"] = f"const seed = {int(profile.get('canvasSeed') or 0)};"
    replacements["const noise = 0;"] = f"const noise = {float(profile.get('audioNoise') or 0.0)};"
    for needle, value in replacements.items():
        script = script.replace(needle, value)
    return script.replace("148.0.7778.96", full_version).replace("version: '148'", f"version: '{major_version}'")


def _billing_minimal_init_script() -> str:
    return r"""
(() => {
  try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined, configurable: true }); } catch (_) {}
})();
"""


def _billing_cloak_noop_init_script() -> str:
    """CloakBrowser already patches fingerprint at the binary/source level.

    Do not override navigator.userAgentData/WebGL/Canvas/Audio from JS in Cloak
    mode; those JS shims can contradict the real 146 Cloak binary and Stripe /
    Vapi device collection.  Keep this script intentionally empty so call sites
    that unconditionally add_init_script stay harmless.
    """
    return "(() => {})();"


def _billing_safe_fingerprint_init_script(browser_fingerprint: dict | None = None, user_agent: str = "") -> str:
    """保守指纹补丁：修正 UA/平台明显矛盾，但不碰 WebGL/Canvas，避免 Stripe iframe 卡住。"""
    if os.getenv("BILLING_BROWSER_ENGINE", "playwright").strip().lower() in ("cloak", "cloakbrowser") and _env_bool("BILLING_CLOAK_NATIVE_FINGERPRINT", True):
        return _billing_cloak_noop_init_script()
    full_version = _chrome_version_from_fingerprint(browser_fingerprint, user_agent)
    major_version = (full_version.split(".", 1)[0] or "148") if full_version else "148"
    profile = _billing_profile_from_fingerprint(browser_fingerprint)
    navigator_platform = json.dumps(str(profile.get("navigatorPlatform") or "Win32"))
    language = json.dumps(str(profile.get("language") or "en-US"))
    languages = json.dumps(profile.get("languages") or ["en-US", "en"])
    ua_platform = json.dumps(str(profile.get("uaPlatform") or "Windows"))
    ua_platform_version = json.dumps(str(profile.get("uaPlatformVersion") or "10.0.0"))
    hardware_concurrency = int(profile.get("hardwareConcurrency") or 8)
    device_memory = int(profile.get("deviceMemory") or 8)
    max_touch_points = int(profile.get("maxTouchPoints") or 0)
    return f"""
(() => {{
  const defineGetter = (obj, prop, value) => {{
    try {{ Object.defineProperty(obj, prop, {{ get: () => value, set: () => true, configurable: true }}); }} catch (_) {{}}
  }};
  defineGetter(Navigator.prototype, 'webdriver', undefined);
  defineGetter(Navigator.prototype, 'platform', {navigator_platform});
  defineGetter(Navigator.prototype, 'languages', {languages});
  defineGetter(Navigator.prototype, 'language', {language});
  defineGetter(Navigator.prototype, 'hardwareConcurrency', {hardware_concurrency});
  defineGetter(Navigator.prototype, 'deviceMemory', {device_memory});
  defineGetter(Navigator.prototype, 'maxTouchPoints', {max_touch_points});
  try {{
    const brands = [
      {{ brand: 'Not;A=Brand', version: '99' }},
      {{ brand: 'Google Chrome', version: '{major_version}' }},
      {{ brand: 'Chromium', version: '{major_version}' }},
    ];
    const fullVersion = '{full_version}';
    const highEntropy = {{
      architecture: 'x86', bitness: '64', brands,
      fullVersionList: [
        {{ brand: 'Not;A=Brand', version: '99.0.0.0' }},
        {{ brand: 'Google Chrome', version: fullVersion }},
        {{ brand: 'Chromium', version: fullVersion }},
      ],
      mobile: false, model: '', platform: {ua_platform}, platformVersion: {ua_platform_version}, uaFullVersion: fullVersion, wow64: false,
    }};
    const uaData = {{
      brands, mobile: false, platform: {ua_platform},
      getHighEntropyValues: async (hints) => {{
        const out = {{}};
        for (const hint of hints || []) if (Object.prototype.hasOwnProperty.call(highEntropy, hint)) out[hint] = highEntropy[hint];
        return out;
      }},
      toJSON: () => ({{ brands, mobile: false, platform: {ua_platform} }}),
    }};
    Object.defineProperty(Navigator.prototype, 'userAgentData', {{ get: () => uaData, set: () => true, configurable: true }});
  }} catch (_) {{}}
  try {{
    if (!window.chrome) Object.defineProperty(window, 'chrome', {{ value: {{ runtime: {{}}, app: {{}}, csi: () => ({{}}), loadTimes: () => ({{}}) }}, configurable: true }});
    else if (!window.chrome.runtime) Object.defineProperty(window.chrome, 'runtime', {{ value: {{}}, configurable: true }});
  }} catch (_) {{}}
}})();
"""


def _billing_top_frame_fingerprint_init_script(browser_fingerprint: dict | None = None, user_agent: str = "") -> str:
    engine = os.getenv("BILLING_BROWSER_ENGINE", "playwright").strip().lower()
    if engine in ("cloak", "cloakbrowser") and _env_bool("BILLING_CLOAK_NATIVE_FINGERPRINT", True) and not _env_bool("BILLING_CLOAK_TOP_FRAME_PATCH", False):
        return _billing_cloak_noop_init_script()
    if engine in ("cloak", "cloakbrowser") and _env_bool("BILLING_CLOAK_TOP_FRAME_PATCH", False):
        # Explicit A/B mode: keep Cloak binary/proxy, but align only the dashboard top frame
        # with the historical successful Chrome 148 dashboard fingerprint. Stripe iframes remain native.
        browser_fingerprint = _apply_billing_user_agent_override(browser_fingerprint or {}) or browser_fingerprint
    script = _billing_fingerprint_init_script(browser_fingerprint, user_agent)
    return script.replace(
        "(() => {",
        "(() => {\n  try { if (window.top !== window) return; } catch (_) { return; }",
        1,
    )


def _stripe_browser_fingerprint_enabled() -> bool:
    return _env_bool("STRIPE_BROWSER_FINGERPRINT", False)


def _stripe_browser_fingerprint_mode() -> str:
    mode = os.getenv("STRIPE_BROWSER_FINGERPRINT_MODE", "").strip().lower()
    if mode:
        return mode
    return "full" if _stripe_browser_fingerprint_enabled() else "safe"


def _validate_card_config(card: dict):
    number = str(card.get("number") or "")
    exp_month = str(card.get("exp_month") or "")
    exp_year = str(card.get("exp_year") or "")
    cvc = str(card.get("cvc") or "")

    problems = []
    if not re.fullmatch(r"\d{12,19}", number):
        problems.append("card number must be 12-19 digits")
    if not exp_month.isdigit() or not (1 <= int(exp_month) <= 12):
        problems.append("expiry month must be 01-12")
    if not re.fullmatch(r"\d{4}", exp_year):
        problems.append("expiry year must be 4 digits")
    if exp_month.isdigit() and re.fullmatch(r"\d{4}", exp_year):
        now = datetime.now(timezone.utc)
        if (int(exp_year), int(exp_month)) < (now.year, now.month):
            problems.append("card expiry is in the past")
    if not re.fullmatch(r"\d{3,4}", cvc):
        problems.append("cvc must be 3-4 digits")

    if problems:
        raise RuntimeError("Billing card config invalid: " + "; ".join(problems))


def _signup(
    proxies,
    email: str,
    password: str,
    csrf_token: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
):
    r = cffi_requests.post(
        "https://api.vapi.ai/auth/signup",
        headers={
            "x-csrf-token": csrf_token,
            **_dashboard_headers(
                user_agent=user_agent,
                referer="https://dashboard.vapi.ai/register?redirect=%2Fsignup",
                browser_fingerprint=browser_fingerprint,
                client_ctx=client_ctx,
                include_device_fingerprint=bool((client_ctx or {}).get("device_fingerprint_token")),
                include_session_headers=True,
            ),
        },
        json={
            "email": email,
            "password": password,
            "emailRedirectTo": "https://dashboard.vapi.ai/",
        },
        proxies=proxies,
        impersonate="chrome",
        timeout=15,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"signup {r.status_code}: {r.text[:200]}")


def _verify_and_get_keys(
    proxies,
    verify_url: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> dict:
    """完整的验证+JWT交换+取key链路"""

    # 步骤1: 验证链接 → 从 Location fragment 提取 Supabase JWT
    r = cffi_requests.get(
        verify_url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": user_agent or "Mozilla/5.0",
        },
        proxies=proxies,
        impersonate="chrome",
        timeout=30,
        allow_redirects=False,
    )
    location = r.headers.get("location", "")
    sb_token = _extract_fragment_param(location, "access_token")
    if not sb_token:
        raise RuntimeError(f"验证重定向无 access_token: {location[:150]}")
    sb_refresh_token = _extract_fragment_param(location, "refresh_token") or ""
    sb_expires_at = _extract_fragment_param(location, "expires_at") or ""

    # 步骤2: GET /org/dashboard (Supabase JWT) → 拿 orgId。
    # 新账号邮件确认后 org 有时会异步落库，短窗口内会返回 200 + []。
    # 这里按 dashboard 实际行为做等待重试，并用 /user/auth JWT / GET /org 作为旁路补提取。
    org_id = ""
    user_token = ""
    dashboard_text = ""
    attempts = max(1, ORG_DASHBOARD_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        r2 = cffi_requests.get(
            "https://api.vapi.ai/org/dashboard",
            headers=_dashboard_headers(
                user_agent=user_agent,
                authorization=f"Bearer {sb_token}",
                referer="https://dashboard.vapi.ai/",
                browser_fingerprint=browser_fingerprint,
                client_ctx=client_ctx,
            ),
            proxies=proxies,
            impersonate="chrome",
            timeout=15,
        )
        dashboard_text = r2.text
        if r2.status_code != 200:
            if attempt < attempts and r2.status_code in (409, 425, 429, 500, 502, 503, 504):
                log.warning(f"/org/dashboard 暂不可用，等待重试: status={r2.status_code} attempt={attempt}/{attempts}")
                time.sleep(min(8.0, ORG_DASHBOARD_RETRY_SECONDS * attempt))
                continue
            raise RuntimeError(f"/org/dashboard {r2.status_code}: {r2.text[:200]}")

        dashboard = r2.json()
        org_id = _extract_org_id(dashboard) or ""
        if org_id:
            break

        if not user_token:
            try:
                user_token = _fetch_user_token(
                    proxies,
                    sb_token,
                    user_agent,
                    browser_fingerprint,
                    client_ctx,
                )
                org_id = _extract_org_id_from_jwt(user_token) or ""
                if org_id:
                    log.warning(f"/org/dashboard 为空，改用 user JWT 中的 orgId: attempt={attempt}/{attempts}")
                    break
            except Exception as e:
                log.warning(f"/org/dashboard 为空，预取 /user/auth 失败: {e}")

        if user_token:
            org_id = _fetch_org_id_from_org_list(
                proxies,
                user_token,
                user_agent,
                browser_fingerprint,
                client_ctx,
            ) or ""
            if org_id:
                log.warning(f"/org/dashboard 为空，改用 /org 列表中的 orgId: attempt={attempt}/{attempts}")
                break

        if attempt < attempts:
            log.warning(f"/org/dashboard 暂无 org，等待异步创建: attempt={attempt}/{attempts} body={dashboard_text[:120]}")
            time.sleep(min(8.0, ORG_DASHBOARD_RETRY_SECONDS * attempt))

    if not org_id:
        raise RuntimeError(f"无法从 /org/dashboard 提取 orgId: {dashboard_text[:300]}")

    # 步骤3: GET /user/auth (Supabase JWT) → user JWT
    if not user_token:
        user_token = _fetch_user_token(
            proxies,
            sb_token,
            user_agent,
            browser_fingerprint,
            client_ctx,
        )

    # 步骤4: GET /org/{orgId}/auth (user JWT) → org JWT
    r4 = cffi_requests.get(
        f"https://api.vapi.ai/org/{org_id}/auth",
        headers=_dashboard_headers(
            user_agent=user_agent,
            authorization=f"Bearer {user_token}",
            referer="https://dashboard.vapi.ai/",
            browser_fingerprint=browser_fingerprint,
            client_ctx=client_ctx,
        ),
        proxies=proxies,
        impersonate="chrome",
        timeout=15,
    )
    if r4.status_code != 200:
        raise RuntimeError(f"/org/{org_id}/auth {r4.status_code}: {r4.text[:200]}")

    org_data = r4.json()
    org_token = org_data.get("accessToken")
    if not org_token:
        raise RuntimeError(f"/org/auth 无 accessToken: {r4.text[:200]}")

    # 步骤5: GET /token (org JWT) → private key
    r5 = cffi_requests.get(
        config.VAPI_TOKEN_API,
        headers=_dashboard_headers(
            user_agent=user_agent,
            authorization=f"Bearer {org_token}",
            referer="https://dashboard.vapi.ai/settings/billing",
            browser_fingerprint=browser_fingerprint,
            client_ctx=client_ctx,
        ),
        proxies=proxies,
        impersonate="chrome",
        timeout=15,
    )
    if r5.status_code != 200:
        raise RuntimeError(f"/token {r5.status_code}: {r5.text[:200]}")

    tokens = r5.json()
    if isinstance(tokens, list):
        private = next((t for t in tokens if t.get("tag") == "private"), None)
        public = next((t for t in tokens if t.get("tag") == "public"), None)
    else:
        raise RuntimeError(f"/token 格式异常: {r5.text[:200]}")

    if not private:
        raise RuntimeError(f"/token 无 private key: {r5.text[:200]}")

    return {
        "private_key": private["value"],
        "public_key": public["value"] if public else "",
        "org_id": private.get("orgId", org_id),
        "org_token": org_token,
        "user_token": user_token,
        "supabase_token": sb_token,
        "supabase_refresh_token": sb_refresh_token,
        "supabase_expires_at": sb_expires_at,
    }


def _extract_fragment_param(location: str, param: str) -> str | None:
    if "#" not in location:
        return None
    fragment = location.split("#", 1)[1]
    for part in fragment.split("&"):
        if part.startswith(f"{param}="):
            return unquote(part.split("=", 1)[1])
    return None


def _extract_org_id_from_jwt(token: str) -> str | None:
    payload = _decode_jwt_payload(token)
    for key in (
        "orgId",
        "organizationId",
        "org_id",
        "organization_id",
        "currentOrgId",
        "selectedOrgId",
    ):
        value = payload.get(key)
        if value:
            return str(value)
    for key in ("org", "organization", "currentOrg", "selectedOrg"):
        value = payload.get(key)
        if isinstance(value, dict):
            oid = value.get("id") or value.get("orgId") or value.get("organizationId")
            if oid:
                return str(oid)
    return None


def _fetch_user_token(
    proxies,
    sb_token: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> str:
    r = cffi_requests.get(
        "https://api.vapi.ai/user/auth",
        headers=_dashboard_headers(
            user_agent=user_agent,
            authorization=f"Bearer {sb_token}",
            referer="https://dashboard.vapi.ai/",
            browser_fingerprint=browser_fingerprint,
            client_ctx=client_ctx,
        ),
        proxies=proxies,
        impersonate="chrome",
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"/user/auth {r.status_code}: {r.text[:200]}")

    user_data = r.json()
    user_token = user_data.get("accessToken")
    if not user_token:
        raise RuntimeError(f"/user/auth 无 accessToken: {r.text[:200]}")
    return user_token


def _fetch_org_id_from_org_list(
    proxies,
    user_token: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> str | None:
    try:
        r = cffi_requests.get(
            "https://api.vapi.ai/org",
            headers=_dashboard_headers(
                user_agent=user_agent,
                authorization=f"Bearer {user_token}",
                referer="https://dashboard.vapi.ai/",
                browser_fingerprint=browser_fingerprint,
                client_ctx=client_ctx,
            ),
            proxies=proxies,
            impersonate="chrome",
            timeout=15,
        )
        if r.status_code != 200:
            return None
        return _extract_org_id(r.json())
    except Exception:
        return None


def _extract_org_id(dashboard_data) -> str | None:
    """从 /org/dashboard 响应提取 orgId"""
    # 可能是列表、对象，或包在 data/items/organizations 里的对象。
    if isinstance(dashboard_data, list):
        for item in dashboard_data:
            if isinstance(item, dict):
                oid = (
                    item.get("id")
                    or item.get("orgId")
                    or item.get("organizationId")
                    or item.get("org_id")
                    or item.get("organization_id")
                )
                if oid:
                    return str(oid)
                # 嵌套结构
                sub = item.get("subscription", {})
                if sub and item.get("id"):
                    return str(item["id"])
    elif isinstance(dashboard_data, dict):
        for key in ("id", "orgId", "organizationId", "org_id", "organization_id"):
            if dashboard_data.get(key):
                return str(dashboard_data[key])
        for key in ("org", "organization", "currentOrg", "selectedOrg"):
            oid = _extract_org_id(dashboard_data.get(key))
            if oid:
                return oid
        for key in ("data", "items", "organizations", "orgs", "results"):
            oid = _extract_org_id(dashboard_data.get(key))
            if oid:
                return oid
    return None


def _add_card(
    proxies,
    token: str,
    pm_id: str,
    user_agent: str,
    token_label: str,
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
):
    # 尽量贴近真实 dashboard fetch：浏览器成功样本里 add-card 是 Accept */*、Referer 根路径。
    headers = _dashboard_headers(
        user_agent=user_agent,
        authorization=f"Bearer {token}",
        referer=os.getenv("BILLING_ADD_CARD_REFERER", "https://dashboard.vapi.ai/"),
        browser_fingerprint=browser_fingerprint,
        client_ctx=client_ctx,
        include_device_fingerprint=True,
    )
    headers["Accept"] = os.getenv("BILLING_ADD_CARD_ACCEPT", "*/*")
    dashboard_version_override = _billing_add_card_dashboard_version_override()
    if dashboard_version_override:
        headers["X-DASHBOARD-VERSION"] = dashboard_version_override
    body = {"paymentMethodId": pm_id}
    started = time.time()
    claims = _decode_jwt_payload(token)
    debug_base = {
        "type": "add-card",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "tokenLabel": token_label,
        "pm": _safe_value("paymentMethodId", pm_id),
        "request": {
            "url": "https://api.vapi.ai/stripe/add-card",
            "method": "POST",
            "headers": _safe_headers(headers),
            "body": _safe_payload(body),
            "proxies": proxies or {},
        },
        "jwtClaims": {
            key: claims.get(key)
            for key in ("iss", "sub", "aud", "exp", "iat", "role", "email", "userEmail", "orgId", "organizationId")
            if claims.get(key) is not None
        },
        "clientContext": _safe_client_context(client_ctx),
        "fingerprint": {
            "userAgent": _fingerprint_user_agent(browser_fingerprint, user_agent),
            "secChUa": (browser_fingerprint or {}).get("sec_ch_ua") if isinstance(browser_fingerprint, dict) else "",
        },
        "dashboardVersion": DASHBOARD_VERSION,
        "addCardDashboardVersionSent": dashboard_version_override or DASHBOARD_VERSION,
    }
    try:
        if _billing_mock_add_card_enabled():
            status, payload = _mock_add_card_response(pm_id)
            r = _MockHTTPResponse(status, payload)
            log.warning(f"BILLING_TEST_MODE/mock add-card active: status={status} pm={_mask_id(pm_id)}")
        else:
            r = cffi_requests.post(
                "https://api.vapi.ai/stripe/add-card",
                headers=headers,
                json=body,
                proxies=proxies,
                impersonate="chrome",
                timeout=20,
            )
    except Exception as e:
        debug_payload = dict(debug_base)
        debug_payload.update({
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "elapsedMs": int((time.time() - started) * 1000),
            "exception": str(e),
        })
        path = _write_billing_debug("add-card", debug_payload)
        if path:
            log.info(f"add-card 诊断日志: {path}")
        raise

    debug_payload = dict(debug_base)
    debug_payload.update({
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "elapsedMs": int((time.time() - started) * 1000),
        "response": {
            "status": r.status_code,
            "headers": _safe_headers(dict(getattr(r, "headers", {}) or {})),
            "body": _safe_post_data(getattr(r, "text", "") or ""),
        },
        "classification": _classify_add_card_failure(r.status_code, getattr(r, "text", "") or ""),
    })
    path = _write_billing_debug("add-card", debug_payload)
    if path:
        log.info(f"add-card 诊断日志: {path}")

    if r.status_code not in (200, 201):
        summary = _summarize_json_error(r)
        lowered = summary.lower()
        if "card was declined" in lowered or "couldn't attach payment method" in lowered or "couldn’t attach payment method" in lowered:
            raise RuntimeError(f"billing attach declined by Stripe/Vapi using {token_label} token: add-card {r.status_code}: {summary}")
        raise RuntimeError(f"add-card using {token_label} token {r.status_code}: {summary}")
    return r


def _billing_name(email_addr: str) -> str:
    return (email_addr.split("@", 1)[0] or "Vapi User").replace(".", " ").replace("_", " ")[:80]


def _attach_payment_method(
    proxies,
    org_token: str,
    pm_id: str,
    user_agent: str,
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> str:
    log.info("调用 add-card: 使用 org token")
    _add_card(proxies, org_token, pm_id, user_agent, "org", browser_fingerprint, client_ctx)
    return pm_id


async def _attach_payment_method_browser_fetch(
    proxy_url: str,
    keys: dict,
    pm_id: str,
    email_addr: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> str:
    """在真实 dashboard 页面上下文里 fetch add-card，补齐浏览器 CORS/cookie/Referer/CH 链路。"""
    org_token = (keys or {}).get("org_token") or ""
    if not org_token:
        raise RuntimeError("browser-fetch add-card missing org token")

    debug_dir = Path("/data/browser-bind-debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    network_path = debug_dir / f"attach-browser-fetch-{timestamp}.json"
    network_log = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "email": email_addr,
        "pm": _safe_value("paymentMethodId", pm_id),
        "events": [],
    }

    async def write_network_log():
        try:
            network_log["finishedAt"] = datetime.now(timezone.utc).isoformat()
            network_log["eventCount"] = len(network_log["events"])
            network_path.write_text(json.dumps(network_log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as e:
            log.debug(f"写入 browser-fetch attach network 失败: {e}")

    async def record_request(request):
        try:
            url = request.url
            if not _network_interesting(url):
                return
            network_log["events"].append({
                "type": "request",
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": request.method,
                "url": _safe_url(url),
                "resourceType": request.resource_type,
                "headers": _safe_headers(request.headers),
                "postData": _safe_post_data(request.post_data or ""),
            })
        except Exception as e:
            network_log["events"].append({"type": "request-log-error", "ts": datetime.now(timezone.utc).isoformat(), "error": str(e)})

    async def record_response(response):
        try:
            url = response.url
            if not _network_interesting(url):
                return
            event = {
                "type": "response",
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": response.status,
                "url": _safe_url(url),
                "headers": _safe_headers(response.headers),
            }
            if "api.stripe.com" in url or "/stripe/add-card" in url or "/subscription/" in url:
                try:
                    event["body"] = _safe_post_data(await response.text())
                except Exception as body_error:
                    event["bodyError"] = str(body_error)
            network_log["events"].append(event)
        except Exception as e:
            network_log["events"].append({"type": "response-log-error", "ts": datetime.now(timezone.utc).isoformat(), "error": str(e)})

    engine, browser_driver = _billing_browser_engine()
    playwright = await browser_driver().start()
    browser = None
    context = None
    started = time.time()
    try:
        browser_fingerprint = _with_billing_profile(browser_fingerprint, client_ctx)
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url, client_ctx)
        log.info(f"add-card 使用 browser-fetch attach: engine={engine}, headless={_billing_browser_headless()}")
        log.info(f"add-card browser-fetch 代理: {'direct' if not browser_proxy_url else browser_proxy_url}")
        network_log["browserEngine"] = engine
        network_log["browserHeadless"] = _billing_browser_headless()
        network_log["billingProxy"] = "direct" if not browser_proxy_url else browser_proxy_url
        network_log["billingRetryContext"] = {
            "retryIndex": int((client_ctx or {}).get("billing_retry_index", 0) or 0),
            "proxyIndex": int((client_ctx or {}).get("billing_proxy_index", 0) or 0),
            "proxySequenceConfigured": bool(_billing_proxy_sequence()),
            "forceRandomProfile": bool((client_ctx or {}).get("billing_force_random_profile")),
            "dashboardVersion": DASHBOARD_VERSION,
            "addCardDashboardVersionOverride": _billing_add_card_dashboard_version_override(),
        }
        network_log["billingFingerprintProfile"] = _billing_profile_summary(browser_fingerprint)
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine, browser_proxy_url))
        context_kwargs = {
            "proxy": _billing_context_proxy_option(engine, browser_proxy_url),
            "user_agent": None if engine == "cloak" and _env_bool("BILLING_CLOAK_NATIVE_UA", True) else (effective_user_agent or None),
            "viewport": _billing_profile_from_fingerprint(browser_fingerprint)["viewport"],
            "screen": _billing_profile_from_fingerprint(browser_fingerprint)["screen"],
            "locale": _billing_locale(),
            "timezone_id": _billing_timezone_id(),
            "extra_http_headers": _fingerprint_extra_headers(browser_fingerprint),
        }
        storage_state = (client_ctx or {}).get("browser_storage_state")
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**context_kwargs)
        await context.route("**/v1/sourceToken**", _route_dodgeball_source_token)
        await context.route("**/v1/payment_methods**", _make_route_stripe_payment_methods_request(email_addr))
        await context.route("**/stripe/add-card**", _make_route_add_card_request(client_ctx))
        # 完整 billing UI 会加载 Stripe 安全 iframe；只修补 dashboard 顶层，避免 Stripe Elements ready/输入框被干扰。
        await context.add_init_script(_billing_top_frame_fingerprint_init_script(browser_fingerprint, user_agent))
        page = await context.new_page()
        page.on("request", lambda request: asyncio.create_task(record_request(request)))
        page.on("response", lambda response: asyncio.create_task(record_response(response)))

        await _goto_with_abort_tolerance(page, "https://dashboard.vapi.ai/", wait_until="domcontentloaded", timeout=60000, label="dashboard root")
        await _store_dashboard_session(page, keys, email_addr)
        if _env_bool("BILLING_SAME_BROWSER_USE_BILLING_PAGE", True):
            await _goto_with_abort_tolerance(page, BILLING_URL, wait_until="domcontentloaded", timeout=120000, label="same-browser billing page")
            network_log["pageStateBeforeBillingReady"] = await _billing_page_diagnostics(page)
            try:
                await _ensure_billing_ready(page, email_addr, keys)
                await _complete_welcome_onboarding_if_present(page, email_addr, timeout=2500)
            except Exception as billing_ready_error:
                network_log["billingReadyWarning"] = str(billing_ready_error)[:500]
                log.warning(f"same-browser billing page ready warning: {billing_ready_error}")
            network_log["pageStateAfterBillingReady"] = await _billing_page_diagnostics(page)
        else:
            await _goto_with_abort_tolerance(page, "https://dashboard.vapi.ai/", wait_until="domcontentloaded", timeout=60000, label="dashboard root")
        network_log["fingerprintDashboard"] = await _browser_fingerprint_snapshot(page)

        token = await _read_dodgeball_token_from_page(page)
        if not token:
            try:
                has_dodgeball = await page.evaluate("() => typeof window.Dodgeball === 'function'")
            except Exception:
                has_dodgeball = False
            if has_dodgeball:
                token = await page.evaluate(
                    """async ({ publicKey, apiUrl }) => {
                        const db = new window.Dodgeball(publicKey, { apiVersion: 'v1', apiUrl });
                        const token = await db.getSourceToken();
                        await new Promise((resolve) => setTimeout(resolve, 500));
                        return token || '';
                    }""",
                    {"publicKey": DODGEBALL_PUBLIC_KEY, "apiUrl": DODGEBALL_API_URL},
                )
        await page.wait_for_timeout(_env_int("BILLING_ATTACH_BROWSER_SETTLE_MS", 800))
        storage_state = await context.storage_state()
        token = str(token or await _read_dodgeball_token_from_page(page) or _extract_dodgeball_token_from_storage_state(storage_state) or "")
        if client_ctx is not None:
            client_ctx["browser_storage_state"] = storage_state
            if token:
                client_ctx["device_fingerprint_token"] = token
                client_ctx["dodgeball_source_token"] = token

        await _human_pause_before_add_card(page, email_addr, "browser-fetch add-card")
        await wait_billing_attach_slot(email_addr, "browser-fetch add-card")
        add_card_dashboard_version = _billing_add_card_dashboard_version_override() or DASHBOARD_VERSION
        network_log["addCardDashboardVersionSent"] = add_card_dashboard_version
        if _billing_mock_add_card_enabled():
            status, payload = _mock_add_card_response(pm_id)
            result = {"ok": status in (200, 201), "status": status, "statusText": "MOCK", "body": json.dumps(payload, ensure_ascii=False), "mocked": True}
            network_log["mockAddCard"] = {"status": status, "classification": _classify_add_card_failure(status, payload)}
            log.warning(f"BILLING_TEST_MODE/mock browser-fetch add-card active: status={status} pm={_mask_id(pm_id)}")
        else:
            result = await page.evaluate(
            """async ({ orgToken, pmId, fingerprintToken, dashboardVersion, requestId }) => {
                const headers = {
                    'accept': '*/*',
                    'authorization': `Bearer ${orgToken}`,
                    'content-type': 'application/json',
                    'x-client-platform': 'web',
                    'x-client-source': 'dashboard',
                    'x-dashboard-version': dashboardVersion,
                    'x-request-id': requestId,
                };
                if (fingerprintToken) headers['x-device-fingerprint-token'] = fingerprintToken;
                const response = await fetch('https://api.vapi.ai/stripe/add-card', {
                    method: 'POST',
                    mode: 'cors',
                    credentials: 'include',
                    headers,
                    body: JSON.stringify({ paymentMethodId: pmId }),
                });
                const text = await response.text();
                return {
                    ok: response.ok,
                    status: response.status,
                    statusText: response.statusText,
                    body: text,
                    fingerprintTokenUsed: fingerprintToken || '',
                };
            }""",
            {
                "orgToken": org_token,
                "pmId": pm_id,
                "fingerprintToken": token or (client_ctx or {}).get("device_fingerprint_token", ""),
                "dashboardVersion": add_card_dashboard_version,
                "requestId": _request_id(),
            },
            )
        await write_network_log()
        body_text = result.get("body", "") if isinstance(result, dict) else str(result)
        debug_path = _write_billing_debug("add-card-browser", {
            "type": "add-card-browser-fetch",
            "startedAt": network_log.get("startedAt"),
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "elapsedMs": int((time.time() - started) * 1000),
            "email": email_addr,
            "pm": _safe_value("paymentMethodId", pm_id),
            "response": {
                "status": result.get("status") if isinstance(result, dict) else None,
                "body": _safe_post_data(body_text),
            },
            "classification": _classify_add_card_failure(result.get("status") if isinstance(result, dict) else None, body_text),
            "clientContext": _safe_client_context(client_ctx),
            "network": str(network_path),
        })
        if debug_path:
            log.info(f"browser-fetch add-card 诊断日志: {debug_path}")
        if not isinstance(result, dict) or not result.get("ok"):
            status = result.get("status") if isinstance(result, dict) else "unknown"
            raise RuntimeError(f"browser-fetch add-card {status}: {body_text[:300]}; network={network_path}")
        log.info(f"browser-fetch add-card 成功: {_mask_id(pm_id)} network={network_path}")
        return pm_id
    except Exception as e:
        await write_network_log()
        raise
    finally:
        if context:
            await _close_context_safely(context)
        if browser:
            await _close_browser_safely(browser)
        await _stop_playwright_safely(playwright)



async def _bind_card_dashboard_browser_fetch(
    proxy_url: str,
    keys: dict,
    card: dict,
    email_addr: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> str:
    """在同一个 dashboard 页面上下文里创建 Stripe PM 并 fetch add-card。"""
    org_token = (keys or {}).get("org_token") or ""
    if not org_token:
        raise RuntimeError("same-browser add-card missing org token")

    debug_dir = Path("/data/browser-bind-debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    network_path = debug_dir / f"same-browser-bind-{timestamp}.json"
    network_log = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "email": email_addr,
        "cardTail": card["number"][-4:],
        "events": [],
    }

    async def write_network_log():
        try:
            network_log["finishedAt"] = datetime.now(timezone.utc).isoformat()
            network_log["eventCount"] = len(network_log["events"])
            network_path.write_text(json.dumps(network_log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as e:
            log.debug(f"写入 same-browser bind network 失败: {e}")

    async def record_request(request):
        try:
            url = request.url
            if not _network_interesting(url):
                return
            network_log["events"].append({
                "type": "request",
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": request.method,
                "url": _safe_url(url),
                "resourceType": request.resource_type,
                "headers": _safe_headers(request.headers),
                "postData": _safe_post_data(request.post_data or ""),
            })
        except Exception as e:
            network_log["events"].append({"type": "request-log-error", "ts": datetime.now(timezone.utc).isoformat(), "error": str(e)})

    async def record_response(response):
        try:
            url = response.url
            if not _network_interesting(url):
                return
            event = {
                "type": "response",
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": response.status,
                "url": _safe_url(url),
                "headers": _safe_headers(response.headers),
            }
            if "api.stripe.com" in url or "/stripe/add-card" in url or "/subscription/" in url:
                try:
                    event["body"] = _safe_post_data(await response.text())
                except Exception as body_error:
                    event["bodyError"] = str(body_error)
            network_log["events"].append(event)
        except Exception as e:
            network_log["events"].append({"type": "response-log-error", "ts": datetime.now(timezone.utc).isoformat(), "error": str(e)})

    engine, browser_driver = _billing_browser_engine()
    playwright = await browser_driver().start()
    browser = None
    context = None
    started = time.time()
    try:
        browser_fingerprint = _with_billing_profile(browser_fingerprint, client_ctx)
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url, client_ctx)
        log.info(f"same-browser 绑卡: engine={engine}, headless={_billing_browser_headless()}")
        log.info(f"same-browser 绑卡代理: {'direct' if not browser_proxy_url else browser_proxy_url}")
        network_log["browserEngine"] = engine
        network_log["browserHeadless"] = _billing_browser_headless()
        network_log["billingProxy"] = "direct" if not browser_proxy_url else browser_proxy_url
        network_log["billingRetryContext"] = {
            "retryIndex": int((client_ctx or {}).get("billing_retry_index", 0) or 0),
            "proxyIndex": int((client_ctx or {}).get("billing_proxy_index", 0) or 0),
            "proxySequenceConfigured": bool(_billing_proxy_sequence()),
            "forceRandomProfile": bool((client_ctx or {}).get("billing_force_random_profile")),
            "dashboardVersion": DASHBOARD_VERSION,
            "addCardDashboardVersionOverride": _billing_add_card_dashboard_version_override(),
        }
        network_log["billingFingerprintProfile"] = _billing_profile_summary(browser_fingerprint)
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine, browser_proxy_url))
        context_kwargs = {
            "proxy": _billing_context_proxy_option(engine, browser_proxy_url),
            "user_agent": None if engine == "cloak" and _env_bool("BILLING_CLOAK_NATIVE_UA", True) else (effective_user_agent or None),
            "viewport": _billing_profile_from_fingerprint(browser_fingerprint)["viewport"],
            "screen": _billing_profile_from_fingerprint(browser_fingerprint)["screen"],
            "locale": _billing_locale(),
            "timezone_id": _billing_timezone_id(),
            "extra_http_headers": _fingerprint_extra_headers(browser_fingerprint),
        }
        storage_state = (client_ctx or {}).get("browser_storage_state")
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**context_kwargs)
        await context.route("**/v1/sourceToken**", _route_dodgeball_source_token)
        await context.route("**/v1/payment_methods**", _make_route_stripe_payment_methods_request(email_addr))
        await context.route("**/stripe/add-card**", _make_route_add_card_request(client_ctx))
        # 只修补 dashboard 顶层窗口指纹，跳过 Stripe iframe，避免 createPaymentMethod 卡住。
        await context.add_init_script(_billing_top_frame_fingerprint_init_script(browser_fingerprint, user_agent))
        page = await context.new_page()
        page.on("request", lambda request: asyncio.create_task(record_request(request)))
        page.on("response", lambda response: asyncio.create_task(record_response(response)))

        await _goto_with_abort_tolerance(page, "https://dashboard.vapi.ai/", wait_until="domcontentloaded", timeout=60000, label="dashboard root")
        await _store_dashboard_session(page, keys, email_addr)
        await _goto_with_abort_tolerance(page, "https://dashboard.vapi.ai/", wait_until="domcontentloaded", timeout=60000, label="dashboard root")
        network_log["fingerprintDashboard"] = await _browser_fingerprint_snapshot(page)

        token = await _read_dodgeball_token_from_page(page)
        if not token and await page.evaluate("() => typeof window.Dodgeball === 'function'"):
            token = await page.evaluate(
                """async ({ publicKey, apiUrl }) => {
                    const db = new window.Dodgeball(publicKey, { apiVersion: 'v1', apiUrl });
                    const token = await db.getSourceToken();
                    await new Promise((resolve) => setTimeout(resolve, 500));
                    return token || '';
                }""",
                {"publicKey": DODGEBALL_PUBLIC_KEY, "apiUrl": DODGEBALL_API_URL},
            )
        await page.wait_for_timeout(_env_int("BILLING_SAME_BROWSER_SETTLE_MS", 800))
        storage_state = await context.storage_state()
        storage_token = _extract_dodgeball_token_from_storage_state(storage_state)
        header_token = str((client_ctx or {}).get("device_fingerprint_token") or token or storage_token or "")
        if client_ctx is not None:
            client_ctx["browser_storage_state"] = storage_state
            if not client_ctx.get("device_fingerprint_token") and header_token:
                client_ctx["device_fingerprint_token"] = header_token
            if token or storage_token:
                client_ctx["dodgeball_source_token"] = token or storage_token

        await page.evaluate(
            """() => {
                let host = document.getElementById('vapi-stripe-pm-host');
                if (!host) {
                    host = document.createElement('div');
                    host.id = 'vapi-stripe-pm-host';
                    host.style.cssText = 'position:fixed;left:16px;top:16px;z-index:2147483647;background:#fff;padding:12px;width:460px;min-height:190px;opacity:0.98;';
                    document.body.appendChild(host);
                }
                host.innerHTML = '<div id="card-number" style="min-height:44px;margin:8px;border:1px solid #ccc;padding:10px"></div><div id="card-expiry" style="height:44px;margin:8px;border:1px solid #ccc;padding:10px"></div><div id="card-cvc" style="height:44px;margin:8px;border:1px solid #ccc;padding:10px"></div>';
            }"""
        )
        await _mount_stripe_elements(page, STRIPE_PK)
        await _human_mouse_wiggle(page, "before stripe pm fill")
        await _fill_stripe_card_details(page, card)
        await _human_mouse_wiggle(page, "after stripe pm fill")
        network_log["stripeInputStateBeforeCreatePm"] = await _stripe_debug_input_lengths(page)
        log.info(f"same-browser Stripe Elements inputs filled mode={_stripe_element_mode()}")

        create_timeout_ms = _env_int("STRIPE_CREATE_PM_TIMEOUT_MS", 60000)
        billing_details = _stripe_pm_billing_details(email_addr)
        include_billing_details = bool(billing_details)
        if _billing_mock_stripe_pm_enabled():
            payload = _mock_stripe_payment_method_payload(email_addr, card.get("number", ""))
            pm_result = {"ok": True, "id": payload["id"], "billingDetails": payload.get("billing_details"), "card": payload.get("card"), "mocked": True}
            log.warning(f"BILLING_TEST_MODE/mock same-browser Stripe PaymentMethod active: pm={_mask_id(payload['id'])}")
        else:
            pm_result = await page.evaluate(
            """async ({ billingDetails, timeoutMs }) => {
                const payload = { type: 'card', card: window.__vapiStripeCardNumber };
                if (billingDetails) payload.billing_details = billingDetails;
                const create = window.__vapiStripe.createPaymentMethod(payload);
                const timeout = new Promise((resolve) => setTimeout(() => resolve({
                    error: { message: `Stripe createPaymentMethod timeout after ${timeoutMs}ms`, code: 'create_payment_method_timeout', type: 'timeout' }
                }), timeoutMs));
                const result = await Promise.race([create, timeout]);
                if (result.error) {
                    return { ok: false, error: { message: result.error.message || '', code: result.error.code || '', decline_code: result.error.decline_code || '', type: result.error.type || '' } };
                }
                const pm = result.paymentMethod || {};
                return { ok: true, id: pm.id, billingDetails: pm.billing_details || null, card: pm.card ? { brand: pm.card.brand, country: pm.card.country, funding: pm.card.funding, last4: pm.card.last4 } : null };
            }""",
            {"billingDetails": billing_details, "timeoutMs": create_timeout_ms},
            )
        if not isinstance(pm_result, dict) or not pm_result.get("ok"):
            raise RuntimeError(f"same-browser Stripe payment_methods failed: {pm_result}")
        pm_id = pm_result.get("id") or ""
        if not pm_id:
            raise RuntimeError(f"same-browser Stripe payment_methods returned no pm id: {pm_result}")
        if _env_bool("STRIPE_PM_DEBUG", True):
            _write_billing_debug("stripe-pm-same-browser", {
                "type": "stripe-pm-same-browser",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "pm": _safe_value("paymentMethodId", pm_id),
                "billingDetailsSent": bool(billing_details),
                "result": _safe_payload(pm_result),
                "cardTail": card["number"][-4:],
                "email": email_addr,
                "network": str(network_path),
            })
        log.info(f"same-browser Stripe PaymentMethod 创建成功: {_mask_id(pm_id)}")

        await _human_pause_before_add_card(page, email_addr, "same-browser add-card")
        await wait_billing_attach_slot(email_addr, "same-browser add-card")
        add_card_dashboard_version = _billing_add_card_dashboard_version_override() or DASHBOARD_VERSION
        network_log["addCardDashboardVersionSent"] = add_card_dashboard_version
        if _billing_mock_add_card_enabled():
            status, payload = _mock_add_card_response(pm_id)
            result = {"ok": status in (200, 201), "status": status, "statusText": "MOCK", "body": json.dumps(payload, ensure_ascii=False), "mocked": True}
            network_log["mockAddCard"] = {"status": status, "classification": _classify_add_card_failure(status, payload)}
            log.warning(f"BILLING_TEST_MODE/mock same-browser add-card active: status={status} pm={_mask_id(pm_id)}")
        else:
            result = await page.evaluate(
            """async ({ orgToken, pmId, fingerprintToken, dashboardVersion, requestId }) => {
                const headers = {
                    'accept': '*/*',
                    'authorization': `Bearer ${orgToken}`,
                    'content-type': 'application/json',
                    'x-client-platform': 'web',
                    'x-client-source': 'dashboard',
                    'x-dashboard-version': dashboardVersion,
                    'x-request-id': requestId,
                };
                if (fingerprintToken) headers['x-device-fingerprint-token'] = fingerprintToken;
                const response = await fetch('https://api.vapi.ai/stripe/add-card', {
                    method: 'POST', mode: 'cors', credentials: 'include', headers,
                    body: JSON.stringify({ paymentMethodId: pmId }),
                });
                const text = await response.text();
                return { ok: response.ok, status: response.status, statusText: response.statusText, body: text, fingerprintTokenUsed: fingerprintToken || '' };
            }""",
            {
                "orgToken": org_token,
                "pmId": pm_id,
                "fingerprintToken": header_token,
                "dashboardVersion": add_card_dashboard_version,
                "requestId": _request_id(),
            },
            )
        await write_network_log()
        body_text = result.get("body", "") if isinstance(result, dict) else str(result)
        debug_path = _write_billing_debug("add-card-same-browser", {
            "type": "add-card-same-browser",
            "startedAt": network_log.get("startedAt"),
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "elapsedMs": int((time.time() - started) * 1000),
            "email": email_addr,
            "pm": _safe_value("paymentMethodId", pm_id),
            "response": {"status": result.get("status") if isinstance(result, dict) else None, "body": _safe_post_data(body_text)},
            "classification": _classify_add_card_failure(result.get("status") if isinstance(result, dict) else None, body_text),
            "clientContext": _safe_client_context(client_ctx),
            "network": str(network_path),
        })
        if debug_path:
            log.info(f"same-browser add-card 诊断日志: {debug_path}")
        if not isinstance(result, dict) or not result.get("ok"):
            status = result.get("status") if isinstance(result, dict) else "unknown"
            raise RuntimeError(f"same-browser add-card {status}: {body_text[:300]}; network={network_path}")
        log.info(f"same-browser add-card 成功: {_mask_id(pm_id)} network={network_path}")
        return pm_id
    except Exception:
        await write_network_log()
        raise
    finally:
        if context:
            await _close_context_safely(context)
        if browser:
            await _close_browser_safely(browser)
        await _stop_playwright_safely(playwright)

def _bind_card_api(
    proxies,
    org_token: str,
    email_addr: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> str:
    """旧版 raw API 绑卡：直接 POST Stripe payment_methods → Vapi add-card"""
    card = config.billing_card()
    if not all(card.values()):
        raise RuntimeError("Billing card config incomplete: number, expiry, and cvc are required")
    _validate_card_config(card)

    log.info(f"使用后台配置绑卡: ****{card['number'][-4:]} exp={card['exp_month']}/{card['exp_year']}")
    billing_name = _billing_name(email_addr)

    # 步骤1: 调 Stripe API 创建 PaymentMethod
    stripe_guid = str(uuid.uuid4())
    stripe_muid = str(uuid.uuid4())
    stripe_sid = str(uuid.uuid4())
    r = cffi_requests.post(
        "https://api.stripe.com/v1/payment_methods",
        headers={
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": user_agent or "Mozilla/5.0",
        },
        data={
            "type": "card",
            "card[number]": card["number"],
            "card[exp_month]": card["exp_month"],
            "card[exp_year]": card["exp_year"],
            "card[cvc]": card["cvc"],
            "billing_details[email]": email_addr,
            "billing_details[name]": billing_name,
            "guid": stripe_guid,
            "muid": stripe_muid,
            "sid": stripe_sid,
            "payment_user_agent": "stripe.js/af71287371; stripe-js-v3/af71287371; card-element",
            "referrer": "https://dashboard.vapi.ai/settings/billing",
            "time_on_page": str(random.randint(18000, 65000)),
            "key": STRIPE_PK,
        },
        proxies=proxies,
        impersonate="chrome",
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Stripe payment_methods {r.status_code}: {_summarize_json_error(r)}")

    pm_id = r.json().get("id")
    if not pm_id:
        raise RuntimeError(f"Stripe 未返回 pm id: {r.text[:200]}")
    log.info(f"Stripe PaymentMethod 创建成功: {_mask_id(pm_id)}")

    # 步骤2: 调 Vapi add-card
    return _attach_payment_method(proxies, org_token, pm_id, user_agent, browser_fingerprint, client_ctx)


async def _stripe_input_visible(page, input_name: str) -> bool:
    selector = f'input[name="{input_name}"]'
    for frame in page.frames:
        try:
            locator = frame.locator(selector).first
            if await locator.count() and await locator.is_visible(timeout=200):
                return True
        except Exception:
            continue
    return False


async def _stripe_mount_diagnostics(page) -> dict:
    try:
        return await page.evaluate(
            """() => ({
                url: location.href,
                stripeType: typeof window.Stripe,
                readyState: window.__vapiStripeReadyState || null,
                elementClasses: Array.from(document.querySelectorAll('#card-number,#card-expiry,#card-cvc')).map((el) => ({
                    id: el.id,
                    className: el.className,
                    iframeCount: el.querySelectorAll('iframe').length,
                    text: (el.innerText || '').slice(0, 80),
                })),
                stripeIframeCount: document.querySelectorAll('iframe[src*="stripe.com"]').length,
                stripeIframeTitles: Array.from(document.querySelectorAll('iframe[src*="stripe.com"]')).map((f) => f.title || '').slice(0, 10),
            })"""
        )
    except Exception as e:
        return {"error": str(e)[:240]}


def _stripe_element_mode() -> str:
    raw = (
        os.getenv("BILLING_STRIPE_ELEMENT_MODE")
        or os.getenv("STRIPE_ELEMENT_MODE")
        or "card"
    ).strip().lower()
    if raw in ("split", "split-card", "split_card", "cardnumber"):
        return "split"
    return "card"


async def _wait_stripe_cardnumber_ready(page, timeout_ms: int | None = None):
    timeout_ms = timeout_ms or _env_int("STRIPE_ELEMENTS_INPUT_TIMEOUT_MS", 90000)
    deadline = time.time() + (timeout_ms / 1000)
    last_error = ""
    while time.time() < deadline:
        try:
            if await _stripe_input_visible(page, "cardnumber"):
                return
        except Exception as e:
            last_error = str(e)
        await page.wait_for_timeout(250)
    diag = await _stripe_mount_diagnostics(page)
    detail = f"; last={last_error[:240]}" if last_error else ""
    raise RuntimeError(f"Stripe card input not ready after {timeout_ms}ms; diag={str(diag)[:800]}{detail}")


async def _fill_stripe_card_details(page, card: dict):
    """Fill mounted manual Stripe Elements. Default uses unified CardElement to match dashboard card-element telemetry."""
    number = str(card["number"])
    exp_digits = f"{card['exp_month']}{card['exp_year'][-2:]}"
    cvc_digits = re.sub(r"\D", "", str(card["cvc"]))
    if _stripe_element_mode() == "card":
        try:
            frame = await _find_stripe_input_frame(page, "cardnumber", timeout_ms=45000)
            locator = frame.locator('input[name="cardnumber"]').first
            await _focus_stripe_locator(page, locator, "cardnumber")
            type_delay = _billing_stripe_type_delay_ms() if _billing_humanize_stripe_inputs_enabled() else 0
            mm_yy = f"{card['exp_month']}{card['exp_year'][-2:]}"
            # Stripe unified CardElement relies on keyboard events to fan out PAN/expiry/CVC.
            # locator.fill() can set the visible text while leaving the internal expiry state incomplete.
            values = [f"{number} {mm_yy} {card['cvc']}", f"{number}{mm_yy}{card['cvc']}"]
            last_state = None
            hosted_card_element = await _host_stripe_ready_state(page) is not None
            for attempt, value in enumerate(values, start=1):
                await _stripe_keyboard_clear_and_type(page, locator, "cardnumber", value, delay=type_delay)
                complete, state = await _wait_host_stripe_complete(page, timeout_ms=9000)
                last_state = state
                # Hosted Stripe pages created by this code expose __vapiStripeReadyState; if absent,
                # this is the real dashboard UI, where the later iframe-state checker is authoritative.
                if state is None or complete:
                    return
                log.debug(f"Unified CardElement incomplete after keyboard type attempt={attempt}: {state}")
            if hosted_card_element:
                debug_state = await _stripe_debug_input_lengths(page)
                if _env_bool("BILLING_STRIPE_UNIFIED_FALLBACK_SPLIT_ON_INCOMPLETE", True):
                    log.warning(
                        "Unified CardElement incomplete after keyboard fill, remount split Elements: "
                        f"{json.dumps(debug_state, ensure_ascii=False)[:500]}"
                    )
                    await _mount_stripe_elements(page, STRIPE_PK, element_mode_override="split")
                else:
                    raise RuntimeError(f"Unified CardElement incomplete after fill: {json.dumps(debug_state, ensure_ascii=False)[:700]}")
            else:
                return
        except Exception as e:
            if await _host_stripe_ready_state(page) is not None:
                if _env_bool("BILLING_STRIPE_UNIFIED_FALLBACK_SPLIT_ON_INCOMPLETE", True):
                    log.warning(f"Unified CardElement fill failed, remount split Elements: {e}")
                    await _mount_stripe_elements(page, STRIPE_PK, element_mode_override="split")
                else:
                    raise
            else:
                log.warning(f"Unified CardElement fill failed, falling back to split-field fill: {e}")

    await _fill_stripe_input(page, "cardnumber", number, re.sub(r"\D", "", number))
    await _fill_stripe_input(page, "exp-date", f"{card['exp_month']} / {card['exp_year'][-2:]}", exp_digits)
    await _fill_stripe_input(page, "cvc", str(card["cvc"]), cvc_digits)


async def _wait_stripe_inputs_ready(page, timeout_ms: int | None = None):
    timeout_ms = timeout_ms or _env_int("STRIPE_ELEMENTS_INPUT_TIMEOUT_MS", 90000)
    names = ("cardnumber", "exp-date", "cvc")
    deadline = time.time() + (timeout_ms / 1000)
    last_pending = list(names)
    while time.time() < deadline:
        pending = []
        for name in names:
            if not await _stripe_input_visible(page, name):
                pending.append(name)
        if not pending:
            return
        last_pending = pending
        await page.wait_for_timeout(250)
    diag = await _stripe_mount_diagnostics(page)
    raise RuntimeError(f"Stripe input frames not ready after {timeout_ms}ms: pending={last_pending}; diag={str(diag)[:800]}")


async def _find_stripe_input_frame(page, input_name: str, timeout_ms: int = 45000):
    selector = f'input[name="{input_name}"]'
    deadline = time.time() + (timeout_ms / 1000)
    last_error = ""
    while time.time() < deadline:
        for frame in page.frames:
            try:
                locator = frame.locator(selector).first
                if await locator.count() and await locator.is_visible(timeout=200):
                    return frame
            except Exception as e:
                last_error = str(e)
        await page.wait_for_timeout(250)
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"Stripe iframe input not found: {input_name}{detail}")


async def _focus_stripe_locator(page, locator, input_name: str = "cardnumber") -> None:
    """Focus Stripe iframe inputs without waiting on brittle click hit-target checks."""
    try:
        await locator.click(timeout=3000, force=True)
        return
    except Exception as click_error:
        log.debug(f"Stripe input force-click failed, fallback focus: {input_name} {click_error}")
    try:
        await locator.focus(timeout=3000)
        return
    except Exception as focus_error:
        log.debug(f"Stripe input locator.focus failed, fallback DOM focus: {input_name} {focus_error}")
    try:
        await locator.evaluate("el => { el.scrollIntoView({block: 'center', inline: 'center'}); el.focus(); }")
    except Exception as dom_error:
        raise RuntimeError(f"Stripe input focus failed: {input_name}: {dom_error}") from dom_error




async def _stripe_keyboard_clear_and_type(page, locator, input_name: str, value: str, delay: int = 0) -> None:
    """Type into a Stripe iframe input using element-targeted keyboard events, avoiding normal click waits."""
    text = str(value or "")
    timeout = max(15000, int(len(text) * max(int(delay or 0), 1) + 10000))
    await _focus_stripe_locator(page, locator, input_name)
    try:
        await locator.press("Control+A", timeout=3000)
        await locator.press("Backspace", timeout=3000)
    except Exception:
        pass
    if not text:
        return
    # page.keyboard.type can lose focus across Stripe's OOPIF boundary under Cloak;
    # locator.type targets the input's frame/element directly and triggers Stripe change events.
    await locator.type(text, delay=max(int(delay or 0), 0), timeout=timeout)


async def _host_stripe_ready_state(page) -> dict | None:
    try:
        state = await page.evaluate("() => window.__vapiStripeReadyState || null")
        return state if isinstance(state, dict) else None
    except Exception:
        return None


async def _wait_host_stripe_complete(page, timeout_ms: int = 7000) -> tuple[bool, dict | None]:
    deadline = time.time() + (max(500, timeout_ms) / 1000)
    last_state = None
    while time.time() < deadline:
        last_state = await _host_stripe_ready_state(page)
        if last_state is None:
            return False, None
        if bool(last_state.get("complete")):
            return True, last_state
        await page.wait_for_timeout(200)
    return False, last_state



async def _stripe_read_digits_by_name(page, input_name: str) -> str:
    try:
        frame = await _find_stripe_input_frame(page, input_name, timeout_ms=1200)
        locator = frame.locator(f'input[name="{input_name}"]').first
        return re.sub(r"\D", "", await locator.input_value(timeout=1200))
    except Exception:
        return ""


async def _stripe_debug_input_lengths(page) -> dict:
    state = await _host_stripe_ready_state(page)
    return {
        "readyState": state,
        "cardnumberDigits": len(await _stripe_read_digits_by_name(page, "cardnumber")),
        "expiryDigits": len(await _stripe_read_digits_by_name(page, "exp-date")),
        "cvcDigits": len(await _stripe_read_digits_by_name(page, "cvc")),
    }

async def _fill_stripe_input(page, input_name: str, value: str, expected_digits: str = ""):
    frame = await _find_stripe_input_frame(page, input_name)
    locator = frame.locator(f'input[name="{input_name}"]').first

    async def read_digits() -> str:
        try:
            return re.sub(r"\D", "", await locator.input_value(timeout=1500))
        except Exception:
            return ""

    await _focus_stripe_locator(page, locator, input_name)
    type_delay = _billing_stripe_type_delay_ms()
    # Stripe Elements validation is driven by keyboard/input events; use a focused keyboard path even with zero delay.
    await _stripe_keyboard_clear_and_type(
        page,
        locator,
        input_name,
        value,
        delay=type_delay if _billing_humanize_stripe_inputs_enabled() else 0,
    )

    if expected_digits:
        current_digits = await read_digits()
        if current_digits != expected_digits:
            try:
                await _stripe_keyboard_clear_and_type(
                    page,
                    locator,
                    input_name,
                    value,
                    delay=type_delay if _billing_humanize_stripe_inputs_enabled() else 0,
                )
            except Exception:
                await _focus_stripe_locator(page, locator, input_name)
                await locator.press("Control+A", timeout=3000)
                await locator.press("Backspace", timeout=3000)
                await locator.type(value, delay=type_delay if _billing_humanize_stripe_inputs_enabled() else 0, timeout=15000)
            current_digits = await read_digits()
            if current_digits != expected_digits:
                raise RuntimeError(
                    f"Stripe input {input_name} incomplete after fill: got {len(current_digits)} digits, expected {len(expected_digits)}"
                )


async def _load_stripe_js(page, timeout_ms: int = 30000):
    last_error = ""
    per_attempt_timeout = max(1000, timeout_ms // 3)
    for attempt in range(3):
        try:
            await page.evaluate(
                """() => new Promise((resolve, reject) => {
                    if (typeof window.Stripe === 'function') {
                        resolve(true);
                        return;
                    }
                    document.querySelectorAll('script[data-vapi-stripe-js]').forEach((script) => script.remove());
                    const script = document.createElement('script');
                    const timeout = setTimeout(() => reject(new Error('Stripe.js script load timed out')), 10000);
                    script.src = 'https://js.stripe.com/v3/';
                    script.async = true;
                    script.dataset.vapiStripeJs = '1';
                    script.onload = () => {
                        clearTimeout(timeout);
                        resolve(true);
                    };
                    script.onerror = () => {
                        clearTimeout(timeout);
                        reject(new Error('Stripe.js script load failed'));
                    };
                    document.head.appendChild(script);
                })"""
            )
            await page.wait_for_function("() => typeof window.Stripe === 'function'", timeout=per_attempt_timeout)
            return
        except Exception as e:
            last_error = str(e)
            await page.wait_for_timeout(500 * (attempt + 1))

    try:
        stripe_type = await page.evaluate("() => typeof window.Stripe")
    except Exception:
        stripe_type = "unavailable"
    raise RuntimeError(f"Stripe.js not ready after retries: typeof window.Stripe={stripe_type}; last_error={last_error[:300]}")


async def _mount_stripe_elements(page, publishable_key: str, element_mode_override: str | None = None):
    await _load_stripe_js(page)
    event_grace_ms = _env_int("STRIPE_ELEMENTS_EVENT_GRACE_MS", 8000)
    element_mode = (element_mode_override or _stripe_element_mode() or "card").strip().lower()
    if element_mode in ("split-card", "split_card", "cardnumber"):
        element_mode = "split"
    await page.evaluate(
        """async ({ publishableKey, eventGraceMs, elementMode }) => {
            if (typeof window.Stripe !== 'function') {
                throw new Error(`Stripe.js not ready: typeof window.Stripe=${typeof window.Stripe}`);
            }
            for (const key of ['__vapiStripeCardNumber', '__vapiStripeCardExpiry', '__vapiStripeCardCvc', '__vapiStripeCard']) {
                try { if (window[key] && typeof window[key].destroy === 'function') window[key].destroy(); } catch (_) {}
            }
            for (const id of ['card-number', 'card-expiry', 'card-cvc']) {
                const el = document.getElementById(id);
                if (el) {
                    el.innerHTML = '';
                    // Previous unified CardElement mounts hide expiry/cvc containers.
                    // Reset visibility before split remount, otherwise the ready event fires
                    // but Playwright cannot see/focus exp-date and cvc inputs.
                    el.style.display = '';
                    el.style.visibility = '';
                }
            }
            const stripe = window.Stripe(publishableKey);
            const elements = stripe.elements({ locale: 'en' });
            const style = {
                base: {
                    fontSize: '16px',
                    color: '#101828',
                    '::placeholder': { color: '#667085' },
                },
            };
            window.__vapiStripe = stripe;
            window.__vapiStripeElements = elements;
            const state = { mode: elementMode, cardNumber: false, cardExpiry: false, cardCvc: false, eventReady: false, complete: false };
            window.__vapiStripeReadyState = state;
            if (elementMode === 'split') {
                const cardNumber = elements.create('cardNumber', { style, showIcon: true });
                const cardExpiry = elements.create('cardExpiry', { style });
                const cardCvc = elements.create('cardCvc', { style });
                window.__vapiStripeCardNumber = cardNumber;
                window.__vapiStripeCardExpiry = cardExpiry;
                window.__vapiStripeCardCvc = cardCvc;
                cardNumber.on('change', (ev) => { state.cardNumberComplete = !!ev.complete; state.complete = !!(state.cardNumberComplete && state.cardExpiryComplete && state.cardCvcComplete); });
                cardExpiry.on('change', (ev) => { state.cardExpiryComplete = !!ev.complete; state.complete = !!(state.cardNumberComplete && state.cardExpiryComplete && state.cardCvcComplete); });
                cardCvc.on('change', (ev) => { state.cardCvcComplete = !!ev.complete; state.complete = !!(state.cardNumberComplete && state.cardExpiryComplete && state.cardCvcComplete); });
                const pNumber = new Promise((resolve) => cardNumber.on('ready', () => { state.cardNumber = true; resolve(true); }));
                const pExpiry = new Promise((resolve) => cardExpiry.on('ready', () => { state.cardExpiry = true; resolve(true); }));
                const pCvc = new Promise((resolve) => cardCvc.on('ready', () => { state.cardCvc = true; resolve(true); }));
                window.__vapiStripeReady = Promise.all([pNumber, pExpiry, pCvc]).then(() => { state.eventReady = true; return true; });
                cardNumber.mount('#card-number');
                cardExpiry.mount('#card-expiry');
                cardCvc.mount('#card-cvc');
            } else {
                const card = elements.create('card', { style, hidePostalCode: true });
                window.__vapiStripeCard = card;
                window.__vapiStripeCardNumber = card;
                window.__vapiStripeCardExpiry = card;
                window.__vapiStripeCardCvc = card;
                const exp = document.getElementById('card-expiry');
                const cvc = document.getElementById('card-cvc');
                if (exp) exp.style.display = 'none';
                if (cvc) cvc.style.display = 'none';
                card.on('change', (ev) => { state.complete = !!ev.complete; state.error = ev.error ? (ev.error.message || '') : ''; });
                window.__vapiStripeReady = new Promise((resolve) => card.on('ready', () => { state.cardNumber = true; state.cardExpiry = true; state.cardCvc = true; state.eventReady = true; resolve(true); }));
                card.mount('#card-number');
            }
            if (eventGraceMs > 0) {
                await Promise.race([window.__vapiStripeReady, new Promise((resolve) => setTimeout(() => resolve(false), eventGraceMs))]);
            }
            return state;
        }""",
        {"publishableKey": publishable_key, "eventGraceMs": event_grace_ms, "elementMode": element_mode},
    )
    # Stripe 的 ready 事件偶发不触发，但 iframe/input 已可用；以真实可填输入框作为最终判据。
    if element_mode == "split":
        await _wait_stripe_inputs_ready(page)
    else:
        await _wait_stripe_cardnumber_ready(page)


async def _create_stripe_payment_method_browser(
    proxy_url: str,
    card: dict,
    email_addr: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> str:
    debug_dir = Path("/data/browser-bind-debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    screenshot_path = debug_dir / f"stripe-protocol-failed-{timestamp}.png"
    html_path = debug_dir / f"stripe-protocol-page-{timestamp}.html"

    engine, browser_driver = _billing_browser_engine()
    playwright = await browser_driver().start()
    browser = None
    context = None
    try:
        browser_fingerprint = _with_billing_profile(browser_fingerprint, client_ctx)
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url, client_ctx)
        log.info(f"Stripe PM 浏览器引擎: {engine}, headless={_billing_browser_headless()}")
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine, browser_proxy_url))
        context = await browser.new_context(
            proxy=_billing_context_proxy_option(engine, browser_proxy_url),
            user_agent=None if engine == "cloak" and _env_bool("BILLING_CLOAK_NATIVE_UA", True) else (effective_user_agent or None),
            viewport=_billing_profile_from_fingerprint(browser_fingerprint)["viewport"],
            screen=_billing_profile_from_fingerprint(browser_fingerprint)["screen"],
            locale=_billing_locale(),
            timezone_id=_billing_timezone_id(),
            extra_http_headers=_fingerprint_extra_headers(browser_fingerprint),
        )
        await context.route("**/v1/payment_methods**", _make_route_stripe_payment_methods_request(email_addr))
        fp_mode = _stripe_browser_fingerprint_mode()
        if fp_mode in ("full", "1", "true", "on"):
            log.info("Stripe PM 浏览器指纹注入: full")
            await context.add_init_script(_billing_fingerprint_init_script(browser_fingerprint, user_agent))
        elif fp_mode in ("top", "top-frame", "top_frame"):
            log.info("Stripe PM 浏览器指纹注入: top-frame")
            await context.add_init_script(_billing_top_frame_fingerprint_init_script(browser_fingerprint, user_agent))
        elif fp_mode in ("minimal", "min", "0", "false", "off"):
            log.info("Stripe PM 浏览器指纹注入: minimal")
            await context.add_init_script(_billing_minimal_init_script())
        else:
            log.info("Stripe PM 浏览器指纹注入: safe")
            await context.add_init_script(_billing_safe_fingerprint_init_script(browser_fingerprint, user_agent))
        page = await context.new_page()
        await _open_stripe_pm_page(context, page)
        await _mount_stripe_elements(page, STRIPE_PK)
        log.info("Stripe Elements inputs ready")

        await _human_mouse_wiggle(page, "before stripe pm fill")
        await _fill_stripe_card_details(page, card)
        await _human_mouse_wiggle(page, "after stripe pm fill")
        stripe_input_state = await _stripe_debug_input_lengths(page)
        log.info(f"Stripe Elements inputs filled mode={_stripe_element_mode()} state={stripe_input_state}")

        create_timeout_ms = _env_int("STRIPE_CREATE_PM_TIMEOUT_MS", 60000)
        billing_details = _stripe_pm_billing_details(email_addr)
        include_billing_details = bool(billing_details)
        log.info(f"Stripe createPaymentMethod billing_details={'on' if include_billing_details else 'off'}")
        if _billing_mock_stripe_pm_enabled():
            payload = _mock_stripe_payment_method_payload(email_addr, card.get("number", ""))
            result = {"ok": True, "id": payload["id"], "billingDetails": payload.get("billing_details"), "card": payload.get("card"), "mocked": True}
            log.warning(f"BILLING_TEST_MODE/mock Stripe browser PaymentMethod active: pm={_mask_id(payload['id'])}")
        else:
            result = await page.evaluate(
            """async ({ billingDetails, timeoutMs }) => {
                const payload = {
                    type: 'card',
                    card: window.__vapiStripeCardNumber,
                };
                if (billingDetails) {
                    payload.billing_details = billingDetails;
                }
                const create = window.__vapiStripe.createPaymentMethod(payload);
                const timeout = new Promise((resolve) => setTimeout(() => resolve({
                    error: { message: `Stripe createPaymentMethod timeout after ${timeoutMs}ms`, code: 'create_payment_method_timeout', type: 'timeout' }
                }), timeoutMs));
                const result = await Promise.race([create, timeout]);
                if (result.error) {
                    return {
                        ok: false,
                        error: {
                            message: result.error.message || '',
                            code: result.error.code || '',
                            decline_code: result.error.decline_code || '',
                            type: result.error.type || '',
                        },
                    };
                }
                const pm = result.paymentMethod || {};
                return {
                    ok: true,
                    id: pm.id,
                    billingDetails: pm.billing_details || null,
                    card: pm.card ? { brand: pm.card.brand, country: pm.card.country, funding: pm.card.funding, last4: pm.card.last4 } : null,
                };
            }""",
            {"billingDetails": billing_details, "timeoutMs": create_timeout_ms},
            )
        if not isinstance(result, dict) or not result.get("ok"):
            error = (result or {}).get("error") if isinstance(result, dict) else result
            if isinstance(error, dict):
                parts = [str(error.get(key) or "") for key in ("message", "code", "decline_code", "type") if error.get(key)]
                summary = "; ".join(parts)
            else:
                summary = str(error)
            raise RuntimeError(f"Stripe browser payment_methods failed: {summary[:300]}")
        pm_id = result.get("id")
        if not pm_id:
            raise RuntimeError(f"Stripe browser payment_methods returned no pm id: {result}")
        if _env_bool("STRIPE_PM_DEBUG", True):
            _write_billing_debug("stripe-pm", {
                "type": "stripe-pm",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "pm": _safe_value("paymentMethodId", pm_id),
                "billingDetailsSent": bool(billing_details),
                "result": _safe_payload(result),
                "cardTail": card["number"][-4:],
                "email": email_addr,
            })
        log.info(f"Stripe browser PaymentMethod 创建成功: {_mask_id(pm_id)}")
        return pm_id
    except Exception as e:
        try:
            if "page" in locals():
                await page.screenshot(path=str(screenshot_path), full_page=True)
                html_path.write_text(await page.content(), encoding="utf-8")
                raise RuntimeError(f"{e}; screenshot={screenshot_path}; html={html_path}") from e
        except RuntimeError:
            raise
        except Exception:
            pass
        raise
    finally:
        if context:
            await _close_context_safely(context)
        if browser:
            await _close_browser_safely(browser)
        await _stop_playwright_safely(playwright)


async def _create_stripe_payment_method_solver(proxy_url: str, card: dict, email_addr: str) -> tuple[str, str]:
    if not STRIPE_SOLVER_URL or os.getenv("STRIPE_SOLVER_DISABLED", "0") in ("1", "true", "TRUE", "yes", "YES"):
        raise RuntimeError("Stripe solver is disabled")

    payload = {
        "email": email_addr,
        "publishableKey": STRIPE_PK,
        "card": {
            "number": card["number"],
            "exp_month": card["exp_month"],
            "exp_year": card["exp_year"],
            "cvc": card["cvc"],
        },
    }
    solver_proxy = _turnstile_solver_proxy(proxy_url)
    if solver_proxy:
        payload["proxy"] = solver_proxy
    if solver_proxy != str(proxy_url or "").strip():
        log.info(
            f"[{email_addr}] solver browser 代理固定为: "
            f"{'direct' if not solver_proxy else solver_proxy} "
            f"(outer={'direct' if not proxy_url else proxy_url})"
        )

    timeout = httpx.Timeout(30.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        task_id = ""
        last_error = ""
        for _ in range(10):
            try:
                response = await client.post(f"{STRIPE_SOLVER_URL}/stripe/payment-method", json=payload)
                data = response.json()
                if data.get("errorId") == 1:
                    raise RuntimeError(data.get("errorDescription") or str(data))
                task_id = data.get("taskId") or data.get("task_id") or ""
                if task_id:
                    break
                last_error = str(data)
            except Exception as e:
                last_error = str(e)
            await asyncio.sleep(2)

        if not task_id:
            raise RuntimeError(f"Stripe solver task creation failed: {last_error}")

        deadline = asyncio.get_event_loop().time() + STRIPE_SOLVER_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            response = await client.get(f"{STRIPE_SOLVER_URL}/result", params={"id": task_id})
            data = response.json()
            if data.get("status") == "ready":
                solution = data.get("solution") or {}
                pm_id = solution.get("paymentMethodId") or solution.get("id") or ""
                user_agent = solution.get("userAgent") or ""
                if pm_id:
                    log.info(f"Stripe solver PaymentMethod 创建成功: {_mask_id(pm_id)}")
                    return pm_id, user_agent

            if data.get("errorId") == 1 and data.get("errorCode") != "CAPTCHA_NOT_READY":
                raise RuntimeError(f"Stripe solver failed: {data.get('errorDescription') or data}")

            await asyncio.sleep(TURNSTILE_SOLVER_POLL_INTERVAL)

        raise RuntimeError("Stripe solver timed out waiting for PaymentMethod")


async def _bind_card_protocol(
    proxy_url: str,
    proxies,
    org_token: str,
    email_addr: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
    keys: dict | None = None,
    card_override: dict | None = None,
) -> str:
    """协议绑卡：Stripe.js/浏览器创建 PaymentMethod → Vapi add-card。支持卡池/生成卡覆盖。"""
    raw_card = card_override or config.billing_card()
    if not all(raw_card.values()):
        raise RuntimeError("Billing card config incomplete: number, expiry, and cvc are required")
    _validate_card_config(raw_card)
    card = {
        "number": raw_card["number"],
        "exp_month": raw_card["exp_month"],
        "exp_year": raw_card["exp_year"],
        "cvc": raw_card["cvc"],
    }
    log.info(f"使用后台配置协议绑卡: ****{card['number'][-4:]} exp={card['exp_month']}/{card['exp_year']}")
    attach_mode = os.getenv("BILLING_ATTACH_MODE", "protocol").strip().lower()
    stripe_mode = os.getenv("STRIPE_PAYMENT_METHOD_MODE", "browser").strip().lower()
    if attach_mode in ("same-browser", "dashboard-browser", "browser-stripe", "stripe-dashboard"):
        try:
            return await _bind_card_dashboard_browser_fetch(
                proxy_url,
                keys or {},
                card,
                email_addr,
                user_agent,
                browser_fingerprint,
                client_ctx,
            )
        except Exception as e:
            if _billing_attach_declined_error(str(e)):
                await _recover_after_billing_attach_decline(f"same-browser add-card decline: {type(e).__name__}: {e}")
                if keys and _env_bool("BILLING_SAME_BROWSER_FALLBACK_ON_ADD_CARD_400", False):
                    log.warning(
                        f"same-browser add-card 被拒，按配置切完整 billing UI fallback 重试: "
                        f"tail=****{_billing_card_tail(raw_card)} error={e}"
                    )
                    try:
                        return await _bind_card_browser_with_timeout(
                            proxy_url,
                            keys,
                            email_addr,
                            user_agent,
                            browser_fingerprint,
                            client_ctx,
                            reason="full billing UI fallback after same-browser decline",
                            card_override=raw_card,
                        )
                    except Exception as fallback_error:
                        await _recover_after_billing_attach_decline(
                            f"full billing UI fallback failed after same-browser decline: "
                            f"{type(fallback_error).__name__}: {fallback_error}"
                        )
                        raise RuntimeError(
                            f"full billing UI fallback after same-browser decline failed: {fallback_error}; original={e}"
                        ) from fallback_error
                log.warning(
                    f"same-browser add-card 400/card_declined 不再默认同卡 full-UI 复试，"
                    f"交给外层卡池/新环境切换: tail=****{_billing_card_tail(raw_card)}"
                )
            raise

    async def create_payment_method_for_attempt(attempt: int) -> tuple[str, str]:
        if stripe_mode in ("solver", "nexos"):
            try:
                pm, stripe_ua = await _create_stripe_payment_method_solver(proxy_url, card, email_addr)
                return pm, stripe_ua
            except Exception as e:
                if os.getenv("STRIPE_SOLVER_FALLBACK", "0") not in ("1", "true", "TRUE", "yes", "YES"):
                    raise RuntimeError(f"Stripe solver payment_methods failed: {e}") from e
                log.warning(f"Stripe solver 不可用，回退本地浏览器: {e}")
                pm = await _create_stripe_payment_method_browser(proxy_url, card, email_addr, user_agent, browser_fingerprint, client_ctx)
                return pm, user_agent

        if attempt == 1:
            log.info("Stripe PaymentMethod 使用独立浏览器创建，复用 Turnstile 指纹")
        else:
            log.info(f"Stripe PaymentMethod 重新创建用于 add-card 重试: attempt={attempt}")
        pm = await _create_stripe_payment_method_browser(proxy_url, card, email_addr, user_agent, browser_fingerprint, client_ctx)
        return pm, user_agent

    retry_attempts = max(1, _env_int("BILLING_ATTACH_DECLINE_RETRY_ATTEMPTS", 2))
    card_declined_attempts = max(1, _env_int("BILLING_ATTACH_CARD_DECLINED_RETRY_ATTEMPTS", 2))
    retry_sleep = max(0, _env_int("BILLING_ATTACH_DECLINE_RETRY_SLEEP_SECONDS", 0))
    attach_error: Exception | None = None
    for attempt in range(1, retry_attempts + 1):
        if attempt > 1 and retry_sleep:
            await asyncio.sleep(retry_sleep)
        pm_id, stripe_user_agent = await create_payment_method_for_attempt(attempt)
        try:
            await _human_pause_before_add_card(None, email_addr, "protocol add-card")
            await wait_billing_attach_slot(email_addr, "protocol add-card")
            if attach_mode in ("browser", "browser-fetch", "fetch", "page-fetch"):
                if not keys:
                    raise RuntimeError("browser-fetch attach requires keys")
                await _attach_payment_method_browser_fetch(
                    proxy_url,
                    keys,
                    pm_id,
                    email_addr,
                    stripe_user_agent or user_agent,
                    browser_fingerprint,
                    client_ctx,
                )
            else:
                attach_proxies = _make_session(_billing_stage_proxy(proxy_url, client_ctx))
                await asyncio.to_thread(
                    _attach_payment_method,
                    attach_proxies,
                    org_token,
                    pm_id,
                    stripe_user_agent or user_agent,
                    browser_fingerprint,
                    client_ctx,
                )
            return pm_id
        except Exception as e:
            attach_error = e
            text = str(e)
            if not _billing_attach_declined_error(text):
                raise

            is_card_declined = _billing_card_declined_error(text)
            max_attempts_for_error = min(retry_attempts, card_declined_attempts) if is_card_declined else retry_attempts

            if (
                keys
                and attach_mode not in ("browser", "browser-fetch", "fetch", "page-fetch")
                and _env_bool("BILLING_ATTACH_BROWSER_FETCH_ON_DECLINE", True)
                and (not is_card_declined or _env_bool("BILLING_ATTACH_BROWSER_FETCH_ON_CARD_DECLINED", True))
            ):
                log.warning(f"protocol add-card 400/decline，先用同一 PM 走真实 dashboard fetch 补试: attempt={attempt}/{retry_attempts}")
                try:
                    return await _attach_payment_method_browser_fetch(
                        proxy_url,
                        keys,
                        pm_id,
                        email_addr,
                        stripe_user_agent or user_agent,
                        browser_fingerprint,
                        client_ctx,
                    )
                except Exception as browser_fetch_error:
                    attach_error = browser_fetch_error
                    if not _billing_attach_declined_error(str(browser_fetch_error)):
                        raise
                    is_card_declined = is_card_declined or _billing_card_declined_error(str(browser_fetch_error))
                    max_attempts_for_error = min(retry_attempts, card_declined_attempts) if is_card_declined else retry_attempts
                    log.warning(
                        "dashboard browser-fetch add-card 补试仍被拒: "
                        f"{type(browser_fetch_error).__name__}: {browser_fetch_error}"
                    )

            if is_card_declined and _billing_card_declined_stop_enabled():
                _record_billing_attach_risk(f"card_declined stop: {type(attach_error).__name__}: {attach_error}")
                log.warning(
                    f"protocol add-card 明确 card_declined，按 BILLING_STOP_ON_CARD_DECLINED=1 终止本账号绑卡，"
                    f"不再切换 WARP/指纹重试: attempt={attempt}/{retry_attempts}"
                )
                raise
            if is_card_declined and not _env_bool("BILLING_ATTACH_RECOVER_ON_CARD_DECLINED", True):
                restarted, recycled = False, False
                log.warning(
                    f"protocol add-card 明确 card_declined，配置为不恢复环境: "
                    f"attempt={attempt}/{retry_attempts}"
                )
            else:
                restarted, recycled = await _recover_after_billing_attach_decline(
                    f"add-card decline attempt={attempt}/{retry_attempts}: {type(attach_error).__name__}: {attach_error}"
                )
            if attempt < max_attempts_for_error:
                if is_card_declined:
                    _reset_billing_environment_for_retry(client_ctx, f"protocol card_declined retry attempt={attempt + 1}")
                log.warning(
                    f"add-card 400/decline 后按环境问题重试: attempt={attempt + 1}/{max_attempts_for_error} "
                    f"warpRestarted={restarted} poolRecycled={recycled}"
                )
                continue

            if keys and _env_bool("BILLING_BROWSER_FALLBACK_ON_DECLINE", False):
                log.warning(f"browser/protocol attach 被拒，按配置回退完整浏览器绑卡重试: {e}")
                try:
                    return await _bind_card_browser_with_timeout(
                        proxy_url,
                        keys,
                        email_addr,
                        user_agent,
                        browser_fingerprint,
                        client_ctx,
                        reason="browser fallback after add-card decline",
                        card_override=raw_card,
                    )
                except Exception as fallback_error:
                    await _recover_after_billing_attach_decline(
                        f"browser fallback failed after add-card decline: {type(fallback_error).__name__}: {fallback_error}"
                    )
                    raise RuntimeError(
                        f"browser fallback after attach decline failed: {fallback_error}; original={e}"
                    ) from fallback_error
            raise

    if attach_error:
        raise attach_error
    raise RuntimeError("billing attach failed without error")


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception:
        return {}


def _billing_card_for_browser(card: dict | None = None) -> dict:
    card = card or config.billing_card()
    if not all(card.values()):
        raise RuntimeError("Billing card config incomplete: number, expiry, and cvc are required")
    _validate_card_config(card)
    return {
        "number": card["number"],
        "expiry": f"{card['exp_month']} / {card['exp_year'][-2:]}",
        "cvc": card["cvc"],
    }


def _stripe_pm_billing_details(email_addr: str) -> dict | None:
    if not _stripe_pm_billing_details_enabled():
        return None
    details = {"email": email_addr, "name": _billing_name(email_addr)}
    postal = _stripe_pm_postal_code()
    country = _stripe_pm_country()
    if postal or country:
        details["address"] = {}
        if postal:
            details["address"]["postal_code"] = postal
        if country:
            details["address"]["country"] = country
    return details


def _billing_card_tail(card: dict | None) -> str:
    try:
        return str((card or {}).get("number") or "")[-4:] or "unknown"
    except Exception:
        return "unknown"


def _billing_card_key_material(card: dict | None) -> str:
    card = card or {}
    mode = os.getenv("BILLING_CARD_DECLINE_KEY_MODE", "pan").strip().lower()
    number = str(card.get("number") or "")
    if mode in ("full", "card", "all"):
        parts = [
            number,
            str(card.get("exp_month") or ""),
            str(card.get("exp_year") or ""),
            str(card.get("cvc") or ""),
        ]
        return "|".join(parts)
    return number


def _billing_card_key(card: dict | None) -> str:
    # 默认按 PAN 隔离。之前把 exp/cvc 纳入 key，随机日期/CVV 会绕过同卡 card_declined 止损。
    return _hash_value(_billing_card_key_material(card))


def _billing_card_legacy_key(card: dict | None) -> str:
    card = card or {}
    parts = [
        str(card.get("number") or ""),
        str(card.get("exp_month") or ""),
        str(card.get("exp_year") or ""),
        str(card.get("cvc") or ""),
    ]
    return _hash_value("|".join(parts))


def _billing_card_decline_tail_fallback_enabled() -> bool:
    # 兼容旧版 full-card hash 状态；用户随机日期/CVV 后仍应按同 PAN/同 tail 止损。
    return _env_bool("BILLING_CARD_DECLINE_TAIL_FALLBACK", True)


def _billing_card_decline_record_by_tail(card: dict | None, state: dict | None = None) -> dict:
    if not _billing_card_decline_tail_fallback_enabled():
        return {}
    tail = _billing_card_tail(card)
    if not tail or tail == "unknown":
        return {}
    state = state if isinstance(state, dict) else _load_billing_card_decline_state()
    cards = state.get("cards")
    if not isinstance(cards, dict):
        return {}
    now = time.time()
    for rec in cards.values():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("tail") or "") == tail and float(rec.get("quarantinedUntil", 0) or 0) > now:
            return rec
    return {}


def _billing_card_decline_state_path() -> Path:
    return Path(os.getenv("BILLING_CARD_DECLINE_STATE", "/data/billing-card-declines.json"))


def _billing_card_decline_threshold() -> int:
    return max(1, _env_int("BILLING_CARD_DECLINE_QUARANTINE_THRESHOLD", 1))


def _billing_card_decline_quarantine_seconds() -> int:
    return max(0, _env_int("BILLING_CARD_DECLINE_QUARANTINE_SECONDS", 24 * 3600))


def _load_billing_card_decline_state() -> dict:
    path = _billing_card_decline_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.debug(f"读取 billing card decline state 失败: {e}")
        return {}


def _save_billing_card_decline_state(state: dict) -> None:
    path = _billing_card_decline_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        log.warning(f"写入 billing card decline state 失败: {e}")


def _billing_card_decline_record(card: dict | None, state: dict | None = None) -> dict:
    state = state if isinstance(state, dict) else _load_billing_card_decline_state()
    cards = state.get("cards")
    if not isinstance(cards, dict):
        return {}
    rec = cards.get(_billing_card_key(card))
    if not isinstance(rec, dict):
        rec = cards.get(_billing_card_legacy_key(card))
    if not isinstance(rec, dict):
        rec = _billing_card_decline_record_by_tail(card, state)
    return rec if isinstance(rec, dict) else {}


def _billing_card_quarantine_reason(card: dict | None, state: dict | None = None) -> str:
    if _env_bool("BILLING_CARD_DECLINE_QUARANTINE_DISABLED", False):
        return ""
    rec = _billing_card_decline_record(card, state)
    until = float(rec.get("quarantinedUntil", 0) or 0)
    if until <= time.time():
        return ""
    until_iso = rec.get("quarantinedUntilIso") or datetime.fromtimestamp(until, timezone.utc).isoformat()
    count = rec.get("declineCount", 0)
    return f"card ****{_billing_card_tail(card)} quarantined until {until_iso} after {count} card_declined"


def _filter_quarantined_billing_cards(cards: list[dict]) -> tuple[list[dict], list[str]]:
    state = _load_billing_card_decline_state()
    available: list[dict] = []
    skipped: list[str] = []
    for card in cards:
        reason = _billing_card_quarantine_reason(card, state)
        if reason:
            skipped.append(reason)
        else:
            available.append(card)
    return available, skipped


def _billing_card_pool_state_path() -> Path:
    return Path(os.getenv("BILLING_CARD_POOL_STATE", "/data/billing-card-pool-state.json"))


def _billing_card_pool_selection_mode() -> str:
    return os.getenv("BILLING_CARD_POOL_SELECTION", "round-robin").strip().lower() or "round-robin"


def _order_billing_cards_for_registration(cards: list[dict]) -> list[dict]:
    cards = list(cards or [])
    if len(cards) <= 1:
        return cards
    mode = _billing_card_pool_selection_mode()
    if mode in ("ordered", "static", "first", "off", "0", "false", "no"):
        return cards
    if mode in ("random", "shuffle"):
        random.shuffle(cards)
        log.info("billing card pool shuffled: selected first tail=****%s total=%d", _billing_card_tail(cards[0]), len(cards))
        return cards
    if mode not in ("round-robin", "roundrobin", "rr", "rotate"):
        log.warning(f"未知 BILLING_CARD_POOL_SELECTION={mode!r}，按 ordered 处理")
        return cards

    path = _billing_card_pool_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            file.seek(0)
            raw = file.read().strip()
            try:
                state = json.loads(raw) if raw else {}
            except Exception:
                state = {}
            cursor = int(state.get("cursor", 0) or 0)
            start = cursor % len(cards)
            ordered = cards[start:] + cards[:start]
            state.update({
                "cursor": cursor + 1,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "lastSelectedTail": _billing_card_tail(ordered[0]),
                "poolSize": len(cards),
                "mode": mode,
            })
            file.seek(0)
            file.truncate()
            file.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            log.info(
                f"billing card pool round-robin: cursor={cursor}->{cursor + 1} "
                f"selected=****{_billing_card_tail(ordered[0])} total={len(cards)}"
            )
            return ordered
    except Exception as e:
        log.warning(f"billing card pool round-robin 状态不可用，回退随机选择: {e}")
        random.shuffle(cards)
        return cards


def _record_billing_card_decline(card: dict | None, reason: str = "") -> None:
    if _env_bool("BILLING_CARD_DECLINE_QUARANTINE_DISABLED", False):
        log.info(f"billing card ****{_billing_card_tail(card)} card_declined 记录跳过：当前按环境问题处理，未隔离卡")
        return
    state = _load_billing_card_decline_state()
    cards = state.setdefault("cards", {})
    if not isinstance(cards, dict):
        cards = {}
        state["cards"] = cards
    key = _billing_card_key(card)
    now = time.time()
    rec = cards.get(key) if isinstance(cards.get(key), dict) else {}
    count = int(rec.get("declineCount", 0) or 0) + 1
    rec.update({
        "cardKey": key,
        "keyMode": os.getenv("BILLING_CARD_DECLINE_KEY_MODE", "pan").strip().lower() or "pan",
        "tail": _billing_card_tail(card),
        "declineCount": count,
        "lastDeclinedAt": now,
        "lastDeclinedAtIso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "lastReason": str(reason or "")[:500],
    })
    threshold = _billing_card_decline_threshold()
    cooldown = _billing_card_decline_quarantine_seconds()
    if count >= threshold and cooldown > 0:
        until = now + cooldown
        rec["quarantinedUntil"] = until
        rec["quarantinedUntilIso"] = datetime.fromtimestamp(until, timezone.utc).isoformat()
        log.warning(
            f"billing card ****{_billing_card_tail(card)} card_declined 达到 {count}/{threshold}，"
            f"已隔离 {cooldown}s，避免继续刷同卡"
        )
    cards[key] = rec
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    _save_billing_card_decline_state(state)


def _clear_billing_card_decline(card: dict | None) -> None:
    state = _load_billing_card_decline_state()
    cards = state.get("cards")
    if not isinstance(cards, dict):
        return
    keys_to_remove = {_billing_card_key(card), _billing_card_legacy_key(card)}
    tail = _billing_card_tail(card)
    if _billing_card_decline_tail_fallback_enabled() and tail and tail != "unknown":
        for key, rec in list(cards.items()):
            if isinstance(rec, dict) and str(rec.get("tail") or "") == tail:
                keys_to_remove.add(key)
    removed = False
    for key in keys_to_remove:
        if key in cards:
            cards.pop(key, None)
            removed = True
    if not removed:
        return
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    _save_billing_card_decline_state(state)
    log.info(f"billing card ****{_billing_card_tail(card)} 已成功，清除 decline 隔离记录")


def _billing_browser_card_pool_mode(bind_mode: str | None = None) -> bool:
    mode = (bind_mode or os.getenv("BILLING_BIND_MODE", "browser")).strip().lower()
    # Card pool/generator is useful for both full browser and protocol+same-browser attach.
    # Keep api/raw on the primary card unless explicitly moved to browser/protocol modes.
    return mode not in ("api", "raw")


def _billing_card_complete(card: dict | None) -> bool:
    card = card or {}
    return bool(card.get("number") and card.get("exp_month") and card.get("exp_year") and card.get("cvc"))


def _billing_cards_for_preflight(bind_mode: str | None = None) -> list[dict]:
    # Browser/protocol modes support card pool/generator rotation; api/raw use the primary card only.
    if _billing_browser_card_pool_mode(bind_mode):
        return [card for card in config.billing_cards() if _billing_card_complete(card)]
    primary = config.billing_card()
    return [primary] if _billing_card_complete(primary) else []


def _available_billing_cards_for_mode(bind_mode: str | None = None) -> tuple[list[dict], list[str]]:
    return _filter_quarantined_billing_cards(_billing_cards_for_preflight(bind_mode))


def ensure_billing_cards_available_for_mode(bind_mode: str | None = None) -> tuple[bool, list[str]]:
    """注册前 preflight：无可用卡时不要先生成邮箱/注册账号。"""
    available, skipped = _available_billing_cards_for_mode(bind_mode)
    if available or _env_bool("BILLING_CARD_DECLINE_ALLOW_ALL_QUARANTINED", False):
        return True, skipped
    return False, skipped or ["no billing cards configured"]


def _safe_card_descriptor(card: dict | None) -> dict:
    card = card or {}
    return {
        "number": _safe_value("card[number]", card.get("number", "")),
        "exp_month": str(card.get("exp_month") or ""),
        "exp_year": str(card.get("exp_year") or ""),
        "cvc": _safe_value("card[cvc]", card.get("cvc", "")),
    }


def _billing_browser_engine():
    # cloak/cloakbrowser 表示启动外置 CloakBrowser/反指纹 Chromium。
    # 默认严格模式：未配置真实 CLOAK_BROWSER_PATH/BILLING_CLOAK_BROWSER_PATH
    # 时直接失败，不再回退镜像内置 Chromium，避免“看似 cloak 实际非 cloak”。
    engine = os.getenv("BILLING_BROWSER_ENGINE", "playwright").strip().lower()
    if engine in ("cloak", "cloakbrowser", "cloak-browser", "anti-detect", "antidetect"):
        return "cloak", async_playwright
    if engine in ("patchright", "stealth", "patched"):
        try:
            from patchright.async_api import async_playwright as patchright_async_playwright
            return "patchright", patchright_async_playwright
        except Exception as e:
            log.warning(f"Patchright 不可用，回退 Playwright: {e}")
    return "playwright", async_playwright


def _billing_browser_headless() -> bool:
    # 近期 add-card 400 样本显示 headful+xvfb 成功率明显更好；默认 headful，显式 BILLING_BROWSER_HEADLESS=1 可回退无头。
    value = os.getenv("BILLING_BROWSER_HEADLESS", "0").strip().lower()
    return value not in ("0", "false", "no", "off")


def _billing_proxy_arg(proxy_url: str = "") -> list[str]:
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return []
    return [f"--proxy-server={proxy_url}"]


def _billing_context_proxy_option(engine: str, proxy_url: str = ""):
    if engine == "cloak" and _env_bool("BILLING_CLOAK_PROXY_LAUNCH_ARG", True):
        return None
    return {"server": proxy_url} if proxy_url else None


def _billing_launch_args(engine: str, proxy_url: str = "") -> list[str]:
    if engine == "cloak":
        extra_args = [
            "--disable-dev-shm-usage",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
        if _env_bool("BILLING_CLOAK_PROXY_LAUNCH_ARG", True):
            extra_args.extend(_billing_proxy_arg(proxy_url))
        if os.getenv("BILLING_CLOAK_WEBRTC_IP_AUTO", "1").strip().lower() not in ("0", "false", "no", "off"):
            extra_args.append("--fingerprint-webrtc-ip=auto")
        try:
            from cloakbrowser import build_args  # type: ignore
            return build_args(
                True,
                extra_args,
                timezone=_billing_timezone_id(),
                locale=_billing_locale(),
                headless=_billing_browser_headless(),
            )
        except Exception as e:
            if _env_bool("BILLING_CLOAK_STRICT", _env_bool("CLOAK_BROWSER_STRICT", True)):
                raise RuntimeError(f"CloakBrowser build_args 不可用: {e}") from e
            log.warning(f"CloakBrowser build_args 不可用，使用兼容参数: {e}")

    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]
    if engine != "cloak" and os.getenv("BILLING_ENABLE_WEBGL", "1").strip().lower() not in ("0", "false", "no", "off"):
        args.extend([
            "--enable-webgl",
            "--enable-webgl2",
            "--ignore-gpu-blocklist",
            "--use-angle=swiftshader",
            "--use-gl=swiftshader",
            "--enable-unsafe-swiftshader",
        ])
    if engine != "patchright":
        args.append("--disable-blink-features=AutomationControlled")
    return args


def _cloak_browser_executable_path() -> str | None:
    explicit_candidates = [
        os.getenv("CLOAK_BROWSER_PATH", ""),
        os.getenv("BILLING_CLOAK_BROWSER_PATH", ""),
        os.getenv("BILLING_BROWSER_EXECUTABLE", ""),
        os.getenv("CLOAK_BROWSER_EXECUTABLE", ""),
    ]
    for candidate in explicit_candidates:
        if candidate and Path(candidate).exists():
            return candidate

    # Real CloakBrowser package: downloads/caches patched stealth Chromium and
    # returns its chrome executable. This is the preferred path in strict mode.
    try:
        from cloakbrowser import ensure_binary  # type: ignore
        cloak_path = ensure_binary()
        if cloak_path and Path(cloak_path).exists():
            return str(cloak_path)
    except Exception as e:
        log.warning(f"CloakBrowser ensure_binary 不可用: {e}")

    fallback_candidates = [
        config.CHROME_PATH,
        "/ms-playwright/chromium-1223/chrome-linux64/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    if _env_bool("BILLING_CLOAK_STRICT", _env_bool("CLOAK_BROWSER_STRICT", True)):
        return None
    for candidate in fallback_candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _billing_launch_kwargs(engine: str, proxy_url: str = "") -> dict:
    executable_path = _cloak_browser_executable_path() if engine == "cloak" else _browser_executable_path()
    if engine == "cloak" and _env_bool("BILLING_CLOAK_STRICT", _env_bool("CLOAK_BROWSER_STRICT", True)) and not executable_path:
        raise RuntimeError(
            "BILLING_BROWSER_ENGINE=cloak 且严格模式开启，但未找到真实 CloakBrowser executable；"
            "请设置 CLOAK_BROWSER_PATH 或 BILLING_CLOAK_BROWSER_PATH，或显式 BILLING_CLOAK_STRICT=0 才允许回退。"
        )
    kwargs = {
        "headless": _billing_browser_headless(),
        "executable_path": executable_path,
        "args": _billing_launch_args(engine, proxy_url),
    }
    if engine == "cloak":
        try:
            from cloakbrowser.config import IGNORE_DEFAULT_ARGS  # type: ignore
            kwargs["ignore_default_args"] = IGNORE_DEFAULT_ARGS
        except Exception:
            kwargs["ignore_default_args"] = ["--enable-automation", "--enable-unsafe-swiftshader"]
    elif engine != "patchright" and os.getenv("BILLING_IGNORE_ENABLE_AUTOMATION", "1").strip().lower() not in ("0", "false", "no"):
        kwargs["ignore_default_args"] = ["--enable-automation"]
    return kwargs


def _billing_attach_declined_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(part in lowered for part in (
        "card was declined",
        "couldn't attach payment method",
        "couldn’t attach payment method",
        "couldnt attach payment method",
        "browser-fetch add-card 400",
        "add-card 400",
        "stripe/add-card 400",
    ))


def _billing_card_declined_stop_enabled() -> bool:
    # Stripe 明确返回 card_declined 时默认不按环境/指纹继续重试，避免把支付拒绝扩大成批量失败。
    return _env_bool("BILLING_STOP_ON_CARD_DECLINED", True)


def _billing_environment_retryable_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(part in lowered for part in (
        "net::err_aborted",
        "net::err_tunnel_connection_failed",
        "net::err_proxy_connection_failed",
        "net::err_connection_reset",
        "net::err_connection_closed",
        "net::err_timed_out",
        "page.goto",
        "navigation timeout",
        "timeout ",
        "target page, context or browser has been closed",
        "browser billing timed out",
    ))


def _billing_card_declined_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(part in lowered for part in (
        "your card was declined",
        "card was declined",
        "card_declined",
        "decline_code",
    ))


def _billing_attach_risk_state_path() -> Path:
    return Path(os.getenv("BILLING_ATTACH_RISK_STATE", "/data/billing-attach-risk-state.json"))


def _billing_attach_risk_enabled() -> bool:
    return _env_bool("BILLING_ATTACH_400_COOLDOWN_ENABLED", False)


def _billing_attach_risk_threshold() -> int:
    return max(1, _env_int("BILLING_ATTACH_400_COOLDOWN_THRESHOLD", 3))


def _billing_attach_risk_cooldown_seconds() -> int:
    return max(0, _env_int("BILLING_ATTACH_400_COOLDOWN_SECONDS", 600))


def _billing_attach_risk_load() -> dict:
    path = _billing_attach_risk_state_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _billing_attach_risk_save(state: dict) -> None:
    if not _billing_attach_risk_enabled():
        return
    path = _billing_attach_risk_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            file.seek(0)
            file.truncate()
            file.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        log.warning(f"billing attach risk state write failed: {e}")


def _record_billing_attach_risk(reason: str = "") -> None:
    if not _billing_attach_risk_enabled():
        return
    now = time.time()
    state = _billing_attach_risk_load()
    consecutive = int(state.get("consecutive400", 0) or 0) + 1
    threshold = _billing_attach_risk_threshold()
    cooldown = _billing_attach_risk_cooldown_seconds()
    cooldown_until = float(state.get("cooldownUntil", 0) or 0)
    if consecutive >= threshold and cooldown > 0:
        cooldown_until = max(cooldown_until, now + cooldown)
    state.update({
        "consecutive400": consecutive,
        "threshold": threshold,
        "cooldownSeconds": cooldown,
        "cooldownUntil": cooldown_until,
        "lastReason": str(reason or "")[:500],
        "lastAt": datetime.now(timezone.utc).isoformat(),
    })
    _billing_attach_risk_save(state)
    if cooldown_until > now:
        log.warning(
            "billing attach 400 风控冷却已激活: "
            f"consecutive={consecutive}/{threshold} remaining={cooldown_until - now:.0f}s"
        )


def _clear_billing_attach_risk(reason: str = "") -> None:
    if not _billing_attach_risk_enabled():
        return
    state = _billing_attach_risk_load()
    if not state.get("consecutive400") and not state.get("cooldownUntil"):
        return
    state.update({
        "consecutive400": 0,
        "cooldownUntil": 0,
        "clearedAt": datetime.now(timezone.utc).isoformat(),
        "clearReason": str(reason or "billing success")[:300],
    })
    _billing_attach_risk_save(state)


async def wait_billing_attach_risk_cooldown_if_needed() -> None:
    if not _billing_attach_risk_enabled():
        return
    state = _billing_attach_risk_load()
    remaining = float(state.get("cooldownUntil", 0) or 0) - time.time()
    if remaining <= 0:
        return
    max_wait = max(0, _env_int("BILLING_ATTACH_400_COOLDOWN_MAX_WAIT_SECONDS", _billing_attach_risk_cooldown_seconds()))
    wait_for = min(remaining, max_wait) if max_wait > 0 else 0
    if wait_for <= 0:
        return
    log.warning(
        "检测到连续 add-card 400 风控冷却，暂停新注册以避免继续放大环境特征: "
        f"sleep={wait_for:.0f}s remaining={remaining:.0f}s consecutive={state.get('consecutive400')}"
    )
    await asyncio.sleep(wait_for)


def _billing_attach_rate_state_path() -> Path:
    return Path(os.getenv("BILLING_ATTACH_RATE_STATE", "/data/billing-attach-rate-state.json"))


def _billing_attach_rate_limit_enabled() -> bool:
    return _env_bool("BILLING_ATTACH_RATE_LIMIT_ENABLED", False)


def _billing_attach_min_interval_seconds() -> int:
    return max(0, _env_int("BILLING_ATTACH_MIN_INTERVAL_SECONDS", 0))


def _billing_attach_interval_jitter_seconds() -> int:
    return max(0, _env_int("BILLING_ATTACH_INTERVAL_JITTER_SECONDS", 0))


def _billing_before_add_card_delay_ms() -> int:
    min_ms = max(0, _env_int("BILLING_BEFORE_ADD_CARD_MIN_MS", 0))
    max_ms = max(min_ms, _env_int("BILLING_BEFORE_ADD_CARD_MAX_MS", 0))
    if max_ms <= 0:
        return 0
    return random.randint(min_ms, max_ms) if max_ms > min_ms else min_ms


def _billing_before_billing_delay_ms() -> int:
    min_ms = max(0, _env_int("BILLING_BEFORE_BILLING_MIN_MS", 0))
    max_ms = max(min_ms, _env_int("BILLING_BEFORE_BILLING_MAX_MS", 0))
    if max_ms <= 0:
        return 0
    return random.randint(min_ms, max_ms) if max_ms > min_ms else min_ms


async def wait_before_billing_stage(email_addr: str = "") -> int:
    delay_ms = _billing_before_billing_delay_ms()
    if delay_ms <= 0:
        return 0
    log.warning(f"[{email_addr}] 注册/验证完成后进入绑卡前等待: {delay_ms}ms")
    await asyncio.sleep(delay_ms / 1000)
    return delay_ms


async def _human_pause_before_add_card(page=None, email_addr: str = "", label: str = "") -> int:
    delay_ms = _billing_before_add_card_delay_ms()
    if delay_ms <= 0:
        return 0
    log.info(f"[{email_addr}] add-card 前拟人停顿: {delay_ms}ms label={label or '-'}")
    try:
        if page is not None:
            await page.wait_for_timeout(delay_ms)
        else:
            await asyncio.sleep(delay_ms / 1000)
    except Exception:
        await asyncio.sleep(delay_ms / 1000)
    return delay_ms


def _billing_humanize_stripe_inputs_enabled() -> bool:
    return _env_bool("BILLING_HUMANIZE_STRIPE_INPUTS", False)


def _billing_stripe_type_delay_ms() -> int:
    return max(0, _env_int("BILLING_STRIPE_TYPE_DELAY_MS", 0))


async def _human_mouse_wiggle(page, label: str = "") -> None:
    if page is None or not _env_bool("BILLING_HUMAN_MOUSE_WIGGLE", False):
        return
    try:
        viewport = page.viewport_size or {"width": 1365, "height": 900}
        width = int(viewport.get("width") or 1365)
        height = int(viewport.get("height") or 900)
        for _ in range(random.randint(2, 5)):
            await page.mouse.move(
                random.randint(max(1, width // 5), max(2, width - width // 5)),
                random.randint(max(1, height // 5), max(2, height - height // 5)),
                steps=random.randint(6, 18),
            )
            await page.wait_for_timeout(random.randint(120, 450))
    except Exception as e:
        log.debug(f"human mouse wiggle skipped: {label} {e}")


def _reserve_billing_attach_slot_sync(email_addr: str = "", label: str = "") -> float:
    if not _billing_attach_rate_limit_enabled():
        return 0.0
    min_interval = _billing_attach_min_interval_seconds()
    jitter = _billing_attach_interval_jitter_seconds()
    if min_interval <= 0 and jitter <= 0:
        return 0.0
    path = _billing_attach_rate_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        file.seek(0)
        raw = file.read().strip()
        try:
            state = json.loads(raw) if raw else {}
        except Exception:
            state = {}
        now = time.time()
        last_at = float(state.get("lastAttachAt", 0) or 0)
        interval = float(min_interval) + (random.uniform(0, float(jitter)) if jitter > 0 else 0.0)
        wait_for = max(0.0, last_at + interval - now)
        if wait_for > 0:
            log.warning(
                "绑卡 add-card 全局节奏限制: "
                f"sleep={wait_for:.0f}s interval={interval:.0f}s label={label or '-'} email={email_addr or '-'}"
            )
            time.sleep(wait_for)
            now = time.time()
        state.update({
            "lastAttachAt": now,
            "lastAttachAtIso": datetime.now(timezone.utc).isoformat(),
            "lastEmail": email_addr,
            "lastLabel": label,
            "minIntervalSeconds": min_interval,
            "jitterSeconds": jitter,
        })
        file.seek(0)
        file.truncate()
        file.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        return wait_for


async def wait_billing_attach_slot(email_addr: str = "", label: str = "") -> None:
    if not _billing_attach_rate_limit_enabled():
        return
    await asyncio.to_thread(_reserve_billing_attach_slot_sync, email_addr, label)


async def _recover_after_billing_attach_decline(reason: str) -> tuple[bool, bool]:
    reason = str(reason or "billing attach decline")[:220]
    _record_billing_attach_risk(reason)
    restarted = await _restart_warp_after_billing_issue(reason)
    recycled = await _recycle_turnstile_solver_pool(reason)
    log.warning(
        "billing attach 400/decline 已按环境风控信号执行恢复: "
        f"warpRestarted={restarted} poolRecycled={recycled} reason={reason[:160]}"
    )
    return restarted, recycled


def _reset_billing_environment_for_retry(client_ctx: dict | None, reason: str = "") -> None:
    """card_declined 在本挑战中按环境/指纹风控处理：重试前切换账单阶段浏览器身份。"""
    if not isinstance(client_ctx, dict):
        return
    # add-card 400/card_declined is treated as an environment/fingerprint hit.
    # Do not carry a just-flagged Dodgeball/browser storage identity into the
    # next card attempt by default; let the next real browser page mint a fresh
    # sourceToken/device token under the recycled WARP/Cloak environment.
    keep_dodgeball = _env_bool("BILLING_RETRY_KEEP_DODGEBALL", False)
    client_ctx.pop("billing_fingerprint_profile", None)
    if keep_dodgeball:
        if not client_ctx.get("device_fingerprint_token"):
            client_ctx["device_fingerprint_token"] = str(uuid.uuid4())
        preserved = bool(client_ctx.get("browser_storage_state") or client_ctx.get("dodgeball_source_token"))
    else:
        client_ctx.pop("browser_storage_state", None)
        client_ctx.pop("dodgeball_source_token", None)
        client_ctx.pop("device_fingerprint_token", None)
        client_ctx["billing_force_new_dodgeball"] = True
        preserved = False
    client_ctx["session_id"] = str(uuid.uuid4())
    client_ctx["verification_id"] = str(uuid.uuid4())
    retry_index = int(client_ctx.get("billing_retry_index", 0) or 0) + 1
    client_ctx["billing_retry_index"] = retry_index
    sequence = _billing_proxy_sequence()
    proxy_label = ""
    if sequence and _env_bool("BILLING_RETRY_ROTATE_BIND_PROXY", True):
        client_ctx["billing_proxy_index"] = retry_index
        proxy_label = _proxy_value_from_text(sequence[retry_index % len(sequence)], "") or "direct"
    if _env_bool("BILLING_RETRY_FORCE_RANDOM_FINGERPRINT", True):
        client_ctx["billing_force_random_profile"] = True
    log.info(
        "billing retry 环境已重置: new profile/session"
        f"; retryIndex={retry_index}; dodgeballPreserved={preserved}; "
        f"nextProxy={proxy_label or 'unchanged'}; "
        f"forceRandomProfile={bool(client_ctx.get('billing_force_random_profile'))}; "
        f"reason={str(reason or '')[:160]}"
    )


async def _cleanup_await(coro, label: str, timeout: float | None = None) -> bool:
    timeout = timeout if timeout is not None else float(os.getenv("BILLING_BROWSER_CLOSE_TIMEOUT", "15"))
    task = asyncio.create_task(coro)
    try:
        if timeout and timeout > 0:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        else:
            await task
        return True
    except asyncio.CancelledError:
        try:
            if timeout and timeout > 0:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            else:
                await task
            return True
        except asyncio.TimeoutError:
            log.warning(f"{label} cleanup after cancellation timed out after {timeout}s")
            task.cancel()
        except Exception as e:
            log.warning(f"{label} cleanup after cancellation failed: {e}")
    except asyncio.TimeoutError:
        log.warning(f"{label} cleanup timed out after {timeout}s")
        task.cancel()
    except Exception as e:
        log.warning(f"{label} cleanup failed: {e}")
    return False


async def _close_context_safely(context, label: str = "browser context") -> bool:
    if not context:
        return True
    try:
        return await _cleanup_await(context.close(), label)
    except Exception as e:
        log.warning(f"{label} close scheduling failed: {e}")
        return False


async def _close_browser_safely(browser, label: str = "browser") -> bool:
    if not browser:
        return True
    try:
        return await _cleanup_await(browser.close(), label, timeout=float(os.getenv("BILLING_BROWSER_CLOSE_TIMEOUT", "15")))
    except Exception as e:
        log.warning(f"{label} close scheduling failed: {e}")
        return False


async def _stop_playwright_safely(playwright, label: str = "playwright") -> bool:
    if not playwright:
        return True
    try:
        return await _cleanup_await(playwright.stop(), label, timeout=float(os.getenv("BILLING_PLAYWRIGHT_STOP_TIMEOUT", "10")))
    except Exception as e:
        log.warning(f"{label} stop scheduling failed: {e}")
        return False


def _sanitize_dodgeball_payload(payload: dict) -> tuple[dict, list[str]]:
    changes: list[str] = []
    if not isinstance(payload, dict):
        return payload, changes
    fingerprints = payload.get("fingerprints")
    if not isinstance(fingerprints, list):
        return payload, changes

    for fp in fingerprints:
        if not isinstance(fp, dict):
            continue
        source = fp.get("source")
        props = fp.get("props")
        if not isinstance(props, dict):
            continue
        if source == "DODGEBALL_FINGERPRINT_SERVICE_V2":
            data = props.get("data")
            if isinstance(data, dict):
                audio = data.get("offlineAudioContext")
                if isinstance(audio, dict) and audio.get("lied"):
                    audio["lied"] = False
                    changes.append("v2.offlineAudioContext.lied=false")
                lies = data.get("lies")
                if isinstance(lies, dict):
                    lie_data = lies.get("data")
                    if isinstance(lie_data, dict) and "AudioBuffer" in lie_data:
                        lie_data.pop("AudioBuffer", None)
                        changes.append("v2.lies.AudioBuffer removed")
                    if isinstance(lie_data, dict):
                        lies["totalLies"] = sum(
                            1 for value in lie_data.values()
                            if value not in (None, False, [], {}, "")
                        )
                        if lies["totalLies"] == 0:
                            lies.pop("$hash", None)
                captured = data.get("capturedErrors")
                if isinstance(captured, dict):
                    captured_data = captured.get("data")
                    if captured_data:
                        captured["data"] = []
                        captured.pop("$hash", None)
                        changes.append("v2.capturedErrors cleared")
                if changes:
                    audio = data.get("offlineAudioContext")
                    if isinstance(audio, dict):
                        audio.pop("$hash", None)
                    fp.pop("hash", None)
        elif source == "DODGEBALL_FINGERPRINT_SERVICE":
            fp_error = str(fp.get("error") or "")
            if fp_error:
                # Older Dodgeball collector attempts strict-mode writes to navigator.platform.
                # The init-script now provides no-op setters, but clear already-produced
                # transient collector errors before sending sourceToken.
                fp.pop("error", None)
                changes.append("v1.error removed")
            if str(props.get("os") or "").lower() == "windows":
                fonts = str(props.get("fonts") or "")
                if "Loma" in fonts:
                    props["fonts"] = "Arial, Calibri, Cambria, Courier New, Georgia, Segoe UI, Times New Roman, Verdana, "
                    changes.append("v1.fonts windows-normalized")
    return payload, changes


async def _route_dodgeball_source_token(route):
    if os.getenv("DODGEBALL_SANITIZE_DISABLED", "0").strip().lower() in ("1", "true", "yes", "on"):
        await route.continue_()
        return
    request = route.request
    try:
        if request.method.upper() != "POST" or "api.dodgeballhq.com/v1/sourceToken" not in request.url:
            await route.continue_()
            return
        raw = request.post_data or ""
        payload = json.loads(raw) if raw else {}
        payload, changes = _sanitize_dodgeball_payload(payload)
        if not changes:
            await route.continue_()
            return
        headers = dict(request.headers)
        headers.pop("content-length", None)
        headers["content-type"] = "application/json"
        await route.continue_(headers=headers, post_data=json.dumps(payload, separators=(",", ":")))
        log.info(f"Dodgeball fingerprint sanitized: {', '.join(changes)}")
    except Exception as e:
        log.warning(f"Dodgeball fingerprint sanitize failed: {e}")
        await route.continue_()


def _billing_add_card_dashboard_version_default() -> str:
    # Full-log comparison: latest successful attaches consistently used daily-2026-06-09-1400
    # while the newer dashboard header daily-2026-06-17-11-28 correlated with add-card 400/card_declined.
    # Keep this as a reversible default; set BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE=live/auto
    # to follow the currently served dashboard or 0/off to disable route rewriting.
    return os.getenv("BILLING_ADD_CARD_DASHBOARD_VERSION_DEFAULT", "daily-2026-06-09-1400").strip()


def _billing_add_card_dashboard_version_override() -> str:
    raw = os.getenv("BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE")
    if raw is None or str(raw).strip() == "":
        raw = _billing_add_card_dashboard_version_default()
    raw = str(raw or "").strip()
    if _env_disabled_text(raw):
        return ""
    if raw.lower() in ("live", "current", "auto"):
        return DASHBOARD_VERSION if str(DASHBOARD_VERSION or "").startswith("daily-") else ""
    return raw


async def _route_add_card_request(route, client_ctx: dict | None = None):
    request = route.request

    def _header_value(headers: dict, name: str) -> str:
        for key, value in (headers or {}).items():
            if str(key).lower() == name.lower():
                return str(value or "")
        return ""

    try:
        if request.method.upper() != "POST" or "/stripe/add-card" not in request.url:
            await route.continue_()
            return
        headers = dict(request.headers)
        original_headers = dict(headers)
        changed = []
        if _billing_mock_add_card_enabled():
            pm_id = _extract_payment_method_id_from_add_card_body(request.post_data or "")
            status, payload = _mock_add_card_response(pm_id)
            if _env_bool("BILLING_ADD_CARD_ROUTE_DEBUG", True):
                _write_billing_debug("add-card-route", {
                    "type": "add-card-route",
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "mocked": True,
                    "url": _safe_url(request.url),
                    "method": request.method,
                    "response": {"status": status, "body": _safe_payload(payload)},
                    "classification": _classify_add_card_failure(status, payload),
                    "headersBefore": _safe_headers(original_headers),
                    "postData": _safe_post_data(request.post_data or ""),
                })
            await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))
            log.warning(f"BILLING_TEST_MODE/mock browser add-card active: status={status} pm={_mask_id(pm_id)}")
            return
        if _env_bool("BILLING_ADD_CARD_STRIP_DEVICE_FINGERPRINT", False):
            for key in list(headers.keys()):
                if key.lower() == "x-device-fingerprint-token":
                    headers.pop(key, None)
                    changed.append("x-device-fingerprint-token removed")
        if _env_bool("BILLING_ADD_CARD_STRIP_REQUEST_ID", False):
            for key in list(headers.keys()):
                if key.lower() == "x-request-id":
                    headers.pop(key, None)
                    changed.append("x-request-id removed")
        dashboard_version_override = _billing_add_card_dashboard_version_override()
        if dashboard_version_override:
            for key in list(headers.keys()):
                if key.lower() == "x-dashboard-version":
                    if headers.get(key) != dashboard_version_override:
                        headers[key] = dashboard_version_override
                        changed.append("x-dashboard-version overridden")
                    break
            else:
                headers["x-dashboard-version"] = dashboard_version_override
                changed.append("x-dashboard-version added")

        if _env_bool("BILLING_ADD_CARD_USE_CTX_DODGEBALL", False):
            ctx = client_ctx if isinstance(client_ctx, dict) else {}
            ctx_token = str(ctx.get("dodgeball_source_token") or ctx.get("device_fingerprint_token") or "").strip()
            if ctx_token and ctx_token != "DISABLED_SOURCE_TOKEN":
                for key in list(headers.keys()):
                    if key.lower() == "x-device-fingerprint-token":
                        if headers.get(key) != ctx_token:
                            headers[key] = ctx_token
                            changed.append("x-device-fingerprint-token ctx-dodgeball")
                        break
                else:
                    headers["x-device-fingerprint-token"] = ctx_token
                    changed.append("x-device-fingerprint-token ctx-dodgeball added")

        device_fingerprint_override = os.getenv("BILLING_ADD_CARD_DEVICE_FINGERPRINT_OVERRIDE")
        if device_fingerprint_override is not None and device_fingerprint_override.strip() != "":
            device_fingerprint_override = device_fingerprint_override.strip()
            if _env_disabled_text(device_fingerprint_override):
                for key in list(headers.keys()):
                    if key.lower() == "x-device-fingerprint-token":
                        headers.pop(key, None)
                        changed.append("x-device-fingerprint-token removed by override")
                        break
            else:
                for key in list(headers.keys()):
                    if key.lower() == "x-device-fingerprint-token":
                        if headers.get(key) != device_fingerprint_override:
                            headers[key] = device_fingerprint_override
                            changed.append("x-device-fingerprint-token overridden")
                        break
                else:
                    headers["x-device-fingerprint-token"] = device_fingerprint_override
                    changed.append("x-device-fingerprint-token added")

        request_id_override = os.getenv("BILLING_ADD_CARD_REQUEST_ID_OVERRIDE")
        if request_id_override is not None and request_id_override.strip() != "":
            request_id_override = request_id_override.strip()
            if _env_disabled_text(request_id_override):
                for key in list(headers.keys()):
                    if key.lower() == "x-request-id":
                        headers.pop(key, None)
                        changed.append("x-request-id removed by override")
                        break
            else:
                for key in list(headers.keys()):
                    if key.lower() == "x-request-id":
                        if headers.get(key) != request_id_override:
                            headers[key] = request_id_override
                            changed.append("x-request-id overridden")
                        break
                else:
                    headers["x-request-id"] = request_id_override
                    changed.append("x-request-id added")

        if _env_bool("BILLING_ADD_CARD_ROUTE_DEBUG", True):
            _write_billing_debug("add-card-route", {
                "type": "add-card-route",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "url": _safe_url(request.url),
                "method": request.method,
                "changed": changed,
                "dashboardVersionBefore": _header_value(original_headers, "x-dashboard-version"),
                "dashboardVersionAfter": _header_value(headers, "x-dashboard-version"),
                "userAgentAfter": _header_value(headers, "user-agent"),
                "deviceFingerprintBefore": _safe_value("x-device-fingerprint-token", _header_value(original_headers, "x-device-fingerprint-token")),
                "deviceFingerprintAfter": _safe_value("x-device-fingerprint-token", _header_value(headers, "x-device-fingerprint-token")),
                "requestIdAfter": _safe_value("x-request-id", _header_value(headers, "x-request-id")),
                "ctxDodgeballSourceTokenSha256": _hash_value((client_ctx or {}).get("dodgeball_source_token", "")) if isinstance(client_ctx, dict) and (client_ctx or {}).get("dodgeball_source_token") else "",
                "ctxDeviceFingerprintTokenSha256": _hash_value((client_ctx or {}).get("device_fingerprint_token", "")) if isinstance(client_ctx, dict) and (client_ctx or {}).get("device_fingerprint_token") else "",
                "headersAfter": _safe_headers(headers),
                "postData": _safe_post_data(request.post_data or ""),
            })

        if changed:
            await route.continue_(headers=headers)
            log.info(f"add-card request headers adjusted: {', '.join(changed)}")
            return
    except Exception as e:
        log.warning(f"add-card request route adjust failed: {e}")
    await route.continue_()


def _make_route_add_card_request(client_ctx: dict | None = None):
    async def _route_add_card_request_with_context(route):
        await _route_add_card_request(route, client_ctx)
    return _route_add_card_request_with_context


def _stripe_pm_billing_details_enabled() -> bool:
    return _env_bool("BILLING_STRIPE_PM_BILLING_DETAILS", _env_bool("STRIPE_PM_BILLING_DETAILS", False))


def _stripe_pm_postal_code() -> str:
    return os.getenv("BILLING_STRIPE_PM_POSTAL_CODE", os.getenv("STRIPE_PM_POSTAL_CODE", "")).strip()


def _stripe_pm_country() -> str:
    return os.getenv("BILLING_STRIPE_PM_COUNTRY", os.getenv("STRIPE_PM_COUNTRY", "")).strip().upper()


def _stripe_pm_user_agent_version() -> str:
    # 近期失败样本从旧 Stripe.js ab68db42e2 切到新版 7c9a63d3d1 后，
    # /v1/payment_methods 额外带上 HUMAN/px3 风控字段，随后 Vapi add-card 连续 card_declined。
    # 默认固定到已成功样本的 payment_user_agent；显式设为 0/off 可关闭。
    raw = os.getenv("BILLING_STRIPE_PM_USER_AGENT_VERSION")
    if raw is None or raw == "":
        raw = os.getenv("STRIPE_PM_USER_AGENT_VERSION")
    if raw is None or raw == "":
        raw = "ab68db42e2"
    raw = str(raw).strip()
    if raw.lower() in ("", "0", "false", "no", "off", "none"):
        return ""
    return raw


def _stripe_pm_bool_from_map(card_number: str, env_name: str) -> bool | None:
    raw = os.getenv(env_name, "")
    card_number = re.sub(r"\D", "", str(card_number or ""))
    if not raw or not card_number:
        return None
    matches: list[tuple[int, bool]] = []
    for entry in re.split(r"[;,\n]+", raw):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        key = re.sub(r"\D", "", key)
        if not key or not card_number.startswith(key):
            continue
        matches.append((len(key), not _env_disabled_text(value)))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def _stripe_pm_strip_human_security_enabled(card_number: str = "") -> bool:
    mapped = _stripe_pm_bool_from_map(card_number, "BILLING_STRIPE_PM_STRIP_HUMAN_SECURITY_MAP")
    if mapped is None:
        mapped = _stripe_pm_bool_from_map(card_number, "STRIPE_PM_STRIP_HUMAN_SECURITY_MAP")
    if mapped is not None:
        return mapped
    return _env_bool("BILLING_STRIPE_PM_STRIP_HUMAN_SECURITY", _env_bool("STRIPE_PM_STRIP_HUMAN_SECURITY", True))


def _stripe_pm_param_should_strip(key: str) -> bool:
    if not _stripe_pm_strip_human_security_enabled():
        return False
    lowered = str(key or "").lower()
    # In the current Cloak/same-browser path, successful add-card samples have
    # the compact historical Stripe PM shape (no passive hcaptcha token and no
    # wallet_config_id). Recent 400/card_declined samples consistently picked up
    # both fields from newer Stripe/Radar passive collection before add-card.
    # Keep these reversible but default-on so the PM request matches the known
    # good shape instead of forwarding high-risk passive telemetry.
    if lowered == "radar_options[hcaptcha_token]" and _env_bool("BILLING_STRIPE_PM_STRIP_HCAPTCHA", True):
        return True
    if lowered == "client_attribution_metadata[wallet_config_id]" and _env_bool("BILLING_STRIPE_PM_STRIP_WALLET_CONFIG", True):
        return True
    if lowered in (
        "radar_options[human_security_enabled]",
        "radar_options[human_security_reason]",
        "radar_options[px3]",
    ):
        return True
    return lowered.startswith("radar_options[human_security_") or lowered.startswith("radar_options[px")


def _stripe_pm_version_from_map(card_number: str = "") -> str:
    raw = os.getenv("BILLING_STRIPE_PM_USER_AGENT_VERSION_MAP", os.getenv("STRIPE_PM_USER_AGENT_VERSION_MAP", ""))
    card_number = re.sub(r"\D", "", str(card_number or ""))
    if not raw or not card_number:
        return ""
    matches: list[tuple[int, str]] = []
    for entry in re.split(r"[;,\n]+", raw):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        key = re.sub(r"\D", "", key)
        value = value.strip()
        if key and value and card_number.startswith(key):
            matches.append((len(key), value))
    if not matches:
        return ""
    matches.sort(reverse=True)
    return matches[0][1]


def _stripe_pm_user_agent_override(card_number: str = "") -> str:
    version = _stripe_pm_version_from_map(card_number) or _stripe_pm_user_agent_version()
    if not version:
        return ""
    if "stripe.js/" in version:
        return version
    return f"stripe.js/{version}; stripe-js-v3/{version}; card-element"


def _make_route_stripe_payment_methods_request(email_addr: str):
    async def _route_stripe_payment_methods_request(route):
        request = route.request
        continued = False
        try:
            if request.method.upper() != "POST" or "api.stripe.com/v1/payment_methods" not in request.url:
                await route.continue_()
                return
            raw = request.post_data or ""
            pairs = parse_qsl(raw, keep_blank_values=True)
            if not pairs:
                await route.continue_()
                return

            changed: list[str] = []
            original_keys = [key for key, _ in pairs]
            card_number_for_route = ""
            for key, value in pairs:
                if key == "card[number]":
                    card_number_for_route = value
                    break
            if _billing_mock_stripe_pm_enabled():
                payload = _mock_stripe_payment_method_payload(email_addr, card_number_for_route)
                if _env_bool("BILLING_STRIPE_PM_ROUTE_DEBUG", True):
                    _write_billing_debug("stripe-pm-route", {
                        "type": "stripe-pm-route",
                        "createdAt": datetime.now(timezone.utc).isoformat(),
                        "mocked": True,
                        "url": _safe_url(request.url),
                        "email": email_addr,
                        "originalKeyCount": len(original_keys),
                        "postData": _safe_post_data(raw),
                        "response": {"status": 200, "body": _safe_payload(payload)},
                    })
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))
                log.warning(f"BILLING_TEST_MODE/mock Stripe payment_methods active: pm={_mask_id(payload.get('id',''))}")
                return
            if _stripe_pm_strip_human_security_enabled(card_number_for_route):
                kept_pairs = []
                stripped = []
                for key, value in pairs:
                    if _stripe_pm_param_should_strip(key):
                        stripped.append(key)
                    else:
                        kept_pairs.append((key, value))
                if stripped:
                    pairs = kept_pairs
                    changed.append("human-security-radar-stripped=" + ",".join(sorted(set(stripped)))[:180])

            def upsert_param(key: str, value: str, *, overwrite: bool = False) -> None:
                nonlocal pairs
                if not value:
                    return
                for idx, (existing_key, existing_value) in enumerate(pairs):
                    if existing_key == key:
                        if overwrite and existing_value != value:
                            pairs[idx] = (key, value)
                            changed.append(f"{key}=overridden")
                        return
                pairs.append((key, value))
                changed.append(f"{key}=added")

            if _stripe_pm_billing_details_enabled():
                upsert_param("billing_details[email]", email_addr)
                upsert_param("billing_details[name]", _billing_name(email_addr))
                postal = _stripe_pm_postal_code()
                country = _stripe_pm_country()
                if postal:
                    upsert_param("billing_details[address][postal_code]", postal)
                if country:
                    upsert_param("billing_details[address][country]", country)

            ua_override = _stripe_pm_user_agent_override(card_number_for_route)
            if ua_override:
                upsert_param("payment_user_agent", ua_override, overwrite=True)
                if _env_bool("BILLING_STRIPE_PM_SYNC_CARD_ELEMENT_SUBTYPE", True) and "card-element" in ua_override:
                    upsert_param("client_attribution_metadata[merchant_integration_subtype]", "card-element", overwrite=True)

            if not changed:
                if _env_bool("BILLING_STRIPE_PM_ROUTE_DEBUG", True):
                    _write_billing_debug("stripe-pm-route", {
                        "type": "stripe-pm-route",
                        "createdAt": datetime.now(timezone.utc).isoformat(),
                        "url": _safe_url(request.url),
                        "email": email_addr,
                        "changed": [],
                        "originalKeyCount": len(original_keys),
                        "finalKeyCount": len(pairs),
                        "paymentUserAgentOverride": bool(ua_override),
                        "billingDetailsEnabled": _stripe_pm_billing_details_enabled(),
                        "postData": _safe_post_data(raw),
                    })
                await route.continue_()
                return

            encoded = urlencode(pairs)
            headers = dict(request.headers)
            headers.pop("content-length", None)
            headers["content-type"] = "application/x-www-form-urlencoded"
            continued = True
            await route.continue_(headers=headers, post_data=encoded)
            log.info(f"Stripe payment_methods request adjusted: {', '.join(changed)}")
            if _env_bool("BILLING_STRIPE_PM_ROUTE_DEBUG", True):
                _write_billing_debug("stripe-pm-route", {
                    "type": "stripe-pm-route",
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "url": _safe_url(request.url),
                    "email": email_addr,
                    "changed": changed,
                    "originalKeyCount": len(original_keys),
                    "finalKeyCount": len(pairs),
                    "paymentUserAgentOverride": bool(ua_override),
                    "billingDetailsEnabled": _stripe_pm_billing_details_enabled(),
                    "postData": _safe_post_data(encoded),
                })
            return

        except Exception as e:
            log.warning(f"Stripe payment_methods route adjust failed: {e}")
            if continued:
                return
        await route.continue_()

    return _route_stripe_payment_methods_request


def _extract_dodgeball_token_from_storage_state(storage_state: dict | None) -> str:
    if not isinstance(storage_state, dict):
        return ""
    for cookie in storage_state.get("cookies") or []:
        try:
            name = str(cookie.get("name") or "")
            if not name.startswith("_db-"):
                continue
            raw_value = unquote(str(cookie.get("value") or ""))
            data = json.loads(raw_value)
            token = str(data.get("token") or "")
            if token:
                return token
        except Exception:
            continue
    for origin in storage_state.get("origins") or []:
        for item in origin.get("localStorage") or []:
            try:
                name = str(item.get("name") or "")
                if not name.startswith("_db-"):
                    continue
                data = json.loads(str(item.get("value") or ""))
                token = str(data.get("token") or "")
                if token:
                    return token
            except Exception:
                continue
    return ""


async def _prepare_dodgeball_identity(
    proxy_url: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
) -> dict:
    """生成真实 Dodgeball sourceToken，并保存浏览器 storage state 供注册/绑卡复用。"""
    if os.getenv("DODGEBALL_IDENTITY_DISABLED", "0").strip().lower() in ("1", "true", "yes", "on"):
        return {}

    engine, browser_driver = _billing_browser_engine()
    playwright = await browser_driver().start()
    browser = None
    context = None
    try:
        browser_fingerprint = _with_billing_profile(browser_fingerprint, None)
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url)
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine, browser_proxy_url))
        context = await browser.new_context(
            proxy=_billing_context_proxy_option(engine, browser_proxy_url),
            user_agent=None if engine == "cloak" and _env_bool("BILLING_CLOAK_NATIVE_UA", True) else (effective_user_agent or None),
            viewport=_billing_profile_from_fingerprint(browser_fingerprint)["viewport"],
            screen=_billing_profile_from_fingerprint(browser_fingerprint)["screen"],
            locale=_billing_locale(),
            timezone_id=_billing_timezone_id(),
            extra_http_headers=_fingerprint_extra_headers(browser_fingerprint),
        )
        await context.route("**/v1/sourceToken**", _route_dodgeball_source_token)
        # 完整 billing UI 会加载 Stripe 安全 iframe；只修补 dashboard 顶层，避免 Stripe iframe 被指纹 patch 影响。
        await context.add_init_script(_billing_top_frame_fingerprint_init_script(browser_fingerprint, user_agent))
        page = await context.new_page()
        await page.goto("https://dashboard.vapi.ai/register?redirect=%2Fsignup", wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_function("() => typeof window.Dodgeball === 'function'", timeout=30000)
        except Exception:
            pass

        token = ""
        if await page.evaluate("() => typeof window.Dodgeball === 'function'"):
            token = await page.evaluate(
                """async ({ publicKey, apiUrl }) => {
                    const db = new window.Dodgeball(publicKey, { apiVersion: 'v1', apiUrl });
                    return await db.getSourceToken();
                }""",
                {"publicKey": DODGEBALL_PUBLIC_KEY, "apiUrl": DODGEBALL_API_URL},
            )
        await page.wait_for_timeout(1000)
        storage_state = await context.storage_state()
        token = str(token or _extract_dodgeball_token_from_storage_state(storage_state) or "")
        if not token or token == "DISABLED_SOURCE_TOKEN":
            log.warning("Dodgeball sourceToken 未取到，继续使用现有流程")
            return {"browser_storage_state": storage_state}
        log.info(f"Dodgeball sourceToken 已生成: {_mask_id(token)}")
        return {
            "device_fingerprint_token": token,
            "dodgeball_source_token": token,
            "browser_storage_state": storage_state,
        }
    except Exception as e:
        log.warning(f"Dodgeball 身份预热失败，继续使用现有流程: {e}")
        return {}
    finally:
        if context:
            await _close_context_safely(context)
        if browser:
            await _close_browser_safely(browser)
        await _stop_playwright_safely(playwright)


async def _read_dodgeball_token_from_page(page) -> str:
    try:
        token = await page.evaluate(
            """() => {
                const readCookieToken = () => {
                    const item = document.cookie.split('; ').find((part) => part.startsWith('_db-'));
                    if (!item) return '';
                    const value = item.split('=').slice(1).join('=');
                    try {
                        return JSON.parse(decodeURIComponent(value)).token || '';
                    } catch (_) {
                        return '';
                    }
                };
                const readStorageToken = () => {
                    try {
                        for (let i = 0; i < localStorage.length; i++) {
                            const key = localStorage.key(i);
                            if (!key || !key.startsWith('_db-')) continue;
                            const value = localStorage.getItem(key);
                            const parsed = JSON.parse(value || '{}');
                            if (parsed.token) return parsed.token;
                        }
                    } catch (_) {}
                    return '';
                };
                return readCookieToken() || readStorageToken();
            }"""
        )
        return str(token or "")
    except Exception:
        return ""


async def _read_turnstile_token_from_page(page) -> str:
    try:
        token = await page.evaluate(
            """() => {
                const values = [];
                if (window.__vapiSignupTurnstileToken) values.push(window.__vapiSignupTurnstileToken);
                if (window.__nexosTurnstileToken) values.push(window.__nexosTurnstileToken);
                for (const input of document.querySelectorAll('input[name="cf-turnstile-response"]')) {
                    if (input.value) values.push(input.value);
                }
                return values.find(value => String(value || '').length > 20) || '';
            }"""
        )
        return str(token or "")
    except Exception:
        return ""


async def _solve_turnstile_token_in_signup_page(page, sitekey: str = SITEKEY, timeout: float | None = None) -> str:
    """优先在当前 signup 页面上下文里产出 Turnstile token，避免 solver token 与浏览器会话/IP 脱钩。"""
    deadline = time.time() + (timeout if timeout is not None else SIGNUP_BROWSER_TURNSTILE_TIMEOUT)
    last_execute = 0.0

    while time.time() < deadline:
        token = await _read_turnstile_token_from_page(page)
        if token:
            return token

        now = time.time()
        if now - last_execute >= 2.0:
            last_execute = now
            try:
                await page.evaluate(
                    """async ({ sitekey }) => {
                        const saveToken = (token) => {
                            if (!token) return;
                            window.__vapiSignupTurnstileToken = token;
                            let inputs = Array.from(document.querySelectorAll('input[name="cf-turnstile-response"]'));
                            if (!inputs.length) {
                                const input = document.createElement('input');
                                input.type = 'hidden';
                                input.name = 'cf-turnstile-response';
                                document.body.appendChild(input);
                                inputs = [input];
                            }
                            for (const input of inputs) {
                                input.value = token;
                                input.dispatchEvent(new Event('input', { bubbles: true }));
                                input.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        };

                        if (!window.turnstile) return { ok: false, reason: 'turnstile-not-loaded' };

                        const options = {
                            sitekey,
                            size: 'invisible',
                            retry: 'auto',
                            'retry-interval': 1000,
                            'refresh-expired': 'auto',
                            'refresh-timeout': 'auto',
                            callback: saveToken,
                            'error-callback': (error) => console.log('signup turnstile error', error),
                            'expired-callback': () => console.log('signup turnstile expired'),
                            'timeout-callback': () => console.log('signup turnstile timeout'),
                        };

                        const candidates = Array.from(document.querySelectorAll('[id^="captcha-"], .cf-turnstile, [data-sitekey]'))
                            .filter((el) => el && el.nodeType === Node.ELEMENT_NODE && el.tagName !== 'SCRIPT');
                        for (const el of candidates) {
                            try {
                                if (typeof window.turnstile.execute === 'function') window.turnstile.execute(el);
                            } catch (_) {}
                        }

                        if (typeof window.turnstile.render === 'function' && !window.__vapiSignupTurnstileWidgetId) {
                            let fallback = document.getElementById('vapi-signup-turnstile-hidden');
                            if (!fallback) {
                                fallback = document.createElement('div');
                                fallback.id = 'vapi-signup-turnstile-hidden';
                                fallback.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;overflow:hidden;z-index:-1;';
                                document.body.appendChild(fallback);
                            }
                            try {
                                window.__vapiSignupTurnstileWidgetId = window.turnstile.render(fallback, options);
                            } catch (_) {}
                        }

                        if (typeof window.turnstile.execute === 'function' && window.__vapiSignupTurnstileWidgetId) {
                            try { window.turnstile.execute(window.__vapiSignupTurnstileWidgetId); } catch (_) {}
                        }
                        if (typeof window.turnstile.getResponse === 'function' && window.__vapiSignupTurnstileWidgetId) {
                            try { saveToken(window.turnstile.getResponse(window.__vapiSignupTurnstileWidgetId)); } catch (_) {}
                        }

                        return { ok: true, candidates: candidates.length };
                    }""",
                    {"sitekey": sitekey},
                )
            except Exception:
                pass

        await page.wait_for_timeout(500)

    return await _read_turnstile_token_from_page(page)


async def _signup_solver_browser_once(
    proxy_url: str,
    email: str,
    password: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> dict:
    """让 Nexos solver 在产出 Turnstile token 的同一浏览器上下文里提交 signup。

    这避免把 solver token 搬到另一个 dashboard 页面后被 Vapi 判定为
    "Invalid CSRF token"。
    """
    if not TURNSTILE_SOLVER_URL or os.getenv("SIGNUP_SOLVER_BROWSER_DISABLED", "0") in ("1", "true", "TRUE", "yes", "YES"):
        raise RuntimeError("signup solver-browser is disabled")

    ctx = client_ctx or {}
    payload = {
        "email": email,
        "password": password,
        "sitekey": SITEKEY,
        "dashboardVersion": DASHBOARD_VERSION,
        "sessionId": ctx.get("session_id", ""),
        "verificationId": ctx.get("verification_id", ""),
    }
    solver_proxy = _turnstile_solver_proxy(proxy_url)
    if solver_proxy:
        payload["proxy"] = solver_proxy
    if solver_proxy != str(proxy_url or "").strip():
        log.info(
            f"[{email}] Turnstile/Cloak solver 代理固定为: "
            f"{'direct' if not solver_proxy else solver_proxy} "
            f"(outer={'direct' if not proxy_url else proxy_url})"
        )

    timeout = httpx.Timeout(30.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        task_id = ""
        last_error = ""
        for _ in range(10):
            try:
                response = await client.post(f"{TURNSTILE_SOLVER_URL}/vapi/signup", json=payload)
                data = response.json()
                if data.get("errorId") == 1:
                    raise RuntimeError(data.get("errorDescription") or str(data))
                task_id = data.get("taskId") or data.get("task_id") or ""
                if task_id:
                    break
                last_error = str(data)
            except Exception as e:
                last_error = str(e)
            await asyncio.sleep(2)

        if not task_id:
            raise RuntimeError(f"signup solver-browser task creation failed: {last_error}")

        deadline = asyncio.get_event_loop().time() + SIGNUP_SOLVER_BROWSER_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            response = await client.get(f"{TURNSTILE_SOLVER_URL}/result", params={"id": task_id})
            data = response.json()
            if data.get("status") == "ready":
                solution = data.get("solution") or {}
                if solution.get("kind") != "vapi_signup":
                    raise RuntimeError(f"signup solver-browser unexpected solution: {solution}")

                device_token = str(solution.get("deviceFingerprintToken") or "")
                if client_ctx is not None and device_token:
                    client_ctx["device_fingerprint_token"] = device_token
                    client_ctx["dodgeball_source_token"] = device_token
                storage_state = solution.get("browserStorageState")
                if client_ctx is not None and isinstance(storage_state, dict):
                    client_ctx["browser_storage_state"] = storage_state
                    storage_token = _extract_dodgeball_token_from_storage_state(storage_state)
                    if storage_token:
                        client_ctx["device_fingerprint_token"] = storage_token
                        client_ctx["dodgeball_source_token"] = storage_token

                fp = {
                    "user_agent": solution.get("userAgent") or user_agent or "",
                    "sec_ch_ua": solution.get("secChUa") or (browser_fingerprint or {}).get("sec_ch_ua", ""),
                    "browser_name": solution.get("browserName") or (browser_fingerprint or {}).get("browser_name", ""),
                    "browser_version": str(solution.get("browserVersion") or (browser_fingerprint or {}).get("browser_version", "")),
                }
                log.info(
                    f"[{email}] solver-browser signup 成功: status={solution.get('statusCode')} "
                    f"csrfLen={solution.get('csrfLength')} fpLen={solution.get('fingerprintLength')}"
                )
                return fp

            if data.get("errorId") == 1 and data.get("errorCode") != "CAPTCHA_NOT_READY":
                raise RuntimeError(f"signup solver-browser failed: {data.get('errorDescription') or data}")

            await asyncio.sleep(TURNSTILE_SOLVER_POLL_INTERVAL)

        raise RuntimeError("signup solver-browser timed out waiting for result")


async def _signup_solver_browser(
    proxy_url: str,
    email: str,
    password: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> dict:
    attempts = max(1, SIGNUP_SOLVER_BROWSER_ATTEMPTS)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _signup_solver_browser_once(
                proxy_url,
                email,
                password,
                user_agent,
                browser_fingerprint,
                client_ctx,
            )
        except Exception as e:
            last_error = e
            error_text = f"{type(e).__name__}: {e}"
            if attempt < attempts and _warp_restartable_solver_error(error_text):
                restarted = await _restart_warp_after_solver_issue(f"signup solver-browser {error_text[:180]}")
                recycled = await _recycle_turnstile_solver_pool(f"signup solver-browser {error_text[:180]}")
                if restarted or recycled:
                    log.warning(
                        f"[{email}] solver-browser signup 超时/求解失败，已执行恢复后重试: "
                        f"warpRestarted={restarted} poolRecycled={recycled} "
                        f"attempt={attempt + 1}/{attempts}"
                    )
                    continue
            raise

    raise RuntimeError(f"signup solver-browser failed: {last_error}")


async def _refresh_browser_dodgeball_state(page, context, client_ctx: dict | None, label: str = "", network_log: dict | None = None) -> str:
    """把当前页面/上下文里的 Dodgeball token 与 storage state 写回 client_ctx，供后续重试复用。"""
    if not isinstance(client_ctx, dict) or context is None:
        return ""
    active_generated = False
    try:
        storage_state = await context.storage_state()
        token = str(await _read_dodgeball_token_from_page(page) or _extract_dodgeball_token_from_storage_state(storage_state) or "")
        force_active = str(label or "") == "before-add-card" and _env_bool("BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD", False)
        if (force_active or not token) and _env_bool("BILLING_REFRESH_DODGEBALL_ACTIVE", True):
            try:
                has_dodgeball = await page.evaluate("() => typeof window.Dodgeball === 'function'")
            except Exception:
                has_dodgeball = False
            if has_dodgeball:
                fresh_token = str(await page.evaluate(
                    """async ({ publicKey, apiUrl }) => {
                        const db = new window.Dodgeball(publicKey, { apiVersion: 'v1', apiUrl });
                        const token = await db.getSourceToken();
                        await new Promise((resolve) => setTimeout(resolve, 500));
                        return token || '';
                    }""",
                    {"publicKey": DODGEBALL_PUBLIC_KEY, "apiUrl": DODGEBALL_API_URL},
                ) or "")
                if fresh_token:
                    token = fresh_token
                    active_generated = True
                storage_state = await context.storage_state()
                if not token:
                    token = str(_extract_dodgeball_token_from_storage_state(storage_state) or "")
        client_ctx["browser_storage_state"] = storage_state
        if token and token != "DISABLED_SOURCE_TOKEN":
            client_ctx["dodgeball_source_token"] = token
            if _env_bool("BILLING_SYNC_DEVICE_FINGERPRINT_WITH_DODGEBALL", False):
                client_ctx["device_fingerprint_token"] = token
        if isinstance(network_log, dict):
            network_log.setdefault("dodgeballRefresh", []).append({
                "label": label,
                "tokenSha256": _hash_value(token) if token else "",
                "activeGenerated": active_generated,
                "storageCookieCount": len((storage_state or {}).get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "storageOriginCount": len((storage_state or {}).get("origins") or []) if isinstance(storage_state, dict) else 0,
                "deviceTokenSha256": _hash_value(client_ctx.get("device_fingerprint_token", "")) if client_ctx.get("device_fingerprint_token") else "",
            })
        if token:
            log.info(f"Dodgeball state refreshed for billing: label={label or '-'} token={_mask_id(token)} active={active_generated}")
        return token
    except Exception as e:
        if isinstance(network_log, dict):
            network_log.setdefault("dodgeballRefresh", []).append({"label": label, "error": str(e)[:300]})
        log.warning(f"Dodgeball state refresh failed: label={label or '-'} error={e}")
        return ""


async def _signup_browser_fetch(
    proxy_url: str,
    email: str,
    password: str,
    csrf_token: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
):
    """在真实 dashboard 页面上下文里发 /auth/signup，保留 Dodgeball cookie/sourceToken 链路。"""
    debug_dir = Path("/data/browser-bind-debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    debug_path = debug_dir / f"signup-browser-fetch-{timestamp}.json"

    engine, browser_driver = _billing_browser_engine()
    playwright = await browser_driver().start()
    browser = None
    context = None
    try:
        browser_fingerprint = _with_billing_profile(browser_fingerprint, client_ctx)
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url, client_ctx)
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine, browser_proxy_url))
        context_kwargs = {
            "proxy": _billing_context_proxy_option(engine, browser_proxy_url),
            "user_agent": None if engine == "cloak" and _env_bool("BILLING_CLOAK_NATIVE_UA", True) else (effective_user_agent or None),
            "viewport": _billing_profile_from_fingerprint(browser_fingerprint)["viewport"],
            "screen": _billing_profile_from_fingerprint(browser_fingerprint)["screen"],
            "locale": _billing_locale(),
            "timezone_id": _billing_timezone_id(),
            "extra_http_headers": _fingerprint_extra_headers(browser_fingerprint),
        }
        storage_state = (client_ctx or {}).get("browser_storage_state")
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**context_kwargs)
        await context.route("**/v1/sourceToken**", _route_dodgeball_source_token)
        await context.route("**/v1/payment_methods**", _make_route_stripe_payment_methods_request(email))
        await context.route("**/stripe/add-card**", _make_route_add_card_request(client_ctx))
        # 完整 billing UI 会加载 Stripe 安全 iframe；只修补 dashboard 顶层，避免 Stripe iframe 被指纹 patch 影响。
        await context.add_init_script(_billing_top_frame_fingerprint_init_script(browser_fingerprint, user_agent))
        page = await context.new_page()
        await page.goto("https://dashboard.vapi.ai/register?redirect=%2Fsignup", wait_until="domcontentloaded", timeout=60000)

        token = ""
        deadline = time.time() + 20
        while time.time() < deadline:
            token = await _read_dodgeball_token_from_page(page)
            if token:
                break
            await page.wait_for_timeout(500)

        if not token and await page.evaluate("() => typeof window.Dodgeball === 'function'"):
            token = await page.evaluate(
                """async ({ publicKey, apiUrl }) => {
                    const db = new window.Dodgeball(publicKey, { apiVersion: 'v1', apiUrl });
                    const token = await db.getSourceToken();
                    await new Promise((resolve) => setTimeout(resolve, 500));
                    return token;
                }""",
                {"publicKey": DODGEBALL_PUBLIC_KEY, "apiUrl": DODGEBALL_API_URL},
            )

        if token and client_ctx is not None:
            client_ctx["device_fingerprint_token"] = token
            client_ctx["dodgeball_source_token"] = token

        page_csrf_token = await _solve_turnstile_token_in_signup_page(page, SITEKEY)
        signup_csrf_token = page_csrf_token or csrf_token
        csrf_source = "page" if page_csrf_token else "solver"
        if csrf_source == "solver":
            log.warning(f"[{email}] signup 页面未产出 Turnstile token，回退 solver token")

        result = await page.evaluate(
            """async ({ email, password, csrfToken, fingerprintToken, sessionId, verificationId, dashboardVersion, requestId }) => {
                const headers = {
                    'accept': 'application/json',
                    'content-type': 'application/json',
                    'x-csrf-token': csrfToken || '',
                    'x-client-source': 'dashboard',
                    'x-client-platform': 'web',
                    'x-dashboard-version': dashboardVersion,
                    'x-request-id': requestId,
                };
                if (fingerprintToken) headers['x-device-fingerprint-token'] = fingerprintToken;
                if (sessionId) headers['x-session-id'] = sessionId;
                if (verificationId) headers['x-verification-id'] = verificationId;
                const response = await fetch('https://api.vapi.ai/auth/signup', {
                    method: 'POST',
                    mode: 'cors',
                    credentials: 'include',
                    headers,
                    body: JSON.stringify({
                        email,
                        password,
                        emailRedirectTo: 'https://dashboard.vapi.ai/',
                    }),
                });
                const text = await response.text();
                return {
                    ok: response.ok,
                    status: response.status,
                    statusText: response.statusText,
                    body: text,
                    csrfTokenUsed: csrfToken || '',
                    fingerprintTokenUsed: fingerprintToken || '',
                };
            }""",
            {
                "email": email,
                "password": password,
                "csrfToken": signup_csrf_token,
                "fingerprintToken": token,
                "sessionId": (client_ctx or {}).get("session_id", ""),
                "verificationId": (client_ctx or {}).get("verification_id", ""),
                "dashboardVersion": DASHBOARD_VERSION,
                "requestId": _request_id(),
            },
        )
        storage_state = await context.storage_state()
        token_after = await _read_dodgeball_token_from_page(page) or _extract_dodgeball_token_from_storage_state(storage_state) or token
        if client_ctx is not None:
            client_ctx["browser_storage_state"] = storage_state
            if token_after:
                client_ctx["device_fingerprint_token"] = token_after
                client_ctx["dodgeball_source_token"] = token_after

        debug = {
            "email": email,
            "status": result.get("status") if isinstance(result, dict) else None,
            "ok": result.get("ok") if isinstance(result, dict) else None,
            "body": _safe_post_data((result or {}).get("body", "") if isinstance(result, dict) else str(result)),
            "csrfTokenSource": csrf_source,
            "csrfTokenSha256": _hash_value(signup_csrf_token),
            "pageCsrfTokenSha256": _hash_value(page_csrf_token),
            "solverCsrfTokenSha256": _hash_value(csrf_token),
            "fingerprintTokenUsedSha256": _hash_value(token),
            "fingerprintTokenAfterSha256": _hash_value(token_after),
            "storageTokenSha256": _hash_value(_extract_dodgeball_token_from_storage_state(storage_state)),
            "engine": engine,
            "headless": _billing_browser_headless(),
            "proxy": "direct" if not browser_proxy_url else browser_proxy_url,
            "billingFingerprintProfile": _billing_profile_summary(browser_fingerprint),
        }
        debug_path.write_text(json.dumps(debug, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log.info(
            f"[{email}] 浏览器 signup-fetch: status={debug['status']} "
            f"csrf={csrf_source}:{_mask_id(signup_csrf_token)} fp={_mask_id(token_after or token)} debug={debug_path}"
        )

        if not isinstance(result, dict) or not result.get("ok"):
            status = result.get("status") if isinstance(result, dict) else "unknown"
            body = result.get("body", "") if isinstance(result, dict) else str(result)
            raise RuntimeError(f"browser signup-fetch {status}: {body[:240]}")
    finally:
        if context:
            await _close_context_safely(context)
        if browser:
            await _close_browser_safely(browser)
        await _stop_playwright_safely(playwright)


def _browser_executable_path() -> str | None:
    candidates = [
        os.getenv("BILLING_BROWSER_EXECUTABLE", ""),
        config.CHROME_PATH,
        "/ms-playwright/chromium-1223/chrome-linux64/chrome",
        "/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


async def _is_visible(locator, timeout: int = 0) -> bool:
    try:
        return await locator.is_visible(timeout=timeout)
    except Exception:
        return False


async def _goto_with_abort_tolerance(page, url: str, *, wait_until: str = "domcontentloaded", timeout: int = 60000, label: str = "navigation"):
    """Playwright may raise net::ERR_ABORTED when the SPA redirects during goto.

    The dashboard often handles deep links by aborting the document navigation and
    replacing it with a client-side route. Treat that as a soft navigation only
    for billing/dashboard pages; subsequent readiness checks decide whether the
    page is actually usable.
    """
    try:
        return await page.goto(url, wait_until=wait_until, timeout=timeout)
    except Exception as e:
        text = str(e)
        if (
            "net::err_aborted" not in text.lower()
            or not _env_bool("BILLING_TOLERATE_GOTO_ABORTED", True)
            or not ("dashboard.vapi.ai" in str(url or ""))
        ):
            raise
        log.warning(f"{label} Page.goto net::ERR_ABORTED tolerated: url={url} current={getattr(page, 'url', '')}")
        wait_ms = max(1000, min(int(timeout or 15000), _env_int("BILLING_GOTO_ABORT_WAIT_MS", 15000)))
        try:
            await page.wait_for_load_state(wait_until, timeout=wait_ms)
        except Exception:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=wait_ms)
            except Exception:
                pass
        await page.wait_for_timeout(max(0, _env_int("BILLING_GOTO_ABORT_SETTLE_MS", 1500)))
        return None


async def _browser_fingerprint_snapshot(page) -> dict:
    try:
        return await page.evaluate(
            """() => {
                const out = {
                    userAgent: navigator.userAgent || '',
                    webdriver: navigator.webdriver,
                    platform: navigator.platform || '',
                    language: navigator.language || '',
                    languages: Array.from(navigator.languages || []),
                    hardwareConcurrency: navigator.hardwareConcurrency,
                    deviceMemory: navigator.deviceMemory,
                    maxTouchPoints: navigator.maxTouchPoints,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
                    screen: {
                        width: screen.width,
                        height: screen.height,
                        availWidth: screen.availWidth,
                        availHeight: screen.availHeight,
                        colorDepth: screen.colorDepth,
                    },
                    viewport: {
                        innerWidth,
                        innerHeight,
                        outerWidth,
                        outerHeight,
                        devicePixelRatio,
                    },
                    chromeRuntime: !!(window.chrome && window.chrome.runtime),
                    pluginsLength: navigator.plugins ? navigator.plugins.length : null,
                    mimeTypesLength: navigator.mimeTypes ? navigator.mimeTypes.length : null,
                };
                try {
                    out.userAgentData = {
                        platform: navigator.userAgentData && navigator.userAgentData.platform,
                        mobile: navigator.userAgentData && navigator.userAgentData.mobile,
                        brands: navigator.userAgentData && navigator.userAgentData.brands,
                    };
                } catch (_) {}
                try {
                    const canvas = document.createElement('canvas');
                    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
                    const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
                    out.webgl = dbg ? {
                        vendor: gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL),
                        renderer: gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL),
                    } : {};
                } catch (e) {
                    out.webglError = String(e);
                }
                return out;
            }"""
        )
    except Exception as e:
        return {"error": str(e)}



async def _billing_page_diagnostics(page) -> dict:
    """抓取 billing fallback 的当前页面状态，敏感字段只落 hash/长度。"""
    try:
        raw = await page.evaluate(
            """() => {
                const dumpStorage = (storage) => {
                    const out = {};
                    try {
                        for (let i = 0; i < storage.length; i++) {
                            const key = storage.key(i);
                            out[key] = storage.getItem(key);
                        }
                    } catch (e) {
                        out.__error = String(e);
                    }
                    return out;
                };
                const visible = (el) => {
                    try {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return !!(rect.width || rect.height) && style.visibility !== 'hidden' && style.display !== 'none';
                    } catch (_) { return false; }
                };
                return {
                    url: location.href,
                    title: document.title || '',
                    readyState: document.readyState || '',
                    bodyText: ((document.body && document.body.innerText) || '').slice(0, 2000),
                    headings: Array.from(document.querySelectorAll('h1,h2,h3,h4')).slice(0, 30).map((el) => ({
                        tag: el.tagName,
                        text: (el.innerText || '').trim().slice(0, 160),
                        visible: visible(el),
                    })).filter((item) => item.text),
                    buttons: Array.from(document.querySelectorAll('button')).slice(0, 60).map((el) => ({
                        text: (el.innerText || el.textContent || '').trim().slice(0, 120),
                        type: el.getAttribute('type') || '',
                        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                        visible: visible(el),
                    })),
                    inputs: Array.from(document.querySelectorAll('input,textarea,select')).slice(0, 60).map((el) => ({
                        tag: el.tagName,
                        type: el.getAttribute('type') || '',
                        name: el.getAttribute('name') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        autocomplete: el.getAttribute('autocomplete') || '',
                        valueLength: (el.value || '').length,
                        visible: visible(el),
                    })),
                    iframes: Array.from(document.querySelectorAll('iframe')).slice(0, 80).map((el) => ({
                        title: el.getAttribute('title') || '',
                        src: el.getAttribute('src') || '',
                        visible: visible(el),
                    })),
                    localStorage: dumpStorage(window.localStorage),
                    sessionStorage: dumpStorage(window.sessionStorage),
                };
            }"""
        )
    except Exception as e:
        raw = {"error": str(e)[:500]}

    if not isinstance(raw, dict):
        raw = {"value": str(raw)[:500]}
    for storage_key in ("localStorage", "sessionStorage"):
        storage = raw.get(storage_key)
        if isinstance(storage, dict):
            raw[storage_key] = {key: _safe_value(key, value) for key, value in sorted(storage.items())}
    if isinstance(raw.get("iframes"), list):
        for frame in raw["iframes"]:
            if isinstance(frame, dict) and frame.get("src"):
                frame["src"] = _safe_url(frame.get("src") or "")
    raw["fingerprint"] = await _browser_fingerprint_snapshot(page)
    return raw


async def _store_dashboard_session(page, keys: dict, email_addr: str):
    now = int(time.time())
    payload = _decode_jwt_payload(keys.get("supabase_token", ""))
    expires_at = int(keys.get("supabase_expires_at") or payload.get("exp") or (now + 3600))
    user_id = payload.get("sub") or ""
    user_email = payload.get("email") or email_addr
    supabase_auth = {
        "access_token": keys.get("supabase_token", ""),
        "refresh_token": keys.get("supabase_refresh_token", ""),
        "token_type": "bearer",
        "expires_in": max(1, expires_at - now),
        "expires_at": expires_at,
        "user": {
            "id": user_id,
            "aud": payload.get("aud") or "authenticated",
            "role": payload.get("role") or "authenticated",
            "email": user_email,
            "email_confirmed_at": datetime.now(timezone.utc).isoformat(),
            "user_metadata": payload.get("user_metadata") or {"email": user_email},
            "app_metadata": payload.get("app_metadata") or {},
        },
    }
    await page.evaluate(
        """(session) => {
            localStorage.setItem('vapi-supabase-auth', JSON.stringify(session.supabaseAuth));
            localStorage.setItem('ORG_TOKEN', session.orgToken);
            localStorage.setItem('USER_TOKEN', session.userToken);
            localStorage.setItem('SELECTED_ORG', session.orgId);
            localStorage.setItem('AUTH_PROVIDER', 'supabase');
            sessionStorage.setItem('SELECTED_ORG', session.orgId);
            try { window.dispatchEvent(new Event('USER_TOKEN_CHANGED')); } catch (_) {}
            try { window.dispatchEvent(new Event('ORG_TOKEN_CHANGED')); } catch (_) {}
        }""",
        {
            "supabaseAuth": supabase_auth,
            "orgToken": keys.get("org_token", ""),
            "userToken": keys.get("user_token", ""),
            "orgId": keys.get("org_id", ""),
        },
    )


async def _welcome_onboarding_dialog(page, timeout: int = 1500):
    title = re.compile(r"Welcome\s+to\s+Vapi", re.I)
    candidates = [
        page.locator('[role="dialog"][data-state="open"]').filter(has_text=title).first,
        page.locator('[role="dialog"]').filter(has_text=title).first,
        page.get_by_text(title).first.locator('xpath=ancestor::*[@role="dialog"][1]'),
    ]
    for locator in candidates:
        if await _is_visible(locator, timeout=timeout):
            return locator
    return None


async def _complete_welcome_onboarding_if_present(page, email_addr: str, timeout: int = 1500):
    dialog = await _welcome_onboarding_dialog(page, timeout=timeout)
    if not dialog:
        return False

    name_input = dialog.locator('input[name="name"]').first
    if await _is_visible(name_input, timeout=3000):
        name = (email_addr.split("@", 1)[0] or "Vapi User").replace(".", " ").replace("_", " ")[:60]
        try:
            await name_input.click(timeout=3000, force=True)
            await name_input.fill("", timeout=3000)
            await name_input.type(name, delay=25, timeout=8000)
        except Exception:
            await name_input.fill(name, timeout=5000)

    other_input = dialog.locator('input[name="otherSource"]').first
    if await _is_visible(other_input, timeout=1000):
        await other_input.fill("")
        await other_input.type("Dashboard", delay=25)

    for button_name in ("Google", "Personal"):
        button = dialog.get_by_role("radio", name=re.compile(f"^{re.escape(button_name)}", re.I)).first
        if not await _is_visible(button, timeout=800):
            button = dialog.get_by_role("button", name=re.compile(f"^{re.escape(button_name)}", re.I)).first
        if await _is_visible(button, timeout=1000):
            await button.click(force=True)
            await page.wait_for_timeout(500)

    start = dialog.get_by_role("button", name=re.compile("Start Building", re.I)).first
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if await start.is_enabled():
                await start.click(force=True)
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
    raise RuntimeError("Welcome onboarding Start Building stayed disabled")


async def _ensure_billing_ready(page, email_addr: str, keys: dict | None = None):
    deadline = time.time() + 120
    login_re = re.compile(r"\b(log\s*in|login|sign\s*in|sign\s*up)\b", re.I)
    ready_locators = [
        page.get_by_text(re.compile(r"Payment\s+Method", re.I)).first,
        page.get_by_text(re.compile(r"Billing", re.I)).first,
        page.locator('input[name="stripeCustomerEmail"]').first,
        page.locator('iframe[title*="Secure"], iframe[src*="stripe.com"]').first,
    ]
    while time.time() < deadline:
        dialog = await _welcome_onboarding_dialog(page, timeout=500)
        if dialog:
            await _complete_welcome_onboarding_if_present(page, email_addr, timeout=500)
            await page.wait_for_timeout(1000)
            continue

        for locator in ready_locators:
            if await _is_visible(locator, timeout=500):
                return

        body_text = ""
        try:
            body_text = (await page.locator("body").inner_text(timeout=1000))[:800]
        except Exception:
            pass
        if keys and ("/login" in page.url or "/register" in page.url or login_re.search(body_text or "")):
            log.warning(f"billing fallback 会话未生效，重新注入 dashboard session: url={page.url}")
            await _store_dashboard_session(page, keys, email_addr)
            await _goto_with_abort_tolerance(page, BILLING_URL, wait_until="domcontentloaded", timeout=60000, label="billing page")
            await page.wait_for_timeout(1000)
            continue

        if "/settings/billing" not in page.url:
            await _goto_with_abort_tolerance(page, BILLING_URL, wait_until="domcontentloaded", timeout=60000, label="billing page")
        await page.wait_for_timeout(1000)

    state = await _billing_page_diagnostics(page)
    summary = {
        "url": state.get("url"),
        "title": state.get("title"),
        "headings": state.get("headings", [])[:8],
        "bodyText": str(state.get("bodyText") or "")[:500],
        "localStorageKeys": list((state.get("localStorage") or {}).keys()),
        "sessionStorageKeys": list((state.get("sessionStorage") or {}).keys()),
    }
    raise RuntimeError(f"Billing page not ready after 120s: {json.dumps(summary, ensure_ascii=False)[:1400]}")


async def _open_payment_method_editor_if_needed(page):
    iframe = page.locator('iframe[title*="Secure"], iframe[src*="stripe.com"]').first
    if await _is_visible(iframe):
        return
    row = page.locator('xpath=(//p[normalize-space()="Payment Method"]/following-sibling::div[contains(@class,"flex")][1])[last()]').first
    if not await _is_visible(row):
        return
    edit = row.locator("button").last
    if await _is_visible(edit):
        await edit.click(force=True)
        await page.wait_for_timeout(1500)


async def _update_billing_email_if_needed(page, email_addr: str):
    email_input = page.locator('input[name="stripeCustomerEmail"]').first
    if not await _is_visible(email_input, timeout=5000):
        return
    current = (await email_input.input_value()).strip().lower()
    if current == email_addr.strip().lower():
        return
    await email_input.fill(email_addr)
    card = page.locator('input[name="stripeCustomerEmail"]').locator('xpath=ancestor::div[contains(@class,"space-y-4")][1]')
    save = card.locator("button").nth(0)
    try:
        async with page.expect_response(
            lambda response: "/subscription/" in response.url and response.request.method == "PATCH",
            timeout=30000,
        ) as response_info:
            await save.click()
        response = await response_info.value
        if not response.ok:
            body = await response.text()
            raise RuntimeError(f"billing email update {response.status}: {body[:240]}")
    except Exception as e:
        log.warning(f"浏览器 fallback 更新账单邮箱失败，继续尝试绑卡: {e}")


async def _stripe_input_state(page, expected_card: dict | None = None) -> dict:
    """Return Stripe iframe fill state without persisting raw PAN/CVC."""
    expected: dict[str, str] = {}
    if isinstance(expected_card, dict):
        expected = {
            "cardnumber": re.sub(r"\D", "", str(expected_card.get("number") or "")),
            "exp-date": f"{str(expected_card.get('exp_month') or '').zfill(2)}{str(expected_card.get('exp_year') or '')[-2:]}",
            "cvc": re.sub(r"\D", "", str(expected_card.get("cvc") or "")),
        }
    state: dict = {"frames": []}
    try:
        state["host"] = await page.evaluate(
            """() => {
                const visible = (el) => {
                    try {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return !!(rect.width || rect.height) && style.visibility !== 'hidden' && style.display !== 'none';
                    } catch (_) { return false; }
                };
                return {
                    stripeElementClasses: Array.from(document.querySelectorAll('.StripeElement')).map((el) => ({
                        className: el.className || '',
                        visible: visible(el),
                        text: (el.innerText || '').trim().slice(0, 120),
                    })).slice(0, 8),
                    privateStripeInputCount: document.querySelectorAll('.__PrivateStripeElement-input').length,
                    visibleStripeIframeCount: Array.from(document.querySelectorAll('iframe[src*="stripe.com"]')).filter(visible).length,
                };
            }"""
        )
    except Exception as e:
        state["hostError"] = str(e)[:240]

    for frame in page.frames:
        try:
            if "stripe.com" not in str(frame.url or ""):
                continue
            inputs = await frame.locator("input").evaluate_all(
                r"""(els) => els.map((el) => {
                    const value = el.value || '';
                    return {
                        name: el.getAttribute('name') || '',
                        type: el.getAttribute('type') || '',
                        autocomplete: el.getAttribute('autocomplete') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        ariaInvalid: el.getAttribute('aria-invalid') || '',
                        disabled: !!el.disabled,
                        valueLength: value.length,
                        digitLength: (value.replace(/\D/g, '') || '').length,
                    };
                }).slice(0, 20)"""
            )
            safe_inputs = []
            for item in inputs or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                expected_digits = expected.get(name, "")
                if expected_digits:
                    item["expectedDigitLength"] = len(expected_digits)
                    item["matchesExpectedLength"] = int(item.get("digitLength") or 0) == len(expected_digits)
                safe_inputs.append(item)
            state["frames"].append({
                "url": _safe_url(frame.url or ""),
                "inputCount": len(inputs or []),
                "inputs": safe_inputs,
            })
        except Exception as e:
            state.setdefault("frameErrors", []).append(str(e)[:240])
    return state


def _stripe_card_inputs_complete_from_state(state: dict | None) -> bool:
    if not isinstance(state, dict):
        return False
    for frame in state.get("frames") or []:
        if not isinstance(frame, dict):
            continue
        inputs = frame.get("inputs") or []
        by_name = {str(item.get("name") or ""): item for item in inputs if isinstance(item, dict)}
        required = [by_name.get("cardnumber"), by_name.get("exp-date"), by_name.get("cvc")]
        if not all(isinstance(item, dict) for item in required):
            continue
        if all(bool(item.get("matchesExpectedLength")) for item in required):
            # aria-invalid may be absent during transitions; when present it must not remain true.
            if all(str(item.get("ariaInvalid") or "").lower() != "true" for item in required):
                return True
    return False


async def _wait_stripe_card_inputs_complete(page, expected_card: dict, timeout_ms: int = 9000) -> dict:
    deadline = time.time() + (max(1000, timeout_ms) / 1000)
    last_state: dict = {}
    while time.time() < deadline:
        last_state = await _stripe_input_state(page, expected_card)
        if _stripe_card_inputs_complete_from_state(last_state):
            return last_state
        await page.wait_for_timeout(300)
    return last_state


async def _find_billing_payment_save_button(page) -> tuple[object | None, dict]:
    """Find the actual card-save check button near Stripe Elements, not side/nav buttons."""
    info: dict = {}
    try:
        info = await page.evaluate(
            r"""() => {
                const isVisible = (el) => {
                    try {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return !!(r.width && r.height) && s.visibility !== 'hidden' && s.display !== 'none';
                    } catch (_) { return false; }
                };
                const textOf = (el) => ((el.innerText || el.textContent || el.getAttribute('aria-label') || el.title || '') + '').trim();
                const buttons = Array.from(document.querySelectorAll('button'));
                const iframe = Array.from(document.querySelectorAll('iframe')).find((el) => {
                    const title = el.getAttribute('title') || '';
                    const src = el.getAttribute('src') || '';
                    return isVisible(el) && (/Secure/i.test(title) || /elements-inner-card|stripe\.com\/v3/i.test(src));
                });
                if (!iframe) {
                    return { selectedIndex: -1, reason: 'stripe iframe not found', candidates: [] };
                }
                const ir = iframe.getBoundingClientRect();
                let card = null;
                for (let el = iframe.parentElement, depth = 0; el && depth < 14; el = el.parentElement, depth++) {
                    const text = (el.innerText || '').slice(0, 2000);
                    if (/Payment\s+Method/i.test(text) && el.querySelector('input[name="stripeCustomerEmail"]') && el.querySelector('button')) {
                        card = el;
                        break;
                    }
                }
                if (!card) {
                    for (let el = iframe.parentElement, depth = 0; el && depth < 8; el = el.parentElement, depth++) {
                        if (el.querySelectorAll && el.querySelectorAll('button').length && (el.innerText || '').match(/Payment\s+Method/i)) {
                            card = el;
                            break;
                        }
                    }
                }
                const candidates = buttons.map((button, index) => {
                    const r = button.getBoundingClientRect();
                    const text = textOf(button);
                    const html = (button.outerHTML || '').replace(/\s+/g, ' ').slice(0, 260);
                    const disabled = !!button.disabled || button.getAttribute('aria-disabled') === 'true';
                    const role = button.getAttribute('role') || '';
                    const isSwitch = role === 'switch' || button.hasAttribute('aria-checked') || /auto-reload-switch|role="switch"|data-tracks="Switch"/i.test(html);
                    const visible = isVisible(button);
                    const centerX = r.left + r.width / 2;
                    const centerY = r.top + r.height / 2;
                    const sameCard = !!(card && card.contains(button));
                    const nearY = centerY >= ir.top - 80 && centerY <= ir.bottom + 120;
                    const nearX = centerX >= ir.left - 40 && centerX <= ir.right + 180;
                    let score = Math.abs(centerY - (ir.top + ir.height / 2)) + Math.max(0, Math.abs(centerX - (ir.right + 22)) / 4);
                    if (sameCard) score -= 350;
                    if (nearY && nearX) score -= 300;
                    if (/save|update|add|payment|card/i.test(text + ' ' + html)) score -= 180;
                    if (/check|M229\.66|M213\.66|path/i.test(html) && nearY && nearX) score -= 80;
                    if (isSwitch) score += 2500;
                    if (/buy|coupon|sales|download|search|daily|weekly|statement|org/i.test(text)) score += 1000;
                    if (!visible || disabled) score += 5000;
                    if (r.width > 140 || r.height > 70) score += 150;
                    return {
                        index, text: text.slice(0, 120), role, isSwitch, disabled, visible, sameCard, nearY, nearX, score,
                        bbox: { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) },
                        html,
                    };
                }).filter((item) => item.visible && !item.disabled && !item.isSwitch && (item.sameCard || (item.nearY && item.nearX)))
                  .sort((a, b) => a.score - b.score);
                return {
                    selectedIndex: candidates.length ? candidates[0].index : -1,
                    iframeBox: { x: Math.round(ir.x), y: Math.round(ir.y), width: Math.round(ir.width), height: Math.round(ir.height) },
                    candidates: candidates.slice(0, 8),
                };
            }"""
        )
    except Exception as e:
        info = {"error": str(e)[:300], "selectedIndex": -1, "candidates": []}

    selected = int((info or {}).get("selectedIndex", -1) or -1)
    if selected >= 0:
        return page.locator("button").nth(selected), info

    # Compatibility fallback: old DOM had email edit as button 0 and card check as button 1 inside the payment card.
    stripe_iframe = page.locator('iframe[title*="Secure"], iframe[src*="stripe.com"]').first
    payment_card = stripe_iframe.locator('xpath=ancestor::div[contains(@class,"space-y-4")][1]')
    save_button = payment_card.locator("button").nth(1)
    if not await _is_visible(save_button, timeout=1500):
        save_button = payment_card.locator("button").last
    info.setdefault("fallback", "ancestor-space-y-4")
    return save_button, info


async def _click_billing_save_and_wait_add_card(page, button_info: dict | None = None) -> tuple[object, list[dict]]:
    """Click likely card-save buttons and wait for the real add-card response.

    A wrong icon click currently looks like a 120s hang because no Stripe PM nor
    Vapi add-card request is emitted. This helper tries only near-Stripe
    candidates and records whether each click triggered Stripe/Vapi traffic.
    """
    queue: asyncio.Queue = asyncio.Queue()

    def _handler(response):
        try:
            url = response.url or ""
            if (
                ("api.stripe.com/v1/payment_methods" in url or "/stripe/add-card" in url)
                and response.request.method == "POST"
            ):
                queue.put_nowait(response)
        except Exception:
            pass

    candidates = []
    seen = set()
    for item in (button_info or {}).get("candidates") or []:
        try:
            idx = int(item.get("index"))
        except Exception:
            continue
        if idx < 0 or idx in seen:
            continue
        seen.add(idx)
        candidates.append(item)
    selected = int((button_info or {}).get("selectedIndex", -1) or -1)
    if selected >= 0 and selected not in seen:
        candidates.insert(0, {"index": selected, "source": "selectedIndex"})
    if not candidates:
        candidates = [{"index": -1, "source": "fallback-locator"}]

    no_request_timeout = max(3000, _env_int("BILLING_SAVE_NO_REQUEST_TIMEOUT_MS", 18000))
    add_card_timeout = max(no_request_timeout, _env_int("BILLING_SAVE_ADD_CARD_TIMEOUT_MS", 120000))
    attempts: list[dict] = []
    page.on("response", _handler)
    try:
        for ordinal, candidate in enumerate(candidates[: max(1, _env_int("BILLING_SAVE_BUTTON_CANDIDATES", 4))], start=1):
            index = int(candidate.get("index", -1) or -1)
            attempt = {
                "ordinal": ordinal,
                "buttonIndex": index,
                "candidate": candidate,
                "events": [],
            }
            attempts.append(attempt)
            try:
                locator = page.locator("button").nth(index) if index >= 0 else None
                if locator is None or not await _is_visible(locator, timeout=1000):
                    attempt["clickError"] = "button not visible"
                    continue
                await locator.scroll_into_view_if_needed(timeout=5000)
                await locator.click(timeout=8000)
                attempt["clicked"] = True
            except Exception as e:
                attempt["clickError"] = str(e)[:300]
                continue

            saw_pm = False
            deadline = time.time() + (no_request_timeout / 1000)
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    response = await asyncio.wait_for(queue.get(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    continue
                url = response.url or ""
                if "api.stripe.com/v1/payment_methods" in url:
                    saw_pm = True
                    attempt["events"].append({"kind": "stripe-payment-methods", "status": response.status})
                    # Once Stripe PM was emitted the same submit flow owns the outcome;
                    # wait longer for Vapi add-card instead of clicking another button.
                    deadline = time.time() + (add_card_timeout / 1000)
                    continue
                if "/stripe/add-card" in url:
                    attempt["events"].append({"kind": "vapi-add-card", "status": response.status})
                    return response, attempts
            attempt["timedOutAfterMs"] = add_card_timeout if saw_pm else no_request_timeout
            attempt["sawStripePaymentMethod"] = saw_pm
            if saw_pm:
                break
    finally:
        try:
            page.remove_listener("response", _handler)
        except Exception:
            pass
    raise RuntimeError(f"billing save click did not produce add-card response: {json.dumps(attempts, ensure_ascii=False)[:1200]}")


async def _bind_card_browser(
    proxy_url: str,
    keys: dict,
    email_addr: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
    card_override: dict | None = None,
) -> str:
    card = _billing_card_for_browser(card_override)
    log.info(f"[{email_addr}] 启动浏览器绑卡: ****{card['number'][-4:]} exp={card['expiry']}")
    debug_dir = Path("/data/browser-bind-debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    network_path = debug_dir / f"network-{timestamp}.json"
    network_log = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "email": email_addr,
        "cardTail": card["number"][-4:],
        "card": _safe_card_descriptor(card_override or config.billing_card()),
        "dodgeballSourceTokenSha256": _hash_value((client_ctx or {}).get("dodgeball_source_token", "")) if (client_ctx or {}).get("dodgeball_source_token") else "",
        "events": [],
    }

    async def write_network_log():
        network_log["finishedAt"] = datetime.now(timezone.utc).isoformat()
        network_log["eventCount"] = len(network_log["events"])
        network_log["antiFraudTimeline"] = _billing_network_anti_fraud_summary(network_log)
        network_path.write_text(json.dumps(network_log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async def record_request(request):
        try:
            url = request.url
            if not _network_interesting(url):
                return
            post_data = request.post_data or ""
            network_log["events"].append({
                "type": "request",
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": request.method,
                "url": _safe_url(url),
                "resourceType": request.resource_type,
                "headers": _safe_headers(request.headers),
                "postData": _safe_post_data(post_data),
            })
        except Exception as e:
            network_log["events"].append({
                "type": "request-log-error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            })

    async def record_response(response):
        try:
            url = response.url
            if not _network_interesting(url):
                return
            event = {
                "type": "response",
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": response.status,
                "url": _safe_url(url),
                "headers": _safe_headers(response.headers),
            }
            if "api.stripe.com" in url or "/stripe/add-card" in url or "/subscription/" in url:
                try:
                    body = await response.text()
                    event["body"] = _safe_post_data(body)
                except Exception as body_error:
                    event["bodyError"] = str(body_error)
            network_log["events"].append(event)
        except Exception as e:
            network_log["events"].append({
                "type": "response-log-error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            })

    engine, browser_driver = _billing_browser_engine()
    playwright = await browser_driver().start()
    browser = None
    context = None
    page = None
    try:
        browser_fingerprint = _with_billing_profile(browser_fingerprint, client_ctx)
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url, client_ctx)
        log.info(f"绑卡浏览器引擎: {engine}, headless={_billing_browser_headless()}")
        log.info(f"绑卡阶段代理: {'direct' if not browser_proxy_url else browser_proxy_url}")
        network_log["browserEngine"] = engine
        network_log["browserHeadless"] = _billing_browser_headless()
        network_log["billingProxy"] = "direct" if not browser_proxy_url else browser_proxy_url
        network_log["billingRetryContext"] = {
            "retryIndex": int((client_ctx or {}).get("billing_retry_index", 0) or 0),
            "proxyIndex": int((client_ctx or {}).get("billing_proxy_index", 0) or 0),
            "proxySequenceConfigured": bool(_billing_proxy_sequence()),
            "forceRandomProfile": bool((client_ctx or {}).get("billing_force_random_profile")),
            "dashboardVersion": DASHBOARD_VERSION,
            "addCardDashboardVersionOverride": _billing_add_card_dashboard_version_override(),
        }
        network_log["billingFingerprintProfile"] = _billing_profile_summary(browser_fingerprint)
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine, browser_proxy_url))
        context_kwargs = {
            "proxy": _billing_context_proxy_option(engine, browser_proxy_url),
            "user_agent": None if engine == "cloak" and _env_bool("BILLING_CLOAK_NATIVE_UA", True) else (effective_user_agent or None),
            "viewport": _billing_profile_from_fingerprint(browser_fingerprint)["viewport"],
            "screen": _billing_profile_from_fingerprint(browser_fingerprint)["screen"],
            "locale": _billing_locale(),
            "timezone_id": _billing_timezone_id(),
            "extra_http_headers": _fingerprint_extra_headers(browser_fingerprint),
        }
        storage_state = (client_ctx or {}).get("browser_storage_state")
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**context_kwargs)
        await context.route("**/v1/sourceToken**", _route_dodgeball_source_token)
        await context.route("**/v1/payment_methods**", _make_route_stripe_payment_methods_request(email_addr))
        await context.route("**/stripe/add-card**", _make_route_add_card_request(client_ctx))
        # 完整 billing UI 会加载 Stripe 安全 iframe；只修补 dashboard 顶层，避免 Stripe iframe 被指纹 patch 影响。
        await context.add_init_script(_billing_top_frame_fingerprint_init_script(browser_fingerprint, user_agent))
        page = await context.new_page()
        network_log["fingerprintInitial"] = await _browser_fingerprint_snapshot(page)
        page.on("request", lambda request: asyncio.create_task(record_request(request)))
        page.on("response", lambda response: asyncio.create_task(record_response(response)))
        await _goto_with_abort_tolerance(page, "https://dashboard.vapi.ai/", wait_until="domcontentloaded", timeout=60000, label="dashboard root")
        network_log["fingerprintDashboard"] = await _browser_fingerprint_snapshot(page)
        await _store_dashboard_session(page, keys, email_addr)
        network_log["sessionInjected"] = {
            "hasOrgToken": bool(keys.get("org_token")),
            "hasUserToken": bool(keys.get("user_token")),
            "hasSupabaseToken": bool(keys.get("supabase_token")),
            "orgId": keys.get("org_id", ""),
        }
        await _goto_with_abort_tolerance(page, BILLING_URL, wait_until="domcontentloaded", timeout=120000, label="billing page")
        network_log["pageStateBeforeBillingReady"] = await _billing_page_diagnostics(page)
        await _ensure_billing_ready(page, email_addr, keys)
        if _billing_dodgeball_refresh_enabled("after-billing-ready"):
            await _refresh_browser_dodgeball_state(page, context, client_ctx, "after-billing-ready", network_log)
        else:
            network_log.setdefault("dodgeballRefresh", []).append({"label": "after-billing-ready", "skipped": True})
        await _complete_welcome_onboarding_if_present(page, email_addr, timeout=2500)
        network_log["pageStateAfterBillingReady"] = await _billing_page_diagnostics(page)
        await _open_payment_method_editor_if_needed(page)
        await _wait_stripe_inputs_ready(page, _env_int("BILLING_STRIPE_INPUT_TIMEOUT_MS", 120000))
        await _update_billing_email_if_needed(page, email_addr)
        await _complete_welcome_onboarding_if_present(page, email_addr, timeout=1000)

        await _human_mouse_wiggle(page, "before stripe fill")
        raw_card_for_state = card_override or config.billing_card()
        if _env_bool("BILLING_FULL_UI_UNIFIED_CARD_TYPE", True):
            # Dashboard currently mounts a unified CardElement. Typing all fields
            # into cardnumber lets Stripe distribute digits internally and avoids
            # focus races where exp/cardnumber were observed one digit short.
            await _fill_stripe_card_details(page, raw_card_for_state)
        else:
            await _fill_stripe_input(page, "cardnumber", card["number"], re.sub(r"\D", "", card["number"]))
            await _fill_stripe_input(page, "exp-date", card["expiry"], re.sub(r"\D", "", card["expiry"]))
            await _fill_stripe_input(page, "cvc", card["cvc"], re.sub(r"\D", "", card["cvc"]))
        await _human_mouse_wiggle(page, "after stripe fill")
        await _complete_welcome_onboarding_if_present(page, email_addr, timeout=1000)

        network_log["stripeInputStateAfterFill"] = await _wait_stripe_card_inputs_complete(
            page,
            raw_card_for_state,
            _env_int("BILLING_FULL_UI_CARD_COMPLETE_TIMEOUT_MS", 10000),
        )
        if not _stripe_card_inputs_complete_from_state(network_log["stripeInputStateAfterFill"]):
            raise RuntimeError(
                "Stripe card inputs incomplete after fill: "
                f"{json.dumps(network_log['stripeInputStateAfterFill'], ensure_ascii=False)[:1000]}"
            )
        save_button, save_button_info = await _find_billing_payment_save_button(page)
        network_log["selectedSaveButton"] = save_button_info
        if not save_button or not await _is_visible(save_button, timeout=1500):
            raise RuntimeError(f"billing payment save button not found: {json.dumps(save_button_info, ensure_ascii=False)[:800]}")
        if _billing_dodgeball_refresh_enabled("before-add-card"):
            await _refresh_browser_dodgeball_state(page, context, client_ctx, "before-add-card", network_log)
        else:
            network_log.setdefault("dodgeballRefresh", []).append({"label": "before-add-card", "skipped": True})
        network_log["saveButtonState"] = await _billing_page_diagnostics(page)
        network_log["beforeAddCardDelayMs"] = await _human_pause_before_add_card(page, email_addr, "browser add-card")
        await wait_billing_attach_slot(email_addr, "browser add-card")
        response, save_click_attempts = await _click_billing_save_and_wait_add_card(page, save_button_info)
        network_log["saveClickAttempts"] = save_click_attempts
        body = await response.text()
        network_log["addCardResult"] = {
            "status": response.status,
            "ok": bool(response.ok),
            "classification": _classify_add_card_failure(response.status, body),
            "body": _safe_post_data(body),
        }
        if not response.ok:
            raise RuntimeError(f"browser add-card {response.status}: {body[:300]}")

        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        pm_id = data.get("stripePaymentMethodId") or data.get("paymentMethodId") or "browser"
        await page.wait_for_timeout(2000)
        await write_network_log()
        log.info(f"[{email_addr}] 浏览器绑卡网络日志: {network_path}")
        log.info(f"[{email_addr}] 浏览器绑卡成功: {_mask_id(pm_id)}")
        return pm_id
    except asyncio.CancelledError as e:
        screenshot_path = debug_dir / f"billing-bind-cancelled-{timestamp}.png"
        html_path = debug_dir / f"billing-bind-cancelled-page-{timestamp}.html"
        network_log["cancelled"] = True
        network_log["lastError"] = f"CancelledError: {e}"
        try:
            if page:
                network_log["pageStateOnCancel"] = await _billing_page_diagnostics(page)
                await page.screenshot(path=str(screenshot_path), full_page=True)
                html_path.write_text(await page.content(), encoding="utf-8")
                network_log["cancelScreenshot"] = str(screenshot_path)
                network_log["cancelHtml"] = str(html_path)
        except Exception as diag_error:
            network_log["cancelDiagnosticError"] = str(diag_error)[:500]
        await write_network_log()
        raise
    except Exception as e:
        screenshot_path = debug_dir / f"billing-bind-failed-{timestamp}.png"
        html_path = debug_dir / f"billing-bind-page-{timestamp}.html"
        network_log["lastError"] = f"{type(e).__name__}: {e}"
        try:
            if page:
                network_log["pageStateOnFailure"] = await _billing_page_diagnostics(page)
        except Exception as diag_error:
            network_log["failureDiagnosticError"] = str(diag_error)[:500]
        await write_network_log()
        try:
            if page:
                await page.screenshot(path=str(screenshot_path), full_page=True)
                html_path.write_text(await page.content(), encoding="utf-8")
                raise RuntimeError(f"{e}; network={network_path}; screenshot={screenshot_path}; html={html_path}") from e
        except RuntimeError:
            raise
        except Exception:
            pass
        raise
    finally:
        if context:
            await _close_context_safely(context)
        if browser:
            await _close_browser_safely(browser)
        await _stop_playwright_safely(playwright)


async def _bind_card_browser_with_timeout(
    proxy_url: str,
    keys: dict,
    email_addr: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
    reason: str = "browser billing",
    card_override: dict | None = None,
) -> str:
    total_timeout = float(os.getenv("BILLING_BROWSER_TOTAL_TIMEOUT", "300"))
    attempts = max(1, _env_int(
        "BILLING_BROWSER_DECLINE_RETRY_ATTEMPTS",
        _env_int("BILLING_ATTACH_DECLINE_RETRY_ATTEMPTS", 3),
    ))
    card_declined_attempts = max(1, _env_int("BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS", 2))
    retry_sleep = max(0, _env_int("BILLING_BROWSER_DECLINE_RETRY_SLEEP_SECONDS", _env_int("BILLING_ATTACH_DECLINE_RETRY_SLEEP_SECONDS", 0)))
    attempt_timeout = float(os.getenv("BILLING_BROWSER_ATTEMPT_TIMEOUT", "180"))
    if attempt_timeout <= 0:
        attempt_timeout = total_timeout if total_timeout > 0 else 0
    deadline = time.time() + total_timeout if total_timeout and total_timeout > 0 else None
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        if attempt > 1 and retry_sleep:
            await asyncio.sleep(retry_sleep)
        remaining = (deadline - time.time()) if deadline else None
        if remaining is not None and remaining <= 0:
            break
        timeout = min(attempt_timeout, remaining) if remaining is not None and attempt_timeout > 0 else attempt_timeout
        try:
            log.info(f"[{email_addr}] 浏览器绑卡尝试: attempt={attempt}/{attempts} timeout={timeout:.0f}s reason={reason}")
            coro = _bind_card_browser(proxy_url, keys, email_addr, user_agent, browser_fingerprint, client_ctx, card_override)
            if timeout and timeout > 0:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro
        except asyncio.TimeoutError as e:
            last_error = RuntimeError(f"browser billing timed out after {timeout:.0f}s: {reason}; attempt={attempt}/{attempts}")
            is_card_declined = False
            max_attempts_for_error = attempts
            restarted, recycled = await _recover_after_billing_attach_decline(str(last_error))
            remaining = (deadline - time.time()) if deadline else None
            if attempt < max_attempts_for_error and (remaining is None or remaining > 30):
                _reset_billing_environment_for_retry(client_ctx, f"browser timeout retry attempt={attempt + 1}")
                log.warning(
                    f"浏览器绑卡超时后按环境问题重试: attempt={attempt + 1}/{max_attempts_for_error} "
                    f"warpRestarted={restarted} poolRecycled={recycled} remaining={remaining if remaining is not None else -1:.0f}s"
                )
                continue
            raise last_error from e
        except Exception as e:
            last_error = e
            error_text = str(e)
            if not (_billing_attach_declined_error(error_text) or _billing_environment_retryable_error(error_text)):
                raise
            is_card_declined = _billing_card_declined_error(error_text)
            max_attempts_for_error = min(attempts, card_declined_attempts) if is_card_declined else attempts
            if is_card_declined and _billing_card_declined_stop_enabled():
                _record_billing_attach_risk(f"browser card_declined stop: {type(e).__name__}: {e}")
                log.warning(
                    f"浏览器 add-card 明确 card_declined，按 BILLING_STOP_ON_CARD_DECLINED=1 终止本账号绑卡，"
                    f"不再切换 WARP/指纹重试: attempt={attempt}/{attempts}"
                )
                raise
            if is_card_declined and not _env_bool("BILLING_BROWSER_RECOVER_ON_CARD_DECLINED", True):
                restarted, recycled = False, False
                log.warning(
                    f"浏览器 add-card 明确 card_declined，配置为不恢复环境: "
                    f"attempt={attempt}/{attempts}"
                )
            else:
                restarted, recycled = await _recover_after_billing_attach_decline(
                    f"browser billing environment issue attempt={attempt}/{attempts}: {type(e).__name__}: {e}"
                )
            remaining = (deadline - time.time()) if deadline else None
            if attempt < max_attempts_for_error and (remaining is None or remaining > 30):
                _reset_billing_environment_for_retry(
                    client_ctx,
                    f"browser {'card_declined' if is_card_declined else 'environment'} retry attempt={attempt + 1}",
                )
                log.warning(
                    f"浏览器绑卡 400/环境异常后重试: attempt={attempt + 1}/{max_attempts_for_error} "
                    f"warpRestarted={restarted} poolRecycled={recycled} remaining={remaining if remaining is not None else -1:.0f}s"
                )
                continue
            raise

    if last_error:
        raise last_error
    raise RuntimeError(f"browser billing timed out after {total_timeout:.0f}s: {reason}")


async def register_one(
    proxy_url: str,
    email_addr: str,
    email_id: str,
    password: str,
    mail: MoeMailClient,
) -> dict | None:
    """纯协议注册全流程"""
    try:
        proxies = _make_session(proxy_url)

        # 1. 注册
        log.info(f"[{email_addr}] 注册中...")
        client_ctx = _new_client_context()
        _init_billing_proxy_index_for_registration(client_ctx)
        signup_mode = os.getenv("SIGNUP_MODE", os.getenv("VAPI_SIGNUP_MODE", "browser-fetch")).strip().lower()
        bind_mode = os.getenv("BILLING_BIND_MODE", "browser").strip().lower()
        browser_fingerprint: dict | None = None
        user_agent = ""
        preselected_billing_cards: list[dict] | None = None
        preflight_cards = _billing_cards_for_preflight(bind_mode)
        available_cards, quarantined_cards = _filter_quarantined_billing_cards(preflight_cards)
        if quarantined_cards:
            log.warning(f"[{email_addr}] 注册前跳过已隔离卡: " + "; ".join(quarantined_cards[:3]))
        if not available_cards and not _env_bool("BILLING_CARD_DECLINE_ALLOW_ALL_QUARANTINED", False):
            raise RuntimeError(
                "所有 billing 卡都处于 card_declined 隔离期；"
                "请更换新卡/配置 billingCardPool，或临时设置 BILLING_CARD_DECLINE_QUARANTINE_DISABLED=1"
            )
        if _billing_browser_card_pool_mode(bind_mode):
            preselected_billing_cards = available_cards or preflight_cards

        use_solver_browser = (
            signup_mode in ("browser", "browser-fetch", "fetch-browser", "solver-browser", "browser-solver")
            and _env_bool("SIGNUP_SOLVER_BROWSER", True)
        )
        solver_browser_error: Exception | None = None
        if use_solver_browser:
            try:
                _refresh_dashboard_version(proxies, "")
                browser_fingerprint = await _signup_solver_browser(
                    proxy_url,
                    email_addr,
                    password,
                    "",
                    None,
                    client_ctx,
                )
                user_agent = _fingerprint_user_agent(browser_fingerprint, "")
            except Exception as e:
                solver_browser_error = e
                if signup_mode in ("solver-browser", "browser-solver") or not _env_bool("SIGNUP_SOLVER_BROWSER_FALLBACK", True):
                    raise
                log.warning(f"[{email_addr}] solver-browser signup 失败，回退旧链路: {e}")

        if not use_solver_browser or solver_browser_error is not None:
            # 旧链路：先用真实注册页取得 Turnstile/CSRF token，再由协议/页面 fetch 提交。
            csrf_token, browser_fingerprint = await get_signup_csrf_token(proxy_url)
            user_agent = _fingerprint_user_agent(browser_fingerprint, "")
            _refresh_dashboard_version(proxies, user_agent)
            if signup_mode in ("browser", "browser-fetch", "fetch-browser"):
                await _signup_browser_fetch(
                    proxy_url,
                    email_addr,
                    password,
                    csrf_token,
                    user_agent,
                    browser_fingerprint,
                    client_ctx,
                )
            else:
                client_ctx.update(await _prepare_dodgeball_identity(proxy_url, user_agent, browser_fingerprint))
                await asyncio.to_thread(
                    _signup,
                    proxies,
                    email_addr,
                    password,
                    csrf_token,
                    user_agent,
                    browser_fingerprint,
                    client_ctx,
                )
        log.info(f"[{email_addr}] 注册请求成功")

        # 2. 等邮件
        log.info(f"[{email_addr}] 等待验证邮件...")
        msg = await mail.wait_for_message(email_id, timeout=90, subject_contains="")
        html = msg.get("html", "") or msg.get("text", "")
        verify_url = extract_verify_link(html)
        log.info(f"[{email_addr}] 验证链接获取成功")

        # 3. 验证 + JWT 交换 + 取 key
        log.info(f"[{email_addr}] 验证并提取 key...")
        keys = await asyncio.to_thread(
            _verify_and_get_keys,
            proxies,
            verify_url,
            user_agent,
            browser_fingerprint,
            client_ctx,
        )

        await wait_before_billing_stage(email_addr)

        # 4. 绑卡
        if bind_mode in ("protocol", "stripe", "stripe-browser"):
            log.info(f"[{email_addr}] 绑卡中... mode=protocol")
            cards = preselected_billing_cards or _billing_cards_for_preflight(bind_mode)
            cards = _order_billing_cards_for_registration(cards)
            max_cards = max(1, _env_int("BILLING_CARD_POOL_MAX_CARDS", len(cards) or 1))
            cards = cards[:max_cards]
            bind_error: Exception | None = None
            for card_index, card in enumerate(cards, start=1):
                try:
                    if len(cards) > 1:
                        log.info(f"[{email_addr}] 协议绑卡卡池尝试: card={card_index}/{len(cards)} tail=****{_billing_card_tail(card)}")
                    pm_id = await _bind_card_protocol(
                        proxy_url,
                        proxies,
                        keys["org_token"],
                        email_addr,
                        user_agent,
                        browser_fingerprint,
                        client_ctx,
                        keys,
                        card,
                    )
                    _clear_billing_card_decline(card)
                    break
                except Exception as e:
                    bind_error = e
                    error_text = str(e)
                    is_card_declined = _billing_card_declined_error(error_text)
                    is_attach_400 = _billing_attach_declined_error(error_text)
                    is_environment = _billing_environment_retryable_error(error_text)
                    if is_card_declined:
                        _record_billing_card_decline(card, error_text)
                    if (is_card_declined or is_attach_400) and _env_bool("BILLING_ONE_CARD_PER_ORG_ON_DECLINE", False):
                        log.warning(
                            f"[{email_addr}] protocol add-card 400/card_declined 后按单 org 单卡策略停止当前 org，"
                            f"下次补号会轮换卡/环境: tail=****{_billing_card_tail(card)}"
                        )
                        raise
                    if not (is_card_declined or is_attach_400 or is_environment) or card_index >= len(cards):
                        raise
                    _reset_billing_environment_for_retry(
                        client_ctx,
                        f"protocol card pool switch after {'card_declined' if is_card_declined else 'add-card 400' if is_attach_400 else 'environment'} tail=****{_billing_card_tail(card)}",
                    )
                    log.warning(
                        f"[{email_addr}] 当前协议卡 ****{_billing_card_tail(card)} 返回 {'card_declined' if is_card_declined else 'add-card 400' if is_attach_400 else 'environment'}，"
                        f"按环境问题切换新环境/备用卡继续: next={card_index + 1}/{len(cards)}"
                    )
            else:
                raise bind_error or RuntimeError("protocol billing card pool exhausted")
        elif bind_mode in ("api", "raw"):
            log.info(f"[{email_addr}] 绑卡中... mode=api")
            primary_card = config.billing_card()
            try:
                pm_id = await asyncio.to_thread(
                    _bind_card_api,
                    proxies,
                    keys["org_token"],
                    email_addr,
                    user_agent,
                    browser_fingerprint,
                    client_ctx,
                )
                _clear_billing_card_decline(primary_card)
            except Exception as e:
                if _billing_card_declined_error(str(e)):
                    _record_billing_card_decline(primary_card, str(e))
                raise
        else:
            log.info(f"[{email_addr}] 绑卡中... mode=browser")
            cards = preselected_billing_cards or _billing_cards_for_preflight(bind_mode)
            cards = _order_billing_cards_for_registration(cards)
            max_cards = max(1, _env_int("BILLING_CARD_POOL_MAX_CARDS", len(cards) or 1))
            cards = cards[:max_cards]
            bind_error: Exception | None = None
            for card_index, card in enumerate(cards, start=1):
                try:
                    if len(cards) > 1:
                        log.info(f"[{email_addr}] 绑卡卡池尝试: card={card_index}/{len(cards)} tail=****{_billing_card_tail(card)}")
                    pm_id = await _bind_card_browser_with_timeout(
                        proxy_url,
                        keys,
                        email_addr,
                        user_agent,
                        browser_fingerprint,
                        client_ctx,
                        reason=f"cloak+warp browser billing mode card={card_index}/{len(cards)}",
                        card_override=card,
                    )
                    _clear_billing_card_decline(card)
                    break
                except Exception as e:
                    bind_error = e
                    error_text = str(e)
                    is_card_declined = _billing_card_declined_error(error_text)
                    is_attach_400 = _billing_attach_declined_error(error_text)
                    is_environment = _billing_environment_retryable_error(error_text)
                    if is_card_declined:
                        _record_billing_card_decline(card, error_text)
                    if (is_card_declined or is_attach_400) and _env_bool("BILLING_ONE_CARD_PER_ORG_ON_DECLINE", False):
                        log.warning(
                            f"[{email_addr}] add-card 400/card_declined 后按单 org 单卡策略停止当前 org，"
                            f"下次补号会轮换卡/环境: tail=****{_billing_card_tail(card)}"
                        )
                        raise
                    if not (is_card_declined or is_attach_400 or is_environment) or card_index >= len(cards):
                        raise
                    if is_card_declined:
                        switch_reason = "card_declined"
                    elif is_attach_400:
                        switch_reason = "add-card 400"
                    else:
                        switch_reason = "environment retryable"
                    _reset_billing_environment_for_retry(
                        client_ctx,
                        f"card pool switch after {switch_reason} tail=****{_billing_card_tail(card)}",
                    )
                    log.warning(
                        f"[{email_addr}] 当前卡 ****{_billing_card_tail(card)} 返回 {switch_reason}，"
                        f"按环境问题切换新环境/备用卡继续: next={card_index + 1}/{len(cards)}"
                    )
            else:
                raise bind_error or RuntimeError("billing card pool exhausted")
        log.info(f"[{email_addr}] 绑卡成功: {pm_id}")
        _clear_billing_attach_risk("billing success")

        result = {"email": email_addr, "private_key": keys["private_key"],
                  "public_key": keys["public_key"], "org_id": keys["org_id"]}
        log.info(f"[{email_addr}] ✅ 注册+绑卡完成 private_key={result['private_key'][:8]}...")
        return result

    except Exception as e:
        log.error(f"[{email_addr}] ❌ 注册失败: {e}")
        return None
