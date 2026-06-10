package main

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

const adminCookieName = "vapi_gateway_admin"

func AdminPageHandler(pool *KeyPool, configStore *ConfigStore, topup *TopupManager) http.HandlerFunc {
	page := `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Vapi Gateway</title>
  <style>
    body{margin:0;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f6f7f9;color:#1f2328}
    header{background:#fff;border-bottom:1px solid #d8dee4;padding:16px 24px}
    main{max-width:1120px;margin:0 auto;padding:24px}
    h1{font-size:22px;margin:0 0 4px}
	    h2{font-size:16px;margin:0 0 12px}
	    .muted{color:#667085;font-size:13px}
	    .grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:18px 0}
	    .two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
	    .metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:12px}
	    .card{background:#fff;border:1px solid #d8dee4;border-radius:8px;padding:14px}
	    .label{font-size:12px;color:#667085;margin-bottom:6px}
	    .value{font-size:28px;font-weight:700;margin-top:8px}
	    .metric{border:1px solid #d8dee4;border-radius:6px;padding:10px;background:#f6f8fa;min-width:0}
	    .metric .value{font-size:20px;margin-top:0}
	    input,select,textarea{width:100%;box-sizing:border-box;border:1px solid #d0d7de;border-radius:6px;padding:9px;font:inherit}
	    textarea{min-height:110px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
	    button{border:0;border-radius:6px;background:#0969da;color:#fff;padding:9px 14px;font-weight:600;cursor:pointer}
	    button.secondary{background:#57606a}
	    .badge{display:inline-flex;align-items:center;border-radius:999px;padding:4px 9px;font-size:13px;font-weight:600;background:#eaeef2;color:#57606a}
	    .badge.ok{background:#dafbe1;color:#116329}
	    .badge.warn{background:#fff8c5;color:#7d4e00}
	    .badge.bad{background:#ffebe9;color:#cf222e}
	    .row{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
	    .form-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
	    pre{background:#fff;border:1px solid #d8dee4;border-radius:8px;padding:12px;overflow:auto;max-width:100%;box-sizing:border-box;white-space:pre-wrap;overflow-wrap:anywhere}
	    #topupLog{max-height:260px}
    .card{min-width:0}
    .two>*{min-width:0}
    @media(max-width:820px){.grid,.form-grid,.two{grid-template-columns:1fr}main{padding:16px}}
  </style>
</head>
<body>
  <header>
    <h1>Vapi Gateway</h1>
    <div class="muted">一次性 key 池、补号配置和导入</div>
  </header>
  <main>
    <section class="grid">
      <div class="card"><div class="label">总计</div><div id="total" class="value">-</div></div>
      <div class="card"><div class="label">可用</div><div id="active" class="value">-</div></div>
      <div class="card"><div class="label">使用中 key</div><div id="inFlight" class="value">-</div></div>
      <div class="card"><div class="label">活跃请求</div><div id="activeRequests" class="value">-</div></div>
      <div class="card"><div class="label">已消费</div><div id="consumed" class="value">-</div></div>
      <div class="card"><div class="label">失败</div><div id="failed" class="value">-</div></div>
    </section>
    <section class="two">
      <div class="card">
        <h2>补号配置</h2>
        <form id="configForm">
          <div class="form-grid">
            <label><div class="label">自动补号</div><select name="autoTopupEnabled"><option value="true">开启</option><option value="false">关闭</option></select></label>
            <label><div class="label">目标池数量</div><input type="number" min="1" name="minAccounts"></label>
            <label><div class="label">低水位</div><input type="number" min="0" name="topupLowWatermark"></label>
            <label><div class="label">补号并发 / solver窗口</div><input type="number" min="1" name="topupConcurrency"></label>
            <label><div class="label">检查间隔 ms</div><input type="number" min="1000" name="topupCheckIntervalMs"></label>
            <label><div class="label">注册后探测</div><select name="requireChatReadyAfterSignup"><option value="false">关闭</option><option value="true">开启</option></select></label>
            <label><div class="label">默认模型</div><input name="defaultModel"></label>
            <label><div class="label">卡号</div><input name="billingCardNumber" autocomplete="off"></label>
            <label><div class="label">有效期</div><input name="billingCardExpiry" autocomplete="off" placeholder="MM / YY"></label>
            <label><div class="label">CVC</div><input name="billingCardCvc" autocomplete="off"></label>
          </div>
          <label><div class="label" style="margin-top:12px">补号命令</div><textarea name="autoTopupCommand"></textarea></label>
          <div class="row"><button type="submit">保存配置</button><span id="configMsg" class="muted"></span></div>
        </form>
      </div>
      <div class="card">
        <h2>补号状态</h2>
	        <div class="row">
	          <button onclick="runTopup()">运行补号</button>
	          <button class="secondary" onclick="stopTopup()">停止补号</button>
	          <span id="topupBadge" class="badge">-</span>
	        </div>
	        <div class="metric-grid">
	          <div class="metric"><div class="label">最近成功率</div><div id="topupSuccessRate" class="value">-</div></div>
	          <div class="metric"><div class="label">平均耗时</div><div id="topupAvgDuration" class="value">-</div></div>
	          <div class="metric"><div class="label">成功 / 失败</div><div id="topupSuccessFail" class="value">-</div></div>
	          <div class="metric"><div class="label">最近任务</div><div id="topupRuns" class="value">-</div></div>
	        </div>
	        <pre id="topupLog">{}</pre>
	      </div>
    </section>
    <section class="card" style="margin-top:14px">
        <h2>导入 private keys</h2>
        <textarea id="keys" spellcheck="false" placeholder="每行一个 Vapi private key"></textarea>
        <div class="row">
          <button onclick="importKeys()">导入</button>
          <button class="secondary" onclick="refresh()">刷新</button>
          <button class="secondary" onclick="logout()">退出登录</button>
          <span id="message" class="muted"></span>
        </div>
    </section>
    <section class="card" style="margin-top:14px">
      <h2>最后错误</h2>
      <pre id="raw">暂无错误</pre>
    </section>
  </main>
	  <script>
	    const dirtyConfigFields = new Set();
	    let refreshInFlight = false;
	    let refreshTimer = null;

    async function api(url, options) {
      const res = await fetch(url, Object.assign({ credentials: 'same-origin' }, options || {}));
      if (res.status === 401) { location.href = '/admin/login'; return; }
      const text = await res.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch { data = { text }; }
      if (!res.ok) throw new Error(data && data.error && data.error.message || text || res.statusText);
      return data;
    }
    function markConfigDirty(event) {
      if (event.target && event.target.name) dirtyConfigFields.add(event.target.name);
    }
    function setConfigField(form, name, value) {
      const field = form.elements[name];
      if (!field) return;
      if (document.activeElement === field || dirtyConfigFields.has(name)) return;
      field.value = value == null ? '' : String(value);
    }
	    function setForm(config) {
	      const form = configForm;
      setConfigField(form, 'autoTopupEnabled', String(config.autoTopupEnabled));
      setConfigField(form, 'minAccounts', config.minAccounts);
      setConfigField(form, 'topupLowWatermark', config.topupLowWatermark);
      setConfigField(form, 'topupConcurrency', config.topupConcurrency);
      setConfigField(form, 'topupCheckIntervalMs', config.topupCheckIntervalMs);
      setConfigField(form, 'requireChatReadyAfterSignup', String(config.requireChatReadyAfterSignup || false));
      setConfigField(form, 'defaultModel', config.defaultModel || 'claude-opus-4-6');
      setConfigField(form, 'billingCardNumber', config.billingCardNumber || '');
      setConfigField(form, 'billingCardExpiry', config.billingCardExpiry || '');
      setConfigField(form, 'billingCardCvc', config.billingCardCvc || '');
	      setConfigField(form, 'autoTopupCommand', config.autoTopupCommand || '');
	    }
	    function formatDuration(ms) {
	      const value = Number(ms || 0);
	      if (!Number.isFinite(value) || value <= 0) return '-';
	      const seconds = value / 1000;
	      if (seconds < 60) return seconds.toFixed(seconds < 10 ? 1 : 0) + 's';
	      const minutes = Math.floor(seconds / 60);
	      const rest = Math.round(seconds % 60);
	      return minutes + 'm ' + rest + 's';
	    }
	    function formatPercent(value) {
	      const number = Number(value);
	      if (!Number.isFinite(number)) return '-';
	      return (number * 100).toFixed(1) + '%';
	    }
	    function topupStateInfo(topup) {
	      if (topup && topup.running) return { text: '运行中 PID ' + topup.pid, cls: 'warn' };
	      if (topup && topup.refilling) return { text: '自动补号中，等待下一轮', cls: 'warn' };
	      if (topup && topup.message === 'completed') return { text: '最近完成', cls: 'ok' };
	      if (topup && topup.message && topup.message !== 'idle') return { text: topup.message, cls: topup.code ? 'bad' : 'ok' };
	      return { text: '空闲', cls: 'ok' };
	    }
	    function updateTopupView(state) {
	      const topup = state.topup || {};
	      const metrics = state.topupMetrics || {};
	      const info = topupStateInfo(topup);
	      topupBadge.textContent = info.text;
	      topupBadge.className = 'badge ' + info.cls;
	      topupSuccessRate.textContent = metrics.attempts ? formatPercent(metrics.successRate) : '-';
	      topupAvgDuration.textContent = formatDuration(metrics.avgDurationMs);
	      topupSuccessFail.textContent = (metrics.success ?? 0) + ' / ' + (metrics.fail ?? 0);
	      topupRuns.textContent = String(metrics.runs ?? 0);
	      const logText = [topup.stderr, topup.stdout].filter(Boolean).join('\n').trim();
	      const summary = {
	        current: {
	          status: info.text,
	          reason: topup.reason || '',
	          message: topup.message || '',
	          startedAt: topup.startedAt || null,
	          finishedAt: topup.finishedAt || null,
	          duration: formatDuration(topup.durationMs),
	          success: topup.success || 0,
	          fail: topup.fail || 0,
	        },
	        recent: metrics.history || [],
	      };
	      topupLog.textContent = logText || JSON.stringify(summary, null, 2);
	    }
	    function updateLastError(state) {
	      const topup = state.topup || {};
	      const metrics = state.topupMetrics || {};
	      const recent = Array.isArray(metrics.history) ? metrics.history.find((item) => item && item.lastError) : null;
	      raw.textContent = topup.lastError || metrics.lastError || (recent && recent.lastError) || '暂无错误';
	    }
	    async function refresh() {
	      if (refreshInFlight) return;
	      refreshInFlight = true;
	      try {
	      const state = await api('/admin/state');
	      total.textContent = state.stats.total;
	      active.textContent = state.stats.active;
      inFlight.textContent = state.stats.in_flight_keys ?? state.stats.in_flight;
      activeRequests.textContent = state.stats.active_requests ?? state.stats.in_flight;
	      consumed.textContent = state.stats.consumed;
	      failed.textContent = state.stats.failed;
	      updateTopupView(state);
	      updateLastError(state);
	      setForm(state.config);
	      message.textContent = '已刷新 ' + new Date().toLocaleTimeString();
	      } catch (error) {
	        message.textContent = '刷新失败: ' + error.message;
	      } finally {
	        refreshInFlight = false;
	      }
	    }
    async function importKeys() {
      const body = keys.value.trim();
      if (!body) return;
      const data = await api('/admin/keys/import', { method: 'POST', body });
      keys.value = '';
      message.textContent = '导入 ' + data.imported + ' 个';
      await refresh();
    }
    configForm.addEventListener('input', markConfigDirty);
    configForm.addEventListener('change', markConfigDirty);
    configForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(configForm).entries());
      values.autoTopupEnabled = values.autoTopupEnabled === 'true';
      values.requireChatReadyAfterSignup = values.requireChatReadyAfterSignup === 'true';
      values.minAccounts = Number(values.minAccounts);
      values.topupLowWatermark = Number(values.topupLowWatermark);
      values.topupConcurrency = Number(values.topupConcurrency);
      values.topupCheckIntervalMs = Number(values.topupCheckIntervalMs);
      await api('/admin/config', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(values) });
      dirtyConfigFields.clear();
      configMsg.textContent = '已保存 ' + new Date().toLocaleTimeString();
      await refresh();
    });
    async function logout() {
      await fetch('/admin/logout', { method: 'POST', credentials: 'same-origin' });
      location.href = '/admin/login';
    }
	    async function runTopup() {
	      const data = await api('/admin/topup/run', { method: 'POST' });
	      topupLog.textContent = JSON.stringify(data.status || data, null, 2);
      await refresh();
    }
    async function stopTopup() {
      const data = await api('/admin/topup/stop', { method: 'POST' });
      topupLog.textContent = JSON.stringify(data.status || data, null, 2);
      await refresh();
	    }
	    refresh();
	    refreshTimer = setInterval(refresh, 2000);
	  </script>
</body>
</html>`

	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(page))
	}
}

