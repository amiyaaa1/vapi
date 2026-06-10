package main

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestConvertRequestUsesStringInputForTextUserMessage(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "user", Content: json.RawMessage(`"hello"`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	var input string
	if err := json.Unmarshal(got.Input, &input); err != nil {
		t.Fatalf("input should be a JSON string, got %s: %v", got.Input, err)
	}
	if input != "hello" {
		t.Fatalf("input = %q, want hello", input)
	}
}

func TestConvertRequestUsesStringInputForTextContentArray(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "system", Content: json.RawMessage(`"system prompt"`)},
			{Role: "user", Content: json.RawMessage(`[{"type":"text","text":"hello"}]`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	var input string
	if err := json.Unmarshal(got.Input, &input); err != nil {
		t.Fatalf("input should be a JSON string, got %s: %v", got.Input, err)
	}
	if input != "hello" {
		t.Fatalf("input = %q, want hello", input)
	}
}

func TestConvertRequestOmitsImageOnlyContentArrayInput(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "system", Content: json.RawMessage(`"system prompt"`)},
			{Role: "user", Content: json.RawMessage(`[{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}}]`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	var input string
	if err := json.Unmarshal(got.Input, &input); err != nil {
		t.Fatalf("input should be a JSON string, got %s: %v", got.Input, err)
	}
	if input != omittedNonTextContentPlaceholder {
		t.Fatalf("input = %q, want omitted placeholder", input)
	}
	if strings.Contains(input, "data:image") || strings.Contains(input, "AAAA") {
		t.Fatalf("input should not contain image payload: %q", input)
	}
}

func TestConvertRequestKeepsTextAndOmitsImageContentArrayInput(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "user", Content: json.RawMessage(`[{"type":"text","text":"describe this"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}}]`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	var input string
	if err := json.Unmarshal(got.Input, &input); err != nil {
		t.Fatalf("input should be a JSON string, got %s: %v", got.Input, err)
	}
	if !strings.Contains(input, "describe this") || !strings.Contains(input, omittedNonTextContentPlaceholder) {
		t.Fatalf("input = %q, want text plus omitted placeholder", input)
	}
	if strings.Contains(input, "data:image") || strings.Contains(input, "AAAA") {
		t.Fatalf("input should not contain image payload: %q", input)
	}
}

func TestConvertRequestNormalizesTextContentArrayHistory(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "system", Content: json.RawMessage(`[{"type":"text","text":"system prompt"}]`)},
			{Role: "user", Content: json.RawMessage(`[{"type":"text","text":"history"}]`)},
			{Role: "assistant", Content: json.RawMessage(`"noted"`)},
			{Role: "user", Content: json.RawMessage(`"hello"`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	if len(got.Assistant.Model.Messages) != 3 {
		t.Fatalf("history length = %d, want 3", len(got.Assistant.Model.Messages))
	}

	for i, msg := range got.Assistant.Model.Messages {
		var text string
		if err := json.Unmarshal(msg.Content, &text); err != nil {
			t.Fatalf("history[%d] content should be a string, got %s: %v", i, msg.Content, err)
		}
	}
}

func TestConvertRequestMergesLeadingSystemMessages(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "system", Content: json.RawMessage(`"system one"`)},
			{Role: "system", Content: json.RawMessage(`"system two"`)},
			{Role: "user", Content: json.RawMessage(`"history"`)},
			{Role: "assistant", Content: json.RawMessage(`"noted"`)},
			{Role: "user", Content: json.RawMessage(`"hello"`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	if len(got.Assistant.Model.Messages) != 3 {
		t.Fatalf("history length = %d, want merged system + two history messages", len(got.Assistant.Model.Messages))
	}
	if got.Assistant.Model.Messages[0].Role != "system" {
		t.Fatalf("history[0] role = %q, want system", got.Assistant.Model.Messages[0].Role)
	}
	var systemText string
	if err := json.Unmarshal(got.Assistant.Model.Messages[0].Content, &systemText); err != nil {
		t.Fatalf("merged system content should be string: %v", err)
	}
	if !strings.Contains(systemText, "system one") || !strings.Contains(systemText, "system two") {
		t.Fatalf("merged system text = %q, want both system messages", systemText)
	}
	for i, msg := range got.Assistant.Model.Messages[1:] {
		if msg.Role == "system" {
			t.Fatalf("history[%d] unexpected extra system message: %+v", i+1, msg)
		}
	}
}

func TestConvertRequestConvertsNonLeadingSystemMessagesToUser(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "system", Content: json.RawMessage(`"root system"`)},
			{Role: "user", Content: json.RawMessage(`"history"`)},
			{Role: "system", Content: json.RawMessage(`"late system"`)},
			{Role: "assistant", Content: json.RawMessage(`"noted"`)},
			{Role: "user", Content: json.RawMessage(`"hello"`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	systemCount := 0
	foundConverted := false
	for _, msg := range got.Assistant.Model.Messages {
		if msg.Role == "system" {
			systemCount++
			continue
		}
		text := textFromContent(msg.Content)
		if msg.Role == "user" && strings.HasPrefix(text, "### SYSTEM\n") && strings.Contains(text, "late system") {
			foundConverted = true
		}
	}
	if systemCount != 1 {
		t.Fatalf("system message count = %d, want exactly one leading system", systemCount)
	}
	if !foundConverted {
		t.Fatalf("late system message should be converted to marked user history: %+v", got.Assistant.Model.Messages)
	}
}

func TestConvertRequestConvertsAllSystemMessagesWhenNoLeadingSystem(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "user", Content: json.RawMessage(`"history"`)},
			{Role: "system", Content: json.RawMessage(`"middle system"`)},
			{Role: "assistant", Content: json.RawMessage(`"noted"`)},
			{Role: "user", Content: json.RawMessage(`"hello"`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	foundConverted := false
	for _, msg := range got.Assistant.Model.Messages {
		if msg.Role == "system" {
			t.Fatalf("no leading system should mean no system messages upstream: %+v", got.Assistant.Model.Messages)
		}
		text := textFromContent(msg.Content)
		if msg.Role == "user" && strings.HasPrefix(text, "### SYSTEM\n") && strings.Contains(text, "middle system") {
			foundConverted = true
		}
	}
	if !foundConverted {
		t.Fatalf("middle system message should be converted to marked user history: %+v", got.Assistant.Model.Messages)
	}
}

func TestConvertRequestProjectsMessageNameIntoHistoryAndInputContent(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "user", Name: "Milkyway", Content: json.RawMessage(`"给你10000baka币让我抱一天"`)},
			{Role: "assistant", Content: json.RawMessage(`"成交！"`)},
			{Role: "user", Name: "Milkyway", Content: json.RawMessage(`"过来抱"`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	if len(got.Assistant.Model.Messages) != 2 {
		t.Fatalf("history length = %d, want 2", len(got.Assistant.Model.Messages))
	}
	if got.Assistant.Model.Messages[0].Role != "user" {
		t.Fatalf("history[0] role = %q, want user", got.Assistant.Model.Messages[0].Role)
	}
	var historyText string
	if err := json.Unmarshal(got.Assistant.Model.Messages[0].Content, &historyText); err != nil {
		t.Fatalf("history[0] content should be string: %v", err)
	}
	if !strings.Contains(historyText, "speaker: Milkyway") || !strings.Contains(historyText, "10000baka") {
		t.Fatalf("history text = %q, want speaker and original content", historyText)
	}

	var input []VapiMessage
	if err := json.Unmarshal(got.Input, &input); err != nil {
		t.Fatalf("input should keep message form when name is present, got %s: %v", got.Input, err)
	}
	if len(input) != 1 {
		t.Fatalf("input length = %d, want 1", len(input))
	}
	if input[0].Role != "user" {
		t.Fatalf("input[0] role = %q, want user", input[0].Role)
	}
	var inputText string
	if err := json.Unmarshal(input[0].Content, &inputText); err != nil {
		t.Fatalf("input[0] content should be string: %v", err)
	}
	if !strings.Contains(inputText, "speaker: Milkyway") || !strings.Contains(inputText, "过来抱") {
		t.Fatalf("input text = %q, want speaker and original content", inputText)
	}
}

func TestConvertRequestSplitsLongToolInputContent(t *testing.T) {
	toolCalls := []json.RawMessage{
		json.RawMessage(`{"id":"call_1","type":"function","function":{"name":"lookup","arguments":"{}"}}`),
	}
	toolText := strings.Repeat("T", vapiInputContentCharLimit+1000)
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "user", Content: json.RawMessage(`"find data"`)},
			{Role: "assistant", Content: json.RawMessage(`""`), ToolCalls: toolCalls},
			{Role: "tool", ToolCallID: "call_1"},
			{Role: "user", Content: json.RawMessage(`"summarize"`)},
		},
	}
	req.Messages[2].Content, _ = json.Marshal(toolText)

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	var input []OAIMessage
	if err := json.Unmarshal(got.Input, &input); err != nil {
		t.Fatalf("input should be a message array, got %s: %v", got.Input, err)
	}

	toolParts := 0
	continuedParts := 0
	combined := ""
	for _, msg := range input {
		var text string
		if err := json.Unmarshal(msg.Content, &text); err != nil {
			t.Fatalf("input content should be string: %v", err)
		}
		if len([]rune(text)) > vapiInputContentCharLimit {
			t.Fatalf("input content length = %d, want <= %d", len([]rune(text)), vapiInputContentCharLimit)
		}

		if msg.Role == "tool" {
			toolParts++
			if msg.ToolCallID != "call_1" {
				t.Fatalf("tool message tool_call_id = %q, want call_1", msg.ToolCallID)
			}
			combined += text
			continue
		}
		if strings.HasPrefix(text, "[continued tool result: id=call_1") {
			continuedParts++
			bodyStart := strings.Index(text, "\n")
			if bodyStart < 0 {
				t.Fatalf("continued tool result should include header newline: %q", text)
			}
			combined += text[bodyStart+1:]
		}
	}
	if toolParts != 1 || continuedParts == 0 {
		t.Fatalf("tool parts = %d continued parts = %d, want one tool plus continuations; input=%+v", toolParts, continuedParts, input)
	}
	if combined != toolText {
		t.Fatalf("combined tool text length = %d, want original length %d", len([]rune(combined)), len([]rune(toolText)))
	}
	if strings.Contains(combined, "truncated by gateway") {
		t.Fatalf("tool content should be split, not truncated")
	}
}

func TestConvertRequestKeepsAllStructuredToolResultsByDefault(t *testing.T) {
	toolCalls := []json.RawMessage{}
	messages := []OAIMessage{
		{Role: "user", Content: json.RawMessage(`"find data"`)},
	}
	for i := 1; i <= 5; i++ {
		toolCalls = append(toolCalls, json.RawMessage(fmt.Sprintf(`{"id":"call_%d","type":"function","function":{"name":"lookup","arguments":"{\"n\":%d}"}}`, i, i)))
	}
	messages = append(messages, OAIMessage{Role: "assistant", Content: json.RawMessage(`""`), ToolCalls: toolCalls})
	for i := 1; i <= 5; i++ {
		messages = append(messages, OAIMessage{
			Role:       "tool",
			Name:       "lookup",
			ToolCallID: fmt.Sprintf("call_%d", i),
			Content:    json.RawMessage(fmt.Sprintf(`"result %d"`, i)),
		})
	}
	messages = append(messages, OAIMessage{Role: "user", Content: json.RawMessage(`"summarize"`)})

	req := &OAIRequest{
		Model:    "claude-opus-4-6",
		Messages: messages,
	}
	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	var input []OAIMessage
	if err := json.Unmarshal(got.Input, &input); err != nil {
		t.Fatalf("input should be a message array, got %s: %v", got.Input, err)
	}
	if len(input) != 7 {
		t.Fatalf("input length = %d, want assistant + all tool results + user; input=%+v", len(input), input)
	}
	if len(input[0].ToolCalls) != 5 {
		t.Fatalf("structured tool calls = %d, want 5", len(input[0].ToolCalls))
	}
	if firstID := toolCallID(input[0].ToolCalls[0]); firstID != "call_1" {
		t.Fatalf("first kept tool call = %q, want call_1", firstID)
	}

	toolCount := 0
	for _, msg := range input {
		if msg.Role == "tool" {
			toolCount++
		}
	}
	if toolCount != 5 {
		t.Fatalf("structured tool results = %d, want 5", toolCount)
	}

	for _, msg := range got.Assistant.Model.Messages {
		if msg.Role == "tool" || len(msg.ToolCalls) > 0 {
			t.Fatalf("history should not contain structured tool fields: %+v", msg)
		}
		if strings.Contains(textFromContent(msg.Content), "result 1") {
			t.Fatalf("default full input should not format completed tool result into history: %+v", got.Assistant.Model.Messages)
		}
	}
}

func TestConvertRequestLimitsStructuredToolResultsWithEnv(t *testing.T) {
	t.Setenv(toolInputMaxResultsEnv, "2")

	toolCalls := []json.RawMessage{}
	messages := []OAIMessage{
		{Role: "user", Content: json.RawMessage(`"find data"`)},
	}
	for i := 1; i <= 5; i++ {
		toolCalls = append(toolCalls, json.RawMessage(fmt.Sprintf(`{"id":"call_%d","type":"function","function":{"name":"lookup","arguments":"{\"n\":%d}"}}`, i, i)))
	}
	messages = append(messages, OAIMessage{Role: "assistant", Content: json.RawMessage(`""`), ToolCalls: toolCalls})
	for i := 1; i <= 5; i++ {
		messages = append(messages, OAIMessage{
			Role:       "tool",
			Name:       "lookup",
			ToolCallID: fmt.Sprintf("call_%d", i),
			Content:    json.RawMessage(fmt.Sprintf(`"result %d"`, i)),
		})
	}
	messages = append(messages, OAIMessage{Role: "user", Content: json.RawMessage(`"summarize"`)})

	req := &OAIRequest{
		Model:    "claude-opus-4-6",
		Messages: messages,
	}
	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	var input []OAIMessage
	if err := json.Unmarshal(got.Input, &input); err != nil {
		t.Fatalf("input should be a message array, got %s: %v", got.Input, err)
	}
	if len(input) != 4 {
		t.Fatalf("input length = %d, want assistant + 2 latest tool results + user; input=%+v", len(input), input)
	}
	if len(input[0].ToolCalls) != 2 {
		t.Fatalf("structured tool calls = %d, want 2", len(input[0].ToolCalls))
	}
	if firstID := toolCallID(input[0].ToolCalls[0]); firstID != "call_4" {
		t.Fatalf("first kept tool call = %q, want call_4", firstID)
	}
	if secondID := toolCallID(input[0].ToolCalls[1]); secondID != "call_5" {
		t.Fatalf("second kept tool call = %q, want call_5", secondID)
	}

	toolCount := 0
	for _, msg := range input {
		if msg.Role == "tool" {
			toolCount++
			if msg.ToolCallID != "call_4" && msg.ToolCallID != "call_5" {
				t.Fatalf("only latest two tool results should be structured input, got %q", msg.ToolCallID)
			}
		}
	}
	if toolCount != 2 {
		t.Fatalf("structured tool results = %d, want 2", toolCount)
	}

	foundOldToolHistory := false
	for _, msg := range got.Assistant.Model.Messages {
		if msg.Role == "tool" || len(msg.ToolCalls) > 0 {
			t.Fatalf("history should not contain structured tool fields: %+v", msg)
		}
		text := textFromContent(msg.Content)
		if strings.Contains(text, "call_1") && strings.Contains(text, "result 1") {
			foundOldToolHistory = true
		}
	}
	if !foundOldToolHistory {
		t.Fatalf("old tool call/result should be preserved as text history: %+v", got.Assistant.Model.Messages)
	}
}

func TestToolCallSummarySplitsShellCommandSemantically(t *testing.T) {
	raw := json.RawMessage(`{"id":"call_1","type":"function","function":{"name":"exec","arguments":"{\"command\":\"curl -sL https://example.com/secret | head -c 6000 2>&1\",\"timeout\":20}"}}`)

	got := toolCallSummary(raw)
	for _, want := range []string{
		"name=exec",
		"program:curl",
		"https://example.com/secret",
		"pipe_to:{program:head,args:[-c,6000]}",
		"stderr:merge",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("summary missing %q: %q", want, got)
		}
	}
	if strings.Contains(got, " | ") || strings.Contains(got, "2>&1") {
		t.Fatalf("summary leaked raw shell command: %q", got)
	}
}

func TestConvertRequestSanitizesLatestStructuredShellToolCall(t *testing.T) {
	rawToolCall := json.RawMessage(`{"id":"call_1","type":"function","index":7,"extra":{"kept":true},"function":{"name":"exec","arguments":"{\"command\":\"curl -sL https://example.com/secret | head -c 6000 2>&1\",\"timeout\":20}"}}`)
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "user", Content: json.RawMessage(`"check site"`)},
			{Role: "assistant", Content: json.RawMessage(`""`), ToolCalls: []json.RawMessage{rawToolCall}},
			{Role: "tool", ToolCallID: "call_1", Content: json.RawMessage(`"ok"`)},
			{Role: "user", Content: json.RawMessage(`"summarize"`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}

	var input []VapiMessage
	if err := json.Unmarshal(got.Input, &input); err != nil {
		t.Fatalf("input should be a message array, got %s: %v", got.Input, err)
	}
	if len(input) != 3 || len(input[0].ToolCalls) != 1 {
		t.Fatalf("input should keep latest assistant/tool/user chain, got %+v", input)
	}

	var call map[string]any
	if err := json.Unmarshal(input[0].ToolCalls[0], &call); err != nil {
		t.Fatalf("tool call should be valid JSON: %v", err)
	}
	if call["id"] != "call_1" || call["type"] != "function" || call["index"].(float64) != 7 {
		t.Fatalf("tool call identity fields were not preserved: %+v", call)
	}
	if extra, ok := call["extra"].(map[string]any); !ok || extra["kept"] != true {
		t.Fatalf("tool call extra fields were not preserved: %+v", call)
	}

	function := call["function"].(map[string]any)
	argsRaw := function["arguments"].(string)
	if strings.Contains(argsRaw, `"command"`) || strings.Contains(argsRaw, " | ") || strings.Contains(argsRaw, "2>&1") {
		t.Fatalf("sanitized arguments leaked raw shell command: %s", argsRaw)
	}

	var args map[string]any
	if err := json.Unmarshal([]byte(argsRaw), &args); err != nil {
		t.Fatalf("sanitized arguments should be JSON: %v", err)
	}
	if _, ok := args["command"]; ok {
		t.Fatalf("sanitized arguments should remove raw command: %+v", args)
	}
	if args["timeout"].(float64) != 20 {
		t.Fatalf("non-command argument should be preserved: %+v", args)
	}

	semantic := args["command_semantic"].(map[string]any)
	if semantic["program"] != "curl" || semantic["stderr"] != "merge" {
		t.Fatalf("unexpected command semantic root: %+v", semantic)
	}
	pipe := semantic["pipe_to"].(map[string]any)
	if pipe["program"] != "head" {
		t.Fatalf("unexpected pipe target: %+v", semantic)
	}
}

func TestConvertRequestClampsSmallMaxTokens(t *testing.T) {
	maxTokens := 32
	req := &OAIRequest{
		Model:     "claude-opus-4-6",
		MaxTokens: &maxTokens,
		Messages: []OAIMessage{
			{Role: "user", Content: json.RawMessage(`"hello"`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}
	if got.Assistant.Model.MaxTokens != 50 {
		t.Fatalf("maxTokens = %d, want 50", got.Assistant.Model.MaxTokens)
	}
}

func TestConvertRequestClampsLargeMaxTokens(t *testing.T) {
	maxTokens := 20000
	req := &OAIRequest{
		Model:     "claude-opus-4-6",
		MaxTokens: &maxTokens,
		Messages: []OAIMessage{
			{Role: "user", Content: json.RawMessage(`"hello"`)},
		},
	}

	got, err := convertRequest(req)
	if err != nil {
		t.Fatal(err)
	}
	if got.Assistant.Model.MaxTokens != 10000 {
		t.Fatalf("maxTokens = %d, want 10000", got.Assistant.Model.MaxTokens)
	}
}

func TestConvertResponseIncludesEstimatedUsage(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "user", Content: json.RawMessage(`"hello"`)},
		},
	}
	vapiResp := &VapiResponse{
		ID: "resp_1",
		Output: []VapiOutputItem{
			{
				Type: "message",
				Role: "assistant",
				Content: []VapiTextContent{
					{Type: "output_text", Text: "hello back"},
				},
			},
		},
	}

	got := convertResponse(vapiResp, req)
	if got.Usage == nil {
		t.Fatal("usage should be populated")
	}
	if got.Usage.PromptTokens <= 0 {
		t.Fatalf("prompt tokens = %d, want > 0", got.Usage.PromptTokens)
	}
	if got.Usage.CompletionTokens <= 0 {
		t.Fatalf("completion tokens = %d, want > 0", got.Usage.CompletionTokens)
	}
	if got.Usage.TotalTokens != got.Usage.PromptTokens+got.Usage.CompletionTokens {
		t.Fatalf("total tokens = %d, want prompt+completion", got.Usage.TotalTokens)
	}
}

func TestConvertResponsePrefersVapiUsage(t *testing.T) {
	req := &OAIRequest{
		Model: "claude-opus-4-6",
		Messages: []OAIMessage{
			{Role: "user", Content: json.RawMessage(`"hello"`)},
		},
	}
	vapiResp := &VapiResponse{
		ID:    "resp_1",
		Usage: &VapiUsage{InputTokens: 11, OutputTokens: 7, TotalTokens: 18},
		Output: []VapiOutputItem{
			{
				Type: "message",
				Role: "assistant",
				Content: []VapiTextContent{
					{Type: "output_text", Text: "hello back"},
				},
			},
		},
	}

	got := convertResponse(vapiResp, req)
	if got.Usage == nil {
		t.Fatal("usage should be populated")
	}
	if got.Usage.PromptTokens != 11 || got.Usage.CompletionTokens != 7 || got.Usage.TotalTokens != 18 {
		t.Fatalf("usage = %+v, want 11/7/18", got.Usage)
	}
}
