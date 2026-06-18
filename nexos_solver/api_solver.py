import os
import sys
import time
import uuid
import random
import logging
import asyncio
import re
import signal
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Union
import argparse
from quart import Quart, request, jsonify
try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    AsyncCamoufox = None
from patchright.async_api import async_playwright
from db_results import init_db, save_result, load_result, cleanup_old_results
from browser_configs import browser_config
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box



COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}

STRIPE_DEFAULT_PK = os.getenv(
    "STRIPE_PUBLISHABLE_KEY",
    "pk_live_51NvVHqCRkod4mKy3BF9IHbOHhM3dGiYOPThym9Son9DdkS0DIyQKWkModLfDdPHO6hmEmqmzKrInZwA52PfMzrzX00MFliNTGB",
)
DODGEBALL_PUBLIC_KEY = os.getenv("DODGEBALL_PUBLIC_KEY", "364218e31251444ca8851a2dea555f6a")
DODGEBALL_API_URL = os.getenv("DODGEBALL_API_URL", "https://api.dodgeballhq.com")
VAPI_DASHBOARD_VERSION = os.getenv("DASHBOARD_VERSION", "670f2f3f21685ccb9be46866fdab17542cd08e28")
VAPI_SIGNUP_URL = os.getenv("VAPI_SIGNUP_URL", "https://dashboard.vapi.ai/register?redirect=%2Fsignup")
VAPI_API_URL = os.getenv("VAPI_API_URL", "https://api.vapi.ai")


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
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


