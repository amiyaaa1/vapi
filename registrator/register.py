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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, unquote, urlsplit, urlunsplit
import httpx
from curl_cffi import requests as cffi_requests
from playwright.async_api import async_playwright
from .csrf import BILLING_URL, TURNSTILE_SOLVER_POLL_INTERVAL, TURNSTILE_SOLVER_URL, get_signup_csrf_token
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


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


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
            match = re.search(r"daily-\d{4}-\d{2}-\d{2}-\d{4}", js.text)
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


def _billing_stage_proxy(proxy_url: str = "") -> str:
    """绑卡阶段代理覆盖：BILLING_BIND_PROXY=direct 可让 billing/Stripe 直连。"""
    override = os.getenv("BILLING_BIND_PROXY", os.getenv("BILLING_BIND_PROXY_URL", "")).strip()
    if not override:
        return proxy_url
    if override.lower() in ("0", "none", "no", "off", "direct", "direct://"):
        return ""
    return override


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
    if isinstance(browser_fingerprint, dict):
        sec_ch_ua = browser_fingerprint.get("sec_ch_ua") or browser_fingerprint.get("secChUa") or ""
        if sec_ch_ua:
            headers["sec-ch-ua"] = str(sec_ch_ua)
            headers["sec-ch-ua-platform"] = '"Windows"'
    return headers


def _billing_locale() -> str:
    return os.getenv("BILLING_LOCALE", "en-US").strip() or "en-US"


def _billing_timezone_id() -> str:
    # WARP 当前出口实测在 Germany/Dreieich；默认先让 JS timezone 不再暴露 UTC。
    return os.getenv("BILLING_TIMEZONE_ID", "Europe/Berlin").strip() or "Europe/Berlin"


def _billing_fingerprint_init_script() -> str:
    return r"""
(() => {
  const defineGetter = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, { get: () => value, configurable: true });
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
      { brand: 'Not/A)Brand', version: '99' },
      { brand: 'Google Chrome', version: '148' },
      { brand: 'Chromium', version: '148' },
    ];
    const fullVersion = '148.0.7778.96';
    const highEntropy = {
      architecture: 'x86',
      bitness: '64',
      brands,
      fullVersionList: [
        { brand: 'Not/A)Brand', version: '99.0.0.0' },
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
})();
"""


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
    r = cffi_requests.post(
        "https://api.vapi.ai/stripe/add-card",
        headers=_dashboard_headers(
            user_agent=user_agent,
            authorization=f"Bearer {token}",
            referer="https://dashboard.vapi.ai/settings/billing",
            browser_fingerprint=browser_fingerprint,
            client_ctx=client_ctx,
            include_device_fingerprint=True,
        ),
        json={"paymentMethodId": pm_id},
        proxies=proxies,
        impersonate="chrome",
        timeout=20,
    )
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
    time.sleep(random.uniform(0.8, 2.4))
    return _attach_payment_method(proxies, org_token, pm_id, user_agent, browser_fingerprint, client_ctx)


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


async def _fill_stripe_input(page, input_name: str, value: str, expected_digits: str = ""):
    frame = await _find_stripe_input_frame(page, input_name)
    locator = frame.locator(f'input[name="{input_name}"]').first

    async def read_digits() -> str:
        try:
            return re.sub(r"\D", "", await locator.input_value(timeout=1500))
        except Exception:
            return ""

    await locator.click(timeout=5000)
    try:
        await locator.fill(value, timeout=7000)
    except Exception:
        await locator.type(value, delay=35, timeout=15000)

    if expected_digits:
        current_digits = await read_digits()
        if current_digits != expected_digits:
            try:
                await locator.fill("", timeout=3000)
                await locator.fill(value, timeout=7000)
            except Exception:
                await locator.click(timeout=5000)
                await locator.press("Control+A", timeout=3000)
                await locator.press("Backspace", timeout=3000)
                await locator.type(value, delay=35, timeout=15000)
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


