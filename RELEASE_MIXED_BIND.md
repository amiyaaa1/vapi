# Mixed billing bind release

默认注册链路保留 browser-fetch，绑卡入口已切到完整浏览器 fallback：

```bash
SIGNUP_MODE=browser-fetch
BILLING_BIND_MODE=browser
STRIPE_PAYMENT_METHOD_MODE=browser
BILLING_BROWSER_ENGINE=playwright
BILLING_BROWSER_HEADLESS=0
BILLING_ENABLE_WEBGL=1
BILLING_BIND_PROXY=socks5://warp:1080
BILLING_BROWSER_TOTAL_TIMEOUT=300
BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS=1
BILLING_BROWSER_RECOVER_ON_CARD_DECLINED=0
```

流程：

1. signup 在真实 dashboard 页面上下文里发起，复用 Dodgeball source token / storage。
2. 绑卡默认打开 dashboard billing 页面，复用当前注册/验证得到的 storage 与 org/user token。
3. Stripe Element 与 Vapi `/stripe/add-card` 都在完整浏览器页面上下文完成；若改回 protocol，仍保留 400 后 fallback。
4. 绑卡阶段默认 5 分钟总超时，避免浏览器卡死后长期占用补号槽。
5. Vapi/Stripe 明确返回 `Your card was declined` 时默认只记录一次失败，不再重复创建 PM、重启 WARP 或 recycle solver；连续 card_declined 达到阈值会自动关闭补号，避免继续刷卡/刷号。

自动补号默认命令已改为 xvfb headful browser fallback bind，可直接 `python3 -m registrator.main ...`。

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

card_declined 止损参数：

```bash
BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS=1
BILLING_BROWSER_RECOVER_ON_CARD_DECLINED=0
AUTO_TOPUP_CARD_DECLINED_FAILS_TO_DISABLE=1
```

手动单次测试：

```bash
docker compose exec vapi-gateway sh -lc \
  'cd /app && xvfb-run -a python3 -m registrator.main --count 1 --concurrency 1 --proxy "$SOCKS5_PROXY"'
```

如需临时回退混合协议绑卡：

```bash
BILLING_BIND_MODE=protocol
```

如需强制 headless 排障：

```bash
BILLING_BROWSER_HEADLESS=1
python3 -m registrator.main --count 1 --concurrency 1 --proxy "$SOCKS5_PROXY"
```


指纹/Stripe PM 强化参数：
```bash
BILLING_RANDOMIZE_FINGERPRINT=1
BILLING_STRIPE_PM_BILLING_DETAILS=0
# 可选：如需要 AVS，可填
BILLING_STRIPE_PM_POSTAL_CODE=
BILLING_STRIPE_PM_COUNTRY=
```
- billing/signup 浏览器上下文默认按账号生成一组稳定随机 viewport/screen/WebGL/cores profile。
- navigator getter 补丁带 no-op setter，避免 Dodgeball 严格模式赋值探测报 getter-only。
- Stripe `/v1/payment_methods` 默认不改 body；需要 A/B 时可开启 `BILLING_STRIPE_PM_BILLING_DETAILS=1` 补 billing_details[email/name]，可选补 postal/country。

2026-06-18 billing add-card 400 继续修复：
- 新增 `BILLING_STRIPE_PM_USER_AGENT_VERSION` 实验开关；默认关闭，必要时可填近期成功样本 `ab68db42e2` 做 A/B，避免默认制造 Stripe.js/hcaptcha 版本不一致。
- Stripe PM route 现在会写 `stripe-pm-route-*` 诊断，确认 `billing_details[email/name]` 和 UA override 是否真正落到最终 POST body。

2026-06-18 card_declined 隔离：
- 连续样本证明同卡从最后成功后开始稳定 `card_declined`，且 ab68/dfdd、WARP/direct、是否补 billing_details 都不能恢复；新增注册前同卡 decline 隔离，默认一次明确 `card_declined` 后隔离 24h，避免自动补号继续刷同卡/刷号。
- 新增 `BILLING_CARD_DECLINE_QUARANTINE_THRESHOLD/SECONDS/DISABLED`，有备用卡池时会自动跳过隔离卡继续尝试下一张。

2026-06-18 网关层 preflight：
- Go 网关 `topup.Run` 也读取 `/data/billing-card-declines.json`，如果所有配置卡都处于 `card_declined` 隔离期，会在启动 `registrator` 子进程前直接返回 `billing cards quarantined`，并自动关闭补号，避免先生成邮箱/注册账号再失败。
- 管理端手动补号和 scheduler 都走同一个 preflight；`/admin/state` 会显示具体隔离到期时间和换卡提示。

2026-06-18 card_declined 隔离键修正：
- 隔离 key 默认改为按卡号 PAN（`BILLING_CARD_DECLINE_KEY_MODE=pan`），避免随机日期/CVV 后绕过同卡 `card_declined` 止损；旧版 full-card hash 状态仍兼容。
- 新增 `BILLING_CARD_DECLINE_TAIL_FALLBACK=1` 兼容旧状态：如果只有旧 hash 且同卡日期/CVV被改，会用 last4 命中旧隔离记录。
- `/admin/state` 现在输出 `billingCards`，管理端补号状态里能看到每张配置卡是否处于隔离、隔离到期时间和 decline 次数。


