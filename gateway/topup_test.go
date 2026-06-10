package main

import (
	"strings"
	"testing"
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
