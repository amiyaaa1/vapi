# Mixed billing bind release

默认注册/绑卡链路已经切到已验证的混合模式：

```bash
SIGNUP_MODE=browser-fetch
BILLING_BIND_MODE=protocol
STRIPE_PAYMENT_METHOD_MODE=browser
BILLING_BROWSER_ENGINE=playwright
BILLING_BROWSER_HEADLESS=1
BILLING_ENABLE_WEBGL=1
BILLING_BIND_PROXY=socks5://warp:1080
```

流程：

1. signup 在真实 dashboard 页面上下文里发起，复用 Dodgeball source token / storage。
2. Stripe PaymentMethod 使用浏览器中的 Stripe Element 创建。
3. Vapi `/stripe/add-card` 使用协议请求 + org token + 当前 Dodgeball fingerprint token attach。

自动补号默认命令已改为 headless mixed bind，可直接 `python3 -m registrator.main ...`。

WARP 已内置到 `docker-compose.yml`：

- 默认启动 `warp` service，网关通过 `socks5://warp:1080` 出口。
- 网关挂载 `/var/run/docker.sock`，Turnstile solver 超时/失败时会自动重启 WARP 容器并重试。
- 关键参数：

```bash
TURNSTILE_SOLVER_ATTEMPTS=2
WARP_CONTAINER_NAME=vapi-gateway-warp
WARP_RESTART_ON_TURNSTILE_TIMEOUT=1
WARP_RESTART_COOLDOWN_SECONDS=75
WARP_RESTART_WAIT_SECONDS=12
```

另外验证邮件跳回后，`/org/dashboard` 偶发会先返回 `[]`；已默认等待重试：

```bash
ORG_DASHBOARD_ATTEMPTS=8
ORG_DASHBOARD_RETRY_SECONDS=2
```

手动单次测试：

```bash
docker compose exec vapi-gateway sh -lc \
  'cd /app && python3 -m registrator.main --count 1 --concurrency 1 --proxy "$SOCKS5_PROXY"'
```

如需临时回退全浏览器绑卡：

```bash
BILLING_BIND_MODE=browser
```

如需 headful 排障：

```bash
BILLING_BROWSER_HEADLESS=0
xvfb-run -a python3 -m registrator.main --count 1 --concurrency 1 --proxy "$SOCKS5_PROXY"
```
