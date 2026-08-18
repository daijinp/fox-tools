package main

import (
	"bufio"
	"context"
	"database/sql"
	"encoding/csv"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"github.com/go-sql-driver/mysql"
	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

const (
	mysqlNetworkName       = "mysql-over-ssh"
	defaultBatchSize       = 200
	defaultBatchInterval   = 1
	defaultTimeoutSeconds  = 60
	maxBatchSize           = 200
	allDeviceOutputName    = "all_device_data.csv"
	onlineDeviceOutputName = "online_device_by_protocol.csv"
)

type config struct {
	SSH    sshConfig    `json:"ssh"`
	MySQL  mysqlConfig  `json:"mysql"`
	Input  inputConfig  `json:"input"`
	Output outputConfig `json:"output"`
	Query  queryConfig  `json:"query"`
}

type sshConfig struct {
	Address              string `json:"address"`
	Username             string `json:"username"`
	PrivateKey           string `json:"private_key"`
	PrivateKeyPassphrase string `json:"private_key_passphrase"`
	KnownHostsFile       string `json:"known_hosts_file"`
	TimeoutSeconds       int    `json:"timeout_seconds"`
}

type mysqlConfig struct {
	Address               string `json:"address"`
	Username              string `json:"username"`
	Password              string `json:"password"`
	Database              string `json:"database"`
	ConnectTimeoutSeconds int    `json:"connect_timeout_seconds"`
}

type inputConfig struct {
	CSVFiles []string `json:"csv_files"`
}

type outputConfig struct {
	Directory string `json:"directory"`
}

type queryConfig struct {
	BatchSize            int `json:"batch_size"`
	BatchIntervalSeconds int `json:"batch_interval_seconds"`
	TimeoutSeconds       int `json:"timeout_seconds"`
}

type inputStats struct {
	Rows       int
	Empty      int
	Duplicates int
}

type deviceSNGroup struct {
	Name      string
	SourceCSV string
	DeviceSNs []string
	Stats     inputStats
}

type groupQueryResult struct {
	Group         deviceSNGroup
	AllDevices    []allDeviceRecord
	OnlineDevices []onlineDeviceRecord
}

type allDeviceRecord struct {
	DeviceID        string
	DeviceSN        string
	ProtocolVersion string
	MasterVersion   string
	ProductType     string
}

type onlineDeviceRecord struct {
	ProtocolVersion string
	DeviceID        string
}

type queryer interface {
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
}

func main() {
	configPathFlag := flag.String("config", "", "配置文件路径；默认自动查找 config/config.json")
	flag.Parse()

	configPath, err := locateConfig(*configPathFlag)
	if err != nil {
		fatal(err)
	}
	if err := run(configPath); err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "错误：", err)
	os.Exit(1)
}

