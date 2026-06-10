package main

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"unicode"
)

const vapiInputContentCharLimit = 9500
const toolInputMaxResultsEnv = "TOOL_INPUT_MAX_RESULTS"
const omittedNonTextContentPlaceholder = "[non-text content omitted by gateway]"

// --- OpenAI Chat Completions 请求/响应结构 ---

type OAIRequest struct {
	Model       string            `json:"model"`
	Messages    []OAIMessage      `json:"messages"`
	Tools       []json.RawMessage `json:"tools,omitempty"`
	Stream      bool              `json:"stream"`
	MaxTokens   *int              `json:"max_tokens,omitempty"`
	Temperature *float64          `json:"temperature,omitempty"`
}

type OAIMessage struct {
	Role       string            `json:"role"`
	Name       string            `json:"name,omitempty"`
	Content    json.RawMessage   `json:"content,omitempty"`
	ToolCalls  []json.RawMessage `json:"tool_calls,omitempty"`
	ToolCallID string            `json:"tool_call_id,omitempty"`
}

type OAIResponse struct {
	ID      string      `json:"id"`
	Object  string      `json:"object"`
	Created int64       `json:"created"`
	Model   string      `json:"model"`
	Choices []OAIChoice `json:"choices"`
	Usage   *OAIUsage   `json:"usage,omitempty"`
}

type OAIChoice struct {
	Index        int        `json:"index"`
	Message      OAIMessage `json:"message"`
	FinishReason string     `json:"finish_reason"`
}

type OAIUsage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

// --- Vapi /chat/responses 请求/响应结构 ---

type VapiRequest struct {
	Input     json.RawMessage `json:"input"`
	Assistant VapiAssistant   `json:"assistant"`
	Stream    bool            `json:"stream"`
}

type VapiAssistant struct {
	Model VapiModel `json:"model"`
}

type VapiModel struct {
	Provider    string            `json:"provider"`
	Model       string            `json:"model"`
	Messages    []VapiMessage     `json:"messages,omitempty"`
	Tools       []json.RawMessage `json:"tools,omitempty"`
	MaxTokens   int               `json:"maxTokens,omitempty"`
	Temperature *float64          `json:"temperature,omitempty"`
}

type VapiMessage struct {
	Role       string            `json:"role"`
	Content    json.RawMessage   `json:"content,omitempty"`
	ToolCalls  []json.RawMessage `json:"tool_calls,omitempty"`
	ToolCallID string            `json:"tool_call_id,omitempty"`
}

// Vapi /chat/responses 非流式响应
type VapiResponse struct {
	ID     string           `json:"id"`
	Object string           `json:"object"`
	Status string           `json:"status"`
	Output []VapiOutputItem `json:"output"`
	Usage  *VapiUsage       `json:"usage,omitempty"`
	Error  string           `json:"error,omitempty"`
}

type VapiUsage struct {
	InputTokens      int `json:"input_tokens,omitempty"`
	OutputTokens     int `json:"output_tokens,omitempty"`
	TotalTokens      int `json:"total_tokens,omitempty"`
	PromptTokens     int `json:"prompt_tokens,omitempty"`
	CompletionTokens int `json:"completion_tokens,omitempty"`
}

type VapiOutputItem struct {
	Type   string `json:"type"` // "message" 或 "function_call"
	ID     string `json:"id"`
	Status string `json:"status"`

	// type=message 时
	Role    string            `json:"role,omitempty"`
	Content []VapiTextContent `json:"content,omitempty"`

	// type=function_call 时
	CallID    string `json:"call_id,omitempty"`
	Name      string `json:"name,omitempty"`
	Arguments string `json:"arguments,omitempty"`
}

type VapiTextContent struct {
	Type string `json:"type"` // "output_text"
	Text string `json:"text"`
}

// --- 转换函数 ---

// isToolRelated 判断消息是否与 tool call 相关
func isToolRelated(msg *OAIMessage) bool {
	return msg.Role == "tool" || len(msg.ToolCalls) > 0
}

func rawContentText(content json.RawMessage) (string, bool) {
	var s string
	if err := json.Unmarshal(content, &s); err == nil {
		return s, true
	}

	var parts []json.RawMessage
	if err := json.Unmarshal(content, &parts); err == nil {
		if len(parts) == 0 {
			return "", false
		}

		out := ""
		omittedNonText := false
		for _, rawPart := range parts {
			var partString string
			if err := json.Unmarshal(rawPart, &partString); err == nil {
				out += partString
				continue
			}

			var part struct {
				Type string `json:"type"`
				Text string `json:"text"`
			}
			if err := json.Unmarshal(rawPart, &part); err != nil {
				return "", false
			}
			switch part.Type {
			case "", "text", "input_text", "output_text":
				if part.Text == "" {
					continue
				}
				out += part.Text
			default:
				omittedNonText = true
			}
		}
		if out == "" && omittedNonText {
			return omittedNonTextContentPlaceholder, true
		}
		if omittedNonText {
			out += "\n" + omittedNonTextContentPlaceholder
		}
		return out, true
	}

	var obj struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(content, &obj); err == nil && obj.Text != "" {
		switch obj.Type {
		case "", "text", "input_text", "output_text":
			return obj.Text, true
		}
	}

	return "", false
}

