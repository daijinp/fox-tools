package main

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

const defaultConfigFile = "mqtt_perf_config.json"

type Config struct {
	Broker                  string   `json:"broker"`
	Port                    int      `json:"port"`
	Username                string   `json:"username"`
	Password                string   `json:"password"`
	ClientID                string   `json:"client_id"`
	CertFile                string   `json:"cert_file"`
	KeyFile                 string   `json:"key_file"`
	CACertFile              string   `json:"ca_cert_file"`
	InsecureSkipVerify      bool     `json:"insecure_skip_verify"`
	SubTopics               []string `json:"sub_topics"`
	QoS                     byte     `json:"qos"`
	TotalQPS                int      `json:"total_qps"`
	DurationSeconds         int      `json:"duration_seconds"`
	LogFile                 string   `json:"log_file"`
	MessageFile             string   `json:"message_file"`
	StatsIntervalSeconds    int      `json:"stats_interval_seconds"`
	ConnectTimeoutSec       int      `json:"connect_timeout_seconds"`
	ReconnectIntervalSec    int      `json:"reconnect_interval_seconds"`
	PublishAllTopicsOnStart bool     `json:"publish_all_topics_on_start"`
}

type MessageDefinition struct {
	Topic   string `json:"topic"`
	Payload any    `json:"payload"`
}

type Stats struct {
	publishAttempts uint64
	publishSuccess  uint64
	publishFailures uint64
	receivedCount   uint64
	reconnectCount  uint64
	connectCount    uint64
}

type TopicStats struct {
	mu                 sync.Mutex
	topicOrder         []string
	knownTopics        map[string]bool
	publishAttempts    map[string]uint64
	publishSuccess     map[string]uint64
	publishFailures    map[string]uint64
	receivedCount      map[string]uint64
	firstPublishLogged map[string]bool
	firstReceiveLogged map[string]bool
}

type TopicSnapshot struct {
	Topic           string
	PublishAttempts uint64
	PublishSuccess  uint64
	PublishFailures uint64
	ReceivedCount   uint64
}

