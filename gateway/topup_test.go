package main

import (
	"fmt"
	"os"
	"strings"
	"testing"
	"time"
)

func TestParseTopupResult(t *testing.T) {
	stdout := `11:31:49 INFO 完成: 成功 3, 失败 1
11:31:49 INFO 密钥已保存到: /app/accounts/keys.jsonl 和 /data/keys.txt`

	success, fail := parseTopupResult(stdout, "")
	if success != 3 || fail != 1 {
		t.Fatalf("parseTopupResult = %d, %d; want 3, 1", success, fail)
	}
}

func TestParseTopupResultIgnoresSuccessfulAddCardNoise(t *testing.T) {
	stderr := `11:31:46 INFO Stripe browser PaymentMethod 创建成功: pm_1TgN...THxRGV
11:31:46 INFO 调用 add-card: 使用 org token
11:31:49 INFO [user@example.com] 绑卡成功: pm_1TgNskCRkod4mKy30OTHxRGV`

	success, fail := parseTopupResult("", stderr)
	if success != 0 || fail != 0 {
		t.Fatalf("parseTopupResult = %d, %d; want 0, 0 for logs without final summary", success, fail)
	}
}

func TestMetricsFromTopupHistory(t *testing.T) {
	metrics := metricsFromTopupHistory([]TopupRunSummary{
		{Success: 1, Fail: 0, DurationMs: 10_000, LastError: "last error"},
		{Success: 1, Fail: 1, DurationMs: 20_000},
	})

	if metrics.Runs != 2 || metrics.Attempts != 3 || metrics.Success != 2 || metrics.Fail != 1 {
		t.Fatalf("metrics counts = %+v, want runs=2 attempts=3 success=2 fail=1", metrics)
	}
	if metrics.SuccessRate < 0.666 || metrics.SuccessRate > 0.667 {
		t.Fatalf("success rate = %f, want about 0.6667", metrics.SuccessRate)
	}
	if metrics.AvgDurationMs != 15_000 {
		t.Fatalf("avg duration = %d, want 15000", metrics.AvgDurationMs)
	}
	if metrics.LastError != "last error" {
		t.Fatalf("last error = %q, want last error", metrics.LastError)
	}
}

func TestExtractTopupLastError(t *testing.T) {
	stderr := `12:48:22 INFO HTTP Request: POST http://127.0.0.1:5000/stripe/payment-method "HTTP/1.1 200 "
12:48:24 ERROR [user@example.com] ❌ 注册失败: Stripe solver payment_methods failed: Stripe solver failed: Page.evaluate: ReferenceError: Stripe is not defined
    at eval (eval at evaluate (:302:30), <anonymous>:2:36)
    at UtilityScript.evaluate (<anonymous>:309:18)
12:48:24 INFO 完成: 成功 0, 失败 1`

	got := extractTopupLastError("", stderr, "")
	if !strings.Contains(got, "ReferenceError: Stripe is not defined") {
		t.Fatalf("last error missing Stripe message: %q", got)
	}
	if strings.Contains(got, "完成: 成功") {
		t.Fatalf("last error should stop before final INFO line: %q", got)
	}
}

func TestTopupCardDeclineError(t *testing.T) {
	if !topupCardDeclineError(`browser add-card 400: {"message":"Couldn't Attach Payment Method. Stripe Error: Your card was declined."}`) {
		t.Fatalf("expected card declined error to match")
	}
	if topupCardDeclineError("browser add-card 400: rate limited") {
		t.Fatalf("non-card decline should not match")
	}
}