func normalizeTextContentMessages(messages []OAIMessage) []OAIMessage {
	out := make([]OAIMessage, len(messages))
	for i, msg := range messages {
		out[i] = msg
		if text, ok := rawContentText(msg.Content); ok {
			if raw, err := json.Marshal(text); err == nil {
				out[i].Content = json.RawMessage(raw)
			}
		}
	}
	return out
}

func contentForVapiMessage(msg OAIMessage) json.RawMessage {
	if text, ok := rawContentText(msg.Content); ok {
		if msg.Name != "" {
			text = fmt.Sprintf("[speaker: %s]\n%s", msg.Name, text)
		}
		if raw, err := json.Marshal(text); err == nil {
			return json.RawMessage(raw)
		}
	}
	return msg.Content
}

func textForVapiMessage(msg OAIMessage) string {
	return textFromContent(contentForVapiMessage(msg))
}

func mergeSystemMessages(messages []OAIMessage) OAIMessage {
	parts := make([]string, 0, len(messages))
	for _, msg := range messages {
		if text := strings.TrimSpace(textForVapiMessage(msg)); text != "" {
			parts = append(parts, text)
		}
	}
	return OAIMessage{
		Role:    "system",
		Content: jsonStringRaw(strings.Join(parts, "\n\n")),
	}
}

func systemMessageAsUser(msg OAIMessage) OAIMessage {
	text := textForVapiMessage(msg)
	if strings.TrimSpace(text) == "" {
		text = "[empty system message]"
	}
	return OAIMessage{
		Role:    "user",
		Content: jsonStringRaw("### SYSTEM\n" + text),
	}
}

func normalizeSystemMessagesForVapi(messages []OAIMessage) ([]OAIMessage, []OAIMessage) {
	index := 0
	leadingSystem := []OAIMessage{}
	for index < len(messages) && messages[index].Role == "system" {
		leadingSystem = append(leadingSystem, messages[index])
		index++
	}

	systemMsgs := []OAIMessage{}
	if len(leadingSystem) > 0 {
		systemMsgs = append(systemMsgs, mergeSystemMessages(leadingSystem))
	}

	normalized := make([]OAIMessage, 0, len(messages)-index)
	for ; index < len(messages); index++ {
		msg := messages[index]
		if msg.Role == "system" {
			normalized = append(normalized, systemMessageAsUser(msg))
			continue
		}
		normalized = append(normalized, msg)
	}

	return systemMsgs, normalized
}

func toVapiMessage(msg OAIMessage, content json.RawMessage) VapiMessage {
	return VapiMessage{
		Role:       msg.Role,
		Content:    content,
		ToolCalls:  sanitizeToolCallsForVapi(msg.ToolCalls),
		ToolCallID: msg.ToolCallID,
	}
}

func toVapiMessages(messages []OAIMessage) []VapiMessage {
	out := make([]VapiMessage, 0, len(messages))
	for _, msg := range messages {
		out = append(out, toVapiMessage(msg, contentForVapiMessage(msg)))
	}
	return out
}

func jsonStringRaw(text string) json.RawMessage {
	raw, _ := json.Marshal(text)
	return json.RawMessage(raw)
}

func splitTextForInputLimit(text string) []string {
	runes := []rune(text)
	if len(runes) <= vapiInputContentCharLimit {
		return []string{text}
	}

	chunks := make([]string, 0, len(runes)/vapiInputContentCharLimit+1)
	for start := 0; start < len(runes); start += vapiInputContentCharLimit {
		end := start + vapiInputContentCharLimit
		if end > len(runes) {
			end = len(runes)
		}
		chunks = append(chunks, string(runes[start:end]))
	}
	return chunks
}

func truncateTextForInputLimit(text string) string {
	runes := []rune(text)
	if len(runes) <= vapiInputContentCharLimit {
		return text
	}
	marker := "\n[truncated by gateway: tool output exceeded Vapi input content limit]"
	markerRunes := []rune(marker)
	keep := vapiInputContentCharLimit - len(markerRunes)
	if keep < 0 {
		keep = vapiInputContentCharLimit
		markerRunes = nil
	}
	return string(runes[:keep]) + string(markerRunes)
}