func main() {
	configPath := defaultConfigFile
	if len(os.Args) > 1 && strings.TrimSpace(os.Args[1]) != "" {
		configPath = os.Args[1]
	}

	config, err := loadConfig(configPath)
	if err != nil {
		log.Fatalf("ERROR loading config failed: %v", err)
	}

	logger, logFile, err := setupLogger(config.LogFile)
	if err != nil {
		log.Fatalf("ERROR setting up logger failed: %v", err)
	}
	if logFile != nil {
		defer logFile.Close()
	}

	messages, err := loadMessages(config.MessageFile)
	if err != nil {
		logger.Fatalf("ERROR loading message definitions failed: %v", err)
	}
	if len(messages) == 0 {
		logger.Fatal("ERROR no valid topic/payload entries found in message file")
	}
	logMessageCatalog(logger, messages)

	tlsConfig, err := buildTLSConfig(config)
	if err != nil {
		logger.Fatalf("ERROR building TLS config failed: %v", err)
	}

	stats := &Stats{}
	topicStats := newTopicStats(messages)
	initialSubscriptionsReady := make(chan struct{})
	client := newMQTTClient(config, tlsConfig, logger, stats, topicStats, initialSubscriptionsReady)

	logger.Printf(
		"starting mqtt perf test: broker=%s:%d total_qps=%d duration=%ds topics=%d message_file=%s publish_all_topics_on_start=%t",
		config.Broker,
		config.Port,
		config.TotalQPS,
		config.DurationSeconds,
		len(messages),
		config.MessageFile,
		config.PublishAllTopicsOnStart,
	)

	connectToken := client.Connect()
	if !connectToken.WaitTimeout(time.Duration(config.ConnectTimeoutSec) * time.Second) {
		logger.Fatal("ERROR waiting for initial connection timed out")
	}
	if err := connectToken.Error(); err != nil {
		logger.Fatalf("ERROR initial connect failed: %v", err)
	}
	waitForInitialSubscriptions(config, logger, initialSubscriptionsReady)

	if config.PublishAllTopicsOnStart {
		logger.Printf("publishing every configured topic once before timed QPS loop: topics=%d", len(messages))
		for _, message := range messages {
			publishMessage(client, config.QoS, message, logger, stats, topicStats)
		}
	}

	ctxDuration := time.Duration(config.DurationSeconds) * time.Second
	statsInterval := time.Duration(config.StatsIntervalSeconds) * time.Second
	publishInterval := time.Second / time.Duration(config.TotalQPS)
	if publishInterval <= 0 {
		publishInterval = time.Nanosecond
	}

	stopSignals := make(chan os.Signal, 1)
	signal.Notify(stopSignals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(stopSignals)

	runDeadline := time.Now().Add(ctxDuration)
	publishTicker := time.NewTicker(publishInterval)
	statsTicker := time.NewTicker(statsInterval)
	defer publishTicker.Stop()
	defer statsTicker.Stop()

	var msgIndex int
	var previousAttempts uint64
	var previousSuccess uint64
	var previousFailures uint64
	var previousReceived uint64
	previousTopicSnapshots := make(map[string]TopicSnapshot)

runLoop:
	for {
		select {
		case sig := <-stopSignals:
			logger.Printf("received stop signal: %s", sig)
			break runLoop
		case now := <-statsTicker.C:
			attempts := atomic.LoadUint64(&stats.publishAttempts)
			success := atomic.LoadUint64(&stats.publishSuccess)
			failures := atomic.LoadUint64(&stats.publishFailures)
			received := atomic.LoadUint64(&stats.receivedCount)
			logger.Printf(
				"stats time=%s total_attempts=%d total_success=%d total_failures=%d total_received=%d delta_attempts=%d delta_success=%d delta_failures=%d delta_received=%d reconnects=%d connects=%d",
				now.Format(time.RFC3339),
				attempts,
				success,
				failures,
				received,
				attempts-previousAttempts,
				success-previousSuccess,
				failures-previousFailures,
				received-previousReceived,
				atomic.LoadUint64(&stats.reconnectCount),
				atomic.LoadUint64(&stats.connectCount),
			)
			previousAttempts = attempts
			previousSuccess = success
			previousFailures = failures
			previousReceived = received
			topicSnapshots := topicStats.snapshot()
			logger.Printf("topic_stats time=%s %s", now.Format(time.RFC3339), formatTopicStats(topicSnapshots, previousTopicSnapshots))
			rememberTopicSnapshots(previousTopicSnapshots, topicSnapshots)
		case now := <-publishTicker.C:
			if now.After(runDeadline) {
				break runLoop
			}

			message := messages[msgIndex]
			msgIndex = (msgIndex + 1) % len(messages)
			publishMessage(client, config.QoS, message, logger, stats, topicStats)
		}
	}

	logger.Printf(
		"perf test finished: attempts=%d success=%d failures=%d received=%d reconnects=%d connects=%d",
		atomic.LoadUint64(&stats.publishAttempts),
		atomic.LoadUint64(&stats.publishSuccess),
		atomic.LoadUint64(&stats.publishFailures),
		atomic.LoadUint64(&stats.receivedCount),
		atomic.LoadUint64(&stats.reconnectCount),
		atomic.LoadUint64(&stats.connectCount),
	)
	logger.Printf("final_topic_stats %s", formatFinalTopicStats(topicStats.snapshot()))

	client.Disconnect(500)
	logger.Println("program finished")
}

type PreparedMessage struct {
	Topic       string
	PayloadJSON []byte
}

func newTopicStats(messages []PreparedMessage) *TopicStats {
	stats := &TopicStats{
		knownTopics:        make(map[string]bool),
		publishAttempts:    make(map[string]uint64),
		publishSuccess:     make(map[string]uint64),
		publishFailures:    make(map[string]uint64),
		receivedCount:      make(map[string]uint64),
		firstPublishLogged: make(map[string]bool),
		firstReceiveLogged: make(map[string]bool),
	}

	for _, message := range messages {
		stats.ensureTopicLocked(message.Topic)
	}

	return stats
}

func (stats *TopicStats) recordPublishAttempt(topic string) {
	stats.mu.Lock()
	defer stats.mu.Unlock()

	stats.ensureTopicLocked(topic)
	stats.publishAttempts[topic]++
}

func (stats *TopicStats) recordPublishSuccess(topic string) (uint64, bool) {
	stats.mu.Lock()
	defer stats.mu.Unlock()

	stats.ensureTopicLocked(topic)
	stats.publishSuccess[topic]++
	count := stats.publishSuccess[topic]
	first := !stats.firstPublishLogged[topic]
	stats.firstPublishLogged[topic] = true
	return count, first
}

func (stats *TopicStats) recordPublishFailure(topic string) uint64 {
	stats.mu.Lock()
	defer stats.mu.Unlock()

	stats.ensureTopicLocked(topic)
	stats.publishFailures[topic]++
	return stats.publishFailures[topic]
}

func (stats *TopicStats) recordReceived(topic string) (uint64, bool) {
	stats.mu.Lock()
	defer stats.mu.Unlock()

	stats.ensureTopicLocked(topic)
	stats.receivedCount[topic]++
	count := stats.receivedCount[topic]
	first := !stats.firstReceiveLogged[topic]
	stats.firstReceiveLogged[topic] = true
	return count, first
}

func (stats *TopicStats) snapshot() []TopicSnapshot {
	stats.mu.Lock()
	defer stats.mu.Unlock()

	snapshots := make([]TopicSnapshot, 0, len(stats.topicOrder))
	for _, topic := range stats.topicOrder {
		snapshots = append(snapshots, TopicSnapshot{
			Topic:           topic,
			PublishAttempts: stats.publishAttempts[topic],
			PublishSuccess:  stats.publishSuccess[topic],
			PublishFailures: stats.publishFailures[topic],
			ReceivedCount:   stats.receivedCount[topic],
		})
	}

	return snapshots
}

func (stats *TopicStats) ensureTopicLocked(topic string) {
	if stats.knownTopics[topic] {
		return
	}

	stats.knownTopics[topic] = true
	stats.topicOrder = append(stats.topicOrder, topic)
}

func logMessageCatalog(logger *log.Logger, messages []PreparedMessage) {
	for idx, message := range messages {
		logger.Printf(
			"loaded message topic[%d/%d]=%s payload_bytes=%d",
			idx+1,
			len(messages),
			message.Topic,
			len(message.PayloadJSON),
		)
	}
}

func waitForInitialSubscriptions(config Config, logger *log.Logger, ready <-chan struct{}) {
	timeout := time.Duration(config.ConnectTimeoutSec) * time.Second
	select {
	case <-ready:
		logger.Println("initial subscriptions ready")
	case <-time.After(timeout):
		logger.Printf("warning waiting for initial subscriptions timed out after %s", timeout)
	}
}

func publishMessage(client mqtt.Client, qos byte, message PreparedMessage, logger *log.Logger, stats *Stats, topicStats *TopicStats) {
	atomic.AddUint64(&stats.publishAttempts, 1)
	topicStats.recordPublishAttempt(message.Topic)

	token := client.Publish(message.Topic, qos, false, message.PayloadJSON)
	token.Wait()
	if err := token.Error(); err != nil {
		atomic.AddUint64(&stats.publishFailures, 1)
		failures := topicStats.recordPublishFailure(message.Topic)
		logger.Printf("ERROR publish failed: topic=%s topic_failures=%d err=%v", message.Topic, failures, err)
		return
	}

	atomic.AddUint64(&stats.publishSuccess, 1)
	count, first := topicStats.recordPublishSuccess(message.Topic)
	if first {
		logger.Printf(
			"first publish success: topic=%s qos=%d payload_bytes=%d topic_success=%d",
			message.Topic,
			qos,
			len(message.PayloadJSON),
			count,
		)
	}
}

func formatTopicStats(snapshots []TopicSnapshot, previous map[string]TopicSnapshot) string {
	if len(snapshots) == 0 {
		return "no_topics"
	}

	parts := make([]string, 0, len(snapshots))
	for idx, snapshot := range snapshots {
		prev := previous[snapshot.Topic]
		parts = append(parts, fmt.Sprintf(
			"[%d] topic=%s attempts=%d(+%d) success=%d(+%d) failures=%d(+%d) received=%d(+%d)",
			idx+1,
			snapshot.Topic,
			snapshot.PublishAttempts,
			snapshot.PublishAttempts-prev.PublishAttempts,
			snapshot.PublishSuccess,
			snapshot.PublishSuccess-prev.PublishSuccess,
			snapshot.PublishFailures,
			snapshot.PublishFailures-prev.PublishFailures,
			snapshot.ReceivedCount,
			snapshot.ReceivedCount-prev.ReceivedCount,
		))
	}

	return strings.Join(parts, " | ")
}

func formatFinalTopicStats(snapshots []TopicSnapshot) string {
	if len(snapshots) == 0 {
		return "no_topics"
	}

	parts := make([]string, 0, len(snapshots))
	for idx, snapshot := range snapshots {
		parts = append(parts, fmt.Sprintf(
			"[%d] topic=%s attempts=%d success=%d failures=%d received=%d",
			idx+1,
			snapshot.Topic,
			snapshot.PublishAttempts,
			snapshot.PublishSuccess,
			snapshot.PublishFailures,
			snapshot.ReceivedCount,
		))
	}

	return strings.Join(parts, " | ")
}

func rememberTopicSnapshots(previous map[string]TopicSnapshot, snapshots []TopicSnapshot) {
	for _, snapshot := range snapshots {
		previous[snapshot.Topic] = snapshot
	}
}

func loadConfig(configPath string) (Config, error) {
	configPath = resolvePath(configPath)

	defaultConfig := Config{
		Broker:                  "emqx-svc.foxess-cloud.svc.cluster.local",
		Port:                    8883,
		Username:                "foxessTest",
		Password:                "<>foxess",
		ClientID:                "foxess_perf_tester",
		CertFile:                "server.pem",
		KeyFile:                 "server.key",
		CACertFile:              "",
		InsecureSkipVerify:      true,
		SubTopics:               []string{"/kp23bhcpmt91n2v8/R250312J0069/#"},
		QoS:                     1,
		TotalQPS:                10,
		DurationSeconds:         60,
		LogFile:                 "mqtt_perf.log",
		MessageFile:             "message.json",
		StatsIntervalSeconds:    1,
		ConnectTimeoutSec:       10,
		ReconnectIntervalSec:    5,
		PublishAllTopicsOnStart: true,
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		return Config{}, fmt.Errorf("read config %s: %w", configPath, err)
	}
	if err := json.Unmarshal(data, &defaultConfig); err != nil {
		return Config{}, fmt.Errorf("parse config %s: %w", configPath, err)
	}

	defaultConfig.CertFile = resolvePath(defaultConfig.CertFile)
	defaultConfig.KeyFile = resolvePath(defaultConfig.KeyFile)
	defaultConfig.CACertFile = resolvePath(defaultConfig.CACertFile)
	defaultConfig.LogFile = resolvePath(defaultConfig.LogFile)
	defaultConfig.MessageFile = resolvePath(defaultConfig.MessageFile)

	if defaultConfig.Broker == "" {
		return Config{}, errors.New("broker is required")
	}
	if defaultConfig.Port <= 0 {
		return Config{}, errors.New("port must be greater than 0")
	}
	if defaultConfig.TotalQPS <= 0 {
		return Config{}, errors.New("total_qps must be greater than 0")
	}
	if defaultConfig.DurationSeconds <= 0 {
		return Config{}, errors.New("duration_seconds must be greater than 0")
	}
	if defaultConfig.StatsIntervalSeconds <= 0 {
		return Config{}, errors.New("stats_interval_seconds must be greater than 0")
	}
	if defaultConfig.ConnectTimeoutSec <= 0 {
		return Config{}, errors.New("connect_timeout_seconds must be greater than 0")
	}
	if defaultConfig.ReconnectIntervalSec <= 0 {
		return Config{}, errors.New("reconnect_interval_seconds must be greater than 0")
	}
	if defaultConfig.CertFile == "" || defaultConfig.KeyFile == "" {
		return Config{}, errors.New("cert_file and key_file are required")
	}

	return defaultConfig, nil
}

func setupLogger(logPath string) (*log.Logger, *os.File, error) {
	if logPath == "" {
		return log.New(os.Stdout, "", log.LstdFlags|log.Lmicroseconds), nil, nil
	}

	if err := os.MkdirAll(filepath.Dir(logPath), 0o755); err != nil && !errors.Is(err, os.ErrExist) {
		return nil, nil, fmt.Errorf("create log dir for %s: %w", logPath, err)
	}

	file, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, nil, fmt.Errorf("open log file %s: %w", logPath, err)
	}

	writer := io.MultiWriter(os.Stdout, file)
	return log.New(writer, "", log.LstdFlags|log.Lmicroseconds), file, nil
}

