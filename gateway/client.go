package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const vapiURL = "https://api.vapi.ai/chat/responses"

type VapiClient struct {
	helper      string
	socks5Addr  string
	impersonate string
}

type curlCFFIRequest struct {
	URL     string            `json:"url"`
	Payload any               `json:"payload"`
	Headers map[string]string `json:"headers"`
	Stream  bool              `json:"stream"`
	Timeout float64           `json:"timeout"`
}

type curlCFFIMetadata struct {
	StatusCode int               `json:"status_code"`
	Headers    map[string]string `json:"headers"`
}

type curlCFFIResponseBody struct {
	reader *bufio.Reader
	cmd    *exec.Cmd
	stderr *bytes.Buffer
	once   sync.Once
	done   atomic.Bool
	err    error
}

func NewVapiClient(socks5Addr string) *VapiClient {
	return &VapiClient{
		helper:      envOr("VAPI_CHAT_HELPER", defaultChatHelper()),
		socks5Addr:  socks5Addr,
		impersonate: envOr("CURL_CFFI_IMPERSONATE", "chrome131"),
	}
}

// Chat 调用 Vapi /chat/responses（非流式）
func (c *VapiClient) Chat(ctx context.Context, apiKey string, req *VapiRequest) (*VapiResponse, int, error) {
	resp, err := c.do(ctx, apiKey, req, false)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, resp.StatusCode, fmt.Errorf("vapi %d: %s", resp.StatusCode, string(raw))
	}

	var vr VapiResponse
	if err := json.Unmarshal(raw, &vr); err != nil {
		return nil, resp.StatusCode, fmt.Errorf("decode: %w body=%s", err, string(raw[:min(len(raw), 200)]))
	}
	return &vr, resp.StatusCode, nil
}

// ChatStream 调用 Vapi /chat/responses（流式），返回原始 http.Response
func (c *VapiClient) ChatStream(ctx context.Context, apiKey string, req *VapiRequest) (*http.Response, error) {
	return c.do(ctx, apiKey, req, true)
}

func (c *VapiClient) do(ctx context.Context, apiKey string, payload *VapiRequest, stream bool) (*http.Response, error) {
	headers := map[string]string{
		"Content-Type":  "application/json",
		"Authorization": "Bearer " + apiKey,
	}
	return c.curlCFFI(ctx, curlCFFIRequest{
		URL:     vapiURL,
		Payload: payload,
		Headers: headers,
		Stream:  stream,
		Timeout: (5 * time.Minute).Seconds(),
	})
}

func (c *VapiClient) curlCFFI(ctx context.Context, request curlCFFIRequest) (*http.Response, error) {
	helper := resolveChatHelperPath(c.helper)
	name := helper
	cmdArgs := []string{}
	if strings.HasSuffix(helper, ".py") {
		name = envOr("PYTHON", "python3")
		cmdArgs = []string{helper}
	}

	requestBody, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}

	cmd := exec.CommandContext(ctx, name, cmdArgs...)
	cmd.Env = append(os.Environ(),
		"CURL_CFFI_IMPERSONATE="+c.impersonate,
		"SOCKS5_PROXY="+c.socks5Addr,
	)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start curl_cffi helper %q: %w", helper, err)
	}
	go func() {
		defer stdin.Close()
		_, _ = stdin.Write(requestBody)
	}()

	reader := bufio.NewReader(stdout)
	metaLine, err := reader.ReadBytes('\n')
	if err != nil {
		_ = cmd.Wait()
		return nil, helperError("curl_cffi helper failed before response metadata", err, &stderr)
	}

	var meta curlCFFIMetadata
	if err := json.Unmarshal(bytes.TrimSpace(metaLine), &meta); err != nil {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		_ = cmd.Wait()
		return nil, fmt.Errorf("curl_cffi helper returned invalid metadata: %w", err)
	}
	if meta.StatusCode == 0 {
		meta.StatusCode = http.StatusBadGateway
	}

	return &http.Response{
		StatusCode: meta.StatusCode,
		Status:     fmt.Sprintf("%d %s", meta.StatusCode, http.StatusText(meta.StatusCode)),
		Header:     curlCFFIHTTPHeader(meta.Headers),
		Body:       &curlCFFIResponseBody{reader: reader, cmd: cmd, stderr: &stderr},
	}, nil
}

func (b *curlCFFIResponseBody) Read(p []byte) (int, error) {
	n, err := b.reader.Read(p)
	if errors.Is(err, io.EOF) {
		b.done.Store(true)
	}
	return n, err
}

func (b *curlCFFIResponseBody) Close() error {
	b.once.Do(func() {
		if !b.done.Load() && b.cmd.Process != nil && b.cmd.ProcessState == nil {
			_ = b.cmd.Process.Kill()
		}
		err := b.cmd.Wait()
		if errors.Is(err, os.ErrProcessDone) {
			err = nil
		}
		if err != nil {
			b.err = helperError("curl_cffi helper exited", err, b.stderr)
		}
	})
	return b.err
}

func resolveChatHelperPath(configured string) string {
	candidates := []string{configured}
	if configured == "" || configured == "/app/scripts/vapi_chat.py" {
		candidates = append(candidates,
			"./scripts/vapi_chat.py",
			"../scripts/vapi_chat.py",
		)
	}
	for _, candidate := range candidates {
		if candidate == "" {
			continue
		}
		if filepath.IsAbs(candidate) {
			if _, err := os.Stat(candidate); err == nil {
				return candidate
			}
			continue
		}
		if abs, err := filepath.Abs(candidate); err == nil {
			if _, statErr := os.Stat(abs); statErr == nil {
				return abs
			}
		}
	}
	return configured
}

func defaultChatHelper() string {
	if _, err := os.Stat("/app/scripts/vapi_chat.py"); err == nil {
		return "/app/scripts/vapi_chat.py"
	}
	return "./scripts/vapi_chat.py"
}

func curlCFFIHTTPHeader(headers map[string]string) http.Header {
	out := http.Header{}
	for key, value := range headers {
		switch strings.ToLower(key) {
		case "content-length", "content-encoding", "transfer-encoding", "connection":
			continue
		default:
			out.Set(key, value)
		}
	}
	return out
}

func helperError(prefix string, err error, stderr *bytes.Buffer) error {
	msg := strings.TrimSpace(stderr.String())
	if msg != "" {
		return fmt.Errorf("%s: %w: %s", prefix, err, msg)
	}
	return fmt.Errorf("%s: %w", prefix, err)
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

var _ io.ReadCloser = (*curlCFFIResponseBody)(nil)
