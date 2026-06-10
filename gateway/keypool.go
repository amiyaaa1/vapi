package main

import (
	"bufio"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type Key struct {
	Value          string
	ActiveRequests int
	Terminal       string // "", "consumed", "failed"
	UsedAt         time.Time
}

type KeyPool struct {
	mu           sync.RWMutex
	keys         []*Key
	idx          int
	path         string
	consumedPath string
	failedPath   string
}

func NewKeyPool(path string) *KeyPool {
	p := &KeyPool{
		path:         path,
		consumedPath: envOr("CONSUMED_KEYS_PATH", sidecarPath(path, ".consumed")),
		failedPath:   envOr("FAILED_KEYS_PATH", sidecarPath(path, ".failed")),
		idx:          -1,
	}
	p.load()
	return p
}

func sidecarPath(path string, suffix string) string {
	ext := filepath.Ext(path)
	if ext == "" {
		return path + suffix
	}
	return strings.TrimSuffix(path, ext) + suffix + ext
}

func (p *KeyPool) load() {
	p.keys = p.loadKeysFromDisk(nil)
	log.Printf("[keypool] 加载 %d 个 key", len(p.keys))
}

func (p *KeyPool) loadKeysFromDisk(existing map[string]*Key) []*Key {
	f, err := os.Open(p.path)
	if err != nil {
		return preserveExistingKeys(nil, existing)
	}
	defer f.Close()

	seen := map[string]bool{}
	consumed := readKeySet(p.consumedPath)
	failed := readKeySet(p.failedPath)
	var keys []*Key
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		k := strings.TrimSpace(sc.Text())
		if k == "" || strings.HasPrefix(k, "#") || seen[k] || consumed[k] || failed[k] {
			continue
		}
		seen[k] = true
		if existingKey := existing[k]; existingKey != nil {
			keys = append(keys, existingKey)
		} else {
			keys = append(keys, &Key{Value: k})
		}
	}
	return preserveExistingKeys(keys, existing)
}

func (p *KeyPool) Reload() {
	p.mu.Lock()
	defer p.mu.Unlock()

	existing := map[string]*Key{}
	for _, key := range p.keys {
		if key.ActiveRequests > 0 || key.Terminal != "" {
			existing[key.Value] = key
		}
	}
	p.keys = p.loadKeysFromDisk(existing)
	if len(p.keys) == 0 {
		p.idx = -1
	} else if p.idx >= len(p.keys) {
		p.idx = len(p.keys) - 1
	}
	log.Printf("[keypool] 重新加载 %d 个 active key", p.assignableLocked())
}

func preserveExistingKeys(keys []*Key, existing map[string]*Key) []*Key {
	if len(existing) == 0 {
		return keys
	}
	seen := map[string]bool{}
	for _, key := range keys {
		seen[key.Value] = true
	}
	for _, key := range existing {
		if seen[key.Value] {
			continue
		}
		keys = append(keys, key)
	}
	return keys
}

func (p *KeyPool) saveLocked() {
	if err := os.MkdirAll(filepath.Dir(p.path), 0755); err != nil {
		log.Printf("[keypool] 创建目录失败: %v", err)
		return
	}
	f, err := os.Create(p.path)
	if err != nil {
		log.Printf("[keypool] 保存失败: %v", err)
		return
	}
	defer f.Close()
	for _, k := range p.keys {
		if k.Terminal != "" {
			continue
		}
		f.WriteString(k.Value + "\n")
	}
}

// Next 优先复用尚未完成任何请求的 in-flight key。
// 一旦某个请求成功完成，key 会进入 draining，不再分配新请求。
func (p *KeyPool) Next() *Key {
	p.mu.Lock()
	defer p.mu.Unlock()

	n := len(p.keys)
	if n == 0 {
		return nil
	}

	for i := 0; i < n; i++ {
		p.idx = (p.idx + 1) % n
		k := p.keys[p.idx]
		if k.Terminal == "" && k.ActiveRequests > 0 {
			k.ActiveRequests++
			k.UsedAt = time.Now()
			return k
		}
	}

	for i := 0; i < n; i++ {
		p.idx = (p.idx + 1) % n
		k := p.keys[p.idx]
		if k.Terminal == "" && k.ActiveRequests == 0 {
			k.ActiveRequests = 1
			k.UsedAt = time.Now()
			return k
		}
	}
	return nil
}

// Release 释放未消费的 in-flight key，用于可重试的非额度错误。
func (p *KeyPool) Release(value string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, k := range p.keys {
		if k.Value == value {
			p.finishRequestLocked(k)
			return
		}
	}
}

// Consume 成功调用后让 key 进入 draining，并持久消费，避免重启后复活。
func (p *KeyPool) Consume(value string) {
	p.finishTerminal(value, "consumed")
}

// Disable 将不可用 key 持久移入 failed 池。
func (p *KeyPool) Disable(value string) {
	p.finishTerminal(value, "failed")
}

func (p *KeyPool) finishTerminal(value string, terminal string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, k := range p.keys {
		if k.Value == value {
			p.markTerminalLocked(k, terminal)
			p.finishRequestLocked(k)
			return
		}
	}
}

