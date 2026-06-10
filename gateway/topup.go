package main

import (
	"context"
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
	LastError      string            `json:"lastError,omitempty"`
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
