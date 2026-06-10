#!/usr/bin/env python3
"""Send Vapi chat requests with curl_cffi browser TLS impersonation."""
import json
import os
import sys
from typing import Any

from curl_cffi import requests as cffi_requests


def env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


IMPERSONATE = env("CURL_CFFI_IMPERSONATE", "chrome131")
SOCKS5_PROXY = env("SOCKS5_PROXY", "")


def normalize_proxy(proxy: str) -> str:
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    return "socks5://" + proxy


def write_meta(status_code: int, headers: dict[str, str]) -> None:
    payload = {"status_code": status_code, "headers": headers}
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def safe_json_dumps(payload: Any) -> str:
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return (
        body.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def make_session() -> cffi_requests.Session:
    session = cffi_requests.Session(impersonate=IMPERSONATE)
    proxy = normalize_proxy(SOCKS5_PROXY)
    if proxy:
        session.proxies = {
            "http": proxy,
            "https": proxy,
        }
    return session


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
        request: dict[str, Any] = json.loads(raw.decode("utf-8"))
        url = str(request["url"])
        payload = request["payload"]
        headers = {str(k): str(v) for k, v in request.get("headers", {}).items() if v is not None}
        stream = bool(request.get("stream", False))
        timeout = float(request.get("timeout", 300))

        session = make_session()
        body = safe_json_dumps(payload)
        response = session.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            stream=stream,
            timeout=timeout,
        )
        write_meta(response.status_code, dict(response.headers))

        if stream:
            for chunk in response.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        else:
            sys.stdout.buffer.write(response.content)
            sys.stdout.buffer.flush()
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
