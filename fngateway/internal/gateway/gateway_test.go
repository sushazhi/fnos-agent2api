package gateway

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// 前缀与改写规则
// ---------------------------------------------------------------------------

func TestPrefixStrip(t *testing.T) {
	p := NewPrefix("/app/agent2api")
	cases := []struct {
		in     string
		want   string
		wantOK bool
	}{
		{"/app/agent2api", "/", true},
		{"/app/agent2api/", "/", true},
		{"/app/agent2api/api/session", "/api/session", true},
		{"/app/agent2api/islands/ui.js", "/islands/ui.js", true},
		{"/app/agent2apix/api", "/app/agent2apix/api", false},
		{"/other", "/other", false},
		// 幂等：被重复加前缀时一路剥到底，不能只剥一层留下 /app/agent2api/api。
		{"/app/agent2api/app/agent2api/api/session", "/api/session", true},
		{"/app/agent2api/app/agent2api", "/", true},
		{"/app/agent2api/app/agent2api/", "/", true},
	}
	for _, c := range cases {
		got, ok := p.Strip(c.in)
		if got != c.want || ok != c.wantOK {
			t.Errorf("Strip(%q) = (%q,%v), want (%q,%v)", c.in, got, ok, c.want, c.wantOK)
		}
	}
}

func TestNewPrefixDefault(t *testing.T) {
	if got := NewPrefix("").Path; got != "/app/agent2api" {
		t.Fatalf("NewPrefix(\"\") = %q", got)
	}
	if got := NewPrefix("/app/x/").Path; got != "/app/x" {
		t.Fatalf("NewPrefix 未去掉尾斜杠: %q", got)
	}
}

// 文档 URL 不带尾斜杠时（桌面入口 url = /app/agent2api），裸相对引用会被浏览器
// 解析到上一级，模型图标（assets/providers/*.png）全部 404。
// 注入 <base href="{前缀}/"> 把文档基址钉回前缀之下。
func TestBaseTag(t *testing.T) {
	p := NewPrefix("/app/agent2api")
	if got, want := p.BaseTag(), `<base href="/app/agent2api/">`; got != want {
		t.Fatalf("BaseTag() = %q, want %q", got, want)
	}
	// 前缀本身已去掉尾斜杠，这里再补一个，正好一个且只有一个。
	if strings.Contains(p.BaseTag(), `//"`) {
		t.Fatalf("BaseTag 出现双斜杠: %s", p.BaseTag())
	}
	// 直通模式（无前缀）不注入。
	if got := (Prefix{}).BaseTag(); got != "" {
		t.Fatalf("空前缀应返回空串，实际 %q", got)
	}
}

// agent2api 面板的资源引用是相对路径（<script src="islands/ui.js">），
// 文档 URL 可能是 /app/agent2api（不带尾斜杠），必须改写成绝对前缀路径，
// 否则浏览器会解析到上一级 /app/islands/ui.js。
func TestRewriteHTML(t *testing.T) {
	p := NewPrefix("/app/agent2api")
	in := `<head></head><body>` +
		`<script src="islands/ui.js"></script>` +
		`<link href="./css/tokens.css" rel="stylesheet">` +
		`<img src="/app/agent2api/keep.png">` +
		`<a href="//cdn.example.com/x">c</a>` +
		`<a href="https://other.example.com/y">d</a>` +
		`</body>`
	got := string(p.RewriteHTML([]byte(in)))

	wants := []string{
		`src="/app/agent2api/islands/ui.js"`,
		`href="/app/agent2api/css/tokens.css"`,
		`src="/app/agent2api/keep.png"`, // 已带前缀，原样保留
		`href="//cdn.example.com/x"`,    // 协议相对，不动
		`href="https://other.example.com/y"`,
	}
	for _, w := range wants {
		if !strings.Contains(got, w) {
			t.Errorf("改写结果缺少 %q\n实际: %s", w, got)
		}
	}
	if strings.Contains(got, "/app/agent2api/app/agent2api") {
		t.Errorf("出现重复前缀: %s", got)
	}
}

