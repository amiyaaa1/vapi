package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const topupHistoryLimit = 20

var topupResultRe = regexp.MustCompile(`完成:\s*成功\s*(\d+)\s*,\s*失败\s*(\d+)`)
var topupNonErrorLogLineRe = regexp.MustCompile(`^\d{2}:\d{2}:\d{2}\s+(INFO|WARNING|DEBUG)\b`)

type TopupStatus struct {
	Running    bool   `json:"running"`
	Refilling  bool   `json:"refilling,omitempty"`
	PID        int    `json:"pid,omitempty"`
	Reason     string `json:"reason,omitempty"`
	StartedAt  string `json:"startedAt,omitempty"`
	FinishedAt string `json:"finishedAt,omitempty"`
	Message    string `json:"message,omitempty"`
	Success    int    `json:"success,omitempty"`
	Fail       int    `json:"fail,omitempty"`
	DurationMs int64  `json:"durationMs,omitempty"`
	Code       int    `json:"code,omitempty"`
	Signal     string `json:"signal,omitempty"`
	LastError  string `json:"lastError,omitempty"`
	Stdout     string `json:"stdout,omitempty"`
	Stderr     string `json:"stderr,omitempty"`
}

type TopupRunSummary struct {
	Reason     string `json:"reason,omitempty"`
	StartedAt  string `json:"startedAt,omitempty"`
	FinishedAt string `json:"finishedAt,omitempty"`
	Message    string `json:"message,omitempty"`
	Success    int    `json:"success"`
	Fail       int    `json:"fail"`
	DurationMs int64  `json:"durationMs"`
	Code       int    `json:"code,omitempty"`
	Signal     string `json:"signal,omitempty"`
	LastError  string `json:"lastError,omitempty"`
}

type TopupMetrics struct {
	Window        int               `json:"window"`
	Runs          int               `json:"runs"`
	Attempts      int               `json:"attempts"`
	Success       int               `json:"success"`
	Fail          int               `json:"fail"`
	SuccessRate   float64           `json:"successRate"`
	AvgDurationMs int64             `json:"avgDurationMs"`
	LastError     string            `json:"lastError,omitempty"`
	History       []TopupRunSummary `json:"history"`
}

type TopupManager struct {
	mu          sync.Mutex
	pool        *KeyPool
	configStore *ConfigStore
	cancel      context.CancelFunc
	cmd         *exec.Cmd
	status      TopupStatus
	stdout      *tailBuffer
	stderr      *tailBuffer
	refilling   bool
	history     []TopupRunSummary
}

func NewTopupManager(pool *KeyPool, configStore *ConfigStore) *TopupManager {
	return &TopupManager{
		pool:        pool,
		configStore: configStore,
		status:      TopupStatus{Message: "idle"},
	}
}

func (m *TopupManager) Status() TopupStatus {
	m.mu.Lock()
	defer m.mu.Unlock()
	status := m.status
	if status.Running {
		status.DurationMs = durationSinceMs(status.StartedAt)
	}
	if m.stdout != nil {
		status.Stdout = m.stdout.String()
	}
	if m.stderr != nil {
		status.Stderr = m.stderr.String()
	}
	status.LastError = extractTopupLastError(status.Stdout, status.Stderr, status.LastError)
	if status.LastError == "" {
		for _, item := range m.history {
			if item.LastError != "" {
				status.LastError = item.LastError
				break
			}
		}
	}
	status.Refilling = m.refilling
	return status
}

func (m *TopupManager) Metrics() TopupMetrics {
	m.mu.Lock()
	defer m.mu.Unlock()
	return metricsFromTopupHistory(m.history)
}