class TurnstileAPIServer:

    def __init__(self, headless: bool, useragent: Optional[str], debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool = False, browser_name: Optional[str] = None, browser_version: Optional[str] = None):
        self.app = Quart(__name__)
        self.debug = debug
        self.browser_type = browser_type
        self.headless = headless
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.browser_slots = {}
        self.browser_use_counts = {}
        self.browser_root_pids = {}
        self.browser_profile_dirs = {}
        self.retire_browsers = set()
        self.pool_lock = asyncio.Lock()
        self.next_browser_index = 1
        self.pool_generation = 1
        try:
            self.max_tasks_per_browser = int(os.getenv("TURNSTILE_SOLVER_MAX_TASKS_PER_BROWSER", "5"))
        except Exception:
            self.max_tasks_per_browser = 5
        if self.max_tasks_per_browser < 0:
            self.max_tasks_per_browser = 0
        try:
            self.close_timeout = float(os.getenv("TURNSTILE_SOLVER_CLOSE_TIMEOUT", "12"))
        except Exception:
            self.close_timeout = 12.0
        try:
            self.tmp_cleanup_max_age = float(os.getenv("TURNSTILE_SOLVER_TMP_CLEANUP_MAX_AGE_SECONDS", "21600"))
        except Exception:
            self.tmp_cleanup_max_age = 21600.0
        self.playwright = None
        self.camoufox = None
        self.use_random_config = use_random_config
        self.browser_name = browser_name
        self.browser_version = browser_version
        self.console = Console()
        self.debug_dir = os.getenv("TURNSTILE_SOLVER_DEBUG_DIR", "/tmp/turnstile-debug")
        
        # Initialize useragent and sec_ch_ua attributes
        self.useragent = useragent
        self.sec_ch_ua = None
        
        
        if self.browser_type in ['chromium', 'chrome', 'msedge', 'cloak', 'cloakbrowser']:
            if browser_name and browser_version:
                config = browser_config.get_browser_config(browser_name, browser_version)
                if config:
                    useragent, sec_ch_ua = config
                    self.useragent = useragent
                    self.sec_ch_ua = sec_ch_ua
            elif useragent:
                self.useragent = useragent
            else:
                browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                self.browser_name = browser
                self.browser_version = version
                self.useragent = useragent
                self.sec_ch_ua = sec_ch_ua
        
        self.browser_args = []
        if self.useragent:
            self.browser_args.append(f"--user-agent={self.useragent}")

        self._setup_routes()

    def display_welcome(self):
        """Displays welcome screen with logo."""
        self.console.clear()
        
        combined_text = Text()
        combined_text.append("\n📢 Channel: ", style="bold white")
        combined_text.append("https://t.me/D3_vin", style="cyan")
        combined_text.append("\n💬 Chat: ", style="bold white")
        combined_text.append("https://t.me/D3vin_chat", style="cyan")
        combined_text.append("\n📁 GitHub: ", style="bold white")
        combined_text.append("https://github.com/D3-vin", style="cyan")
        combined_text.append("\n📁 Version: ", style="bold white")
        combined_text.append("1.2a", style="green")
        combined_text.append("\n")

        info_panel = Panel(
            Align.left(combined_text),
            title="[bold blue]Turnstile Solver[/bold blue]",
            subtitle="[bold magenta]Dev by D3vin[/bold magenta]",
            box=box.ROUNDED,
            border_style="bright_blue",
            padding=(0, 1),
            width=50
        )

        self.console.print(info_panel)
        self.console.print()




    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/stripe/payment-method', methods=['POST'])(self.process_stripe_payment_method)
        self.app.route('/vapi/signup', methods=['POST'])(self.process_vapi_signup)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/pool/status', methods=['GET'])(self.pool_status)
        self.app.route('/pool/resize', methods=['GET', 'POST'])(self.resize_pool_route)
        self.app.route('/pool/recycle', methods=['GET', 'POST'])(self.recycle_pool_route)
        self.app.route('/')(self.index)
        

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        self.display_welcome()
        logger.info("Starting browser initialization")
        try:
            await init_db()
            await self._initialize_browser()
            asyncio.create_task(asyncio.to_thread(self._cleanup_stale_temp_artifacts_sync, "startup"))
            
            # Запускаем периодическую очистку старых результатов
            asyncio.create_task(self._periodic_cleanup())
            
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""
        if self.browser_type in ['chromium', 'chrome', 'msedge', 'cloak', 'cloakbrowser']:
            self.playwright = await async_playwright().start()
        elif self.browser_type == "camoufox":
            self.camoufox = AsyncCamoufox(headless=self.headless)

        async with self.pool_lock:
            for _ in range(self.thread_count):
                await self._add_browser_to_pool_locked()

        logger.info(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")
        if self.max_tasks_per_browser > 0:
            logger.info(f"Browser auto recycle enabled: maxTasksPerBrowser={self.max_tasks_per_browser}")
        else:
            logger.info("Browser auto recycle by task count disabled")
        
        if self.use_random_config:
            logger.info(f"Each browser in pool received random configuration")
        elif self.browser_name and self.browser_version:
            logger.info(f"All browsers using configuration: {self.browser_name} {self.browser_version}")
        else:
            logger.info("Using custom configuration")
            
        if self.debug:
            for index, (_, config) in self.browser_slots.items():
                logger.debug(f"Browser {index} config: {config['browser_name']} {config['browser_version']}")
                logger.debug(f"Browser {index} User-Agent: {config['useragent']}")
                logger.debug(f"Browser {index} Sec-CH-UA: {config['sec_ch_ua']}")

    def _build_browser_config(self):
        if self.browser_type in ['chromium', 'chrome', 'msedge', 'cloak', 'cloakbrowser']:
            if self.use_random_config:
                browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
            elif self.browser_name and self.browser_version:
                config = browser_config.get_browser_config(self.browser_name, self.browser_version)
                if config:
                    useragent, sec_ch_ua = config
                    browser = self.browser_name
                    version = self.browser_version
                else:
                    browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
            else:
                browser = self.browser_name or 'custom'
                version = self.browser_version or 'custom'
                useragent = self.useragent
                sec_ch_ua = self.sec_ch_ua or ''
        else:
            browser = self.browser_type
            version = 'custom'
            useragent = self.useragent
            sec_ch_ua = self.sec_ch_ua or ''

        return {
            'browser_name': browser,
            'browser_version': version,
            'useragent': useragent,
            'sec_ch_ua': sec_ch_ua
        }

    def _build_browser_args(self, config, launch_proxy: Optional[str] = None):
        if self.browser_type in ("cloak", "cloakbrowser"):
            extra_args = [
                "--disable-dev-shm-usage",
                "--renderer-process-limit=1",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ]
            if self._cloak_proxy_launch_arg_enabled():
                proxy_for_arg = launch_proxy or os.getenv("TURNSTILE_SOLVER_PROXY", "") or os.getenv("WARP_PROXY_URL", "") or os.getenv("SOCKS5_PROXY", "")
                extra_args.extend(self._proxy_launch_arg(proxy_for_arg))
            if os.getenv("TURNSTILE_CLOAK_WEBRTC_IP_AUTO", "1").strip().lower() not in ("0", "false", "no", "off"):
                extra_args.append("--fingerprint-webrtc-ip=auto")
            if config.get('useragent') and os.getenv("TURNSTILE_CLOAK_OVERRIDE_UA", "0").strip().lower() in ("1", "true", "yes", "on"):
                extra_args.append(f"--user-agent={config['useragent']}")
            try:
                from cloakbrowser import build_args  # type: ignore
                return build_args(True, extra_args, timezone=os.getenv("BILLING_TIMEZONE_ID", "Europe/Berlin"), locale="en-US", headless=self.headless)
            except Exception as e:
                if self._cloak_strict_enabled():
                    raise RuntimeError(f"CloakBrowser build_args unavailable: {e}") from e
                logger.warning(f"CloakBrowser build_args unavailable, using compatibility args: {e}")

        browser_args = [
            "--window-position=0,0",
            "--force-device-scale-factor=1",
            "--ignore-gpu-blocklist",
            "--enable-unsafe-webgpu",
            "--enable-unsafe-swiftshader",
            "--use-angle=swiftshader",
            "--renderer-process-limit=1",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-translate",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-hang-monitor",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--disable-domain-reliability",
            "--disable-component-update",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-ipc-flooding-protection",
            "--no-first-run",
            "--no-default-browser-check",
            "--metrics-recording-only",
            "--mute-audio",
            "--memory-pressure-off",
            "--max_old_space_size=256",
            "--disable-features=dbus",
        ]
        if os.getenv("TURNSTILE_SOLVER_DISABLE_GPU", "0") in ("1", "true", "TRUE", "yes", "YES"):
            browser_args.extend([
                "--disable-gpu",
                "--disable-gpu-compositing",
                "--disable-software-rasterizer",
                "--disable-gpu-sandbox",
                "--disable-gl-drawing-for-tests",
            ])
        if config['useragent']:
            browser_args.append(f"--user-agent={config['useragent']}")
        return browser_args

    def _process_snapshot(self):
        try:
            output = subprocess.check_output(
                ["ps", "-eo", "pid=,ppid=,args="],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return {}, {}

        parents = {}
        args_by_pid = {}
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except Exception:
                continue
            parents[pid] = ppid
            args_by_pid[pid] = parts[2]
        return parents, args_by_pid

    def _is_own_descendant(self, pid: int, parents: dict[int, int]) -> bool:
        own_pid = os.getpid()
        seen = set()
        while pid and pid not in seen:
            if pid == own_pid:
                return True
            seen.add(pid)
            pid = parents.get(pid, 0)
        return False

    def _chrome_root_processes(self) -> dict[int, str]:
        parents, args_by_pid = self._process_snapshot()
        roots = {}
        for pid, args in args_by_pid.items():
            if not self._is_own_descendant(pid, parents):
                continue
            if "chrome" not in args.lower():
                continue
            if "--type=" in args:
                continue
            if "--remote-debugging-pipe" not in args:
                continue
            roots[pid] = args
        return roots

    def _profile_dirs_from_args(self, args_list) -> set[str]:
        dirs = set()
        for args in args_list:
            match = re.search(r"--user-data-dir=([^\s]+)", args or "")
            if match:
                dirs.add(match.group(1))
        return dirs

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return True

    def _kill_tracked_browser_processes_sync(self, index: int, label: str = "") -> None:
        pids = list(self.browser_root_pids.pop(index, set()) or [])
        profile_dirs = set(self.browser_profile_dirs.pop(index, set()) or [])
        alive = [pid for pid in pids if self._pid_alive(pid)]
        if alive:
            logger.warning(f"{label}: browser root still alive after close, terminating pids={alive}")
            for pid in alive:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
            deadline = time.time() + 3
            while time.time() < deadline and any(self._pid_alive(pid) for pid in alive):
                time.sleep(0.1)
            for pid in alive:
                if self._pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass

        active_args = "\n".join(self._chrome_root_processes().values())
        for profile_dir in profile_dirs:
            try:
                if profile_dir and profile_dir.startswith("/tmp/") and profile_dir not in active_args:
                    shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception as e:
                logger.debug(f"{label}: profile cleanup failed {profile_dir}: {e}")

    async def _cleanup_await(self, awaitable, label: str, timeout: Optional[float] = None) -> bool:
        timeout = self.close_timeout if timeout is None else timeout
        task = asyncio.create_task(awaitable)
        cancelled = False
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            cancelled = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"{label} cleanup after cancellation timed out after {timeout}s")
                task.cancel()
            except Exception as e:
                logger.warning(f"{label} cleanup after cancellation failed: {type(e).__name__}: {e}")
        except asyncio.TimeoutError:
            logger.warning(f"{label} cleanup timed out after {timeout}s")
            task.cancel()
        except Exception as e:
            logger.warning(f"{label} cleanup failed: {type(e).__name__}: {e}")
        return cancelled

    async def _close_context_safely(self, context, label: str) -> bool:
        if not context:
            return False
        return await self._cleanup_await(context.close(), label)

    async def _close_browser_safely(self, browser, index: Optional[int] = None, label: str = "browser") -> bool:
        cancelled = False
        if browser:
            cancelled = await self._cleanup_await(browser.close(), label, timeout=max(self.close_timeout, 15.0))
        if index is not None:
            cleanup_cancelled = await self._cleanup_await(
                asyncio.to_thread(self._kill_tracked_browser_processes_sync, index, label),
                f"{label} process cleanup",
                timeout=8.0,
            )
            cancelled = cancelled or cleanup_cancelled
        return cancelled

    async def _finish_browser_task(self, index: int, browser, browser_config, context, label: str) -> None:
        cancelled = False
        if context:
            cancelled = await self._close_context_safely(context, f"{label} context") or cancelled
        cancelled = await self._cleanup_await(
            self._return_browser_to_pool(index, browser, browser_config),
            f"{label} return browser",
            timeout=max(self.close_timeout, 20.0),
        ) or cancelled
        if cancelled:
            raise asyncio.CancelledError()

    def _cleanup_stale_temp_artifacts_sync(self, reason: str = "") -> int:
        if self.tmp_cleanup_max_age <= 0:
            return 0
        now = time.time()
        _, all_args = self._process_snapshot()
        active_args = "\n".join(all_args.values())
        patterns = (
            "playwright_chromiumdev_profile-",
            "playwright-artifacts-",
            "xvfb-run.",
        )
        removed = 0
        try:
            entries = list(os.scandir("/tmp"))
        except Exception:
            return 0
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                name = entry.name
                if not any(name.startswith(prefix) for prefix in patterns):
                    continue
                path = entry.path
                if path in active_args:
                    continue
                if now - entry.stat(follow_symlinks=False).st_mtime < self.tmp_cleanup_max_age:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            except Exception:
                continue
        if removed:
            logger.info(f"Cleaned stale browser temp artifacts: count={removed} reason={reason}")
        return removed

    async def _launch_browser(self, config):
        if self.browser_type in ['chromium', 'chrome', 'msedge'] and self.playwright:
            return await self.playwright.chromium.launch(
                channel=self.browser_type,
                headless=self.headless,
                args=self._build_browser_args(config)
            )
        if self.browser_type in ("cloak", "cloakbrowser") and self.playwright:
            executable = self._cloak_browser_executable_path()
            if self._cloak_strict_enabled() and not executable:
                raise RuntimeError(
                    "TURNSTILE solver browser_type=cloak but no real CloakBrowser executable found; "
                    "set CLOAK_BROWSER_PATH or TURNSTILE_CLOAK_BROWSER_PATH, or set TURNSTILE_CLOAK_STRICT=0 to allow fallback."
                )
            logger.info(f"Launching CloakBrowser mode executable={executable or 'bundled chromium fallback'}")
            ignore_default_args = ["--enable-automation", "--enable-unsafe-swiftshader"]
            try:
                from cloakbrowser.config import IGNORE_DEFAULT_ARGS  # type: ignore
                ignore_default_args = IGNORE_DEFAULT_ARGS
            except Exception:
                pass
            launch_proxy = os.getenv("TURNSTILE_SOLVER_PROXY", "") or os.getenv("WARP_PROXY_URL", "") or os.getenv("SOCKS5_PROXY", "")
            return await self.playwright.chromium.launch(
                executable_path=executable,
                headless=self.headless,
                args=self._build_browser_args(config, launch_proxy=launch_proxy),
                ignore_default_args=ignore_default_args,
            )
        if self.browser_type == "camoufox" and self.camoufox:
            return await self.camoufox.start()
        raise RuntimeError(f"Unsupported or uninitialized browser type: {self.browser_type}")

    def _cloak_strict_enabled(self) -> bool:
        value = os.getenv("TURNSTILE_CLOAK_STRICT", os.getenv("CLOAK_BROWSER_STRICT", "1"))
        return str(value or "").strip().lower() not in ("0", "false", "no", "off")

    def _cloak_browser_executable_path(self) -> Optional[str]:
        explicit_candidates = [
            os.getenv("TURNSTILE_CLOAK_BROWSER_PATH", ""),
            os.getenv("CLOAK_BROWSER_PATH", ""),
            os.getenv("CLOAK_BROWSER_EXECUTABLE", ""),
        ]
        for candidate in explicit_candidates:
            if candidate and Path(candidate).exists():
                return candidate

        try:
            from cloakbrowser import ensure_binary  # type: ignore
            cloak_path = ensure_binary()
            if cloak_path and Path(cloak_path).exists():
                return str(cloak_path)
        except Exception as e:
            logger.warning(f"CloakBrowser ensure_binary unavailable: {e}")

        fallback_candidates = [
            os.getenv("CHROME_PATH", ""),
            "/ms-playwright/chromium-1223/chrome-linux64/chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
        ]
        if self._cloak_strict_enabled():
            return None
        for candidate in fallback_candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return None

    async def _add_browser_to_pool_locked(self):
        index = self.next_browser_index
        self.next_browser_index += 1
        config = self._build_browser_config()
        before_roots = self._chrome_root_processes()
        browser = await self._launch_browser(config)
        # Give the launched browser root process a brief moment to appear in ps.
        await asyncio.sleep(0.05)
        after_roots = self._chrome_root_processes()
        new_pids = set(after_roots.keys()) - set(before_roots.keys())
        if not new_pids:
            tracked = set().union(*self.browser_root_pids.values()) if self.browser_root_pids else set()
            new_pids = set(after_roots.keys()) - tracked
        root_args = [after_roots.get(pid, "") for pid in new_pids]
        self.browser_root_pids[index] = set(new_pids)
        self.browser_profile_dirs[index] = self._profile_dirs_from_args(root_args)
        self.browser_slots[index] = (browser, config)
        self.browser_use_counts[index] = 0
        await self.browser_pool.put((index, browser, config))
        if self.debug:
            logger.info(
                f"Browser {index} initialized successfully with {config['browser_name']} {config['browser_version']} "
                f"pids={sorted(new_pids)} profiles={sorted(self.browser_profile_dirs[index])}"
            )

    async def _ensure_browser_pool_target_locked(self):
        added = 0
        while len(self.browser_slots) < self.thread_count:
            await self._add_browser_to_pool_locked()
            added += 1
        return added

    async def _resize_browser_pool(self, target: int):
        if target < 1:
            target = 1

        to_close = []
        added = 0
        closed = 0
        retiring = 0

        async with self.pool_lock:
            self.thread_count = target
            self.retire_browsers.intersection_update(self.browser_slots.keys())

            while len([idx for idx in self.browser_slots.keys() if idx not in self.retire_browsers]) < target:
                await self._add_browser_to_pool_locked()
                added += 1

            active_capacity = len([idx for idx in self.browser_slots.keys() if idx not in self.retire_browsers])
            excess = active_capacity - target
            if excess > 0:
                idle = []
                while True:
                    try:
                        idle.append(self.browser_pool.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                close_indexes = set()
                for item in sorted(idle, key=lambda entry: entry[0], reverse=True):
                    if excess <= 0:
                        break
                    index, browser, _ = item
                    close_indexes.add(index)
                    self.browser_slots.pop(index, None)
                    self.browser_use_counts.pop(index, None)
                    to_close.append((index, browser))
                    closed += 1
                    excess -= 1

                for item in idle:
                    if item[0] not in close_indexes:
                        await self.browser_pool.put(item)

                if excess > 0:
                    idle_indexes = {item[0] for item in idle if item[0] not in close_indexes}
                    candidates = sorted(
                        [index for index in self.browser_slots.keys() if index not in idle_indexes],
                        reverse=True
                    )
                    for index in candidates[:excess]:
                        self.retire_browsers.add(index)
                        retiring += 1

            status = {
                "target": self.thread_count,
                "total": len(self.browser_slots),
                "idle": self.browser_pool.qsize(),
                "inUse": max(0, len(self.browser_slots) - self.browser_pool.qsize()),
                "retiring": len(self.retire_browsers),
                "added": added,
                "closed": closed,
                "markedForRetire": retiring,
            }

        for index, browser in to_close:
            await self._close_browser_safely(browser, index=index, label=f"Browser {index}: resized out")

        logger.info(f"Browser pool resized: target={status['target']} total={status['total']} idle={status['idle']} retiring={status['retiring']}")
        return status

    async def _recycle_browser_pool(self, target: Optional[int] = None, reason: str = ""):
        """Replace idle browsers immediately and retire in-use browsers.

        `--random` picks UA/Sec-CH-UA when a browser is launched, not for every
        task/context. Recycling therefore forces the next task to use a freshly
        launched browser with a new random browser config, while currently
        running tasks are closed when they eventually return to the pool.
        """
        if target is None:
            target = self.thread_count
        if target < 1:
            target = 1

        to_close = []
        closed = 0
        marked = 0
        added = 0
        generation = 0

        async with self.pool_lock:
            self.thread_count = target
            self.pool_generation += 1
            generation = self.pool_generation

            idle = []
            while True:
                try:
                    idle.append(self.browser_pool.get_nowait())
                except asyncio.QueueEmpty:
                    break

            for index, browser, _ in idle:
                self.browser_slots.pop(index, None)
                self.browser_use_counts.pop(index, None)
                self.retire_browsers.discard(index)
                to_close.append((index, browser))
                closed += 1

            # Anything left in browser_slots is currently checked out. Mark it
            # for retirement; it will be closed by _return_browser_to_pool().
            active_indexes = list(self.browser_slots.keys())
            for index in active_indexes:
                if index not in self.retire_browsers:
                    self.retire_browsers.add(index)
                    marked += 1

            for _ in range(target):
                await self._add_browser_to_pool_locked()
                added += 1

            status = {
                "target": self.thread_count,
                "total": len(self.browser_slots),
                "idle": self.browser_pool.qsize(),
                "inUse": max(0, len(self.browser_slots) - self.browser_pool.qsize()),
                "retiring": len(self.retire_browsers),
                "added": added,
                "closed": closed,
                "markedForRetire": marked,
                "generation": generation,
            }

        for index, browser in to_close:
            await self._close_browser_safely(browser, index=index, label=f"Browser {index}: recycled out")

        logger.info(
            f"Browser pool recycled: target={status['target']} total={status['total']} "
            f"idle={status['idle']} retiring={status['retiring']} generation={generation} "
            f"reason={reason[:160]}"
        )
        return status

    async def _periodic_cleanup(self):
        """Periodic cleanup of old results every hour"""
        while True:
            try:
                await asyncio.sleep(3600)
                deleted_count = await cleanup_old_results(days_old=7)
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} old results")
                await asyncio.to_thread(self._cleanup_stale_temp_artifacts_sync, "periodic")
            except Exception as e:
                logger.error(f"Error during periodic cleanup: {e}")

    async def _antishadow_inject(self, page):
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
        """Оптимизированный обработчик маршрутов для экономии ресурсов."""
        url = route.request.url
        resource_type = route.request.resource_type

        allowed_types = {'document', 'script', 'xhr', 'fetch'}

        allowed_domains = [
            'challenges.cloudflare.com',
            'static.cloudflareinsights.com',
            'cloudflare.com'
        ]
        
        if resource_type in allowed_types:
            await route.continue_()
        elif any(domain in url for domain in allowed_domains):
            await route.continue_() 
        else:
            await route.abort()

    async def _block_rendering(self, page):
        """Блокировка рендеринга для экономии ресурсов"""
        await page.route("**/*", self._optimized_route_handler)

    async def _unblock_rendering(self, page):
        """Разблокировка рендеринга"""
        await page.unroute("**/*", self._optimized_route_handler)

    def _attach_page_debug_handlers(self, page, index: int):
        if not self.debug:
            return

        def _short(value: str, limit: int = 500) -> str:
            value = str(value or "").replace("\n", "\\n")
            return value[:limit]

        page.on("console", lambda msg: logger.debug(f"Browser {index}: console[{msg.type}]: {_short(msg.text)}"))
        page.on("pageerror", lambda err: logger.debug(f"Browser {index}: pageerror: {_short(str(err))}"))
        page.on("requestfailed", lambda req: logger.debug(
            f"Browser {index}: request failed {req.resource_type} {req.url} -> {_short(req.failure or '')}"
        ))
        page.on("response", lambda resp: logger.debug(
            f"Browser {index}: response {resp.status} {resp.url}"
        ) if resp.status >= 400 and (
            "vapi.ai" in resp.url or "cloudflare" in resp.url or "turnstile" in resp.url
        ) else None)

    async def _capture_page_debug(self, page, index: int, task_id: str, label: str):
        if not self.debug and os.getenv("TURNSTILE_SOLVER_CAPTURE", "0") not in ("1", "true", "TRUE", "yes", "YES"):
            return

        safe_task_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in task_id)
        prefix = os.path.join(self.debug_dir, f"{safe_task_id}-{label}")
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"Browser {index}: Could not create debug dir {self.debug_dir}: {str(e)}")
            return

        try:
            state = await page.evaluate("""
            () => {
              const short = (value) => String(value || '').slice(0, 240);
              return {
                href: location.href,
                title: document.title,
                readyState: document.readyState,
                userAgent: navigator.userAgent,
                turnstileType: typeof window.turnstile,
                cfTurnstileCount: document.querySelectorAll('.cf-turnstile').length,
                sitekeyCount: document.querySelectorAll('[data-sitekey]').length,
                tokenInputs: Array.from(document.querySelectorAll('input[name="cf-turnstile-response"]')).map(input => ({
                  length: (input.value || '').length,
                  head: short(input.value)
                })),
                iframes: Array.from(document.querySelectorAll('iframe')).map(frame => ({
                  title: short(frame.getAttribute('title')),
                  src: short(frame.getAttribute('src'))
                })),
                bodyText: short(document.body ? document.body.innerText : '')
              };
            }
            """)
            logger.debug(f"Browser {index}: page state {label}: {state}")
        except Exception as e:
            logger.debug(f"Browser {index}: Could not read page state {label}: {str(e)}")

        try:
            await page.screenshot(path=f"{prefix}.png", full_page=True, timeout=5000)
            logger.debug(f"Browser {index}: Saved screenshot {prefix}.png")
        except Exception as e:
            logger.debug(f"Browser {index}: Could not save screenshot {label}: {str(e)}")

        try:
            html = await page.content()
            with open(f"{prefix}.html", "w", encoding="utf-8") as fh:
                fh.write(html)
            logger.debug(f"Browser {index}: Saved HTML {prefix}.html")
        except Exception as e:
            logger.debug(f"Browser {index}: Could not save HTML {label}: {str(e)}")

    def _select_proxy(self, proxy_url: Optional[str], index: int) -> Optional[str]:
        if proxy_url:
            if self.debug:
                logger.debug(f"Browser {index}: Selected request proxy: {proxy_url}")
            return proxy_url

        if not self.proxy_support:
            return None

        proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
        try:
            with open(proxy_file_path) as proxy_file:
                proxies = [line.strip() for line in proxy_file if line.strip()]
            proxy = random.choice(proxies) if proxies else None
            if self.debug and proxy:
                logger.debug(f"Browser {index}: Selected proxy: {proxy}")
            elif self.debug and not proxy:
                logger.debug(f"Browser {index}: No proxies available")
            return proxy
        except FileNotFoundError:
            logger.warning(f"Proxy file not found: {proxy_file_path}")
            return None
        except Exception as e:
            logger.error(f"Error reading proxy file: {str(e)}")
            return None

    def _proxy_context_option(self, proxy: str) -> dict:
        if "://" in proxy and "@" in proxy:
            scheme_part, auth_part = proxy.split("://", 1)
            auth, address = auth_part.rsplit("@", 1)
            username, password = auth.split(":", 1)
            return {
                "server": f"{scheme_part}://{address}",
                "username": username,
                "password": password,
            }

        parts = proxy.split(":")
        if "://" not in proxy and len(parts) == 5:
            proxy_scheme, proxy_ip, proxy_port, proxy_user, proxy_pass = parts
            return {
                "server": f"{proxy_scheme}://{proxy_ip}:{proxy_port}",
                "username": proxy_user,
                "password": proxy_pass,
            }

        return {"server": proxy}

    def _proxy_launch_arg(self, proxy: str) -> list[str]:
        proxy = str(proxy or "").strip()
        return [f"--proxy-server={proxy}"] if proxy else []

    def _cloak_proxy_launch_arg_enabled(self) -> bool:
        value = os.getenv("TURNSTILE_CLOAK_PROXY_LAUNCH_ARG", os.getenv("CLOAK_PROXY_LAUNCH_ARG", "1"))
        return str(value or "").strip().lower() not in ("0", "false", "no", "off")

    async def _new_browser_context(self, browser, browser_config, index: int, proxy_url: Optional[str] = None, viewport: Optional[dict] = None):
        headers = {"Accept-Language": "en-US,en;q=0.9"}
        native_cloak_ua = self.browser_type in ("cloak", "cloakbrowser") and os.getenv("TURNSTILE_CLOAK_NATIVE_UA", "1").strip().lower() not in ("0", "false", "no", "off")
        if browser_config.get("sec_ch_ua") and not native_cloak_ua:
            headers["sec-ch-ua"] = browser_config["sec_ch_ua"]
            headers["sec-ch-ua-mobile"] = "?0"
            headers["sec-ch-ua-platform"] = '"Windows"'

        context_options = {
            "locale": "en-US",
            "timezone_id": os.getenv("BILLING_TIMEZONE_ID", "Europe/Berlin"),
            "extra_http_headers": headers,
        }
        if browser_config.get("useragent") and not native_cloak_ua:
            context_options["user_agent"] = browser_config["useragent"]
        if viewport:
            context_options["viewport"] = viewport

        proxy = self._select_proxy(proxy_url, index)
        if proxy and not (self.browser_type in ("cloak", "cloakbrowser") and self._cloak_proxy_launch_arg_enabled()):
            context_options["proxy"] = self._proxy_context_option(proxy)
            if self.debug:
                proxy_server = context_options["proxy"].get("server", proxy)
                logger.debug(f"Browser {index}: Creating context with proxy {proxy_server}")
        elif proxy and self.debug:
            logger.debug(f"Browser {index}: Proxy handled at Cloak launch level: {proxy}")
        elif self.debug:
            logger.debug(f"Browser {index}: Creating context without proxy")

        return await browser.new_context(**context_options)

    def _browser_fingerprint_result(self, browser_config: dict) -> dict:
        return {
            "user_agent": browser_config.get("useragent", ""),
            "sec_ch_ua": browser_config.get("sec_ch_ua", ""),
            "browser_name": browser_config.get("browser_name", ""),
            "browser_version": browser_config.get("browser_version", ""),
        }

    async def _load_stripe_js(self, page, index: int, timeout_ms: int = 30000):
        last_error = ""
        per_attempt_timeout = max(1000, timeout_ms // 3)
        for attempt in range(3):
            try:
                await page.evaluate(
                    """() => new Promise((resolve, reject) => {
                        if (typeof window.Stripe === 'function') {
                            resolve(true);
                            return;
                        }
                        document.querySelectorAll('script[data-vapi-stripe-js]').forEach((script) => script.remove());
                        const script = document.createElement('script');
                        const timeout = setTimeout(() => reject(new Error('Stripe.js script load timed out')), 10000);
                        script.src = 'https://js.stripe.com/v3/';
                        script.async = true;
                        script.dataset.vapiStripeJs = '1';
                        script.onload = () => {
                            clearTimeout(timeout);
                            resolve(true);
                        };
                        script.onerror = () => {
                            clearTimeout(timeout);
                            reject(new Error('Stripe.js script load failed'));
                        };
                        document.head.appendChild(script);
                    })"""
                )
                await page.wait_for_function("() => typeof window.Stripe === 'function'", timeout=per_attempt_timeout)
                return
            except Exception as e:
                last_error = str(e)
                if self.debug:
                    logger.debug(f"Browser {index}: Stripe.js load attempt {attempt + 1} failed: {last_error[:240]}")
                await page.wait_for_timeout(500 * (attempt + 1))

        try:
            stripe_type = await page.evaluate("() => typeof window.Stripe")
        except Exception:
            stripe_type = "unavailable"
        raise RuntimeError(f"Stripe.js not ready after retries: typeof window.Stripe={stripe_type}; last_error={last_error[:300]}")

    async def _mount_stripe_elements(self, page, publishable_key: str, index: int):
        await self._load_stripe_js(page, index)
        await page.evaluate(
            """async (publishableKey) => {
                if (typeof window.Stripe !== 'function') {
                    throw new Error(`Stripe.js not ready: typeof window.Stripe=${typeof window.Stripe}`);
                }
                const stripe = window.Stripe(publishableKey);
                const elements = stripe.elements({ locale: 'en' });
                const style = {
                    base: {
                        fontSize: '16px',
                        color: '#101828',
                        '::placeholder': { color: '#667085' },
                    },
                };
                const cardNumber = elements.create('cardNumber', { style, showIcon: true });
                const cardExpiry = elements.create('cardExpiry', { style });
                const cardCvc = elements.create('cardCvc', { style });
                window.__vapiStripe = stripe;
                window.__vapiStripeElements = elements;
                window.__vapiStripeCardNumber = cardNumber;
                window.__vapiStripeCardExpiry = cardExpiry;
                window.__vapiStripeCardCvc = cardCvc;
                const state = { cardNumber: false, cardExpiry: false, cardCvc: false };
                window.__vapiStripeReadyState = state;
                window.__vapiStripeReady = Promise.all([
                    new Promise((resolve) => cardNumber.on('ready', () => { state.cardNumber = true; resolve(true); })),
                    new Promise((resolve) => cardExpiry.on('ready', () => { state.cardExpiry = true; resolve(true); })),
                    new Promise((resolve) => cardCvc.on('ready', () => { state.cardCvc = true; resolve(true); })),
                ]).then(() => true);
                cardNumber.mount('#card-number');
                cardExpiry.mount('#card-expiry');
                cardCvc.mount('#card-cvc');
            }""",
            publishable_key,
        )
        # Stripe ready 事件偶发不触发；真实 iframe input 可见即可继续填卡。
        await self._find_stripe_input_frame(page, "cardnumber")
        await self._find_stripe_input_frame(page, "exp-date")
        await self._find_stripe_input_frame(page, "cvc")

    async def _return_browser_to_pool(self, index: int, browser, browser_config):
        try:
            connected = True
            if hasattr(browser, "is_connected"):
                connected = browser.is_connected()

            if connected:
                close_browser = False
                recycle_by_usage = False
                use_count = 0
                added = 0
                async with self.pool_lock:
                    if index in self.retire_browsers:
                        self.retire_browsers.discard(index)
                        self.browser_slots.pop(index, None)
                        self.browser_use_counts.pop(index, None)
                        close_browser = True
                        added = await self._ensure_browser_pool_target_locked()
                    else:
                        use_count = self.browser_use_counts.get(index, 0) + 1
                        self.browser_use_counts[index] = use_count
                        if self.max_tasks_per_browser > 0 and use_count >= self.max_tasks_per_browser:
                            self.browser_slots.pop(index, None)
                            self.browser_use_counts.pop(index, None)
                            close_browser = True
                            recycle_by_usage = True
                            self.pool_generation += 1
                            added = await self._ensure_browser_pool_target_locked()
                        else:
                            await self.browser_pool.put((index, browser, browser_config))

                if close_browser:
                    await self._close_browser_safely(browser, index=index, label=f"Browser {index}: retired")
                    if recycle_by_usage:
                        logger.info(
                            f"Browser {index}: recycled after {use_count}/{self.max_tasks_per_browser} tasks; "
                            f"added={added} generation={self.pool_generation}"
                        )
                    elif self.debug:
                        logger.debug(f"Browser {index}: Browser retired after task")
                elif self.debug:
                    logger.debug(f"Browser {index}: Browser returned to pool use={use_count}/{self.max_tasks_per_browser or 'disabled'}")
            else:
                added = 0
                async with self.pool_lock:
                    self.browser_slots.pop(index, None)
                    self.browser_use_counts.pop(index, None)
                    self.retire_browsers.discard(index)
                    added = await self._ensure_browser_pool_target_locked()
                await self._cleanup_await(
                    asyncio.to_thread(self._kill_tracked_browser_processes_sync, index, f"Browser {index}: disconnected"),
                    f"Browser {index}: disconnected process cleanup",
                    timeout=8.0,
                )
                if self.debug:
                    logger.warning(f"Browser {index}: Browser disconnected, replaced={added}")
        except Exception as e:
            if self.debug:
                logger.warning(f"Browser {index}: Error returning browser to pool: {str(e)}")

    async def _find_stripe_input_frame(self, page, input_name: str, timeout_ms: int = 45000):
        selector = f'input[name="{input_name}"]'
        deadline = time.time() + (timeout_ms / 1000)
        last_error = ""
        while time.time() < deadline:
            for frame in page.frames:
                try:
                    locator = frame.locator(selector).first
                    if await locator.count() and await locator.is_visible(timeout=200):
                        return frame
                except Exception as e:
                    last_error = str(e)
            await page.wait_for_timeout(250)
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"Stripe iframe input not found: {input_name}{detail}")

    async def _fill_stripe_input(self, page, input_name: str, value: str, expected_digits: str = ""):
        frame = await self._find_stripe_input_frame(page, input_name)
        locator = frame.locator(f'input[name="{input_name}"]').first

        async def read_digits() -> str:
            try:
                return re.sub(r"\D", "", await locator.input_value(timeout=1500))
            except Exception:
                return ""

        await locator.click(timeout=5000)
        try:
            await locator.fill(value, timeout=7000)
        except Exception:
            await locator.type(value, delay=35, timeout=15000)

        if expected_digits:
            current_digits = await read_digits()
            if current_digits != expected_digits:
                try:
                    await locator.fill("", timeout=3000)
                    await locator.fill(value, timeout=7000)
                except Exception:
                    await locator.click(timeout=5000)
                    await locator.press("Control+A", timeout=3000)
                    await locator.press("Backspace", timeout=3000)
                    await locator.type(value, delay=35, timeout=15000)
                current_digits = await read_digits()
                if current_digits != expected_digits:
                    raise RuntimeError(
                        f"Stripe input {input_name} incomplete after fill: got {len(current_digits)} digits, expected {len(expected_digits)}"
                    )

    def _stripe_error_summary(self, result) -> str:
        if not isinstance(result, dict):
            return str(result)[:300]
        error = result.get("error")
        if isinstance(error, dict):
            parts = [
                str(error.get(key) or "")
                for key in ("message", "code", "decline_code", "type")
                if error.get(key)
            ]
            return "; ".join(parts)[:300] or str(error)[:300]
        return str(error or result)[:300]

    async def _solve_stripe_payment_method(self, task_id: str, card: dict, email: str, publishable_key: str, proxy_url: Optional[str] = None):
        index, browser, browser_config = await self.browser_pool.get()
        context = None
        start_time = time.time()

        try:
            if hasattr(browser, "is_connected") and not browser.is_connected():
                async with self.pool_lock:
                    self.browser_slots.pop(index, None)
                    self.browser_use_counts.pop(index, None)
                    self.retire_browsers.discard(index)
                    await self._ensure_browser_pool_target_locked()
                await save_result(task_id, "stripe_payment_method", {
                    "value": "STRIPE_FAIL",
                    "elapsed_time": 0,
                    "error": "browser disconnected",
                })
                return

            context = await self._new_browser_context(
                browser,
                browser_config,
                index,
                proxy_url=proxy_url,
                viewport={"width": 1280, "height": 720},
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = await context.new_page()
            self._attach_page_debug_handlers(page, index)

            await page.goto("https://dashboard.vapi.ai/settings/billing", wait_until="domcontentloaded", timeout=60000)
            await page.set_content(
                """
                <!doctype html>
                <html>
                  <head>
                    <meta charset="utf-8">
                    <title>Stripe PaymentMethod</title>
                    <style>
                      body { margin: 24px; font-family: Arial, sans-serif; }
                      .field { width: 420px; min-height: 44px; margin: 12px 0; padding: 12px; border: 1px solid #d0d5dd; border-radius: 6px; }
                    </style>
                  </head>
                  <body>
                    <div id="card-number" class="field"></div>
                    <div id="card-expiry" class="field"></div>
                    <div id="card-cvc" class="field"></div>
                  </body>
                </html>
                """,
                wait_until="domcontentloaded",
            )
            await self._mount_stripe_elements(page, publishable_key, index)

            exp_year = str(card["exp_year"])
            exp_digits = f"{card['exp_month']}{exp_year[-2:]}"
            await self._fill_stripe_input(page, "cardnumber", card["number"], re.sub(r"\D", "", card["number"]))
            await self._fill_stripe_input(page, "exp-date", f"{card['exp_month']} / {exp_year[-2:]}", exp_digits)
            await self._fill_stripe_input(page, "cvc", card["cvc"], re.sub(r"\D", "", card["cvc"]))

            result = await page.evaluate(
                """async (billingDetails) => {
                    const result = await window.__vapiStripe.createPaymentMethod({
                        type: 'card',
                        card: window.__vapiStripeCardNumber,
                        billing_details: billingDetails,
                    });
                    if (result.error) {
                        return {
                            ok: false,
                            error: {
                                message: result.error.message || '',
                                code: result.error.code || '',
                                decline_code: result.error.decline_code || '',
                                type: result.error.type || '',
                            },
                        };
                    }
                    return { ok: true, id: result.paymentMethod && result.paymentMethod.id };
                }""",
                {"email": email, "name": email.split("@", 1)[0] or email},
            )

            if not isinstance(result, dict) or not result.get("ok"):
                elapsed_time = round(time.time() - start_time, 3)
                summary = self._stripe_error_summary(result)
                await save_result(task_id, "stripe_payment_method", {
                    "value": "STRIPE_FAIL",
                    "elapsed_time": elapsed_time,
                    "error": summary,
                    "user_agent": browser_config.get("useragent", ""),
                })
                logger.error(f"Browser {index}: Stripe PaymentMethod failed in {elapsed_time}s: {summary}")
                return

            pm_id = result.get("id")
            if not pm_id:
                raise RuntimeError(f"Stripe returned no payment method id: {result}")

            elapsed_time = round(time.time() - start_time, 3)
            await save_result(task_id, "stripe_payment_method", {
                "value": pm_id,
                "payment_method_id": pm_id,
                "elapsed_time": elapsed_time,
                "user_agent": browser_config.get("useragent", ""),
                "browser_name": browser_config.get("browser_name", ""),
                "browser_version": browser_config.get("browser_version", ""),
            })
            logger.success(f"Browser {index}: Stripe PaymentMethod created {pm_id[:7]}... in {elapsed_time}s")
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            try:
                if "page" in locals():
                    await self._capture_page_debug(page, index, task_id, "stripe-exception")
            except Exception:
                pass
            await save_result(task_id, "stripe_payment_method", {
                "value": "STRIPE_FAIL",
                "elapsed_time": elapsed_time,
                "error": str(e)[:500],
                "user_agent": browser_config.get("useragent", ""),
            })
            logger.error(f"Browser {index}: Stripe PaymentMethod task failed: {str(e)}")
        finally:
            await self._finish_browser_task(index, browser, browser_config, context, f"Browser {index}: Stripe")

    async def _find_turnstile_elements(self, page, index: int):
        """Умная проверка всех возможных Turnstile элементов"""
        selectors = [
            '.cf-turnstile',
            '[data-sitekey]',
            'iframe[src*="turnstile"]',
            'iframe[title*="widget"]',
            'div[id*="turnstile"]',
            'div[class*="turnstile"]'
        ]
        
        elements = []
        for selector in selectors:
            try:
                # Безопасная проверка count()
                try:
                    count = await page.locator(selector).count()
                except Exception:
                    # Если count() дает ошибку, пропускаем этот селектор
                    continue
                    
                if count > 0:
                    elements.append((selector, count))
                    if self.debug:
                        logger.debug(f"Browser {index}: Found {count} elements with selector '{selector}'")
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Selector '{selector}' failed: {str(e)}")
                continue
        
        return elements

    async def _find_and_click_checkbox(self, page, index: int):
        """Найти и кликнуть по чекбоксу Turnstile CAPTCHA внутри iframe"""
        try:
            # Пробуем разные селекторы iframe с защитой от ошибок
            iframe_selectors = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'iframe[title*="widget"]'
            ]
            
            iframe_locator = None
            for selector in iframe_selectors:
                try:
                    test_locator = page.locator(selector).first
                    # Безопасная проверка count для iframe
                    try:
                        iframe_count = await test_locator.count()
                    except Exception:
                        iframe_count = 0
                        
                    if iframe_count > 0:
                        iframe_locator = test_locator
                        if self.debug:
                            logger.debug(f"Browser {index}: Found Turnstile iframe with selector: {selector}")
                        break
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Iframe selector '{selector}' failed: {str(e)}")
                    continue
            
            if iframe_locator:
                try:
                    # Получаем frame из iframe
                    iframe_element = await iframe_locator.element_handle(timeout=1500)
                    frame = await iframe_element.content_frame()
                    
                    if frame:
                        # Ищем чекбокс внутри iframe
                        checkbox_selectors = [
                            'input[type="checkbox"]',
                            '.cb-lb input[type="checkbox"]',
                            'label input[type="checkbox"]'
                        ]
                        
                        for selector in checkbox_selectors:
                            try:
                                # Полностью избегаем locator.count() в iframe - используем альтернативный подход
                                try:
                                    # Пробуем кликнуть напрямую без count проверки
                                    checkbox = frame.locator(selector).first
                                    await checkbox.click(timeout=2000)
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Successfully clicked checkbox in iframe with selector '{selector}'")
                                    return True
                                except Exception as click_e:
                                    # Если прямой клик не сработал, записываем в debug но не падаем
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Direct checkbox click failed for '{selector}': {str(click_e)}")
                                    continue
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Iframe checkbox selector '{selector}' failed: {str(e)}")
                                continue
                    
                        # Если нашли iframe, но не смогли кликнуть чекбокс, пробуем клик по iframe
                        try:
                            if self.debug:
                                logger.debug(f"Browser {index}: Trying to click iframe directly as fallback")
                            await iframe_locator.click(timeout=1000)
                            return True
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Iframe direct click failed: {str(e)}")
                
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Failed to access iframe content: {str(e)}")
                    try:
                        if self.debug:
                            logger.debug(f"Browser {index}: Trying forced iframe click after content access failure")
                        await iframe_locator.click(timeout=1500, force=True)
                        return True
                    except Exception as click_e:
                        if self.debug:
                            logger.debug(f"Browser {index}: Forced iframe click failed: {str(click_e)}")
            
        except Exception as e:
            if self.debug:
                logger.debug(f"Browser {index}: General iframe search failed: {str(e)}")
        
        return False

    async def _try_click_strategies(self, page, index: int):
        strategies = [
            ('checkbox_click', lambda: self._find_and_click_checkbox(page, index)),
            ('direct_widget', lambda: self._safe_click(page, '.cf-turnstile', index)),
            ('iframe_click', lambda: self._safe_click(page, 'iframe[src*="turnstile"]', index)),
            ('js_click', lambda: page.evaluate("document.querySelector('.cf-turnstile')?.click()")),
            ('sitekey_attr', lambda: self._safe_click(page, '[data-sitekey]', index)),
            ('any_turnstile', lambda: self._safe_click(page, '*[class*="turnstile"]', index)),
            ('xpath_click', lambda: self._safe_click(page, "//div[@class='cf-turnstile']", index))
        ]
        
        for strategy_name, strategy_func in strategies:
            try:
                result = await strategy_func()
                if result is True or result is None:  # None означает успех для большинства стратегий
                    if self.debug:
                        logger.debug(f"Browser {index}: Click strategy '{strategy_name}' succeeded")
                    return True
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Click strategy '{strategy_name}' failed: {str(e)}")
                continue
        
        return False

    async def _safe_click(self, page, selector: str, index: int):
        """Полностью безопасный клик с максимальной защитой от ошибок"""
        try:
            # Пробуем кликнуть напрямую без count() проверки
            locator = page.locator(selector).first
            await locator.click(timeout=1000, force=True)
            return True
        except Exception as e:
            # Логируем ошибку только в debug режиме
            if self.debug and "Can't query n-th element" not in str(e):
                logger.debug(f"Browser {index}: Safe click failed for '{selector}': {str(e)}")
            return False

    async def _get_turnstile_token_from_page(self, page, index: int) -> str:
        try:
            token = await page.evaluate("""
            () => {
              const values = [];
              if (window.__nexosTurnstileToken) values.push(window.__nexosTurnstileToken);
              for (const input of document.querySelectorAll('input[name="cf-turnstile-response"]')) {
                if (input.value) values.push(input.value);
              }
              return values.find(value => String(value || '').length > 20) || '';
            }
            """)
            return token or ""
        except Exception as e:
            if self.debug:
                logger.debug(f"Browser {index}: Failed reading Turnstile token from page: {str(e)}")
            return ""

    async def _try_execute_turnstile(self, page, sitekey: str, index: int):
        try:
            result = await page.evaluate("""
            async (sitekey) => {
              const out = {turnstileType: typeof window.turnstile, candidates: 0, executed: 0, rendered: 0, errors: []};
              const saveToken = (token) => {
                if (!token) return;
                window.__nexosTurnstileToken = token;
                let inputs = Array.from(document.querySelectorAll('input[name="cf-turnstile-response"]'));
                if (!inputs.length) {
                  const input = document.createElement('input');
                  input.type = 'hidden';
                  input.name = 'cf-turnstile-response';
                  document.body.appendChild(input);
                  inputs = [input];
                }
                for (const input of inputs) {
                  input.value = token;
                  input.dispatchEvent(new Event('input', {bubbles: true}));
                  input.dispatchEvent(new Event('change', {bubbles: true}));
                }
              };

              if (!window.turnstile) {
                return out;
              }

              const candidates = Array.from(document.querySelectorAll('[id^="captcha-"], .cf-turnstile, [data-sitekey]'))
                .filter((el) => el && el.nodeType === Node.ELEMENT_NODE && el.tagName !== 'SCRIPT');
              out.candidates = candidates.length;

              const renderOptions = {
                sitekey,
                size: 'invisible',
                retry: 'auto',
                'retry-interval': 1000,
                'refresh-expired': 'auto',
                'refresh-timeout': 'auto',
                callback: saveToken,
                'error-callback': (error) => console.log('Nexos Turnstile error:', error),
                'expired-callback': () => console.log('Nexos Turnstile expired'),
                'timeout-callback': () => console.log('Nexos Turnstile timeout')
              };

              if (typeof window.turnstile.execute === 'function') {
                for (const el of candidates) {
                  try {
                    window.turnstile.execute(el);
                    out.executed += 1;
                  } catch (e) {
                    out.errors.push(`execute-el:${String(e && e.message || e).slice(0, 120)}`);
                  }
                }
              }

              if (typeof window.turnstile.render === 'function' && !window.__nexosTurnstileWidgetId) {
                let fallback = document.getElementById('nexos-turnstile-hidden');
                if (!fallback) {
                  fallback = document.createElement('div');
                  fallback.id = 'nexos-turnstile-hidden';
                  fallback.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;overflow:hidden;z-index:-1;';
                  document.body.appendChild(fallback);
                }
                try {
                  window.__nexosTurnstileWidgetId = window.turnstile.render(fallback, renderOptions);
                  out.rendered += 1;
                } catch (e) {
                  out.errors.push(`render:${String(e && e.message || e).slice(0, 120)}`);
                }
              }

              if (typeof window.turnstile.execute === 'function' && window.__nexosTurnstileWidgetId) {
                try {
                  window.turnstile.execute(window.__nexosTurnstileWidgetId);
                  out.executed += 1;
                } catch (e) {
                  out.errors.push(`execute-widget:${String(e && e.message || e).slice(0, 120)}`);
                }
              }

              if (typeof window.turnstile.getResponse === 'function' && window.__nexosTurnstileWidgetId) {
                try {
                  saveToken(window.turnstile.getResponse(window.__nexosTurnstileWidgetId));
                } catch (e) {
                  out.errors.push(`getResponse:${String(e && e.message || e).slice(0, 120)}`);
                }
              }

              return out;
            }
            """, sitekey)
            if self.debug:
                logger.debug(f"Browser {index}: Turnstile execute result: {result}")
            return result
        except Exception as e:
            if self.debug:
                logger.debug(f"Browser {index}: Turnstile execute failed: {str(e)}")
            return None

    async def _inject_captcha_directly(self, page, websiteKey: str, action: str = '', cdata: str = '', index: int = 0):
        """Inject CAPTCHA directly into the target website"""
        script = f"""
        // Remove any existing turnstile widgets first
        document.querySelectorAll('.cf-turnstile').forEach(el => el.remove());
        document.querySelectorAll('[data-sitekey]').forEach(el => el.remove());
        
        // Create turnstile widget directly on the page
        const captchaDiv = document.createElement('div');
        captchaDiv.className = 'cf-turnstile';
        captchaDiv.setAttribute('data-sitekey', '{websiteKey}');
        captchaDiv.setAttribute('data-callback', 'onTurnstileCallback');
        {f'captchaDiv.setAttribute("data-action", "{action}");' if action else ''}
        {f'captchaDiv.setAttribute("data-cdata", "{cdata}");' if cdata else ''}
        captchaDiv.style.position = 'fixed';
        captchaDiv.style.top = '20px';
        captchaDiv.style.left = '20px';
        captchaDiv.style.zIndex = '9999';
        captchaDiv.style.backgroundColor = 'white';
        captchaDiv.style.padding = '15px';
        captchaDiv.style.border = '2px solid #0f79af';
        captchaDiv.style.borderRadius = '8px';
        captchaDiv.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.3)';
        
        // Add to body immediately
        document.body.appendChild(captchaDiv);
        
        // Load Turnstile script and render widget
        const loadTurnstile = () => {{
            const script = document.createElement('script');
            script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
            script.async = true;
            script.defer = true;
            script.onload = function() {{
                console.log('Turnstile script loaded');
                // Wait a bit for script to initialize
                setTimeout(() => {{
                    if (window.turnstile && window.turnstile.render) {{
                        try {{
                            window.turnstile.render(captchaDiv, {{
                                sitekey: '{websiteKey}',
                                {f'action: "{action}",' if action else ''}
                                {f'cdata: "{cdata}",' if cdata else ''}
                                callback: function(token) {{
                                    console.log('Turnstile solved with token:', token);
                                    // Create hidden input for token
                                    let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                                    if (!tokenInput) {{
                                        tokenInput = document.createElement('input');
                                        tokenInput.type = 'hidden';
                                        tokenInput.name = 'cf-turnstile-response';
                                        document.body.appendChild(tokenInput);
                                    }}
                                    tokenInput.value = token;
                                }},
                                'error-callback': function(error) {{
                                    console.log('Turnstile error:', error);
                                }}
                            }});
                        }} catch (e) {{
                            console.log('Turnstile render error:', e);
                        }}
                    }} else {{
                        console.log('Turnstile API not available');
                    }}
                }}, 1000);
            }};
            script.onerror = function() {{
                console.log('Failed to load Turnstile script');
            }};
            document.head.appendChild(script);
        }};
        
        // Check if Turnstile is already loaded
        if (window.turnstile) {{
            console.log('Turnstile already loaded, rendering immediately');
            try {{
                window.turnstile.render(captchaDiv, {{
                    sitekey: '{websiteKey}',
                    {f'action: "{action}",' if action else ''}
                    {f'cdata: "{cdata}",' if cdata else ''}
                    callback: function(token) {{
                        console.log('Turnstile solved with token:', token);
                        let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                        if (!tokenInput) {{
                            tokenInput = document.createElement('input');
                            tokenInput.type = 'hidden';
                            tokenInput.name = 'cf-turnstile-response';
                            document.body.appendChild(tokenInput);
                        }}
                        tokenInput.value = token;
                    }},
                    'error-callback': function(error) {{
                        console.log('Turnstile error:', error);
                    }}
                }});
            }} catch (e) {{
                console.log('Immediate render error:', e);
                loadTurnstile();
            }}
        }} else {{
            loadTurnstile();
        }}
        
        // Setup global callback
        window.onTurnstileCallback = function(token) {{
            console.log('Global turnstile callback executed:', token);
        }};
        """

        await page.evaluate(script)
        if self.debug:
            logger.debug(f"Browser {index}: Injected CAPTCHA directly into website with sitekey: {websiteKey}")

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: Optional[str] = None, cdata: Optional[str] = None, proxy_url: Optional[str] = None):
        """Solve the Turnstile challenge."""
        index, browser, browser_config = await self.browser_pool.get()
        context = None
        start_time = time.time()

        try:
            if hasattr(browser, 'is_connected') and not browser.is_connected():
                if self.debug:
                    logger.warning(f"Browser {index}: Browser disconnected, skipping")
                await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0})
                return

            proxy = self._select_proxy(proxy_url, index)
            context = await self._new_browser_context(
                browser,
                browser_config,
                index,
                proxy_url=proxy,
                viewport={"width": 500, "height": 240},
            )
            page = await context.new_page()
            self._attach_page_debug_handlers(page, index)
            await self._antishadow_inject(page)
            await self._block_rendering(page)
            await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });

            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
            };
            """)

            if self.debug:
                logger.debug(f"Browser {index}: Starting Turnstile solve for URL: {url} with Sitekey: {sitekey} | Action: {action} | Cdata: {cdata} | Proxy: {proxy}")
                logger.debug(f"Browser {index}: Loading real website directly: {url}")

            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await self._capture_page_debug(page, index, task_id, "after-goto")

            if self.debug:
                logger.debug(f"Browser {index}: Waiting for page-owned invisible Turnstile widget")

            await asyncio.sleep(5)
            await self._try_execute_turnstile(page, sitekey, index)
            await self._capture_page_debug(page, index, task_id, "after-page-widget")

            locator = page.locator('input[name="cf-turnstile-response"]')
            max_attempts = 60
            click_count = 0
            max_clicks = 3

            for attempt in range(max_attempts):
                try:
                    direct_token = await self._get_turnstile_token_from_page(page, index)
                    if direct_token:
                        elapsed_time = round(time.time() - start_time, 3)
                        logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{direct_token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                        await save_result(task_id, "turnstile", {
                            "value": direct_token,
                            "elapsed_time": elapsed_time,
                            **self._browser_fingerprint_result(browser_config),
                        })
                        return

                    try:
                        count = await locator.count()
                    except Exception as e:
                        if self.debug:
                            logger.debug(f"Browser {index}: Locator count failed on attempt {attempt + 1}: {str(e)}")
                        count = 0

                    if count == 0:
                        if self.debug and attempt % 5 == 0:
                            logger.debug(f"Browser {index}: No token elements found on attempt {attempt + 1}")
                    elif count == 1:
                        try:
                            token = await locator.input_value(timeout=500)
                            if token:
                                elapsed_time = round(time.time() - start_time, 3)
                                logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                                await save_result(task_id, "turnstile", {
                                    "value": token,
                                    "elapsed_time": elapsed_time,
                                    **self._browser_fingerprint_result(browser_config),
                                })
                                return
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Single token element check failed: {str(e)}")
                    else:
                        if self.debug:
                            logger.debug(f"Browser {index}: Found {count} token elements, checking all")
                        for i in range(count):
                            try:
                                element_token = await locator.nth(i).input_value(timeout=500)
                                if element_token:
                                    elapsed_time = round(time.time() - start_time, 3)
                                    logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{element_token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                                    await save_result(task_id, "turnstile", {
                                        "value": element_token,
                                        "elapsed_time": elapsed_time,
                                        **self._browser_fingerprint_result(browser_config),
                                    })
                                    return
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Token element {i} check failed: {str(e)}")
                                continue

                    if attempt > 1 and attempt % 4 == 0:
                        await self._try_execute_turnstile(page, sitekey, index)

                    if attempt > 8 and attempt % 8 == 0 and click_count < max_clicks:
                        click_success = await self._try_click_strategies(page, index)
                        click_count += 1
                        if click_success and self.debug:
                            logger.debug(f"Browser {index}: Click successful (click #{click_count}/{max_clicks})")
                        elif not click_success and self.debug:
                            logger.debug(f"Browser {index}: All click strategies failed on attempt {attempt + 1} (click #{click_count}/{max_clicks})")

                    wait_time = min(0.5 + (attempt * 0.05), 2.0)
                    await asyncio.sleep(wait_time)

                    if self.debug and attempt % 5 == 0:
                        logger.debug(f"Browser {index}: Attempt {attempt + 1}/{max_attempts} - Waiting for token (clicks: {click_count}/{max_clicks})")

                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Attempt {attempt + 1} error: {str(e)}")
                    continue

            elapsed_time = round(time.time() - start_time, 3)
            if context:
                await self._capture_page_debug(page, index, task_id, "failed")
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time})
            if self.debug:
                logger.error(f"Browser {index}: Error solving Turnstile in {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds")
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            try:
                if "page" in locals():
                    await self._capture_page_debug(page, index, task_id, "exception")
            except Exception:
                pass
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time, "error": str(e)[:500]})
            if self.debug:
                logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
        finally:
            if self.debug:
                logger.debug(f"Browser {index}: Closing browser context and cleaning up")
            await self._finish_browser_task(index, browser, browser_config, context, f"Browser {index}: Turnstile")

    async def _solve_turnstile_guarded(self, task_id: str, url: str, sitekey: str, action: Optional[str] = None, cdata: Optional[str] = None, proxy_url: Optional[str] = None):
        timeout = float(os.getenv("TURNSTILE_SOLVER_TASK_TIMEOUT", "90"))
        task = asyncio.create_task(self._solve_turnstile(task_id=task_id, url=url, sitekey=sitekey, action=action, cdata=cdata, proxy_url=proxy_url))
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": timeout, "error": "task timeout"})
            logger.error(f"Task {task_id}: Turnstile solve timed out after {timeout}s")
        except Exception as e:
            await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0, "error": str(e)})
            logger.error(f"Task {task_id}: Turnstile solve failed: {str(e)}")

    async def _solve_stripe_payment_method_guarded(self, task_id: str, card: dict, email: str, publishable_key: str, proxy_url: Optional[str] = None):
        timeout = float(os.getenv("STRIPE_SOLVER_TASK_TIMEOUT", os.getenv("TURNSTILE_SOLVER_TASK_TIMEOUT", "90")))
        task = asyncio.create_task(self._solve_stripe_payment_method(
            task_id=task_id,
            card=card,
            email=email,
            publishable_key=publishable_key,
            proxy_url=proxy_url,
        ))
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
            await save_result(task_id, "stripe_payment_method", {
                "value": "STRIPE_FAIL",
                "elapsed_time": timeout,
                "error": "task timeout",
            })
            logger.error(f"Task {task_id}: Stripe PaymentMethod task timed out after {timeout}s")
        except Exception as e:
            await save_result(task_id, "stripe_payment_method", {
                "value": "STRIPE_FAIL",
                "elapsed_time": 0,
                "error": str(e),
            })
            logger.error(f"Task {task_id}: Stripe PaymentMethod task failed: {str(e)}")

    async def _read_dodgeball_token_from_page(self, page) -> str:
        try:
            token = await page.evaluate("""
            () => {
              const readCookieToken = () => {
                const item = document.cookie.split('; ').find((part) => part.startsWith('_db-'));
                if (!item) return '';
                const value = item.split('=').slice(1).join('=');
                try {
                  return JSON.parse(decodeURIComponent(value)).token || '';
                } catch (_) {
                  return '';
                }
              };
              const readStorageToken = () => {
                try {
                  for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    if (!key || !key.startsWith('_db-')) continue;
                    const value = localStorage.getItem(key);
                    const parsed = JSON.parse(value || '{}');
                    if (parsed.token) return parsed.token;
                  }
                } catch (_) {}
                return '';
              };
              return readCookieToken() || readStorageToken();
            }
            """)
            return str(token or "")
        except Exception:
            return ""

    async def _ensure_dodgeball_token(self, page, public_key: str = DODGEBALL_PUBLIC_KEY, api_url: str = DODGEBALL_API_URL) -> str:
        token = await self._read_dodgeball_token_from_page(page)
        if token:
            return token

        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                has_dodgeball = await page.evaluate("() => typeof window.Dodgeball === 'function'")
            except Exception:
                has_dodgeball = False
            if has_dodgeball:
                break
            await page.wait_for_timeout(500)

        try:
            token = await page.evaluate(
                """async ({ publicKey, apiUrl }) => {
                  if (typeof window.Dodgeball !== 'function') return '';
                  const db = new window.Dodgeball(publicKey, { apiVersion: 'v1', apiUrl });
                  const token = await db.getSourceToken();
                  await new Promise((resolve) => setTimeout(resolve, 500));
                  return token || '';
                }""",
                {"publicKey": public_key, "apiUrl": api_url},
            )
            return str(token or await self._read_dodgeball_token_from_page(page) or "")
        except Exception:
            return await self._read_dodgeball_token_from_page(page)

    async def _solve_vapi_signup(
        self,
        task_id: str,
        email: str,
        password: str,
        sitekey: str,
        dashboard_version: str,
        proxy_url: Optional[str] = None,
        page_url: str = VAPI_SIGNUP_URL,
        api_url: str = VAPI_API_URL,
        session_id: str = "",
        verification_id: str = "",
    ):
        """Solve Vapi signup Turnstile and submit signup in the same browser context.

        Current dashboard-side Turnstile tokens can be rejected when moved from the
        solver browser into a separate page/session.  This task keeps token, PAT
        challenge state, cookies, user-agent and proxy in one context, then submits
        /auth/signup from that same page.
        """
        index, browser, browser_config = await self.browser_pool.get()
        context = None
        start_time = time.time()

        try:
            if hasattr(browser, "is_connected") and not browser.is_connected():
                async with self.pool_lock:
                    self.browser_slots.pop(index, None)
                    self.browser_use_counts.pop(index, None)
                    self.retire_browsers.discard(index)
                await save_result(task_id, "vapi_signup", {
                    "value": "VAPI_SIGNUP_FAIL",
                    "elapsed_time": 0,
                    "error": "browser disconnected",
                })
                return

            context = await self._new_browser_context(
                browser,
                browser_config,
                index,
                proxy_url=proxy_url,
                viewport={"width": 500, "height": 240},
            )
            page = await context.new_page()
            self._attach_page_debug_handlers(page, index)
            await self._antishadow_inject(page)
            await self._block_rendering(page)
            await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {}, loadTimes: function() {}, csi: function() {} };
            """)

            await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            await self._capture_page_debug(page, index, task_id, "vapi-signup-after-goto")
            await page.wait_for_timeout(5000)

            token = ""
            max_attempts = int(os.getenv("VAPI_SIGNUP_TURNSTILE_ATTEMPTS", "60"))
            for attempt in range(max_attempts):
                token = await self._get_turnstile_token_from_page(page, index)
                if token:
                    break

                if attempt == 0 or attempt % 4 == 0:
                    await self._try_execute_turnstile(page, sitekey, index)
                if attempt > 8 and attempt % 8 == 0:
                    await self._try_click_strategies(page, index)
                await asyncio.sleep(min(0.5 + attempt * 0.05, 2.0))

            if not token:
                elapsed_time = round(time.time() - start_time, 3)
                await self._capture_page_debug(page, index, task_id, "vapi-signup-no-token")
                await save_result(task_id, "vapi_signup", {
                    "value": "VAPI_SIGNUP_FAIL",
                    "elapsed_time": elapsed_time,
                    "error": "Turnstile token not produced in solver browser",
                    **self._browser_fingerprint_result(browser_config),
                })
                logger.error(f"Browser {index}: Vapi signup failed: no Turnstile token after {elapsed_time}s")
                return

            fingerprint_token = await self._ensure_dodgeball_token(page)
            result = await page.evaluate(
                """async ({ apiUrl, email, password, csrfToken, fingerprintToken, sessionId, verificationId, dashboardVersion, requestId }) => {
                  const headers = {
                    'accept': 'application/json',
                    'content-type': 'application/json',
                    'x-csrf-token': csrfToken || '',
                    'x-client-source': 'dashboard',
                    'x-client-platform': 'web',
                    'x-dashboard-version': dashboardVersion || '',
                    'x-request-id': requestId || `dash_${Date.now()}_${Math.random().toString(16).slice(2)}`,
                  };
                  if (fingerprintToken) headers['x-device-fingerprint-token'] = fingerprintToken;
                  if (sessionId) headers['x-session-id'] = sessionId;
                  if (verificationId) headers['x-verification-id'] = verificationId;
                  const response = await fetch(`${apiUrl.replace(/\\/$/, '')}/auth/signup`, {
                    method: 'POST',
                    mode: 'cors',
                    credentials: 'include',
                    headers,
                    body: JSON.stringify({
                      email,
                      password,
                      emailRedirectTo: 'https://dashboard.vapi.ai/',
                    }),
                  });
                  const text = await response.text();
                  return {
                    ok: response.ok,
                    status: response.status,
                    statusText: response.statusText,
                    body: text,
                    csrfLength: (csrfToken || '').length,
                    fingerprintLength: (fingerprintToken || '').length,
                  };
                }""",
                {
                    "apiUrl": api_url,
                    "email": email,
                    "password": password,
                    "csrfToken": token,
                    "fingerprintToken": fingerprint_token,
                    "sessionId": session_id,
                    "verificationId": verification_id,
                    "dashboardVersion": dashboard_version or VAPI_DASHBOARD_VERSION,
                    "requestId": f"dash_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}",
                },
            )

            elapsed_time = round(time.time() - start_time, 3)
            if isinstance(result, dict) and result.get("ok"):
                try:
                    storage_state = await context.storage_state() if context else None
                except Exception:
                    storage_state = None
                await save_result(task_id, "vapi_signup", {
                    "signup_ok": True,
                    "value": "VAPI_SIGNUP_OK",
                    "status_code": result.get("status"),
                    "elapsed_time": elapsed_time,
                    "device_fingerprint_token": fingerprint_token,
                    "browser_storage_state": storage_state,
                    "csrf_length": result.get("csrfLength", 0),
                    "fingerprint_length": result.get("fingerprintLength", 0),
                    **self._browser_fingerprint_result(browser_config),
                })
                logger.success(f"Browser {index}: Vapi signup submitted in same solver context in {elapsed_time}s")
                return

            body = result.get("body") if isinstance(result, dict) else str(result)
            status = result.get("status") if isinstance(result, dict) else "unknown"
            await self._capture_page_debug(page, index, task_id, "vapi-signup-submit-failed")
            await save_result(task_id, "vapi_signup", {
                "value": "VAPI_SIGNUP_FAIL",
                "status_code": status,
                "elapsed_time": elapsed_time,
                "error": f"signup {status}: {str(body)[:500]}",
                "device_fingerprint_token": fingerprint_token,
                "csrf_length": result.get("csrfLength", 0) if isinstance(result, dict) else 0,
                "fingerprint_length": result.get("fingerprintLength", 0) if isinstance(result, dict) else 0,
                **self._browser_fingerprint_result(browser_config),
            })
            logger.error(f"Browser {index}: Vapi signup failed in same solver context: status={status} body={str(body)[:200]}")
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            try:
                if "page" in locals():
                    await self._capture_page_debug(page, index, task_id, "vapi-signup-exception")
            except Exception:
                pass
            await save_result(task_id, "vapi_signup", {
                "value": "VAPI_SIGNUP_FAIL",
                "elapsed_time": elapsed_time,
                "error": str(e)[:500],
                **self._browser_fingerprint_result(browser_config),
            })
            logger.error(f"Browser {index}: Vapi signup task failed: {str(e)}")
        finally:
            await self._finish_browser_task(index, browser, browser_config, context, f"Browser {index}: Vapi signup")

    async def _solve_vapi_signup_guarded(
        self,
        task_id: str,
        email: str,
        password: str,
        sitekey: str,
        dashboard_version: str,
        proxy_url: Optional[str] = None,
        page_url: str = VAPI_SIGNUP_URL,
        api_url: str = VAPI_API_URL,
        session_id: str = "",
        verification_id: str = "",
    ):
        timeout = float(os.getenv("VAPI_SIGNUP_SOLVER_TASK_TIMEOUT", os.getenv("TURNSTILE_SOLVER_TASK_TIMEOUT", "90")))
        task = asyncio.create_task(self._solve_vapi_signup(
            task_id=task_id,
            email=email,
            password=password,
            sitekey=sitekey,
            dashboard_version=dashboard_version,
            proxy_url=proxy_url,
            page_url=page_url,
            api_url=api_url,
            session_id=session_id,
            verification_id=verification_id,
        ))
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
            await save_result(task_id, "vapi_signup", {
                "value": "VAPI_SIGNUP_FAIL",
                "elapsed_time": timeout,
                "error": "task timeout",
            })
            logger.error(f"Task {task_id}: Vapi signup task timed out after {timeout}s")
        except Exception as e:
            await save_result(task_id, "vapi_signup", {
                "value": "VAPI_SIGNUP_FAIL",
                "elapsed_time": 0,
                "error": str(e),
            })
            logger.error(f"Task {task_id}: Vapi signup task failed: {str(e)}")

    def _normalize_stripe_card(self, raw_card) -> tuple[dict, list[str]]:
        raw_card = raw_card if isinstance(raw_card, dict) else {}
        number = re.sub(r"\D", "", str(raw_card.get("number") or ""))
        exp_month = re.sub(r"\D", "", str(raw_card.get("exp_month") or raw_card.get("expMonth") or ""))
        exp_year = re.sub(r"\D", "", str(raw_card.get("exp_year") or raw_card.get("expYear") or ""))
        cvc = re.sub(r"\D", "", str(raw_card.get("cvc") or ""))

        if len(exp_year) == 2:
            current_year = time.gmtime().tm_year
            century = current_year - (current_year % 100)
            exp_year = str(century + int(exp_year))

        if exp_month:
            exp_month = exp_month.zfill(2)

        problems = []
        if not re.fullmatch(r"\d{12,19}", number):
            problems.append("card.number must be 12-19 digits")
        if not exp_month.isdigit() or not (1 <= int(exp_month) <= 12):
            problems.append("card.exp_month must be 01-12")
        if not re.fullmatch(r"\d{4}", exp_year):
            problems.append("card.exp_year must be 4 digits")
        if not re.fullmatch(r"\d{3,4}", cvc):
            problems.append("card.cvc must be 3-4 digits")

        return {
            "number": number,
            "exp_month": exp_month,
            "exp_year": exp_year,
            "cvc": cvc,
        }, problems

    async def process_stripe_payment_method(self):
        """Create a Stripe PaymentMethod using the existing solver browser pool."""
        try:
            body = await request.get_json(silent=True) or {}
        except Exception:
            body = {}

        card, problems = self._normalize_stripe_card(body.get("card"))
        email = str(body.get("email") or "").strip()
        publishable_key = str(body.get("publishableKey") or body.get("publishable_key") or STRIPE_DEFAULT_PK).strip()
        proxy_url = str(body.get("proxy") or request.args.get("proxy") or "").strip() or None

        if not email:
            problems.append("email is required")
        if not publishable_key:
            problems.append("publishableKey is required")
        if problems:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_BAD_STRIPE_REQUEST",
                "errorDescription": "; ".join(problems),
            }), 200

        task_id = str(uuid.uuid4())
        await save_result(task_id, "stripe_payment_method", {
            "status": "CAPTCHA_NOT_READY",
            "createTime": int(time.time()),
            "type": "stripe_payment_method",
            "email": email,
        })

        try:
            asyncio.create_task(self._solve_stripe_payment_method_guarded(
                task_id=task_id,
                card=card,
                email=email,
                publishable_key=publishable_key,
                proxy_url=proxy_url,
            ))
            if self.debug:
                logger.debug(f"Stripe PaymentMethod request queued with taskid {task_id}.")
            return jsonify({
                "errorId": 0,
                "taskId": task_id,
            }), 200
        except Exception as e:
            logger.error(f"Unexpected error processing Stripe request: {str(e)}")
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_UNKNOWN",
                "errorDescription": str(e),
            }), 200


    async def process_vapi_signup(self):
        """Submit Vapi signup from the same browser context that solves Turnstile."""
        try:
            body = await request.get_json(silent=True) or {}
        except Exception:
            body = {}

        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "")
        sitekey = str(body.get("sitekey") or body.get("siteKey") or os.getenv("VAPI_TURNSTILE_SITEKEY", "0x4AAAAAAAa7ZSD7onoZcTuC")).strip()
        dashboard_version = str(body.get("dashboardVersion") or body.get("dashboard_version") or VAPI_DASHBOARD_VERSION).strip()
        proxy_url = str(body.get("proxy") or request.args.get("proxy") or "").strip() or None
        page_url = str(body.get("url") or body.get("pageUrl") or VAPI_SIGNUP_URL).strip()
        api_url = str(body.get("apiUrl") or body.get("api_url") or VAPI_API_URL).strip()
        session_id = str(body.get("sessionId") or body.get("session_id") or "").strip()
        verification_id = str(body.get("verificationId") or body.get("verification_id") or "").strip()

        problems = []
        if not email:
            problems.append("email is required")
        if not password:
            problems.append("password is required")
        if not sitekey:
            problems.append("sitekey is required")
        if not page_url:
            problems.append("url is required")
        if not api_url:
            problems.append("apiUrl is required")
        if problems:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_BAD_VAPI_SIGNUP_REQUEST",
                "errorDescription": "; ".join(problems),
            }), 200

        task_id = str(uuid.uuid4())
        await save_result(task_id, "vapi_signup", {
            "status": "CAPTCHA_NOT_READY",
            "createTime": int(time.time()),
            "type": "vapi_signup",
            "email": email,
        })

        try:
            asyncio.create_task(self._solve_vapi_signup_guarded(
                task_id=task_id,
                email=email,
                password=password,
                sitekey=sitekey,
                dashboard_version=dashboard_version,
                proxy_url=proxy_url,
                page_url=page_url,
                api_url=api_url,
                session_id=session_id,
                verification_id=verification_id,
            ))
            if self.debug:
                logger.debug(f"Vapi signup request queued with taskid {task_id}.")
            return jsonify({
                "errorId": 0,
                "taskId": task_id,
            }), 200
        except Exception as e:
            logger.error(f"Unexpected error processing Vapi signup request: {str(e)}")
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_UNKNOWN",
                "errorDescription": str(e),
            }), 200




    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')
        proxy_url = request.args.get('proxy')

        if not url or not sitekey:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_PAGEURL",
                "errorDescription": "Both 'url' and 'sitekey' are required"
            }), 200

        task_id = str(uuid.uuid4())
        await save_result(task_id, "turnstile", {
            "status": "CAPTCHA_NOT_READY",
            "createTime": int(time.time()),
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "cdata": cdata
        })

        try:
            asyncio.create_task(self._solve_turnstile_guarded(task_id=task_id, url=url, sitekey=sitekey, action=action, cdata=cdata, proxy_url=proxy_url))

            if self.debug:
                logger.debug(f"Request completed with taskid {task_id}.")
            return jsonify({
                "errorId": 0,
                "taskId": task_id
            }), 200
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_UNKNOWN",
                "errorDescription": str(e)
            }), 200

    async def pool_status(self):
        async with self.pool_lock:
            return jsonify({
                "ok": True,
                "target": self.thread_count,
                "total": len(self.browser_slots),
                "idle": self.browser_pool.qsize(),
                "inUse": max(0, len(self.browser_slots) - self.browser_pool.qsize()),
                "retiring": len(self.retire_browsers),
                "generation": self.pool_generation,
                "maxTasksPerBrowser": self.max_tasks_per_browser,
                "browserUses": {str(index): count for index, count in self.browser_use_counts.items()},
                "browserPids": {str(index): sorted(pids) for index, pids in self.browser_root_pids.items()},
                "browserProfiles": {str(index): sorted(paths) for index, paths in self.browser_profile_dirs.items()},
                "closeTimeout": self.close_timeout,
            }), 200

    async def resize_pool_route(self):
        raw_threads = request.args.get('threads') or request.args.get('thread') or ""
        if not raw_threads and request.method == "POST":
            try:
                body = await request.get_json(silent=True) or {}
                raw_threads = str(body.get("threads") or body.get("thread") or "")
            except Exception:
                raw_threads = ""

        try:
            target = int(raw_threads)
        except Exception:
            return jsonify({
                "ok": False,
                "error": "threads must be a positive integer",
            }), 400

        status = await self._resize_browser_pool(target)
        return jsonify({"ok": True, **status}), 200

    async def recycle_pool_route(self):
        raw_threads = request.args.get('threads') or request.args.get('thread') or ""
        reason = request.args.get('reason') or ""
        if request.method == "POST":
            try:
                body = await request.get_json(silent=True) or {}
                raw_threads = raw_threads or str(body.get("threads") or body.get("thread") or "")
                reason = reason or str(body.get("reason") or "")
            except Exception:
                pass

        target = None
        if raw_threads:
            try:
                target = int(raw_threads)
            except Exception:
                return jsonify({
                    "ok": False,
                    "error": "threads must be a positive integer",
                }), 400

        status = await self._recycle_browser_pool(target=target, reason=reason)
        return jsonify({"ok": True, **status}), 200

    async def get_result(self):
        """Return solved data"""
        task_id = request.args.get('id')

        if not task_id:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_CAPTCHA_ID",
                "errorDescription": "Invalid task ID/Request parameter"
            }), 200

        result = await load_result(task_id)
        if not result:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Task not found"
            }), 200

        if result == "CAPTCHA_NOT_READY" or (isinstance(result, dict) and result.get("status") == "CAPTCHA_NOT_READY"):
            return jsonify({"status": "processing"}), 200

        if isinstance(result, dict) and result.get("payment_method_id"):
            return jsonify({
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "paymentMethodId": result["payment_method_id"],
                    "id": result["payment_method_id"],
                    "userAgent": result.get("user_agent", ""),
                    "browserName": result.get("browser_name", ""),
                    "browserVersion": result.get("browser_version", ""),
                    "elapsedTime": result.get("elapsed_time", 0),
                }
            }), 200

        if isinstance(result, dict) and result.get("signup_ok"):
            return jsonify({
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "kind": "vapi_signup",
                    "statusCode": result.get("status_code", 0),
                    "elapsedTime": result.get("elapsed_time", 0),
                    "deviceFingerprintToken": result.get("device_fingerprint_token", ""),
                    "browserStorageState": result.get("browser_storage_state") or None,
                    "csrfLength": result.get("csrf_length", 0),
                    "fingerprintLength": result.get("fingerprint_length", 0),
                    "userAgent": result.get("user_agent", ""),
                    "secChUa": result.get("sec_ch_ua", ""),
                    "browserName": result.get("browser_name", ""),
                    "browserVersion": result.get("browser_version", ""),
                }
            }), 200

        if isinstance(result, dict) and result.get("value") == "VAPI_SIGNUP_FAIL":
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_VAPI_SIGNUP",
                "errorDescription": result.get("error") or "Vapi signup task failed",
                "statusCode": result.get("status_code", 0),
            }), 200

        if isinstance(result, dict) and result.get("value") == "STRIPE_FAIL":
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_STRIPE_PAYMENT_METHOD",
                "errorDescription": result.get("error") or "Stripe PaymentMethod task failed"
            }), 200

        if isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL":
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Workers could not solve the Captcha"
            }), 200

        if isinstance(result, dict) and result.get("value") and result.get("value") != "CAPTCHA_FAIL":
            return jsonify({
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "token": result["value"],
                    "userAgent": result.get("user_agent", ""),
                    "secChUa": result.get("sec_ch_ua", ""),
                    "browserName": result.get("browser_name", ""),
                    "browserVersion": result.get("browser_version", ""),
                }
            }), 200
        else:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Workers could not solve the Captcha"
            }), 200

    

    @staticmethod
    async def index():
        """Serve the API documentation page."""
        return """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Turnstile Solver API</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-gray-900 text-gray-200 min-h-screen flex items-center justify-center">
                <div class="bg-gray-800 p-8 rounded-lg shadow-md max-w-2xl w-full border border-red-500">
                    <h1 class="text-3xl font-bold mb-6 text-center text-red-500">Welcome to Turnstile Solver API</h1>

                    <p class="mb-4 text-gray-300">To use the turnstile service, send a GET request to 
                       <code class="bg-red-700 text-white px-2 py-1 rounded">/turnstile</code> with the following query parameters:</p>

                    <ul class="list-disc pl-6 mb-6 text-gray-300">
                        <li><strong>url</strong>: The URL where Turnstile is to be validated</li>
                        <li><strong>sitekey</strong>: The site key for Turnstile</li>
                    </ul>

                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Example usage:</p>
                        <code class="text-sm break-all text-red-300">/turnstile?url=https://example.com&sitekey=sitekey</code>
                    </div>


                    <div class="bg-gray-700 p-4 rounded-lg mb-6">
                        <p class="text-gray-200 font-semibold mb-3">📢 Connect with Us</p>
                        <div class="space-y-2 text-sm">
                            <p class="text-gray-300">
                                📢 <strong>Channel:</strong> 
                                <a href="https://t.me/D3_vin" class="text-red-300 hover:underline">https://t.me/D3_vin</a> 
                                - Latest updates and releases
                            </p>
                            <p class="text-gray-300">
                                💬 <strong>Chat:</strong> 
                                <a href="https://t.me/D3vin_chat" class="text-red-300 hover:underline">https://t.me/D3vin_chat</a> 
                                - Community support and discussions
                            </p>
                            <p class="text-gray-300">
                                📁 <strong>GitHub:</strong> 
                                <a href="https://github.com/D3-vin" class="text-red-300 hover:underline">https://github.com/D3-vin</a> 
                                - Source code and development
                            </p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
        """


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")

    parser.add_argument('--no-headless', action='store_true', help='Run the browser with GUI (disable headless mode). By default, headless mode is enabled.')
    parser.add_argument('--useragent', type=str, help='User-Agent string (if not specified, random configuration is used)')
    parser.add_argument('--debug', action='store_true', help='Enable or disable debug mode for additional logging and troubleshooting information (default: False)')
    parser.add_argument('--browser_type', type=str, default='chromium', help='Specify the browser type for the solver. Supported options: chromium, chrome, msedge, cloak, camoufox (default: chromium)')
    parser.add_argument('--thread', type=int, default=4, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', action='store_true', help='Enable proxy support for the solver (Default: False)')
    parser.add_argument('--random', action='store_true', help='Use random User-Agent and Sec-CH-UA configuration from pool')
    parser.add_argument('--browser', type=str, help='Specify browser name to use (e.g., chrome, firefox)')
    parser.add_argument('--version', type=str, help='Specify browser version to use (e.g., 139, 141)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5000', help='Set the port for the API solver to listen on. (Default: 5072)')
    return parser.parse_args()


def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool, browser_name: str, browser_version: str) -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support, use_random_config=use_random_config, browser_name=browser_name, browser_version=browser_version)
    return server.app


if __name__ == '__main__':
    args = parse_args()
    browser_types = [
        'chromium',
        'chrome',
        'msedge',
        'cloak',
        'cloakbrowser',
        'camoufox',
    ]
    if args.browser_type not in browser_types:
        logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    else:
        app = create_app(
            headless=not args.no_headless, 
            debug=args.debug, 
            useragent=args.useragent, 
            browser_type=args.browser_type, 
            thread=args.thread, 
            proxy_support=args.proxy,
            use_random_config=args.random,
            browser_name=args.browser,
            browser_version=args.version
        )
        app.run(host=args.host, port=int(args.port))
