import sys
import random
from . import config

# easy_proxy SDK 路径
_SDK_DIR = "D:/isha_project/suxiaotang/emergent.sh/emergent2api/python"
if _SDK_DIR not in sys.path:
    sys.path.insert(0, _SDK_DIR)


def get_proxy_list(direct_proxy: str = "") -> list[str]:
    """
    获取代理列表。
    - direct_proxy 不为空时直接返回单个代理（如 socks5://127.0.0.1:10808）
    - 否则从 easy_proxies 拉取
    """
    if direct_proxy:
        return [direct_proxy]

    from emergent2api.easy_proxy_sdk import EasyProxyClient
    client = EasyProxyClient(config.EASY_PROXY_URL)
    if config.EASY_PROXY_PASSWORD:
        client.login(password=config.EASY_PROXY_PASSWORD)
    return client.export_proxy_uris(scheme="socks5")


def pick_proxy(proxies: list[str]) -> str:
    return random.choice(proxies)