func splitInputMessagesByContentLimit(messages []OAIMessage) []VapiMessage {
	out := make([]VapiMessage, 0, len(messages))

	for _, msg := range messages {
		content := contentForVapiMessage(msg)
		text, ok := rawContentText(content)
		if !ok {
			out = append(out, toVapiMessage(msg, content))
			continue
		}

		if msg.Role == "tool" {
			out = append(out, splitToolInputMessage(msg, text)...)
			continue
		}

		chunks := splitTextForInputLimit(text)
		if len(chunks) == 1 {
			out = append(out, toVapiMessage(msg, content))
			continue
		}

		for index, chunk := range chunks {
			next := toVapiMessage(msg, content)
			raw, err := json.Marshal(chunk)
			if err == nil {
				next.Content = json.RawMessage(raw)
			}
			if index > 0 {
				next.ToolCalls = nil
			}
			out = append(out, next)
		}
	}

	return out
}

func splitToolInputMessage(msg OAIMessage, text string) []VapiMessage {
	runes := []rune(text)
	if len(runes) <= vapiInputContentCharLimit {
		return []VapiMessage{toVapiMessage(msg, jsonStringRaw(text))}
	}

	out := []VapiMessage{toVapiMessage(msg, jsonStringRaw(string(runes[:vapiInputContentCharLimit])))}
	remaining := runes[vapiInputContentCharLimit:]
	for part := 2; len(remaining) > 0; part++ {
		header := "[continued tool result"
		if msg.ToolCallID != "" {
			header += ": id=" + msg.ToolCallID
		}
		header += fmt.Sprintf(" part=%d]\n", part)
		budget := vapiInputContentCharLimit - len([]rune(header))
		if budget < 1 {
			budget = vapiInputContentCharLimit
			header = ""
		}
		take := budget
		if take > len(remaining) {
			take = len(remaining)
		}
		out = append(out, VapiMessage{
			Role:    "user",
			Content: jsonStringRaw(header + string(remaining[:take])),
		})
		remaining = remaining[take:]
	}
	return out
}

func toolCallID(raw json.RawMessage) string {
	var call struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(raw, &call); err != nil {
		return ""
	}
	return call.ID
}

func sanitizeToolCallsForVapi(calls []json.RawMessage) []json.RawMessage {
	if len(calls) == 0 {
		return calls
	}
	out := make([]json.RawMessage, 0, len(calls))
	for _, call := range calls {
		out = append(out, sanitizeToolCallForVapi(call))
	}
	return out
}

func sanitizeToolCallForVapi(raw json.RawMessage) json.RawMessage {
	var call map[string]any
	if err := json.Unmarshal(raw, &call); err != nil {
		return raw
	}
	function, ok := call["function"].(map[string]any)
	if !ok {
		return raw
	}
	arguments, ok := function["arguments"].(string)
	if !ok || arguments == "" {
		return raw
	}
	sanitized, ok := sanitizeToolArguments(arguments)
	if !ok {
		return raw
	}
	function["arguments"] = sanitized
	if out, err := json.Marshal(call); err == nil {
		return json.RawMessage(out)
	}
	return raw
}

func sanitizeToolArguments(arguments string) (string, bool) {
	var obj map[string]any
	if err := json.Unmarshal([]byte(arguments), &obj); err != nil {
		return "", false
	}
	command, ok := obj["command"].(string)
	if !ok || command == "" {
		return "", false
	}
	delete(obj, "command")
	obj["command_semantic"] = shellCommandSemantic(command)
	raw, err := json.Marshal(obj)
	if err != nil {
		return "", false
	}
	return string(raw), true
}

func toolCallSummary(raw json.RawMessage) string {
	var call struct {
		ID       string `json:"id"`
		Type     string `json:"type"`
		Function struct {
			Name      string `json:"name"`
			Arguments string `json:"arguments"`
		} `json:"function"`
	}
	if err := json.Unmarshal(raw, &call); err != nil {
		return strings.TrimSpace(string(raw))
	}

	parts := []string{}
	if call.Function.Name != "" {
		parts = append(parts, "name="+call.Function.Name)
	}
	if call.ID != "" {
		parts = append(parts, "id="+call.ID)
	}
	if call.Function.Arguments != "" {
		parts = append(parts, "arguments="+toolArgumentsShape(call.Function.Arguments))
	}
	if len(parts) == 0 {
		return strings.TrimSpace(string(raw))
	}
	return strings.Join(parts, " ")
}

func toolArgumentsShape(arguments string) string {
	var value any
	if err := json.Unmarshal([]byte(arguments), &value); err != nil {
		return fmt.Sprintf("raw_string_chars=%d", len([]rune(arguments)))
	}
	return jsonValueShape("", value)
}

