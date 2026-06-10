"""
浏览器指纹配置 - 优化版
同步自 D3vin/Turnstile-Solver-NEW v1.2b

支持 Chrome/Edge/Avast/Brave 多种浏览器的 User-Agent 和 Sec-CH-UA 配置
版本范围: 136-139
"""

import random
from typing import Tuple, Optional, List, Dict


class BrowserConfig:
    """浏览器配置管理类"""

    # Sec-CH-UA 配置 (版本 136-139)
    SEC_CH_UA_CONFIGS: Dict[str, Dict[str, str]] = {
        "chrome": {
            "139": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
            "138": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
            "137": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
            "136": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'
        },
        "edge": {
            "139": '"Not;A=Brand";v="99", "Microsoft Edge";v="139", "Chromium";v="139"',
            "138": '"Not)A;Brand";v="8", "Chromium";v="138", "Microsoft Edge";v="138"',
            "137": '"Microsoft Edge";v="137", "Chromium";v="137", "Not/A)Brand";v="24"'
        },
        "avast": {
            "138": '"Not)A;Brand";v="8", "Chromium";v="138", "Avast Secure Browser";v="138"',
            "137": '"Avast Secure Browser";v="137", "Chromium";v="137", "Not/A)Brand";v="24"'
        },
        "brave": {
            "139": '"Not;A=Brand";v="99", "Brave";v="139", "Chromium";v="139"',
            "138": '"Not)A;Brand";v="8", "Chromium";v="138", "Brave";v="138"',
            "137": '"Brave";v="137", "Chromium";v="137", "Not/A)Brand";v="24"'
        }
    }

    # User-Agent 配置
    USER_AGENT_CONFIGS: Dict[str, Dict[str, str]] = {
        "chrome": {
            "139": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
            "138": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "137": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "136": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        },
        "edge": {
            "139": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0",
            "138": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0",
            "137": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0"
        },
        "avast": {
            "138": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Avast/138.0.0.0",
            "137": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Avast/137.0.0.0"
        },
        "brave": {
            "139": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
            "138": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "137": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        }
    }

    def __init__(self):
        self.available_browsers = list(self.USER_AGENT_CONFIGS.keys())

    def get_random_browser_config(self, browser_type: Optional[str] = None) -> Tuple[str, str, str, str]:
        """
        获取随机浏览器配置

        Args:
            browser_type: 浏览器类型过滤 (chrome/chromium/msedge/camoufox)

        Returns:
            Tuple[browser_name, version, user_agent, sec_ch_ua]
        """
        if browser_type in ['chrome', 'chromium', 'msedge']:
            browser = random.choice(['chrome', 'edge', 'avast', 'brave'])
        elif browser_type == 'camoufox':
            return 'firefox', 'custom', '', ''
        else:
            browser = random.choice(self.available_browsers)

        versions = list(self.USER_AGENT_CONFIGS[browser].keys())
        version = random.choice(versions)

        user_agent = self.USER_AGENT_CONFIGS[browser][version]
        sec_ch_ua = self.SEC_CH_UA_CONFIGS.get(browser, {}).get(version, "")

        return browser, version, user_agent, sec_ch_ua

    def get_browser_config(self, browser: str, version: str) -> Optional[Tuple[str, str]]:
        """
        获取指定浏览器配置

        Args:
            browser: 浏览器名称
            version: 版本号

        Returns:
            Tuple[user_agent, sec_ch_ua] 或 None
        """
        try:
            user_agent = self.USER_AGENT_CONFIGS[browser][version]
            sec_ch_ua = self.SEC_CH_UA_CONFIGS.get(browser, {}).get(version, "")
            return user_agent, sec_ch_ua
        except KeyError:
            return None

    def get_all_configs(self) -> List[Tuple[str, str, str, str]]:
        """获取所有可用配置"""
        configs = []
        for browser in self.available_browsers:
            for version in self.USER_AGENT_CONFIGS[browser].keys():
                user_agent = self.USER_AGENT_CONFIGS[browser][version]
                sec_ch_ua = self.SEC_CH_UA_CONFIGS.get(browser, {}).get(version, "")
                configs.append((browser, version, user_agent, sec_ch_ua))
        return configs

    def get_available_browsers(self) -> List[str]:
        """获取所有可用浏览器"""
        return self.available_browsers.copy()

    def get_browser_versions(self, browser: str) -> List[str]:
        """获取指定浏览器的所有版本"""
        return list(self.USER_AGENT_CONFIGS.get(browser, {}).keys())


# 全局单例
browser_config = BrowserConfig()


if __name__ == '__main__':
    config = BrowserConfig()
    print("随机配置:")
    browser, version, ua, sec_ua = config.get_random_browser_config('chrome')
    print(f"  浏览器: {browser} {version}")
    print(f"  User-Agent: {ua}")
    print(f"  Sec-CH-UA: {sec_ua}")
    print()
    print("可用浏览器:")
    for b in config.get_available_browsers():
        versions = config.get_browser_versions(b)
        print(f"  {b}: {', '.join(versions)}")
    print()
    print(f"总配置数: {len(config.get_all_configs())}")
