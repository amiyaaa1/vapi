import os
import json
import re
import random
from datetime import datetime, timezone
from pathlib import Path
try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*_args, **_kwargs):
        return False

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# CloakBrowser
CHROME_PATH = os.getenv("CHROME_PATH", "")
DEBUG_PORT_BASE = 9300

# GPTMail / ChatGPTMail
GPTMAIL_BASE_URL = os.getenv("GPTMAIL_BASE_URL", os.getenv("MAIL_BASE_URL", "https://mail.chatgpt.org.uk")).rstrip("/")
GPTMAIL_TIMEOUT = float(os.getenv("GPTMAIL_TIMEOUT", "30"))
GPTMAIL_RETRY_COUNT = int(os.getenv("GPTMAIL_RETRY_COUNT", "3"))
GPTMAIL_RETRY_DELAY = float(os.getenv("GPTMAIL_RETRY_DELAY", "1.5"))
GPTMAIL_GENERATE_EMAIL_ATTEMPTS = int(os.getenv("GPTMAIL_GENERATE_EMAIL_ATTEMPTS", "20"))

# easy_proxies
EASY_PROXY_URL = os.getenv("EASY_PROXY_URL", "http://127.0.0.1:9091")
EASY_PROXY_PASSWORD = os.getenv("EASY_PROXY_PASSWORD", "")

# Vapi
VAPI_REGISTER_URL = "https://dashboard.vapi.ai/register"
VAPI_TOKEN_API = "https://api.vapi.ai/token"


