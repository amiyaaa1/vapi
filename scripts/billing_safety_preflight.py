#!/usr/bin/env python3
"""Billing safety preflight before enabling auto topup.

Summarizes current config, card quarantine state, attach-risk cooldown and recent
add-card results without exposing PAN/CVC/token values.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import time
from pathlib import Path
from typing import Any

KNOWN_GOOD_ADD_CARD_DASHBOARD_VERSION = "daily-2026-06-09-1400"
KNOWN_GOOD_STRIPE_PM_USER_AGENT_VERSION = "ab68db42e2"


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def classify_text(text: str) -> str:
    lowered = str(text or "").lower()
    if "card was declined" in lowered or "card_declined" in lowered or "decline_code" in lowered:
        return "card_declined"
    if "couldn't attach payment method" in lowered or "couldn’t attach payment method" in lowered:
        return "attach_rejected"
    if "invalid csrf" in lowered or "csrf" in lowered:
        return "csrf_or_session"
    return "unknown"


def recent_add_card(debug_dir: Path, limit: int) -> dict[str, Any]:
    files = sorted(glob.glob(str(debug_dir / "network-*.json")), key=os.path.getmtime)[-max(limit, 1):]
    counts: dict[str, int] = {}
    latest = []
    for raw in files:
        path = Path(raw)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        statuses = []
        bodies = [str(data.get("lastError") or "")]
        for ev in data.get("events") or []:
            if isinstance(ev, dict) and ev.get("type") == "response" and "/stripe/add-card" in str(ev.get("url") or ""):
                statuses.append(ev.get("status"))
                body = ev.get("body")
                bodies.append(json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body or ""))
        if statuses:
            key = str(statuses[-1])
        elif data.get("lastError"):
            key = "error_no_response"
        else:
            key = "no_add_card"
        cat = classify_text("\n".join(bodies))
        counts[f"{key}:{cat}"] = counts.get(f"{key}:{cat}", 0) + 1
        latest.append({
            "file": path.name,
            "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%F %T"),
            "tail": str(data.get("cardTail") or ""),
            "status": key,
            "category": cat,
            "proxy": data.get("billingProxy") or "",
            "error": str(data.get("lastError") or "")[:160],
        })
    return {"counts": counts, "latest": latest[-8:]}


def quarantine_summary(state_path: Path) -> dict[str, Any]:
    state = load_json(state_path)
    now = time.time()
    active = []
    expired = []
    for rec in (state.get("cards") or {}).values():
        if not isinstance(rec, dict):
            continue
        item = {
            "tail": rec.get("tail") or "",
            "declineCount": rec.get("declineCount") or 0,
            "until": rec.get("quarantinedUntilIso") or "",
            "reason": str(rec.get("lastReason") or "")[:140],
        }
        if float(rec.get("quarantinedUntil", 0) or 0) > now:
            active.append(item)
        else:
            expired.append(item)
    active.sort(key=lambda x: str(x.get("tail")))
    return {"activeCount": len(active), "expiredCount": len(expired), "active": active}


def risk_summary(path: Path) -> dict[str, Any]:
    state = load_json(path)
    cooldown_until = float(state.get("cooldownUntil", 0) or 0)
    remaining = max(0.0, cooldown_until - time.time())
    return {
        "consecutive400": state.get("consecutive400", 0),
        "threshold": state.get("threshold"),
        "cooldownRemainingSeconds": int(remaining),
        "lastReason": str(state.get("lastReason") or "")[:160],
        "lastAt": state.get("lastAt") or "",
    }




def _boolish(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _intish(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _camel_to_env(key: str) -> str:
    out = []
    for ch in key:
        if ch.isupper():
            out.append("_")
        out.append(ch.upper())
    return "".join(out).strip("_")


def _config_or_env(config: dict[str, Any], key: str, default: Any = "") -> Any:
    value = os.getenv(_camel_to_env(key))
    if value is not None and value != "":
        return value
    if key in config:
        return config.get(key)
    return default


def configured_card_like_count(config: dict[str, Any]) -> int:
    count = 0
    if str(config.get("billingCardNumber") or "").strip():
        count += 1
    pool = config.get("billingCardPool") or ""
    if isinstance(config.get("billingCards"), list):
        count += len(config.get("billingCards") or [])
    if isinstance(pool, str) and pool.strip():
        try:
            parsed = json.loads(pool)
            if isinstance(parsed, list):
                count += len(parsed)
            elif isinstance(parsed, dict):
                cards = parsed.get("cards")
                count += len(cards) if isinstance(cards, list) else 1
            return count
        except Exception:
            count += len([p for p in re.split(r"[;\n]+", pool) if p.strip()])
    return count


def generator_summary(config: dict[str, Any]) -> dict[str, Any]:
    enabled = _boolish(_config_or_env(config, "billingCardGeneratorEnabled", False))
    allow_live = _boolish(_config_or_env(config, "billingCardGeneratorAllowLive", False))
    only = _boolish(_config_or_env(config, "billingCardGeneratorOnly", False))
    mock_context = any(_boolish(os.getenv(name), False) for name in ("BILLING_TEST_MODE", "BILLING_MOCK_STRIPE_PM", "BILLING_MOCK_ADD_CARD"))
    prefixes = str(_config_or_env(config, "billingCardGeneratorPrefixes", "") or "")
    count = max(0, min(50, _intish(_config_or_env(config, "billingCardGeneratorCount", 5), 5))) if enabled else 0
    active = enabled and (mock_context or allow_live)
    return {
        "enabled": enabled,
        "active": active,
        "mockContext": mock_context,
        "allowLive": allow_live,
        "only": only,
        "count": count,
        "prefixes": prefixes or "auto",
        "useConfigPrefixes": _boolish(_config_or_env(config, "billingCardGeneratorUseConfigPrefixes", True), True),
        "prefixDigits": max(1, min(12, _intish(_config_or_env(config, "billingCardGeneratorPrefixDigits", 6), 6))),
        "configuredCardLikeCount": configured_card_like_count(config),
        "blockedReason": "requires BILLING_TEST_MODE/mock or BILLING_CARD_GENERATOR_ALLOW_LIVE=1" if enabled and not active else "",
    }

def env_or_config_flag(config: dict[str, Any], key: str, default: str = "") -> str:
    cmd = str(config.get("autoTopupCommand") or "")
    m = re.search(rf"{re.escape(key)}=([^ ]+)", cmd)
    if m:
        return m.group(1).strip('"')
    return os.getenv(key, default)


def resolved_flag_value(value: str) -> str:
    """Resolve simple shell-default fragments like ${KEY:-value} for preflight display."""
    text = str(value or "").strip().strip('"')
    m = re.fullmatch(r"\$\{[^}:]+:-([^}]*)\}", text)
    if m:
        return m.group(1).strip()
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/data/config.json")
    parser.add_argument("--debug-dir", default="/data/browser-bind-debug")
    parser.add_argument("--decline-state", default="/data/billing-card-declines.json")
    parser.add_argument("--risk-state", default="/data/billing-attach-risk-state.json")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--strict", action="store_true", help="warnings 非空时返回 2；默认只报告并返回 0")
    args = parser.parse_args()

    config = load_json(Path(args.config))
    quarantine = quarantine_summary(Path(args.decline_state))
    risk = risk_summary(Path(args.risk_state))
    recent = recent_add_card(Path(args.debug_dir), args.limit)
    generator = generator_summary(config)
    checks = {
        "autoTopupEnabled": bool(config.get("autoTopupEnabled")),
        "stopOnCardDeclined": env_or_config_flag(config, "BILLING_STOP_ON_CARD_DECLINED", os.getenv("BILLING_STOP_ON_CARD_DECLINED", "")),
        "cardQuarantineDisabled": env_or_config_flag(config, "BILLING_CARD_DECLINE_QUARANTINE_DISABLED", os.getenv("BILLING_CARD_DECLINE_QUARANTINE_DISABLED", "")),
        "browserCardDeclinedAttempts": env_or_config_flag(config, "BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS", os.getenv("BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS", "")),
        "addCardDashboardVersionOverride": env_or_config_flag(config, "BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE", os.getenv("BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE", "")),
        "stripePmUserAgentVersion": env_or_config_flag(config, "BILLING_STRIPE_PM_USER_AGENT_VERSION", os.getenv("BILLING_STRIPE_PM_USER_AGENT_VERSION", "")),
        "refreshDodgeballBeforeAddCard": env_or_config_flag(config, "BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD", os.getenv("BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD", "")),
    }
    resolved_checks = {key: resolved_flag_value(str(value)) for key, value in checks.items() if key != "autoTopupEnabled"}
    warnings = []
    if resolved_checks.get("stopOnCardDeclined") not in {"1", "true", "TRUE", "yes", "on"}:
        warnings.append("BILLING_STOP_ON_CARD_DECLINED is not enabled")
    if resolved_checks.get("cardQuarantineDisabled") in {"1", "true", "TRUE", "yes", "on"}:
        warnings.append("card-decline quarantine is disabled")
    dash = resolved_checks.get("addCardDashboardVersionOverride", "")
    if dash != KNOWN_GOOD_ADD_CARD_DASHBOARD_VERSION:
        warnings.append(f"add-card dashboard version is not known-good {KNOWN_GOOD_ADD_CARD_DASHBOARD_VERSION}: {dash or 'unset'}")
    pua = resolved_checks.get("stripePmUserAgentVersion", "")
    if KNOWN_GOOD_STRIPE_PM_USER_AGENT_VERSION not in pua:
        warnings.append(f"Stripe PM user-agent version is not known-good {KNOWN_GOOD_STRIPE_PM_USER_AGENT_VERSION}: {pua or 'unset'}")
    if resolved_checks.get("refreshDodgeballBeforeAddCard") in {"1", "true", "TRUE", "yes", "on"}:
        warnings.append("BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD is enabled; recent evidence favors natural page sourceToken only")
    if risk["cooldownRemainingSeconds"]:
        warnings.append(f"attach 400 cooldown active for {risk['cooldownRemainingSeconds']}s")
    if quarantine["activeCount"] == 0:
        warnings.append("no active card quarantine records")
    if generator.get("enabled") and not generator.get("active"):
        warnings.append("billing card generator requested but inactive outside BILLING_TEST_MODE/mock; set BILLING_CARD_GENERATOR_ALLOW_LIVE=1 only for explicit live A/B")
    if generator.get("enabled") and generator.get("allowLive") and not generator.get("mockContext"):
        warnings.append("billing card generator live override is enabled")

    print(json.dumps({
        "checks": checks,
        "resolvedChecks": resolved_checks,
        "knownGood": {
            "addCardDashboardVersionOverride": KNOWN_GOOD_ADD_CARD_DASHBOARD_VERSION,
            "stripePmUserAgentVersion": KNOWN_GOOD_STRIPE_PM_USER_AGENT_VERSION,
        },
        "warnings": warnings,
        "billingCardGenerator": generator,
        "risk": risk,
        "quarantine": quarantine,
        "recentAddCard": recent,
    }, ensure_ascii=False, indent=2))
    return 2 if args.strict and warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())