func loadMessages(messagePath string) ([]PreparedMessage, error) {
	data, err := os.ReadFile(messagePath)
	if err != nil {
		return nil, fmt.Errorf("read message file %s: %w", messagePath, err)
	}

	var rawMessages []MessageDefinition
	if err := json.Unmarshal(data, &rawMessages); err != nil {
		return nil, fmt.Errorf("parse message file %s: %w", messagePath, err)
	}

	prepared := make([]PreparedMessage, 0, len(rawMessages))
	for idx, item := range rawMessages {
		topic := strings.TrimSpace(item.Topic)
		if topic == "" {
			return nil, fmt.Errorf("message index %d has empty topic", idx)
		}

		payloadJSON, err := json.Marshal(item.Payload)
		if err != nil {
			return nil, fmt.Errorf("marshal payload for topic %s: %w", topic, err)
		}

		prepared = append(prepared, PreparedMessage{
			Topic:       topic,
			PayloadJSON: payloadJSON,
		})
	}

	return prepared, nil
}

func buildTLSConfig(config Config) (*tls.Config, error) {
	if _, err := os.Stat(config.CertFile); err != nil {
		return nil, fmt.Errorf("certificate file not found: %s", config.CertFile)
	}
	if _, err := os.Stat(config.KeyFile); err != nil {
		return nil, fmt.Errorf("private key file not found: %s", config.KeyFile)
	}

	cert, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
	if err != nil {
		return nil, fmt.Errorf("load certificate/key pair: %w", err)
	}

	tlsConfig := &tls.Config{
		Certificates:       []tls.Certificate{cert},
		InsecureSkipVerify: config.InsecureSkipVerify,
		MinVersion:         tls.VersionTLS12,
	}

	if config.CACertFile != "" {
		caData, err := os.ReadFile(config.CACertFile)
		if err != nil {
			return nil, fmt.Errorf("read ca cert file %s: %w", config.CACertFile, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caData) {
			return nil, fmt.Errorf("append ca certs from %s failed", config.CACertFile)
		}
		tlsConfig.RootCAs = pool
	}

	return tlsConfig, nil
}

func newMQTTClient(config Config, tlsConfig *tls.Config, logger *log.Logger, stats *Stats, topicStats *TopicStats, initialSubscriptionsReady chan<- struct{}) mqtt.Client {
	var initialSubscriptionsOnce sync.Once

	opts := mqtt.NewClientOptions()
	opts.AddBroker(fmt.Sprintf("ssl://%s:%d", config.Broker, config.Port))
	opts.SetClientID(config.ClientID)
	opts.SetUsername(config.Username)
	opts.SetPassword(config.Password)
	opts.SetTLSConfig(tlsConfig)
	opts.SetOrderMatters(false)
	opts.SetAutoReconnect(true)
	opts.SetConnectRetry(true)
	opts.SetConnectRetryInterval(time.Duration(config.ReconnectIntervalSec) * time.Second)
	opts.SetConnectTimeout(time.Duration(config.ConnectTimeoutSec) * time.Second)
	opts.SetDefaultPublishHandler(func(_ mqtt.Client, msg mqtt.Message) {
		atomic.AddUint64(&stats.receivedCount, 1)
		count, first := topicStats.recordReceived(msg.Topic())
		if first {
			logger.Printf(
				"first received topic: topic=%s qos=%d retained=%t payload_bytes=%d topic_received=%d",
				msg.Topic(),
				msg.Qos(),
				msg.Retained(),
				len(msg.Payload()),
				count,
			)
		}
	})
	opts.OnConnect = func(client mqtt.Client) {
		connectCount := atomic.AddUint64(&stats.connectCount, 1)
		logger.Printf("connected to MQTT broker: count=%d", connectCount)

		for _, topic := range config.SubTopics {
			subTopic := strings.TrimSpace(topic)
			if subTopic == "" {
				continue
			}

			token := client.Subscribe(subTopic, config.QoS, nil)
			token.Wait()
			if err := token.Error(); err != nil {
				logger.Printf("ERROR subscribe failed: topic=%s err=%v", subTopic, err)
				continue
			}
			logger.Printf("subscribed topic: %s (QoS %d)", subTopic, config.QoS)
		}
		initialSubscriptionsOnce.Do(func() {
			close(initialSubscriptionsReady)
		})
	}
	opts.OnReconnecting = func(_ mqtt.Client, _ *mqtt.ClientOptions) {
		reconnectCount := atomic.AddUint64(&stats.reconnectCount, 1)
		logger.Printf("warning reconnecting to MQTT broker: attempt=%d", reconnectCount)
	}
	opts.OnConnectionLost = func(_ mqtt.Client, err error) {
		logger.Printf("warning connection lost: err=%v", err)
	}

	return mqtt.NewClient(opts)
}

func resolvePath(value string) string {
	if value == "" {
		return value
	}

	value = normalizePath(value)
	if filepath.IsAbs(value) {
		return value
	}

	execDir := executableDir()
	candidate := filepath.Join(execDir, value)
	if _, err := os.Stat(candidate); err == nil {
		return candidate
	}

	wd, err := os.Getwd()
	if err == nil {
		candidate = filepath.Join(wd, value)
		if _, statErr := os.Stat(candidate); statErr == nil {
			return candidate
		}
	}

	return filepath.Join(execDir, value)
}

func executableDir() string {
	execPath, err := os.Executable()
	if err != nil {
		wd, wdErr := os.Getwd()
		if wdErr != nil {
			return "."
		}
		return wd
	}
	return filepath.Dir(execPath)
}

func normalizePath(value string) string {
	return strings.ReplaceAll(value, "\\", string(filepath.Separator))
}
