// Command fngateway Agent2API 飞牛网关入口：反向代理 + 子进程监督。
//
// 进程模型（单进程，双监听，仅由 cmd/main 管理这一个 PID）：
//
//	┌─ 飞牛统一网关 ─→ ${TRIM_APPDEST}/agent2api.sock
//	│      └─ 面板入口（仅管理员，见 internal/gateway/gate.go）：
//	│           剥前缀 /app/agent2api → 改写 HTML/Cookie →
//	│           注入桥接脚本与密钥占位 → 127.0.0.1:<内部端口>
//	├─ TCP ${TRIM_SERVICE_PORT}（3065，上游默认端口）
//	│      └─ 下游独立接入：只放行 /v1/* 与 /health，OpenAI 兼容客户端直连
//	└─ agent2api-server 子进程（上游原样二进制，零源码改动）：127.0.0.1:<内部端口>
//
// 为什么必须有 TCP 下游端口：飞牛统一网关（1.2.0604+）会把下游客户端必带的
// Authorization 当成自己的票据拦截（invalid token），OpenAI 兼容客户端走不通
// 网关路径 —— 与 D:\fnos 下 workbuddy2api / deepseek.harness 两个项目的结论一致。
//
// 为什么面板在网关下能免密登录：agent2api 的面板有两套鉴权入口 ——
//
//	(a) 管理员账号 + 会话 Cookie（server/src/server/access.rs 的 panel gate）；
//	(b) API Key（Authorization: Bearer / x-api-key，见 http::require_api_key）。
//
// 本应用**只用 (b)**：服务端把密钥注入 x-api-key，浏览器侧只放占位值。
// 实测（v2.9.0）在只设 AGENT2API_PROXY_API_KEY、不注册管理员时：
//
//	/api/panel/status → {"registered":false}
//	/api/session 无密钥 → 401 panel_login_required；带 x-api-key → 200
//	/v1/models 带密钥 → 200
//	任何响应都不下发 Set-Cookie
//
// 因此既不需要管理员口令，也完全绕开了「飞牛网关剥掉 Set-Cookie 导致登录弹回」
// 这个坑（上游修此问题的 PR #41 在 v2.9.0 里尚未合并）。
//
// 密钥从哪来（优先级从高到低）：
//  1. 环境变量 AGENT2API_PROXY_API_KEY（便于开发直跑）；
//  2. ${TRIM_PKGVAR}/apikey（首次启动自动生成 32 字节随机串，0600）；
//  3. 都拿不到 → 不注入任何密钥，面板会 401（子进程按 fail-closed 启动）。
//
// 安全约束：密钥只在本进程与子进程内存中使用，不下发到浏览器、不写日志。
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"agent2api-fnos/internal/gateway"
)

// version 构建期注入：-ldflags "-X main.version=..."
var version = "dev"

const (
	// gatewayPrefix 必须与 app/ui/config 里的 gatewayPrefix 一致。
	gatewayPrefix = "/app/agent2api"
	// defaultPort 与 manifest 的 service_port 一致（TRIM_SERVICE_PORT 优先）。
	// 取上游 agent2api 的默认端口（AGENT2API_PROXY_PORT 默认 3065），
	// 下游客户端按上游文档照抄 base_url 就能直连。
	defaultPort = 3065
	// keyFileName 自动生成的网关密钥文件名（落在 ${TRIM_PKGVAR} 下，0600）。
	keyFileName = "apikey"
	// readyTimeout 子进程就绪等待上限；应小于 cmd/main 的失效窗口。
	readyTimeout = 20 * time.Second
	// stopTimeout 子进程优雅退出等待上限。
	stopTimeout = 15 * time.Second
	// logMaxBytes 日志单文件上限（超出轮转到 .1）。
	logMaxBytes = 8 << 20
)

type layout struct {
	appDest string
	pkgVar  string
	socket  string
	port    int
	prefix  gateway.Prefix
}