func (m *TopupManager) Run(reason string) (TopupStatus, error) {
	m.mu.Lock()
	if m.cmd != nil && m.status.Running {
		status := m.status
		status.Refilling = m.refilling
		m.mu.Unlock()
		return status, errors.New("topup already running")
	}

	cfg := m.configStore.Load()
	if ok, blockReason := topupBillingCardsAvailable(cfg); !ok {
		now := nowIso()
		status := TopupStatus{
			Running:    false,
			Reason:     reason,
			StartedAt:  now,
			FinishedAt: now,
			Message:    "billing cards quarantined",
			Fail:       1,
			LastError:  blockReason,
		}
		m.status = status
		m.recordRunLocked(status)
		m.refilling = false
		disableAutoTopup := cfg.AutoTopupEnabled
		m.mu.Unlock()
		if disableAutoTopup {
			cfg.AutoTopupEnabled = false
			if _, err := m.configStore.Save(cfg); err != nil {
				log.Printf("[topup] billing 卡全部隔离，关闭自动补号失败: %v", err)
			} else {
				log.Printf("[topup] billing 卡全部隔离，已自动关闭补号，避免继续刷卡/刷号")
			}
		}
		return status, errors.New(blockReason)
	}
	command := cfg.AutoTopupCommand
	if command == "" {
		m.mu.Unlock()
		return TopupStatus{Message: "topup command is empty"}, errors.New("topup command is empty")
	}

	stats := m.pool.Stats()
	shortage := cfg.MinAccounts - stats.Active
	if shortage < 1 {
		shortage = 1
	}
	count := shortage
	if count > cfg.TopupConcurrency {
		count = cfg.TopupConcurrency
	}
	if count < 1 {
		count = 1
	}
	if err := syncSolverConcurrency(cfg.TopupConcurrency); err != nil {
		log.Printf("[topup] 同步 Turnstile solver 窗口数失败: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(ctx, "sh", "-c", command)
	cmd.Dir = "/app"
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Env = append(os.Environ(),
		"TOPUP_COUNT="+strconv.Itoa(count),
		"TOPUP_CONCURRENCY="+strconv.Itoa(cfg.TopupConcurrency),
		"AUTO_TOPUP_SHORTAGE="+strconv.Itoa(shortage),
		"AUTO_TOPUP_TARGET="+strconv.Itoa(cfg.MinAccounts),
		"AUTO_TOPUP_REASON="+reason,
	)
	stdout := &tailBuffer{limit: 16000}
	stderr := &tailBuffer{limit: 16000}
	cmd.Stdout = stdout
	cmd.Stderr = stderr

	startedAt := nowIso()
	if err := cmd.Start(); err != nil {
		m.mu.Unlock()
		return TopupStatus{Message: err.Error()}, err
	}

	m.cancel = cancel
	m.cmd = cmd
	m.stdout = stdout
	m.stderr = stderr
	m.status = TopupStatus{
		Running:   true,
		PID:       cmd.Process.Pid,
		Reason:    reason,
		StartedAt: startedAt,
		Message:   "running",
	}
	m.mu.Unlock()

	go m.wait(cmd, stdout, stderr, cancel)
	return m.Status(), nil
}

func (m *TopupManager) wait(cmd *exec.Cmd, stdout *tailBuffer, stderr *tailBuffer, cancel context.CancelFunc) {
	err := cmd.Wait()
	cancel()
	m.pool.Reload()

	status := TopupStatus{
		Running:    false,
		PID:        cmd.Process.Pid,
		FinishedAt: nowIso(),
		Stdout:     stdout.String(),
		Stderr:     stderr.String(),
	}
	if err != nil {
		status.Message = err.Error()
		if exitErr, ok := err.(*exec.ExitError); ok {
			status.Code = exitErr.ExitCode()
			if waitStatus, ok := exitErr.Sys().(syscall.WaitStatus); ok && waitStatus.Signaled() {
				status.Signal = waitStatus.Signal().String()
			}
		}
	} else {
		status.Message = "completed"
	}

	m.mu.Lock()
	status.Reason = m.status.Reason
	status.StartedAt = m.status.StartedAt
	status.Success, status.Fail = parseTopupResult(status.Stdout, status.Stderr)
	status.DurationMs = durationBetweenMs(status.StartedAt, status.FinishedAt)
	status.LastError = extractTopupLastError(status.Stdout, status.Stderr, status.Message)
	m.status = status
	m.cmd = nil
	m.cancel = nil
	m.recordRunLocked(status)
	m.mu.Unlock()
	m.disableAutoTopupAfterCardDeclineBurst()
}

func (m *TopupManager) recordRunLocked(status TopupStatus) {
	summary := TopupRunSummary{
		Reason:     status.Reason,
		StartedAt:  status.StartedAt,
		FinishedAt: status.FinishedAt,
		Message:    status.Message,
		Success:    status.Success,
		Fail:       status.Fail,
		DurationMs: status.DurationMs,
		Code:       status.Code,
		Signal:     status.Signal,
		LastError:  status.LastError,
	}
	m.history = append([]TopupRunSummary{summary}, m.history...)
	if len(m.history) > topupHistoryLimit {
		m.history = m.history[:topupHistoryLimit]
	}
}

func parseTopupResult(stdout string, stderr string) (int, int) {
	match := topupResultRe.FindStringSubmatch(stdout + "\n" + stderr)
	if len(match) != 3 {
		return 0, 0
	}
	success, _ := strconv.Atoi(match[1])
	fail, _ := strconv.Atoi(match[2])
	return success, fail
}

func extractTopupLastError(stdout string, stderr string, fallback string) string {
	text := strings.TrimSpace(strings.Join([]string{stdout, stderr}, "\n"))
	if text == "" {
		return normalizeTopupErrorFallback(fallback)
	}
	text = strings.ReplaceAll(text, "\r\n", "\n")
	lines := strings.Split(text, "\n")
	start := -1
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if strings.Contains(line, " ERROR ") ||
			strings.HasPrefix(line, "ERROR ") ||
			strings.Contains(line, "❌") ||
			strings.Contains(line, "注册失败") ||
			strings.Contains(line, "failed:") ||
			strings.Contains(line, "Traceback") {
			start = i
			break
		}
	}
	if start < 0 {
		return normalizeTopupErrorFallback(fallback)
	}

	end := len(lines)
	for i := start + 1; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if topupNonErrorLogLineRe.MatchString(line) {
			end = i
			break
		}
	}
	return tailString(strings.TrimSpace(strings.Join(lines[start:end], "\n")), 4000)
}