func run(configPath string) error {
	cfg, configDir, err := loadConfig(configPath)
	if err != nil {
		return err
	}
	if err := validateConfig(&cfg, configDir); err != nil {
		return err
	}

	groups, err := readDeviceSNGroups(cfg.Input.CSVFiles, configDir)
	if err != nil {
		return err
	}
	for _, group := range groups {
		fmt.Printf(
			"分组 %s：%d 个唯一 SN（CSV 记录：%d，空值：%d，重复：%d）\n",
			group.Name,
			len(group.DeviceSNs),
			group.Stats.Rows,
			group.Stats.Empty,
			group.Stats.Duplicates,
		)
	}

	fmt.Printf("正在连接 SSH：%s\n", cfg.SSH.Address)
	sshClient, err := connectSSH(cfg.SSH, configDir)
	if err != nil {
		return err
	}
	defer sshClient.Close()

	db, err := connectMySQL(cfg, sshClient)
	if err != nil {
		return err
	}
	defer db.Close()
	fmt.Printf("SSH 隧道和 MySQL 连接成功：%s/%s\n", cfg.MySQL.Address, cfg.MySQL.Database)

	if err := checkRequiredIndexes(db, cfg.Query.TimeoutSeconds); err != nil {
		return err
	}
	fmt.Println("索引检查通过：devices(device_sn)、versions(model_id)")

	results := make([]groupQueryResult, 0, len(groups))
	for index, group := range groups {
		if len(group.DeviceSNs) == 0 {
			fmt.Printf("分组 %s 没有有效 SN，将生成只有表头的结果文件\n", group.Name)
			results = append(results, groupQueryResult{Group: group})
			continue
		}
		if index > 0 && cfg.Query.BatchIntervalSeconds > 0 {
			fmt.Printf("切换分组前等待 %d 秒...\n", cfg.Query.BatchIntervalSeconds)
			time.Sleep(time.Duration(cfg.Query.BatchIntervalSeconds) * time.Second)
		}

		fmt.Printf("开始查询分组：%s（来源：%s）\n", group.Name, group.SourceCSV)
		allDevices, onlineDevices, err := queryDevices(db, group.DeviceSNs, cfg.Query)
		if err != nil {
			return fmt.Errorf("查询分组 %s：%w", group.Name, err)
		}
		fmt.Printf("分组 %s 全部设备：%d 行，在线协议设备：%d 行\n", group.Name, len(allDevices), len(onlineDevices))
		results = append(results, groupQueryResult{
			Group:         group,
			AllDevices:    allDevices,
			OnlineDevices: onlineDevices,
		})
	}

	outputDir := resolvePath(configDir, cfg.Output.Directory)
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return fmt.Errorf("创建输出目录：%w", err)
	}
	var outputPaths []string
	for _, result := range results {
		allDevicePath := filepath.Join(outputDir, result.Group.Name+"_"+allDeviceOutputName)
		if err := writeAllDevices(allDevicePath, result.AllDevices); err != nil {
			return err
		}
		onlineDevicePath := filepath.Join(outputDir, result.Group.Name+"_"+onlineDeviceOutputName)
		if err := writeOnlineDevices(onlineDevicePath, result.OnlineDevices); err != nil {
			return err
		}
		outputPaths = append(outputPaths, allDevicePath, onlineDevicePath)
	}
	archivedPaths, err := archiveLegacyCombinedOutputs(outputDir)
	if err != nil {
		return err
	}
	fmt.Println("结果已写入：")
	for _, outputPath := range outputPaths {
		fmt.Println("  " + outputPath)
	}
	if len(archivedPaths) > 0 {
		fmt.Println("旧版混合结果已归档：")
		for _, archivedPath := range archivedPaths {
			fmt.Println("  " + archivedPath)
		}
	}
	return nil
}

func locateConfig(explicitPath string) (string, error) {
	if explicitPath != "" {
		path, err := filepath.Abs(explicitPath)
		if err != nil {
			return "", fmt.Errorf("解析配置文件路径：%w", err)
		}
		return path, nil
	}

	var candidates []string
	if workingDir, err := os.Getwd(); err == nil {
		candidates = append(
			candidates,
			filepath.Join(workingDir, "get_data_for_mysql", "config", "config.json"),
			filepath.Join(workingDir, "config", "config.json"),
		)
	}
	if executable, err := os.Executable(); err == nil {
		candidates = append(candidates, filepath.Join(filepath.Dir(executable), "config", "config.json"))
	}
	if _, sourceFile, _, ok := runtime.Caller(0); ok {
		candidates = append(
			candidates,
			filepath.Join(filepath.Dir(sourceFile), "..", "..", "config", "config.json"),
		)
	}

	seen := make(map[string]struct{})
	for _, candidate := range candidates {
		absoluteCandidate, err := filepath.Abs(candidate)
		if err != nil {
			continue
		}
		absoluteCandidate = filepath.Clean(absoluteCandidate)
		if _, ok := seen[absoluteCandidate]; ok {
			continue
		}
		seen[absoluteCandidate] = struct{}{}
		if info, err := os.Stat(absoluteCandidate); err == nil && !info.IsDir() {
			return absoluteCandidate, nil
		}
	}

	return "", errors.New("找不到 config/config.json；请使用 -config 指定配置文件")
}

func loadConfig(path string) (config, string, error) {
	file, err := os.Open(path)
	if err != nil {
		return config{}, "", fmt.Errorf("打开配置文件 %s：%w", path, err)
	}
	defer file.Close()

	decoder := json.NewDecoder(file)
	decoder.DisallowUnknownFields()
	var cfg config
	if err := decoder.Decode(&cfg); err != nil {
		return config{}, "", fmt.Errorf("解析配置文件 %s：%w", path, err)
	}
	if err := ensureJSONEOF(decoder); err != nil {
		return config{}, "", fmt.Errorf("解析配置文件 %s：%w", path, err)
	}

	absolutePath, err := filepath.Abs(path)
	if err != nil {
		return config{}, "", fmt.Errorf("解析配置文件目录：%w", err)
	}
	return cfg, filepath.Dir(absolutePath), nil
}