func TestTopupBillingCardsAvailableQuarantine(t *testing.T) {
	card := normalizeTopupBillingCard("4154644401338166", "06/32", "662", "", "")
	key := topupBillingCardKey(card)
	if key != "4930d8d04522e85888886106eef12da662c318186d0733f2b0ec8e3e3ff60fea" {
		t.Fatalf("card key mismatch: %s", key)
	}
	legacyKey := topupBillingCardLegacyKey(card)
	if legacyKey != "504e6fd9fcb6488092e2bb92bfce8de5418a3ca5312a8748ce70acdb72057dd6" {
		t.Fatalf("legacy card key mismatch: %s", legacyKey)
	}

	statePath := t.TempDir() + "/billing-card-declines.json"
	future := time.Now().Add(time.Hour).Unix()
	state := `{"cards":{"` + key + `":{"tail":"8166","declineCount":1,"quarantinedUntil":` + strconvFormatInt(future) + `,"quarantinedUntilIso":"2099-01-01T00:00:00Z"}}}`
	if err := os.WriteFile(statePath, []byte(state), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("BILLING_CARD_DECLINE_STATE", statePath)
	t.Setenv("BILLING_CARD_DECLINE_QUARANTINE_DISABLED", "0")
	t.Setenv("BILLING_BIND_MODE", "browser")

	ok, reason := topupBillingCardsAvailable(GatewayConfig{
		BillingCardNumber: "4154644401338166",
		BillingCardExpiry: "06/32",
		BillingCardCvc:    "662",
	})
	if ok || !strings.Contains(reason, "card_declined") {
		t.Fatalf("topupBillingCardsAvailable ok=%v reason=%q, want quarantine block", ok, reason)
	}
}

func strconvFormatInt(v int64) string {
	return fmt.Sprintf("%d", v)
}

func TestTopupBillingCardsAvailableProtocolUsesPool(t *testing.T) {
	t.Setenv("BILLING_BIND_MODE", "protocol")
	cards := topupBillingCardsForMode(GatewayConfig{
		BillingCardNumber: "4154644401338166",
		BillingCardExpiry: "06/32",
		BillingCardCvc:    "662",
		BillingCardPool:   "5450469400688553|07/30|321\n4154644401331054|07/30|321",
	}, topupBillingBindMode())
	if len(cards) != 3 {
		t.Fatalf("protocol mode cards len=%d, want primary+pool=3", len(cards))
	}
}

func TestNormalizeGatewayConfigInjectsStopOnCardDeclined(t *testing.T) {
	cfg := normalizeGatewayConfig(GatewayConfig{
		MinAccounts:          1,
		TopupConcurrency:     1,
		TopupCheckIntervalMs: 1000,
		AutoTopupCommand:     `SIGNUP_MODE="${SIGNUP_MODE:-browser-fetch}" xvfb-run -a python3 -m registrator.main --count "${TOPUP_COUNT:-1}"`,
	})
	if !strings.Contains(cfg.AutoTopupCommand, `BILLING_STOP_ON_CARD_DECLINED="${BILLING_STOP_ON_CARD_DECLINED:-0}"`) {
		t.Fatalf("normalized command missing stop-on-card-declined: %s", cfg.AutoTopupCommand)
	}
	if !strings.Contains(cfg.AutoTopupCommand, `BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS="${BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS:-1}"`) {
		t.Fatalf("normalized command missing one-attempt card_declined default: %s", cfg.AutoTopupCommand)
	}
	if !strings.Contains(cfg.AutoTopupCommand, `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-daily-2026-06-09-1400}"`) {
		t.Fatalf("normalized command missing known-good add-card dashboard version: %s", cfg.AutoTopupCommand)
	}
	if !strings.Contains(cfg.AutoTopupCommand, `BILLING_STRIPE_PM_USER_AGENT_VERSION="${BILLING_STRIPE_PM_USER_AGENT_VERSION:-ab68db42e2}"`) {
		t.Fatalf("normalized command missing known-good Stripe PM user agent version: %s", cfg.AutoTopupCommand)
	}
	if !strings.Contains(cfg.AutoTopupCommand, `BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-0}"`) {
		t.Fatalf("normalized command missing disabled before-add Dodgeball refresh default: %s", cfg.AutoTopupCommand)
	}
}

func TestDefaultTopupCommandKeepsAutoTopupOnCardDeclined(t *testing.T) {
	cmd := defaultTopupCommand()
	if !strings.Contains(cmd, `BILLING_STOP_ON_CARD_DECLINED="${BILLING_STOP_ON_CARD_DECLINED:-0}"`) {
		t.Fatalf("default command missing stop-on-card-declined: %s", cmd)
	}
	if !strings.Contains(cmd, `BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS="${BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS:-1}"`) {
		t.Fatalf("default command should default browser card_declined attempts to 1: %s", cmd)
	}
	if !strings.Contains(cmd, `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-daily-2026-06-09-1400}"`) {
		t.Fatalf("default command missing known-good add-card dashboard version: %s", cmd)
	}
	if !strings.Contains(cmd, `BILLING_STRIPE_PM_USER_AGENT_VERSION="${BILLING_STRIPE_PM_USER_AGENT_VERSION:-ab68db42e2}"`) {
		t.Fatalf("default command missing known-good Stripe PM user agent version: %s", cmd)
	}
	if !strings.Contains(cmd, `BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-0}"`) {
		t.Fatalf("default command should disable before-add Dodgeball refresh by default: %s", cmd)
	}
	if !strings.Contains(cmd, `BILLING_BIND_MODE="${BILLING_BIND_MODE:-protocol}"`) || !strings.Contains(cmd, `BILLING_ATTACH_MODE="${BILLING_ATTACH_MODE:-same-browser}"`) {
		t.Fatalf("default command should use protocol+same-browser attach: %s", cmd)
	}
	if !strings.Contains(cmd, `BILLING_RETRY_KEEP_DODGEBALL="${BILLING_RETRY_KEEP_DODGEBALL:-0}"`) {
		t.Fatalf("default command should clear Dodgeball on retry: %s", cmd)
	}
	if !strings.Contains(cmd, `BILLING_STRIPE_PM_STRIP_HCAPTCHA="${BILLING_STRIPE_PM_STRIP_HCAPTCHA:-1}"`) || !strings.Contains(cmd, `BILLING_STRIPE_PM_STRIP_WALLET_CONFIG="${BILLING_STRIPE_PM_STRIP_WALLET_CONFIG:-1}"`) {
		t.Fatalf("default command should strip Stripe PM risk fields: %s", cmd)
	}
}

func TestTopupCardDeclineDisableDefaultThresholdIsZero(t *testing.T) {
	t.Setenv("AUTO_TOPUP_CARD_DECLINED_FAILS_TO_DISABLE", "")
	if got := envInt("AUTO_TOPUP_CARD_DECLINED_FAILS_TO_DISABLE", 0); got != 0 {
		t.Fatalf("default AUTO_TOPUP_CARD_DECLINED_FAILS_TO_DISABLE = %d, want 0", got)
	}
}

func TestNormalizeGatewayConfigRewritesLiveDashboardVersion(t *testing.T) {
	cfg := normalizeGatewayConfig(GatewayConfig{
		MinAccounts:          1,
		TopupConcurrency:     1,
		TopupCheckIntervalMs: 1000,
		AutoTopupCommand:     `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-live}" xvfb-run -a python3 -m registrator.main --count "${TOPUP_COUNT:-1}"`,
	})
	if strings.Contains(cfg.AutoTopupCommand, `:-live}`) {
		t.Fatalf("normalized command still uses live dashboard version: %s", cfg.AutoTopupCommand)
	}
	if !strings.Contains(cfg.AutoTopupCommand, `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-daily-2026-06-09-1400}"`) {
		t.Fatalf("normalized command missing rewritten known-good dashboard version: %s", cfg.AutoTopupCommand)
	}
}

func TestNormalizeGatewayConfigRewritesBeforeAddDodgeballRefresh(t *testing.T) {
	cfg := normalizeGatewayConfig(GatewayConfig{
		MinAccounts:          1,
		TopupConcurrency:     1,
		TopupCheckIntervalMs: 1000,
		AutoTopupCommand:     `BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-1}" xvfb-run -a python3 -m registrator.main --count "${TOPUP_COUNT:-1}"`,
	})
	if strings.Contains(cfg.AutoTopupCommand, `BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-1}"`) {
		t.Fatalf("normalized command still actively refreshes Dodgeball before add-card: %s", cfg.AutoTopupCommand)
	}
	if !strings.Contains(cfg.AutoTopupCommand, `BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-0}"`) {
		t.Fatalf("normalized command missing disabled before-add Dodgeball refresh: %s", cfg.AutoTopupCommand)
	}
}

func TestTopupBillingCardAllowedPrefixesFiltersPool(t *testing.T) {
	cfg := GatewayConfig{
		BillingCardNumber:          "4154644401338166",
		BillingCardExpiry:          "06/32",
		BillingCardCvc:             "662",
		BillingCardPool:            "5450469400688553|07/30|321\n4154644401331054|07/30|321",
		BillingCardAllowedPrefixes: "415464",
	}
	cards := topupBillingCardsFromConfig(cfg)
	if len(cards) != 2 {
		t.Fatalf("filtered cards len=%d, want 2: %+v", len(cards), cards)
	}
	for _, card := range cards {
		if !strings.HasPrefix(card.Number, "415464") {
			t.Fatalf("unexpected card after prefix filter: %+v", card)
		}
	}
}

func TestTopupBillingCardGeneratorSwitchIsEnough(t *testing.T) {
	cfg := GatewayConfig{
		BillingCardGeneratorEnabled:         true,
		BillingCardGeneratorCount:           3,
		BillingCardGeneratorPrefixes:        "415464",
		BillingCardGeneratorUseConfigPrefix: true,
	}
	t.Setenv("BILLING_TEST_MODE", "0")
	if !topupBillingCardGeneratorActive(cfg) {
		t.Fatalf("generator should be active when switch is enabled")
	}
	cards := topupGeneratedBillingCards(cfg, nil)
	if len(cards) != 3 {
		t.Fatalf("generated cards len=%d, want 3", len(cards))
	}
	for _, card := range cards {
		if !card.Generated || !strings.HasPrefix(card.Number, "415464") || !luhnValid(card.Number) {
			t.Fatalf("bad generated card metadata: %+v", card)
		}
	}
}

func TestTopupBillingCardGeneratorDefaultPrefix(t *testing.T) {
	cards := topupGeneratedBillingCards(GatewayConfig{BillingCardGeneratorEnabled: true, BillingCardGeneratorCount: 2}, nil)
	if len(cards) != 2 {
		t.Fatalf("generated cards len=%d, want 2", len(cards))
	}
	for _, card := range cards {
		if !strings.HasPrefix(card.Number, "415464") || !luhnValid(card.Number) {
			t.Fatalf("bad generated card: %+v", card)
		}
	}
}
