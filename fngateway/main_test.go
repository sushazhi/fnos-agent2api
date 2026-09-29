package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// TestLayoutPortDefaultsToUpstream 锁定下游端口默认值 = 上游 agent2api 的默认端口。
//
// 上游 server/src/server/config/parse.rs 里 AGENT2API_PROXY_PORT 的默认值是
// 3065（README 也写 3065）。取上游默认值，下游客户端照抄上游文档即可直连；
// 同时飞牛注入的 TRIM_SERVICE_PORT 必须优先，否则应用中心改端口会静默失效。
func TestLayoutPortDefaultsToUpstream(t *testing.T) {
	t.Setenv("TRIM_SERVICE_PORT", "")
	if got := resolveLayout().port; got != 3065 {
		t.Fatalf("默认下游端口 = %d，期望上游默认值 3065", got)
	}
	t.Setenv("TRIM_SERVICE_PORT", "4321")
	if got := resolveLayout().port; got != 4321 {
		t.Fatalf("TRIM_SERVICE_PORT=4321 时端口 = %d，期望 4321", got)
	}
	// 越界值必须回落到默认端口，不能把 0 或 >65535 带进监听。
	for _, bad := range []string{"0", "70000", "abc"} {
		t.Setenv("TRIM_SERVICE_PORT", bad)
		if got := resolveLayout().port; got != 3065 {
			t.Fatalf("TRIM_SERVICE_PORT=%q 未被拒绝: 端口 = %d，期望回落 3065", bad, got)
		}
	}
}

// TestResolveLayoutSocketAndPrefix 锁定 Socket 文件名与前缀。
//
// 飞牛统一网关把 socket 目标固定为
// /var/apps/{appname}/target/{gatewaySocket}，且 gatewaySocket 必须与
// app/ui/config 里声明的一致；前缀必须与 gatewayPrefix 一致，否则面板
// 点开就是 404。
func TestResolveLayoutSocketAndPrefix(t *testing.T) {
	t.Setenv("TRIM_APPDEST", "/var/apps/agent2api/target")
	t.Setenv("TRIM_PKGVAR", "/var/apps/agent2api/var")
	t.Setenv("TRIM_SERVICE_PORT", "")
	lay := resolveLayout()

	// 用 filepath.Join 表达期望值：本机跑测试是 Windows，部署目标是 Linux，
	// 断言的是「相对布局」而不是分隔符字面量。
	if want := filepath.Join("/var/apps/agent2api/target", "agent2api.sock"); lay.socket != want {
		t.Errorf("socket = %q，期望 %q", lay.socket, want)
	}
	if lay.prefix.Path != "/app/agent2api" {
		t.Errorf("前缀 = %q，期望 /app/agent2api", lay.prefix.Path)
	}
	if lay.pkgVar != "/var/apps/agent2api/var" {
		t.Errorf("数据目录 = %q", lay.pkgVar)
	}
}

// 开发直跑（没有 TRIM_* 环境）不能崩，且数据目录要落在安装目录下的 var/。
func TestResolveLayoutWithoutTrimEnv(t *testing.T) {
	t.Setenv("TRIM_APPDEST", "")
	t.Setenv("TRIM_PKGVAR", "")
	t.Setenv("TRIM_SERVICE_PORT", "")
	lay := resolveLayout()
	if lay.appDest == "" || lay.pkgVar == "" {
		t.Fatalf("缺 TRIM_* 时应回落到当前目录: %+v", lay)
	}
	if !strings.HasSuffix(lay.socket, string(os.PathSeparator)+"agent2api.sock") {
		t.Errorf("socket = %q", lay.socket)
	}
}

