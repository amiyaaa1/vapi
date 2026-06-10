package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestKeyPoolPrefersHotKeyUntilFirstSuccessThenDrains(t *testing.T) {
	dir := t.TempDir()
	keysPath := filepath.Join(dir, "keys.txt")
	t.Setenv("CONSUMED_KEYS_PATH", filepath.Join(dir, "keys.consumed.txt"))
	t.Setenv("FAILED_KEYS_PATH", filepath.Join(dir, "keys.failed.txt"))
	if err := os.WriteFile(keysPath, []byte("key-a\nkey-b\n"), 0600); err != nil {
		t.Fatal(err)
	}

	pool := NewKeyPool(keysPath)

	first := pool.Next()
	if first == nil || first.Value != "key-a" {
		t.Fatalf("first key = %#v, want key-a", first)
	}
	second := pool.Next()
	if second == nil || second.Value != "key-a" {
		t.Fatalf("second key = %#v, want hot key-a", second)
	}

	pool.Consume(second.Value)
	stats := pool.Stats()
	if stats.Active != 1 || stats.InFlight != 1 || stats.InFlightKeys != 1 || stats.ActiveRequests != 1 || stats.DrainingKeys != 1 || stats.Consumed != 1 {
		t.Fatalf("after first success stats = %+v, want active=1 in_flight=1 in_flight_keys=1 active_requests=1 draining_keys=1 consumed=1", stats)
	}

	third := pool.Next()
	if third == nil || third.Value != "key-b" {
		t.Fatalf("third key = %#v, want key-b after key-a starts draining", third)
	}

	pool.Release(first.Value)
	stats = pool.Stats()
	if stats.Active != 1 || stats.InFlight != 1 || stats.InFlightKeys != 1 || stats.ActiveRequests != 1 || stats.DrainingKeys != 0 || stats.Consumed != 1 {
		t.Fatalf("after drained key-a release stats = %+v, want active=1 in_flight=1 in_flight_keys=1 active_requests=1 draining_keys=0 consumed=1", stats)
	}

	pool.Consume(third.Value)
	stats = pool.Stats()
	if stats.Active != 0 || stats.InFlight != 0 || stats.InFlightKeys != 0 || stats.ActiveRequests != 0 || stats.Consumed != 2 {
		t.Fatalf("final stats = %+v, want active=0 in_flight=0 in_flight_keys=0 active_requests=0 consumed=2", stats)
	}
}

func TestKeyPoolDoesNotMoveConsumedDrainingKeyToFailed(t *testing.T) {
	dir := t.TempDir()
	keysPath := filepath.Join(dir, "keys.txt")
	failedPath := filepath.Join(dir, "keys.failed.txt")
	t.Setenv("CONSUMED_KEYS_PATH", filepath.Join(dir, "keys.consumed.txt"))
	t.Setenv("FAILED_KEYS_PATH", failedPath)
	if err := os.WriteFile(keysPath, []byte("key-a\n"), 0600); err != nil {
		t.Fatal(err)
	}

	pool := NewKeyPool(keysPath)
	first := pool.Next()
	second := pool.Next()
	if first == nil || second == nil || first.Value != second.Value {
		t.Fatalf("expected two in-flight uses of same key, got %#v %#v", first, second)
	}

	pool.Consume(second.Value)
	pool.Disable(first.Value)

	stats := pool.Stats()
	if stats.Consumed != 1 || stats.Failed != 0 || stats.Active != 0 || stats.InFlight != 0 || stats.InFlightKeys != 0 || stats.ActiveRequests != 0 {
		t.Fatalf("stats = %+v, want consumed=1 failed=0 active=0 in_flight=0 in_flight_keys=0 active_requests=0", stats)
	}
	if raw, err := os.ReadFile(failedPath); err == nil && len(raw) > 0 {
		t.Fatalf("failed sidecar should be empty, got %q", string(raw))
	}
}
