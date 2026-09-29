package main

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// elfHeader 造一个最小可解析的 ELF 头：4 字节魔数 + EI_CLASS/EI_DATA + e_machine。
//
// 只覆盖 checkELFArch 真正会读的那 20 个字节，其余留零 —— 它本就只做
// 「架构对不对」的提示，不做完整 ELF 校验。
func elfHeader(class, data byte, machine uint16) []byte {
	h := make([]byte, 20)
	copy(h, []byte{0x7f, 'E', 'L', 'F'})
	h[4] = class
	h[5] = data
	h[18] = byte(machine)
	h[19] = byte(machine >> 8)
	return h
}

func writeFile(t *testing.T, dir, name string, data []byte) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, data, 0o755); err != nil {
		t.Fatal(err)
	}
	return p
}

// TestChildEnvDataDirMatchesUpstreamContract 锁定数据目录与 HOME。
//
// 两件事必须同时成立，缺一个都会在真机上变成「装完打不开」：
//   - AGENT2API_PROXY_HOME 必须显式给绝对路径：它同时是「配置目录」和
//     「旧目录迁移的豁免条件」（上游 config_migration::env_dir_override_set），
//     不设的话上游会去动 ~/.workbuddy-proxy → ~/.agent2api 的默认路径；
//   - HOME 必须在应用数据目录下且可写：各家提供商的 CLI 把凭据/缓存写这里，
//     指到只读的安装目录会让登录直接失败。
func TestChildEnvDataDirMatchesUpstreamContract(t *testing.T) {
	// 期望值用 filepath.Join 拼：本测试在开发机（Windows）上也会跑，
	// 而真机是 Linux —— 断言不该把某一种路径分隔符写死。
	lay := layout{
		pkgVar:  filepath.Join("/vol1/@appdata/agent2api"),
		appDest: filepath.Join("/vol1/@appcenter/agent2api"),
	}
	env := childEnv(lay, "k", "3065")

	home, ok := envLookup(env, "HOME")
	if !ok {
		t.Fatal("缺少 HOME：提供商 CLI 无处写凭据")
	}
	if home != filepath.Join(lay.pkgVar, "home") {
		t.Fatalf("HOME = %q，期望落在应用数据目录下", home)
	}

	proxyHome, ok := envLookup(env, "AGENT2API_PROXY_HOME")
	if !ok {
		t.Fatal("缺少 AGENT2API_PROXY_HOME")
	}
	if proxyHome != filepath.Join(lay.pkgVar, "data") {
		t.Fatalf("AGENT2API_PROXY_HOME = %q，期望 <pkgVar>/data", proxyHome)
	}
	// 绝对路径这件事只在 Linux 上断言：Windows 的 filepath.IsAbs 要求「盘符+根」，
	// 而 \vol1\... 这种「根相对」路径在那边不算绝对 —— 那是本测试的宿主差异，
	// 不是被测代码的问题（真机是 Linux）。
	if runtime.GOOS != "windows" && !filepath.IsAbs(proxyHome) {
		t.Fatalf("AGENT2API_PROXY_HOME 必须是绝对路径（子进程 CWD 不由我们掌控）: %q", proxyHome)
	}

	// 数据目录与 HOME 都必须在安装目录之外：重装/升级会整体替换安装目录。
	for _, kv := range []string{home, proxyHome} {
		if strings.HasPrefix(kv, lay.appDest) {
			t.Fatalf("持久化路径落在了安装目录内，升级会丢数据: %q", kv)
		}
	}

	// 回环监听：面板只经 Unix Socket 与下游白名单暴露，不额外开监听面。
	if host, _ := envLookup(env, "AGENT2API_HOST"); host != "127.0.0.1" {
		t.Fatalf("AGENT2API_HOST = %q，期望 127.0.0.1", host)
	}
	// 面板走网关免密，不需要 ALTCHA 人机校验。
	if captcha, _ := envLookup(env, "AGENT2API_CAPTCHA_ENABLED"); captcha != "0" {
		t.Fatalf("AGENT2API_CAPTCHA_ENABLED = %q，期望 0", captcha)
	}
	// 面板静态资源指向安装目录下的 ui/。
	if ui, _ := envLookup(env, "AGENT2API_UI_DIR"); ui != filepath.Join(lay.appDest, "ui") {
		t.Fatalf("AGENT2API_UI_DIR = %q，期望 <appDest>/ui", ui)
	}
}

