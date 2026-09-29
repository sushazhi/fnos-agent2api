// Package gateway 飞牛统一网关适配层。
//
// 职责：把挂在 /app/agent2api 子路径下的请求透明地代理到 agent2api 内部服务，
// 并完成子路径改写（HTML 绝对路径、重定向、Cookie 作用域、SSE），
// 使未做 base-path 适配的官方 Web 面板能直接在网关下工作；
// 另提供下游独立端口的直通代理（仅 /v1/* 与 /health，不做任何改写）。
//
// 与 cli2api 版的差异（agent2api 上游的实测结论，见 README「子路径适配」）：
//   - 面板没有任何前端路由：不读 location.pathname、不写 history，页面切换靠内存
//     状态。因此**不需要**构建期 router 补丁，运行时桥接即可覆盖全部动态 URL。
//   - 面板的 Web 壳（web_shim.rs，编译进二进制）自身用根绝对路径 fetch('/api/...')，
//     改不了源码，必须由桥接脚本在运行时补前缀。
//   - 面板的鉴权密钥存在 localStorage['agent2api.webKey']，服务端注入后浏览器侧
//     只需一个占位值（见 SeedScript）。
//
// 参考 fnos-developer skill references/gateway-proxy.md 与 D:\fnos 下
// fnos-workbuddy2api / deepseek.harness 两个已验证项目的实现。
package gateway

import (
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path"
	"regexp"
	"strings"
	"time"
)

// Prefix 网关挂载前缀（运行时注入）。
type Prefix struct {
	// Path 形如 /app/agent2api（无尾斜杠）。零值表示不做前缀处理。
	Path string
}

// NewPrefix 构造前缀对象；空值回落到本应用默认前缀。
func NewPrefix(p string) Prefix {
	p = strings.TrimRight(strings.TrimSpace(p), "/")
	if p == "" {
		p = "/app/agent2api"
	}
	return Prefix{Path: p}
}

// Strip 剥除前缀，返回以 / 开头的内部路径。
// safe 为 false 表示该路径本就不带前缀（调用方自行决定如何处理）。
//
// 循环剥除，保证幂等：万一请求被拦了两次（外层代理与内层代理都改写过，
// 或调用方误用），/app/x/app/x/api 只剥一层会剩下 /app/x/api 回源，
// 上游必然 404。这里一路剥到底。
func (p Prefix) Strip(path string) (string, bool) {
	ok := false
	for {
		if path == p.Path {
			return "/", true
		}
		if strings.HasPrefix(path, p.Path+"/") {
			path = path[len(p.Path):]
			ok = true
			continue
		}
		break
	}
	return path, ok
}

// ---------------------------------------------------------------------------
// 子路径改写
// ---------------------------------------------------------------------------

var (
	// HTML 中需要加前缀的路径属性：src/href/action/poster。
	// 这里先宽松地取出**任意**带引号的值，再由 shouldRewrite 判定 —— 因为
	// agent2api 面板是手写 HTML，资源引用是**裸相对路径**：
	//   <script src="icons.js">、<script src="islands/ui.js">
	// 而 cli2api 是 Vite 产物（./assets/x.js 或 /assets/x.js）。只匹配
	// 「以 / 或 ./ 开头」会漏掉裸相对路径，页面在网关下会白屏。
	htmlAttrRe = regexp.MustCompile(`(?i)\b(src|href|action|poster)\s*=\s*(["'])([^"']*)`)
	// 带 scheme 的绝对 URL（http:、https:、data:、blob:、mailto:、javascript:…）。
	schemeRe = regexp.MustCompile(`^[a-zA-Z][a-zA-Z0-9+.\-]*:`)
	// Set-Cookie 的 Path=<value>。
	cookiePathRe = regexp.MustCompile(`(?i)\bpath\s*=\s*(/[^;,]*)`)
	// PWA manifest 链接：必须补 crossorigin，否则 manifest 请求不带凭证，
	// 被飞牛网关判为未登录（invalid token）。
	manifestLinkRe = regexp.MustCompile(`(?i)<link[^>]*rel\s*=\s*["']manifest["'][^>]*>`)
)