// TestPublicRoutesWhitelist 锁定下游对外端口的路径白名单。
//
// 这是进程里唯一对外裸露的入口：面板接口（/api/*）一旦从这里可达，
// 等于把管理员面板开到局域网。用 Clean 后的路径判定，才能挡住靠 /v1/
// 前缀蒙混、再指望上游归一化的绕过写法。
func TestPublicRoutesWhitelist(t *testing.T) {
	var reached int
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(r.URL.Path))
	})
	h := publicRoutes(upstream)

	cases := []struct {
		path    string
		reached bool
		name    string
	}{
		{path: "/health", reached: true, name: "健康检查放行"},
		{path: "/v1/chat/completions", reached: true, name: "chat 放行"},
		{path: "/v1/models", reached: true, name: "models 放行"},
		{path: "/v1/messages", reached: true, name: "anthropic 放行"},
		{path: "/v1/responses", reached: true, name: "responses 放行"},

		{path: "/", reached: false, name: "根路径拒绝"},
		{path: "/api/keys", reached: false, name: "面板密钥接口拒绝"},
		{path: "/api/session", reached: false, name: "面板会话接口拒绝"},
		{path: "/api/panel/status", reached: false, name: "面板状态接口拒绝"},
		{path: "/api/config", reached: false, name: "面板配置接口拒绝"},
		{path: "/islands/ui.js", reached: false, name: "面板静态资源拒绝"},
		{path: "/login", reached: false, name: "面板页面拒绝"},
		{path: "/v1", reached: false, name: "无尾斜杠的 /v1 拒绝"},
		{path: "/v1x/models", reached: false, name: "前缀相似路径拒绝"},

		{path: "/v1/../api/keys", reached: false, name: "点段逃逸到面板接口拒绝"},
		{path: "/v1/../api/panel/status", reached: false, name: "点段逃逸到面板状态拒绝"},
		{path: "/v1/./../../etc/passwd", reached: false, name: "多层点段逃逸拒绝"},
		{path: "/v1/../../api/keys", reached: false, name: "越界后回退拒绝"},
	}

	for _, c := range cases {
		reached = 0
		req := httptest.NewRequest(http.MethodGet, c.path, nil)
		// 万一构造请求时路径就被规范化了，点段用例会「因为错误的原因」通过。
		if req.URL.Path != c.path {
			t.Fatalf("%s: 夹具路径被规范化成 %q，用例失去意义", c.name, req.URL.Path)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)

		got := reached > 0
		if got != c.reached {
			t.Errorf("%s: %s 转发=%v，期望 %v（状态码 %d）", c.name, c.path, got, c.reached, rec.Code)
		}
		if !c.reached && rec.Code != http.StatusNotFound {
			t.Errorf("%s: %s 拒绝状态码 %d，期望 404", c.name, c.path, rec.Code)
		}
	}
}

// TestPublicRoutesForwardsPathVerbatim 放行时不得改写路径：上游要按原样
// 收到请求（含尾斜杠），否则等于网关偷偷改了路由语义。
func TestPublicRoutesForwardsPathVerbatim(t *testing.T) {
	var seen string
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.URL.Path
		w.WriteHeader(http.StatusOK)
	})
	h := publicRoutes(upstream)

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions/", nil)
	h.ServeHTTP(httptest.NewRecorder(), req)

	if seen != "/v1/chat/completions/" {
		t.Errorf("上游收到的路径为 %q，期望原样 /v1/chat/completions/", seen)
	}
}

func envLookup(env []string, key string) (string, bool) {
	for _, kv := range env {
		if strings.HasPrefix(kv, key+"=") {
			return strings.TrimPrefix(kv, key+"="), true
		}
	}
	return "", false
}

// TestChildEnvContract 锁定上游子进程的环境契约。
//
// 变量名必须与上游 server/src/server/config/parse.rs 一致：写错一个字母，
// 上游会静默用默认值（比如把数据写进 HOME 下的 ~/.agent2api 而不是
// ${TRIM_PKGVAR}），面板看上去能开，但升级/卸载时数据全丢。
func TestChildEnvContract(t *testing.T) {
	lay := layout{appDest: "/appdest", pkgVar: "/pkgvar", socket: "/appdest/agent2api.sock", port: 3065}
	env := childEnv(lay, "the-gateway-key", "4567")

	wants := map[string]string{
		"AGENT2API_PROXY_HOME": filepath.Join("/pkgvar", "data"),
		"AGENT2API_HOST":       "127.0.0.1",
		"AGENT2API_PROXY_PORT": "4567",
		"AGENT2API_UI_DIR":     filepath.Join("/appdest", "ui"),
		// 面板不走登录流程，不需要 ALTCHA 人机校验。
		"AGENT2API_CAPTCHA_ENABLED": "0",
		// 各提供商 CLI/凭据都往 HOME 下写，必须在可写且持久的位置。
		"HOME": filepath.Join("/pkgvar", "home"),
		// 密钥进 config.api_key，进而出现在 active_api_keys() 里，
		// 让「无密钥即 fail-closed」的启动门失效（见 main.go 文件头）。
		"AGENT2API_PROXY_API_KEY": "the-gateway-key",
	}
	for k, v := range wants {
		if got, ok := envLookup(env, k); !ok || got != v {
			t.Errorf("%s = (%q,%v)，期望 %q", k, got, ok, v)
		}
	}

	// 子进程必须只监听回环：面板只经统一网关与下游白名单暴露。
	if host, _ := envLookup(env, "AGENT2API_HOST"); host != "127.0.0.1" {
		t.Errorf("AGENT2API_HOST = %q，必须为 127.0.0.1", host)
	}
}