func normalizeTopupErrorFallback(value string) string {
	value = strings.TrimSpace(value)
	switch value {
	case "", "idle", "running", "completed":
		return ""
	default:
		return tailString(value, 4000)
	}
}

func tailString(value string, limit int) string {
	if limit <= 0 || len(value) <= limit {
		return value
	}
	return value[len(value)-limit:]
}

func metricsFromTopupHistory(history []TopupRunSummary) TopupMetrics {
	metrics := TopupMetrics{
		Window:  topupHistoryLimit,
		Runs:    len(history),
		History: append([]TopupRunSummary(nil), history...),
	}
	var totalDuration int64
	durationRuns := 0
	for _, item := range history {
		metrics.Success += item.Success
		metrics.Fail += item.Fail
		if item.DurationMs > 0 {
			totalDuration += item.DurationMs
			durationRuns++
		}
		if metrics.LastError == "" && item.LastError != "" {
			metrics.LastError = item.LastError
		}
	}
	metrics.Attempts = metrics.Success + metrics.Fail
	if metrics.Attempts > 0 {
		metrics.SuccessRate = float64(metrics.Success) / float64(metrics.Attempts)
	}
	if durationRuns > 0 {
		metrics.AvgDurationMs = totalDuration / int64(durationRuns)
	}
	return metrics
}

func (m *TopupManager) disableAutoTopupAfterCardDeclineBurst() {
	threshold := envInt("AUTO_TOPUP_CARD_DECLINED_FAILS_TO_DISABLE", 0)
	if threshold < 1 {
		return
	}

	m.mu.Lock()
	consecutive := 0
	for _, item := range m.history {
		if item.Fail > 0 && topupCardDeclineError(item.LastError) {
			consecutive++
			continue
		}
		break
	}
	if consecutive < threshold {
		m.mu.Unlock()
		return
	}
	m.refilling = false
	m.mu.Unlock()

	cfg := m.configStore.Load()
	if !cfg.AutoTopupEnabled {
		return
	}
	cfg.AutoTopupEnabled = false
	if _, err := m.configStore.Save(cfg); err != nil {
		log.Printf("[topup] card_declined 连续 %d 次，关闭自动补号失败: %v", consecutive, err)
		return
	}
	log.Printf("[topup] card_declined 连续 %d 次，已自动关闭补号，避免继续刷卡/刷号", consecutive)
}

func topupCardDeclineError(text string) bool {
	lowered := strings.ToLower(text)
	return strings.Contains(lowered, "your card was declined") ||
		strings.Contains(lowered, "card was declined") ||
		strings.Contains(lowered, "card_declined")
}

func durationBetweenMs(startedAt string, finishedAt string) int64 {
	started, err := time.Parse(time.RFC3339Nano, startedAt)
	if err != nil {
		return 0
	}
	finished, err := time.Parse(time.RFC3339Nano, finishedAt)
	if err != nil {
		return 0
	}
	if finished.Before(started) {
		return 0
	}
	return finished.Sub(started).Milliseconds()
}

func durationSinceMs(startedAt string) int64 {
	started, err := time.Parse(time.RFC3339Nano, startedAt)
	if err != nil {
		return 0
	}
	return time.Since(started).Milliseconds()
}

func (m *TopupManager) Stop() TopupStatus {
	m.mu.Lock()
	cmd := m.cmd
	cancel := m.cancel
	if cmd == nil || !m.status.Running {
		status := m.status
		m.mu.Unlock()
		return status
	}
	pid := cmd.Process.Pid
	m.status.Message = "stopping"
	m.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	_ = syscall.Kill(-pid, syscall.SIGTERM)
	time.AfterFunc(5*time.Second, func() {
		_ = syscall.Kill(-pid, syscall.SIGKILL)
	})
	return m.Status()
}