func jsonValueShape(key string, value any) string {
	switch v := value.(type) {
	case map[string]any:
		keys := make([]string, 0, len(v))
		for key := range v {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, key := range keys {
			parts = append(parts, fmt.Sprintf("%s:%s", key, jsonValueShape(key, v[key])))
		}
		return "{" + strings.Join(parts, ",") + "}"
	case []any:
		return fmt.Sprintf("array(len=%d)", len(v))
	case string:
		if key == "command" {
			return shellCommandShape(v)
		}
		return fmt.Sprintf("string(chars=%d)", len([]rune(v)))
	case float64:
		return "number"
	case bool:
		return "bool"
	case nil:
		return "null"
	default:
		return fmt.Sprintf("%T", value)
	}
}

func shellCommandShape(command string) string {
	tokens := shellTokens(command)
	if len(tokens) == 0 {
		return fmt.Sprintf("{program:unknown,chars:%d}", len([]rune(command)))
	}
	return fmt.Sprintf("{%s,chars:%d}", shellTokensShape(tokens), len([]rune(command)))
}

func shellCommandSemantic(command string) map[string]any {
	tokens := shellTokens(command)
	if len(tokens) == 0 {
		return map[string]any{
			"program": "unknown",
			"chars":   len([]rune(command)),
		}
	}
	out := shellTokensSemantic(tokens)
	out["chars"] = len([]rune(command))
	return out
}

func shellTokens(command string) []string {
	tokens := []string{}
	var current strings.Builder
	quote := rune(0)
	escaped := false

	flush := func() {
		if current.Len() == 0 {
			return
		}
		tokens = append(tokens, current.String())
		current.Reset()
	}

	for _, r := range command {
		if escaped {
			current.WriteRune(r)
			escaped = false
			continue
		}
		if r == '\\' {
			escaped = true
			continue
		}
		if quote != 0 {
			if r == quote {
				quote = 0
				continue
			}
			current.WriteRune(r)
			continue
		}
		switch r {
		case '\'', '"':
			quote = r
		case ' ', '\t', '\n', '\r':
			flush()
		case '|', ';':
			flush()
			tokens = append(tokens, string(r))
		case '&':
			if current.String() == "&" {
				current.Reset()
				tokens = append(tokens, "&&")
			} else {
				flush()
				current.WriteRune(r)
			}
		default:
			current.WriteRune(r)
		}
	}
	if escaped {
		current.WriteRune('\\')
	}
	flush()
	return mergeShellControlTokens(tokens)
}

func mergeShellControlTokens(tokens []string) []string {
	out := []string{}
	for i := 0; i < len(tokens); i++ {
		if i+2 < len(tokens) && tokens[i] == "2" && tokens[i+1] == ">" && tokens[i+2] == "&1" {
			out = append(out, "2>&1")
			i += 2
			continue
		}
		if i+1 < len(tokens) && tokens[i] == "2>" && tokens[i+1] == "&1" {
			out = append(out, "2>&1")
			i++
			continue
		}
		if i+1 < len(tokens) && tokens[i] == "&" && tokens[i+1] == "&" {
			out = append(out, "&&")
			i++
			continue
		}
		out = append(out, tokens[i])
	}
	return out
}

func shellTokensShape(tokens []string) string {
	steps := []string{}
	operators := []string{}
	current := []string{}
	stderrMerge := false

	flush := func() {
		if len(current) == 0 {
			return
		}
		steps = append(steps, shellStepShape(current))
		current = nil
	}

	for _, token := range tokens {
		switch token {
		case "2>&1":
			stderrMerge = true
		case "|":
			flush()
			operators = append(operators, "pipe_to")
		case "&&":
			flush()
			operators = append(operators, "then_if_success")
		case "||":
			flush()
			operators = append(operators, "then_if_failure")
		case ";":
			flush()
			operators = append(operators, "then")
		default:
			current = append(current, token)
		}
	}
	flush()

	if len(steps) == 0 {
		return "program:unknown"
	}

	parts := []string{steps[0]}
	if stderrMerge {
		parts = append(parts, "stderr:merge")
	}
	for i := 1; i < len(steps); i++ {
		op := "then"
		if i-1 < len(operators) {
			op = operators[i-1]
		}
		parts = append(parts, op+":{"+steps[i]+"}")
	}
	return strings.Join(parts, ",")
}

func shellTokensSemantic(tokens []string) map[string]any {
	steps := [][]string{}
	operators := []string{}
	current := []string{}
	stderrMerge := false

	flush := func() {
		if len(current) == 0 {
			return
		}
		steps = append(steps, current)
		current = nil
	}

	for _, token := range tokens {
		switch token {
		case "2>&1":
			stderrMerge = true
		case "|":
			flush()
			operators = append(operators, "pipe_to")
		case "&&":
			flush()
			operators = append(operators, "then_if_success")
		case "||":
			flush()
			operators = append(operators, "then_if_failure")
		case ";":
			flush()
			operators = append(operators, "then")
		default:
			current = append(current, token)
		}
	}
	flush()

	if len(steps) == 0 {
		return map[string]any{"program": "unknown"}
	}

	root := shellStepSemantic(steps[0])
	if stderrMerge {
		root["stderr"] = "merge"
	}
	currentMap := root
	for i := 1; i < len(steps); i++ {
		op := "then"
		if i-1 < len(operators) {
			op = operators[i-1]
		}
		next := shellStepSemantic(steps[i])
		currentMap[op] = next
		currentMap = next
	}
	return root
}

func shellStepShape(tokens []string) string {
	if len(tokens) == 0 {
		return "program:unknown"
	}
	program := safeShellToken(tokens[0])
	args := []string{}
	for _, token := range tokens[1:] {
		args = append(args, safeShellToken(token))
	}
	if len(args) == 0 {
		return "program:" + program
	}
	return fmt.Sprintf("program:%s,args:[%s]", program, strings.Join(args, ","))
}

func shellStepSemantic(tokens []string) map[string]any {
	if len(tokens) == 0 {
		return map[string]any{"program": "unknown"}
	}
	out := map[string]any{"program": safeShellToken(tokens[0])}
	if len(tokens) > 1 {
		args := []string{}
		for _, token := range tokens[1:] {
			args = append(args, safeShellToken(token))
		}
		out["args"] = args
	}
	return out
}

func safeShellToken(token string) string {
	token = strings.ReplaceAll(token, "\n", "\\n")
	token = strings.ReplaceAll(token, "\r", "\\r")
	runes := []rune(token)
	if len(runes) > 200 {
		token = string(runes[:200]) + "...[truncated]"
	}
	return token
}

func textFromContent(raw json.RawMessage) string {
	if text, ok := rawContentText(raw); ok {
		return text
	}
	return strings.TrimSpace(string(raw))
}

func formattedAssistantToolCallsMessage(msg OAIMessage, calls []json.RawMessage) OAIMessage {
	lines := []string{}
	if text := textFromContent(msg.Content); text != "" {
		lines = append(lines, "[assistant message]\n"+text)
	}
	lines = append(lines, "[assistant tool calls preserved as history]")
	for i, call := range calls {
		lines = append(lines, fmt.Sprintf("%d. %s", i+1, toolCallSummary(call)))
	}
	return OAIMessage{
		Role:    "assistant",
		Content: jsonStringRaw(strings.Join(lines, "\n")),
	}
}

func formattedToolResultMessage(msg OAIMessage) OAIMessage {
	label := msg.Name
	if label == "" {
		label = "tool"
	}
	header := fmt.Sprintf("[tool result preserved as history: name=%s", label)
	if msg.ToolCallID != "" {
		header += " id=" + msg.ToolCallID
	}
	header += "]"
	return OAIMessage{
		Role:    "user",
		Content: jsonStringRaw(header + "\n" + textFromContent(msg.Content)),
	}
}

func appendHistoryMessage(out []OAIMessage, msg OAIMessage) []OAIMessage {
	if len(msg.ToolCalls) > 0 {
		return append(out, formattedAssistantToolCallsMessage(msg, msg.ToolCalls))
	}
	if msg.Role == "tool" {
		return append(out, formattedToolResultMessage(msg))
	}
	return append(out, msg)
}

func appendHistoryMessages(out []OAIMessage, messages []OAIMessage) []OAIMessage {
	for _, msg := range messages {
		out = appendHistoryMessage(out, msg)
	}
	return out
}

func toolCallIDSet(calls []json.RawMessage) map[string]bool {
	out := map[string]bool{}
	for _, call := range calls {
		if id := toolCallID(call); id != "" {
			out[id] = true
		}
	}
	return out
}

func filterToolCalls(calls []json.RawMessage, keep map[string]bool, wantKept bool) []json.RawMessage {
	out := []json.RawMessage{}
	for _, call := range calls {
		id := toolCallID(call)
		if id == "" {
			if !wantKept {
				out = append(out, call)
			}
			continue
		}
		if keep[id] == wantKept {
			out = append(out, call)
		}
	}
	return out
}

func mergeToolMessageContent(left OAIMessage, right OAIMessage) OAIMessage {
	leftText := textFromContent(left.Content)
	rightText := textFromContent(right.Content)
	if leftText == "" {
		left.Content = jsonStringRaw(rightText)
		return left
	}
	if rightText == "" {
		return left
	}
	left.Content = jsonStringRaw(leftText + "\n" + rightText)
	return left
}

func appendStructuredInputMessage(out []OAIMessage, toolIndexes map[string]int, msg OAIMessage) ([]OAIMessage, map[string]int) {
	if msg.Role != "tool" || msg.ToolCallID == "" {
		return append(out, msg), toolIndexes
	}
	if toolIndexes == nil {
		toolIndexes = map[string]int{}
	}
	if index, ok := toolIndexes[msg.ToolCallID]; ok {
		out[index] = mergeToolMessageContent(out[index], msg)
		return out, toolIndexes
	}
	toolIndexes[msg.ToolCallID] = len(out)
	return append(out, msg), toolIndexes
}

func structuredToolInputResultLimit() int {
	limit := envInt(toolInputMaxResultsEnv, 0)
	if limit < 0 {
		return 0
	}
	return limit
}

func completedStructuredToolIDs(messages []OAIMessage) []string {
	knownCalls := map[string]bool{}
	completed := []string{}
	seenResults := map[string]bool{}

	for _, msg := range messages {
		for _, call := range msg.ToolCalls {
			if id := toolCallID(call); id != "" {
				knownCalls[id] = true
			}
		}

		if msg.Role != "tool" || msg.ToolCallID == "" {
			continue
		}
		if !knownCalls[msg.ToolCallID] || seenResults[msg.ToolCallID] {
			continue
		}
		seenResults[msg.ToolCallID] = true
		completed = append(completed, msg.ToolCallID)
	}

	return completed
}

func structuredToolIDsForInput(messages []OAIMessage, maxResults int) map[string]bool {
	completed := completedStructuredToolIDs(messages)
	keep := map[string]bool{}
	if len(completed) == 0 {
		return keep
	}

	start := 0
	if maxResults > 0 && maxResults < len(completed) {
		start = len(completed) - maxResults
	}
	for _, id := range completed[start:] {
		keep[id] = true
	}
	return keep
}

func firstStructuredInputIndex(messages []OAIMessage, keepIDs map[string]bool) int {
	for index, msg := range messages {
		if len(filterToolCalls(msg.ToolCalls, keepIDs, true)) > 0 {
			return index
		}
	}
	return -1
}

func splitToolMessagesForVapi(messages []OAIMessage) ([]OAIMessage, []OAIMessage) {
	keepIDs := structuredToolIDsForInput(messages, structuredToolInputResultLimit())
	inputStart := firstStructuredInputIndex(messages, keepIDs)
	if inputStart < 0 {
		if len(messages) == 1 {
			return appendHistoryMessages(nil, messages), nil
		}
		last := messages[len(messages)-1]
		if isToolRelated(&last) {
			return appendHistoryMessages(nil, messages), nil
		}
		return appendHistoryMessages(nil, messages[:len(messages)-1]), messages[len(messages)-1:]
	}

	history := appendHistoryMessages(nil, messages[:inputStart])
	input := []OAIMessage{}
	var toolIndexes map[string]int

	for _, msg := range messages[inputStart:] {
		if len(msg.ToolCalls) > 0 {
			keptCalls := filterToolCalls(msg.ToolCalls, keepIDs, true)
			oldCalls := filterToolCalls(msg.ToolCalls, keepIDs, false)
			if len(oldCalls) > 0 {
				history = append(history, formattedAssistantToolCallsMessage(msg, oldCalls))
			}
			if len(keptCalls) == 0 {
				continue
			}
			inputMsg := msg
			inputMsg.ToolCalls = keptCalls
			input, toolIndexes = appendStructuredInputMessage(input, toolIndexes, inputMsg)
			continue
		}
		if msg.Role == "tool" {
			if keepIDs[msg.ToolCallID] {
				input, toolIndexes = appendStructuredInputMessage(input, toolIndexes, msg)
			} else {
				history = append(history, formattedToolResultMessage(msg))
			}
			continue
		}
		input, toolIndexes = appendStructuredInputMessage(input, toolIndexes, msg)
	}

	return history, input
}

func firstPositive(values ...int) int {
	for _, value := range values {
		if value > 0 {
			return value
		}
	}
	return 0
}

func convertUsage(usage *VapiUsage) *OAIUsage {
	if usage == nil {
		return nil
	}

	promptTokens := firstPositive(usage.PromptTokens, usage.InputTokens)
	completionTokens := firstPositive(usage.CompletionTokens, usage.OutputTokens)
	totalTokens := usage.TotalTokens
	if totalTokens <= 0 {
		totalTokens = promptTokens + completionTokens
	}
	if promptTokens == 0 && completionTokens == 0 && totalTokens == 0 {
		return nil
	}

	return &OAIUsage{
		PromptTokens:     promptTokens,
		CompletionTokens: completionTokens,
		TotalTokens:      totalTokens,
	}
}

func usageFromVapiResponse(raw json.RawMessage) *OAIUsage {
	if len(raw) == 0 {
		return nil
	}

	var response struct {
		Usage *VapiUsage `json:"usage,omitempty"`
	}
	if err := json.Unmarshal(raw, &response); err != nil {
		return nil
	}
	return convertUsage(response.Usage)
}

func estimateTokensFromText(text string) int {
	if text == "" {
		return 0
	}

	asciiNonSpace := 0
	nonASCII := 0
	for _, r := range text {
		if unicode.IsSpace(r) {
			continue
		}
		if r < 128 {
			asciiNonSpace++
		} else {
			nonASCII++
		}
	}

	tokens := nonASCII + (asciiNonSpace+3)/4
	if tokens == 0 {
		return 1
	}
	return tokens
}

func estimatePromptTokens(req *OAIRequest) int {
	if req == nil {
		return 0
	}
	payload := struct {
		Messages []OAIMessage      `json:"messages"`
		Tools    []json.RawMessage `json:"tools,omitempty"`
	}{
		Messages: req.Messages,
		Tools:    req.Tools,
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return 0
	}
	return estimateTokensFromText(string(raw))
}

func estimateUsage(req *OAIRequest, outputText string, toolCalls []json.RawMessage) *OAIUsage {
	promptTokens := estimatePromptTokens(req)
	completionTokens := estimateTokensFromText(outputText)
	for _, toolCall := range toolCalls {
		completionTokens += estimateTokensFromText(string(toolCall))
	}

	totalTokens := promptTokens + completionTokens
	if totalTokens == 0 {
		return nil
	}
	return &OAIUsage{
		PromptTokens:     promptTokens,
		CompletionTokens: completionTokens,
		TotalTokens:      totalTokens,
	}
}

func addUsage(left *OAIUsage, right *OAIUsage) *OAIUsage {
	if left == nil {
		return right
	}
	if right == nil {
		return left
	}
	return &OAIUsage{
		PromptTokens:     left.PromptTokens + right.PromptTokens,
		CompletionTokens: left.CompletionTokens + right.CompletionTokens,
		TotalTokens:      left.TotalTokens + right.TotalTokens,
	}
}

func clampVapiMaxTokens(maxTokens *int) int {
	maxT := 10000
	if maxTokens != nil && *maxTokens > 0 {
		maxT = *maxTokens
	}
	if maxT > 10000 {
		maxT = 10000
	}
	if maxT < 50 {
		maxT = 50
	}
	return maxT
}

func convertRequest(req *OAIRequest) (*VapiRequest, error) {
	if len(req.Messages) == 0 {
		return nil, fmt.Errorf("messages is required")
	}

	provider, vapiModel, ok := modelProvider(req.Model)
	if !ok {
		return nil, fmt.Errorf("unsupported model: %s", req.Model)
	}

	// 消息分流策略（Vapi /chat/responses 的限制）：
	//   input[]：每条 content <= 10000 字符，但支持 tool_calls / role:tool
	//   messages[]：几乎无长度限制，但不支持 tool_calls（会超时/丢弃）
	//
	// 所以：
	//   - 开头连续 system → 合并成唯一一条 system 放入 messages
	//   - 非开头 system → 转为带标记的 user 消息，保持原相对顺序
	//   - 普通 user/assistant 历史 → messages（无长度限制，安全放长文本）
	//   - 完整 tool call/result → input（支持 tool 格式）
	//   - TOOL_INPUT_MAX_RESULTS > 0 时，只保留最近 N 个完整 tool result；
	//     更早 tool call/result → 普通文本历史 messages
	//   - input 内每条 content 拆到 Vapi 单条 10000 字符限制以内
	//   - 如果没有 tool 相关消息，最后一条 user → input

	systemMsgs, nonSystem := normalizeSystemMessagesForVapi(req.Messages)
	if len(nonSystem) == 0 {
		return nil, fmt.Errorf("at least one non-system message is required")
	}

	// 找 tool 区域起始位置
	toolStart := -1
	for i := range nonSystem {
		if isToolRelated(&nonSystem[i]) {
			toolStart = i
			break
		}
	}

	var historyMsgs []OAIMessage
	var inputMsgs []OAIMessage
	if toolStart >= 0 {
		historyMsgs, inputMsgs = splitToolMessagesForVapi(nonSystem)
	} else {
		// 无 tool 消息：最后一条放 input，其余放 messages
		historyMsgs = nonSystem[:len(nonSystem)-1]
		inputMsgs = nonSystem[len(nonSystem)-1:]
	}
	if len(inputMsgs) == 0 {
		inputMsgs = []OAIMessage{{Role: "user", Content: jsonStringRaw("")}}
	}

	// 构建 input
	var inputRaw json.RawMessage
	if len(inputMsgs) == 1 && !isToolRelated(&inputMsgs[0]) && inputMsgs[0].Role == "user" && inputMsgs[0].Name == "" {
		// 单条纯文本 user 消息：content 直接作为 string input（兼容性最好）。
		// OpenAI-compatible clients often send text as content parts; Vapi rejects
		// input message-array content in cases where the same text string is accepted.
		// If a message name is present, keep message-array form so the speaker
		// identity is not lost in multi-user chat integrations.
		if text, ok := rawContentText(inputMsgs[0].Content); ok {
			b, err := json.Marshal(text)
			if err != nil {
				return nil, fmt.Errorf("marshal input: %w", err)
			}
			inputRaw = b
		} else {
			b, err := json.Marshal(splitInputMessagesByContentLimit(inputMsgs))
			if err != nil {
				return nil, fmt.Errorf("marshal input: %w", err)
			}
			inputRaw = b
		}
	} else {
		// 多条或含 tool：用 messages 数组格式
		b, err := json.Marshal(splitInputMessagesByContentLimit(inputMsgs))
		if err != nil {
			return nil, fmt.Errorf("marshal input: %w", err)
		}
		inputRaw = b
	}

	vr := &VapiRequest{
		Input:  inputRaw,
		Stream: req.Stream,
		Assistant: VapiAssistant{
			Model: VapiModel{
				Provider: provider,
				Model:    vapiModel,
			},
		},
	}

	// system + 普通历史 → messages
	allMsgs := append(systemMsgs, historyMsgs...)
	if len(allMsgs) > 0 {
		vr.Assistant.Model.Messages = toVapiMessages(allMsgs)
	}

	// tools
	if len(req.Tools) > 0 {
		vr.Assistant.Model.Tools = req.Tools
	}

	// maxTokens
	vr.Assistant.Model.MaxTokens = clampVapiMaxTokens(req.MaxTokens)

	// temperature
	if req.Temperature != nil {
		vr.Assistant.Model.Temperature = req.Temperature
	}

	return vr, nil
}

// buildContinueRequest 构造续写请求：把已生成文本作为 assistant 历史，input 提示续写
func buildContinueRequest(origReq *OAIRequest, collectedText string, round int) (*VapiRequest, error) {
	provider, vapiModel, ok := modelProvider(origReq.Model)
	if !ok {
		return nil, fmt.Errorf("unsupported model: %s", origReq.Model)
	}

	// 原始 messages 归一化后作为历史，避免续写请求传多条 system。
	systemMsgs, normalizedMsgs := normalizeSystemMessagesForVapi(origReq.Messages)
	historyInput := append(systemMsgs, normalizedMsgs...)
	history := toVapiMessages(historyInput)

	assistantContent, _ := json.Marshal(collectedText)
	history = append(history, VapiMessage{
		Role:    "assistant",
		Content: json.RawMessage(assistantContent),
	})

	// 续写指令
	continuePrompt := fmt.Sprintf(
		"[系统续写指令-第%d次] 你之前的回答被系统在120秒时截断了。"+
			"请从断点处无缝继续，不要重复已写内容，不要加过渡语，直接续写。", round)
	inputRaw, _ := json.Marshal(continuePrompt)

	vr := &VapiRequest{
		Input:  inputRaw,
		Stream: true,
		Assistant: VapiAssistant{
			Model: VapiModel{
				Provider: provider,
				Model:    vapiModel,
				Messages: history,
			},
		},
	}

	if len(origReq.Tools) > 0 {
		vr.Assistant.Model.Tools = origReq.Tools
	}

	vr.Assistant.Model.MaxTokens = clampVapiMaxTokens(origReq.MaxTokens)

	if origReq.Temperature != nil {
		vr.Assistant.Model.Temperature = origReq.Temperature
	}

	return vr, nil
}

// convertResponse 将 Vapi /chat/responses 非流式响应转为 OpenAI Chat Completions 格式
func convertResponse(vr *VapiResponse, req *OAIRequest) *OAIResponse {
	resp := &OAIResponse{
		ID:      "vapi-" + vr.ID,
		Object:  "chat.completion",
		Model:   req.Model,
		Choices: []OAIChoice{},
	}

	// 从 output 中提取：
	// - function_call → tool_calls
	// - 最后一条 message → content
	var toolCalls []json.RawMessage
	var lastText string

	for _, item := range vr.Output {
		switch item.Type {
		case "function_call":
			tc := map[string]any{
				"id":   item.CallID,
				"type": "function",
				"function": map[string]string{
					"name":      item.Name,
					"arguments": item.Arguments,
				},
			}
			tcBytes, _ := json.Marshal(tc)
			toolCalls = append(toolCalls, tcBytes)
		case "message":
			if item.Role == "assistant" && len(item.Content) > 0 {
				lastText = item.Content[0].Text
			}
		}
	}

	if len(toolCalls) > 0 {
		// tool call 响应：只返回第一条（tool_calls），忽略 Vapi 自动执行后的续写
		msg := OAIMessage{
			Role:      "assistant",
			ToolCalls: toolCalls,
		}
		resp.Choices = append(resp.Choices, OAIChoice{
			Index:        0,
			Message:      msg,
			FinishReason: "tool_calls",
		})
	} else if lastText != "" {
		contentBytes, _ := json.Marshal(lastText)
		msg := OAIMessage{
			Role:    "assistant",
			Content: json.RawMessage(contentBytes),
		}
		resp.Choices = append(resp.Choices, OAIChoice{
			Index:        0,
			Message:      msg,
			FinishReason: "stop",
		})
	} else {
		resp.Choices = append(resp.Choices, OAIChoice{
			Index:        0,
			Message:      OAIMessage{Role: "assistant", Content: json.RawMessage(`""`)},
			FinishReason: "stop",
		})
	}

	resp.Usage = convertUsage(vr.Usage)
	if resp.Usage == nil {
		resp.Usage = estimateUsage(req, lastText, toolCalls)
	}

	return resp
}
