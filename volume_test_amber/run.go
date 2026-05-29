package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/md5"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"html/template"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	debug          = false
	sleepTime      = 0 * time.Second
	configFileName = "config.json"
)

// ---------------- 配置 ----------------

// Config 对应 config.json，所有字段均从文件加载；严格模式下缺失/非法值会直接报错退出。
type Config struct {
	DeviceCount             int    `json:"device_count"`
	DurationSeconds         int    `json:"duration_seconds"`
	SNFile                  string `json:"sn_file"`
	SNStartIndex            int    `json:"sn_start_index"`
	ReportDir               string `json:"report_dir"`
	Domain                  string `json:"domain"`
	APIToken                string `json:"api_token"`
	APIPath                 string `json:"api_path"`
	ProgressIntervalSeconds int    `json:"progress_interval_seconds"`
	RequestTimeoutSeconds   int    `json:"request_timeout_seconds"`
	DropWithLogErrnos       []int  `json:"drop_with_log_errnos"`
	DropSilentErrnos        []int  `json:"drop_silent_errnos"`
}

// TestDuration 返回压测总时长。
func (c *Config) TestDuration() time.Duration {
	return time.Duration(c.DurationSeconds) * time.Second
}

// ProgressInterval 返回进度打印间隔。
func (c *Config) ProgressInterval() time.Duration {
	return time.Duration(c.ProgressIntervalSeconds) * time.Second
}

// RequestTimeout 返回单个请求的超时时间。
func (c *Config) RequestTimeout() time.Duration {
	return time.Duration(c.RequestTimeoutSeconds) * time.Second
}

func loadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取配置文件失败 (%s): %w", path, err)
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var cfg Config
	if err := dec.Decode(&cfg); err != nil {
		return nil, fmt.Errorf("解析配置文件失败 (%s): %w", path, err)
	}

	var missing []string
	check := func(cond bool, name string) {
		if cond {
			missing = append(missing, name)
		}
	}
	check(cfg.DeviceCount <= 0, "device_count (> 0)")
	check(cfg.DurationSeconds <= 0, "duration_seconds (> 0)")
	check(strings.TrimSpace(cfg.SNFile) == "", "sn_file")
	check(cfg.SNStartIndex <= 0, "sn_start_index (>= 1, 1-based)")
	check(strings.TrimSpace(cfg.ReportDir) == "", "report_dir")
	check(strings.TrimSpace(cfg.Domain) == "", "domain")
	check(strings.TrimSpace(cfg.APIToken) == "", "api_token")
	check(strings.TrimSpace(cfg.APIPath) == "", "api_path")
	check(cfg.ProgressIntervalSeconds <= 0, "progress_interval_seconds (> 0)")
	check(cfg.RequestTimeoutSeconds <= 0, "request_timeout_seconds (> 0)")
	if len(missing) > 0 {
		return nil, fmt.Errorf("配置文件缺少或非法的字段: %s", strings.Join(missing, ", "))
	}
	if err := validateDiscardErrnos("drop_with_log_errnos", cfg.DropWithLogErrnos); err != nil {
		return nil, err
	}
	if err := validateDiscardErrnos("drop_silent_errnos", cfg.DropSilentErrnos); err != nil {
		return nil, err
	}
	if dup := findOverlapErrno(cfg.DropWithLogErrnos, cfg.DropSilentErrnos); len(dup) > 0 {
		return nil, fmt.Errorf("配置冲突：errno 不能同时出现在 drop_with_log_errnos 和 drop_silent_errnos: %v", dup)
	}
	return &cfg, nil
}

func validateDiscardErrnos(field string, errnos []int) error {
	seen := make(map[int]struct{}, len(errnos))
	for _, errno := range errnos {
		if errno == 0 {
			return fmt.Errorf("配置非法：%s 不能包含 0，errno=0 固定视为成功", field)
		}
		if _, ok := seen[errno]; ok {
			return fmt.Errorf("配置非法：%s 包含重复 errno=%d", field, errno)
		}
		seen[errno] = struct{}{}
	}
	return nil
}

func findOverlapErrno(a, b []int) []int {
	left := make(map[int]struct{}, len(a))
	for _, errno := range a {
		left[errno] = struct{}{}
	}
	var overlap []int
	for _, errno := range b {
		if _, ok := left[errno]; ok {
			overlap = append(overlap, errno)
		}
	}
	sort.Ints(overlap)
	return overlap
}

// 运行期注入的配置值（避免把 *Config 透传到每个底层函数）。
var (
	cfgDomain   string
	cfgAPIToken string
	// baseDir 是 config.json 所在目录，用于把 cfg 里的相对路径锚定在此
	// （避免受执行时 CWD 影响，导致 reports 被建到仓库根目录）。
	baseDir string
)

// anchorPath 把相对路径锚定到 baseDir；绝对路径原样返回。
func anchorPath(p string) string {
	if p == "" {
		return p
	}
	if filepath.IsAbs(p) {
		return p
	}
	return filepath.Join(baseDir, p)
}

// ---------------- HTTP 基础设施（保留原有逻辑） ----------------