## Billing card_declined 止损与诊断

- 默认 `BILLING_STOP_ON_CARD_DECLINED=1`：当 Vapi add-card 返回明确 `Stripe Error: Your card was declined` 时，注册器会把它分类为 `card_declined` 并终止当前账号绑卡，不再切换 WARP/指纹继续重试。
- 默认 `BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS=1`：避免同一类支付拒绝在同一 org 内重复尝试。
- 诊断脚本：`python3 /app/scripts/diagnose_billing_add_card.py --limit 120 --show 20 --compare`，会脱敏汇总 Stripe PM、add-card 状态、dashboard version、payment_user_agent 和 hcaptcha/human-security 字段。
- `BILLING_TEST_MODE=1` 只用于本地/测试链路，会 mock Stripe PM 与 add-card；默认关闭，不影响 live 行为。


### Preflight before enabling topup

Run this before turning `autoTopupEnabled` back on:

```bash
python3 /app/scripts/billing_safety_preflight.py --limit 120
```

Expected safe state: `warnings=[]`, `BILLING_STOP_ON_CARD_DECLINED=1`, `BILLING_CARD_DECLINE_QUARANTINE_DISABLED=0`, and active quarantine records for recently declined card tails.

If old logs contain explicit `card_declined` for configured cards, seed quarantine records first:

```bash
python3 /app/scripts/seed_billing_declines_from_logs.py --limit 3000 --seconds 86400
```

- `AUTO_TOPUP_CARD_DECLINED_FAILS_TO_DISABLE=1`: topup 层一旦看到 card_declined 失败即关闭自动补号。

2026-06-18 preflight CLI 固化：
- `python3 -m registrator.main --billing-preflight-only` 是只读入口：只检查可用卡、隔离卡和 attach-risk，不创建邮箱、不注册、不触发 Stripe/Vapi。
- `billing_safety_preflight.py` 默认只输出 JSON 并返回 0，便于巡检；需要 CI/脚本强校验时加 `--strict`，warnings 非空返回 2。
- 分发版默认仍关闭 `BILLING_TEST_MODE`；本地链路验证可临时用 `BILLING_TEST_MODE=1 BILLING_MOCK_ADD_CARD_STATUS=201`，不要把测试卡或生成卡放进 live 绑卡池。

2026-06-18 add-card 成功/失败差异回拨：
- 全量 `browser-bind-debug` 对比：最后一批成功样本使用 `x-dashboard-version=daily-2026-06-09-1400` 且 Stripe `payment_user_agent=stripe.js/ab68db42e2...`；之后失败样本集中切到 `daily-2026-06-17-11-28` + `7c9a63d3d1`，Stripe PM 仍 200，但 Vapi add-card 返回 `card_declined`。
- 默认把 add-card header 回拨为 `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE=daily-2026-06-09-1400`，Stripe PM route 默认保持 `BILLING_STRIPE_PM_USER_AGENT_VERSION=ab68db42e2`；仍可设置 `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE=live` 跟随当前 dashboard，或 `0/off` 禁用改写。

2026-06-18 known-good 诊断固化：
- `diagnose_billing_add_card.py --compare` 现在额外输出 `known_good_combo/dashboard/pua` 计数和 `known_good_alignment`，用于确认新样本是否命中 `daily-2026-06-09-1400 + ab68db42e2`。
- `billing_safety_preflight.py` 输出 `resolvedChecks` 和 `knownGood`，会在 dashboard/Stripe PUA 未命中已知成功组合时给 warning；默认仍只报告，`--strict` 才用 warnings 决定非 0 退出。
- `python3 -m registrator.main --billing-preflight-only` 增加 `billingRuntime`，显示注册器实际解析到的 add-card dashboard override 和 Stripe PM payment_user_agent。

2026-06-18 新增风控时序诊断：
- 新增 `analyze_billing_fraud_timeline.py`，用于离线对比 Dodgeball sourceToken、Stripe hcaptcha/human_security、r.stripe telemetry 和 Vapi add-card 的时序。
- 最近样本显示，主动 before-add `getSourceToken()` 不是成功必要条件，且会额外制造 sourceToken 请求；默认改为 `BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD=0`，保留 `=1` 做 A/B 回滚。
- 新 network log 会写入 `antiFraudTimeline`，便于后续新样本直接判断新增风控触发点。

### 内联 synthetic card generator

新增可选生成器：`BILLING_CARD_GENERATOR_ENABLED=1`，默认只在 `BILLING_TEST_MODE`/mock 链路生效；live A/B 需显式 `BILLING_CARD_GENERATOR_ALLOW_LIVE=1`。`BILLING_CARD_GENERATOR_PREFIXES=auto` 会优先使用已配置卡头，也支持 `common` 或 `415464,545046`；生成结果只在日志/预检中显示尾号。