func (m *TopupManager) StartScheduler() {
	go func() {
		for {
			cfg := m.configStore.Load()
			sleepMs := cfg.TopupCheckIntervalMs
			if sleepMs < 1000 {
				sleepMs = 1000
			}
			time.Sleep(time.Duration(sleepMs) * time.Millisecond)
			cfg = m.configStore.Load()
			if !cfg.AutoTopupEnabled {
				m.setRefilling(false)
				continue
			}

			stats := m.pool.Stats()
			if stats.Active >= cfg.MinAccounts {
				m.setRefilling(false)
				continue
			}

			if !m.isRefilling() {
				if stats.Active > cfg.TopupLowWatermark {
					continue
				}
				m.setRefilling(true)
				log.Printf("[topup] 进入补号周期 active=%d low=%d target=%d", stats.Active, cfg.TopupLowWatermark, cfg.MinAccounts)
			}

			_, _ = m.Run("scheduler")
		}
	}()
}

func (m *TopupManager) isRefilling() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.refilling
}

func (m *TopupManager) setRefilling(refilling bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.refilling = refilling
}

type tailBuffer struct {
	mu    sync.Mutex
	limit int
	data  []byte
}

func (b *tailBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.data = append(b.data, p...)
	if b.limit > 0 && len(b.data) > b.limit {
		b.data = b.data[len(b.data)-b.limit:]
	}
	return len(p), nil
}

func (b *tailBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return string(b.data)
}

func nowIso() string {
	return time.Now().UTC().Format(time.RFC3339Nano)
}

func syncSolverConcurrency(concurrency int) error {
	if concurrency < 1 {
		concurrency = 1
	}
	baseURL := strings.TrimRight(os.Getenv("TURNSTILE_SOLVER_URL"), "/")
	if baseURL == "" {
		baseURL = "http://127.0.0.1:5000"
	}
	endpoint := baseURL + "/pool/resize?threads=" + strconv.Itoa(concurrency)

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, nil)
	if err != nil {
		return err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("solver resize returned %s", resp.Status)
	}
	return nil
}

type topupBillingCard struct {
	Number    string
	ExpMonth  string
	ExpYear   string
	Cvc       string
	Generated bool
}

type topupCardDeclineRecord struct {
	Tail                string  `json:"tail"`
	KeyMode             string  `json:"keyMode"`
	DeclineCount        int     `json:"declineCount"`
	LastReason          string  `json:"lastReason"`
	QuarantinedUntil    float64 `json:"quarantinedUntil"`
	QuarantinedUntilIso string  `json:"quarantinedUntilIso"`
}

type topupCardDeclineState struct {
	Cards map[string]topupCardDeclineRecord `json:"cards"`
}

type TopupBillingCardStatus struct {
	Index            int    `json:"index"`
	Tail             string `json:"tail"`
	KeyMode          string `json:"keyMode"`
	Generated        bool   `json:"generated,omitempty"`
	Quarantined      bool   `json:"quarantined"`
	QuarantinedUntil string `json:"quarantinedUntil,omitempty"`
	DeclineCount     int    `json:"declineCount,omitempty"`
	LastReason       string `json:"lastReason,omitempty"`
}

func topupBillingCardStatuses(cfg GatewayConfig) []TopupBillingCardStatus {
	quarantineDisabled := envBool("BILLING_CARD_DECLINE_QUARANTINE_DISABLED", false)
	mode := topupBillingBindMode()
	cards := topupBillingCardsForMode(cfg, mode)
	state := loadTopupCardDeclineState()
	now := float64(time.Now().Unix())
	statuses := make([]TopupBillingCardStatus, 0, len(cards))
	for idx, card := range cards {
		status := TopupBillingCardStatus{
			Index:     idx + 1,
			Tail:      topupBillingCardTail(card),
			KeyMode:   topupBillingCardKeyMode(),
			Generated: card.Generated,
		}
		if !quarantineDisabled {
			if rec, ok := topupCardDeclineRecordForCard(state, card); ok && rec.QuarantinedUntil > now {
				status.Quarantined = true
				status.QuarantinedUntil = rec.QuarantinedUntilIso
				if status.QuarantinedUntil == "" {
					status.QuarantinedUntil = time.Unix(int64(rec.QuarantinedUntil), 0).UTC().Format(time.RFC3339)
				}
				status.DeclineCount = rec.DeclineCount
				status.LastReason = rec.LastReason
			}
		}
		statuses = append(statuses, status)
	}
	return statuses
}