type auth struct{}
type rawJSON string

func (a auth) getSignature(token, path, lang string) map[string]string {
	if lang == "" {
		lang = "en"
	}

	now := time.Now().UnixNano()
	timestamp := (now + int64(500*time.Microsecond)) / int64(time.Millisecond)
	signature := fmt.Sprintf(`%s\r\n%s\r\n%d`, path, token, timestamp)

	return map[string]string{
		"token":      token,
		"lang":       lang,
		"timestamp":  fmt.Sprintf("%d", timestamp),
		"signature":  md5c(signature),
		"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
	}
}

func md5c(text string) string {
	sum := md5.Sum([]byte(text))
	return hex.EncodeToString(sum[:])
}

var httpClient *http.Client

func buildHTTPClient(timeout time.Duration) *http.Client {
	return &http.Client{
		Transport: &http.Transport{
			TLSClientConfig:     &tls.Config{InsecureSkipVerify: true},
			ForceAttemptHTTP2:   false,
			MaxIdleConns:        512,
			MaxIdleConnsPerHost: 512,
			IdleConnTimeout:     90 * time.Second,
		},
		Timeout: timeout,
	}
}

func frRequests(method, path string, param any) (*http.Response, []byte, error) {
	fullURL := cfgDomain + path
	headers := auth{}.getSignature(cfgAPIToken, path, "en")

	if sleepTime > 0 {
		time.Sleep(sleepTime)
	}

	var (
		req *http.Request
		err error
	)

	switch strings.ToLower(method) {
	case "get":
		if param != nil {
			values := url.Values{}
			switch query := param.(type) {
			case map[string]string:
				for k, v := range query {
					values.Set(k, v)
				}
			case map[string]any:
				for k, v := range query {
					values.Set(k, fmt.Sprintf("%v", v))
				}
			default:
				return nil, nil, fmt.Errorf("unsupported get params type: %T", param)
			}
			fullURL += "?" + values.Encode()
		}
		req, err = http.NewRequest(http.MethodGet, fullURL, nil)
	case "post":
		var body []byte
		switch value := param.(type) {
		case rawJSON:
			body = []byte(value)
		default:
			marshaled, marshalErr := marshalPythonJSON(param)
			if marshalErr != nil {
				return nil, nil, marshalErr
			}
			body = marshaled
		}
		req, err = http.NewRequest(http.MethodPost, fullURL, bytes.NewReader(body))
		if err == nil {
			req.Header["Content-Type"] = []string{"application/json"}
		}
	default:
		return nil, nil, fmt.Errorf("request method error")
	}

	if err != nil {
		return nil, nil, err
	}

	for k, v := range headers {
		req.Header[k] = []string{v}
	}
	req.Header["Accept"] = []string{"*/*"}
	req.Header["Connection"] = []string{"keep-alive"}

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, nil, err
	}

	respBody, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		return resp, nil, err
	}

	resp.Body = io.NopCloser(bytes.NewReader(respBody))

	if debug {
		result := map[string]any{
			"url":      fullURL,
			"method":   method,
			"param":    param,
			"headers":  headers,
			"response": string(respBody),
		}
		pretty, _ := json.MarshalIndent(result, "", " ")
		fmt.Println(string(pretty))
		fmt.Println(strings.Repeat("-------------------------", 5))
	}

	return resp, respBody, nil
}

func marshalPythonJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := writePythonJSON(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writePythonJSON(buf *bytes.Buffer, v any) error {
	switch value := v.(type) {
	case nil:
		buf.WriteString("null")
	case string:
		encoded, err := json.Marshal(value)
		if err != nil {
			return err
		}
		buf.Write(encoded)
	case bool:
		if value {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case int:
		buf.WriteString(strconv.Itoa(value))
	case int8, int16, int32, int64:
		buf.WriteString(fmt.Sprintf("%d", value))
	case uint, uint8, uint16, uint32, uint64:
		buf.WriteString(fmt.Sprintf("%d", value))
	case float32, float64:
		buf.WriteString(strconv.FormatFloat(reflectFloat64(value), 'f', -1, 64))
	case map[string]any:
		return writePythonJSONObject(buf, value)
	case map[string]string:
		converted := make(map[string]any, len(value))
		for k, v := range value {
			converted[k] = v
		}
		return writePythonJSONObject(buf, converted)
	case []any:
		return writePythonJSONArray(buf, value)
	case []map[string]any:
		items := make([]any, 0, len(value))
		for _, item := range value {
			items = append(items, item)
		}
		return writePythonJSONArray(buf, items)
	default:
		encoded, err := json.Marshal(value)
		if err != nil {
			return err
		}
		buf.Write(encoded)
	}
	return nil
}

func writePythonJSONObject(buf *bytes.Buffer, m map[string]any) error {
	buf.WriteByte('{')
	i := 0
	for k, v := range m {
		if i > 0 {
			buf.WriteString(", ")
		}
		keyJSON, err := json.Marshal(k)
		if err != nil {
			return err
		}
		buf.Write(keyJSON)
		buf.WriteString(": ")
		if err := writePythonJSON(buf, v); err != nil {
			return err
		}
		i++
	}
	buf.WriteByte('}')
	return nil
}

func writePythonJSONArray(buf *bytes.Buffer, items []any) error {
	buf.WriteByte('[')
	for i, item := range items {
		if i > 0 {
			buf.WriteString(", ")
		}
		if err := writePythonJSON(buf, item); err != nil {
			return err
		}
	}
	buf.WriteByte(']')
	return nil
}

func reflectFloat64(v any) float64 {
	switch n := v.(type) {
	case float32:
		return float64(n)
	case float64:
		return n
	default:
		return 0
	}
}

// ---------------- 业务接口 ----------------

type device struct{}

func (d device) deviceV2SchedulerGet(path, sn string) (*http.Response, []byte, error) {
	requestParam := rawJSON(fmt.Sprintf(`{"deviceSN": "%s"}`, sn))
	return frRequests("post", path, requestParam)
}

func (d device) deviceV2SchedulerSet() (*http.Response, []byte, error) {
	path := "/op/v2/device/scheduler/enable"
	requestParam := rawJSON(`{"groups": [{"fdPwr": 0, "fdSoc": 6, "enable": 0, "maxSoc": 80, "endHour": 3, "workMode": "ForceDischarge", "endMinute": 4, "startHour": 1, "startMinute": 2, "minSocOnGrid": 5}, {"fdPwr": 2422, "fdSoc": 7, "enable": 1, "maxSoc": 92, "endHour": 3, "workMode": "Feedin", "endMinute": 30, "startHour": 2, "startMinute": 22, "minSocOnGrid": 6}], "deviceSN": "your_device_sn"}`)
	return frRequests("post", path, requestParam)
}

// ---------------- 压测核心 ----------------

type requestResult struct {
	SN            string
	Errno         int
	Msg           string
	Elapsed       time.Duration
	Timestamp     time.Time
	HTTPError     string
	Discarded     bool
	DiscardLogged bool
}

// resolvePath 允许脚本从仓库根目录或 volume_test_amber 目录下运行均可找到文件
func resolvePath(name string) string {
	if _, err := os.Stat(name); err == nil {
		return name
	}
	alt := filepath.Join("volume_test_amber", name)
	if _, err := os.Stat(alt); err == nil {
		return alt
	}
	return name
}

func readSNList(path string) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var sns []string
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		// 支持 csv 多列时只取第一列
		if idx := strings.IndexAny(line, ",\t"); idx >= 0 {
			line = strings.TrimSpace(line[:idx])
		}
		if line == "" {
			continue
		}
		sns = append(sns, line)
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return sns, nil
}

type errnoStat struct {
	Errno     int
	Count     int
	Sample    string
	RatioText string
	TagText   string
}

type errorDetail struct {
	Timestamp string
	SN        string
	Errno     int
	Msg       string
	ElapsedMs float64
}

type reportData struct {
	StartTime           string
	EndTime             string
	DurationSec         float64
	DeviceCount         int
	PlannedQPS          string
	ActualQPS           string
	Interval            string
	TotalRequests       int
	EffectiveRequests   int
	SuccessCount        int
	FailCount           int
	SuccessRate         string
	LatencyMinMs        string
	LatencyAvgMs        string
	LatencyP50Ms        string
	LatencyP95Ms        string
	LatencyP99Ms        string
	LatencyMaxMs        string
	ErrnoDist           []errnoStat
	Errors              []errorDetail
	ErrorsShown         int
	ErrorsTotal         int
	DiscardWithLogCount int
	DiscardSilentCount  int
	DropWithLogDist     []errnoStat
	DropSilentDist      []errnoStat
	ErrorLogFile        string
	APIPath             string
	Domain              string
}

// snapshotConfig 把本次运行时的配置写到运行目录下，便于复现。
func snapshotConfig(cfg *Config, path string) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	return enc.Encode(cfg)
}

func percentile(sortedMs []float64, p float64) float64 {
	if len(sortedMs) == 0 {
		return 0
	}
	if p <= 0 {
		return sortedMs[0]
	}
	if p >= 100 {
		return sortedMs[len(sortedMs)-1]
	}
	rank := p / 100 * float64(len(sortedMs)-1)
	lo := int(math.Floor(rank))
	hi := int(math.Ceil(rank))
	if lo == hi {
		return sortedMs[lo]
	}
	return sortedMs[lo] + (sortedMs[hi]-sortedMs[lo])*(rank-float64(lo))
}