func ensureJSONEOF(decoder *json.Decoder) error {
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("配置文件只能包含一个 JSON 对象")
		}
		return err
	}
	return nil
}

func validateConfig(cfg *config, configDir string) error {
	cfg.SSH.Address = strings.TrimSpace(cfg.SSH.Address)
	cfg.SSH.Username = strings.TrimSpace(cfg.SSH.Username)
	cfg.SSH.PrivateKey = strings.TrimSpace(cfg.SSH.PrivateKey)
	cfg.SSH.KnownHostsFile = strings.TrimSpace(cfg.SSH.KnownHostsFile)
	cfg.MySQL.Address = strings.TrimSpace(cfg.MySQL.Address)
	cfg.MySQL.Username = strings.TrimSpace(cfg.MySQL.Username)
	cfg.MySQL.Database = strings.TrimSpace(cfg.MySQL.Database)
	cfg.Output.Directory = strings.TrimSpace(cfg.Output.Directory)

	if err := requireHostPort("ssh.address", cfg.SSH.Address); err != nil {
		return err
	}
	if cfg.SSH.Username == "" || looksLikePlaceholder(cfg.SSH.Username) {
		return errors.New("请在 config.json 中填写 ssh.username")
	}
	if cfg.SSH.PrivateKey == "" {
		return errors.New("请在 config.json 中填写 ssh.private_key")
	}
	privateKeyPath := resolvePath(configDir, cfg.SSH.PrivateKey)
	if info, err := os.Stat(privateKeyPath); err != nil {
		return fmt.Errorf("访问 SSH 私钥 %s：%w", privateKeyPath, err)
	} else if info.IsDir() {
		return fmt.Errorf("SSH 私钥路径是目录：%s", privateKeyPath)
	}
	if cfg.SSH.KnownHostsFile == "" {
		cfg.SSH.KnownHostsFile = "known_hosts"
	}
	if cfg.SSH.TimeoutSeconds <= 0 {
		cfg.SSH.TimeoutSeconds = 15
	}

	if err := requireHostPort("mysql.address", cfg.MySQL.Address); err != nil {
		return err
	}
	if cfg.MySQL.Username == "" || looksLikePlaceholder(cfg.MySQL.Username) {
		return errors.New("请在 config.json 中填写 mysql.username")
	}
	if cfg.MySQL.Password == "" || looksLikePlaceholder(cfg.MySQL.Password) {
		return errors.New("请在 config.json 中填写 mysql.password")
	}
	if cfg.MySQL.Database == "" || looksLikePlaceholder(cfg.MySQL.Database) {
		return errors.New("请在 config.json 中填写 mysql.database")
	}
	if cfg.MySQL.ConnectTimeoutSeconds <= 0 {
		cfg.MySQL.ConnectTimeoutSeconds = 15
	}

	if len(cfg.Input.CSVFiles) == 0 {
		return errors.New("input.csv_files 至少需要一个 CSV 文件")
	}
	if cfg.Output.Directory == "" {
		return errors.New("请在 config.json 中填写 output.directory")
	}
	if cfg.Query.BatchSize <= 0 {
		cfg.Query.BatchSize = defaultBatchSize
	}
	if cfg.Query.BatchSize > maxBatchSize {
		return fmt.Errorf("query.batch_size 不能超过 %d", maxBatchSize)
	}
	if cfg.Query.BatchIntervalSeconds < 0 {
		return errors.New("query.batch_interval_seconds 不能小于 0")
	}
	if cfg.Query.BatchIntervalSeconds == 0 {
		cfg.Query.BatchIntervalSeconds = defaultBatchInterval
	}
	if cfg.Query.TimeoutSeconds <= 0 {
		cfg.Query.TimeoutSeconds = defaultTimeoutSeconds
	}
	return nil
}

func requireHostPort(fieldName, address string) error {
	if address == "" || looksLikePlaceholder(address) {
		return fmt.Errorf("请在 config.json 中填写 %s", fieldName)
	}
	if _, _, err := net.SplitHostPort(address); err != nil {
		return fmt.Errorf("%s 必须使用 host:port 格式：%w", fieldName, err)
	}
	return nil
}

func looksLikePlaceholder(value string) bool {
	lowerValue := strings.ToLower(value)
	return strings.Contains(lowerValue, "replace-with") || strings.Contains(lowerValue, "example.com")
}

