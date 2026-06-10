package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"
)

const (
	maxRetry           = 3
	maxContinue        = 5 // 最大续写次数
	minInputCharsNoSys = 20
)

// Vapi /chat/responses SSE event 格式
type vapiEvent struct {
	Type           string          `json:"type"`
	Delta          string          `json:"delta,omitempty"`
	Text           string          `json:"text,omitempty"`
	ItemID         string          `json:"item_id,omitempty"`
	OutputIndex    int             `json:"output_index"`
	ContentIndex   int             `json:"content_index"`
	SequenceNumber int             `json:"sequence_number"`
	Item           json.RawMessage `json:"item,omitempty"`
	Response       json.RawMessage `json:"response,omitempty"`
}

type vapiItemDone struct {
	ID        string `json:"id"`
	Type      string `json:"type"`
	CallID    string `json:"call_id,omitempty"`
	Name      string `json:"name,omitempty"`
	Arguments string `json:"arguments,omitempty"`
	Status    string `json:"status,omitempty"`
}

// streamState 在续写循环中共享的流状态
type streamState struct {
	streamID      string
	first         bool // 是否还没发过 role
	gotToolCall   bool
	toolCallIndex int
	toolCalls     []json.RawMessage
	usage         *OAIUsage
	collected     strings.Builder // 累积全部文本
}

// streamResult 单次流式调用的结果
type streamResult struct {
	gotDone     bool // 收到 [DONE] 或 response.completed
	gotToolCall bool
	text        string // 本轮收集的文本
}

func ChatHandler(pool *KeyPool, client *VapiClient) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		requestID := fmt.Sprintf("%d", time.Now().UnixNano())
		rawBody, err := io.ReadAll(r.Body)
		if err != nil {
			jsonError(w, 400, "invalid request body: "+err.Error())
			return
		}
		logRequestBody(requestID, "incoming-openai", rawBody)

		var req OAIRequest
		if err := json.NewDecoder(bytes.NewReader(rawBody)).Decode(&req); err != nil {
			jsonError(w, 400, "invalid request: "+err.Error())
			return
		}
		if len(req.Messages) > 0 {
			inputChars := nonSystemInputCharLen(req.Messages)
			if inputChars < minInputCharsNoSys {
				jsonError(w, 429, "Too Many Requests")
				return
			}
		}

		vapiReq, err := convertRequest(&req)
		if err != nil {
			jsonError(w, 400, err.Error())
			return
		}
		logJSONPayload(requestID, "converted-vapi", vapiReq)
		logVapiRequestSummary(requestID, vapiReq)

		if req.Stream {
			handleStream(w, r, pool, client, &req, vapiReq, requestID)
		} else {
			handleNonStream(w, r, pool, client, &req, vapiReq, requestID)
		}
	}
}

func nonSystemInputCharLen(messages []OAIMessage) int {
	parts := make([]string, 0, len(messages))
	for _, msg := range messages {
		role := strings.ToLower(strings.TrimSpace(msg.Role))
		if role == "system" || role == "developer" {
			continue
		}
		if text, ok := rawContentText(msg.Content); ok {
			parts = append(parts, text)
		}
	}
	return len([]rune(strings.TrimSpace(strings.Join(parts, "\n"))))
}

func handleNonStream(w http.ResponseWriter, r *http.Request, pool *KeyPool, client *VapiClient, req *OAIRequest, vapiReq *VapiRequest, requestID string) {
	vapiReq.Stream = false

	var key *Key
	for attempt := 0; attempt < maxRetry; attempt++ {
		key = pool.Next()
		if key == nil {
			jsonError(w, 503, "no available keys")
			return
		}

		start := time.Now()
		vapiResp, status, err := client.Chat(r.Context(), key.Value, vapiReq)
		elapsed := time.Since(start)
		if err != nil {
			log.Printf("[upstream] id=%s stream=false attempt=%d key=%s status=%d elapsed_ms=%d err=%s", requestID, attempt+1, maskKey(key.Value), status, elapsed.Milliseconds(), logPrefix(err.Error(), 2000))
			if status == 402 {
				pool.Disable(key.Value)
				log.Printf("[chat] key %s... 被禁用 (status=%d)", maskKey(key.Value), status)
				continue
			}
			pool.Release(key.Value)
			jsonError(w, 502, fmt.Sprintf("vapi error: %v", err))
			return
		}
		log.Printf("[upstream] id=%s stream=false attempt=%d key=%s status=%d elapsed_ms=%d", requestID, attempt+1, maskKey(key.Value), status, elapsed.Milliseconds())

		resp := convertResponse(vapiResp, req)
		resp.Created = time.Now().Unix()
		w.Header().Set("Content-Type", "application/json")
		if err := json.NewEncoder(w).Encode(resp); err != nil {
			pool.Release(key.Value)
			return
		}
		pool.Consume(key.Value)
		return
	}
	jsonError(w, 503, "all keys exhausted after retries")
}