func main() {
	showVersion := flag.Bool("version", false, "打印版本后退出")
	flag.Parse()
	if *showVersion {
		fmt.Printf("agent2api-fnos %s\n", version)
		return
	}

	lay := resolveLayout()
	panelLog, err := newRotatingWriter(filepath.Join(lay.pkgVar, "panel.log"), logMaxBytes)
	if err != nil {
		log.Printf("警告：初始化面板日志失败: %v", err)
	} else {
		log.SetOutput(io.MultiWriter(os.Stderr, panelLog))
		defer func() { _ = panelLog.Close() }()
	}

	log.Printf("=== Agent2API 飞牛网关 %s 启动 ===", version)
	log.Printf("  安装目录:   %s", lay.appDest)
	log.Printf("  数据目录:   %s", lay.pkgVar)
	log.Printf("  网关前缀:   %s", lay.prefix.Path)
	log.Printf("  Socket:     %s", lay.socket)
	log.Printf("  下游端口:   %d（仅 /v1/* 与 /health）", lay.port)

	// ---------------------------------------------------------------------
	// 0) 网关密钥：服务端注入用，绝不下发到浏览器
	// ---------------------------------------------------------------------
	apiKey, keySource := resolveAPIKey(lay.pkgVar)
	if apiKey == "" {
		log.Printf("⚠️  未取得网关密钥（面板会返回 401）: %s", keySource)
	} else {
		log.Printf("  网关密钥:   已就绪（来源: %s，不写日志）", keySource)
	}

	// ---------------------------------------------------------------------
	// 1) 上游 agent2api-server 子进程
	// ---------------------------------------------------------------------
	childLog, err := newRotatingWriter(filepath.Join(lay.pkgVar, "agent2api.log"), logMaxBytes)
	if err != nil {
		log.Printf("警告：初始化上游日志失败: %v；上游输出将只进面板日志", err)
	} else {
		defer func() { _ = childLog.Close() }()
	}

	internalPort, err := pickPort()
	if err != nil {
		log.Fatalf("分配内部端口失败: %v", err)
	}
	internalAddr := "127.0.0.1:" + strconv.Itoa(internalPort)

	child := startChild(lay, internalPort, apiKey, childLog)
	if child != nil {
		if readyErr := waitReady(internalAddr, readyTimeout); readyErr != nil {
			log.Printf("⚠️  上游未就绪: %v（面板仍会打开并显示错误页）", readyErr)
		} else {
			log.Printf("✅ 上游 agent2api 就绪: http://%s", internalAddr)
		}
	}

	// ---------------------------------------------------------------------
	// 2) 面板入口（Unix Socket，仅管理员）
	// ---------------------------------------------------------------------
	panelProxy, err := gateway.NewProxy(gateway.Options{
		Prefix:       lay.prefix,
		InternalAddr: internalAddr,
		RewriteHTML:  true,
		Bridge: func() string {
			// 顺序要紧：SeedScript 必须先于服务端注入的 Web 壳执行，
			// 否则面板读到的 localStorage 还是空的（见 bridge.go 的 SeedScript）。
			return gateway.SeedScript() + gateway.BridgeScript(lay.prefix)
		},
		Prepare: func(r *http.Request) {
			// 面板 /api/* 一律以本进程的密钥为准：飞牛网关已确认管理员身份，
			// 浏览器送来的值（占位值或旧值）不作数 —— 这样也天然免疫密钥轮换。
			if apiKey == "" || !strings.HasPrefix(r.URL.Path, "/api/") {
				return
			}
			r.Header.Set("x-api-key", apiKey)
		},
	})
	if err != nil {
		log.Fatalf("构造面板反代失败: %v", err)
	}

	gwLn, err := gateway.ListenUnix(lay.socket)
	if err != nil {
		log.Fatalf("%v", err)
	}
	panelSrv := &http.Server{
		Handler:           gateway.AdminOnly(panelProxy, lay.prefix),
		ReadHeaderTimeout: 30 * time.Second,
		IdleTimeout:       120 * time.Second,
		// 不设 WriteTimeout：面板的对话测试台是流式回答，可能持续数分钟。
	}
	go func() {
		if serveErr := panelSrv.Serve(gwLn); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			log.Fatalf("面板入口退出: %v", serveErr)
		}
	}()
	log.Printf("✅ 统一网关入口就绪: unix:%s → %s", lay.socket, lay.prefix.Path)
	log.Printf("   访问方式: 飞牛桌面「Agent2API」图标（仅管理员）")

	// ---------------------------------------------------------------------
	// 3) 下游独立接入端口（不经过统一网关，供任意 OpenAI 兼容客户端使用）
	// ---------------------------------------------------------------------
	// 只挂 /v1/* 与 /health：面板在网关路径上，这里不暴露 /api/*；
	// 未知 /v1 路径由 agent2api 自己返回真 404。
	publicProxy, err := gateway.NewProxy(gateway.Options{
		InternalAddr: internalAddr,
	})
	if err != nil {
		log.Fatalf("构造下游反代失败: %v", err)
	}
	publicHandler := publicRoutes(publicProxy)

	// 绑所有网卡：这是 manifest.service_port 声明的对外端口，局域网内的下游
	// 客户端直连它（网关路径带飞牛登录态，外部客户端走不通）。安全性由
	// publicRoutes 的路径白名单 + agent2api 自己的密钥校验共同保证：
	// 面板 /api/* 在此端口一律 404，不对外暴露。
	downLn, listenErr := net.Listen("tcp", ":"+strconv.Itoa(lay.port))
	if listenErr != nil {
		// 端口占用不应拖垮面板：用户可在应用中心看到提示并换端口。
		log.Printf("⚠️  下游端口 %d 监听失败（面板不受影响）: %v", lay.port, listenErr)
	}
	var downSrv *http.Server
	if downLn != nil {
		downSrv = &http.Server{
			Handler:           publicHandler,
			ReadHeaderTimeout: 30 * time.Second,
			IdleTimeout:       120 * time.Second,
		}
		go func() {
			if serveErr := downSrv.Serve(downLn); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
				log.Printf("下游接入服务退出: %v", serveErr)
			}
		}()
		log.Printf("✅ 下游接入端口就绪: http://<飞牛IP>:%d/v1（面板密钥鉴权）", lay.port)
	}

	// ---------------------------------------------------------------------
	// 4) 等待退出信号 / 子进程死亡
	// ---------------------------------------------------------------------
	ctx, stop := signalContext()
	defer stop()

	select {
	case <-ctx.Done():
		log.Printf("收到退出信号，正在停止…")
	case childErr := <-childDone(child):
		// 子进程是唯一功能载体：它死了留一个空壳没有意义，
		// 直接退出让应用中心显示为「已停止」，用户可一键重启。
		log.Printf("⚠️  上游 agent2api 退出: %v", childErr)
		if tail := childLog.tailLines(6); tail != "" {
			log.Printf("   上游最后输出（完整日志见 %s）:", filepath.Join(lay.pkgVar, "agent2api.log"))
			for _, line := range strings.Split(tail, "\n") {
				log.Printf("     | %s", line)
			}
		}
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	_ = panelSrv.Shutdown(shutdownCtx)
	if downSrv != nil {
		_ = downSrv.Shutdown(shutdownCtx)
	}
	stopChild(child)

	// 清理 Socket，避免下次启动 bind 失败。
	_ = os.Remove(lay.socket)
	log.Printf("已退出")
}