async def _mount_stripe_elements(page, publishable_key: str):
    await _load_stripe_js(page)
    await page.evaluate(
        """async (publishableKey) => {
            if (typeof window.Stripe !== 'function') {
                throw new Error(`Stripe.js not ready: typeof window.Stripe=${typeof window.Stripe}`);
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
            const cardNumber = elements.create('cardNumber', { style, showIcon: true });
            const cardExpiry = elements.create('cardExpiry', { style });
            const cardCvc = elements.create('cardCvc', { style });
            window.__vapiStripe = stripe;
            window.__vapiStripeElements = elements;
            window.__vapiStripeCardNumber = cardNumber;
            window.__vapiStripeCardExpiry = cardExpiry;
            window.__vapiStripeCardCvc = cardCvc;
            const ready = Promise.all([
                new Promise((resolve) => cardNumber.on('ready', resolve)),
                new Promise((resolve) => cardExpiry.on('ready', resolve)),
                new Promise((resolve) => cardCvc.on('ready', resolve)),
            ]).then(() => true);
            const timeout = new Promise((_, reject) => {
                setTimeout(() => reject(new Error('Stripe Elements not ready after 45000ms')), 45000);
            });
            window.__vapiStripeReady = Promise.race([ready, timeout]);
            cardNumber.mount('#card-number');
            cardExpiry.mount('#card-expiry');
            cardCvc.mount('#card-cvc');
        }""",
        publishable_key,
    )
    await page.evaluate("() => window.__vapiStripeReady")


async def _create_stripe_payment_method_browser(
    proxy_url: str,
    card: dict,
    email_addr: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
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
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url)
        log.info(f"Stripe PM 浏览器引擎: {engine}, headless={_billing_browser_headless()}")
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine))
        context = await browser.new_context(
            proxy={"server": browser_proxy_url} if browser_proxy_url else None,
            user_agent=effective_user_agent or None,
            viewport={"width": 1280, "height": 720},
            screen={"width": 1365, "height": 900},
            locale=_billing_locale(),
            timezone_id=_billing_timezone_id(),
            extra_http_headers=_fingerprint_extra_headers(browser_fingerprint),
        )
        await context.add_init_script(_billing_fingerprint_init_script())
        page = await context.new_page()
        await page.goto("https://dashboard.vapi.ai/settings/billing", wait_until="domcontentloaded", timeout=60000)
        await page.set_content(
            """
            <!doctype html>
            <html>
              <head>
                <meta charset="utf-8">
                <title>Stripe PaymentMethod</title>
                <style>
                  body { margin: 24px; font-family: Arial, sans-serif; }
                  .field { width: 420px; min-height: 44px; margin: 12px 0; padding: 12px; border: 1px solid #d0d5dd; border-radius: 6px; }
                </style>
              </head>
              <body>
                <div id="card-number" class="field"></div>
                <div id="card-expiry" class="field"></div>
                <div id="card-cvc" class="field"></div>
              </body>
            </html>
            """,
            wait_until="domcontentloaded",
        )
        await _mount_stripe_elements(page, STRIPE_PK)

        exp_digits = f"{card['exp_month']}{card['exp_year'][-2:]}"
        await _fill_stripe_input(page, "cardnumber", card["number"], re.sub(r"\D", "", card["number"]))
        await _fill_stripe_input(page, "exp-date", f"{card['exp_month']} / {card['exp_year'][-2:]}", exp_digits)
        await _fill_stripe_input(page, "cvc", card["cvc"], re.sub(r"\D", "", card["cvc"]))

        result = await page.evaluate(
            """async (billingDetails) => {
                const result = await window.__vapiStripe.createPaymentMethod({
                    type: 'card',
                    card: window.__vapiStripeCardNumber,
                    billing_details: billingDetails,
                });
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
                return { ok: true, id: result.paymentMethod && result.paymentMethod.id };
            }""",
            {"email": email_addr, "name": _billing_name(email_addr)},
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
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


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
    if proxy_url:
        payload["proxy"] = proxy_url

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
) -> str:
    """协议绑卡：Stripe.js 三框 Element 创建 PaymentMethod → Vapi add-card"""
    raw_card = config.billing_card()
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
    stripe_mode = os.getenv("STRIPE_PAYMENT_METHOD_MODE", "browser").strip().lower()
    if stripe_mode in ("solver", "nexos"):
        try:
            pm_id, stripe_user_agent = await _create_stripe_payment_method_solver(proxy_url, card, email_addr)
        except Exception as e:
            if os.getenv("STRIPE_SOLVER_FALLBACK", "0") not in ("1", "true", "TRUE", "yes", "YES"):
                raise RuntimeError(f"Stripe solver payment_methods failed: {e}") from e
            log.warning(f"Stripe solver 不可用，回退本地浏览器: {e}")
            pm_id = await _create_stripe_payment_method_browser(proxy_url, card, email_addr, user_agent, browser_fingerprint)
            stripe_user_agent = user_agent
    else:
        log.info("Stripe PaymentMethod 使用独立浏览器创建，复用 Turnstile 指纹")
        pm_id = await _create_stripe_payment_method_browser(proxy_url, card, email_addr, user_agent, browser_fingerprint)
        stripe_user_agent = user_agent

    attach_proxies = _make_session(_billing_stage_proxy(proxy_url))
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


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception:
        return {}