func resolvePath(baseDir, configuredPath string) string {
	if filepath.IsAbs(configuredPath) {
		return filepath.Clean(configuredPath)
	}
	return filepath.Clean(filepath.Join(baseDir, configuredPath))
}

func readDeviceSNs(configuredPaths []string, configDir string) ([]string, inputStats, error) {
	seen := make(map[string]struct{})
	var deviceSNs []string
	var stats inputStats

	for _, configuredPath := range configuredPaths {
		path := resolvePath(configDir, configuredPath)
		file, err := os.Open(path)
		if err != nil {
			return nil, stats, fmt.Errorf("打开 CSV %s：%w", path, err)
		}

		reader := csv.NewReader(bufio.NewReaderSize(file, 64*1024))
		reader.FieldsPerRecord = -1
		for {
			record, readErr := reader.Read()
			if errors.Is(readErr, io.EOF) {
				break
			}
			if readErr != nil {
				file.Close()
				return nil, stats, fmt.Errorf("解析 CSV %s：%w", path, readErr)
			}

			stats.Rows++
			if len(record) == 0 {
				stats.Empty++
				continue
			}
			deviceSN := strings.TrimSpace(record[0])
			deviceSN = strings.TrimSpace(strings.TrimPrefix(deviceSN, "\uFEFF"))
			if deviceSN == "" {
				stats.Empty++
				continue
			}
			if _, exists := seen[deviceSN]; exists {
				stats.Duplicates++
				continue
			}
			seen[deviceSN] = struct{}{}
			deviceSNs = append(deviceSNs, deviceSN)
		}
		if err := file.Close(); err != nil {
			return nil, stats, fmt.Errorf("关闭 CSV %s：%w", path, err)
		}
	}

	return deviceSNs, stats, nil
}

func readDeviceSNGroups(configuredPaths []string, configDir string) ([]deviceSNGroup, error) {
	usedNames := make(map[string]int)
	groups := make([]deviceSNGroup, 0, len(configuredPaths))
	for index, configuredPath := range configuredPaths {
		deviceSNs, stats, err := readDeviceSNs([]string{configuredPath}, configDir)
		if err != nil {
			return nil, err
		}

		sourcePath := resolvePath(configDir, configuredPath)
		name := sanitizeFilenameComponent(
			strings.TrimSuffix(filepath.Base(sourcePath), filepath.Ext(sourcePath)),
		)
		if name == "" {
			name = fmt.Sprintf("group_%d", index+1)
		}
		usedNames[name]++
		if usedNames[name] > 1 {
			name = fmt.Sprintf("%s_%d", name, usedNames[name])
		}

		groups = append(groups, deviceSNGroup{
			Name:      name,
			SourceCSV: sourcePath,
			DeviceSNs: deviceSNs,
			Stats:     stats,
		})
	}
	return groups, nil
}

func sanitizeFilenameComponent(value string) string {
	value = strings.Map(func(character rune) rune {
		if character < 32 || strings.ContainsRune(`<>:"/\|?*`, character) {
			return '_'
		}
		return character
	}, value)
	return strings.Trim(value, " .")
}

func connectSSH(cfg sshConfig, configDir string) (*ssh.Client, error) {
	privateKeyPath := resolvePath(configDir, cfg.PrivateKey)
	privateKey, err := os.ReadFile(privateKeyPath)
	if err != nil {
		return nil, fmt.Errorf("读取 SSH 私钥 %s：%w", privateKeyPath, err)
	}

	var signer ssh.Signer
	if cfg.PrivateKeyPassphrase == "" {
		signer, err = ssh.ParsePrivateKey(privateKey)
		var passphraseMissingError *ssh.PassphraseMissingError
		if errors.As(err, &passphraseMissingError) {
			return nil, errors.New("SSH 私钥已加密，请填写 ssh.private_key_passphrase")
		}
	} else {
		signer, err = ssh.ParsePrivateKeyWithPassphrase(privateKey, []byte(cfg.PrivateKeyPassphrase))
	}
	if err != nil {
		return nil, fmt.Errorf("解析 SSH 私钥 %s：%w", privateKeyPath, err)
	}

	hostKeyCallback, err := makeHostKeyCallback(cfg, configDir)
	if err != nil {
		return nil, err
	}
	clientConfig := &ssh.ClientConfig{
		User:            cfg.Username,
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback: hostKeyCallback,
		Timeout:         time.Duration(cfg.TimeoutSeconds) * time.Second,
	}

	client, err := ssh.Dial("tcp", cfg.Address, clientConfig)
	if err != nil {
		return nil, fmt.Errorf("连接 SSH %s：%w", cfg.Address, err)
	}
	return client, nil
}

