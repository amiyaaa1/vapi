#!/usr/bin/env python3
"""Seed billing card-decline quarantine records from existing add-card diagnostics.

This is a stop-loss helper: it only writes quarantine metadata for cards already
present in the local config when recent network logs show explicit Stripe
card_declined responses for matching card tails. It never prints full PAN/CVC.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any


def sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def normalize_year(year: str) -> str:
    year = str(year or "").strip()
    if len(year) == 2 and year.isdigit():
        current = dt.datetime.now(dt.timezone.utc).year
        century = current - current % 100
        value = century + int(year)
        if value < current - 5:
            value += 100
        return str(value)
    return year


def normalize_card(number: str = "", expiry: str = "", cvc: str = "", exp_month: str = "", exp_year: str = "") -> dict[str, str]:
    number = re.sub(r"[\s-]+", "", str(number or "").strip())
    expiry = str(expiry or "").strip()
    exp_month = str(exp_month or "").strip()
    exp_year = str(exp_year or "").strip()
    if expiry and (not exp_month or not exp_year):
        compact = expiry.replace(" ", "").replace("-", "/")
        if "/" in compact:
            left, right = compact.split("/", 1)
            exp_month = exp_month or left
            exp_year = exp_year or right
        elif len(compact) in (4, 6):
            exp_month = exp_month or compact[:2]
            exp_year = exp_year or compact[2:]
    exp_year = normalize_year(exp_year)
    return {
        "number": number,
        "exp_month": exp_month.zfill(2) if exp_month else "",
        "exp_year": exp_year,
        "cvc": str(cvc or "").strip(),
    }


def parse_card_entry(entry: Any) -> dict[str, str]:
    if isinstance(entry, dict):
        return normalize_card(
            number=entry.get("number") or entry.get("cardNumber") or entry.get("billingCardNumber") or "",
            expiry=entry.get("expiry") or entry.get("exp") or entry.get("billingCardExpiry") or "",
            cvc=entry.get("cvc") or entry.get("cvv") or entry.get("billingCardCvc") or "",
            exp_month=entry.get("exp_month") or entry.get("expMonth") or entry.get("month") or "",
            exp_year=entry.get("exp_year") or entry.get("expYear") or entry.get("year") or "",
        )
    text = str(entry or "").strip()
    if not text:
        return normalize_card()
    parts = [part.strip() for part in re.split(r"[|,\t ]+", text) if part.strip()]
    if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit():
        return normalize_card(number=parts[0], exp_month=parts[1], exp_year=parts[2], cvc=parts[3])
    if len(parts) >= 3:
        return normalize_card(number=parts[0], expiry=parts[1], cvc=parts[2])
    return normalize_card()


def configured_cards(config_path: Path, include_primary: bool = False) -> list[dict[str, str]]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    entries: list[Any] = []
    raw_pool = data.get("billingCardPool") or ""
    if isinstance(raw_pool, str) and raw_pool.strip():
        try:
            parsed = json.loads(raw_pool)
            if isinstance(parsed, list):
                entries.extend(parsed)
            elif isinstance(parsed, dict):
                entries.extend(parsed.get("cards") if isinstance(parsed.get("cards"), list) else [parsed])
        except Exception:
            entries.extend(part for part in re.split(r"[;\n]+", raw_pool) if part.strip())
    if isinstance(data.get("billingCards"), list):
        entries.extend(data["billingCards"])
    cards = [parse_card_entry(entry) for entry in entries]
    primary = normalize_card(
        number=data.get("billingCardNumber") or "",
        expiry=data.get("billingCardExpiry") or "",
        cvc=data.get("billingCardCvc") or "",
        exp_month=data.get("billingCardExpMonth") or "",
        exp_year=data.get("billingCardExpYear") or "",
    )
    if include_primary and all(primary.values()):
        cards.insert(0, primary)
    out: list[dict[str, str]] = []
    seen = set()
    for card in cards:
        if not all(card.values()):
            continue
        key = (card["number"], card["exp_month"], card["exp_year"], card["cvc"])
        if key in seen:
            continue
        seen.add(key)
        out.append(card)
    return out


def log_card_declined_tails(debug_dir: Path, limit: int) -> dict[str, int]:
    files = sorted(glob.glob(str(debug_dir / "network-*.json")), key=os.path.getmtime)[-max(limit, 1):]
    tails: dict[str, int] = {}
    for raw_path in files:
        path = Path(raw_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        text_parts = [str(data.get("lastError") or "")]
        for ev in data.get("events") or []:
            if isinstance(ev, dict) and ev.get("type") == "response" and "/stripe/add-card" in str(ev.get("url") or ""):
                text_parts.append(json.dumps(ev.get("body"), ensure_ascii=False) if isinstance(ev.get("body"), (dict, list)) else str(ev.get("body") or ""))
        lowered = "\n".join(text_parts).lower()
        if "card was declined" not in lowered and "card_declined" not in lowered and "decline_code" not in lowered:
            continue
        tail = str(data.get("cardTail") or "")[-4:]
        if re.fullmatch(r"\d{4}", tail):
            tails[tail] = tails.get(tail, 0) + 1
    return tails


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/root/docker/vapi2api/data/config.json")
    parser.add_argument("--debug-dir", default="/root/docker/vapi2api/data/browser-bind-debug")
    parser.add_argument("--state", default="/root/docker/vapi2api/data/billing-card-declines.json")
    parser.add_argument("--limit", type=int, default=3000)
    parser.add_argument("--seconds", type=int, default=24 * 3600)
    parser.add_argument("--include-primary", action="store_true", default=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    debug_dir = Path(args.debug_dir)
    state_path = Path(args.state)
    tails = log_card_declined_tails(debug_dir, args.limit)
    cards = configured_cards(config_path, include_primary=args.include_primary)
    cards_by_tail: dict[str, list[dict[str, str]]] = {}
    for card in cards:
        cards_by_tail.setdefault(card["number"][-4:], []).append(card)

    now = time.time()
    until = now + max(0, args.seconds)
    state = load_state(state_path)
    recs = state.setdefault("cards", {})
    if not isinstance(recs, dict):
        recs = {}
        state["cards"] = recs

    seeded = []
    for tail, count in sorted(tails.items()):
        for card in cards_by_tail.get(tail, []):
            key = sha256(card["number"])
            old = recs.get(key) if isinstance(recs.get(key), dict) else {}
            decline_count = max(int(old.get("declineCount", 0) or 0), count)
            recs[key] = {
                "cardKey": key,
                "keyMode": "pan",
                "tail": tail,
                "declineCount": decline_count,
                "lastDeclinedAt": now,
                "lastDeclinedAtIso": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                "lastReason": f"seeded from browser-bind-debug card_declined logs tail=****{tail} samples={count}",
                "quarantinedUntil": until,
                "quarantinedUntilIso": dt.datetime.fromtimestamp(until, dt.timezone.utc).isoformat(),
            }
            seeded.append({"tail": tail, "samples": count, "cardKey": key[:12] + "…" + key[-8:]})

    state["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
    state["seededFromDebugAt"] = state["updatedAt"]
    state["seededFromDebugLimit"] = args.limit
    if not args.dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dryRun": args.dry_run, "declinedTails": tails, "configuredCards": len(cards), "seeded": seeded, "state": str(state_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