// publicRoutes 下游对外端口的路径白名单：只放行 /v1/* 与 /health。
//
// 这是本进程唯一对外裸露的入口，白名单必须在网关这层给出确定结论：
// 判定用 path.Clean 后的结果，否则 /v1/../api/keys 这类请求会靠 /v1/ 前缀
// 蒙混过关，再由上游路由归一化后落到面板接口上。转发仍用原始路径，不改
// 上游行为（尾斜杠等由上游照常处理）。
func publicRoutes(proxy http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := path.Clean(r.URL.Path)
		if p == "/health" || strings.HasPrefix(p, "/v1/") {
			proxy.ServeHTTP(w, r)
			return
		}
		gateway.NotFound(w, r)
	})
}

// resolveLayout 解析安装布局；TRIM_* 由飞牛注入，缺失时按开发直跑处理。
func resolveLayout() layout {
	appDest := strings.TrimSpace(os.Getenv("TRIM_APPDEST"))
	if appDest == "" {
		if wd, err := os.Getwd(); err == nil {
			appDest = wd
		} else {
			appDest = "."
		}
	}
	pkgVar := strings.TrimSpace(os.Getenv("TRIM_PKGVAR"))
	if pkgVar == "" {
		pkgVar = filepath.Join(appDest, "var")
	}
	port := defaultPort
	if v := strings.TrimSpace(os.Getenv("TRIM_SERVICE_PORT")); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n < 65536 {
			port = n
		}
	}
	return layout{
		appDest: appDest,
		pkgVar:  pkgVar,
		socket:  filepath.Join(appDest, "agent2api.sock"),
		port:    port,
		prefix:  gateway.NewPrefix(gatewayPrefix),
	}
}