func (p *KeyPool) markTerminalLocked(k *Key, terminal string) {
	if k.Terminal == "consumed" {
		return
	}
	if terminal == "consumed" {
		if k.Terminal == "failed" {
			removeKeyFromFile(p.failedPath, k.Value)
		}
		k.Terminal = "consumed"
		appendUniqueKey(p.consumedPath, k.Value)
		p.saveLocked()
		log.Printf("[keypool] 已消费 key: %s...", maskKey(k.Value))
		return
	}
	if k.Terminal == "" {
		k.Terminal = "failed"
		appendUniqueKey(p.failedPath, k.Value)
		p.saveLocked()
		log.Printf("[keypool] 禁用 key: %s...", maskKey(k.Value))
	}
}

func (p *KeyPool) finishRequestLocked(k *Key) {
	if k.ActiveRequests > 0 {
		k.ActiveRequests--
	}
	if k.ActiveRequests == 0 && k.Terminal != "" {
		p.removeLocked(k.Value)
	}
}

func (p *KeyPool) removeLocked(value string) {
	for i, k := range p.keys {
		if k.Value != value {
			continue
		}
		p.keys = append(p.keys[:i], p.keys[i+1:]...)
		if len(p.keys) == 0 {
			p.idx = -1
		} else if p.idx >= len(p.keys) {
			p.idx = len(p.keys) - 1
		}
		return
	}
}

// Import 批量导入，去重
func (p *KeyPool) Import(newKeys []string) int {
	p.mu.Lock()
	defer p.mu.Unlock()

	seen := map[string]bool{}
	for _, k := range p.keys {
		seen[k.Value] = true
	}
	for k := range readKeySet(p.consumedPath) {
		seen[k] = true
	}
	for k := range readKeySet(p.failedPath) {
		seen[k] = true
	}

	added := 0
	for _, k := range newKeys {
		k = strings.TrimSpace(k)
		if k == "" || strings.HasPrefix(k, "#") || seen[k] {
			continue
		}
		seen[k] = true
		p.keys = append(p.keys, &Key{Value: k})
		added++
	}

	if added > 0 {
		p.saveLocked()
		log.Printf("[keypool] 导入 %d 个新 key，总计 %d", added, len(p.keys))
	}
	return added
}

type PoolStats struct {
	Total          int `json:"total"`
	Active         int `json:"active"`
	InFlight       int `json:"in_flight"`
	InFlightKeys   int `json:"in_flight_keys"`
	ActiveRequests int `json:"active_requests"`
	DrainingKeys   int `json:"draining_keys"`
	Consumed       int `json:"consumed"`
	Failed         int `json:"failed"`
}

func (p *KeyPool) Stats() PoolStats {
	p.mu.RLock()
	defer p.mu.RUnlock()
	s := PoolStats{
		Consumed: countKeys(p.consumedPath),
		Failed:   countKeys(p.failedPath),
	}
	for _, k := range p.keys {
		if k.Terminal == "" {
			s.Active++
		}
		if k.ActiveRequests > 0 {
			s.InFlightKeys++
			if k.Terminal != "" {
				s.DrainingKeys++
			}
		}
		s.ActiveRequests += k.ActiveRequests
	}
	s.InFlight = s.ActiveRequests
	s.Total = s.Active + s.Consumed + s.Failed
	return s
}

func (p *KeyPool) assignableLocked() int {
	count := 0
	for _, k := range p.keys {
		if k.Terminal == "" {
			count++
		}
	}
	return count
}

func readKeySet(path string) map[string]bool {
	out := map[string]bool{}
	f, err := os.Open(path)
	if err != nil {
		return out
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	for sc.Scan() {
		k := strings.TrimSpace(sc.Text())
		if k == "" || strings.HasPrefix(k, "#") {
			continue
		}
		out[k] = true
	}
	return out
}

func countKeys(path string) int {
	return len(readKeySet(path))
}

func appendUniqueKey(path string, value string) {
	value = strings.TrimSpace(value)
	if value == "" || readKeySet(path)[value] {
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		log.Printf("[keypool] 创建目录失败: %v", err)
		return
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		log.Printf("[keypool] 写入 sidecar 失败: %v", err)
		return
	}
	defer f.Close()
	_, _ = f.WriteString(value + "\n")
}

func removeKeyFromFile(path string, value string) {
	value = strings.TrimSpace(value)
	if value == "" {
		return
	}
	lines := []string{}
	f, err := os.Open(path)
	if err != nil {
		return
	}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || line == value {
			continue
		}
		lines = append(lines, line)
	}
	_ = f.Close()
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		log.Printf("[keypool] 创建目录失败: %v", err)
		return
	}
	raw := strings.Join(lines, "\n")
	if raw != "" {
		raw += "\n"
	}
	if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
		log.Printf("[keypool] 更新 sidecar 失败: %v", err)
	}
}

func maskKey(value string) string {
	if len(value) <= 8 {
		return value
	}
	return value[:8]
}
