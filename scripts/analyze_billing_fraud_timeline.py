#!/usr/bin/env python3
"""Analyze billing anti-fraud/source-token timing from browser-bind-debug logs.

Read-only diagnostic. Redacts card/token values already present in logs and focuses on
Dodgeball sourceToken, Stripe Radar/hcaptcha/human_security, r.stripe telemetry and
Vapi add-card ordering.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
from pathlib import Path
from typing import Any

KNOWN_GOOD_ADD_CARD_DASHBOARD_VERSION = "daily-2026-06-09-1400"
KNOWN_GOOD_STRIPE_PM_USER_AGENT_VERSION = "ab68db42e2"


def parse_ts(raw: Any) -> float | None:
    text = str(raw or "")
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return dt.datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


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


def header_value(headers: dict[str, Any] | None, name: str) -> Any:
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return value
    return ""


def header_text(headers: dict[str, Any] | None, name: str) -> str:
    value = header_value(headers, name)
    if isinstance(value, dict):
        return str(value.get("prefix") or value.get("sha256") or "redacted")
    return str(value or "")


def safe_sha(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("sha256") or "")
    return ""


def post_dict(ev: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(ev, dict):
        return {}
    post = ev.get("postData")
    return post if isinstance(post, dict) else {}


def redacted_tail(value: Any) -> tuple[str, str, str]:
    if isinstance(value, dict):
        return str(value.get("sha256") or "")[:12], str(value.get("prefix") or ""), str(value.get("suffix") or "")
    return "", "", ""


def classify(status: Any, body: Any, last_error: str = "") -> str:
    text = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body or last_error or "")
    lowered = text.lower()
    if str(status) in {"200", "201"}:
        return "ok"
    if "card was declined" in lowered or "card_declined" in lowered or "decline_code" in lowered:
        return "card_declined"
    if "couldn't attach payment method" in lowered or "couldn’t attach payment method" in lowered:
        return "attach_rejected"
    if status is None:
        return "no_add_card"
    return "unknown"


def rstripe_event_names(ev: dict[str, Any]) -> list[str]:
    post = post_dict(ev)
    raw = post.get("events") or ""
    if isinstance(raw, dict):
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


def summarize_file(path: Path) -> dict[str, Any] | None:
    data = load_json(path)
    if not data:
        return None
    events = [ev for ev in (data.get("events") or []) if isinstance(ev, dict)]
    add_req = add_resp = stripe_req = stripe_resp = None
    source_req_indices: list[int] = []
    source_resp_statuses: list[Any] = []
    rstripe_names: list[str] = []
    for idx, ev in enumerate(events):
        url = str(ev.get("url") or "")
        typ = ev.get("type")
        if "api.stripe.com/v1/payment_methods" in url:
            if typ == "request":
                stripe_req = ev | {"_idx": idx}
            elif typ == "response":
                stripe_resp = ev | {"_idx": idx}
        elif "/stripe/add-card" in url:
            if typ == "request":
                add_req = ev | {"_idx": idx}
            elif typ == "response":
                add_resp = ev | {"_idx": idx}
        elif "api.dodgeballhq.com/v1/sourceToken" in url:
            if typ == "request":
                source_req_indices.append(idx)
            elif typ == "response":
                source_resp_statuses.append(ev.get("status"))
        elif "r.stripe.com/b" in url and typ == "request":
            rstripe_names.extend(rstripe_event_names(ev))

    add_status = (add_resp or {}).get("status")
    add_body = (add_resp or {}).get("body")
    category = classify(add_status, add_body, str(data.get("lastError") or ""))
    stripe_post = post_dict(stripe_req)
    add_headers = (add_req or {}).get("headers") if isinstance((add_req or {}).get("headers"), dict) else {}
    add_idx = (add_req or {}).get("_idx")
    stripe_idx = (stripe_req or {}).get("_idx")
    dash = header_text(add_headers, "x-dashboard-version")
    pua = str(stripe_post.get("payment_user_agent") or "")
    card_sha, card_prefix, card_suffix = redacted_tail(stripe_post.get("card[number]"))
    hcaptcha = "radar_options[hcaptcha_token]" in stripe_post
    human_keys = sorted(k for k in stripe_post if str(k).startswith("radar_options[human_security") or str(k).startswith("radar_options[px"))
    source_between_stripe_add = 0
    source_before_add = 0
    if isinstance(add_idx, int):
        source_before_add = sum(1 for idx in source_req_indices if idx < add_idx)
        if isinstance(stripe_idx, int):
            source_between_stripe_add = sum(1 for idx in source_req_indices if stripe_idx < idx < add_idx)
    refresh = data.get("dodgeballRefresh") if isinstance(data.get("dodgeballRefresh"), list) else []
    before_refresh = [r for r in refresh if isinstance(r, dict) and r.get("label") == "before-add-card"]
    active_before = any(bool(r.get("activeGenerated")) for r in before_refresh)
    last_refresh = next((r for r in reversed(refresh) if isinstance(r, dict) and r.get("tokenSha256")), {})
    add_device_sha = safe_sha(header_value(add_headers, "x-device-fingerprint-token"))
    ctx_dodge_sha = str((last_refresh or {}).get("tokenSha256") or "")
    ctx_device_sha = str((last_refresh or {}).get("deviceTokenSha256") or "")
    source_status_ok = any(str(s) == "201" for s in source_resp_statuses)
    event_counter = collections.Counter(rstripe_names)
    captcha_event = any("captcha" in name for name in rstripe_names)
    consume_event = any("consume_token" in name for name in rstripe_names)
    return {
        "file": path.name,
        "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%F %T"),
        "tail": str(data.get("cardTail") or ""),
        "cardSha": card_sha,
        "cardPrefix": card_prefix,
        "cardSuffix": card_suffix,
        "proxy": data.get("billingProxy") or "",
        "addStatus": add_status,
        "stripeStatus": (stripe_resp or {}).get("status"),
        "category": category,
        "dashboard": dash,
        "puaVersion": pua.split(";", 1)[0] if pua else "",
        "knownGoodCombo": dash == KNOWN_GOOD_ADD_CARD_DASHBOARD_VERSION and KNOWN_GOOD_STRIPE_PM_USER_AGENT_VERSION in pua,
        "hcaptcha": hcaptcha,
        "humanKeyCount": len(human_keys),
        "humanKeys": human_keys[:8],
        "timeOnPage": stripe_post.get("time_on_page") or "",
        "beforeAddCardDelayMs": data.get("beforeAddCardDelayMs") or "",
        "sourceReqCount": len(source_req_indices),
        "sourceRespStatuses": source_resp_statuses,
        "sourceStatusOk": source_status_ok,
        "sourceBeforeAdd": source_before_add,
        "sourceBetweenStripeAndAdd": source_between_stripe_add,
        "refreshLabels": [r.get("label") for r in refresh if isinstance(r, dict)],
        "activeBeforeAddRefresh": active_before,
        "addDeviceSha": add_device_sha[:12],
        "lastRefreshTokenSha": ctx_dodge_sha[:12],
        "lastRefreshDeviceSha": ctx_device_sha[:12],
        "deviceMatchesRefreshToken": bool(add_device_sha and ctx_dodge_sha and add_device_sha == ctx_dodge_sha),
        "deviceMatchesRefreshDevice": bool(add_device_sha and ctx_device_sha and add_device_sha == ctx_device_sha),
        "rstripeEventCount": len(rstripe_names),
        "rstripeCaptchaEvent": captcha_event,
        "rstripeConsumeEvent": consume_event,
        "topRstripeEvents": event_counter.most_common(5),
        "lastError": str(data.get("lastError") or "")[:180],
    }


def bucket_number(value: Any, size: int = 5000) -> str:
    try:
        n = int(float(str(value)))
    except Exception:
        return "none"
    lo = (n // size) * size
    hi = lo + size - 1
    return f"{lo}-{hi}"


def print_counter(rows: list[dict[str, Any]], key: str, title: str | None = None, limit: int = 10) -> None:
    c = collections.Counter(str(r.get(key)) for r in rows)
    print(f"{title or key}: " + ", ".join(f"{k}={v}" for k, v in c.most_common(limit)))


def compare(rows: list[dict[str, Any]]) -> None:
    ok = [r for r in rows if r.get("category") == "ok"]
    bad = [r for r in rows if r.get("category") == "card_declined"]
    print("\ncompare ok_vs_card_declined:")
    for key in (
        "knownGoodCombo", "sourceStatusOk", "sourceBeforeAdd", "sourceBetweenStripeAndAdd",
        "activeBeforeAddRefresh", "deviceMatchesRefreshToken", "deviceMatchesRefreshDevice",
        "hcaptcha", "humanKeyCount", "rstripeCaptchaEvent", "rstripeConsumeEvent", "dashboard", "puaVersion",
    ):
        okc = collections.Counter(str(r.get(key)) for r in ok).most_common(6)
        badc = collections.Counter(str(r.get(key)) for r in bad).most_common(6)
        print(f"{key}: ok={okc} bad={badc}")
    for key, size in (("timeOnPage", 5000), ("beforeAddCardDelayMs", 3000)):
        okc = collections.Counter(bucket_number(r.get(key), size) for r in ok).most_common(8)
        badc = collections.Counter(bucket_number(r.get(key), size) for r in bad).most_common(8)
        print(f"{key}_bucket: ok={okc} bad={badc}")


def print_timeline(rows: list[dict[str, Any]], focus_tail: str = "", limit_cards: int = 12) -> None:
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        key = row.get("cardSha") or f"tail:{row.get('tail')}"
        grouped[str(key)].append(row)
    mixed = []
    for key, items in grouped.items():
        cats = collections.Counter(r.get("category") for r in items)
        if focus_tail and not any(str(r.get("tail")) == focus_tail for r in items):
            continue
        if focus_tail or (cats.get("ok") and cats.get("card_declined")):
            mixed.append((items[-1].get("mtime", ""), key, cats, items))
    print("\ncard_timeline:")
    for _, key, cats, items in mixed[-limit_cards:]:
        print(f"card={key} tail={items[-1].get('tail')} counts={dict(cats)} first={items[0]['mtime']}:{items[0]['category']} last={items[-1]['mtime']}:{items[-1]['category']}")
        first_decl = next((idx for idx, item in enumerate(items) if item.get("category") == "card_declined"), 0)
        for item in items[max(0, first_decl - 3): min(len(items), first_decl + 5)]:
            print(
                f"  {item['mtime']} {item['category']} add={item.get('addStatus')} dash={item.get('dashboard') or '-'} "
                f"pua={item.get('puaVersion') or '-'} src={item.get('sourceBeforeAdd')}/{item.get('sourceReqCount')} "
                f"active={item.get('activeBeforeAddRefresh')} devMatch=tok:{item.get('deviceMatchesRefreshToken')} dev:{item.get('deviceMatchesRefreshDevice')} "
                f"hcap={item.get('hcaptcha')} human={item.get('humanKeyCount')} rcap={item.get('rstripeCaptchaEvent')} file={item['file']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug-dir", default="")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--show", type=int, default=20)
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--timeline", action="store_true")
    parser.add_argument("--tail", default="", help="focus a card last4 in --timeline")
    args = parser.parse_args()

    debug_dir = pick_debug_dir(args.debug_dir)
    files = sorted((Path(p) for p in glob.glob(str(debug_dir / "network-*.json"))), key=lambda p: p.stat().st_mtime)[-max(args.limit, 1):]
    rows = [row for path in files if (row := summarize_file(path))]
    print(f"debug_dir={debug_dir} files={len(files)} parsed={len(rows)}")
    if not rows:
        return 0
    print_counter(rows, "category")
    print_counter(rows, "addStatus")
    print_counter(rows, "sourceReqCount")
    print_counter(rows, "activeBeforeAddRefresh")
    print_counter(rows, "deviceMatchesRefreshToken")
    print_counter(rows, "deviceMatchesRefreshDevice")
    print_counter(rows, "rstripeCaptchaEvent")
    print_counter(rows, "rstripeConsumeEvent")
    if args.compare:
        compare(rows)
    if args.timeline or args.tail:
        print_timeline(rows, args.tail)
    print("\nrecent:")
    for r in rows[-max(args.show, 0):]:
        print(
            f"{r['mtime']} {r['file']} tail={r['tail'] or '-'} cat={r['category']} add={r['addStatus'] or '-'} "
            f"stripe={r['stripeStatus'] or '-'} dash={r['dashboard'] or '-'} pua={r['puaVersion'] or '-'} "
            f"src={r['sourceBeforeAdd']}/{r['sourceReqCount']} active={r['activeBeforeAddRefresh']} "
            f"devMatch=tok:{r['deviceMatchesRefreshToken']} dev:{r['deviceMatchesRefreshDevice']} "
            f"hcap={r['hcaptcha']} human={r['humanKeyCount']} rcap={r['rstripeCaptchaEvent']} consume={r['rstripeConsumeEvent']} "
            f"delay={r['beforeAddCardDelayMs'] or '-'} top={r['timeOnPage'] or '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