func TestRewriteHTMLManifestCrossorigin(t *testing.T) {
	p := NewPrefix("/app/agent2api")
	got := string(p.RewriteHTML([]byte(`<link rel="manifest" href="site.webmanifest">`)))
	if !strings.Contains(got, `crossorigin="use-credentials"`) {
		t.Errorf("manifest 链接未补 crossorigin: %s", got)
	}
	// 已有 crossorigin 的不重复加。
	got = string(p.RewriteHTML([]byte(`<link rel="manifest" href="m.webmanifest" crossorigin="anonymous">`)))
	if strings.Count(got, "crossorigin") != 1 {
		t.Errorf("crossorigin 被重复添加: %s", got)
	}
}

func TestRewriteLocation(t *testing.T) {
	p := NewPrefix("/app/agent2api")
	cases := map[string]string{
		"/login":                  "/app/agent2api/login",
		"/app/agent2api/login":    "/app/agent2api/login",
		"//evil.example.com/x":    "//evil.example.com/x",
		"https://a.example.com/b": "https://a.example.com/b",
		"relative/path":           "relative/path",
		"":                        "",
	}
	for in, want := range cases {
		if got := p.RewriteLocation(in); got != want {
			t.Errorf("RewriteLocation(%q) = %q, want %q", in, got, want)
		}
	}
}

// 面板 Cookie 的 Path 必须收窄到前缀下，否则浏览器把它当成整站 Cookie，
// 既越界又可能在别的应用下互相覆盖。
func TestRewriteCookie(t *testing.T) {
	p := NewPrefix("/app/agent2api")
	cases := map[string]string{
		"agent2api-panel=abc; Path=/; HttpOnly":             "agent2api-panel=abc; Path=/app/agent2api/; HttpOnly",
		"agent2api-panel-rt=abc; Path=/api/panel; HttpOnly": "agent2api-panel-rt=abc; Path=/app/agent2api/api/panel; HttpOnly",
		"a=b; path=/; SameSite=Lax":                         "a=b; Path=/app/agent2api/; SameSite=Lax",
		"a=b; Path=/app/agent2api/x; HttpOnly":              "a=b; Path=/app/agent2api/x; HttpOnly",
		"a=b; Path=/app/agent2api; HttpOnly":                "a=b; Path=/app/agent2api; HttpOnly",
		"a=b; HttpOnly":                                     "a=b; HttpOnly",
	}
	for in, want := range cases {
		if got := p.RewriteCookie(in); got != want {
			t.Errorf("RewriteCookie(%q) = %q, want %q", in, got, want)
		}
	}
}

// 种子脚本必须先于服务端注入的 Web 壳：它得排在 <head> 之后第一个位置。
func TestInjectHeadOrder(t *testing.T) {
	shim := `<script>var apiKey=localStorage.getItem('agent2api.webKey')||'';</script>`
	in := `<html><head>` + shim + `</head><body></body></html>`
	got := string(InjectHead([]byte(in), `<script data-fn-gateway-seed="1">S</script>`))

	seedAt := strings.Index(got, "data-fn-gateway-seed")
	shimAt := strings.Index(got, "apiKey=localStorage")
	if seedAt < 0 || shimAt < 0 {
		t.Fatalf("注入失败: %s", got)
	}
	if seedAt > shimAt {
		t.Fatalf("种子脚本必须排在 Web 壳之前（seed@%d shim@%d）: %s", seedAt, shimAt, got)
	}
}

func TestInjectHeadNoHead(t *testing.T) {
	got := string(InjectHead([]byte(`<body>x</body>`), `S`))
	if !strings.HasPrefix(got, "S") {
		t.Fatalf("无 <head> 时应前置注入: %s", got)
	}
}

// ---------------------------------------------------------------------------
// 注入脚本
// ---------------------------------------------------------------------------

func TestSeedScript(t *testing.T) {
	s := SeedScript()
	// 键名必须与上游 web_shim.rs 的 KEY_STORAGE 完全一致，否则面板读不到。
	if !strings.Contains(s, `"agent2api.webKey"`) {
		t.Errorf("SeedScript 未使用 agent2api.webKey: %s", s)
	}
	if !strings.Contains(s, `data-fn-gateway-seed="1"`) {
		t.Errorf("SeedScript 缺少标记属性: %s", s)
	}
	// 必须能挡住「退出登录」把用户卡住：removeItem / 空 setItem 都要回填。
	if !strings.Contains(s, "removeItem") || !strings.Contains(s, "setItem") {
		t.Errorf("SeedScript 未覆盖 setItem/removeItem: %s", s)
	}
}

