package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type GatewayConfig struct {
	AutoTopupEnabled                    bool   `json:"autoTopupEnabled"`
	MinAccounts                         int    `json:"minAccounts"`
	TopupLowWatermark                   int    `json:"topupLowWatermark"`
	TopupConcurrency                    int    `json:"topupConcurrency"`
	TopupCheckIntervalMs                int    `json:"topupCheckIntervalMs"`
	AutoTopupCommand                    string `json:"autoTopupCommand"`
	DefaultModel                        string `json:"defaultModel"`
	BillingCardNumber                   string `json:"billingCardNumber"`
	BillingCardExpiry                   string `json:"billingCardExpiry"`
	BillingCardCvc                      string `json:"billingCardCvc"`
	BillingCardPool                     string `json:"billingCardPool"`
	BillingCardAllowedPrefixes          string `json:"billingCardAllowedPrefixes"`
	BillingCardGeneratorEnabled         bool   `json:"billingCardGeneratorEnabled"`
	BillingCardGeneratorAllowLive       bool   `json:"billingCardGeneratorAllowLive"`
	BillingCardGeneratorOnly            bool   `json:"billingCardGeneratorOnly"`
	BillingCardGeneratorCount           int    `json:"billingCardGeneratorCount"`
	BillingCardGeneratorPrefixes        string `json:"billingCardGeneratorPrefixes"`
	BillingCardGeneratorUseConfigPrefix bool   `json:"billingCardGeneratorUseConfigPrefixes"`
	BillingCardGeneratorPrefixDigits    int    `json:"billingCardGeneratorPrefixDigits"`
	BillingCardGeneratorMinMonths       int    `json:"billingCardGeneratorMinMonths"`
	BillingCardGeneratorMaxMonths       int    `json:"billingCardGeneratorMaxMonths"`
	RequireChatReadyAfterSignup         bool   `json:"requireChatReadyAfterSignup"`
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
		AutoTopupEnabled:                    envBool("AUTO_TOPUP_WORKER_ENABLED", false),
		MinAccounts:                         minAccounts,
		TopupLowWatermark:                   lowWatermark,
		TopupConcurrency:                    maxInt(1, envInt("AUTO_TOPUP_WORKER_CONCURRENCY", 1)),
		TopupCheckIntervalMs:                maxInt(1000, envInt("AUTO_TOPUP_WORKER_TICK_MS", 30000)),
		AutoTopupCommand:                    command,
		DefaultModel:                        envOr("DEFAULT_MODEL", "claude-opus-4-6"),
		BillingCardNumber:                   os.Getenv("BILLING_CARD_NUMBER"),
		BillingCardExpiry:                   os.Getenv("BILLING_CARD_EXPIRY"),
		BillingCardCvc:                      os.Getenv("BILLING_CARD_CVC"),
		BillingCardPool:                     os.Getenv("BILLING_CARD_POOL"),
		BillingCardAllowedPrefixes:          envOr("BILLING_CARD_ALLOWED_PREFIXES", "415464"),
		BillingCardGeneratorEnabled:         envBool("BILLING_CARD_GENERATOR_ENABLED", false),
		BillingCardGeneratorAllowLive:       envBool("BILLING_CARD_GENERATOR_ALLOW_LIVE", false),
		BillingCardGeneratorOnly:            envBool("BILLING_CARD_GENERATOR_ONLY", false),
		BillingCardGeneratorCount:           envInt("BILLING_CARD_GENERATOR_COUNT", 20),
		BillingCardGeneratorPrefixes:        envOr("BILLING_CARD_GENERATOR_PREFIXES", "415464"),
		BillingCardGeneratorUseConfigPrefix: envBool("BILLING_CARD_GENERATOR_USE_CONFIG_PREFIXES", true),
		BillingCardGeneratorPrefixDigits:    envInt("BILLING_CARD_GENERATOR_PREFIX_DIGITS", 6),
		BillingCardGeneratorMinMonths:       envInt("BILLING_CARD_GENERATOR_MIN_MONTHS", 18),
		BillingCardGeneratorMaxMonths:       envInt("BILLING_CARD_GENERATOR_MAX_MONTHS", 60),
		RequireChatReadyAfterSignup:         envBool("REQUIRE_CHAT_READY_AFTER_SIGNUP", false),
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
	if cfg.BillingCardAllowedPrefixes == "" {
		cfg.BillingCardAllowedPrefixes = envOr("BILLING_CARD_ALLOWED_PREFIXES", "415464")
	}
	if cfg.BillingCardGeneratorEnabled {
		if strings.TrimSpace(cfg.BillingCardGeneratorPrefixes) == "" {
			cfg.BillingCardGeneratorPrefixes = "415464"
		}
		cfg.BillingCardAllowedPrefixes = cfg.BillingCardGeneratorPrefixes
		cfg.BillingCardGeneratorAllowLive = true
		cfg.BillingCardGeneratorOnly = true
		cfg.BillingCardGeneratorUseConfigPrefix = false
	}
	if cfg.BillingCardGeneratorCount < 0 {
		cfg.BillingCardGeneratorCount = 0
	}
	if cfg.BillingCardGeneratorCount == 0 && cfg.BillingCardGeneratorEnabled {
		cfg.BillingCardGeneratorCount = 5
	}
	if cfg.BillingCardGeneratorCount > 50 {
		cfg.BillingCardGeneratorCount = 50
	}
	if cfg.BillingCardGeneratorPrefixDigits < 1 {
		cfg.BillingCardGeneratorPrefixDigits = 6
	}
	if cfg.BillingCardGeneratorPrefixDigits > 12 {
		cfg.BillingCardGeneratorPrefixDigits = 12
	}
	if cfg.BillingCardGeneratorMinMonths < 1 {
		cfg.BillingCardGeneratorMinMonths = 18
	}
	if cfg.BillingCardGeneratorMinMonths > 120 {
		cfg.BillingCardGeneratorMinMonths = 120
	}
	if cfg.BillingCardGeneratorMaxMonths < cfg.BillingCardGeneratorMinMonths {
		cfg.BillingCardGeneratorMaxMonths = cfg.BillingCardGeneratorMinMonths
	}
	if cfg.BillingCardGeneratorMaxMonths > 120 {
		cfg.BillingCardGeneratorMaxMonths = 120
	}
	if cfg.AutoTopupCommand == "" || strings.Contains(cfg.AutoTopupCommand, "scripts/register-and-seed.js") {
		cfg.AutoTopupCommand = defaultTopupCommand()
	} else {
		if strings.Contains(cfg.AutoTopupCommand, `SIGNUP_MODE="${SIGNUP_MODE:-browser-fetch}"`) {
			cfg.AutoTopupCommand = strings.ReplaceAll(cfg.AutoTopupCommand, `SIGNUP_MODE="${SIGNUP_MODE:-browser-fetch}"`, `SIGNUP_MODE="${SIGNUP_MODE:-solver-browser}"`)
		}
		if strings.Contains(cfg.AutoTopupCommand, `BILLING_BROWSER_ENGINE="${BILLING_BROWSER_ENGINE:-playwright}"`) {
			cfg.AutoTopupCommand = strings.ReplaceAll(cfg.AutoTopupCommand, `BILLING_BROWSER_ENGINE="${BILLING_BROWSER_ENGINE:-playwright}"`, `BILLING_BROWSER_ENGINE="${BILLING_BROWSER_ENGINE:-cloak}"`)
		}
		if strings.Contains(cfg.AutoTopupCommand, `BILLING_BIND_MODE="${BILLING_BIND_MODE:-browser}"`) {
			cfg.AutoTopupCommand = strings.ReplaceAll(cfg.AutoTopupCommand, `BILLING_BIND_MODE="${BILLING_BIND_MODE:-browser}"`, `BILLING_BIND_MODE="${BILLING_BIND_MODE:-protocol}"`)
		}
		if strings.Contains(cfg.AutoTopupCommand, "python3 -m registrator.main") && !strings.Contains(cfg.AutoTopupCommand, "SIGNUP_SOLVER_BROWSER_FALLBACK") {
			cfg.AutoTopupCommand = strings.ReplaceAll(
				cfg.AutoTopupCommand,
				"xvfb-run -a python3 -m registrator.main",
				`SIGNUP_SOLVER_BROWSER_FALLBACK="${SIGNUP_SOLVER_BROWSER_FALLBACK:-0}" xvfb-run -a python3 -m registrator.main`,
			)
		}
		if strings.Contains(cfg.AutoTopupCommand, `BILLING_BIND_PROXY_SEQUENCE="${BILLING_BIND_PROXY_SEQUENCE:-socks5://warp:1080,direct,socks5://warp:1080}"`) {
			cfg.AutoTopupCommand = strings.ReplaceAll(cfg.AutoTopupCommand, `BILLING_BIND_PROXY_SEQUENCE="${BILLING_BIND_PROXY_SEQUENCE:-socks5://warp:1080,direct,socks5://warp:1080}"`, `BILLING_BIND_PROXY_SEQUENCE="${BILLING_BIND_PROXY_SEQUENCE:-socks5://warp:1080}"`)
		}
		if strings.Contains(cfg.AutoTopupCommand, "python3 -m registrator.main") && !strings.Contains(cfg.AutoTopupCommand, "BILLING_BIND_PROXY_SEQUENCE") {
			cfg.AutoTopupCommand = strings.ReplaceAll(
				cfg.AutoTopupCommand,
				"xvfb-run -a python3 -m registrator.main",
				`BILLING_BIND_PROXY_SEQUENCE="${BILLING_BIND_PROXY_SEQUENCE:-socks5://warp:1080}" xvfb-run -a python3 -m registrator.main`,
			)
		}
		if strings.Contains(cfg.AutoTopupCommand, "python3 -m registrator.main") && !strings.Contains(cfg.AutoTopupCommand, "BILLING_CLOAK_FORCE_WARP") {
			cfg.AutoTopupCommand = strings.ReplaceAll(
				cfg.AutoTopupCommand,
				"xvfb-run -a python3 -m registrator.main",
				`BILLING_CLOAK_FORCE_WARP="${BILLING_CLOAK_FORCE_WARP:-1}" xvfb-run -a python3 -m registrator.main`,
			)
		}
		if strings.Contains(cfg.AutoTopupCommand, "python3 -m registrator.main") && !strings.Contains(cfg.AutoTopupCommand, "xvfb-run") {
			cfg.AutoTopupCommand = strings.ReplaceAll(cfg.AutoTopupCommand, `BILLING_BROWSER_HEADLESS="${BILLING_BROWSER_HEADLESS:-1}"`, `BILLING_BROWSER_HEADLESS="${BILLING_BROWSER_HEADLESS:-0}"`)
			cfg.AutoTopupCommand = strings.ReplaceAll(cfg.AutoTopupCommand, "python3 -m registrator.main", "xvfb-run -a python3 -m registrator.main")
		}
		if strings.Contains(cfg.AutoTopupCommand, `BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-1}"`) {
			cfg.AutoTopupCommand = strings.ReplaceAll(cfg.AutoTopupCommand, `BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-1}"`, `BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-0}"`)
		}
		if strings.Contains(cfg.AutoTopupCommand, "python3 -m registrator.main") && !strings.Contains(cfg.AutoTopupCommand, "BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD") {
			cfg.AutoTopupCommand = strings.ReplaceAll(
				cfg.AutoTopupCommand,
				"xvfb-run -a python3 -m registrator.main",
				`BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-0}" xvfb-run -a python3 -m registrator.main`,
			)
		}
		if strings.Contains(cfg.AutoTopupCommand, `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-live}"`) {
			cfg.AutoTopupCommand = strings.ReplaceAll(cfg.AutoTopupCommand, `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-live}"`, `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-daily-2026-06-09-1400}"`)
		}
		if strings.Contains(cfg.AutoTopupCommand, `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-current}"`) {
			cfg.AutoTopupCommand = strings.ReplaceAll(cfg.AutoTopupCommand, `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-current}"`, `BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-daily-2026-06-09-1400}"`)
		}
		if strings.Contains(cfg.AutoTopupCommand, "python3 -m registrator.main") && !strings.Contains(cfg.AutoTopupCommand, "BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE") {
			cfg.AutoTopupCommand = strings.ReplaceAll(
				cfg.AutoTopupCommand,
				"xvfb-run -a python3 -m registrator.main",
				`BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-daily-2026-06-09-1400}" xvfb-run -a python3 -m registrator.main`,
			)
		}
		if strings.Contains(cfg.AutoTopupCommand, "python3 -m registrator.main") && !strings.Contains(cfg.AutoTopupCommand, "BILLING_STRIPE_PM_USER_AGENT_VERSION") {
			cfg.AutoTopupCommand = strings.ReplaceAll(
				cfg.AutoTopupCommand,
				"xvfb-run -a python3 -m registrator.main",
				`BILLING_STRIPE_PM_USER_AGENT_VERSION="${BILLING_STRIPE_PM_USER_AGENT_VERSION:-ab68db42e2}" xvfb-run -a python3 -m registrator.main`,
			)
		}
		if strings.Contains(cfg.AutoTopupCommand, "python3 -m registrator.main") && !strings.Contains(cfg.AutoTopupCommand, "BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS") {
			cfg.AutoTopupCommand = strings.ReplaceAll(
				cfg.AutoTopupCommand,
				"xvfb-run -a python3 -m registrator.main",
				`BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS="${BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS:-1}" xvfb-run -a python3 -m registrator.main`,
			)
		}
		if strings.Contains(cfg.AutoTopupCommand, "python3 -m registrator.main") && !strings.Contains(cfg.AutoTopupCommand, "BILLING_STOP_ON_CARD_DECLINED") {
			cfg.AutoTopupCommand = strings.ReplaceAll(
				cfg.AutoTopupCommand,
				"xvfb-run -a python3 -m registrator.main",
				`BILLING_STOP_ON_CARD_DECLINED="${BILLING_STOP_ON_CARD_DECLINED:-1}" xvfb-run -a python3 -m registrator.main`,
			)
		}
		cfg.AutoTopupCommand = normalizeTopupCommandEnv(cfg.AutoTopupCommand)
	}
	return cfg
}

