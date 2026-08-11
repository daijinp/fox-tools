package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/md5"
	"crypto/tls"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// 可调整项集中放在这里。接口地址和请求参数按需求固定。
const (
	baseURL                = "https://test.maitian-yun.com"
	requestPath            = "/generic/v0/device/list"
	requestTimeout         = 60 * time.Second
	responsePreviewBytes   = 2048
	previewPublishEvery    = 500 * time.Millisecond
	consoleRefreshEvery    = time.Second
	skipTLSVerification    = true // 与 get_setting/run.py 中 ssl=False 保持一致，仅用于测试环境。
	defaultConcurrency     = 1000
	maximumConcurrency     = 50000
	responseReadBufferSize = 16 * 1024
)

const requestBody = `{"pageSize":1000,"currentPage":1,"total":165039,"condition":{"status":0,"plantName":"","deviceSN":"","odmSN":"","moduleSN":"","country":"","deviceType":"","productType":"","queryDate":{"begin":0,"end":0}}}`

const userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"

var latencyBounds = [...]time.Duration{
	10 * time.Millisecond,
	25 * time.Millisecond,
	50 * time.Millisecond,
	100 * time.Millisecond,
	200 * time.Millisecond,
	500 * time.Millisecond,
	time.Second,
	2 * time.Second,
	5 * time.Second,
	10 * time.Second,
	30 * time.Second,
}

var responseBufferPool = sync.Pool{
	New: func() any {
		buffer := make([]byte, responseReadBufferSize)
		return &buffer
	},
}

var captureWriterPool = sync.Pool{
	New: func() any {
		return &captureWriter{}
	},
}

var fixedRequestBody = []byte(requestBody)

type runConfig struct {
	token       string
	concurrency int
	duration    time.Duration
}

type responseSnapshot struct {
	time       time.Time
	statusCode int
	latency    time.Duration
	bodyBytes  uint64
	body       string
	err        string
}

type runStats struct {
	started         atomic.Uint64
	completed       atomic.Uint64
	http2xx         atomic.Uint64
	http3xx         atomic.Uint64
	http4xx         atomic.Uint64
	http5xx         atomic.Uint64
	otherHTTP       atomic.Uint64
	transportErrors atomic.Uint64
	canceled        atomic.Uint64
	responseBytes   atomic.Uint64
	latencyNanos    atomic.Uint64
	latencyBuckets  [len(latencyBounds) + 1]atomic.Uint64
	lastPublishNano atomic.Int64
	latest          atomic.Value
}

type captureWriter struct {
	preview [responsePreviewBytes]byte
	used    int
	total   uint64
}

func (writer *captureWriter) Write(data []byte) (int, error) {
	writer.total += uint64(len(data))
	if writer.used < len(writer.preview) {
		writer.used += copy(writer.preview[writer.used:], data)
	}
	return len(data), nil
}

func main() {
	reader := bufio.NewReader(os.Stdin)
	config, err := readConfig(reader)
	if err != nil {
		fmt.Fprintf(os.Stderr, "输入无效：%v\n", err)
		pauseBeforeExit(reader)
		os.Exit(1)
	}

	fmt.Printf("\n目标接口：POST %s%s\n", baseURL, requestPath)
	fmt.Printf("并发数：%d，持续时间：%s，请求超时：%s\n", config.concurrency, formatDuration(config.duration), requestTimeout)
	if skipTLSVerification {
		fmt.Println("提示：测试环境 TLS 证书校验已关闭。按 Ctrl+C 可提前停止。")
	}

	startedAt := time.Now()
	stats := execute(config, startedAt)
	elapsed := time.Since(startedAt)
	summary := buildSummary(config, stats, startedAt, elapsed)

	fmt.Println("\n" + summary)
	logPath, err := writeSummaryLog(summary, startedAt)
	if err != nil {
		fmt.Fprintf(os.Stderr, "日志写入失败：%v\n", err)
	} else {
		absolutePath, pathErr := filepath.Abs(logPath)
		if pathErr == nil {
			logPath = absolutePath
		}
		fmt.Printf("日志文件：%s\n", logPath)
	}

	pauseBeforeExit(reader)
}