def _billing_card_for_browser() -> dict:
    card = config.billing_card()
    if not all(card.values()):
        raise RuntimeError("Billing card config incomplete: number, expiry, and cvc are required")
    _validate_card_config(card)
    return {
        "number": card["number"],
        "expiry": f"{card['exp_month']} / {card['exp_year'][-2:]}",
        "cvc": card["cvc"],
    }


def _billing_browser_engine():
    # 默认仍用 Playwright：当前本地指纹修复依赖 add_init_script 注入到 Stripe iframe；
    # patchright 在该镜像里会屏蔽 init script，反而暴露 Linux/SwiftShader。
    engine = os.getenv("BILLING_BROWSER_ENGINE", "playwright").strip().lower()
    if engine in ("patchright", "stealth", "patched"):
        try:
            from patchright.async_api import async_playwright as patchright_async_playwright
            return "patchright", patchright_async_playwright
        except Exception as e:
            log.warning(f"Patchright 不可用，回退 Playwright: {e}")
    return "playwright", async_playwright


def _billing_browser_headless() -> bool:
    # 当前混合链路已验证 headless 可用；需要 headful 排障时显式 BILLING_BROWSER_HEADLESS=0。
    value = os.getenv("BILLING_BROWSER_HEADLESS", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _billing_launch_args(engine: str) -> list[str]:
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]
    if os.getenv("BILLING_ENABLE_WEBGL", "1").strip().lower() not in ("0", "false", "no", "off"):
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


def _billing_launch_kwargs(engine: str) -> dict:
    kwargs = {
        "headless": _billing_browser_headless(),
        "executable_path": _browser_executable_path(),
        "args": _billing_launch_args(engine),
    }
    if engine != "patchright" and os.getenv("BILLING_IGNORE_ENABLE_AUTOMATION", "1").strip().lower() not in ("0", "false", "no"):
        kwargs["ignore_default_args"] = ["--enable-automation"]
    return kwargs


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
        elif source == "DODGEBALL_FINGERPRINT_SERVICE":
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
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url)
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine))
        context = await browser.new_context(
            proxy={"server": browser_proxy_url} if browser_proxy_url else None,
            user_agent=effective_user_agent or None,
            viewport={"width": 1365, "height": 900},
            screen={"width": 1365, "height": 900},
            locale=_billing_locale(),
            timezone_id=_billing_timezone_id(),
            extra_http_headers=_fingerprint_extra_headers(browser_fingerprint),
        )
        await context.route("**/v1/sourceToken**", _route_dodgeball_source_token)
        await context.add_init_script(_billing_fingerprint_init_script())
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
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


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