func topupBillingCardsAvailable(cfg GatewayConfig) (bool, string) {
	mode := topupBillingBindMode()
	if envBool("BILLING_CARD_DECLINE_QUARANTINE_DISABLED", false) || envBool("BILLING_CARD_DECLINE_ALLOW_ALL_QUARANTINED", false) {
		return true, ""
	}
	cards := topupBillingCardsForMode(cfg, mode)
	if len(cards) == 0 {
		return true, ""
	}
	state := loadTopupCardDeclineState()
	if len(state.Cards) == 0 {
		return true, ""
	}
	now := float64(time.Now().Unix())
	skipped := []string{}
	for _, card := range cards {
		rec, ok := topupCardDeclineRecordForCard(state, card)
		if !ok || rec.QuarantinedUntil <= now {
			return true, ""
		}
		until := rec.QuarantinedUntilIso
		if until == "" {
			until = time.Unix(int64(rec.QuarantinedUntil), 0).UTC().Format(time.RFC3339)
		}
		skipped = append(skipped, fmt.Sprintf("card ****%s quarantined until %s after %d card_declined", topupBillingCardTail(card), until, rec.DeclineCount))
	}
	return false, "所有 billing 卡都处于 card_declined 隔离期；" + strings.Join(skipped, "; ") + "；请更换新卡/配置 billingCardPool，或设置 BILLING_CARD_DECLINE_QUARANTINE_DISABLED=1"
}

func topupCardDeclineRecordForCard(state topupCardDeclineState, card topupBillingCard) (topupCardDeclineRecord, bool) {
	if state.Cards == nil {
		return topupCardDeclineRecord{}, false
	}
	if rec, ok := state.Cards[topupBillingCardKey(card)]; ok {
		return rec, true
	}
	if rec, ok := state.Cards[topupBillingCardLegacyKey(card)]; ok {
		return rec, true
	}
	if envBool("BILLING_CARD_DECLINE_TAIL_FALLBACK", true) {
		tail := topupBillingCardTail(card)
		now := float64(time.Now().Unix())
		for _, rec := range state.Cards {
			if rec.Tail == tail && rec.QuarantinedUntil > now {
				return rec, true
			}
		}
	}
	return topupCardDeclineRecord{}, false
}

func topupBillingBindMode() string {
	mode := strings.ToLower(strings.TrimSpace(os.Getenv("BILLING_BIND_MODE")))
	if mode == "" {
		mode = "browser"
	}
	return mode
}

func topupBillingBrowserCardPoolMode(mode string) bool {
	// Browser and protocol+same-browser attach both support card pools/generator.
	// Only raw/api modes are pinned to the primary card.
	return mode != "api" && mode != "raw"
}

func topupBillingCardsForMode(cfg GatewayConfig, mode string) []topupBillingCard {
	if topupBillingBrowserCardPoolMode(mode) {
		return topupBillingCardsFromConfig(cfg)
	}
	primary := normalizeTopupBillingCard(cfg.BillingCardNumber, cfg.BillingCardExpiry, cfg.BillingCardCvc, "", "")
	if primary.complete() {
		return []topupBillingCard{primary}
	}
	return nil
}

func loadTopupCardDeclineState() topupCardDeclineState {
	path := strings.TrimSpace(os.Getenv("BILLING_CARD_DECLINE_STATE"))
	if path == "" {
		path = "/data/billing-card-declines.json"
	}
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) == 0 {
		return topupCardDeclineState{}
	}
	var state topupCardDeclineState
	if err := json.Unmarshal(raw, &state); err != nil {
		return topupCardDeclineState{}
	}
	return state
}

func topupBillingCardsFromConfig(cfg GatewayConfig) []topupBillingCard {
	configured := topupConfiguredBillingCardsFromConfig(cfg)
	generated := topupGeneratedBillingCards(cfg, configured)
	if cfg.BillingCardGeneratorEnabled && len(generated) > 0 {
		return generated
	}
	return configured
}

func topupConfiguredBillingCardsFromConfig(cfg GatewayConfig) []topupBillingCard {
	cards := []topupBillingCard{}
	primary := normalizeTopupBillingCard(cfg.BillingCardNumber, cfg.BillingCardExpiry, cfg.BillingCardCvc, "", "")
	if primary.complete() {
		cards = append(cards, primary)
	}
	for _, entry := range splitTopupCardPool(cfg.BillingCardPool) {
		card := parseTopupBillingCardEntry(entry)
		if card.complete() {
			cards = append(cards, card)
		}
	}
	seen := map[string]bool{}
	deduped := []topupBillingCard{}
	for _, card := range cards {
		key := topupBillingCardKey(card)
		if seen[key] {
			continue
		}
		seen[key] = true
		deduped = append(deduped, card)
	}
	return topupFilterBillingCardsByAllowedPrefixes(deduped, cfg)
}

