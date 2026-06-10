package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type GatewayConfig struct {
	AutoTopupEnabled            bool   `json:"autoTopupEnabled"`
	MinAccounts                 int    `json:"minAccounts"`
	TopupLowWatermark           int    `json:"topupLowWatermark"`
	TopupConcurrency            int    `json:"topupConcurrency"`
	TopupCheckIntervalMs        int    `json:"topupCheckIntervalMs"`
	AutoTopupCommand            string `json:"autoTopupCommand"`
	DefaultModel                string `json:"defaultModel"`
	BillingCardNumber           string `json:"billingCardNumber"`
	BillingCardExpiry           string `json:"billingCardExpiry"`
	BillingCardCvc              string `json:"billingCardCvc"`
	RequireChatReadyAfterSignup bool   `json:"requireChatReadyAfterSignup"`
}

type ConfigStore struct {
	path string
}

func NewConfigStore(path string) *ConfigStore {
	if path == "" {
		path = envOr("CONFIG_PATH", "/data/config.json")
	}
	return &ConfigStore{path: path}
}

func defaultGatewayConfig() GatewayConfig {
	minAccounts := envInt("AUTO_TOPUP_TARGET_ACCOUNTS", 200)
	if minAccounts < 1 {
		minAccounts = 1
	}
	lowWatermark := envInt("AUTO_TOPUP_LOW_WATERMARK", minAccounts/2)
	if lowWatermark < 0 {
		lowWatermark = 0
	}
	if lowWatermark > minAccounts {
		lowWatermark = minAccounts
	}

	command := os.Getenv("AUTO_TOPUP_COMMAND")
	if command == "" || command == "node scripts/register-and-seed.js" {
		command = defaultTopupCommand()
	}

	return GatewayConfig{
		AutoTopupEnabled:            envBool("AUTO_TOPUP_WORKER_ENABLED", false),
		MinAccounts:                 minAccounts,
		TopupLowWatermark:           lowWatermark,
		TopupConcurrency:            maxInt(1, envInt("AUTO_TOPUP_WORKER_CONCURRENCY", 1)),
		TopupCheckIntervalMs:        maxInt(1000, envInt("AUTO_TOPUP_WORKER_TICK_MS", 30000)),
		AutoTopupCommand:            command,
		DefaultModel:                envOr("DEFAULT_MODEL", "claude-opus-4-6"),
		BillingCardNumber:           os.Getenv("BILLING_CARD_NUMBER"),
		BillingCardExpiry:           os.Getenv("BILLING_CARD_EXPIRY"),
		BillingCardCvc:              os.Getenv("BILLING_CARD_CVC"),
		RequireChatReadyAfterSignup: envBool("REQUIRE_CHAT_READY_AFTER_SIGNUP", false),
	}
}

func (s *ConfigStore) Load() GatewayConfig {
	cfg := defaultGatewayConfig()
	raw, err := os.ReadFile(s.path)
	if err == nil && len(raw) > 0 {
		_ = json.Unmarshal(raw, &cfg)
	}
	return normalizeGatewayConfig(cfg)
}

func (s *ConfigStore) Save(cfg GatewayConfig) (GatewayConfig, error) {
	cfg = normalizeGatewayConfig(cfg)
	if err := os.MkdirAll(filepath.Dir(s.path), 0755); err != nil {
		return cfg, err
	}
	raw, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return cfg, err
	}
	return cfg, os.WriteFile(s.path, append(raw, '\n'), 0600)
}

func normalizeGatewayConfig(cfg GatewayConfig) GatewayConfig {
	if cfg.MinAccounts < 1 {
		cfg.MinAccounts = 1
	}
	if cfg.TopupLowWatermark < 0 {
		cfg.TopupLowWatermark = 0
	}
	if cfg.TopupLowWatermark > cfg.MinAccounts {
		cfg.TopupLowWatermark = cfg.MinAccounts
	}
	cfg.TopupConcurrency = maxInt(1, cfg.TopupConcurrency)
	cfg.TopupCheckIntervalMs = maxInt(1000, cfg.TopupCheckIntervalMs)
	if cfg.DefaultModel == "" {
		cfg.DefaultModel = "claude-opus-4-6"
	}
	if cfg.AutoTopupCommand == "" || strings.Contains(cfg.AutoTopupCommand, "scripts/register-and-seed.js") {
		cfg.AutoTopupCommand = defaultTopupCommand()
	}
	return cfg
}

func defaultTopupCommand() string {
	return `SIGNUP_MODE="${SIGNUP_MODE:-browser-fetch}" BILLING_BIND_MODE="${BILLING_BIND_MODE:-protocol}" STRIPE_PAYMENT_METHOD_MODE="${STRIPE_PAYMENT_METHOD_MODE:-browser}" BILLING_BROWSER_ENGINE="${BILLING_BROWSER_ENGINE:-playwright}" BILLING_BROWSER_HEADLESS="${BILLING_BROWSER_HEADLESS:-1}" python3 -m registrator.main --count "${TOPUP_COUNT:-1}" --concurrency "${TOPUP_CONCURRENCY:-1}" --proxy "${SOCKS5_PROXY:-}"`
}

func envInt(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func envBool(key string, def bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	switch v {
	case "1", "true", "TRUE", "yes", "YES", "on", "ON":
		return true
	case "0", "false", "FALSE", "no", "NO", "off", "OFF":
		return false
	default:
		return def
	}
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