func normalizeTopupCommandEnv(command string) string {
	if !strings.Contains(command, "python3 -m registrator.main") {
		return command
	}
	replacements := map[string]string{
		`BILLING_BIND_MODE="${BILLING_BIND_MODE:-browser}"`:                         `BILLING_BIND_MODE="${BILLING_BIND_MODE:-protocol}"`,
		`BILLING_RETRY_KEEP_DODGEBALL="${BILLING_RETRY_KEEP_DODGEBALL:-1}"`:         `BILLING_RETRY_KEEP_DODGEBALL="${BILLING_RETRY_KEEP_DODGEBALL:-0}"`,
		`BILLING_BROWSER_USER_AGENT="${BILLING_BROWSER_USER_AGENT:-148.0.7778.72}"`: `BILLING_BROWSER_USER_AGENT="${BILLING_BROWSER_USER_AGENT:-}"`,
		`BILLING_BROWSER_USER_AGENT="${BILLING_BROWSER_USER_AGENT:-148.0.7778.96}"`: `BILLING_BROWSER_USER_AGENT="${BILLING_BROWSER_USER_AGENT:-}"`,
		`BILLING_CARD_DECLINE_QUARANTINE_DISABLED=0`:                                `BILLING_CARD_DECLINE_QUARANTINE_DISABLED="${BILLING_CARD_DECLINE_QUARANTINE_DISABLED:-1}"`,
		`BILLING_STOP_ON_CARD_DECLINED="${BILLING_STOP_ON_CARD_DECLINED:-1}"`:       `BILLING_STOP_ON_CARD_DECLINED="${BILLING_STOP_ON_CARD_DECLINED:-0}"`,
	}
	for oldValue, newValue := range replacements {
		command = strings.ReplaceAll(command, oldValue, newValue)
	}
	assignments := []struct {
		name  string
		value string
	}{
		{"BILLING_ATTACH_MODE", `BILLING_ATTACH_MODE="${BILLING_ATTACH_MODE:-same-browser}"`},
		{"BILLING_STRIPE_ELEMENT_MODE", `BILLING_STRIPE_ELEMENT_MODE="${BILLING_STRIPE_ELEMENT_MODE:-card}"`},
		{"BILLING_SAME_BROWSER_USE_BILLING_PAGE", `BILLING_SAME_BROWSER_USE_BILLING_PAGE="${BILLING_SAME_BROWSER_USE_BILLING_PAGE:-1}"`},
		{"BILLING_STRIPE_PM_SYNC_CARD_ELEMENT_SUBTYPE", `BILLING_STRIPE_PM_SYNC_CARD_ELEMENT_SUBTYPE="${BILLING_STRIPE_PM_SYNC_CARD_ELEMENT_SUBTYPE:-1}"`},
		{"BILLING_SAME_BROWSER_FALLBACK_ON_ADD_CARD_400", `BILLING_SAME_BROWSER_FALLBACK_ON_ADD_CARD_400="${BILLING_SAME_BROWSER_FALLBACK_ON_ADD_CARD_400:-0}"`},
		{"BILLING_RETRY_KEEP_DODGEBALL", `BILLING_RETRY_KEEP_DODGEBALL="${BILLING_RETRY_KEEP_DODGEBALL:-0}"`},
		{"BILLING_STRIPE_PM_STRIP_HUMAN_SECURITY", `BILLING_STRIPE_PM_STRIP_HUMAN_SECURITY="${BILLING_STRIPE_PM_STRIP_HUMAN_SECURITY:-1}"`},
		{"BILLING_STRIPE_PM_STRIP_HCAPTCHA", `BILLING_STRIPE_PM_STRIP_HCAPTCHA="${BILLING_STRIPE_PM_STRIP_HCAPTCHA:-1}"`},
		{"BILLING_STRIPE_PM_STRIP_WALLET_CONFIG", `BILLING_STRIPE_PM_STRIP_WALLET_CONFIG="${BILLING_STRIPE_PM_STRIP_WALLET_CONFIG:-1}"`},
		{"BILLING_CARD_DECLINE_QUARANTINE_DISABLED", `BILLING_CARD_DECLINE_QUARANTINE_DISABLED="${BILLING_CARD_DECLINE_QUARANTINE_DISABLED:-1}"`},
		{"BILLING_BROWSER_USER_AGENT", `BILLING_BROWSER_USER_AGENT="${BILLING_BROWSER_USER_AGENT:-}"`},
		{"BILLING_ONE_CARD_PER_ORG_ON_DECLINE", `BILLING_ONE_CARD_PER_ORG_ON_DECLINE="${BILLING_ONE_CARD_PER_ORG_ON_DECLINE:-1}"`},
		{"BILLING_CARD_POOL_MAX_CARDS", `BILLING_CARD_POOL_MAX_CARDS="${BILLING_CARD_POOL_MAX_CARDS:-1}"`},
		{"BILLING_CARD_ALLOWED_PREFIXES", `BILLING_CARD_ALLOWED_PREFIXES="${BILLING_CARD_ALLOWED_PREFIXES:-415464}"`},
		{"BILLING_CARD_GENERATOR_PREFIXES", `BILLING_CARD_GENERATOR_PREFIXES="${BILLING_CARD_GENERATOR_PREFIXES:-415464}"`},
		{"BILLING_BEFORE_BILLING_MIN_MS", `BILLING_BEFORE_BILLING_MIN_MS=0`},
		{"BILLING_BEFORE_BILLING_MAX_MS", `BILLING_BEFORE_BILLING_MAX_MS=0`},
		{"BILLING_ATTACH_400_COOLDOWN_ENABLED", `BILLING_ATTACH_400_COOLDOWN_ENABLED=0`},
		{"BILLING_ATTACH_400_COOLDOWN_MAX_WAIT_SECONDS", `BILLING_ATTACH_400_COOLDOWN_MAX_WAIT_SECONDS=0`},
		{"BILLING_ATTACH_RATE_LIMIT_ENABLED", `BILLING_ATTACH_RATE_LIMIT_ENABLED=0`},
		{"BILLING_ATTACH_MIN_INTERVAL_SECONDS", `BILLING_ATTACH_MIN_INTERVAL_SECONDS=0`},
		{"BILLING_ATTACH_INTERVAL_JITTER_SECONDS", `BILLING_ATTACH_INTERVAL_JITTER_SECONDS=0`},
		{"BILLING_BEFORE_ADD_CARD_MIN_MS", `BILLING_BEFORE_ADD_CARD_MIN_MS=0`},
		{"BILLING_BEFORE_ADD_CARD_MAX_MS", `BILLING_BEFORE_ADD_CARD_MAX_MS=0`},
		{"BILLING_ATTACH_DECLINE_RETRY_SLEEP_SECONDS", `BILLING_ATTACH_DECLINE_RETRY_SLEEP_SECONDS=0`},
		{"BILLING_BROWSER_DECLINE_RETRY_SLEEP_SECONDS", `BILLING_BROWSER_DECLINE_RETRY_SLEEP_SECONDS=0`},
		{"BILLING_HUMANIZE_STRIPE_INPUTS", `BILLING_HUMANIZE_STRIPE_INPUTS=0`},
		{"BILLING_STRIPE_TYPE_DELAY_MS", `BILLING_STRIPE_TYPE_DELAY_MS=0`},
		{"BILLING_HUMAN_MOUSE_WIGGLE", `BILLING_HUMAN_MOUSE_WIGGLE=0`},
	}
	for _, assignment := range assignments {
		if !strings.Contains(command, assignment.name+"=") {
			command = injectTopupAssignment(command, assignment.value)
		}
	}
	return command
}