func topupBillingCardAllowedPrefixes(cfg GatewayConfig) []string {
	raw := strings.TrimSpace(cfg.BillingCardAllowedPrefixes)
	if raw == "" {
		raw = strings.TrimSpace(os.Getenv("BILLING_CARD_ALLOWED_PREFIXES"))
	}
	return splitGeneratorPrefixes(raw)
}

func topupFilterBillingCardsByAllowedPrefixes(cards []topupBillingCard, cfg GatewayConfig) []topupBillingCard {
	prefixes := topupBillingCardAllowedPrefixes(cfg)
	if len(prefixes) == 0 {
		return cards
	}
	filtered := []topupBillingCard{}
	for _, card := range cards {
		for _, prefix := range prefixes {
			if strings.HasPrefix(card.Number, prefix) {
				filtered = append(filtered, card)
				break
			}
		}
	}
	return filtered
}

func topupGeneratedBillingCards(cfg GatewayConfig, existing []topupBillingCard) []topupBillingCard {
	if !topupBillingCardGeneratorActive(cfg) {
		return nil
	}
	count := cfg.BillingCardGeneratorCount
	if count <= 0 {
		count = 5
	}
	if count > 50 {
		count = 50
	}
	prefixes := topupBillingCardGeneratorPrefixes(cfg, existing)
	if len(prefixes) == 0 {
		return nil
	}
	existingNumbers := map[string]bool{}
	for _, card := range existing {
		existingNumbers[card.Number] = true
	}
	cards := []topupBillingCard{}
	for i := 0; len(cards) < count && i < count*20; i++ {
		prefix := prefixes[i%len(prefixes)]
		number := topupGeneratedCardNumber(prefix, i)
		if existingNumbers[number] {
			continue
		}
		existingNumbers[number] = true
		expMonth, expYear := topupGeneratedCardExpiry(cfg, i)
		card := topupBillingCard{Number: number, ExpMonth: expMonth, ExpYear: expYear, Cvc: topupGeneratedCardCvc(prefix, i), Generated: true}
		cards = append(cards, card)
	}
	return topupFilterBillingCardsByAllowedPrefixes(cards, cfg)
}

func topupBillingCardGeneratorActive(cfg GatewayConfig) bool {
	return cfg.BillingCardGeneratorEnabled
}

func topupBillingCardGeneratorMockContext() bool {
	return envBool("BILLING_TEST_MODE", false) || envBool("BILLING_MOCK_STRIPE_PM", false) || envBool("BILLING_MOCK_ADD_CARD", false)
}

func topupBillingCardGeneratorPrefixes(cfg GatewayConfig, existing []topupBillingCard) []string {
	raw := strings.TrimSpace(cfg.BillingCardGeneratorPrefixes)
	mode := strings.ToLower(raw)
	prefixes := []string{}
	if cfg.BillingCardGeneratorEnabled && (mode == "" || mode == "auto" || mode == "default") {
		prefixes = append(prefixes, defaultGeneratedCardPrefixMap("415464"))
	} else if mode == "common" || mode == "stripe-test" || mode == "test" {
		prefixes = append(prefixes, commonSyntheticCardPrefixes()...)
	} else {
		prefixes = append(prefixes, splitGeneratorPrefixes(raw)...)
	}
	if len(prefixes) == 0 {
		prefixes = append(prefixes, defaultGeneratedCardPrefixMap("415464"))
	}
	return dedupeStrings(prefixes)
}

func topupConfiguredCardPrefixes(cfg GatewayConfig, existing []topupBillingCard) []string {
	digits := cfg.BillingCardGeneratorPrefixDigits
	if digits < 1 {
		digits = 6
	}
	if digits > 12 {
		digits = 12
	}
	prefixes := []string{}
	for _, card := range existing {
		if len(card.Number) > digits {
			prefixes = append(prefixes, card.Number[:digits])
		}
	}
	return dedupeStrings(prefixes)
}

func commonSyntheticCardPrefixes() []string {
	return []string{"424242", "400005", "555555", "520082", "222300", "378282"}
}

func defaultGeneratedCardPrefixMap(prefix string) string {
	switch digitsOnlyString(prefix) {
	case "415464":
		return "415464440133"
	default:
		return digitsOnlyString(prefix)
	}
}