func handleStream(w http.ResponseWriter, r *http.Request, pool *KeyPool, client *VapiClient, req *OAIRequest, vapiReq *VapiRequest, requestID string) {
	vapiReq.Stream = true

	// 选 key（续写沿用同一个 key）
	var key *Key
	for attempt := 0; attempt < maxRetry; attempt++ {
		key = pool.Next()
		if key == nil {
			jsonError(w, 503, "no available keys")
			return
		}

		start := time.Now()
		resp, err := client.ChatStream(r.Context(), key.Value, vapiReq)
		elapsed := time.Since(start)
		if err != nil {
			log.Printf("[upstream] id=%s stream=true attempt=%d key=%s status=0 elapsed_ms=%d err=%s", requestID, attempt+1, maskKey(key.Value), elapsed.Milliseconds(), logPrefix(err.Error(), 2000))
			pool.Release(key.Value)
			jsonError(w, 502, fmt.Sprintf("vapi stream error: %v", err))
			return
		}
		log.Printf("[upstream] id=%s stream=true attempt=%d key=%s status=%d elapsed_ms=%d", requestID, attempt+1, maskKey(key.Value), resp.StatusCode, elapsed.Milliseconds())

		if resp.StatusCode == 402 {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			log.Printf("[upstream] id=%s stream=true attempt=%d status=%d body=%s", requestID, attempt+1, resp.StatusCode, logPrefix(string(body), 4000))
			pool.Disable(key.Value)
			log.Printf("[stream] key %s... 被禁用 (status=%d)", maskKey(key.Value), resp.StatusCode)
			continue
		}
		if resp.StatusCode >= 400 {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			log.Printf("[upstream] id=%s stream=true attempt=%d status=%d body=%s", requestID, attempt+1, resp.StatusCode, logPrefix(string(body), 4000))
			pool.Release(key.Value)
			jsonError(w, 502, fmt.Sprintf("vapi %d: %s", resp.StatusCode, string(body[:min(len(body), 200)])))
			return
		}

		// 首次流式成功，进入续写循环
		streamWithContinuation(w, r, resp, pool, client, key, req, req.Model)
		pool.Consume(key.Value)
		return
	}
	jsonError(w, 503, "all keys exhausted after retries")
}

// streamWithContinuation 流式转发 + 自动续写
func streamWithContinuation(w http.ResponseWriter, r *http.Request, firstResp *http.Response, _ *KeyPool, client *VapiClient, key *Key, origReq *OAIRequest, model string) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		firstResp.Body.Close()
		jsonError(w, 500, "streaming not supported")
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")

	state := &streamState{
		streamID: fmt.Sprintf("vapi-%d", time.Now().UnixNano()),
		first:    true,
	}

	sendChunk := func(delta map[string]any, finishReason any, usage *OAIUsage) {
		oaiChunk := map[string]any{
			"id": state.streamID, "object": "chat.completion.chunk",
			"created": time.Now().Unix(), "model": model,
			"choices": []map[string]any{{
				"index": 0, "delta": delta, "finish_reason": finishReason,
			}},
		}
		if usage != nil {
			oaiChunk["usage"] = usage
		}
		out, _ := json.Marshal(oaiChunk)
		fmt.Fprintf(w, "data: %s\n\n", out)
		flusher.Flush()
	}

	// 第一轮
	result := streamAndCollect(firstResp, state, sendChunk)

	// 续写循环：截断 + 非 tool_call + 有文本 → 续写
	if streamContinuationEnabled() {
		for round := 1; round <= maxContinue; round++ {
			if result.gotDone || state.gotToolCall || state.collected.Len() == 0 {
				break
			}

			log.Printf("[续写] 第%d次续写 (已累积 %d 字符)", round, state.collected.Len())

			contReq, err := buildContinueRequest(origReq, state.collected.String(), round)
			if err != nil {
				log.Printf("[续写] 构造请求失败: %v", err)
				break
			}

			resp, err := client.ChatStream(r.Context(), key.Value, contReq)
			if err != nil {
				log.Printf("[续写] 请求失败: %v", err)
				break
			}
			if resp.StatusCode >= 400 {
				body, _ := io.ReadAll(resp.Body)
				resp.Body.Close()
				log.Printf("[续写] 上游错误 %d: %s", resp.StatusCode, string(body[:min(len(body), 200)]))
				break
			}

			result = streamAndCollect(resp, state, sendChunk)
		}
	}

	// 发送结束
	finishReason := "stop"
	if state.gotToolCall {
		finishReason = "tool_calls"
	}
	usage := state.usage
	if usage == nil {
		usage = estimateUsage(origReq, state.collected.String(), state.toolCalls)
	}
	sendChunk(map[string]any{}, finishReason, usage)
	fmt.Fprint(w, "data: [DONE]\n\n")
	flusher.Flush()
}

