import argparse
import asyncio
import json
import logging
import string
import random
import sys
from datetime import datetime, timezone
from . import config
from .email_client import MoeMailClient
from .proxy import get_proxy_list, pick_proxy
from .register import register_one

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("registrator")

# 默认子域名池，可通过 PARENT_DOMAINS 环境变量覆盖（逗号分隔）
DEFAULT_DOMAINS = [
    "kei.us.ci", "aris.cc.cd", "sutang.cc.cd", "sutang.us.ci",
    "ornni.eu.cc", "bitss.eu.cc", "keii.eu.cc",
]
PARENT_DOMAINS = config.PARENT_DOMAINS or DEFAULT_DOMAINS


def _rand_password(n=16) -> str:
    base = string.ascii_letters + string.digits
    pwd = [random.choice("!@#$%"), random.choice(string.ascii_uppercase),
           random.choice(string.ascii_lowercase), random.choice(string.digits)]
    pwd += random.choices(base + "!@#$%", k=n - 4)
    random.shuffle(pwd)
    return "".join(pwd)


class SubdomainPool:
    """按需派生子域名，每个子域名复用多次，结束统一清理"""

    def __init__(self, mail: MoeMailClient, per_subdomain: int = 5):
        self.mail = mail
        self.per_subdomain = per_subdomain
        self._domains: list[dict] = []  # [{id, name, used, limit}]
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        """获取一个可用子域名，每个子域名随机限额，用满自动派生新的"""
        async with self._lock:
            for d in self._domains:
                if d["used"] < d["limit"]:
                    d["used"] += 1
                    return d["name"]

            # 都用满了，派生一个新的
            parent = random.choice(PARENT_DOMAINS)
            info = await self.mail.add_subdomain(parent)
            entry = {"id": info["id"], "name": info["name"], "used": 1,
                     "limit": random.randint(2, self.per_subdomain)}
            self._domains.append(entry)
            log.info(f"[域名池] 派生: {entry['name']} (父域: {parent})")
            return entry["name"]

    async def cleanup(self):
        """统一清理所有派生的子域名"""
        for d in self._domains:
            try:
                await self.mail.delete_domain(d["id"])
                log.info(f"[域名池] 清理: {d['name']}")
            except Exception as e:
                log.warning(f"[域名池] 清理失败 {d['name']}: {e}")
        self._domains.clear()


async def run(count: int, concurrency: int, proxy: str = "", per_subdomain: int = 5):
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("获取代理列表...")
    proxies = get_proxy_list(direct_proxy=proxy)
    log.info(f"可用代理: {len(proxies)} 个")

    mail = MoeMailClient()

    sem = asyncio.Semaphore(concurrency)
    success = 0
    fail = 0

    async def do_one(idx: int):
        nonlocal success, fail
        email_info = None

        async with sem:
            try:
                email_info = await mail.create_email()
                email_addr = email_info["address"]
                email_id = email_info["id"]
                password = _rand_password()
                px = pick_proxy(proxies)

                log.info(f"[{idx+1}/{count}] {email_addr} -> {px}")

                result = await register_one(px, email_addr, email_id, password, mail)

                if result:
                    result["created_at"] = datetime.now(timezone.utc).isoformat()
                    with open(config.KEYS_FILE, "a", encoding="utf-8") as f:
                        f.write(json.dumps(result, ensure_ascii=False) + "\n")
                    config.KEYS_TEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
                    with open(config.KEYS_TEXT_FILE, "a", encoding="utf-8") as f:
                        f.write(result["private_key"] + "\n")
                    success += 1
                else:
                    fail += 1
            except Exception as e:
                log.error(f"[{idx+1}/{count}] 异常: {e}")
                fail += 1
            finally:
                if email_info:
                    try:
                        await mail.delete_email(email_info["id"])
                    except Exception:
                        pass

    tasks = [do_one(i) for i in range(count)]
    await asyncio.gather(*tasks)

    await mail.close()

    log.info(f"完成: 成功 {success}, 失败 {fail}")
    if success:
        log.info(f"密钥已保存到: {config.KEYS_FILE} 和 {config.KEYS_TEXT_FILE}")
    return success, fail


def main():
    parser = argparse.ArgumentParser(description="Vapi 自动注册器")
    parser.add_argument("--count", type=int, default=1, help="注册数量")
    parser.add_argument("--concurrency", type=int, default=1, help="并发数")
    parser.add_argument("--proxy", default="", help="直接指定代理")
    parser.add_argument("--per-subdomain", type=int, default=5, help="每个子域名复用次数 (默认5)")
    args = parser.parse_args()

    success, fail = asyncio.run(run(args.count, args.concurrency, args.proxy, args.per_subdomain))
    if fail > 0:
        sys.exit(1)
    if success < args.count:
        sys.exit(1)


if __name__ == "__main__":
    main()
