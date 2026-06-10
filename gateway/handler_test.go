package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestNonSystemInputCharLenExcludesSystemAndDeveloper(t *testing.T) {
	messages := []OAIMessage{
		{Role: "system", Content: jsonStringRaw(strings.Repeat("s", 100))},
		{Role: "developer", Content: jsonStringRaw(strings.Repeat("d", 100))},
		{Role: "user", Content: jsonStringRaw("你好短")},
	}

	if got := nonSystemInputCharLen(messages); got != 3 {
		t.Fatalf("nonSystemInputCharLen = %d, want 3", got)
	}
}

func TestChatHandlerRejectsShortInputAfterExcludingSystem(t *testing.T) {
	body := `{
		"model":"claude-opus-4-6",
		"messages":[
			{"role":"system","content":"this system prompt is intentionally long enough to prove it is excluded"},
			{"role":"user","content":"hi"}
		]
	}`
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	rec := httptest.NewRecorder()

	ChatHandler(nil, nil)(rec, req)

	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d body=%s, want 429", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"message":"Too Many Requests"`) {
		t.Fatalf("body=%s, want Too Many Requests message", rec.Body.String())
	}
}