func TestBridgeScript(t *testing.T) {
	s := BridgeScript(NewPrefix("/app/agent2api"))
	for _, w := range []string{
		`data-fn-gateway-bridge="1"`,
		`var P="/app/agent2api"`,
		`window.__fnGatewayBase=P`,
		// 前缀改写与鉴权头迁移都要在（后者是飞牛 1.2.0604+ 拦 Authorization 的必需项）。
		"x-api-key",
		"Authorization",
		"fetch",
		"WebSocket",
	} {
		if !strings.Contains(s, w) {
			t.Errorf("BridgeScript 缺少 %q", w)
		}
	}
	// 原始字符串里不能出现反引号，否则 Go 源码会编译不过（这里顺带兜住）。
	if strings.Contains(s, "`") {
		t.Errorf("BridgeScript 含反引号")
	}
}

// 桥接必须按「前缀 + /」解析相对引用。
//
// 若仍按 window.location.href 解析：文档 URL 是 /app/agent2api（无尾斜杠），
// 裸相对引用 assets/providers/x.png 会解析成 /app/assets/providers/x.png，
// 再补前缀就成了 /app/agent2api/app/assets/providers/x.png —— 双重前缀 404，
// 正是「添加账号 → 反代」里模型图标全部不显示的成因。
func TestBridgeScriptResolvesRelativeToPrefix(t *testing.T) {
	s := BridgeScript(NewPrefix("/app/agent2api"))
	if !strings.Contains(s, "var RESOLVE_BASE=") {
		t.Fatalf("桥接缺少解析基准 RESOLVE_BASE: %s", s)
	}
	// 基准必须归一成绝对 URL，且落在前缀之下（new URL 的第二个参数必须是绝对 URL）。
	if !strings.Contains(s, "new URL(P+'/',window.location.href)") {
		t.Errorf("解析基准未按前缀归一化: %s", s)
	}
	// toGw 内部不得再拿 window.location.href 当基准（那会把相对路径解到上一级）。
	start := strings.Index(s, "function toGw(v){")
	if start < 0 {
		t.Fatalf("未找到 toGw: %s", s)
	}
	body := s[start:]
	end := strings.Index(body, "function str(url)")
	if end < 0 {
		t.Fatalf("未找到 str(): %s", body)
	}
	if strings.Contains(body[:end], "window.location.href") {
		t.Errorf("toGw 仍以 window.location.href 为基准: %s", body[:end])
	}
}

// jsString 必须转义任何可能提前闭合 <script> 的字符。
func TestJsString(t *testing.T) {
	if got := jsString(`/a"b</script>`); strings.Contains(got, "<") || strings.Contains(got, `"b`) {
		t.Fatalf("jsString 未转义危险字符: %s", got)
	}
	if got := jsString("/app/agent2api"); got != `"/app/agent2api"` {
		t.Fatalf("jsString 正常字符被改坏: %s", got)
	}
}

// ---------------------------------------------------------------------------
// 管理员门
// ---------------------------------------------------------------------------

func adminRequest(method, target string) *http.Request {
	r := httptest.NewRequest(method, target, nil)
	r.Header.Set("X-Trim-Userid", "1000")
	r.Header.Set("X-Trim-Isadmin", "true")
	return r
}

func TestAdminOnly(t *testing.T) {
	p := NewPrefix("/app/agent2api")
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok":true}`)
	})
	h := AdminOnly(next, p)

	cases := []struct {
		name        string
		req         *http.Request
		wantStatus  int
		wantCT      string
		wantContain string
	}{
		{"非管理员访问 API 得到 JSON 403", httptest.NewRequest("GET", "/app/agent2api/api/session", nil), 403, "application/json", "forbidden"},
		{"非管理员访问页面得到 HTML 403", httptest.NewRequest("GET", "/app/agent2api/", nil), 403, "text/html", "需要管理员权限"},
		{"缺少 Userid 视为非管理员", httptest.NewRequest("GET", "/app/agent2api/api/session", nil), 403, "application/json", "forbidden"},
		{"管理员放行", adminRequest("GET", "/app/agent2api/api/session"), 200, "application/json", "ok"},
	}
	for _, c := range cases {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, c.req)
		if rec.Code != c.wantStatus {
			t.Errorf("%s: 状态码 = %d, want %d", c.name, rec.Code, c.wantStatus)
		}
		if ct := rec.Header().Get("Content-Type"); !strings.Contains(ct, c.wantCT) {
			t.Errorf("%s: Content-Type = %q, want 含 %q", c.name, ct, c.wantCT)
		}
		if !strings.Contains(rec.Body.String(), c.wantContain) {
			t.Errorf("%s: 响应体缺少 %q: %s", c.name, c.wantContain, rec.Body.String())
		}
	}
}