func splitGeneratorPrefixes(raw string) []string {
	parts := strings.FieldsFunc(raw, func(r rune) bool { return r == ',' || r == ';' || r == '\n' || r == '\t' || r == ' ' })
	out := []string{}
	for _, part := range parts {
		digits := defaultGeneratedCardPrefixMap(part)
		if len(digits) >= 1 && len(digits) <= 15 {
			out = append(out, digits)
		}
	}
	return dedupeStrings(out)
}

func dedupeStrings(items []string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, item := range items {
		if item == "" || seen[item] {
			continue
		}
		seen[item] = true
		out = append(out, item)
	}
	return out
}

func topupGeneratedCardNumber(prefix string, index int) string {
	prefix = digitsOnlyString(prefix)
	length := 16
	if strings.HasPrefix(prefix, "34") || strings.HasPrefix(prefix, "37") {
		length = 15
	}
	if len(prefix) >= length {
		prefix = prefix[:length-1]
	}
	bodyLen := length - 1
	seed := fmt.Sprintf("%s:%d:%d", prefix, index, time.Now().UTC().UnixNano())
	sum := sha256.Sum256([]byte(seed))
	body := prefix
	for i := 0; len(body) < bodyLen; i++ {
		body += strconv.Itoa(int(sum[i%len(sum)] % 10))
	}
	return body + luhnCheckDigit(body)
}

func topupGeneratedCardExpiry(cfg GatewayConfig, index int) (string, string) {
	now := time.Now().UTC()
	minMonths := cfg.BillingCardGeneratorMinMonths
	if minMonths < 1 {
		minMonths = envInt("BILLING_CARD_GENERATOR_MIN_MONTHS", 18)
	}
	if minMonths < 1 {
		minMonths = 18
	}
	if minMonths > 120 {
		minMonths = 120
	}
	maxMonths := cfg.BillingCardGeneratorMaxMonths
	if maxMonths < minMonths {
		maxMonths = envInt("BILLING_CARD_GENERATOR_MAX_MONTHS", 60)
	}
	if maxMonths < minMonths {
		maxMonths = minMonths
	}
	if maxMonths > 120 {
		maxMonths = 120
	}
	span := maxMonths - minMonths + 1
	sum := sha256.Sum256([]byte(fmt.Sprintf("expiry:%d:%d", index, now.UnixNano())))
	offset := minMonths + int(sum[0])%span
	monthIndex := now.Year()*12 + int(now.Month()) - 1 + offset
	month := monthIndex%12 + 1
	year := monthIndex / 12
	return fmt.Sprintf("%02d", month), strconv.Itoa(year)
}

func topupGeneratedCardCvc(prefix string, index int) string {
	length := 3
	if strings.HasPrefix(prefix, "34") || strings.HasPrefix(prefix, "37") {
		length = 4
	}
	sum := sha256.Sum256([]byte(fmt.Sprintf("cvc:%s:%d:%d", prefix, index, time.Now().UTC().UnixNano())))
	out := ""
	for i := 0; len(out) < length; i++ {
		out += strconv.Itoa(int(sum[i%len(sum)] % 10))
	}
	return out
}

func luhnCheckDigit(body string) string {
	for d := 0; d <= 9; d++ {
		candidate := body + strconv.Itoa(d)
		if luhnValid(candidate) {
			return strconv.Itoa(d)
		}
	}
	return "0"
}

func luhnValid(number string) bool {
	digits := digitsOnlyString(number)
	if digits == "" {
		return false
	}
	total := 0
	parity := len(digits) % 2
	for i, r := range digits {
		digit := int(r - '0')
		if i%2 == parity {
			digit *= 2
			if digit > 9 {
				digit -= 9
			}
		}
		total += digit
	}
	return total%10 == 0
}

