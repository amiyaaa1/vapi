import random

class browser_config:
    @staticmethod
    def get_random_browser_config(browser_type):
        # 返回: 浏览器名, 版本, User-Agent, Sec-CH-UA
        # 真实启用随机指纹：之前只有一个版本，--random/池回收实际仍是同一 UA。
        # 版本集中在同一大版本带附近，避免和当前镜像 Chromium 差距过大。
        versions = [
            # 保持大版本与镜像内 Chromium 148 一致，避免 UA/二进制特征不一致导致 Turnstile 长时间不出 token。
            "148.0.7778.96",
            "148.0.7778.88",
            "148.0.7778.72",
            "148.0.7778.61",
        ]
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