// resolveAPIKey 取得网关密钥，返回 (密钥, 来源说明)。
//
// 顺序：环境变量 → ${TRIM_PKGVAR}/apikey（不存在则生成）。生成失败等异常
// 一律降级为「无密钥」而不是终止进程 —— 面板要能打开，用户才看得到原因。
func resolveAPIKey(pkgVar string) (string, string) {
	if v := strings.TrimSpace(os.Getenv("AGENT2API_PROXY_API_KEY")); v != "" {
		return v, "环境变量 AGENT2API_PROXY_API_KEY"
	}
	keyPath := filepath.Join(pkgVar, keyFileName)
	if raw, err := os.ReadFile(keyPath); err == nil {
		if v := strings.TrimSpace(string(raw)); v != "" {
			return v, keyPath
		}
	}
	key, err := generateKey()
	if err != nil {
		return "", fmt.Sprintf("生成密钥失败: %v", err)
	}
	if err := os.MkdirAll(pkgVar, 0o700); err != nil {
		return "", fmt.Sprintf("创建数据目录失败: %v", err)
	}
	// 0600：密钥只对属主可读，避免同机其它应用读到。
	if err := os.WriteFile(keyPath, []byte(key+"\n"), 0o600); err != nil {
		return "", fmt.Sprintf("写入密钥失败: %v", err)
	}
	return key, keyPath + "（本次新建）"
}

// generateKey 生成 32 字节随机密钥（64 位十六进制），形如上游 sk- 风格但无前缀，
// 两种鉴权头都能用。
func generateKey() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}

// pickPort 让内核分配一个空闲回环端口后立即释放。
//
// 存在极小的竞态窗口（释放到子进程 bind 之间），但比固定端口冲突可控得多；
// 子进程起不来时日志会明确报出 bind 失败。
func pickPort() (int, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer func() { _ = ln.Close() }()
	addr, ok := ln.Addr().(*net.TCPAddr)
	if !ok {
		return 0, errors.New("非 TCP 监听地址")
	}
	return addr.Port, nil
}