func makeHostKeyCallback(cfg sshConfig, configDir string) (ssh.HostKeyCallback, error) {
	knownHostsPath := resolvePath(configDir, cfg.KnownHostsFile)
	if err := ensureKnownHostsFile(knownHostsPath); err != nil {
		return nil, err
	}
	knownHostsCallback, err := knownhosts.New(knownHostsPath)
	if err != nil {
		return nil, fmt.Errorf("读取 SSH known_hosts 文件 %s：%w", knownHostsPath, err)
	}

	return func(hostname string, remote net.Addr, key ssh.PublicKey) error {
		err := knownHostsCallback(hostname, remote, key)
		if err == nil {
			return nil
		}

		var keyError *knownhosts.KeyError
		if !errors.As(err, &keyError) {
			return err
		}
		if len(keyError.Want) > 0 {
			return fmt.Errorf(
				"SSH 服务器主机密钥已变化，已拒绝连接；请确认服务器变更后检查 %s（当前指纹：%s）",
				knownHostsPath,
				ssh.FingerprintSHA256(key),
			)
		}

		line := knownhosts.Line([]string{hostname}, key)
		file, openErr := os.OpenFile(knownHostsPath, os.O_APPEND|os.O_WRONLY, 0o600)
		if openErr != nil {
			return fmt.Errorf("写入 SSH known_hosts 文件 %s：%w", knownHostsPath, openErr)
		}
		if _, writeErr := fmt.Fprintln(file, line); writeErr != nil {
			file.Close()
			return fmt.Errorf("写入 SSH known_hosts 文件 %s：%w", knownHostsPath, writeErr)
		}
		if syncErr := file.Sync(); syncErr != nil {
			file.Close()
			return fmt.Errorf("同步 SSH known_hosts 文件 %s：%w", knownHostsPath, syncErr)
		}
		if closeErr := file.Close(); closeErr != nil {
			return fmt.Errorf("关闭 SSH known_hosts 文件 %s：%w", knownHostsPath, closeErr)
		}

		fmt.Printf(
			"首次连接，已记录 SSH 服务器主机密钥：%s（%s）\n",
			knownHostsPath,
			ssh.FingerprintSHA256(key),
		)
		return nil
	}, nil
}

func ensureKnownHostsFile(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("创建 SSH known_hosts 目录：%w", err)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return fmt.Errorf("创建 SSH known_hosts 文件 %s：%w", path, err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("关闭 SSH known_hosts 文件 %s：%w", path, err)
	}
	return nil
}

func connectMySQL(cfg config, sshClient *ssh.Client) (*sql.DB, error) {
	mysql.RegisterDialContext(mysqlNetworkName, func(ctx context.Context, address string) (net.Conn, error) {
		connection, err := sshClient.DialContext(ctx, "tcp", address)
		if err != nil {
			return nil, fmt.Errorf("通过 SSH 隧道连接 MySQL %s：%w", address, err)
		}
		return connection, nil
	})

	driverConfig := mysql.NewConfig()
	driverConfig.User = cfg.MySQL.Username
	driverConfig.Passwd = cfg.MySQL.Password
	driverConfig.Net = mysqlNetworkName
	driverConfig.Addr = cfg.MySQL.Address
	driverConfig.DBName = cfg.MySQL.Database
	driverConfig.Timeout = time.Duration(cfg.MySQL.ConnectTimeoutSeconds) * time.Second
	driverConfig.Params = map[string]string{"charset": "utf8mb4"}

	db, err := sql.Open("mysql", driverConfig.FormatDSN())
	if err != nil {
		return nil, fmt.Errorf("创建 MySQL 连接：%w", err)
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)

	pingContext, cancel := context.WithTimeout(
		context.Background(),
		time.Duration(cfg.MySQL.ConnectTimeoutSeconds)*time.Second,
	)
	defer cancel()
	if err := db.PingContext(pingContext); err != nil {
		db.Close()
		return nil, fmt.Errorf("连接 MySQL %s/%s：%w", cfg.MySQL.Address, cfg.MySQL.Database, err)
	}
	return db, nil
}

