import random

class browser_config:
    @staticmethod
    def get_random_browser_config(browser_type):
        # 返回: 浏览器名, 版本, User-Agent, Sec-CH-UA
        versions = ["148.0.7778.96"]
        ver = random.choice(versions)
        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver} Safari/537.36"
        major = ver.split(".")[0]
        sec_ch_ua = f'"Not;A=Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'
        return "chrome", ver, ua, sec_ch_ua

    @staticmethod
    def get_browser_config(name, version):
        ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
        major = version.split(".")[0]
        sec_ch_ua = f'"Not;A=Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'
        return ua, sec_ch_ua