def _load_gateway_config() -> dict:
    config_path = os.getenv("CONFIG_PATH", "/data/config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


GATEWAY_CONFIG = _load_gateway_config()


def _config_value(env_names: tuple[str, ...], config_key: str, default: str = "") -> str:
    for name in env_names:
        value = os.getenv(name)
        if value:
            return value.strip()
    value = GATEWAY_CONFIG.get(config_key, default)
    return str(value).strip() if value is not None else default


def billing_card() -> dict:
    expiry = _config_value(
        ("BILLING_CARD_EXPIRY", "TOPUP_BILLING_CARD_EXPIRY"),
        "billingCardExpiry",
    )
    exp_month = _config_value(
        ("BILLING_CARD_EXP_MONTH", "TOPUP_BILLING_CARD_EXP_MONTH"),
        "billingCardExpMonth",
    )
    exp_year = _config_value(
        ("BILLING_CARD_EXP_YEAR", "TOPUP_BILLING_CARD_EXP_YEAR"),
        "billingCardExpYear",
    )

    if expiry and (not exp_month or not exp_year):
        compact = expiry.replace(" ", "").replace("-", "/")
        if "/" in compact:
            parts = compact.split("/", 1)
            exp_month = exp_month or parts[0]
            exp_year = exp_year or parts[1]
        elif len(compact) in (4, 6):
            exp_month = exp_month or compact[:2]
            exp_year = exp_year or compact[2:]

    exp_year = _normalize_exp_year(exp_year)

    return {
        "number": _config_value(
            ("BILLING_CARD_NUMBER", "TOPUP_BILLING_CARD_NUMBER"),
            "billingCardNumber",
        ).replace(" ", "").replace("-", ""),
        "exp_month": exp_month.zfill(2) if exp_month else "",
        "exp_year": exp_year,
        "cvc": _config_value(
            ("BILLING_CARD_CVC", "TOPUP_BILLING_CARD_CVC"),
            "billingCardCvc",
        ),
    }


def _normalize_card_entry(number: str = "", expiry: str = "", cvc: str = "", exp_month: str = "", exp_year: str = "") -> dict:
    number = str(number or "").replace(" ", "").replace("-", "").strip()
    expiry = str(expiry or "").strip()
    exp_month = str(exp_month or "").strip()
    exp_year = str(exp_year or "").strip()
    if expiry and (not exp_month or not exp_year):
        compact = expiry.replace(" ", "").replace("-", "/")
        if "/" in compact:
            parts = compact.split("/", 1)
            exp_month = exp_month or parts[0]
            exp_year = exp_year or parts[1]
        elif len(compact) in (4, 6):
            exp_month = exp_month or compact[:2]
            exp_year = exp_year or compact[2:]
    exp_year = _normalize_exp_year(exp_year)
    return {
        "number": number,
        "exp_month": exp_month.zfill(2) if exp_month else "",
        "exp_year": exp_year,
        "cvc": str(cvc or "").strip(),
    }


def _parse_card_entry(entry) -> dict:
    if isinstance(entry, dict):
        return _normalize_card_entry(
            number=entry.get("number") or entry.get("cardNumber") or entry.get("billingCardNumber") or "",
            expiry=entry.get("expiry") or entry.get("exp") or entry.get("billingCardExpiry") or "",
            cvc=entry.get("cvc") or entry.get("cvv") or entry.get("billingCardCvc") or "",
            exp_month=entry.get("exp_month") or entry.get("expMonth") or entry.get("month") or "",
            exp_year=entry.get("exp_year") or entry.get("expYear") or entry.get("year") or "",
        )
    text = str(entry or "").strip()
    if not text:
        return _normalize_card_entry()
    parts = [part.strip() for part in re.split(r"[|,\t ]+", text) if part.strip()]
    if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit():
        return _normalize_card_entry(number=parts[0], exp_month=parts[1], exp_year=parts[2], cvc=parts[3])
    if len(parts) >= 3:
        return _normalize_card_entry(number=parts[0], expiry=parts[1], cvc=parts[2])
    return _normalize_card_entry()


DEFAULT_GENERATED_CARD_PREFIX_MAP = {
    # 仅填 6 位卡头时，自动扩展到当前验证过的内置长卡头，避免随机到未被上游接受的 BIN 子段。
    "415464": "415464440133",
}

COMMON_SYNTHETIC_CARD_PREFIXES = (
    # Stripe/test-network style prefixes. Full PANs are generated locally with Luhn
    # and are intended for BILLING_TEST_MODE/mock validation unless explicitly overridden.
    "424242",
    "400005",
    "555555",
    "520082",
    "222300",
    "378282",
)


def _config_bool(key: str, default: bool = False) -> bool:
    env_name = _camel_to_env(key)
    value = os.getenv(env_name)
    if value is None:
        value = GATEWAY_CONFIG.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _config_bool_config_first(key: str, env_name: str | None = None, default: bool = False) -> bool:
    value = GATEWAY_CONFIG.get(key)
    if value is None or value == "":
        value = os.getenv(env_name or _camel_to_env(key))
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _config_int_config_first(key: str, env_name: str, default: int) -> int:
    value = GATEWAY_CONFIG.get(key)
    if value is None or value == "":
        value = os.getenv(env_name)
    try:
        return int(float(value))
    except Exception:
        return default


def _config_str_config_first(key: str, env_name: str, default: str = "") -> str:
    value = GATEWAY_CONFIG.get(key)
    if value is None or value == "":
        value = os.getenv(env_name)
    if value is None or value == "":
        return default
    return str(value).strip()


def _config_int(key: str, env_name: str, default: int) -> int:
    value = os.getenv(env_name)
    if value is None or value == "":
        value = GATEWAY_CONFIG.get(key)
    try:
        return int(float(value))
    except Exception:
        return default


def _camel_to_env(key: str) -> str:
    out = []
    for ch in str(key or ""):
        if ch.isupper():
            out.append("_")
        out.append(ch.upper())
    return "".join(out).strip("_")


def _billing_card_generator_requested() -> bool:
    return _config_bool_config_first("billingCardGeneratorEnabled", "BILLING_CARD_GENERATOR_ENABLED", False)


def _billing_card_generator_live_allowed() -> bool:
    return _config_bool("billingCardGeneratorAllowLive", False)


def _billing_card_generator_mock_context() -> bool:
    for name in ("BILLING_TEST_MODE", "BILLING_MOCK_STRIPE_PM", "BILLING_MOCK_ADD_CARD"):
        value = os.getenv(name)
        if value and value.strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def billing_card_generator_active() -> bool:
    # 用户侧语义：开关打开即使用卡头自动生成卡；未填卡头默认 415464。
    return _billing_card_generator_requested()


def _billing_card_generator_only() -> bool:
    return _config_bool_config_first("billingCardGeneratorOnly", "BILLING_CARD_GENERATOR_ONLY", True)


def _billing_card_generator_count() -> int:
    count = _config_int_config_first("billingCardGeneratorCount", "BILLING_CARD_GENERATOR_COUNT", 20)
    max_count = _config_int_config_first("billingCardGeneratorMaxCount", "BILLING_CARD_GENERATOR_MAX_COUNT", 50)
    return max(0, min(max_count, count))


def _expand_generator_prefix(prefix: str) -> str:
    digits = re.sub(r"\D", "", str(prefix or ""))
    return DEFAULT_GENERATED_CARD_PREFIX_MAP.get(digits, digits)


def _split_generator_prefixes(raw: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[;,\n\t ]+", str(raw or "")):
        digits = _expand_generator_prefix(part)
        if 1 <= len(digits) <= 15:
            out.append(digits)
    return _dedupe_strings(out)


def _dedupe_strings(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _billing_card_allowed_prefixes() -> list[str]:
    raw = _config_str_config_first("billingCardAllowedPrefixes", "BILLING_CARD_ALLOWED_PREFIXES", "")
    prefixes: list[str] = []
    for part in re.split(r"[;,\n\t ]+", str(raw or "")):
        digits = re.sub(r"\D", "", part)
        if digits:
            prefixes.append(digits)
    return _dedupe_strings(prefixes)


def _filter_billing_cards_by_allowed_prefixes(cards: list[dict]) -> list[dict]:
    prefixes = _billing_card_allowed_prefixes()
    if not prefixes:
        return cards
    filtered: list[dict] = []
    for card in cards or []:
        number = re.sub(r"\D", "", str((card or {}).get("number") or ""))
        if any(number.startswith(prefix) for prefix in prefixes):
            filtered.append(card)
    return filtered


def _configured_card_prefixes(existing_cards: list[dict] | None = None) -> list[str]:
    prefix_digits = _config_int("billingCardGeneratorPrefixDigits", "BILLING_CARD_GENERATOR_PREFIX_DIGITS", 6)
    prefix_digits = max(1, min(12, prefix_digits))
    prefixes: list[str] = []
    for card in existing_cards or []:
        number = re.sub(r"\D", "", str((card or {}).get("number") or ""))
        if len(number) > prefix_digits:
            prefixes.append(number[:prefix_digits])
    return _dedupe_strings(prefixes)


def _billing_card_generator_prefixes(existing_cards: list[dict] | None = None) -> list[str]:
    raw = _config_str_config_first("billingCardGeneratorPrefixes", "BILLING_CARD_GENERATOR_PREFIXES", "")
    mode = str(raw or "").strip().lower()
    prefixes: list[str] = []
    if billing_card_generator_active() and mode in ("", "auto", "default"):
        prefixes.append(_expand_generator_prefix("415464"))
    elif mode in ("common", "stripe-test", "test"):
        prefixes.extend(COMMON_SYNTHETIC_CARD_PREFIXES)
    else:
        prefixes.extend(_split_generator_prefixes(raw))
    if not prefixes:
        prefixes.append(_expand_generator_prefix("415464"))
    return _dedupe_strings(prefixes)


def _luhn_valid(number: str) -> bool:
    digits = [int(ch) for ch in re.sub(r"\D", "", str(number or ""))]
    if not digits:
        return False
    total = 0
    parity = len(digits) % 2
    for idx, digit in enumerate(digits):
        if idx % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _luhn_check_digit(body_without_check: str) -> str:
    body = re.sub(r"\D", "", str(body_without_check or ""))
    for digit in "0123456789":
        if _luhn_valid(body + digit):
            return digit
    return "0"


def _card_length_for_prefix(prefix: str) -> int:
    override = _config_int("billingCardGeneratorLength", "BILLING_CARD_GENERATOR_LENGTH", 0)
    if 12 <= override <= 19:
        return override
    prefix = re.sub(r"\D", "", str(prefix or ""))
    if prefix.startswith(("34", "37")):
        return 15
    return 16


def _card_cvc_length_for_prefix(prefix: str) -> int:
    override = _config_int("billingCardGeneratorCvcLength", "BILLING_CARD_GENERATOR_CVC_LENGTH", 0)
    if override in (3, 4):
        return override
    return 4 if re.sub(r"\D", "", str(prefix or "")).startswith(("34", "37")) else 3


def _rng_for_card_generator():
    seed = os.getenv("BILLING_CARD_GENERATOR_SEED") or str(GATEWAY_CONFIG.get("billingCardGeneratorSeed") or "")
    if seed:
        return random.Random(seed)
    return random.SystemRandom()


def _generated_card_number(prefix: str, rng) -> str:
    prefix = re.sub(r"\D", "", str(prefix or ""))
    length = max(len(prefix) + 1, _card_length_for_prefix(prefix))
    body_len = length - 1
    if len(prefix) > body_len:
        prefix = prefix[:body_len]
    body = prefix + "".join(str(rng.randrange(10)) for _ in range(body_len - len(prefix)))
    return body + _luhn_check_digit(body)


def _generated_card_expiry(rng) -> tuple[str, str]:
    expiry = os.getenv("BILLING_CARD_GENERATOR_EXPIRY") or str(GATEWAY_CONFIG.get("billingCardGeneratorExpiry") or "")
    month = os.getenv("BILLING_CARD_GENERATOR_EXP_MONTH") or str(GATEWAY_CONFIG.get("billingCardGeneratorExpMonth") or "")
    year = os.getenv("BILLING_CARD_GENERATOR_EXP_YEAR") or str(GATEWAY_CONFIG.get("billingCardGeneratorExpYear") or "")
    parsed = _normalize_card_entry(number="0" * 16, expiry=expiry, cvc="000", exp_month=month, exp_year=year)
    if parsed.get("exp_month") and parsed.get("exp_year"):
        return parsed["exp_month"], parsed["exp_year"]
    now = datetime.now(timezone.utc)
    # 有效期范围可控，默认 18-60 个月后：避免太近触发不稳定，也避免 7-10 年后太远。
    min_months = _config_int_config_first("billingCardGeneratorMinMonths", "BILLING_CARD_GENERATOR_MIN_MONTHS", 18)
    max_months = _config_int_config_first("billingCardGeneratorMaxMonths", "BILLING_CARD_GENERATOR_MAX_MONTHS", 60)
    min_months = max(1, min(120, min_months))
    max_months = max(min_months, min(120, max_months))
    offset = rng.randint(min_months, max_months)
    month_index = (now.year * 12 + (now.month - 1)) + offset
    exp_year = month_index // 12
    exp_month = month_index % 12 + 1
    return str(exp_month).zfill(2), str(exp_year)


def _generated_card_cvc(prefix: str, rng) -> str:
    override = os.getenv("BILLING_CARD_GENERATOR_CVC") or str(GATEWAY_CONFIG.get("billingCardGeneratorCvc") or "")
    digits = re.sub(r"\D", "", override)
    if len(digits) in (3, 4):
        return digits
    length = _card_cvc_length_for_prefix(prefix)
    return "".join(str(rng.randrange(10)) for _ in range(length))


def generated_billing_cards(existing_cards: list[dict] | None = None) -> list[dict]:
    if not billing_card_generator_active():
        return []
    count = _billing_card_generator_count()
    if count <= 0:
        return []
    rng = _rng_for_card_generator()
    prefixes = _billing_card_generator_prefixes(existing_cards)
    cards: list[dict] = []
    seen_numbers = {re.sub(r"\D", "", str((card or {}).get("number") or "")) for card in existing_cards or []}
    attempts = 0
    while len(cards) < count and attempts < count * 20:
        prefix = prefixes[(len(cards) + attempts) % len(prefixes)]
        number = _generated_card_number(prefix, rng)
        attempts += 1
        if number in seen_numbers:
            continue
        seen_numbers.add(number)
        exp_month, exp_year = _generated_card_expiry(rng)
        cards.append({
            "number": number,
            "exp_month": exp_month,
            "exp_year": exp_year,
            "cvc": _generated_card_cvc(prefix, rng),
            "generated": "synthetic",
        })
    return _filter_billing_cards_by_allowed_prefixes(cards)


def billing_card_generator_summary(existing_cards: list[dict] | None = None) -> dict:
    prefixes = _billing_card_generator_prefixes(existing_cards)
    active = billing_card_generator_active()
    requested = _billing_card_generator_requested()
    return {
        "requested": requested,
        "active": active,
        "mockContext": _billing_card_generator_mock_context(),
        "allowLive": _billing_card_generator_live_allowed(),
        "only": _billing_card_generator_only(),
        "count": _billing_card_generator_count() if requested else 0,
        "prefixCount": len(prefixes),
        "prefixSample": [p[:4] + "…" for p in prefixes[:8]],
        "allowedPrefixes": _billing_card_allowed_prefixes(),
        "blockedReason": "requires BILLING_TEST_MODE/mock or BILLING_CARD_GENERATOR_ALLOW_LIVE=1" if requested and not active else "",
    }


def configured_billing_cards() -> list[dict]:
    """Return complete configured cards only, without synthetic/generated entries."""
    raw_pool = os.getenv("BILLING_CARD_POOL") or os.getenv("BILLING_CARDS") or str(GATEWAY_CONFIG.get("billingCardPool") or "")
    entries = []
    if raw_pool.strip():
        try:
            parsed = json.loads(raw_pool)
            if isinstance(parsed, list):
                entries = parsed
            elif isinstance(parsed, dict):
                entries = parsed.get("cards") if isinstance(parsed.get("cards"), list) else [parsed]
        except Exception:
            entries = [part for part in re.split(r"[;\n]+", raw_pool) if part.strip()]

    config_pool = GATEWAY_CONFIG.get("billingCards")
    if not entries and isinstance(config_pool, list):
        entries = config_pool

    cards = [_parse_card_entry(entry) for entry in entries]
    cards = [card for card in cards if all(card.values())]
    primary = billing_card()
    if all(primary.values()) and os.getenv("BILLING_CARD_POOL_INCLUDE_PRIMARY", "1").strip().lower() not in ("0", "false", "no", "off"):
        cards = [primary] + cards
    seen = set()
    deduped = []
    for card in cards:
        key = (card.get("number"), card.get("exp_month"), card.get("exp_year"), card.get("cvc"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(card)
    return _filter_billing_cards_by_allowed_prefixes(deduped)


def billing_cards() -> list[dict]:
    """返回可轮换卡池。开启生成器时仅使用按卡头生成的卡；否则使用配置卡池。"""
    source_cards = configured_billing_cards()
    generated = generated_billing_cards(source_cards)
    cards = generated if billing_card_generator_active() and generated else source_cards
    if cards:
        return cards
    return [billing_card()]


def _normalize_exp_year(exp_year: str) -> str:
    exp_year = str(exp_year or "").strip()
    if len(exp_year) == 2 and exp_year.isdigit():
        current_year = datetime.now(timezone.utc).year
        century = current_year - (current_year % 100)
        year = century + int(exp_year)
        if year < current_year - 5:
            year += 100
        return str(year)
    return exp_year

# 输出
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "accounts"
KEYS_FILE = OUTPUT_DIR / "keys.jsonl"
KEYS_TEXT_FILE = Path(os.getenv("KEYS_PATH", OUTPUT_DIR / "keys.txt"))

# 注册器子域名池（通过环境变量配置，多个用逗号分隔）
PARENT_DOMAINS = os.getenv("PARENT_DOMAINS", "")