func checkRequiredIndexes(q queryer, timeoutSeconds int) error {
	requirements := []struct {
		table  string
		column string
	}{
		{table: "devices", column: "device_sn"},
		{table: "versions", column: "model_id"},
	}

	const indexQuery = `
SELECT index_name
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name = ?
  AND column_name = ?
  AND seq_in_index = 1
LIMIT 1`

	for _, requirement := range requirements {
		queryContext, cancel := context.WithTimeout(
			context.Background(),
			time.Duration(timeoutSeconds)*time.Second,
		)
		rows, err := q.QueryContext(queryContext, indexQuery, requirement.table, requirement.column)
		if err != nil {
			cancel()
			return fmt.Errorf("检查索引 %s(%s)：%w", requirement.table, requirement.column, err)
		}

		found := rows.Next()
		var indexName string
		if found {
			if err := rows.Scan(&indexName); err != nil {
				rows.Close()
				cancel()
				return fmt.Errorf("读取索引 %s(%s)：%w", requirement.table, requirement.column, err)
			}
		}
		rowsErr := rows.Err()
		closeErr := rows.Close()
		cancel()
		if rowsErr != nil {
			return fmt.Errorf("检查索引 %s(%s)：%w", requirement.table, requirement.column, rowsErr)
		}
		if closeErr != nil {
			return fmt.Errorf("关闭索引检查结果：%w", closeErr)
		}
		if !found {
			return fmt.Errorf(
				"为避免生产库全表扫描，已停止查询：%s.%s 不是任何索引的第一列",
				requirement.table,
				requirement.column,
			)
		}
	}
	return nil
}

func queryDevices(q queryer, deviceSNs []string, cfg queryConfig) ([]allDeviceRecord, []onlineDeviceRecord, error) {
	firstBatchEnd := cfg.BatchSize
	if firstBatchEnd > len(deviceSNs) {
		firstBatchEnd = len(deviceSNs)
	}
	if err := checkQueryPlan(q, deviceSNs[:firstBatchEnd], cfg.TimeoutSeconds); err != nil {
		return nil, nil, err
	}

	var allResults []allDeviceRecord
	onlineMinimums := make(map[string]string)
	totalBatches := (len(deviceSNs) + cfg.BatchSize - 1) / cfg.BatchSize

	for start := 0; start < len(deviceSNs); start += cfg.BatchSize {
		end := start + cfg.BatchSize
		if end > len(deviceSNs) {
			end = len(deviceSNs)
		}
		batch := deviceSNs[start:end]

		query := buildDeviceQuery(len(batch))
		args := stringsToArgs(batch)
		queryContext, cancel := context.WithTimeout(
			context.Background(),
			time.Duration(cfg.TimeoutSeconds)*time.Second,
		)
		rows, err := q.QueryContext(queryContext, query, args...)
		if err != nil {
			cancel()
			return nil, nil, fmt.Errorf("查询设备（批次 %d-%d）：%w", start+1, end, err)
		}

		columnTypes, err := rows.ColumnTypes()
		if err != nil {
			rows.Close()
			cancel()
			return nil, nil, fmt.Errorf("读取 device_id 字段类型：%w", err)
		}
		deviceIDIsNumeric := len(columnTypes) > 0 && isNumericDatabaseType(columnTypes[0].DatabaseTypeName())
		batchRows := 0
		for rows.Next() {
			var deviceID, deviceSN, protocolVersion, masterVersion, productType sql.NullString
			var communication sql.NullInt64
			if err := rows.Scan(
				&deviceID,
				&deviceSN,
				&protocolVersion,
				&masterVersion,
				&productType,
				&communication,
			); err != nil {
				rows.Close()
				cancel()
				return nil, nil, fmt.Errorf("读取设备查询结果：%w", err)
			}

			deviceIDValue := nullStringValue(deviceID)
			protocolVersionValue := nullStringValue(protocolVersion)
			allResults = append(allResults, allDeviceRecord{
				DeviceID:        deviceIDValue,
				DeviceSN:        nullStringValue(deviceSN),
				ProtocolVersion: protocolVersionValue,
				MasterVersion:   nullStringValue(masterVersion),
				ProductType:     nullStringValue(productType),
			})
			batchRows++

			if communication.Valid && communication.Int64 == 0 && deviceIDValue != "" {
				currentMinimum, exists := onlineMinimums[protocolVersionValue]
				if !exists || deviceIDLess(deviceIDValue, currentMinimum, deviceIDIsNumeric) {
					onlineMinimums[protocolVersionValue] = deviceIDValue
				}
			}
		}
		rowsErr := rows.Err()
		closeErr := rows.Close()
		cancel()
		if rowsErr != nil {
			return nil, nil, fmt.Errorf("遍历设备查询结果：%w", rowsErr)
		}
		if closeErr != nil {
			return nil, nil, fmt.Errorf("关闭设备查询结果：%w", closeErr)
		}

		batchNumber := start/cfg.BatchSize + 1
		fmt.Printf(
			"查询批次 %d/%d 完成（SN：%d，结果：%d）\n",
			batchNumber,
			totalBatches,
			len(batch),
			batchRows,
		)
		if end < len(deviceSNs) && cfg.BatchIntervalSeconds > 0 {
			fmt.Printf("等待 %d 秒后执行下一批...\n", cfg.BatchIntervalSeconds)
			time.Sleep(time.Duration(cfg.BatchIntervalSeconds) * time.Second)
		}
	}

	sort.Slice(allResults, func(i, j int) bool {
		if allResults[i].DeviceSN != allResults[j].DeviceSN {
			return allResults[i].DeviceSN < allResults[j].DeviceSN
		}
		if allResults[i].ProtocolVersion != allResults[j].ProtocolVersion {
			return allResults[i].ProtocolVersion < allResults[j].ProtocolVersion
		}
		return allResults[i].DeviceID < allResults[j].DeviceID
	})

	onlineResults := make([]onlineDeviceRecord, 0, len(onlineMinimums))
	for protocolVersion, deviceID := range onlineMinimums {
		onlineResults = append(onlineResults, onlineDeviceRecord{
			ProtocolVersion: protocolVersion,
			DeviceID:        deviceID,
		})
	}
	sort.Slice(onlineResults, func(i, j int) bool {
		return onlineResults[i].ProtocolVersion < onlineResults[j].ProtocolVersion
	})
	return allResults, onlineResults, nil
}