func digitsOnlyString(value string) string {
	var b strings.Builder
	for _, r := range value {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

func splitTopupCardPool(pool string) []string {
	pool = strings.TrimSpace(pool)
	if pool == "" {
		return nil
	}
	var parsed any
	if (strings.HasPrefix(pool, "[") || strings.HasPrefix(pool, "{")) && json.Unmarshal([]byte(pool), &parsed) == nil {
		items := []any{}
		switch value := parsed.(type) {
		case []any:
			items = value
		case map[string]any:
			if cards, ok := value["cards"].([]any); ok {
				items = cards
			} else {
				items = []any{value}
			}
		}
		out := []string{}
		for _, item := range items {
			if m, ok := item.(map[string]any); ok {
				number := firstStringAny(m, "number", "cardNumber", "billingCardNumber")
				expiry := firstStringAny(m, "expiry", "exp", "billingCardExpiry")
				cvc := firstStringAny(m, "cvc", "cvv", "billingCardCvc")
				expMonth := firstStringAny(m, "exp_month", "expMonth", "month")
				expYear := firstStringAny(m, "exp_year", "expYear", "year")
				card := normalizeTopupBillingCard(number, expiry, cvc, expMonth, expYear)
				if card.complete() {
					out = append(out, strings.Join([]string{card.Number, card.ExpMonth, card.ExpYear, card.Cvc}, "|"))
				}
			} else if text := strings.TrimSpace(fmt.Sprint(item)); text != "" {
				out = append(out, text)
			}
		}
		return out
	}
	return strings.FieldsFunc(pool, func(r rune) bool { return r == '\n' || r == ';' })
}

func firstStringAny(m map[string]any, keys ...string) string {
	for _, key := range keys {
		if v, ok := m[key]; ok && v != nil {
			return strings.TrimSpace(fmt.Sprint(v))
		}
	}
	return ""
}

func parseTopupBillingCardEntry(entry string) topupBillingCard {
	parts := strings.FieldsFunc(strings.TrimSpace(entry), func(r rune) bool {
		return r == '|' || r == ',' || r == '\t' || r == ' '
	})
	clean := []string{}
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part != "" {
			clean = append(clean, part)
		}
	}
	if len(clean) >= 4 && digitsOnly(clean[1]) && digitsOnly(clean[2]) {
		return normalizeTopupBillingCard(clean[0], "", clean[3], clean[1], clean[2])
	}
	if len(clean) >= 3 {
		return normalizeTopupBillingCard(clean[0], clean[1], clean[2], "", "")
	}
	return topupBillingCard{}
}

func normalizeTopupBillingCard(number, expiry, cvc, expMonth, expYear string) topupBillingCard {
	number = strings.NewReplacer(" ", "", "-", "").Replace(strings.TrimSpace(number))
	expiry = strings.NewReplacer(" ", "", "-", "/").Replace(strings.TrimSpace(expiry))
	expMonth = strings.TrimSpace(expMonth)
	expYear = strings.TrimSpace(expYear)
	if expiry != "" && (expMonth == "" || expYear == "") {
		if strings.Contains(expiry, "/") {
			parts := strings.SplitN(expiry, "/", 2)
			if expMonth == "" {
				expMonth = parts[0]
			}
			if expYear == "" {
				expYear = parts[1]
			}
		} else if len(expiry) == 4 || len(expiry) == 6 {
			if expMonth == "" {
				expMonth = expiry[:2]
			}
			if expYear == "" {
				expYear = expiry[2:]
			}
		}
	}
	if len(expMonth) == 1 {
		expMonth = "0" + expMonth
	}
	if len(expYear) == 2 && digitsOnly(expYear) {
		year, _ := strconv.Atoi(expYear)
		century := time.Now().UTC().Year() / 100 * 100
		year += century
		if year < time.Now().UTC().Year()-5 {
			year += 100
		}
		expYear = strconv.Itoa(year)
	}
	return topupBillingCard{Number: number, ExpMonth: expMonth, ExpYear: expYear, Cvc: strings.TrimSpace(cvc)}
}

func (c topupBillingCard) complete() bool {
	return c.Number != "" && c.ExpMonth != "" && c.ExpYear != "" && c.Cvc != ""
}

func topupBillingCardKey(card topupBillingCard) string {
	sum := sha256.Sum256([]byte(topupBillingCardKeyMaterial(card)))
	return hex.EncodeToString(sum[:])
}

func topupBillingCardKeyMode() string {
	mode := strings.ToLower(strings.TrimSpace(os.Getenv("BILLING_CARD_DECLINE_KEY_MODE")))
	if mode == "" {
		mode = "pan"
	}
	return mode
}

func topupBillingCardKeyMaterial(card topupBillingCard) string {
	mode := topupBillingCardKeyMode()
	if mode == "full" || mode == "card" || mode == "all" {
		return strings.Join([]string{card.Number, card.ExpMonth, card.ExpYear, card.Cvc}, "|")
	}
	return card.Number
}

func topupBillingCardLegacyKey(card topupBillingCard) string {
	sum := sha256.Sum256([]byte(strings.Join([]string{card.Number, card.ExpMonth, card.ExpYear, card.Cvc}, "|")))
	return hex.EncodeToString(sum[:])
}

func topupBillingCardTail(card topupBillingCard) string {
	if len(card.Number) <= 4 {
		if card.Number == "" {
			return "unknown"
		}
		return card.Number
	}
	return card.Number[len(card.Number)-4:]
}

func digitsOnly(value string) bool {
	if value == "" {
		return false
	}
	for _, r := range value {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}