// 为什么值得测：真机首装时若把 arm64 包装到 x86 机器上，内核只会给一句
// 「exec format error」，不说是哪个架构错了。这条诊断把话说清楚。
func TestCheckELFArch(t *testing.T) {
	dir := t.TempDir()

	amd64 := writeFile(t, dir, "amd64", elfHeader(2, 1, 0x3E))
	arm64 := writeFile(t, dir, "arm64", elfHeader(2, 1, 0xB7))
	notELF := writeFile(t, dir, "text", []byte("#!/bin/sh\necho hi\n"))
	// 回归用例：短于 20 字节的非 ELF 文件也必须报错（魔数只有 4 字节，
	// 早期实现把「读不满 20 字节」当成「判不出来」直接放行，是个漏判）。
	notELFShort := writeFile(t, dir, "textshort", []byte("#!/bi"))
	tooShort := writeFile(t, dir, "short", []byte{0x7f, 'E'})
	elf32 := writeFile(t, dir, "elf32", elfHeader(1, 1, 0x3E))
	bigEndian := writeFile(t, dir, "be", elfHeader(2, 2, 0x3E))

	cases := []struct {
		name string
		path string
		want string
		bad  bool
	}{
		{"amd64 包在 amd64 上", amd64, "amd64", false},
		{"arm64 包在 arm64 上", arm64, "arm64", false},
		{"arm64 包装到 amd64 机器", arm64, "amd64", true},
		{"amd64 包装到 arm64 机器", amd64, "arm64", true},
		{"非 ELF 文件必须报错", notELF, "amd64", true},
		{"短于 20 字节的非 ELF 也要报错", notELFShort, "amd64", true},
		// 读不出完整 ELF 头时不阻断：内核会拦住，这里不抢着下结论。
		{"文件太短则跳过", tooShort, "amd64", false},
		{"ELF32 不解析则跳过", elf32, "amd64", false},
		{"大端 ELF 不解析则跳过", bigEndian, "amd64", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := checkELFArch(c.path, c.want)
			if c.bad && err == nil {
				t.Fatalf("期望报错，实际通过")
			}
			if !c.bad && err != nil {
				t.Fatalf("期望通过，实际报错: %v", err)
			}
		})
	}

	// 文件不存在时不能崩：调用点在 startChild 里，早于任何存在性判断。
	if err := checkELFArch(filepath.Join(dir, "nope"), "amd64"); err != nil {
		t.Fatalf("文件不存在时应返回 nil，实际: %v", err)
	}
}

// TestRotatingWriterTailLines 锁定子进程死因的摘录行为。
//
// 上游启动失败的原因只写在它自己的 stderr（落进 agent2api.log），网关若只记
// 一句「上游退出: exit status 1」，用户还得去翻另一个文件。tailLines 负责把
// 尾部几行直接贴进网关日志，这里锁定「取的是最后 n 行」且不越界。
func TestRotatingWriterTailLines(t *testing.T) {
	dir := t.TempDir()
	w, err := newRotatingWriter(filepath.Join(dir, "agent2api.log"), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	// 空文件：返回空串，不能返回占位内容。
	if got := w.tailLines(6); got != "" {
		t.Fatalf("空日志应返回空串，实际 %q", got)
	}

	for _, line := range []string{"1", "2", "3", "4", "5", "6", "7", "8"} {
		if _, err := w.Write([]byte("第" + line + "行\n")); err != nil {
			t.Fatal(err)
		}
	}

	got := w.tailLines(3)
	want := "第6行\n第7行\n第8行"
	if got != want {
		t.Fatalf("tailLines(3) = %q，期望 %q", got, want)
	}

	// n 大于总行数时给全部行，不能报错或越界。
	all := w.tailLines(100)
	if lines := strings.Split(all, "\n"); len(lines) != 8 {
		t.Fatalf("tailLines(100) 应返回全部 8 行，实际 %d 行: %q", len(lines), all)
	}
	if !strings.HasPrefix(all, "第1行\n") || !strings.HasSuffix(all, "第8行") {
		t.Fatalf("tailLines(100) 内容不对: %q", all)
	}

	// 末尾换行不该造出一个空行。
	if lines := strings.Split(w.tailLines(2), "\n"); len(lines) != 2 {
		t.Fatalf("末尾换行被当成一行：%d 行", len(lines))
	}

	// 轮转之后仍能读到当前文件（.1 里的旧内容不在本次摘录范围内）。
	if _, err := w.Write(make([]byte, 2<<20)); err != nil {
		t.Fatal(err)
	}
	if _, statErr := os.Stat(filepath.Join(dir, "agent2api.log.1")); statErr != nil {
		t.Fatalf("超过上限应触发轮转: %v", statErr)
	}
}