func buildDeviceQuery(snCount int) string {
	return `
SELECT d.device_id,
       d.device_sn,
       v.protocol_version,
       v.master_version,
       d.product_type,
       d.communication
FROM devices AS d
INNER JOIN versions AS v
        ON d.device_id = v.model_id
WHERE d.device_sn IN (` + placeholders(snCount) + `)`
}

func checkQueryPlan(q queryer, deviceSNs []string, timeoutSeconds int) error {
	queryContext, cancel := context.WithTimeout(
		context.Background(),
		time.Duration(timeoutSeconds)*time.Second,
	)
	defer cancel()

	rows, err := q.QueryContext(
		queryContext,
		"EXPLAIN "+buildDeviceQuery(len(deviceSNs)),
		stringsToArgs(deviceSNs)...,
	)
	if err != nil {
		return fmt.Errorf("检查查询执行计划：%w", err)
	}
	defer rows.Close()

	columns, err := rows.Columns()
	if err != nil {
		return fmt.Errorf("读取 EXPLAIN 字段：%w", err)
	}
	seenRequiredTable := map[string]bool{"d": false, "v": false}
	for rows.Next() {
		rawValues := make([]sql.RawBytes, len(columns))
		destinations := make([]any, len(columns))
		for index := range rawValues {
			destinations[index] = &rawValues[index]
		}
		if err := rows.Scan(destinations...); err != nil {
			return fmt.Errorf("读取 EXPLAIN 结果：%w", err)
		}

		plan := make(map[string]string, len(columns))
		for index, column := range columns {
			plan[strings.ToLower(column)] = string(rawValues[index])
		}
		table := plan["table"]
		if table != "d" && table != "v" {
			continue
		}
		seenRequiredTable[table] = true
		accessType := strings.ToUpper(plan["type"])
		key := plan["key"]
		if accessType == "ALL" || accessType == "INDEX" || key == "" {
			return fmt.Errorf(
				"为避免生产库全表/全索引扫描，已停止查询：EXPLAIN 显示表 %s 的访问类型为 %s、索引为 %q",
				table,
				accessType,
				key,
			)
		}
		fmt.Printf(
			"执行计划：表=%s，访问类型=%s，索引=%s，预计扫描行=%s\n",
			table,
			accessType,
			key,
			plan["rows"],
		)
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("遍历 EXPLAIN 结果：%w", err)
	}
	for table, seen := range seenRequiredTable {
		if !seen {
			return fmt.Errorf("EXPLAIN 结果中没有找到表 %s，已停止查询", table)
		}
	}
	return nil
}