// IsAdmin 只认网关注入的头，且 Isadmin 只接受真值。
func TestIsAdminTruthiness(t *testing.T) {
	for _, v := range []string{"1", "true", "TRUE", " yes "} {
		r := httptest.NewRequest("GET", "/", nil)
		r.Header.Set("X-Trim-Userid", "1")
		r.Header.Set("X-Trim-Isadmin", v)
		if !IsAdmin(r) {
			t.Errorf("Isadmin=%q 应判为管理员", v)
		}
	}
	for _, v := range []string{"", "0", "false", "no", "2"} {
		r := httptest.NewRequest("GET", "/", nil)
		r.Header.Set("X-Trim-Userid", "1")
		r.Header.Set("X-Trim-Isadmin", v)
		if IsAdmin(r) {
			t.Errorf("Isadmin=%q 不应判为管理员", v)
		}
	}
}

// ---------------------------------------------------------------------------
// 反代端到端（httptest 上游）
// ---------------------------------------------------------------------------

// fakeUpstream 模拟 agent2api 的关键行为，路由形态与上游
// server/src/server/http.rs + static_files.rs 一致：
// 根路径给 HTML（head 里第一个脚本就是 Web 壳），/api/* 无密钥 401、有密钥 200，
// 另有重定向 / Cookie / SSE 三个探针。
func fakeUpstream(t *testing.T, wantKey string) (*httptest.Server, *string) {
	t.Helper()
	var seenKey string
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = io.WriteString(w, `<html><head><script>var apiKey="";</script></head>`+
				`<body><script src="islands/ui.js"></script></body></html>`)
		case "/api/session":
			seenKey = r.Header.Get("x-api-key")
			if seenKey != wantKey {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(401)
				_, _ = io.WriteString(w, `{"error":{"type":"panel_login_required"}}`)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"success":true,"data":{"accounts":[]}}`)
		case "/api/redirect":
			w.Header().Set("Location", "/login")
			w.WriteHeader(302)
		case "/api/cookie":
			http.SetCookie(w, &http.Cookie{Name: "agent2api-panel", Value: "v", Path: "/"})
			w.WriteHeader(204)
		case "/api/stream":
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = io.WriteString(w, "data: hi\n\n")
		default:
			w.WriteHeader(404)
		}
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, &seenKey
}

func newPanelHandler(t *testing.T, upstream, key string) http.Handler {
	t.Helper()
	p := NewPrefix("/app/agent2api")
	h, err := NewProxy(Options{
		Prefix:       p,
		InternalAddr: strings.TrimPrefix(upstream, "http://"),
		RewriteHTML:  true,
		Bridge:       func() string { return SeedScript() + BridgeScript(p) },
		Prepare: func(r *http.Request) {
			if key != "" && strings.HasPrefix(r.URL.Path, "/api/") {
				r.Header.Set("x-api-key", key)
			}
		},
	})
	if err != nil {
		t.Fatalf("NewProxy: %v", err)
	}
	return AdminOnly(h, p)
}

// 面板密钥由服务端注入：浏览器送空值也能拿到 200，且上游看到的是真密钥。
func TestProxyInjectsKey(t *testing.T) {
	up, seen := fakeUpstream(t, "server-side-key")
	h := newPanelHandler(t, up.URL, "server-side-key")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/agent2api/api/session"))
	if rec.Code != 200 {
		t.Fatalf("状态码 = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	if *seen != "server-side-key" {
		t.Fatalf("上游收到的 x-api-key = %q", *seen)
	}
}

// 浏览器带来的伪造密钥必须被覆盖掉（密钥轮换免疫）。
func TestProxyOverridesClientKey(t *testing.T) {
	up, seen := fakeUpstream(t, "server-side-key")
	h := newPanelHandler(t, up.URL, "server-side-key")

	req := adminRequest("GET", "/app/agent2api/api/session")
	req.Header.Set("x-api-key", "attacker-supplied")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	if *seen != "server-side-key" {
		t.Fatalf("客户端密钥未被覆盖，上游收到 %q", *seen)
	}
}

func TestProxyHTMLRewriteAndInject(t *testing.T) {
	up, _ := fakeUpstream(t, "k")
	h := newPanelHandler(t, up.URL, "k")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/agent2api/"))
	body := rec.Body.String()
	if rec.Code != 200 {
		t.Fatalf("状态码 = %d", rec.Code)
	}
	if !strings.Contains(body, `src="/app/agent2api/islands/ui.js"`) {
		t.Errorf("相对资源未补前缀: %s", body)
	}
	if !strings.Contains(body, `data-fn-gateway-seed="1"`) || !strings.Contains(body, `data-fn-gateway-bridge="1"`) {
		t.Errorf("未注入种子/桥接脚本: %s", body)
	}
	// <base> 是相对引用的兜底：必须注入，且必须排在两个脚本之前
	// （浏览器要求 <base> 先于任何会解析 URL 的元素出现）。
	baseAt := strings.Index(body, `<base href="/app/agent2api/">`)
	seedAt := strings.Index(body, `data-fn-gateway-seed="1"`)
	bridgeAt := strings.Index(body, `data-fn-gateway-bridge="1"`)
	if baseAt < 0 {
		t.Fatalf("未注入 <base> 标签: %s", body)
	}
	if !(baseAt < seedAt && seedAt < bridgeAt) {
		t.Errorf("注入顺序应为 base < seed < bridge，实际 base@%d seed@%d bridge@%d",
			baseAt, seedAt, bridgeAt)
	}
	if strings.Contains(body, "/app/agent2api/app/agent2api") {
		t.Errorf("出现重复前缀: %s", body)
	}
	if cc := rec.Header().Get("Cache-Control"); !strings.Contains(cc, "no-store") {
		t.Errorf("面板 HTML 应 no-store，实际 %q", cc)
	}
}

func TestProxyRedirectAndCookie(t *testing.T) {
	up, _ := fakeUpstream(t, "k")
	h := newPanelHandler(t, up.URL, "k")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/agent2api/api/redirect"))
	if loc := rec.Header().Get("Location"); loc != "/app/agent2api/login" {
		t.Errorf("Location = %q, want /app/agent2api/login", loc)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/agent2api/api/cookie"))
	if sc := rec.Header().Get("Set-Cookie"); !strings.Contains(sc, "Path=/app/agent2api/") {
		t.Errorf("Set-Cookie 未收窄作用域: %q", sc)
	}
}

func TestProxySSE(t *testing.T) {
	up, _ := fakeUpstream(t, "k")
	h := newPanelHandler(t, up.URL, "k")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/agent2api/api/stream"))
	if got := rec.Header().Get("X-Accel-Buffering"); got != "no" {
		t.Errorf("SSE 缺少 X-Accel-Buffering: no，实际 %q", got)
	}
	if cc := rec.Header().Get("Cache-Control"); !strings.Contains(cc, "no-transform") {
		t.Errorf("SSE Cache-Control 应为 no-transform，实际 %q", cc)
	}
	if !strings.Contains(rec.Body.String(), "data: hi") {
		t.Errorf("SSE 内容未透传: %s", rec.Body.String())
	}
}

// 直通模式（下游端口）不做任何改写：路径原样、不注入脚本。
func TestProxyPassthrough(t *testing.T) {
	up, _ := fakeUpstream(t, "k")
	h, err := NewProxy(Options{InternalAddr: strings.TrimPrefix(up.URL, "http://")})
	if err != nil {
		t.Fatalf("NewProxy: %v", err)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	body := rec.Body.String()
	if strings.Contains(body, "data-fn-gateway-bridge") {
		t.Errorf("直通模式不应注入桥接脚本: %s", body)
	}
	if strings.Contains(body, "/app/agent2api/islands/ui.js") {
		t.Errorf("直通模式不应改写路径: %s", body)
	}
}

func TestNewProxyRejectsEmptyAddr(t *testing.T) {
	if _, err := NewProxy(Options{}); err == nil {
		t.Fatal("空内部地址应报错")
	}
}
