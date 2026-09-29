// Command devcheck 用浏览器验证面板在统一网关下的行为。
//
// 把 fngateway 的面板链路（前缀剥离 → HTML/重定向/Cookie 改写 → 桥接脚本注入
// → 服务端注入面板密钥 → 管理员门）原样挂到一个回环 TCP 端口上，用于在没有
// 飞牛设备时复现与验证面板问题；顺带补上 fngateway 唯一缺失的浏览器级验证能力
// （Unix Socket 浏览器连不上，所以线上形态没法直接用浏览器打开）。
//
// 它不参与打包：build.py 只收集 app/bin/ 下编译好的产物，devcheck 只是源码目录里的
// 一个 main 包。
//
//	# 先跑上游（见 README「本地调试」），并记下它的 API Key
//	go run ./devcheck -apikey <面板密钥>
//	# 浏览器打开 http://127.0.0.1:17901/app/agent2api/
//
// 与线上唯一的差异是身份来源：浏览器无法伪造飞牛网关注入的 X-Trim-*，这里由
// 进程自己盖上管理员章（仅回环监听，强制校验，绝不可用于公网）。
package main

import (
	"flag"
	"log"
	"net"
	"net/http"
	"strings"

	"agent2api-fnos/internal/gateway"
)

func main() {
	upstream := flag.String("upstream", "127.0.0.1:17899", "上游 agent2api 地址")
	apikey := flag.String("apikey", "", "上游 agent2api 的 API Key（用于注入面板 /api/*）")
	addr := flag.String("addr", "127.0.0.1:17901", "监听地址（只允许回环）")
	prefix := flag.String("prefix", "/app/agent2api", "网关前缀")
	flag.Parse()

	host, _, err := net.SplitHostPort(*addr)
	if err != nil {
		log.Fatalf("监听地址格式错误 %q: %v", *addr, err)
	}
	switch host {
	case "127.0.0.1", "localhost", "::1":
	default:
		log.Fatalf("devcheck 只允许监听回环地址，收到 %q", *addr)
	}

	prefixObj := gateway.NewPrefix(*prefix)
	key := strings.TrimSpace(*apikey)
	if key == "" {
		log.Printf("警告：未提供 -apikey，面板 /api/* 将返回 401（页面仍可打开，用于验证注入链路）")
	}

	proxy, err := gateway.NewProxy(gateway.Options{
		Prefix:       prefixObj,
		InternalAddr: *upstream,
		RewriteHTML:  true,
		Bridge:       func() string { return gateway.SeedScript() + gateway.BridgeScript(prefixObj) },
		Prepare: func(r *http.Request) {
			// 与线上 main.go 同一套逻辑：面板 /api/* 一律以本进程的密钥为准。
			if key == "" || !strings.HasPrefix(r.URL.Path, "/api/") {
				return
			}
			r.Header.Set("x-api-key", key)
		},
	})
	if err != nil {
		log.Fatalf("构造面板反代失败: %v", err)
	}

	// 合成飞牛网关注入的会话头，再交给与线上同一个管理员门。
	panel := gateway.AdminOnly(proxy, prefixObj)
	chain := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Header.Set("X-Trim-Userid", "devcheck")
		r.Header.Set("X-Trim-Isadmin", "1")
		r.Header.Set("X-Trim-Username", "devcheck")
		panel.ServeHTTP(w, r)
	})

	ln, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatalf("监听 %s 失败: %v", *addr, err)
	}
	log.Printf("面板: http://%s%s/", *addr, prefixObj.Path)
	log.Printf("上游: http://%s", *upstream)
	if err := http.Serve(ln, chain); err != nil {
		log.Fatalf("服务退出: %v", err)
	}
}
