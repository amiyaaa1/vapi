#!/usr/bin/env python3
"""Summarize billing add-card diagnostics without exposing full card/token data."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
from pathlib import Path
from typing import Any

KNOWN_GOOD_ADD_CARD_DASHBOARD_VERSION = "daily-2026-06-09-1400"
KNOWN_GOOD_STRIPE_PM_USER_AGENT_VERSION = "ab68db42e2"


def classify(status: Any, body: Any) -> str:
    if isinstance(body, (dict, list)):
        text = json.dumps(body, ensure_ascii=False)
    else:
        text = str(body or "")
    lowered = text.lower()
    if "invalid csrf" in lowered or "csrf" in lowered:
        return "csrf_or_session"
    if "card was declined" in lowered or "card_declined" in lowered or "decline_code" in lowered:
        return "card_declined"
    if "couldn't attach payment method" in lowered or "couldn’t attach payment method" in lowered:
        return "attach_rejected"
    if str(status) in {"401", "403"}:
        return "auth_or_session"
    if str(status) == "429":
        return "rate_limited"
    if str(status).startswith("5"):
        return "server_error"
    if str(status) in {"200", "201"}:
        return "ok"
    return "unknown"


def safe_body(body: Any, limit: int = 160) -> str:
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    return str(body or "").replace("\n", " ")[:limit]


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def pick_debug_dir(raw: str) -> Path:
    if raw:
        return Path(raw)
    for candidate in (Path("/root/docker/vapi2api/data/browser-bind-debug"), Path("/data/browser-bind-debug")):
        if candidate.exists():
            return candidate
    return Path("/data/browser-bind-debug")


def header_value(headers: dict[str, Any] | None, name: str) -> str:
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            if isinstance(value, dict):
                return str(value.get("prefix") or value.get("sha256") or "redacted")
            return str(value or "")
    return ""


def post_value(post: Any, key: str) -> str:
    if not isinstance(post, dict):
        return ""
    value = post.get(key)
    if isinstance(value, dict):
        if "prefix" in value or "suffix" in value:
            return f"{value.get('prefix','')}…{value.get('suffix','')}"
        return str(value.get("sha256") or "redacted")
    return str(value or "")


def interesting_events(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    add_events: list[dict[str, Any]] = []
    stripe_events: list[dict[str, Any]] = []
    add_requests = 0
    stripe_requests = 0
    for ev in data.get("events") or []:
        if not isinstance(ev, dict):
            continue
        url = str(ev.get("url") or "")
        typ = ev.get("type")
        if "/stripe/add-card" in url:
            if typ == "request":
                add_requests += 1
            add_events.append(ev)
        if "api.stripe.com/v1/payment_methods" in url:
            if typ == "request":
                stripe_requests += 1
            stripe_events.append(ev)
    return add_events, stripe_events, add_requests, stripe_requests


def summarize_file(path: Path) -> dict[str, Any] | None:
    data = load_json(path)
    if not data:
        return None
    add_events, stripe_events, add_requests, stripe_requests = interesting_events(data)
    add_responses = [ev for ev in add_events if ev.get("type") == "response"]
    add_request_events = [ev for ev in add_events if ev.get("type") == "request"]
    stripe_responses = [ev for ev in stripe_events if ev.get("type") == "response"]
    stripe_request_events = [ev for ev in stripe_events if ev.get("type") == "request"]
    add_last = add_responses[-1] if add_responses else {}
    add_req = add_request_events[-1] if add_request_events else {}
    stripe_last = stripe_responses[-1] if stripe_responses else {}
    stripe_req = stripe_request_events[-1] if stripe_request_events else {}
    response = data.get("response") if isinstance(data.get("response"), dict) else {}
    status = add_last.get("status") or response.get("status")
    body = add_last.get("body") or response.get("body") or data.get("lastError") or ""
    stripe_post = stripe_req.get("postData") if isinstance(stripe_req.get("postData"), dict) else {}
    add_headers = add_req.get("headers") if isinstance(add_req.get("headers"), dict) else {}
    dashboard_version = header_value(add_headers, "x-dashboard-version")
    if not dashboard_version and isinstance(data.get("billingRetryContext"), dict):
        dashboard_version = str((data.get("billingRetryContext") or {}).get("dashboardVersion") or "")
    if not dashboard_version and isinstance(data.get("headersAfter"), dict):
        dashboard_version = header_value(data.get("headersAfter"), "x-dashboard-version")

    route_post = data.get("postData") if isinstance(data.get("postData"), dict) else {}
    if not stripe_post and path.name.startswith("stripe-pm-route-"):
        stripe_post = route_post
    if not add_headers and isinstance(data.get("headersAfter"), dict):
        add_headers = data.get("headersAfter")

    payment_user_agent = post_value(stripe_post, "payment_user_agent")
    known_good_dashboard = dashboard_version == KNOWN_GOOD_ADD_CARD_DASHBOARD_VERSION
    known_good_pua = KNOWN_GOOD_STRIPE_PM_USER_AGENT_VERSION in payment_user_agent

    return {
        "file": path.name,
        "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%F %T"),
        "email": data.get("email") or "",
        "tail": str(data.get("cardTail") or ""),
        "proxy": data.get("billingProxy") or "",
        "retry": (data.get("billingRetryContext") or {}).get("retryIndex") if isinstance(data.get("billingRetryContext"), dict) else "",
        "dashboard_version": dashboard_version,
        "stripe_status": stripe_last.get("status"),
        "stripe_requests": stripe_requests,
        "add_status": status,
        "add_requests": add_requests,
        "category": classify(status, body),
        "payment_user_agent": payment_user_agent,
        "known_good_dashboard": known_good_dashboard,
        "known_good_pua": known_good_pua,
        "known_good_combo": known_good_dashboard and known_good_pua,
        "stripe_referrer": post_value(stripe_post, "referrer"),
        "time_on_page": post_value(stripe_post, "time_on_page"),
        "has_hcaptcha": "radar_options[hcaptcha_token]" in stripe_post,
        "has_human_security": any(str(k).startswith("radar_options[human_security") or str(k).startswith("radar_options[px") for k in stripe_post),
        "ua": header_value(add_headers, "user-agent"),
        "sec_ch_ua": header_value(add_headers, "sec-ch-ua"),
        "device_fp_len": (add_headers.get("x-device-fingerprint-token") or {}).get("length") if isinstance(add_headers.get("x-device-fingerprint-token"), dict) else len(str(add_headers.get("x-device-fingerprint-token") or "")),
        "body": safe_body(body),
        "last_error": safe_body(data.get("lastError"), 220),
    }


def display_value(value: Any) -> str:
    if value is None or value == "":
        return "none"
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def print_counter(rows: list[dict[str, Any]], label: str, key: str, limit: int = 12) -> None:
    counter = collections.Counter(display_value(row.get(key)) for row in rows)
    print(f"{label}: " + ", ".join(f"{k}={v}" for k, v in counter.most_common(limit)))


def print_compare(rows: list[dict[str, Any]]) -> None:
    ok = [r for r in rows if str(r.get("add_status")) in {"200", "201"}]
    bad = [r for r in rows if str(r.get("add_status")) == "400"]
    print("\ncompare ok_vs_400:")
    for key in ("dashboard_version", "payment_user_agent", "known_good_combo", "known_good_dashboard", "known_good_pua", "stripe_referrer", "has_hcaptcha", "has_human_security", "proxy", "sec_ch_ua"):
        ok_counter = collections.Counter(display_value(r.get(key)) for r in ok).most_common(5)
        bad_counter = collections.Counter(display_value(r.get(key)) for r in bad).most_common(5)
        print(f"{key}: ok={ok_counter} bad={bad_counter}")


def print_known_good_alignment(rows: list[dict[str, Any]]) -> None:
    ok = [r for r in rows if str(r.get("add_status")) in {"200", "201"}]
    bad = [r for r in rows if str(r.get("add_status")) == "400"]

    def count(items: list[dict[str, Any]], key: str) -> int:
        return sum(1 for row in items if bool(row.get(key)))

    print("\nknown_good_alignment:")
    print(f"target dashboard={KNOWN_GOOD_ADD_CARD_DASHBOARD_VERSION} stripe_pua_version={KNOWN_GOOD_STRIPE_PM_USER_AGENT_VERSION}")
    print(
        f"ok combo={count(ok, 'known_good_combo')}/{len(ok)} "
        f"dashboard={count(ok, 'known_good_dashboard')}/{len(ok)} "
        f"pua={count(ok, 'known_good_pua')}/{len(ok)}"
    )
    print(
        f"400 combo={count(bad, 'known_good_combo')}/{len(bad)} "
        f"dashboard={count(bad, 'known_good_dashboard')}/{len(bad)} "
        f"pua={count(bad, 'known_good_pua')}/{len(bad)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug-dir", default="", help="browser-bind-debug directory")
    parser.add_argument("--limit", type=int, default=120, help="recent file limit")
    parser.add_argument("--show", type=int, default=20, help="recent row count to print")
    parser.add_argument("--compare", action="store_true", help="print ok vs 400 comparison")
    args = parser.parse_args()

    debug_dir = pick_debug_dir(args.debug_dir)
    patterns = ["network-*.json", "same-browser-bind-*.json", "attach-browser-fetch-*.json", "add-card*.json"]
    files: list[Path] = []
    for pat in patterns:
        files.extend(Path(p) for p in glob.glob(str(debug_dir / pat)))
    files = sorted(set(files), key=lambda p: p.stat().st_mtime)[-max(args.limit, 1):]
    rows = [row for path in files if (row := summarize_file(path))]

    print(f"debug_dir={debug_dir} files={len(files)} parsed={len(rows)}")
    if not rows:
        return 0

    for label, key in (("add_status", "add_status"), ("category", "category"), ("proxy", "proxy"), ("stripe_status", "stripe_status"), ("payment_user_agent", "payment_user_agent"), ("dashboard_version", "dashboard_version")):
        print_counter(rows, label, key)

    if args.compare:
        print_compare(rows)
    print_known_good_alignment(rows)

    print("\nrecent:")
    for row in rows[-max(args.show, 0):]:
        print(
            f"{row['mtime']} {row['file']} tail={row['tail'] or '-'} proxy={row['proxy'] or '-'} "
            f"stripe={row['stripe_status'] or '-'} add={row['add_status'] or '-'} cat={row['category']} "
            f"pua={row['payment_user_agent'] or '-'} dash={row['dashboard_version'] or '-'} "
            f"kg={row.get('known_good_combo')} hcap={row['has_hcaptcha']} human={row['has_human_security']} "
            f"reqs(s/a)={row['stripe_requests']}/{row['add_requests']} err={row['last_error'] or row['body']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
