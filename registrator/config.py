import os
import json
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

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