// shouldRewrite 判断某个属性值是否需要补前缀。
//
// 不改写的三类：空值/纯锚点、协议相对（//host/x）、带 scheme 的绝对 URL。
// 已带前缀的也不重复加，避免 /app/agent2api/app/agent2api/...。
func (p Prefix) shouldRewrite(v string) bool {
	v = strings.TrimSpace(v)
	if v == "" || strings.HasPrefix(v, "#") {
		return false
	}
	// 协议相对 //host/x 不动。
	if strings.HasPrefix(v, "//") {
		return false
	}
	// 绝对 URL（含 data:/blob:/javascript:）不动。
	if schemeRe.MatchString(v) {
		return false
	}
	rel := strings.TrimPrefix(v, "./")
	if rel == p.Path || strings.HasPrefix(rel, p.Path+"/") {
		return false
	}
	return true
}

// RewriteHTML 改写 HTML 文档中的资源路径与 manifest 链接。
func (p Prefix) RewriteHTML(body []byte) []byte {
	s := htmlAttrRe.ReplaceAllStringFunc(string(body), func(m string) string {
		sub := htmlAttrRe.FindStringSubmatch(m)
		if len(sub) != 4 {
			return m
		}
		attr, quote, val := sub[1], sub[2], sub[3]
		if !p.shouldRewrite(val) {
			return m
		}
		clean := path.Clean("/" + strings.TrimPrefix(strings.TrimSpace(val), "./"))
		return attr + "=" + quote + p.Path + clean
	})

	s = manifestLinkRe.ReplaceAllStringFunc(s, func(m string) string {
		if strings.Contains(strings.ToLower(m), "crossorigin") {
			return m
		}
		return strings.TrimSuffix(m, ">") + ` crossorigin="use-credentials">`
	})
	return []byte(s)
}

// BaseTag 生成把文档基址钉在网关前缀下的 <base> 标签。
//
// 为什么必须有它：飞牛桌面入口的 url 是 /app/agent2api（**不带尾斜杠**），
// 文档 URL 因此就是 https://host/app/agent2api。这时任何裸相对引用都会按
// 「上一级目录」解析：
//
//	new URL('assets/providers/workbuddy.png', 'https://host/app/agent2api')
//	  → https://host/app/assets/providers/workbuddy.png
//
// agent2api 这一段被吃掉了。表现就是「添加账号 → 反代」里的模型图标全部不显示
// —— 图标（PROVIDER_ICONS / wbPresetProviders.iconOf）走的正是裸相对路径。
//
// 注入 <base href="{前缀}/"> 后，浏览器把文档基址固定成
// https://host/app/agent2api/，裸相对引用重新落回前缀之下。
//
// 它只影响相对引用：以 / 开头的根绝对路径只借用 base 的 origin，路径部分不受
// 影响，因此静态改写（RewriteHTML）与运行时桥接都不会被它打乱。
// 与 fnos-logmanager（ServeIndexWithBase）、nas（injectPrefix）、
// deepseek.harness（fnGatewayBridgeScript）三处的做法一致。
func (p Prefix) BaseTag() string {
	if p.Path == "" {
		return ""
	}
	return `<base href="` + p.Path + `/">`
}

// InjectHead 把片段插入 HTML 的 <head> 之后。
//
// 为什么是「紧随 <head> 之后」而不是「</head> 之前」：agent2api 的 Web 壳
// （server/src/web_shim.rs）把自己那段脚本作为 <head> 的第一个子元素注入，
// 而它**在脚本顶层**就把 localStorage['agent2api.webKey'] 读进了闭包变量
// （`var apiKey = readStoredKey()`）。种子脚本必须排在它前面才来得及改写
// localStorage —— 放到 </head> 之前就已经太晚，面板会拿不到密钥。
func InjectHead(body []byte, snippet string) []byte {
	if snippet == "" {
		return body
	}
	s := string(body)
	if idx := indexFold(s, "<head>"); idx >= 0 {
		at := idx + len("<head>")
		return []byte(s[:at] + snippet + s[at:])
	}
	// 无 <head>（异常文档）：退化为前置，至少保证脚本先于其它脚本执行。
	return []byte(snippet + s)
}

func indexFold(s, sub string) int {
	return strings.Index(strings.ToLower(s), strings.ToLower(sub))
}

// RewriteLocation 改写重定向 Location 的站内绝对路径。
func (p Prefix) RewriteLocation(loc string) string {
	if loc == "" {
		return loc
	}
	if !strings.HasPrefix(loc, "/") || strings.HasPrefix(loc, "//") {
		return loc
	}
	if loc == p.Path || strings.HasPrefix(loc, p.Path+"/") {
		return loc
	}
	return p.Path + loc
}