// TestChildEnvNeverRegistersAdmin 面板登录一旦启用就会踩上「飞牛网关剥掉
// Set-Cookie」的坑（上游 PR #41 在 v2.9.0 未合并），把免密面板变成登录死循环。
// 也绝不能设 ALLOW_NO_KEY，否则下游端口对局域网裸奔。
func TestChildEnvNeverRegistersAdmin(t *testing.T) {
	lay := layout{appDest: "/appdest", pkgVar: "/pkgvar", port: 3065}
	env := childEnv(lay, "k", "4567")
	for _, forbidden := range []string{
		"AGENT2API_ADMIN_USER",
		"AGENT2API_ADMIN_PASSWORD",
		"AGENT2API_ADMIN_PASSWORD_HASH",
		"AGENT2API_ALLOW_NO_KEY",
		"AGENT2API_PANEL_PORT",
	} {
		if got, ok := envLookup(env, forbidden); ok {
			t.Errorf("不应注入 %s（实际 %q）", forbidden, got)
		}
	}
}

// 拿不到密钥时不能注入空串：上游会把「设了但为空」当成已配置，反而让
// fail-closed 门失效。
func TestChildEnvOmitsEmptyKey(t *testing.T) {
	lay := layout{appDest: "/appdest", pkgVar: "/pkgvar", port: 3065}
	if got, ok := envLookup(childEnv(lay, "", "4567"), "AGENT2API_PROXY_API_KEY"); ok {
		t.Errorf("空密钥时不应注入 AGENT2API_PROXY_API_KEY，实际 %q", got)
	}
}

// TestResolveAPIKeyFromEnv 环境变量优先（开发直跑用）。
func TestResolveAPIKeyFromEnv(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("AGENT2API_PROXY_API_KEY", "  env-key  ")
	key, src := resolveAPIKey(dir)
	if key != "env-key" {
		t.Errorf("key = %q，期望去掉首尾空白的 env-key", key)
	}
	if !strings.Contains(src, "AGENT2API_PROXY_API_KEY") {
		t.Errorf("来源说明 = %q", src)
	}
	// 走环境变量时不应顺手在磁盘上建文件。
	if _, err := os.Stat(filepath.Join(dir, keyFileName)); err == nil {
		t.Errorf("环境变量优先时不应写 %s", keyFileName)
	}
}

// TestResolveAPIKeyGeneratesAndReuses 首次生成、之后复用，且权限必须是 0600。
//
// 密钥只对属主可读：同机其它应用（或飞牛的其它用户）不该读到它。
func TestResolveAPIKeyGeneratesAndReuses(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("AGENT2API_PROXY_API_KEY", "")

	key1, src1 := resolveAPIKey(dir)
	if len(key1) != 64 {
		t.Fatalf("生成的密钥长度 = %d，期望 64 位十六进制", len(key1))
	}
	if !strings.Contains(src1, "本次新建") {
		t.Errorf("来源说明 = %q，期望标注新建", src1)
	}
	keyPath := filepath.Join(dir, keyFileName)
	info, err := os.Stat(keyPath)
	if err != nil {
		t.Fatalf("密钥文件未生成: %v", err)
	}
	// Windows 上 Go 的 os.Stat 不返回 POSIX 权限位（恒为 0666），
	// 权限断言只在类 Unix 平台有意义；部署目标 Linux 是强制项。
	if runtime.GOOS != "windows" {
		if perm := info.Mode().Perm(); perm != 0o600 {
			t.Errorf("密钥文件权限 = %04o，期望 0600", perm)
		}
	}

	// 第二次必须读到同一个值（重启不能换密钥，否则下游客户端全废）。
	key2, src2 := resolveAPIKey(dir)
	if key2 != key1 {
		t.Errorf("重启后密钥变了: %q → %q", key1, key2)
	}
	if strings.Contains(src2, "本次新建") {
		t.Errorf("第二次不应重建: %q", src2)
	}
}

// 数据目录不存在时要能自建（升级/首次安装后 ${TRIM_PKGVAR} 可能刚被清空）。
func TestResolveAPIKeyCreatesPkgVar(t *testing.T) {
	base := t.TempDir()
	dir := filepath.Join(base, "nested", "var")
	t.Setenv("AGENT2API_PROXY_API_KEY", "")

	key, _ := resolveAPIKey(dir)
	if key == "" {
		t.Fatal("未生成密钥")
	}
	if _, err := os.Stat(filepath.Join(dir, keyFileName)); err != nil {
		t.Fatalf("嵌套数据目录未创建: %v", err)
	}
}

// 空文件 / 只有空白的文件视为没有密钥，必须重新生成。
func TestResolveAPIKeyIgnoresBlankFile(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("AGENT2API_PROXY_API_KEY", "")
	if err := os.WriteFile(filepath.Join(dir, keyFileName), []byte("  \n"), 0o600); err != nil {
		t.Fatal(err)
	}
	key, _ := resolveAPIKey(dir)
	if key == "" {
		t.Fatal("空白密钥文件应触发重新生成")
	}
}
