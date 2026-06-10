package main

import "strings"

// 模型白名单：model name → provider
// 带 -bedrock 后缀的走 anthropic-bedrock 渠道，不带的走 anthropic 直连
var allowedModels = map[string]string{
	// Anthropic 直连
	"claude-opus-4-6":            "anthropic",
	"claude-opus-4-20250514":     "anthropic",
	"claude-opus-4-5-20251101":   "anthropic",
	"claude-sonnet-4-6":          "anthropic",
	"claude-sonnet-4-20250514":   "anthropic",
	"claude-sonnet-4-5-20250929": "anthropic",
	"claude-haiku-4-5-20251001":  "anthropic",
	"claude-3-7-sonnet-20250219": "anthropic",
	"claude-3-5-sonnet-20241022": "anthropic",
	"claude-3-5-sonnet-20240620": "anthropic",
	"claude-3-5-haiku-20241022":  "anthropic",
	"claude-3-opus-20240229":     "anthropic",
	"claude-3-sonnet-20240229":   "anthropic",
	"claude-3-haiku-20240307":    "anthropic",
	// Anthropic Bedrock
	"claude-opus-4-6-bedrock":            "anthropic-bedrock",
	"claude-opus-4-20250514-bedrock":     "anthropic-bedrock",
	"claude-opus-4-5-20251101-bedrock":   "anthropic-bedrock",
	"claude-sonnet-4-6-bedrock":          "anthropic-bedrock",
	"claude-sonnet-4-20250514-bedrock":   "anthropic-bedrock",
	"claude-sonnet-4-5-20250929-bedrock": "anthropic-bedrock",
	"claude-haiku-4-5-20251001-bedrock":  "anthropic-bedrock",
	"claude-3-7-sonnet-20250219-bedrock": "anthropic-bedrock",
	"claude-3-5-sonnet-20241022-bedrock": "anthropic-bedrock",
	"claude-3-5-sonnet-20240620-bedrock": "anthropic-bedrock",
	"claude-3-5-haiku-20241022-bedrock":  "anthropic-bedrock",
	"claude-3-opus-20240229-bedrock":     "anthropic-bedrock",
	"claude-3-sonnet-20240229-bedrock":   "anthropic-bedrock",
	"claude-3-haiku-20240307-bedrock":    "anthropic-bedrock",
	// Google
	"gemini-3-flash-preview": "google",
	"gemini-2.5-pro":         "google",
	"gemini-2.5-flash":       "google",
}

// modelProvider 返回 (provider, vapiModelName, ok)
// 带 -bedrock 后缀的会剥离后缀得到真实模型名
func modelProvider(model string) (provider, vapiModel string, ok bool) {
	p, exists := allowedModels[model]
	if !exists {
		return "", "", false
	}
	vapiModel = strings.TrimSuffix(model, "-bedrock")
	return p, vapiModel, true
}