// RewriteCookie 把 Set-Cookie 的 Path 收窄/迁移到网关前缀下。
//
// 三种情况：
//   - Path=/          → Path=/app/agent2api/          （根作用域收窄到应用下）
//   - Path=/api/panel → Path=/app/agent2api/api/panel （子路径整体迁移）
//   - 已带前缀        → 原样保留，避免重复加
//
// 为什么必须做：飞牛网关把应用挂在子路径上，浏览器按「整站」理解 Path=/，
// Cookie 既越界又可能被同域其它应用覆盖；而上游 agent2api 的会话 Cookie
// （agent2api-panel / agent2api-panel-rt）都带 Path（见 server/src/server/access.rs），
// 不改写会直接导致「登录后掉线」。
func (p Prefix) RewriteCookie(v string) string {
	return cookiePathRe.ReplaceAllStringFunc(v, func(m string) string {
		sub := cookiePathRe.FindStringSubmatch(m)
		if len(sub) != 2 {
			return m
		}
		val := strings.TrimSpace(sub[1])
		if val == p.Path || strings.HasPrefix(val, p.Path+"/") {
			return "Path=" + val
		}
		if val == "/" {
			return "Path=" + p.Path + "/"
		}
		return "Path=" + p.Path + val
	})
}

// ---------------------------------------------------------------------------
// 反向代理
// ---------------------------------------------------------------------------

// Options 反代构造参数。
type Options struct {
	// Prefix 网关前缀；零值表示直通（下游独立端口）。
	Prefix Prefix
	// InternalAddr 内部服务地址，形如 127.0.0.1:3065。
	InternalAddr string
	// Timeout 建立到内部服务的连接与响应头超时。0 表示使用默认值。
	Timeout time.Duration
	// RewriteHTML 为 true 时改写真响应（HTML/Location/Cookie）。
	RewriteHTML bool
	// Bridge 返回注入 HTML 的脚本片段（RewriteHTML 为 true 时生效）。
	Bridge func() string
	// Prepare 在转发前改写出站请求（例如注入面板密钥）。
	Prepare func(*http.Request)
}