func runLoadTest(cfg *Config, sns []string) error {
	deviceCount := cfg.DeviceCount
	startIndex := cfg.SNStartIndex    // 1-based
	startIdx0 := cfg.SNStartIndex - 1 // 0-based 切片用
	dropWithLogSet := errnoSet(cfg.DropWithLogErrnos)
	dropSilentSet := errnoSet(cfg.DropSilentErrnos)

	// 严格下标校验：起点必须落在列表内，且起点 + 数量不能超过列表长度
	if startIdx0 >= len(sns) {
		return fmt.Errorf(
			"sn_start_index=%d 超出 SN 列表长度 %d（列表可用下标 1~%d）",
			startIndex, len(sns), len(sns),
		)
	}
	endIdx := startIdx0 + deviceCount
	if endIdx > len(sns) {
		return fmt.Errorf(
			"sn_start_index=%d + device_count=%d 需要覆盖到下标 %d，但 SN 列表只有 %d 个；"+
				"请减小 device_count 或将 sn_start_index 向前挪（当前起点之后最多可取 %d 个）",
			startIndex, deviceCount, endIdx, len(sns), len(sns)-startIdx0,
		)
	}
	targetSNs := sns[startIdx0:endIdx]

	testDuration := cfg.TestDuration()
	interval := testDuration / time.Duration(deviceCount)
	targetQPS := float64(deviceCount) / testDuration.Seconds()

	fmt.Println(strings.Repeat("=", 60))
	fmt.Printf("压测目标 : %s%s\n", cfg.Domain, cfg.APIPath)
	fmt.Printf("SN 范围  : #%d ~ #%d（共 %d 个，源文件 %s）\n",
		startIndex, startIndex+deviceCount-1, deviceCount, cfg.SNFile)
	fmt.Printf("总时长   : %s\n", testDuration)
	fmt.Printf("发送间隔 : %s\n", interval)
	fmt.Printf("目标 QPS : %.4f\n", targetQPS)
	fmt.Println(strings.Repeat("=", 60))

	reportRoot := anchorPath(cfg.ReportDir)
	stamp := time.Now().Format("20060102_150405")
	runDirName := fmt.Sprintf("run_%s_s%d_n%d_d%ds", stamp, startIndex, deviceCount, cfg.DurationSeconds)
	runDir := filepath.Join(reportRoot, runDirName)
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		return fmt.Errorf("创建报告目录失败: %w", err)
	}
	errLogPath := filepath.Join(runDir, "errors.log")
	reportPath := filepath.Join(runDir, "report.html")
	configSnapshotPath := filepath.Join(runDir, "config.snapshot.json")

	if err := snapshotConfig(cfg, configSnapshotPath); err != nil {
		fmt.Printf("警告：写入配置快照失败: %v\n", err)
	}

	fmt.Printf("本次结果目录 : %s\n", runDir)
	fmt.Println(strings.Repeat("=", 60))

	errLogFile, err := os.Create(errLogPath)
	if err != nil {
		return fmt.Errorf("创建异常日志失败: %w", err)
	}
	defer errLogFile.Close()
	var errLogMu sync.Mutex

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	var (
		sent              int64
		okCnt             int64
		failCnt           int64
		discardWithLogCnt int64
		discardSilentCnt  int64
		wg                sync.WaitGroup
	)

	resultCh := make(chan requestResult, 1024)
	collectDone := make(chan struct{})
	results := make([]requestResult, 0, deviceCount)
	go func() {
		for r := range resultCh {
			results = append(results, r)
		}
		close(collectDone)
	}()

	progressStop := make(chan struct{})
	startTime := time.Now()
	go func() {
		ticker := time.NewTicker(cfg.ProgressInterval())
		defer ticker.Stop()
		for {
			select {
			case <-progressStop:
				return
			case <-ticker.C:
				elapsed := time.Since(startTime).Seconds()
				s := atomic.LoadInt64(&sent)
				k := atomic.LoadInt64(&okCnt)
				f := atomic.LoadInt64(&failCnt)
				d1 := atomic.LoadInt64(&discardWithLogCnt)
				d2 := atomic.LoadInt64(&discardSilentCnt)
				done := k + f + d1 + d2
				var qps float64
				if elapsed > 0 {
					qps = float64(s) / elapsed
				}
				fmt.Printf("[%5.0fs] sent=%d  done=%d  success=%d  fail=%d  drop(log)=%d  drop(silent)=%d  actualQPS=%.2f\n",
					elapsed, s, done, k, f, d1, d2, qps)
			}
		}
	}()

	d := device{}
	fire := func(sn string) {
		defer wg.Done()
		atomic.AddInt64(&sent, 1)
		reqStart := time.Now()
		_, respBody, err := d.deviceV2SchedulerGet(cfg.APIPath, sn)
		elapsed := time.Since(reqStart)
		r := requestResult{SN: sn, Elapsed: elapsed, Timestamp: reqStart}

		if err != nil {
			r.Errno = -1
			r.HTTPError = err.Error()
			r.Msg = "http error: " + err.Error()
		} else {
			var parsed struct {
				Errno int    `json:"errno"`
				Msg   string `json:"msg"`
			}
			if jerr := json.Unmarshal(respBody, &parsed); jerr != nil {
				r.Errno = -2
				r.Msg = "json parse error: " + jerr.Error() + " | body=" + truncate(string(respBody), 200)
			} else {
				r.Errno = parsed.Errno
				r.Msg = parsed.Msg
			}
		}

		switch {
		case r.Errno == 0:
			atomic.AddInt64(&okCnt, 1)
		case containsErrno(dropWithLogSet, r.Errno):
			r.Discarded = true
			r.DiscardLogged = true
			atomic.AddInt64(&discardWithLogCnt, 1)
		case containsErrno(dropSilentSet, r.Errno):
			r.Discarded = true
			atomic.AddInt64(&discardSilentCnt, 1)
		default:
			atomic.AddInt64(&failCnt, 1)
		}

		if r.Errno != 0 && (r.DiscardLogged || !r.Discarded) {
			line, _ := json.Marshal(map[string]any{
				"timestamp":  r.Timestamp.Format("2006-01-02 15:04:05.000"),
				"sn":         r.SN,
				"errno":      r.Errno,
				"msg":        r.Msg,
				"elapsed_ms": float64(r.Elapsed.Microseconds()) / 1000.0,
			})
			errLogMu.Lock()
			errLogFile.Write(line)
			errLogFile.Write([]byte("\n"))
			errLogMu.Unlock()
		}

		resultCh <- r
	}

	// 首个请求立即发出
	wg.Add(1)
	go fire(targetSNs[0])

	ticker := time.NewTicker(interval)
