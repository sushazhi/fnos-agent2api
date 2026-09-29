package gateway

import (
	"encoding/json"
	"net/http"
	"strings"
)

// 飞牛统一网关注入的会话头（权威写入方是网关本身）。
const (
	headerUserId  = "X-Trim-Userid"
	headerIsAdmin = "X-Trim-Isadmin"
)

// IsAdmin 判断请求是否来自飞牛管理员会话。
//
// 信任边界：这两个头由飞牛统一网关校验登录态后注入。本进程只监听
// ${TRIM_APPDEST}/agent2api.sock（0600），浏览器无法绕过网关直连；
// 下游端口由另一条链路处理，不经过本函数（见 main.go）。
// 因此这里不校验任何客户端可伪造的参数（query/body/自定义头）。
func IsAdmin(r *http.Request) bool {
	if strings.TrimSpace(r.Header.Get(headerUserId)) == "" {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(r.Header.Get(headerIsAdmin))) {
	case "1", "true", "yes":
		return true
	default:
		return false
	}
}

// AdminOnly 仅放行管理员会话；其余返回 403（API 走 JSON，页面走 HTML）。
//
// 本应用不设自己的登录口令：飞牛登录态即身份，且入口在 ui/config 里声明为
// allUsers=false + readonly，非管理员本就不该看到图标。
// （面板自身另有「管理员账号 + 会话 Cookie」那套登录，本应用**不启用** ——
// 服务端直接注入密钥，见 main.go 的 Prepare 钩子。）
func AdminOnly(next http.Handler, prefix Prefix) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if IsAdmin(r) {
			next.ServeHTTP(w, r)
			return
		}
		internal, _ := prefix.Strip(r.URL.Path)
		Forbidden(w, internal)
	})
}

// IsAPIPath 判断是否为机器可读接口（错误按 JSON 返回，便于前端解析）。
func IsAPIPath(path string) bool {
	return strings.HasPrefix(path, "/api/") || path == "/api" ||
		strings.HasPrefix(path, "/v1/") || path == "/v1"
}

// Forbidden 输出 403；internal 为剥除网关前缀后的路径。
func Forbidden(w http.ResponseWriter, internal string) {
	if IsAPIPath(internal) {
		writeErrorJSON(w, http.StatusForbidden, "forbidden", "仅飞牛管理员可访问 Agent2API（请用管理员账号登录飞牛后重试）")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusForbidden)
	_, _ = w.Write([]byte(forbiddenPage))
}

const forbiddenPage = `<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>需要管理员权限</title>
<body style="font-family:system-ui;margin:0;background:#0f1115;color:#e6e6e6;display:flex;min-height:100vh;align-items:center;justify-content:center">
  <div style="max-width:520px;padding:32px;text-align:center">
    <h2 style="margin:0 0 12px">🔒 需要管理员权限</h2>
    <p style="color:#9aa0a6;line-height:1.6">Agent2API 面板会读取本机各提供商账号凭据，因此只对飞牛管理员开放。</p>
    <p style="color:#9aa0a6;line-height:1.6">请用管理员账号登录飞牛后，从桌面图标重新进入。</p>
  </div>
</body>`

// NotFound 输出 404（下游端口对非 /v1 路径使用）。
func NotFound(w http.ResponseWriter, r *http.Request) {
	writeErrorJSON(w, http.StatusNotFound, "not_found", "仅 /v1/* 与 /health 在此端口开放；面板请从飞牛桌面图标进入")
}

func writeErrorJSON(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error": map[string]any{
			"message": msg,
			"type":    "api_error",
			"code":    code,
		},
	})
}