// NewProxy 构造到内部服务的反向代理。
func NewProxy(opt Options) (http.Handler, error) {
	if strings.TrimSpace(opt.InternalAddr) == "" {
		return nil, errors.New("gateway: 内部服务地址为空")
	}
	timeout := opt.Timeout
	if timeout <= 0 {
		timeout = 30 * time.Second
	}

	target := &url.URL{Scheme: "http", Host: opt.InternalAddr}
	rp := httputil.NewSingleHostReverseProxy(target)

	// 默认 ErrorHandler 返回 502 纯文本；这里给友好 HTML 并记录。
	rp.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		log.Printf("网关代理失败 %s %s: %v", r.Method, r.URL.Path, err)
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(http.StatusBadGateway)
		_, _ = io.WriteString(w, `<!doctype html><meta charset="utf-8">`+
			`<body style="font-family:system-ui;padding:40px;background:#0f1115;color:#e6e6e6">`+
			`<h2>⚠️ Agent2API 服务不可达</h2>`+
			`<p>上游 agent2api 可能仍在启动中，请稍候刷新；若持续出现，请在飞牛应用中心重启本应用。</p>`+
			`<pre style="color:#888">`+htmlEscape(err.Error())+`</pre></body>`)
	}

	// DisableCompression：避免拿到 gzip 内容后无法做 HTML 改写。
	// 回源是明文 HTTP 回环，不需要 HTTP/2 协商。
	rp.Transport = &http.Transport{
		DialContext:           (&net.Dialer{Timeout: 10 * time.Second}).DialContext,
		MaxIdleConns:          64,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		DisableCompression:    true,
		ForceAttemptHTTP2:     false,
		ResponseHeaderTimeout: timeout,
	}

	orig := rp.Director
	rp.Director = func(req *http.Request) {
		orig(req)

		// 1) 剥除网关前缀（Path 与 RawPath 都要处理，否则编码路径会错乱）。
		if opt.Prefix.Path != "" {
			internal, _ := opt.Prefix.Strip(req.URL.Path)
			if !strings.HasPrefix(internal, "/") {
				internal = "/" + internal
			}
			req.URL.Path = internal
			if req.URL.RawPath != "" {
				rawInternal, _ := opt.Prefix.Strip(req.URL.RawPath)
				if !strings.HasPrefix(rawInternal, "/") {
					rawInternal = "/" + rawInternal
				}
				req.URL.RawPath = rawInternal
			}
		}

		// 2) 清理干扰内部服务或可被客户端伪造的头。
		req.Header.Del("Accept-Encoding")
		req.Header.Del("X-Forwarded-Host")
		req.Header.Del("X-Forwarded-Proto")
		req.Header.Set("X-Forwarded-Prefix", opt.Prefix.Path)
		// 本应用以飞牛登录态为唯一身份（见 gate.go），
		// 内部服务只认密钥，不读 X-Trim-*，无需为它们盖章。

		// 3) 出站改写（控制台密钥注入等）。
		if opt.Prepare != nil {
			opt.Prepare(req)
		}
	}

	rp.ModifyResponse = func(resp *http.Response) error {
		if !opt.RewriteHTML {
			tuneSSE(resp)
			return nil
		}

		// 3) 重定向 Location 改写。
		if loc := resp.Header.Get("Location"); loc != "" {
			resp.Header.Set("Location", opt.Prefix.RewriteLocation(loc))
		}
		// 4) Cookie 作用域改写。
		if cookies, ok := resp.Header["Set-Cookie"]; ok {
			for i, c := range cookies {
				cookies[i] = opt.Prefix.RewriteCookie(c)
			}
		}

		// 5) SSE 流式响应：关闭缓冲，否则事件流被攒住。
		if tuneSSE(resp) {
			return nil
		}

		ct := resp.Header.Get("Content-Type")
		switch {
		case strings.HasPrefix(ct, "text/html"):
			body, err := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			if err != nil {
				return err
			}
			body = opt.Prefix.RewriteHTML(body)
			// 注入顺序有讲究：<base> 必须最靠前（浏览器要求它先于任何会解析
			// URL 的元素出现），种子脚本其次（要抢在 Web 壳读 localStorage
			// 之前），桥接脚本最后。三者合成一个片段一次插入。
			snippet := opt.Prefix.BaseTag()
			if opt.Bridge != nil {
				snippet += opt.Bridge()
			}
			body = InjectHead(body, snippet)
			resp.Body = io.NopCloser(strings.NewReader(string(body)))
			resp.ContentLength = int64(len(body))
			resp.Header.Set("Content-Length", fmt.Sprint(len(body)))
			// 面板页面每次都要重新注入脚本与 no-store，禁止中间层缓存。
			resp.Header.Set("Cache-Control", "no-store")
			// 改写会注入脚本；若上游带了 CSP，注入会被拦掉。
			resp.Header.Del("Content-Security-Policy")
			resp.Header.Del("Content-Security-Policy-Report-Only")
		}
		return nil
	}

	return rp, nil
}

// tuneSSE 针对事件流关闭缓冲；返回 true 表示该响应按 SSE 处理。
func tuneSSE(resp *http.Response) bool {
	if !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
		return false
	}
	resp.Header.Set("Cache-Control", "no-cache, no-transform")
	resp.Header.Set("X-Accel-Buffering", "no")
	resp.Header.Del("Content-Length")
	return true
}

func htmlEscape(s string) string {
	r := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;")
	return r.Replace(s)
}

// ---------------------------------------------------------------------------
// 监听
// ---------------------------------------------------------------------------

// ListenUnix 在指定路径上监听 Unix Socket（先清理残留），权限 0600。
func ListenUnix(path string) (net.Listener, error) {
	if _, err := os.Stat(path); err == nil {
		if rmErr := os.Remove(path); rmErr != nil {
			return nil, fmt.Errorf("清理旧 Socket %s 失败: %w", path, rmErr)
		}
	}
	ln, err := net.Listen("unix", path)
	if err != nil {
		return nil, fmt.Errorf("监听 Unix Socket %s 失败: %w", path, err)
	}
	// 网关以 root 身份连接，收紧到属主可读写。
	if err := os.Chmod(path, 0o600); err != nil {
		log.Printf("警告：设置 Socket 权限失败: %v", err)
	}
	return ln, nil
}