scheduleLoop:
	for i := 1; i < len(targetSNs); i++ {
		select {
		case <-ctx.Done():
			fmt.Println("\n收到中断信号，停止派发新请求，等待在飞请求完成 ...")
			break scheduleLoop
		case <-ticker.C:
			wg.Add(1)
			go fire(targetSNs[i])
		}
	}
	ticker.Stop()

	wg.Wait()
	close(resultCh)
	<-collectDone
	close(progressStop)
	elapsedTotal := time.Since(startTime)

	fmt.Println(strings.Repeat("=", 60))
	fmt.Printf("压测结束，耗时 %.2fs，生成报告 ...\n", elapsedTotal.Seconds())

	return generateReport(cfg, results, deviceCount, targetQPS, startTime, elapsedTotal, interval, errLogPath, reportPath)
}

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "..."
}

// ---------------- 报告 ----------------

const maxErrorsInHTML = 500

const reportHTMLTemplate = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Amber 下发压测报告 - {{.StartTime}}</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #f4f6f9; color: #2c3e50; }
  h1 { margin: 0 0 16px; }
  h2 { margin: 24px 0 12px; color: #34495e; border-left: 4px solid #3498db; padding-left: 10px; }
  .card { background: #fff; padding: 18px 22px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 18px; }
  .metrics { display: flex; flex-wrap: wrap; gap: 18px; }
  .metric { flex: 1 1 180px; background: #fafbfc; border: 1px solid #eceff1; border-radius: 6px; padding: 12px 14px; }
  .metric .label { color: #7f8c8d; font-size: 12px; margin-bottom: 4px; }
  .metric .value { font-size: 22px; font-weight: 600; color: #2c3e50; }
  .ok   { color: #27ae60 !important; }
  .bad  { color: #c0392b !important; }
  .warn { color: #e67e22 !important; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { border: 1px solid #e1e4e8; padding: 8px 10px; text-align: left; }
  th { background: #ecf0f1; }
  tr:nth-child(even) td { background: #fbfcfd; }
  code { background: #ecf0f1; padding: 1px 6px; border-radius: 3px; font-size: 13px; }
  .muted { color: #95a5a6; font-size: 12px; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; background: #ecf0f1; color: #34495e; }
  .tag.bad { background: #fdecea; color: #c0392b; }
  .tag.warn { background: #fff4e5; color: #b26a00; }
  details summary { cursor: pointer; color: #7f8c8d; font-size: 13px; user-select: none; }
  details[open] summary { margin-bottom: 12px; }
</style>
</head>
<body>
  <h1>Amber 下发压测报告</h1>
  <div class="muted">接口：<code>{{.Domain}}{{.APIPath}}</code> &nbsp; 开始：{{.StartTime}} &nbsp; 结束：{{.EndTime}}</div>

  <h2>总览</h2>
  <div class="card">
    <div class="metrics">
      <div class="metric"><div class="label">设备数量 X</div><div class="value">{{.DeviceCount}}</div></div>
      <div class="metric"><div class="label">测试时长</div><div class="value">{{printf "%.2f" .DurationSec}} s</div></div>
      <div class="metric"><div class="label">发送间隔</div><div class="value">{{.Interval}}</div></div>
      <div class="metric"><div class="label">目标 QPS</div><div class="value">{{.PlannedQPS}}</div></div>
      <div class="metric"><div class="label">实际 QPS</div><div class="value">{{.ActualQPS}}</div></div>
      <div class="metric"><div class="label">总请求数</div><div class="value">{{.TotalRequests}}</div></div>
      <div class="metric"><div class="label">成功率样本数</div><div class="value">{{.EffectiveRequests}}</div></div>
      <div class="metric"><div class="label">成功数</div><div class="value ok">{{.SuccessCount}}</div></div>
      <div class="metric"><div class="label">失败数</div><div class="value bad">{{.FailCount}}</div></div>
      <div class="metric"><div class="label">成功率</div><div class="value">{{.SuccessRate}}</div></div>
    </div>
  </div>

  <h2>延迟分布 (ms)</h2>
  <div class="card">
    <div class="metrics">
      <div class="metric"><div class="label">min</div><div class="value">{{.LatencyMinMs}}</div></div>
      <div class="metric"><div class="label">avg</div><div class="value">{{.LatencyAvgMs}}</div></div>
      <div class="metric"><div class="label">p50</div><div class="value">{{.LatencyP50Ms}}</div></div>
      <div class="metric"><div class="label">p95</div><div class="value">{{.LatencyP95Ms}}</div></div>
      <div class="metric"><div class="label">p99</div><div class="value">{{.LatencyP99Ms}}</div></div>
      <div class="metric"><div class="label">max</div><div class="value">{{.LatencyMaxMs}}</div></div>
    </div>
  </div>

  <h2>Errno 分布</h2>
  <div class="card">
    {{if .ErrnoDist}}
    <table>
      <thead><tr><th style="width:120px">errno</th><th style="width:120px">次数</th><th style="width:120px">占比</th><th>示例 msg</th></tr></thead>
      <tbody>
      {{range .ErrnoDist}}
        <tr>
          <td><span class="tag {{if ne .Errno 0}}bad{{end}}">{{.TagText}}</span></td>
          <td>{{.Count}}</td>
          <td>{{.RatioText}}</td>
          <td><code>{{.Sample}}</code></td>
        </tr>
      {{end}}
      </tbody>
    </table>
    {{else}}
      <div class="muted">无数据</div>
    {{end}}
    <div class="muted" style="margin-top: 10px;">丢弃统计：记录错误并丢弃 {{.DiscardWithLogCount}} 条，不记录错误并丢弃 {{.DiscardSilentCount}} 条</div>
    <details style="margin-top: 8px;">
      <summary>展开查看按错误码统计的丢弃明细</summary>
      {{if .DropWithLogDist}}
      <div class="muted" style="margin: 8px 0;">记录错误并丢弃</div>
      <table>
        <thead><tr><th style="width:120px">errno</th><th style="width:120px">次数</th><th>示例 msg</th></tr></thead>
        <tbody>
        {{range .DropWithLogDist}}
          <tr>
            <td><span class="tag warn">{{.Errno}}</span></td>
            <td>{{.Count}}</td>
            <td><code>{{.Sample}}</code></td>
          </tr>
        {{end}}
        </tbody>
      </table>
      {{else}}
      <div class="muted" style="margin: 8px 0;">记录错误并丢弃：0 条</div>
      {{end}}
      {{if .DropSilentDist}}
      <div class="muted" style="margin: 12px 0 8px;">不记录错误并丢弃</div>
      <table>
        <thead><tr><th style="width:120px">errno</th><th style="width:120px">次数</th><th>示例 msg</th></tr></thead>
        <tbody>
        {{range .DropSilentDist}}
          <tr>
            <td><span class="tag warn">{{.Errno}}</span></td>
            <td>{{.Count}}</td>
            <td><code>{{.Sample}}</code></td>
          </tr>
        {{end}}
        </tbody>
      </table>
      {{else}}
      <div class="muted" style="margin: 12px 0 0;">不记录错误并丢弃：0 条</div>
      {{end}}
    </details>
  </div>

  <h2>异常明细 (errno != 0)</h2>
  <div class="card">
    <div class="muted">完整异常日志（JSONL）：<code>{{.ErrorLogFile}}</code>&nbsp;&nbsp;共 {{.ErrorsTotal}} 条{{if lt .ErrorsShown .ErrorsTotal}}，此处仅展示前 {{.ErrorsShown}} 条{{end}}</div>
    {{if .Errors}}
    <table>
      <thead><tr><th style="width:190px">时间</th><th>SN</th><th style="width:90px">errno</th><th style="width:120px">耗时(ms)</th><th>msg</th></tr></thead>
      <tbody>
      {{range .Errors}}
        <tr>
          <td>{{.Timestamp}}</td>
          <td><code>{{.SN}}</code></td>
          <td><span class="tag bad">{{.Errno}}</span></td>
          <td>{{printf "%.2f" .ElapsedMs}}</td>
          <td>{{.Msg}}</td>
        </tr>
      {{end}}
      </tbody>
    </table>
    {{else}}
      <div class="muted ok">没有发现任何异常请求</div>
    {{end}}
  </div>

</body>
</html>
`

func generateReport(cfg *Config, results []requestResult, deviceCount int, targetQPS float64, startTime time.Time, duration time.Duration, interval time.Duration, errLogPath, reportPath string) error {
	total := len(results)
	latencies := make([]float64, 0, total)
	var success, fail, discardedWithLog, discardedSilent int
	errnoCount := make(map[int]int)
	errnoSample := make(map[int]string)
	dropWithLogCount := make(map[int]int)
	dropWithLogSample := make(map[int]string)
	dropSilentCount := make(map[int]int)
	dropSilentSample := make(map[int]string)
	var errorsAll []errorDetail

	for _, r := range results {
		ms := float64(r.Elapsed.Microseconds()) / 1000.0
		if r.Discarded {
			if r.DiscardLogged {
				discardedWithLog++
				dropWithLogCount[r.Errno]++
				if _, ok := dropWithLogSample[r.Errno]; !ok {
					dropWithLogSample[r.Errno] = truncate(r.Msg, 160)
				}
				errnoCount[r.Errno]++
				if _, ok := errnoSample[r.Errno]; !ok {
					errnoSample[r.Errno] = truncate(r.Msg, 160)
				}
			} else {
				discardedSilent++
				dropSilentCount[r.Errno]++
				if _, ok := dropSilentSample[r.Errno]; !ok {
					dropSilentSample[r.Errno] = truncate(r.Msg, 160)
				}
			}
		} else if r.Errno == 0 {
			success++
			errnoCount[r.Errno]++
			if _, ok := errnoSample[r.Errno]; !ok {
				errnoSample[r.Errno] = truncate(r.Msg, 160)
			}
		} else {
			fail++
			errnoCount[r.Errno]++
			if _, ok := errnoSample[r.Errno]; !ok {
				errnoSample[r.Errno] = truncate(r.Msg, 160)
			}
		}
		if !r.Discarded {
			latencies = append(latencies, ms)
		}
		if r.Errno != 0 && (r.DiscardLogged || !r.Discarded) {
			errorsAll = append(errorsAll, errorDetail{
				Timestamp: r.Timestamp.Format("2006-01-02 15:04:05.000"),
				SN:        r.SN,
				Errno:     r.Errno,
				Msg:       truncate(r.Msg, 240),
				ElapsedMs: ms,
			})
		}
	}

	sort.Float64s(latencies)
	var latMin, latMax, latAvg, latP50, latP95, latP99 float64
	if len(latencies) > 0 {
		latMin = latencies[0]
		latMax = latencies[len(latencies)-1]
		var sum float64
		for _, v := range latencies {
			sum += v
		}
		latAvg = sum / float64(len(latencies))
		latP50 = percentile(latencies, 50)
		latP95 = percentile(latencies, 95)
		latP99 = percentile(latencies, 99)
	}

	actualQPS := 0.0
	if duration.Seconds() > 0 {
		actualQPS = float64(total) / duration.Seconds()
	}
	successRate := 0.0
	effectiveTotal := total - discardedWithLog - discardedSilent
	if effectiveTotal > 0 {
		successRate = float64(success) / float64(effectiveTotal) * 100
	}

	// 排序 errno 分布：先 0，后按次数降序
	type ek struct{ errno, count int }
	keys := make([]ek, 0, len(errnoCount))
	for k, c := range errnoCount {
		keys = append(keys, ek{k, c})
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].errno == 0 {
			return true
		}
		if keys[j].errno == 0 {
			return false
		}
		return keys[i].count > keys[j].count
	})
	dist := make([]errnoStat, 0, len(keys))
	for _, k := range keys {
		tagText := fmt.Sprintf("%d", k.errno)
		ratioText := "-"
		if k.errno == 0 {
			tagText = "0 (成功)"
		}
		if _, ok := dropWithLogCount[k.errno]; ok {
			tagText = fmt.Sprintf("%d (丢弃-记日志)", k.errno)
			ratioText = "-"
		} else if effectiveTotal > 0 {
			ratioText = fmt.Sprintf("%.2f%%", float64(k.count)/float64(effectiveTotal)*100)
		}
		dist = append(dist, errnoStat{
			Errno:     k.errno,
			Count:     k.count,
			Sample:    errnoSample[k.errno],
			RatioText: ratioText,
			TagText:   tagText,
		})
	}

	buildDiscardDist := func(counts map[int]int, samples map[int]string, tagSuffix string) []errnoStat {
		keys := make([]ek, 0, len(counts))
		for k, c := range counts {
			keys = append(keys, ek{k, c})
		}
		sort.Slice(keys, func(i, j int) bool {
			if keys[i].count == keys[j].count {
				return keys[i].errno < keys[j].errno
			}
			return keys[i].count > keys[j].count
		})
		dist := make([]errnoStat, 0, len(keys))
		for _, k := range keys {
			dist = append(dist, errnoStat{
				Errno:     k.errno,
				Count:     k.count,
				Sample:    samples[k.errno],
				RatioText: "-",
				TagText:   fmt.Sprintf("%d%s", k.errno, tagSuffix),
			})
		}
		return dist
	}
	dropWithLogDist := buildDiscardDist(dropWithLogCount, dropWithLogSample, " (丢弃-记日志)")
	dropSilentDist := buildDiscardDist(dropSilentCount, dropSilentSample, " (丢弃-不记日志)")

	// 控制台打印摘要
	fmt.Println(strings.Repeat("-", 60))
	fmt.Printf("总请求        : %d\n", total)
	fmt.Printf("成功率样本数  : %d\n", effectiveTotal)
	fmt.Printf("成功 / 失败   : %d / %d  (成功率 %.2f%%)\n", success, fail, successRate)
	fmt.Printf("丢弃数量      : 记录错误=%d  不记录错误=%d\n", discardedWithLog, discardedSilent)
	fmt.Printf("目标 / 实际 QPS: %.4f / %.4f\n", targetQPS, actualQPS)
	fmt.Printf("延迟(ms)      : min=%.2f avg=%.2f p50=%.2f p95=%.2f p99=%.2f max=%.2f\n",
		latMin, latAvg, latP50, latP95, latP99, latMax)
	if len(dist) > 0 {
		fmt.Println("Errno 分布    :")
		for _, e := range dist {
			fmt.Printf("  errno=%d  count=%d  sample=%q\n", e.Errno, e.Count, truncate(e.Sample, 80))
		}
	}

	shown := len(errorsAll)
	if shown > maxErrorsInHTML {
		shown = maxErrorsInHTML
	}

	data := reportData{
		StartTime:           startTime.Format("2006-01-02 15:04:05"),
		EndTime:             startTime.Add(duration).Format("2006-01-02 15:04:05"),
		DurationSec:         duration.Seconds(),
		DeviceCount:         deviceCount,
		PlannedQPS:          fmt.Sprintf("%.4f", targetQPS),
		ActualQPS:           fmt.Sprintf("%.4f", actualQPS),
		Interval:            interval.String(),
		TotalRequests:       total,
		EffectiveRequests:   effectiveTotal,
		SuccessCount:        success,
		FailCount:           fail,
		SuccessRate:         fmt.Sprintf("%.2f%%", successRate),
		LatencyMinMs:        fmt.Sprintf("%.2f", latMin),
		LatencyAvgMs:        fmt.Sprintf("%.2f", latAvg),
		LatencyP50Ms:        fmt.Sprintf("%.2f", latP50),
		LatencyP95Ms:        fmt.Sprintf("%.2f", latP95),
		LatencyP99Ms:        fmt.Sprintf("%.2f", latP99),
		LatencyMaxMs:        fmt.Sprintf("%.2f", latMax),
		ErrnoDist:           dist,
		Errors:              errorsAll[:shown],
		ErrorsShown:         shown,
		ErrorsTotal:         len(errorsAll),
		DiscardWithLogCount: discardedWithLog,
		DiscardSilentCount:  discardedSilent,
		DropWithLogDist:     dropWithLogDist,
		DropSilentDist:      dropSilentDist,
		ErrorLogFile:        errLogPath,
		APIPath:             cfg.APIPath,
		Domain:              cfg.Domain,
	}

	funcs := template.FuncMap{
		"percent": func(n, total int) float64 {
			if total <= 0 {
				return 0
			}
			return float64(n) / float64(total) * 100
		},
	}
	tpl, err := template.New("report").Funcs(funcs).Parse(reportHTMLTemplate)
	if err != nil {
		return fmt.Errorf("模板解析失败: %w", err)
	}
	f, err := os.Create(reportPath)
	if err != nil {
		return fmt.Errorf("创建报告文件失败: %w", err)
	}
	defer f.Close()
	if err := tpl.Execute(f, data); err != nil {
		return fmt.Errorf("渲染报告失败: %w", err)
	}

	fmt.Printf("HTML 报告     : %s\n", reportPath)
	fmt.Printf("异常日志      : %s\n", errLogPath)
	fmt.Printf("配置快照      : %s\n", filepath.Join(filepath.Dir(reportPath), "config.snapshot.json"))
	return nil
}

// ---------------- main ----------------

func main() {
	cfgPath := resolvePath(configFileName)
	absCfgPath, err := filepath.Abs(cfgPath)
	if err != nil {
		fmt.Printf("解析配置路径失败: %v\n", err)
		os.Exit(1)
	}
	baseDir = filepath.Dir(absCfgPath)

	cfg, err := loadConfig(absCfgPath)
	if err != nil {
		fmt.Printf("加载配置失败: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("已加载配置: %s\n", absCfgPath)
	fmt.Printf("工作目录   : %s\n", baseDir)

	// 根据配置注入运行期全局值
	cfgDomain = cfg.Domain
	cfgAPIToken = cfg.APIToken
	httpClient = buildHTTPClient(cfg.RequestTimeout())

	snPath := anchorPath(cfg.SNFile)
	sns, err := readSNList(snPath)
	if err != nil {
		fmt.Printf("读取 SN 列表失败 (%s): %v\n", snPath, err)
		os.Exit(1)
	}
	if len(sns) == 0 {
		fmt.Printf("SN 列表为空: %s\n", snPath)
		os.Exit(1)
	}
	fmt.Printf("已加载 SN 列表: %s  (共 %d 个)\n", snPath, len(sns))

	if err := runLoadTest(cfg, sns); err != nil {
		fmt.Printf("压测执行失败: %v\n", err)
		os.Exit(1)
	}
}

func errnoSet(values []int) map[int]struct{} {
	set := make(map[int]struct{}, len(values))
	for _, v := range values {
		set[v] = struct{}{}
	}
	return set
}

func containsErrno(set map[int]struct{}, errno int) bool {
	_, ok := set[errno]
	return ok
}