// startChild 拉起上游 agent2api-server（工作目录与全部相对路径都由环境变量给定）。
func startChild(lay layout, internalPort int, apiKey string, childLog *rotatingWriter) *exec.Cmd {
	binName := "agent2api-server-linux-" + runtime.GOARCH // amd64 / arm64，与自身架构一致
	bin := filepath.Join(lay.appDest, "bin", binName)
	if !fileExists(bin) {
		// 命名不一致时的兜底：扫 bin 目录，避免因架构名差异起不来。
		if matches, _ := filepath.Glob(filepath.Join(lay.appDest, "bin", "agent2api-server-*")); len(matches) > 0 {
			bin = matches[0]
		}
	}
	if !fileExists(bin) {
		log.Printf("⚠️  未找到上游程序 %s，面板将显示 502", bin)
		return nil
	}

	// 架构自检：二进制与本进程架构不符时，内核 exec 会以 ENOEXEC 失败，
	// 而报错只说「exec format error」，不说是哪个架构错了。这种包多半是
	// 打包时架构标错或放错了文件，先在这里点明，省一轮排查。
	if err := checkELFArch(bin, runtime.GOARCH); err != nil {
		log.Printf("⚠️  %v", err)
	}

	// 数据目录与 HOME 必须存在。上游自己的 db::open 会建配置目录，但它只在
	// 开库那一步建；而各家提供商的 CLI 往 HOME 下写缓存时不会替你建 HOME，
	// 缺目录就会以一个难懂的错误退出。这里统一先建好，两个目录都是幂等的。
	for _, dir := range []string{filepath.Join(lay.pkgVar, "data"), filepath.Join(lay.pkgVar, "home")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			log.Printf("⚠️  创建目录失败 %s: %v（上游可能因此启动失败）", dir, err)
		}
	}

	env := append(os.Environ(), childEnv(lay, apiKey, strconv.Itoa(internalPort))...)

	cmd := exec.Command(bin)
	cmd.Dir = lay.pkgVar
	cmd.Env = env
	cmd.Stdout = childLog
	cmd.Stderr = childLog
	if err := cmd.Start(); err != nil {
		log.Printf("⚠️  启动上游失败: %v，面板将显示 502", err)
		return nil
	}
	log.Printf("  上游进程:   pid=%d %s", cmd.Process.Pid, bin)
	return cmd
}

// checkELFArch 读 ELF 头确认 e_machine 与期望架构一致。
//
// 只处理小端 ELF64（本应用的两个架构都是）；无法解析时返回 nil —— 这不是
// 校验器，只用来把「架构放错」这种常见错误讲清楚，不阻断启动（真错了内核会拦）。
func checkELFArch(path, want string) error {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	head := make([]byte, 20)
	n, readErr := io.ReadFull(f, head)
	// 魔数只有 4 字节，所以「不是 ELF」在短文件上也能判定；判不出来才放手。
	if n >= 4 && !bytes.Equal(head[:4], []byte{0x7f, 'E', 'L', 'F'}) {
		return fmt.Errorf("上游程序 %s 不是 ELF 可执行文件，无法在本机运行", path)
	}
	if readErr != nil {
		return nil // 头都不完整：交给内核判断，这里不下结论
	}
	if head[4] != 2 || head[5] != 1 { // EI_CLASS=ELF64, EI_DATA=小端
		return nil
	}
	machine := uint16(head[18]) | uint16(head[19])<<8
	wantMachine := map[string]uint16{"amd64": 0x3E, "arm64": 0xB7}[want]
	if wantMachine != 0 && machine != wantMachine {
		name := map[uint16]string{0x3E: "amd64", 0xB7: "arm64"}[machine]
		if name == "" {
			name = fmt.Sprintf("machine=0x%x", machine)
		}
		return fmt.Errorf("上游程序架构不符：本机是 %s，%s 是 %s（安装包装错了架构）",
			want, filepath.Base(path), name)
	}
	return nil
}