func streamContinuationEnabled() bool {
	return envBool("STREAM_CONTINUATION_ENABLED", envBool("ENABLE_STREAM_CONTINUATION", false))
}

// streamAndCollect 读取一次 Vapi SSE 流，转发 delta 给客户端，累积文本
func streamAndCollect(resp *http.Response, state *streamState, sendChunk func(map[string]any, any, *OAIUsage)) streamResult {
	defer resp.Body.Close()

	var result streamResult

	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		data := line[6:]
		if data == "[DONE]" {
			result.gotDone = true
			break
		}

		var evt vapiEvent
		if err := json.Unmarshal([]byte(data), &evt); err != nil {
			continue
		}

		switch evt.Type {
		case "response.created":
			var r struct {
				ID string `json:"id"`
			}
			if json.Unmarshal(evt.Response, &r) == nil && r.ID != "" {
				state.streamID = "vapi-" + r.ID
			}

		case "response.completed":
			state.usage = addUsage(state.usage, usageFromVapiResponse(evt.Response))
			result.gotDone = true

		case "response.output_text.delta":
			if state.gotToolCall {
				continue
			}
			delta := map[string]any{}
			if state.first {
				delta["role"] = "assistant"
				state.first = false
			}
			delta["content"] = evt.Delta
			sendChunk(delta, nil, nil)
			state.collected.WriteString(evt.Delta)

		case "response.output_item.done":
			if evt.Item == nil {
				continue
			}
			var item vapiItemDone
			if err := json.Unmarshal(evt.Item, &item); err != nil {
				continue
			}

			if item.Type == "function_call" {
				state.gotToolCall = true
				result.gotToolCall = true

				toolCall := map[string]any{
					"index": state.toolCallIndex,
					"id":    item.CallID,
					"type":  "function",
					"function": map[string]any{
						"name":      item.Name,
						"arguments": item.Arguments,
					},
				}
				if raw, err := json.Marshal(toolCall); err == nil {
					state.toolCalls = append(state.toolCalls, raw)
				}

				delta := map[string]any{
					"tool_calls": []map[string]any{toolCall},
				}
				if state.first {
					delta["role"] = "assistant"
					state.first = false
				}
				sendChunk(delta, nil, nil)
				state.toolCallIndex++
			}
		}
	}

	result.text = state.collected.String()
	return result
}

func ModelsHandler() http.HandlerFunc {
	type model struct {
		ID      string `json:"id"`
		Object  string `json:"object"`
		OwnedBy string `json:"owned_by"`
	}

	models := make([]model, 0, len(allowedModels))
	for name, provider := range allowedModels {
		models = append(models, model{ID: name, Object: "model", OwnedBy: provider})
	}

	payload, _ := json.Marshal(map[string]any{
		"object": "list",
		"data":   models,
	})

	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write(payload)
	}
}

func ImportHandler(pool *KeyPool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			jsonError(w, 400, "read body: "+err.Error())
			return
		}

		lines := splitLines(string(body))
		added := pool.Import(lines)

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"imported": added,
			"stats":    pool.Stats(),
		})
	}
}

func StatsHandler(pool *KeyPool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(pool.Stats())
	}
}

func jsonError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]any{
		"error": map[string]any{
			"message": msg,
			"type":    "api_error",
		},
	})
}

func splitLines(s string) []string {
	var out []string
	for _, line := range strings.Split(s, "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			out = append(out, line)
		}
	}
	return out
}
