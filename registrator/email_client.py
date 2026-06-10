import asyncio
import json
import logging
import re
from typing import Any

import httpx

from . import config

log = logging.getLogger("registrator.email")


def _extract_browser_auth_payload(html: str) -> dict[str, Any] | None:
    patterns = [
        r"window\.__BROWSER_AUTH\s*=\s*(\{.*?\})\s*;",
        r'"__BROWSER_AUTH"\s*:\s*(\{.*?\})(?:,|})',
    ]
    for pattern in patterns:
        match = re.search(pattern, html or "", re.S)
        if not match:
            continue
        try:
            return json.loads(match.group(1))
        except Exception:
            continue
    return None


def _headers(user_agent: str = "") -> dict[str, str]:
    return {
        "User-Agent": user_agent or "Mozilla/5.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def _email_domain(email: str) -> str:
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].strip().lower()


def _domain_has_hyphen(domain: str) -> bool:
    return any("-" in label for label in domain.split(".") if label)


class MoeMailClient:
    """
    Compatibility wrapper using the main project's GPTMail/ChatGPTMail flow.

    The old release registrator expected MoeMail methods. Keeping the class
    name avoids touching register.py, while create_email/list/get use GPTMail.
    """

    def __init__(self, base_url: str = "", api_key: str = ""):
        self.base_url = (base_url or config.GPTMAIL_BASE_URL).rstrip("/")
        self._http = httpx.AsyncClient(timeout=config.GPTMAIL_TIMEOUT, follow_redirects=False)
        self._inboxes: dict[str, dict[str, str]] = {}

    async def close(self):
        await self._http.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, config.GPTMAIL_RETRY_COUNT + 1):
            try:
                response = await self._http.request(method, url, **kwargs)
                if response.status_code >= 500 and attempt < config.GPTMAIL_RETRY_COUNT:
                    await asyncio.sleep(config.GPTMAIL_RETRY_DELAY * attempt)
                    continue
                response.raise_for_status()
                return response
            except Exception as exc:
                last_error = exc
                if attempt < config.GPTMAIL_RETRY_COUNT:
                    await asyncio.sleep(config.GPTMAIL_RETRY_DELAY * attempt)
                    continue
        raise RuntimeError(f"GPTMail request failed: {last_error}")

    async def add_subdomain(self, parent_domain: str) -> dict:
        return {"id": parent_domain or "gptmail", "name": parent_domain or "gptmail"}

    async def delete_domain(self, domain_id: str):
        return None

    async def create_email(self, domain: str = "", name: str = "") -> dict:
        headers = {
            **_headers(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }
        home = await self._request("GET", f"{self.base_url}/zh/", headers=headers)
        html = home.text
        gm_sid = home.cookies.get("gm_sid")
        auth = _extract_browser_auth_payload(html)
        if not gm_sid:
            raise RuntimeError("GPTMail did not return gm_sid cookie")
        if not auth or not auth.get("token"):
            raise RuntimeError("GPTMail page auth payload missing")

        token = auth["token"]
        api_headers = {
            **_headers(),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": f"{self.base_url}/zh/",
            "Cookie": f"gm_sid={gm_sid}",
            "X-Inbox-Token": token,
        }
        email = ""
        last_rejected = ""
        for attempt in range(1, config.GPTMAIL_GENERATE_EMAIL_ATTEMPTS + 1):
            generated = await self._request(
                "GET",
                f"{self.base_url}/api/generate-email",
                headers=api_headers,
            )
            data = generated.json()
            candidate = data.get("data", {}).get("email") or data.get("auth", {}).get("email") or ""
            token = data.get("auth", {}).get("token") or token
            if not candidate:
                raise RuntimeError(f"GPTMail did not return email: {generated.text[:200]}")

            domain_part = _email_domain(candidate)
            if _domain_has_hyphen(domain_part):
                last_rejected = candidate
                log.info(f"跳过带横杠域名邮箱({attempt}/{config.GPTMAIL_GENERATE_EMAIL_ATTEMPTS}): {candidate}")
                continue

            email = candidate
            break

        if not email:
            raise RuntimeError(
                f"GPTMail generated only hyphenated-domain emails after "
                f"{config.GPTMAIL_GENERATE_EMAIL_ATTEMPTS} attempts; last={last_rejected}"
            )

        bind_headers = {
            **_headers(),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/zh/{email}",
            "Cookie": f"gm_sid={gm_sid}",
        }
        bound = await self._request(
            "POST",
            f"{self.base_url}/api/inbox-token",
            headers=bind_headers,
            json={"email": email},
        )
        bound_data = bound.json()
        token = bound_data.get("auth", {}).get("token") or token

        inbox = {
            "id": email,
            "address": email,
            "email": email,
            "gm_sid": gm_sid,
            "token": token,
        }
        self._inboxes[email] = inbox
        return inbox

    async def delete_email(self, email_id: str):
        self._inboxes.pop(email_id, None)
        return None

    async def list_messages(self, email_id: str) -> list[dict]:
        inbox = self._inboxes.get(email_id, {})
        headers = {
            **_headers(),
            "Accept": "application/json",
            "Referer": f"{self.base_url}/zh/{email_id}",
            "Cookie": f"gm_sid={inbox.get('gm_sid', '')}",
            "X-Inbox-Token": inbox.get("token", ""),
        }
        response = await self._request(
            "GET",
            f"{self.base_url}/api/emails?email={email_id}",
            headers=headers,
        )
        data = response.json()
        if data.get("auth", {}).get("token") and email_id in self._inboxes:
            self._inboxes[email_id]["token"] = data["auth"]["token"]
        return data.get("data", {}).get("emails", [])

    async def get_message(self, email_id: str, message_id: str) -> dict:
        inbox = self._inboxes.get(email_id, {})
        headers = {
            **_headers(),
            "Accept": "application/json",
            "Referer": f"{self.base_url}/zh/{email_id}",
            "Cookie": f"gm_sid={inbox.get('gm_sid', '')}",
            "X-Inbox-Token": inbox.get("token", ""),
        }
        response = await self._request(
            "GET",
            f"{self.base_url}/api/email/{message_id}",
            headers=headers,
        )
        data = response.json()
        if data.get("auth", {}).get("token") and email_id in self._inboxes:
            self._inboxes[email_id]["token"] = data["auth"]["token"]
        message = data.get("data", data)
        return {
            **message,
            "html": message.get("html_content") or message.get("html") or message.get("content") or "",
            "text": message.get("text") or message.get("content") or "",
        }

    async def wait_for_message(
        self,
        email_id: str,
        timeout: float = 90,
        interval: float = 3,
        subject_contains: str = "",
    ) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            msgs = await self.list_messages(email_id)
            for msg in msgs:
                subject = msg.get("subject", "")
                sender = msg.get("from_address", "")
                if subject_contains and subject_contains.lower() not in subject.lower():
                    continue
                if not re.search(r"confirm|verify|signup|welcome", f"{subject} {sender}", re.I):
                    continue
                html = msg.get("html_content") or msg.get("content") or ""
                if extract_verify_link(html, required=False):
                    return {"html": html, "text": msg.get("text", "")}
                return await self.get_message(email_id, msg["id"])
            await asyncio.sleep(interval)
        raise TimeoutError(f"等待邮件超时 ({timeout}s): {email_id}")


def extract_verify_link(html: str, required: bool = True) -> str:
    text = (html or "").replace("&amp;", "&")
    patterns = [
        r'href="(https://auth\.vapi\.ai/auth/v1/verify\?[^"]+)"',
        r"https://auth\.vapi\.ai/auth/v1/verify\?[^\"'<>\s]+",
        r"https?://[^\"'<>\s]*verify[^\"'<>\s]*token=[^\"'<>\s]+",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return (match.group(1) if match.lastindex else match.group(0)).replace("&amp;", "&")
    if required:
        raise ValueError("邮件中未找到验证链接")
    return ""