func readConfig(reader *bufio.Reader) (runConfig, error) {
	var config runConfig

	token, err := promptRequired(reader, "请输入 token：")
	if err != nil {
		return config, err
	}
	config.token = token

	for {
		value, readErr := prompt(reader, fmt.Sprintf("请输入并发数（默认 %d）：", defaultConcurrency))
		if readErr != nil {
			return config, readErr
		}
		if value == "" {
			config.concurrency = defaultConcurrency
			break
		}

		concurrency, parseErr := strconv.Atoi(value)
		if parseErr == nil && concurrency > 0 && concurrency <= maximumConcurrency {
			config.concurrency = concurrency
			break
		}
		fmt.Printf("并发数必须是 1 到 %d 之间的整数。\n", maximumConcurrency)
	}

	for {
		value, readErr := prompt(reader, "请输入持续时间（分钟，可填写 0.5）：")
		if readErr != nil {
			return config, readErr
		}
		minutes, parseErr := strconv.ParseFloat(value, 64)
		if parseErr == nil && minutes > 0 && minutes <= 24*60 {
			config.duration = time.Duration(minutes * float64(time.Minute))
			if config.duration > 0 {
				break
			}
		}
		fmt.Println("持续时间必须是大于 0 且不超过 1440 的数字（单位：分钟）。")
	}

	return config, nil
}

func promptRequired(reader *bufio.Reader, label string) (string, error) {
	for {
		value, err := prompt(reader, label)
		if err != nil {
			return "", err
		}
		if value != "" {
			return value, nil
		}
		fmt.Println("该项不能为空。")
	}
}

func prompt(reader *bufio.Reader, label string) (string, error) {
	fmt.Print(label)
	value, err := reader.ReadString('\n')
	value = strings.TrimSpace(value)
	if err != nil && !errors.Is(err, io.EOF) {
		return "", err
	}
	if errors.Is(err, io.EOF) && value == "" {
		return "", errors.New("输入已结束")
	}
	return value, nil
}

func execute(config runConfig, startedAt time.Time) *runStats {
	stats := &runStats{}
	transport := newTransport(config.concurrency)
	client := &http.Client{Transport: transport}

	interruptContext, stopInterrupt := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stopInterrupt()
	runContext, stopRun := context.WithTimeout(interruptContext, config.duration)
	defer stopRun()

	var workers sync.WaitGroup
	workers.Add(config.concurrency)
	for workerID := 0; workerID < config.concurrency; workerID++ {
		go func(id int) {
			defer workers.Done()
			seed := time.Now().UnixNano() + int64(id+1)*7919
			random := rand.New(rand.NewSource(seed))
			for {
				select {
				case <-runContext.Done():
					return
				default:
				}
				stats.started.Add(1)
				performRequest(runContext, client, config.token, random, stats)
			}
		}(workerID)
	}

	displayDone := make(chan struct{})
	displayStopped := make(chan struct{})
	go func() {
		defer close(displayStopped)
		displayProgress(stats, startedAt, displayDone)
	}()

	<-runContext.Done()
	workers.Wait()
	close(displayDone)
	<-displayStopped
	transport.CloseIdleConnections()
	return stats
}

func newTransport(concurrency int) *http.Transport {
	dialer := &net.Dialer{
		Timeout:   10 * time.Second,
		KeepAlive: 30 * time.Second,
	}
	return &http.Transport{
		Proxy:               http.ProxyFromEnvironment,
		DialContext:         dialer.DialContext,
		ForceAttemptHTTP2:   true,
		MaxIdleConns:        concurrency * 2,
		MaxIdleConnsPerHost: concurrency,
		MaxConnsPerHost:     concurrency,
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: skipTLSVerification, //nolint:gosec -- 仅连接固定的测试域名。
		},
	}
}