func injectTopupAssignment(command string, assignment string) string {
	needle := "xvfb-run -a python3 -m registrator.main"
	if strings.Contains(command, needle) {
		return strings.Replace(command, needle, assignment+" "+needle, 1)
	}
	needle = "python3 -m registrator.main"
	if strings.Contains(command, needle) {
		return strings.Replace(command, needle, assignment+" "+needle, 1)
	}
	return command
}

func defaultTopupCommand() string {
	return `SIGNUP_MODE="${SIGNUP_MODE:-solver-browser}" SIGNUP_SOLVER_BROWSER_FALLBACK="${SIGNUP_SOLVER_BROWSER_FALLBACK:-0}" BILLING_BIND_MODE="${BILLING_BIND_MODE:-protocol}" BILLING_ATTACH_MODE="${BILLING_ATTACH_MODE:-same-browser}" BILLING_BEFORE_BILLING_MIN_MS=0 BILLING_BEFORE_BILLING_MAX_MS=0 BILLING_ATTACH_400_COOLDOWN_ENABLED=0 BILLING_ATTACH_400_COOLDOWN_MAX_WAIT_SECONDS=0 BILLING_ATTACH_RATE_LIMIT_ENABLED=0 BILLING_ATTACH_MIN_INTERVAL_SECONDS=0 BILLING_ATTACH_INTERVAL_JITTER_SECONDS=0 BILLING_BEFORE_ADD_CARD_MIN_MS=0 BILLING_BEFORE_ADD_CARD_MAX_MS=0 BILLING_ATTACH_DECLINE_RETRY_SLEEP_SECONDS=0 BILLING_BROWSER_DECLINE_RETRY_SLEEP_SECONDS=0 BILLING_HUMANIZE_STRIPE_INPUTS=0 BILLING_STRIPE_TYPE_DELAY_MS=0 BILLING_HUMAN_MOUSE_WIGGLE=0 STRIPE_PAYMENT_METHOD_MODE="${STRIPE_PAYMENT_METHOD_MODE:-browser}" BILLING_STRIPE_ELEMENT_MODE="${BILLING_STRIPE_ELEMENT_MODE:-card}" BILLING_SAME_BROWSER_USE_BILLING_PAGE="${BILLING_SAME_BROWSER_USE_BILLING_PAGE:-1}" BILLING_STRIPE_PM_SYNC_CARD_ELEMENT_SUBTYPE="${BILLING_STRIPE_PM_SYNC_CARD_ELEMENT_SUBTYPE:-1}" BILLING_SAME_BROWSER_FALLBACK_ON_ADD_CARD_400="${BILLING_SAME_BROWSER_FALLBACK_ON_ADD_CARD_400:-0}" BILLING_BROWSER_ENGINE="${BILLING_BROWSER_ENGINE:-cloak}" BILLING_BROWSER_HEADLESS="${BILLING_BROWSER_HEADLESS:-0}" BILLING_BROWSER_USER_AGENT="${BILLING_BROWSER_USER_AGENT:-}" BILLING_BIND_PROXY_SEQUENCE="${BILLING_BIND_PROXY_SEQUENCE:-socks5://warp:1080}" BILLING_CLOAK_FORCE_WARP="${BILLING_CLOAK_FORCE_WARP:-1}" BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD="${BILLING_REFRESH_DODGEBALL_BEFORE_ADD_CARD:-0}" BILLING_REFRESH_DODGEBALL="${BILLING_REFRESH_DODGEBALL:-0}" BILLING_RETRY_KEEP_DODGEBALL="${BILLING_RETRY_KEEP_DODGEBALL:-0}" BILLING_STRIPE_PM_USER_AGENT_VERSION="${BILLING_STRIPE_PM_USER_AGENT_VERSION:-ab68db42e2}" BILLING_STRIPE_PM_STRIP_HUMAN_SECURITY="${BILLING_STRIPE_PM_STRIP_HUMAN_SECURITY:-1}" BILLING_STRIPE_PM_STRIP_HCAPTCHA="${BILLING_STRIPE_PM_STRIP_HCAPTCHA:-1}" BILLING_STRIPE_PM_STRIP_WALLET_CONFIG="${BILLING_STRIPE_PM_STRIP_WALLET_CONFIG:-1}" BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE="${BILLING_ADD_CARD_DASHBOARD_VERSION_OVERRIDE:-daily-2026-06-09-1400}" BILLING_CARD_DECLINE_QUARANTINE_DISABLED="${BILLING_CARD_DECLINE_QUARANTINE_DISABLED:-1}" BILLING_STOP_ON_CARD_DECLINED="${BILLING_STOP_ON_CARD_DECLINED:-0}" BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS="${BILLING_BROWSER_CARD_DECLINED_RETRY_ATTEMPTS:-1}" BILLING_ONE_CARD_PER_ORG_ON_DECLINE="${BILLING_ONE_CARD_PER_ORG_ON_DECLINE:-1}" BILLING_CARD_POOL_MAX_CARDS="${BILLING_CARD_POOL_MAX_CARDS:-1}" BILLING_CARD_ALLOWED_PREFIXES="${BILLING_CARD_ALLOWED_PREFIXES:-415464}" BILLING_CARD_GENERATOR_PREFIXES="${BILLING_CARD_GENERATOR_PREFIXES:-415464}" BILLING_CARD_GENERATOR_MIN_MONTHS="${BILLING_CARD_GENERATOR_MIN_MONTHS:-18}" BILLING_CARD_GENERATOR_MAX_MONTHS="${BILLING_CARD_GENERATOR_MAX_MONTHS:-60}" xvfb-run -a python3 -m registrator.main --count "${TOPUP_COUNT:-1}" --concurrency "${TOPUP_CONCURRENCY:-1}" --proxy "${SOCKS5_PROXY:-}"`
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