func isNumericDatabaseType(databaseType string) bool {
	normalizedType := strings.ToUpper(strings.TrimSpace(databaseType))
	normalizedType = strings.TrimSpace(strings.ReplaceAll(normalizedType, "UNSIGNED", ""))
	if parenthesis := strings.IndexByte(normalizedType, '('); parenthesis >= 0 {
		normalizedType = normalizedType[:parenthesis]
	}
	switch strings.TrimSpace(normalizedType) {
	case "TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL":
		return true
	default:
		return false
	}
}

func deviceIDLess(candidate, current string, numeric bool) bool {
	if numeric {
		candidateNumber, candidateOK := new(big.Rat).SetString(candidate)
		currentNumber, currentOK := new(big.Rat).SetString(current)
		if candidateOK && currentOK {
			return candidateNumber.Cmp(currentNumber) < 0
		}
	}
	return candidate < current
}

func placeholders(count int) string {
	if count <= 0 {
		return ""
	}
	return strings.TrimSuffix(strings.Repeat("?,", count), ",")
}

func stringsToArgs(values []string) []any {
	args := make([]any, len(values))
	for index, value := range values {
		args[index] = value
	}
	return args
}

func nullStringValue(value sql.NullString) string {
	if !value.Valid {
		return ""
	}
	return value.String
}

func writeAllDevices(path string, records []allDeviceRecord) error {
	rows := make([][]string, 0, len(records))
	for _, record := range records {
		rows = append(rows, []string{
			record.DeviceID,
			record.DeviceSN,
			record.ProtocolVersion,
			record.MasterVersion,
			record.ProductType,
		})
	}
	return writeCSV(path, rows)
}

func writeOnlineDevices(path string, records []onlineDeviceRecord) error {
	rows := make([][]string, 0, len(records))
	for _, record := range records {
		rows = append(rows, []string{record.ProtocolVersion, record.DeviceID})
	}
	return writeCSV(path, rows)
}

func archiveLegacyCombinedOutputs(outputDir string) ([]string, error) {
	legacyNames := []string{allDeviceOutputName, onlineDeviceOutputName}
	legacyDir := filepath.Join(outputDir, "legacy_combined")
	var archivedPaths []string

	for _, legacyName := range legacyNames {
		sourcePath := filepath.Join(outputDir, legacyName)
		info, err := os.Stat(sourcePath)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("检查旧版混合结果 %s：%w", sourcePath, err)
		}
		if info.IsDir() {
			return nil, fmt.Errorf("旧版混合结果路径是目录：%s", sourcePath)
		}
		if err := os.MkdirAll(legacyDir, 0o755); err != nil {
			return nil, fmt.Errorf("创建旧版结果归档目录：%w", err)
		}

		destinationPath := filepath.Join(legacyDir, legacyName)
		if _, err := os.Stat(destinationPath); err == nil {
			extension := filepath.Ext(legacyName)
			baseName := strings.TrimSuffix(legacyName, extension)
			destinationPath = filepath.Join(
				legacyDir,
				fmt.Sprintf("%s_%s%s", baseName, time.Now().Format("20060102-150405"), extension),
			)
		} else if !errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("检查旧版结果归档路径：%w", err)
		}
		if err := os.Rename(sourcePath, destinationPath); err != nil {
			return nil, fmt.Errorf("归档旧版混合结果 %s：%w", sourcePath, err)
		}
		archivedPaths = append(archivedPaths, destinationPath)
	}
	return archivedPaths, nil
}

func writeCSV(path string, rows [][]string) (returnedErr error) {
	file, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("创建结果文件 %s：%w", path, err)
	}
	defer func() {
		if closeErr := file.Close(); returnedErr == nil && closeErr != nil {
			returnedErr = fmt.Errorf("关闭结果文件 %s：%w", path, closeErr)
		}
	}()

	bufferedWriter := bufio.NewWriterSize(file, 64*1024)
	writer := csv.NewWriter(bufferedWriter)
	if err := writer.WriteAll(rows); err != nil {
		return fmt.Errorf("写入结果文件 %s：%w", path, err)
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		return fmt.Errorf("写入结果文件 %s：%w", path, err)
	}
	if err := bufferedWriter.Flush(); err != nil {
		return fmt.Errorf("刷新结果文件 %s：%w", path, err)
	}
	if err := file.Sync(); err != nil {
		return fmt.Errorf("同步结果文件 %s：%w", path, err)
	}
	return nil
}
