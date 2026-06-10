"""
Turnstile Solver 核心服务端 - 优化版
集成 api_solver.py 的所有高级特性:
- 7 种点击策略
- 资源拦截优化
- Camoufox & Patchright 支持
- 完整代理支持
- Rich 终端美化
"""

import os
import sys
import time
import uuid
import random
import logging
import asyncio
import argparse
from typing import Optional, Union, Dict, Any, List

from quart import Quart, request, jsonify
try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    AsyncCamoufox = None

from patchright.async_api import async_playwright
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box

from .db_results import init_db, save_result, load_result, cleanup_old_results
from .browser_configs import browser_config

# 颜色定义
COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger: CustomLogger = logging.getLogger("TurnstileAPIServer")  # type: ignore
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    logger.addHandler(handler)


class TurnstileAPIServer:
    """
    Turnstile API 服务端核心类
    """

    def __init__(
        self,
        headless: bool = True,
        useragent: Optional[str] = None,
        debug: bool = False,
        browser_type: str = 'chromium',
        thread: int = 4,
        proxy_support: bool = False,
        use_random_config: bool = False,
        browser_name: Optional[str] = None,
        browser_version: Optional[str] = None,
        proxy_file: str = "proxies.txt"
    ):
        self.app = Quart(__name__)
        self.debug = debug
        if debug:
            logger.setLevel(logging.DEBUG)

        self.browser_type = browser_type
        self.headless = headless
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.proxy_file = proxy_file
        self.browser_pool = asyncio.Queue()
        self.use_random_config = use_random_config
        self.browser_name = browser_name
        self.browser_version = browser_version
        self.console = Console()

        # 初始化指纹
        self.useragent = useragent
        self.sec_ch_ua = None

        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            if browser_name and browser_version:
                config = browser_config.get_browser_config(browser_name, browser_version)
                if config:
                    self.useragent, self.sec_ch_ua = config
            elif not useragent:
                browser, version, ua, sec_ua = browser_config.get_random_browser_config(self.browser_type)
                self.browser_name = browser
                self.browser_version = version
                self.useragent = ua
                self.sec_ch_ua = sec_ua

        self._setup_routes()

    def display_welcome(self):
        """显示欢迎界面"""
        self.console.clear()
        combined_text = Text()
        combined_text.append("\n📁 Version: ", style="bold white")
        combined_text.append("1.2b (Optimized)", style="green")
        combined_text.append("\n🚀 Threads: ", style="bold white")
        combined_text.append(str(self.thread_count), style="cyan")
        combined_text.append("\n🌐 Browser: ", style="bold white")
        combined_text.append(self.browser_type, style="magenta")
        combined_text.append("\n")

        info_panel = Panel(
            Align.left(combined_text),
            title="[bold blue]Turnstile Solver Tool[/bold blue]",
            subtitle="[bold magenta]Deeply Optimized[/bold magenta]",
            box=box.ROUNDED,
            border_style="bright_blue",
            padding=(0, 1),
            width=50
        )
        self.console.print(info_panel)

    def _setup_routes(self) -> None:
        """设置路由"""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/')(self.index)

    async def _startup(self) -> None:
        """启动初始化"""
        self.display_welcome()
        logger.info("Starting initialization...")
        try:
            await init_db()
            await self._initialize_browser_pool()
            asyncio.create_task(self._periodic_cleanup())
        except Exception as e:
            logger.error(f"Startup failed: {e}")
            raise

    async def _initialize_browser_pool(self) -> None:
        """初始化浏览器池"""
        playwright = None
        camoufox_instance = None

        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            playwright = await async_playwright().start()
        elif self.browser_type == "camoufox":
            if not AsyncCamoufox:
                raise ImportError("camoufox is not installed")
            camoufox_instance = AsyncCamoufox(headless=self.headless)

        for i in range(self.thread_count):
            # 为每个线程准备配置
            if self.use_random_config:
                b_name, b_ver, ua, sec_ua = browser_config.get_random_browser_config(self.browser_type)
            else:
                b_name = self.browser_name or 'custom'
                b_ver = self.browser_version or 'custom'
                ua = self.useragent
                sec_ua = self.sec_ch_ua or ''

            config = {
                'browser_name': b_name,
                'browser_version': b_ver,
                'useragent': ua,
                'sec_ch_ua': sec_ua
            }

            browser_args = ["--window-position=0,0", "--force-device-scale-factor=1"]
            if ua:
                browser_args.append(f"--user-agent={ua}")

            browser = None
            if self.browser_type in ['chromium', 'chrome', 'msedge'] and playwright:
                browser = await playwright.chromium.launch(
                    channel=self.browser_type,
                    headless=self.headless,
                    args=browser_args
                )
            elif self.browser_type == "camoufox" and camoufox_instance:
                browser = await camoufox_instance.start()

            if browser:
                await self.browser_pool.put((i + 1, browser, config))
                if self.debug:
                    logger.debug(f"Browser {i+1} ready: {b_name} {b_ver}")

        logger.info(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")

    async def _periodic_cleanup(self):
        """定时清理"""
        while True:
            await asyncio.sleep(3600)
            try:
                deleted = await cleanup_old_results(days_old=1)
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old results")
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def _antishadow_inject(self, page):
        """注入反 Shadow DOM 检测脚本"""
        await page.add_init_script("""
          (function() {
            const originalAttachShadow = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function(init) {
              const shadow = originalAttachShadow.call(this, init);
              if (init.mode === 'closed') {
                window.__lastClosedShadowRoot = shadow;
              }
              return shadow;
            };
          })();
        """)

    async def _optimized_route_handler(self, route):
        """资源拦截优化"""
        url = route.request.url
        resource_type = route.request.resource_type
        allowed_types = {'document', 'script', 'xhr', 'fetch'}
        allowed_domains = ['challenges.cloudflare.com', 'static.cloudflareinsights.com', 'cloudflare.com']

        if resource_type in allowed_types or any(domain in url for domain in allowed_domains):
            await route.continue_()
        else:
            await route.abort()

    async def _find_and_click_checkbox(self, page, index: int):
        """精准点击 iframe 内的 checkbox"""
        try:
            iframe_selectors = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'iframe[title*="widget"]'
            ]

            for selector in iframe_selectors:
                try:
                    iframe = page.locator(selector).first
                    if await iframe.count() > 0:
                        frame_element = await iframe.element_handle()
                        frame = await frame_element.content_frame()
                        if frame:
                            checkbox_selectors = ['input[type="checkbox"]', '.cb-lb input', 'label input']
                            for cb_sel in checkbox_selectors:
                                try:
                                    cb = frame.locator(cb_sel).first
                                    await cb.click(timeout=1500)
                                    return True
                                except: continue
                            # Fallback: click iframe directly
                            await iframe.click(timeout=1000)
                            return True
                except: continue
        except: pass
        return False

    async def _try_click_strategies(self, page, index: int):
        """7 种点击策略"""
        strategies = [
            ('checkbox', lambda: self._find_and_click_checkbox(page, index)),
            ('direct', lambda: self._safe_click(page, '.cf-turnstile', index)),
            ('iframe', lambda: self._safe_click(page, 'iframe[src*="turnstile"]', index)),
            ('js', lambda: page.evaluate("document.querySelector('.cf-turnstile')?.click()")),
            ('attr', lambda: self._safe_click(page, '[data-sitekey]', index)),
            ('class', lambda: self._safe_click(page, '*[class*="turnstile"]', index)),
            ('xpath', lambda: self._safe_click(page, "//div[@class='cf-turnstile']", index))
        ]

        for name, func in strategies:
            try:
                if await func():
                    if self.debug: logger.debug(f"Browser {index}: Strategy '{name}' ok")
                    return True
            except: continue
        return False

    async def _safe_click(self, page, selector: str, index: int):
        """安全点击"""
        try:
            await page.locator(selector).first.click(timeout=1000)
            return True
        except: return False

    async def _inject_captcha_directly(self, page, sitekey: str, action: str = '', cdata: str = '', index: int = 0):
        """直接注入 CAPTCHA 元素"""
        script = f"""
        document.querySelectorAll('.cf-turnstile, [data-sitekey]').forEach(el => el.remove());
        const div = document.createElement('div');
        div.className = 'cf-turnstile';
        div.setAttribute('data-sitekey', '{sitekey}');
        {f'div.setAttribute("data-action", "{action}");' if action else ''}
        {f'div.setAttribute("data-cdata", "{cdata}");' if cdata else ''}
        div.style.cssText = 'position:fixed;top:20px;left:20px;z-index:9999;background:white;padding:15px;border:2px solid #0f79af;border-radius:8px;';
        document.body.appendChild(div);

        const s = document.createElement('script');
        s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
        s.async = s.defer = true;
        document.head.appendChild(s);
        """
        await page.evaluate(script)

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: Optional[str] = None, cdata: Optional[str] = None):
        """核心解决逻辑"""
        index, browser, b_config = await self.browser_pool.get()
        context = None
        try:
            # 代理处理
            proxy_config = None
            if self.proxy_support:
                proxy_config = self._get_random_proxy()

            context_args = {"user_agent": b_config['useragent']}
            if proxy_config:
                context_args["proxy"] = proxy_config
            if b_config['sec_ch_ua']:
                context_args["extra_http_headers"] = {"sec-ch-ua": b_config['sec_ch_ua']}

            context = await browser.new_context(**context_args)
            page = await context.new_page()

            # 反检测 & 优化
            await self._antishadow_inject(page)
            await page.route("**/*", self._optimized_route_handler)
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

            if self.browser_type != "camoufox":
                await page.set_viewport_size({"width": 500, "height": 100})

            start_time = time.time()
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await self._inject_captcha_directly(page, sitekey, action or '', cdata or '', index)

            # 轮询 Token
            locator = page.locator('input[name="cf-turnstile-response"]')
            click_count = 0
            for attempt in range(30):
                try:
                    if await locator.count() > 0:
                        token = await locator.first.input_value(timeout=500)
                        if token:
                            elapsed = round(time.time() - start_time, 2)
                            logger.success(f"Browser {index}: Solved in {elapsed}s | Token: {token[:15]}...")
                            await save_result(task_id, "turnstile", {"value": token, "elapsed_time": elapsed})
                            return

                    if attempt > 2 and attempt % 3 == 0 and click_count < 10:
                        await self._try_click_strategies(page, index)
                        click_count += 1

                    await asyncio.sleep(1)
                except: continue

            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 30})
        except Exception as e:
            logger.error(f"Browser {index} error: {e}")
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
        finally:
            if context: await context.close()
            await self.browser_pool.put((index, browser, b_config))

    def _get_random_proxy(self) -> Optional[Dict[str, str]]:
        """获取随机代理配置"""
        if not os.path.exists(self.proxy_file):
            return None
        try:
            with open(self.proxy_file, 'r') as f:
                proxies = [l.strip() for l in f if l.strip()]
            if not proxies: return None
            p = random.choice(proxies)
            # 格式: scheme://user:pass@ip:port 或 ip:port:user:pass
            if '://' in p:
                scheme, rest = p.split('://')
                if '@' in rest:
                    auth, addr = rest.split('@')
                    user, pwd = auth.split(':')
                    return {"server": f"{scheme}://{addr}", "username": user, "password": pwd}
                return {"server": p}
            parts = p.split(':')
            if len(parts) == 4: # ip:port:user:pass
                return {"server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]}
            return {"server": f"http://{p}"}
        except: return None

    async def process_turnstile(self):
        """处理 /turnstile 请求"""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        if not url or not sitekey:
            return jsonify({"errorId": 1, "errorDescription": "url and sitekey required"}), 200

        task_id = str(uuid.uuid4())
        await save_result(task_id, "turnstile", {"status": "CAPTCHA_NOT_READY", "createTime": int(time.time())})

        asyncio.create_task(self._solve_turnstile(
            task_id=task_id,
            url=url,
            sitekey=sitekey,
            action=request.args.get('action'),
            cdata=request.args.get('cdata')
        ))
        return jsonify({"errorId": 0, "taskId": task_id}), 200

    async def get_result(self):
        """获取结果"""
        task_id = request.args.get('id')
        if not task_id: return jsonify({"errorId": 1, "errorDescription": "id required"}), 200

        result = await load_result(task_id)
        if not result: return jsonify({"errorId": 1, "errorDescription": "not found"}), 200

        if isinstance(result, dict) and result.get("status") == "CAPTCHA_NOT_READY":
            return jsonify({"status": "processing"}), 200

        if isinstance(result, dict) and result.get("value") and result.get("value") != "CAPTCHA_FAIL":
            return jsonify({"errorId": 0, "status": "ready", "solution": {"token": result["value"]}}), 200

        return jsonify({"errorId": 1, "errorCode": "ERROR_CAPTCHA_UNSOLVABLE"}), 200

    @staticmethod
    async def index():
        return "Turnstile Solver Tool API is running."


def run_server():
    """启动服务的快捷函数"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5072)
    parser.add_argument('--thread', type=int, default=4)
    parser.add_argument('--browser_type', type=str, default='camoufox')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--no-headless', action='store_true')
    parser.add_argument('--proxy', action='store_true')
    args = parser.parse_args()

    server = TurnstileAPIServer(
        headless=not args.no_headless,
        debug=args.debug,
        browser_type=args.browser_type,
        thread=args.thread,
        proxy_support=args.proxy
    )
    server.app.run(host='0.0.0.0', port=args.port)


if __name__ == '__main__':
    run_server()
