package main

import (
	"fmt"
	"os"
	"sync"
)

// rotatingWriter 单代轮转的日志写入器（path + path.1）。
//
// 为什么需要：飞牛设备常年在线，面板与上游日志此前会无限增长；
// 单代轮转（.1 覆盖）足够定位问题，又不引入多代归档的复杂度。
type rotatingWriter struct {
	mu   sync.Mutex
	path string
	max  int64
	size int64
	file *os.File
}

func newRotatingWriter(path string, max int64) (*rotatingWriter, error) {
	w := &rotatingWriter{path: path, max: max}
	if err := w.reopen(); err != nil {
		return nil, err
	}
	return w, nil
}

func (w *rotatingWriter) reopen() error {
	f, err := os.OpenFile(w.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	w.file = f
	w.size = 0
	if info, statErr := f.Stat(); statErr == nil {
		w.size = info.Size()
	}
	return nil
}

func (w *rotatingWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		return len(p), nil
	}
	if w.size+int64(len(p)) > w.max {
		w.rotateLocked()
	}
	n, err := w.file.Write(p)
	w.size += int64(n)
	return n, err
}

func (w *rotatingWriter) rotateLocked() {
	_ = w.file.Close()
	w.file = nil
	if err := os.Rename(w.path, w.path+".1"); err != nil && !os.IsNotExist(err) {
		// 不能走 log 包：标准日志的输出目标正是本 writer，会递归。
		fmt.Fprintf(os.Stderr, "警告：日志轮转失败: %v\n", err)
	}
	if err := w.reopen(); err != nil {
		fmt.Fprintf(os.Stderr, "警告：重开日志文件失败: %v\n", err)
	}
}

func (w *rotatingWriter) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		return nil
	}
	err := w.file.Close()
	w.file = nil
	return err
}