func LoginPageHandler() http.HandlerFunc {
	page := `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>登录 - Vapi Gateway</title>
  <style>
    body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f7f9;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1f2328}
    form{width:min(380px,calc(100vw - 32px));background:#fff;border:1px solid #d8dee4;border-radius:8px;padding:22px;box-sizing:border-box}
    h1{font-size:20px;margin:0 0 16px}
    input,button{width:100%;box-sizing:border-box;border-radius:6px;font:inherit}
    input{border:1px solid #d0d7de;padding:10px;margin-bottom:12px}
    button{border:0;background:#0969da;color:#fff;font-weight:600;padding:10px;cursor:pointer}
    .msg{min-height:20px;color:#cf222e;font-size:13px;margin-top:10px}
  </style>
</head>
<body>
  <form id="loginForm">
    <h1>Vapi Gateway 登录</h1>
    <input name="password" type="password" placeholder="管理密码" autofocus>
    <button type="submit">登录</button>
    <div id="msg" class="msg"></div>
  </form>
  <script>
    loginForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      msg.textContent = '';
      const res = await fetch('/admin/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ password: loginForm.password.value })
      });
      if (res.ok) location.href = '/';
      else msg.textContent = '密码错误';
    });
  </script>
</body>
</html>`
	return func(w http.ResponseWriter, r *http.Request) {
		if isAdminAuthenticated(r) {
			http.Redirect(w, r, "/", http.StatusFound)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(page))
	}
}

func LoginPostHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		password := os.Getenv("ADMIN_PASSWORD")
		if password == "" {
			setAdminCookie(w)
			writeJSON(w, map[string]any{"ok": true})
			return
		}
		var body struct {
			Password string `json:"password"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if subtle.ConstantTimeCompare([]byte(body.Password), []byte(password)) != 1 {
			clearAdminCookie(w)
			jsonError(w, http.StatusUnauthorized, "invalid admin password")
			return
		}
		setAdminCookie(w)
		writeJSON(w, map[string]any{"ok": true})
	}
}

func LogoutHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		clearAdminCookie(w)
		writeJSON(w, map[string]any{"ok": true})
	}
}

func AdminStateHandler(pool *KeyPool, configStore *ConfigStore, topup *TopupManager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{
			"stats":        pool.Stats(),
			"config":       configStore.Load(),
			"topup":        topup.Status(),
			"topupMetrics": topup.Metrics(),
		})
	}
}

func TopupRunHandler(topup *TopupManager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		status, err := topup.Run("manual")
		if err != nil {
			writeJSON(w, map[string]any{"ok": false, "message": err.Error(), "status": status})
			return
		}
		writeJSON(w, map[string]any{"ok": true, "status": status})
	}
}

func TopupStopHandler(topup *TopupManager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"ok": true, "status": topup.Stop()})
	}
}

func AdminConfigHandler(configStore *ConfigStore) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var cfg GatewayConfig
		if err := json.NewDecoder(r.Body).Decode(&cfg); err != nil {
			jsonError(w, http.StatusBadRequest, "invalid config json")
			return
		}
		saved, err := configStore.Save(cfg)
		if err != nil {
			jsonError(w, http.StatusInternalServerError, err.Error())
			return
		}
		writeJSON(w, map[string]any{"ok": true, "config": saved})
	}
}

func WithAdminAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !isAdminAuthenticated(r) {
			if strings.HasPrefix(r.URL.Path, "/admin/") {
				jsonError(w, http.StatusUnauthorized, "admin login required")
				return
			}
			http.Redirect(w, r, "/admin/login", http.StatusFound)
			return
		}
		next(w, r)
	}
}

func WithServerAuth(next http.HandlerFunc) http.HandlerFunc {
	key := os.Getenv("SERVER_API_KEY")
	if key == "" {
		return next
	}
	return func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if subtle.ConstantTimeCompare([]byte(token), []byte(key)) != 1 {
			jsonError(w, http.StatusUnauthorized, "invalid api key")
			return
		}
		next(w, r)
	}
}

func HealthHandler(pool *KeyPool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"ok": true, "stats": pool.Stats()})
	}
}

func isAdminAuthenticated(r *http.Request) bool {
	if os.Getenv("ADMIN_PASSWORD") == "" {
		return true
	}
	cookie, err := r.Cookie(adminCookieName)
	if err != nil || cookie.Value == "" {
		return false
	}
	return verifyAdminToken(cookie.Value)
}

func setAdminCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name:     adminCookieName,
		Value:    createAdminToken(),
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   int(adminSessionTTL().Seconds()),
	})
}

func clearAdminCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name:     adminCookieName,
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   -1,
	})
}

func createAdminToken() string {
	exp := time.Now().Add(adminSessionTTL()).UnixMilli()
	payload := strconv.FormatInt(exp, 10)
	sig := signAdminPayload(payload)
	return base64.RawURLEncoding.EncodeToString([]byte(payload)) + "." + sig
}

func verifyAdminToken(token string) bool {
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return false
	}
	payloadBytes, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return false
	}
	payload := string(payloadBytes)
	exp, err := strconv.ParseInt(payload, 10, 64)
	if err != nil || exp <= time.Now().UnixMilli() {
		return false
	}
	expected := signAdminPayload(payload)
	return subtle.ConstantTimeCompare([]byte(expected), []byte(parts[1])) == 1
}

func signAdminPayload(payload string) string {
	secret := os.Getenv("ADMIN_SESSION_SECRET")
	if secret == "" {
		secret = os.Getenv("ADMIN_PASSWORD")
	}
	if secret == "" {
		secret = randomSecret()
	}
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(payload))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func adminSessionTTL() time.Duration {
	ms := envInt("ADMIN_SESSION_TTL_MS", 7*24*60*60*1000)
	return time.Duration(ms) * time.Millisecond
}

func randomSecret() string {
	var b [32]byte
	_, _ = rand.Read(b[:])
	return base64.RawURLEncoding.EncodeToString(b[:])
}

func writeJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(value); err != nil {
		http.Error(w, fmt.Sprintf("encode json: %v", err), http.StatusInternalServerError)
	}
}
