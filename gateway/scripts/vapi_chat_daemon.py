#!/usr/bin/env python3
"""Persistent curl_cffi helper service for Vapi chat requests.

The Go gateway used to spawn vapi_chat.py once per client request.  This daemon
keeps curl_cffi imported and reuses idle curl_cffi sessions, while preserving the
same JSON request schema and streaming behaviour.
"""
import argparse
import json
import os
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from curl_cffi import requests as cffi_requests


def env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


IMPERSONATE = env("CURL_CFFI_IMPERSONATE", "chrome131")
SOCKS5_PROXY = env("SOCKS5_PROXY", "")
IDLE_SESSIONS = int(env("VAPI_CHAT_HELPER_IDLE_SESSIONS", "128") or "128")
MAX_BODY_BYTES = int(env("VAPI_CHAT_HELPER_MAX_BODY_BYTES", "52428800") or "52428800")
BACKLOG = int(env("VAPI_CHAT_HELPER_BACKLOG", "1024") or "1024")


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    # curl_cffi may expose decompressed content while preserving the upstream
    # content-encoding header.  Do not forward it to the Go gateway.
    "content-encoding",
    "content-length",
}


def normalize_proxy(proxy: str) -> str:
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    return "socks5://" + proxy


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
        session.proxies = {"http": proxy, "https": proxy}
    return session


class SessionPool:
    """Unbounded active sessions with bounded idle reuse.

    This avoids lowering request concurrency while still eliminating process
    startup and import overhead.  Idle sessions are reused for connection reuse;
    if concurrency exceeds the idle pool, new sessions are created on demand.
    """

    def __init__(self, max_idle: int):
        self.max_idle = max(0, max_idle)
        self.idle: queue.LifoQueue[cffi_requests.Session] = queue.LifoQueue(maxsize=self.max_idle or 1)
        self.created = 0
        self.lock = threading.Lock()

    def acquire(self) -> cffi_requests.Session:
        if self.max_idle > 0:
            try:
                return self.idle.get_nowait()
            except queue.Empty:
                pass
        with self.lock:
            self.created += 1
        return make_session()

    def release(self, session: cffi_requests.Session, healthy: bool = True) -> None:
        if not healthy or self.max_idle <= 0:
            self.close(session)
            return
        try:
            self.idle.put_nowait(session)
        except queue.Full:
            self.close(session)

    def close(self, session: cffi_requests.Session) -> None:
        try:
            session.close()
        except Exception:
            pass


SESSION_POOL = SessionPool(IDLE_SESSIONS)


class HighConcurrencyThreadingHTTPServer(ThreadingHTTPServer):
    request_queue_size = max(128, BACKLOG)


class ChatHelperHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vapi-chat-helper/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        if env("VAPI_CHAT_HELPER_DEBUG", "0").lower() in ("1", "true", "yes", "on"):
            super().log_message(fmt, *args)

    def _send_json(self, status: int, payload: dict[str, Any], helper_error: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if helper_error:
            self.send_header("X-Vapi-Helper-Error", "1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/healthz":
            self._send_json(200, {"ok": True, "idleSessions": SESSION_POOL.idle.qsize(), "createdSessions": SESSION_POOL.created})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] not in ("/chat", "/v1/chat"):
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except Exception:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "empty request body"}, helper_error=True)
            return
        if MAX_BODY_BYTES > 0 and length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"}, helper_error=True)
            return

        session = None
        response = None
        healthy = True
        try:
            raw = self.rfile.read(length)
            request: dict[str, Any] = json.loads(raw.decode("utf-8"))
            url = str(request["url"])
            payload = request["payload"]
            headers = {str(k): str(v) for k, v in request.get("headers", {}).items() if v is not None}
            stream = bool(request.get("stream", False))
            timeout = float(request.get("timeout", 300))

            session = SESSION_POOL.acquire()
            body = safe_json_dumps(payload)
            response = session.post(
                url,
                data=body.encode("utf-8"),
                headers=headers,
                stream=stream,
                timeout=timeout,
            )

            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)

            if stream:
                # HTTP/1.1 responses without Content-Length must either use
                # chunked encoding or close the connection.  http.server does
                # not chunk automatically, so close the helper connection when
                # the upstream stream ends; non-stream requests still keep alive.
                self.close_connection = True
                self.send_header("X-Vapi-Helper-Mode", "daemon-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in response.iter_content(chunk_size=16384):
                    if not chunk:
                        continue
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                content = response.content
                self.send_header("X-Vapi-Helper-Mode", "daemon")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            healthy = False
        except Exception as exc:
            healthy = False
            try:
                self._send_json(502, {"error": str(exc)}, helper_error=True)
            except Exception:
                pass
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                healthy = False
            if session is not None:
                SESSION_POOL.release(session, healthy=healthy)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=env("VAPI_CHAT_HELPER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("VAPI_CHAT_HELPER_PORT", "8099") or "8099"))
    args = parser.parse_args()

    server = HighConcurrencyThreadingHTTPServer((args.host, args.port), ChatHelperHandler)
    server.daemon_threads = True
    print(
        f"vapi chat helper daemon listening on {args.host}:{args.port} "
        f"impersonate={IMPERSONATE} proxy={'on' if SOCKS5_PROXY else 'off'} "
        f"idleSessions={IDLE_SESSIONS} backlog={HighConcurrencyThreadingHTTPServer.request_queue_size}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
