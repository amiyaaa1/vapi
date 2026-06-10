package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"strings"
)

func requestBodyLogEnabled() bool {
	return envBool("REQUEST_BODY_LOG_ENABLED", false)
}

func requestBodyLogMaxBytes() int {
	maxBytes := envInt("REQUEST_BODY_LOG_MAX_BYTES", 200000)
	if maxBytes < 1024 {
		return 1024
	}
	return maxBytes
}

func formatBodyForLog(raw []byte) (string, bool) {
	body := bytes.TrimSpace(raw)
	var parsed any
	if len(body) > 0 && json.Unmarshal(body, &parsed) == nil {
		if compact, err := json.Marshal(parsed); err == nil {
			body = compact
		}
	}

	maxBytes := requestBodyLogMaxBytes()
	truncated := len(body) > maxBytes
	if truncated {
		body = body[:maxBytes]
	}
	return string(body), truncated
}

func logRequestBody(requestID, label string, raw []byte) {
	if !requestBodyLogEnabled() {
		return
	}
	body, truncated := formatBodyForLog(raw)
	log.Printf("[request-body] id=%s label=%s bytes=%d truncated=%t body=%s", requestID, label, len(raw), truncated, body)
}

func logJSONPayload(requestID, label string, payload any) {
	if !requestBodyLogEnabled() {
		return
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		log.Printf("[request-body] id=%s label=%s marshal_error=%v", requestID, label, err)
		return
	}
	logRequestBody(requestID, label, raw)
}

func logPrefix(value string, maxChars int) string {
	if maxChars <= 0 {
		return ""
	}
	runes := []rune(strings.TrimSpace(value))
	if len(runes) <= maxChars {
		return string(runes)
	}
	return string(runes[:maxChars]) + "...[truncated]"
}

func rawContentCharLen(raw json.RawMessage) int {
	if len(raw) == 0 {
		return 0
	}
	if text, ok := rawContentText(raw); ok {
		return len([]rune(text))
	}
	return len([]rune(string(raw)))
}

func logVapiRequestSummary(requestID string, req *VapiRequest) {
	if !requestBodyLogEnabled() || req == nil {
		return
	}

	model := req.Assistant.Model
	historyLens := make([]int, 0, len(model.Messages))
	for _, msg := range model.Messages {
		historyLens = append(historyLens, rawContentCharLen(msg.Content))
	}

	inputType := "unknown"
	inputCount := 0
	inputLens := []int{}
	inputRoles := []string{}
	toolIDCounts := map[string]int{}

	input := bytes.TrimSpace(req.Input)
	if len(input) > 0 {
		switch input[0] {
		case '"':
			var text string
			if json.Unmarshal(input, &text) == nil {
				inputType = "string"
				inputCount = 1
				inputLens = append(inputLens, len([]rune(text)))
			}
		case '[':
			var messages []VapiMessage
			if json.Unmarshal(input, &messages) == nil {
				inputType = "messages"
				inputCount = len(messages)
				for i, msg := range messages {
					inputLens = append(inputLens, rawContentCharLen(msg.Content))
					inputRoles = append(inputRoles, fmt.Sprintf("%d:%s/tc=%d/tid=%t", i, msg.Role, len(msg.ToolCalls), msg.ToolCallID != ""))
					if msg.ToolCallID != "" {
						toolIDCounts[msg.ToolCallID]++
					}
				}
			}
		}
	}

	log.Printf(
		"[request-summary] id=%s stream=%t provider=%s model=%s maxTokens=%d history=%d history_lens=%v tools=%d input_type=%s input_count=%d input_lens=%v input_roles=%v tool_id_counts=%v",
		requestID,
		req.Stream,
		model.Provider,
		model.Model,
		model.MaxTokens,
		len(model.Messages),
		historyLens,
		len(model.Tools),
		inputType,
		inputCount,
		inputLens,
		inputRoles,
		toolIDCounts,
	)
}