func performRequest(runContext context.Context, client *http.Client, token string, random *rand.Rand, stats *runStats) {
	requestContext, cancel := context.WithTimeout(runContext, requestTimeout)
	defer cancel()

	// Python 的 round(time.time() * 1000) 是四舍五入到毫秒，而不是直接截断。
	timestamp := strconv.FormatInt((time.Now().UnixNano()+500_000)/1_000_000, 10)
	signature := signatureValue(requestPath, token, "en", timestamp, random.Intn(1_000_000))
	request, err := http.NewRequestWithContext(
		requestContext,
		http.MethodPost,
		baseURL+requestPath,
		bytes.NewReader(fixedRequestBody),
	)
	if err != nil {
		recordTransportFailure(stats, time.Time{}, err, false)
		return
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("token", token)
	request.Header.Set("lang", "en")
	request.Header.Set("timestamp", timestamp)
	request.Header.Set("signature", signature)
	request.Header.Set("User-Agent", userAgent)

	requestStarted := time.Now()
	response, err := client.Do(request)
	if err != nil {
		recordTransportFailure(stats, requestStarted, err, runContext.Err() != nil)
		return
	}
	defer response.Body.Close()

	capture := captureWriterPool.Get().(*captureWriter)
	capture.used = 0
	capture.total = 0
	readBuffer := responseBufferPool.Get().(*[]byte)
	_, readErr := io.CopyBuffer(capture, response.Body, *readBuffer)
	responseBufferPool.Put(readBuffer)
	latency := time.Since(requestStarted)

	stats.completed.Add(1)
	stats.responseBytes.Add(capture.total)
	stats.latencyNanos.Add(uint64(latency))
	stats.recordLatency(latency)
	stats.recordHTTPStatus(response.StatusCode)

	if readErr != nil {
		if runContext.Err() != nil {
			stats.canceled.Add(1)
		} else {
			stats.transportErrors.Add(1)
		}
	}
	if stats.claimSnapshotSlot() {
		snapshot := responseSnapshot{
			time:       time.Now(),
			statusCode: response.StatusCode,
			latency:    latency,
			bodyBytes:  capture.total,
			body:       oneLinePreview(capture.preview[:capture.used]),
		}
		if readErr != nil {
			snapshot.err = "读取响应失败: " + readErr.Error()
		}
		stats.latest.Store(snapshot)
	}
	captureWriterPool.Put(capture)
}

func recordTransportFailure(stats *runStats, startedAt time.Time, err error, canceled bool) {
	latency := time.Duration(0)
	if !startedAt.IsZero() {
		latency = time.Since(startedAt)
		stats.latencyNanos.Add(uint64(latency))
		stats.recordLatency(latency)
	}
	stats.completed.Add(1)
	if canceled {
		stats.canceled.Add(1)
	} else {
		stats.transportErrors.Add(1)
	}
	if stats.claimSnapshotSlot() {
		stats.latest.Store(responseSnapshot{
			time:    time.Now(),
			latency: latency,
			err:     err.Error(),
		})
	}
}

func signatureValue(path, token, lang, timestamp string, suffix int) string {
	// fr_requests.py 使用的是 raw f-string，参与 MD5 的是字面量 "\\r\\n"。
	source := path + "\\r\\n" + token + "\\r\\n" + lang + "\\r\\n" + timestamp
	digest := md5.Sum([]byte(source))
	return hex.EncodeToString(digest[:]) + "." + strconv.Itoa(suffix)
}

func (stats *runStats) recordHTTPStatus(statusCode int) {
	switch {
	case statusCode >= 200 && statusCode < 300:
		stats.http2xx.Add(1)
	case statusCode >= 300 && statusCode < 400:
		stats.http3xx.Add(1)
	case statusCode >= 400 && statusCode < 500:
		stats.http4xx.Add(1)
	case statusCode >= 500 && statusCode < 600:
		stats.http5xx.Add(1)
	default:
		stats.otherHTTP.Add(1)
	}
}

func (stats *runStats) recordLatency(latency time.Duration) {
	for index, bound := range latencyBounds {
		if latency <= bound {
			stats.latencyBuckets[index].Add(1)
			return
		}
	}
	stats.latencyBuckets[len(latencyBounds)].Add(1)
}

func (stats *runStats) claimSnapshotSlot() bool {
	now := time.Now().UnixNano()
	last := stats.lastPublishNano.Load()
	if last != 0 && now-last < int64(previewPublishEvery) {
		return false
	}
	return stats.lastPublishNano.CompareAndSwap(last, now)
}

func displayProgress(stats *runStats, startedAt time.Time, done <-chan struct{}) {
	ticker := time.NewTicker(consoleRefreshEvery)
	defer ticker.Stop()
	previousCompleted := uint64(0)
	previousTime := startedAt

	for {
		select {
		case <-done:
			return
		case now := <-ticker.C:
			completed := stats.completed.Load()
			interval := now.Sub(previousTime).Seconds()
			currentRPS := float64(completed-previousCompleted) / interval
			fmt.Printf(
				"[%s] 已运行 %s | 已发起 %d | 完成 %d | 当前 RPS %.1f | 2xx %d | 4xx %d | 5xx %d | 网络错误 %d\n",
				now.Format("15:04:05"),
				formatDuration(now.Sub(startedAt)),
				stats.started.Load(),
				completed,
				currentRPS,
				stats.http2xx.Load(),
				stats.http4xx.Load(),
				stats.http5xx.Load(),
				stats.transportErrors.Load(),
			)
			if value := stats.latest.Load(); value != nil {
				snapshot := value.(responseSnapshot)
				if snapshot.err != "" {
					fmt.Printf("  最近响应：ERROR | 耗时 %s | %s\n", snapshot.latency.Round(time.Millisecond), snapshot.err)
				} else {
					fmt.Printf("  最近响应：HTTP %d | 耗时 %s | %d bytes | %s\n", snapshot.statusCode, snapshot.latency.Round(time.Millisecond), snapshot.bodyBytes, snapshot.body)
				}
			}
			previousCompleted = completed
			previousTime = now
		}
	}
}

func buildSummary(config runConfig, stats *runStats, startedAt time.Time, elapsed time.Duration) string {
	completed := stats.completed.Load()
	averageRPS := float64(0)
	averageLatency := time.Duration(0)
	if elapsed > 0 {
		averageRPS = float64(completed) / elapsed.Seconds()
	}
	if completed > 0 {
		averageLatency = time.Duration(stats.latencyNanos.Load() / completed)
	}

	var builder strings.Builder
	builder.WriteString("================ 性能测试结果 ================\n")
	fmt.Fprintf(&builder, "开始时间：%s\n", startedAt.Format("2006-01-02 15:04:05"))
	fmt.Fprintf(&builder, "目标接口：POST %s%s\n", baseURL, requestPath)
	fmt.Fprintf(&builder, "配置：并发 %d，计划时长 %s，实际时长 %s\n", config.concurrency, formatDuration(config.duration), formatDuration(elapsed))
	fmt.Fprintf(&builder, "请求：发起 %d，完成 %d，平均 RPS %.2f\n", stats.started.Load(), completed, averageRPS)
	fmt.Fprintf(&builder, "HTTP：2xx=%d，3xx=%d，4xx=%d，5xx=%d，其他=%d\n", stats.http2xx.Load(), stats.http3xx.Load(), stats.http4xx.Load(), stats.http5xx.Load(), stats.otherHTTP.Load())
	fmt.Fprintf(&builder, "异常：网络/读取错误=%d，结束时取消=%d\n", stats.transportErrors.Load(), stats.canceled.Load())
	fmt.Fprintf(&builder, "响应流量：%s\n", formatBytes(stats.responseBytes.Load()))
	fmt.Fprintf(&builder, "延迟：平均=%s，P50=%s，P90=%s，P95=%s，P99=%s\n", averageLatency.Round(time.Millisecond), stats.percentile(0.50), stats.percentile(0.90), stats.percentile(0.95), stats.percentile(0.99))
	builder.WriteString("说明：延迟分位数按固定区间近似统计；响应正文只在控制台抽样展示，不逐请求写盘。")
	return builder.String()
}

func (stats *runStats) percentile(percentile float64) string {
	total := uint64(0)
	for index := range stats.latencyBuckets {
		total += stats.latencyBuckets[index].Load()
	}
	if total == 0 {
		return "N/A"
	}
	target := uint64(float64(total)*percentile + 0.999999)
	seen := uint64(0)
	for index := range stats.latencyBuckets {
		seen += stats.latencyBuckets[index].Load()
		if seen >= target {
			if index < len(latencyBounds) {
				return "≤" + latencyBounds[index].String()
			}
			return ">" + latencyBounds[len(latencyBounds)-1].String()
		}
	}
	return "N/A"
}

func writeSummaryLog(summary string, startedAt time.Time) (string, error) {
	filename := "performance_" + startedAt.Format("20060102_150405") + ".log"
	return filename, os.WriteFile(filename, []byte(summary+"\n"), 0o644)
}

func oneLinePreview(body []byte) string {
	text := strings.ToValidUTF8(string(body), "�")
	text = strings.ReplaceAll(text, "\r", " ")
	text = strings.ReplaceAll(text, "\n", " ")
	text = strings.TrimSpace(text)
	if len(body) == responsePreviewBytes {
		text += " ...（仅展示前 2048 bytes）"
	}
	return text
}

func formatDuration(duration time.Duration) string {
	duration = duration.Round(time.Second)
	hours := int(duration / time.Hour)
	minutes := int(duration%time.Hour) / int(time.Minute)
	seconds := int(duration%time.Minute) / int(time.Second)
	if hours > 0 {
		return fmt.Sprintf("%02d:%02d:%02d", hours, minutes, seconds)
	}
	return fmt.Sprintf("%02d:%02d", minutes, seconds)
}

func formatBytes(bytes uint64) string {
	const unit = 1024
	if bytes < unit {
		return fmt.Sprintf("%d B", bytes)
	}
	divisor, exponent := uint64(unit), 0
	for value := bytes / unit; value >= unit; value /= unit {
		divisor *= unit
		exponent++
	}
	return fmt.Sprintf("%.2f %ciB", float64(bytes)/float64(divisor), "KMGTPE"[exponent])
}

func pauseBeforeExit(reader *bufio.Reader) {
	info, err := os.Stdin.Stat()
	if err != nil || info.Mode()&os.ModeCharDevice == 0 {
		return
	}
	fmt.Print("按回车键退出...")
	_, _ = reader.ReadString('\n')
}