// childEnv 构造上游子进程环境（相对路径一律显式给绝对路径，不依赖 CWD）。
//
// 变量名对齐上游 server/src/server/config/parse.rs 与 bin/agent2api-server.rs：
//
//	AGENT2API_PROXY_HOME     数据目录（默认 ~/.agent2api）
//	AGENT2API_HOST           监听地址，固定回环：面板只经网关与下游白名单暴露
//	AGENT2API_PROXY_PORT     主监听端口
//	AGENT2API_UI_DIR         面板静态资源目录（默认 exe 旁的 ui/）
//	AGENT2API_PROXY_API_KEY  网关密钥 —— 它进 config.api_key，进而出现在
//	                         active_api_keys() 里，让「无密钥即 fail-closed」的
//	                         启动门失效，同时不必注册管理员账号（见文件头）
//	AGENT2API_CAPTCHA_ENABLED  0：面板不走登录流程，不需要 ALTCHA 人机校验
//
// 刻意**不设** AGENT2API_ADMIN_USER / AGENT2API_ADMIN_PASSWORD：
// 一旦注册了管理员，面板就会启用「账号 + 会话 Cookie」那套登录，而飞牛网关
// 会剥掉 Set-Cookie（上游 PR #41 未合并），反而把简单问题变成登录死循环。
// 也刻意不设 AGENT2API_ALLOW_NO_KEY：那会让 /v1/* 对局域网完全裸奔。
func childEnv(lay layout, apiKey, portText string) []string {
	dataDir := filepath.Join(lay.pkgVar, "data")
	env := []string{
		"AGENT2API_PROXY_HOME=" + dataDir,
		"AGENT2API_HOST=127.0.0.1",
		"AGENT2API_PROXY_PORT=" + portText,
		"AGENT2API_UI_DIR=" + filepath.Join(lay.appDest, "ui"),
		"AGENT2API_CAPTCHA_ENABLED=0",
		// 各提供商的 CLI/凭据都往 HOME 下写，必须给一个可写且持久的位置。
		"HOME=" + filepath.Join(lay.pkgVar, "home"),
	}
	if apiKey != "" {
		env = append(env, "AGENT2API_PROXY_API_KEY="+apiKey)
	}
	return env
}

func waitReady(addr string, timeout time.Duration) error {
	client := &http.Client{Timeout: 5 * time.Second}
	url := "http://" + addr + "/health"
	deadline := time.Now().Add(timeout)
	var last error
	for time.Now().Before(deadline) {
		resp, err := client.Get(url)
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
			last = fmt.Errorf("health 状态码 %d", resp.StatusCode)
		} else {
			last = err
		}
		time.Sleep(400 * time.Millisecond)
	}
	if last == nil {
		last = errors.New("超时")
	}
	return last
}

// childDone 返回子进程退出通道；无子进程时返回永不触发的通道。
func childDone(cmd *exec.Cmd) <-chan error {
	ch := make(chan error, 1)
	if cmd == nil || cmd.Process == nil {
		return ch
	}
	go func() { ch <- cmd.Wait() }()
	return ch
}

// stopChild 先 SIGTERM 让上游优雅收尾（它会一并停掉各提供商的登录子流程），
// 超时强杀。
func stopChild(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	_ = cmd.Process.Signal(syscall.SIGTERM)
	done := make(chan struct{})
	go func() { _, _ = cmd.Process.Wait(); close(done) }()
	select {
	case <-done:
		log.Printf("上游已退出")
	case <-time.After(stopTimeout):
		log.Printf("上游优雅退出超时，强制结束 pid=%d", cmd.Process.Pid)
		_ = cmd.Process.Kill()
	}
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

// signalContext 监听 SIGINT/SIGTERM（cmd/main 用 SIGTERM 请求优雅退出）。
func signalContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}

// throttledWarn 限流日志：密钥读取失败会伴随每个 /api 请求，
// 不能让它把日志刷爆。
type throttledWarn struct {
	mu   sync.Mutex
	last time.Time
	gap  time.Duration
}

func newThrottledWarn(gap time.Duration) func(string, ...any) {
	t := &throttledWarn{gap: gap}
	return func(format string, args ...any) {
		t.mu.Lock()
		defer t.mu.Unlock()
		if time.Since(t.last) < t.gap {
			return
		}
		t.last = time.Now()
		log.Printf(format, args...)
	}
}