async def _signup_solver_browser(
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
    if proxy_url:
        payload["proxy"] = proxy_url

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
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url)
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine))
        context_kwargs = {
            "proxy": {"server": browser_proxy_url} if browser_proxy_url else None,
            "user_agent": effective_user_agent or None,
            "viewport": {"width": 1365, "height": 900},
            "screen": {"width": 1365, "height": 900},
            "locale": _billing_locale(),
            "timezone_id": _billing_timezone_id(),
            "extra_http_headers": _fingerprint_extra_headers(browser_fingerprint),
        }
        storage_state = (client_ctx or {}).get("browser_storage_state")
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**context_kwargs)
        await context.route("**/v1/sourceToken**", _route_dodgeball_source_token)
        await context.add_init_script(_billing_fingerprint_init_script())
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
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


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
        }""",
        {
            "supabaseAuth": supabase_auth,
            "orgToken": keys.get("org_token", ""),
            "userToken": keys.get("user_token", ""),
            "orgId": keys.get("org_id", ""),
        },
    )


async def _complete_welcome_onboarding_if_present(page, email_addr: str):
    dialog = page.locator('[role="dialog"][data-state="open"]').filter(has=page.get_by_text("Welcome to Vapi")).first
    if not await _is_visible(dialog, timeout=1500):
        return False

    name_input = dialog.locator('input[name="name"]').first
    if await _is_visible(name_input, timeout=3000):
        name = (email_addr.split("@", 1)[0] or "Vapi User").replace(".", " ").replace("_", " ")[:60]
        await name_input.fill("")
        await name_input.type(name, delay=25)

    other_input = dialog.locator('input[name="otherSource"]').first
    if await _is_visible(other_input, timeout=1000):
        await other_input.fill("")
        await other_input.type("Dashboard", delay=25)

    for button_name in ("Google", "Personal"):
        button = dialog.get_by_role("button", name=re.compile(f"^{re.escape(button_name)}", re.I)).first
        if await _is_visible(button, timeout=1000):
            await button.click()
            await page.wait_for_timeout(500)

    start = dialog.get_by_role("button", name=re.compile("Start Building", re.I)).first
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if await start.is_enabled():
                await start.click()
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
    raise RuntimeError("Welcome onboarding Start Building stayed disabled")


async def _ensure_billing_ready(page, email_addr: str):
    deadline = time.time() + 120
    heading = page.locator('h2:has-text("Payment Method")').first
    dialog = page.locator('[role="dialog"][data-state="open"]').filter(has=page.get_by_text("Welcome to Vapi")).first
    while time.time() < deadline:
        if await _is_visible(dialog, timeout=500):
            await _complete_welcome_onboarding_if_present(page, email_addr)
            await page.wait_for_timeout(1000)
            continue
        if await _is_visible(heading):
            return
        if "/settings/billing" not in page.url:
            await page.goto(BILLING_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1000)
    await heading.wait_for(state="visible", timeout=30000)


async def _open_payment_method_editor_if_needed(page):
    iframe = page.locator('iframe[title="Secure card payment input frame"]').first
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


async def _bind_card_browser(
    proxy_url: str,
    keys: dict,
    email_addr: str,
    user_agent: str = "",
    browser_fingerprint: dict | None = None,
    client_ctx: dict | None = None,
) -> str:
    card = _billing_card_for_browser()
    log.info(f"[{email_addr}] 启动浏览器绑卡: ****{card['number'][-4:]} exp={card['expiry']}")
    debug_dir = Path("/data/browser-bind-debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    network_path = debug_dir / f"network-{timestamp}.json"
    network_log = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "email": email_addr,
        "cardTail": card["number"][-4:],
        "dodgeballSourceTokenSha256": _hash_value((client_ctx or {}).get("dodgeball_source_token", "")) if (client_ctx or {}).get("dodgeball_source_token") else "",
        "events": [],
    }

    async def write_network_log():
        network_log["finishedAt"] = datetime.now(timezone.utc).isoformat()
        network_log["eventCount"] = len(network_log["events"])
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
    try:
        effective_user_agent = _fingerprint_user_agent(browser_fingerprint, user_agent)
        browser_proxy_url = _billing_stage_proxy(proxy_url)
        log.info(f"绑卡浏览器引擎: {engine}, headless={_billing_browser_headless()}")
        log.info(f"绑卡阶段代理: {'direct' if not browser_proxy_url else browser_proxy_url}")
        network_log["browserEngine"] = engine
        network_log["browserHeadless"] = _billing_browser_headless()
        network_log["billingProxy"] = "direct" if not browser_proxy_url else browser_proxy_url
        browser = await playwright.chromium.launch(**_billing_launch_kwargs(engine))
        context_kwargs = {
            "proxy": {"server": browser_proxy_url} if browser_proxy_url else None,
            "user_agent": effective_user_agent or None,
            "viewport": {"width": 1365, "height": 900},
            "screen": {"width": 1365, "height": 900},
            "locale": _billing_locale(),
            "timezone_id": _billing_timezone_id(),
            "extra_http_headers": _fingerprint_extra_headers(browser_fingerprint),
        }
        storage_state = (client_ctx or {}).get("browser_storage_state")
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**context_kwargs)
        await context.route("**/v1/sourceToken**", _route_dodgeball_source_token)
        await context.add_init_script(_billing_fingerprint_init_script())
        page = await context.new_page()
        network_log["fingerprintInitial"] = await _browser_fingerprint_snapshot(page)
        page.on("request", lambda request: asyncio.create_task(record_request(request)))
        page.on("response", lambda response: asyncio.create_task(record_response(response)))
        await page.goto("https://dashboard.vapi.ai/", wait_until="domcontentloaded", timeout=60000)
        network_log["fingerprintDashboard"] = await _browser_fingerprint_snapshot(page)
        await _store_dashboard_session(page, keys, email_addr)
        await page.goto(BILLING_URL, wait_until="domcontentloaded", timeout=120000)
        await _ensure_billing_ready(page, email_addr)
        await _open_payment_method_editor_if_needed(page)
        iframe = page.locator('iframe[title="Secure card payment input frame"]').first
        await iframe.wait_for(state="visible", timeout=120000)
        await _update_billing_email_if_needed(page, email_addr)

        await _fill_stripe_input(page, "cardnumber", card["number"], re.sub(r"\D", "", card["number"]))
        await _fill_stripe_input(page, "exp-date", card["expiry"], re.sub(r"\D", "", card["expiry"]))
        await _fill_stripe_input(page, "cvc", card["cvc"], re.sub(r"\D", "", card["cvc"]))

        payment_card = page.locator('iframe[title="Secure card payment input frame"]').locator(
            'xpath=ancestor::div[contains(@class,"space-y-4")][1]'
        )
        save_button = payment_card.locator("button").nth(1)
        async with page.expect_response(
            lambda response: "/stripe/add-card" in response.url and response.request.method == "POST",
            timeout=120000,
        ) as response_info:
            await save_button.click()
        response = await response_info.value
        body = await response.text()
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
    except Exception as e:
        screenshot_path = debug_dir / f"billing-bind-failed-{timestamp}.png"
        html_path = debug_dir / f"billing-bind-page-{timestamp}.html"
        await write_network_log()
        try:
            if "page" in locals():
                await page.screenshot(path=str(screenshot_path), full_page=True)
                html_path.write_text(await page.content(), encoding="utf-8")
                raise RuntimeError(f"{e}; network={network_path}; screenshot={screenshot_path}; html={html_path}") from e
        except RuntimeError:
            raise
        except Exception:
            pass
        raise
    finally:
        if browser:
            await browser.close()
        await playwright.stop()


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
        signup_mode = os.getenv("SIGNUP_MODE", os.getenv("VAPI_SIGNUP_MODE", "browser-fetch")).strip().lower()
        browser_fingerprint: dict | None = None
        user_agent = ""

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

        # 4. 绑卡
        bind_mode = os.getenv("BILLING_BIND_MODE", "protocol").strip().lower()
        if bind_mode in ("protocol", "stripe", "stripe-browser"):
            log.info(f"[{email_addr}] 绑卡中... mode=protocol")
            pm_id = await _bind_card_protocol(
                proxy_url,
                proxies,
                keys["org_token"],
                email_addr,
                user_agent,
                browser_fingerprint,
                client_ctx,
            )
        elif bind_mode in ("api", "raw"):
            log.info(f"[{email_addr}] 绑卡中... mode=api")
            pm_id = await asyncio.to_thread(
                _bind_card_api,
                proxies,
                keys["org_token"],
                email_addr,
                user_agent,
                browser_fingerprint,
                client_ctx,
            )
        else:
            log.info(f"[{email_addr}] 绑卡中... mode=browser")
            pm_id = await _bind_card_browser(proxy_url, keys, email_addr, user_agent, browser_fingerprint, client_ctx)
        log.info(f"[{email_addr}] 绑卡成功: {pm_id}")

        result = {"email": email_addr, "private_key": keys["private_key"],
                  "public_key": keys["public_key"], "org_id": keys["org_id"]}
        log.info(f"[{email_addr}] ✅ 注册+绑卡完成 private_key={result['private_key'][:8]}...")
        return result

    except Exception as e:
        log.error(f"[{email_addr}] ❌ 注册失败: {e}")
        return None
