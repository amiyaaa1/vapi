"""
Turnstile Solver 客户端
支持同步调用本地 Solver API 服务
"""

import time
import requests
from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass
class SolveResult:
    """验证结果"""
    success: bool
    token: Optional[str] = None
    elapsed_time: float = 0.0
    error: Optional[str] = None


class TurnstileSolverClient:
    """
    Turnstile Solver 客户端

    使用示例:
        client = TurnstileSolverClient("http://127.0.0.1:5072")
        result = client.solve("https://example.com", "0x4AAA...")
        if result.success:
            print(f"Token: {result.token}")
    """

    def __init__(
        self,
        solver_url: str = "http://127.0.0.1:5072",
        timeout: int = 60,
        poll_interval: float = 2.0,
        max_retries: int = 30
    ):
        """
        初始化客户端

        Args:
            solver_url: Solver 服务地址
            timeout: 请求超时时间(秒)
            poll_interval: 轮询间隔(秒)
            max_retries: 最大重试次数
        """
        self.solver_url = solver_url.rstrip('/')
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self._session = requests.Session()

    def health_check(self) -> bool:
        """检查服务是否可用"""
        try:
            resp = self._session.get(f"{self.solver_url}/", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def create_task(
        self,
        url: str,
        sitekey: str,
        action: Optional[str] = None,
        cdata: Optional[str] = None,
        invisible: bool = False
    ) -> Optional[str]:
        """
        创建验证任务

        Args:
            url: 目标网站 URL
            sitekey: Turnstile sitekey
            action: 可选的 action 参数
            cdata: 可选的 cdata 参数
            invisible: 是否为隐形验证

        Returns:
            任务 ID，失败返回 None
        """
        params = {"url": url, "sitekey": sitekey}
        if action:
            params["action"] = action
        if cdata:
            params["cdata"] = cdata
        if invisible:
            params["invisible"] = "true"

        try:
            resp = self._session.get(
                f"{self.solver_url}/turnstile",
                params=params,
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get('taskId') or data.get('task_id')
        except Exception as e:
            print(f"[TurnstileSolver] 创建任务失败: {e}")
            return None

    def get_result(self, task_id: str) -> Dict[str, Any]:
        """
        获取任务结果

        Args:
            task_id: 任务 ID

        Returns:
            结果字典
        """
        try:
            resp = self._session.get(
                f"{self.solver_url}/result",
                params={"id": task_id},
                timeout=self.timeout
            )
            return resp.json()
        except Exception as e:
            return {"error": str(e)}

    def solve(
        self,
        url: str,
        sitekey: str,
        action: Optional[str] = None,
        cdata: Optional[str] = None,
        invisible: bool = False,
        initial_delay: float = 3.0
    ) -> SolveResult:
        """
        一站式解决 Turnstile 验证

        Args:
            url: 目标网站 URL
            sitekey: Turnstile sitekey
            action: 可选的 action 参数
            cdata: 可选的 cdata 参数
            invisible: 是否为隐形验证
            initial_delay: 首次查询前等待时间

        Returns:
            SolveResult 对象
        """
        start_time = time.time()

        task_id = self.create_task(url, sitekey, action, cdata, invisible)
        if not task_id:
            return SolveResult(
                success=False,
                error="创建任务失败",
                elapsed_time=time.time() - start_time
            )

        time.sleep(initial_delay)

        for _ in range(self.max_retries):
            result = self.get_result(task_id)

            if result.get('status') == 'ready':
                token = result.get('solution', {}).get('token')
                if token:
                    return SolveResult(
                        success=True,
                        token=token,
                        elapsed_time=time.time() - start_time
                    )

            if 'value' in result:
                value = result['value']
                if value and value != 'CAPTCHA_FAIL':
                    return SolveResult(
                        success=True,
                        token=value,
                        elapsed_time=result.get('elapsed_time', time.time() - start_time)
                    )
                elif value == 'CAPTCHA_FAIL':
                    return SolveResult(
                        success=False,
                        error="验证失败",
                        elapsed_time=result.get('elapsed_time', time.time() - start_time)
                    )

            if result.get('errorId') == 1:
                return SolveResult(
                    success=False,
                    error=result.get('errorDescription', '未知错误'),
                    elapsed_time=time.time() - start_time
                )

            time.sleep(self.poll_interval)

        return SolveResult(
            success=False,
            error="超时",
            elapsed_time=time.time() - start_time
        )

    def close(self):
        """关闭会话"""
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
