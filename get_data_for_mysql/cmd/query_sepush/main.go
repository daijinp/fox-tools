package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/go-sql-driver/mysql"
	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

const networkName = "sepush-audit-over-ssh"

type config struct {
	SSH   sshConfig   `json:"ssh"`
	MySQL mysqlConfig `json:"mysql"`
	Query queryConfig `json:"query"`
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

type queryConfig struct {
	TimeoutSeconds int `json:"timeout_seconds"`
}

func main() {
	configPath := flag.String("config", "config/config.json", "MySQL/SSH 配置文件")
	blockID := flag.String("block-id", "", "可选：只核查指定的 plants.block_id")
	plantID := flag.String("plant-id", "", "可选：核查指定的 plant_id")
	deviceID := flag.String("device-id", "", "可选：核查指定的 device_id 是否属于电站")
	daysBefore := flag.Int("days-before", 3, "向前核查的天数")
	daysAfter := flag.Int("days-after", 7, "向后核查的天数")
	flag.Parse()

	if *daysBefore < 0 || *daysAfter < 0 {
		fatal(errors.New("days-before 和 days-after 不能小于 0"))
	}
	if err := run(*configPath, strings.TrimSpace(*plantID), strings.TrimSpace(*blockID),
		strings.TrimSpace(*deviceID), *daysBefore, *daysAfter); err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "错误：", err)
	os.Exit(1)
}

func run(configPath, plantID, blockID, deviceID string, daysBefore, daysAfter int) error {
	cfg, configDir, err := loadConfig(configPath)
	if err != nil {
		return err
	}
	if err := validateConfig(&cfg, configDir); err != nil {
		return err
	}

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

	timeout := time.Duration(cfg.Query.TimeoutSeconds) * time.Second
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return fmt.Errorf("开启只读事务：%w", err)
	}
	defer tx.Rollback()

	if err := printServerTime(ctx, tx); err != nil {
		return err
	}
	if err := printTableColumns(ctx, tx); err != nil {
		return err
	}
	if err := printSummary(ctx, tx, daysBefore, daysAfter); err != nil {
		return err
	}
	if err := printRecentDays(ctx, tx, daysBefore, daysAfter); err != nil {
		return err
	}
	if err := printStageCoverage(ctx, tx, daysBefore, daysAfter); err != nil {
		return err
	}
	if blockID != "" {
		if err := printBlockRows(ctx, tx, blockID, daysBefore, daysAfter); err != nil {
			return err
		}
	}
	if plantID != "" {
		if err := printTargetReadiness(ctx, tx, plantID, blockID, deviceID); err != nil {
			return err
		}
	}

	return tx.Commit()
}

func printServerTime(ctx context.Context, q *sql.Tx) error {
	var now, today, sessionTZ, globalTZ string
	err := q.QueryRowContext(ctx, `
SELECT DATE_FORMAT(NOW(), '%Y-%m-%d %H:%i:%s'),
       DATE_FORMAT(CURDATE(), '%Y-%m-%d'),
       @@session.time_zone,
       @@global.time_zone`).Scan(&now, &today, &sessionTZ, &globalTZ)
	if err != nil {
		return fmt.Errorf("查询数据库时间：%w", err)
	}
	fmt.Printf("数据库时间：%s；日期：%s；session/global 时区：%s/%s\n", now, today, sessionTZ, globalTZ)
	return nil
}

func printTableColumns(ctx context.Context, q *sql.Tx) error {
	rows, err := q.QueryContext(ctx, `
SELECT column_name
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'ns_power_outage_info'
ORDER BY ordinal_position`)
	if err != nil {
		return fmt.Errorf("查询 ns_power_outage_info 字段：%w", err)
	}
	defer rows.Close()

	var columns []string
	for rows.Next() {
		var column string
		if err := rows.Scan(&column); err != nil {
			return fmt.Errorf("读取字段：%w", err)
		}
		columns = append(columns, column)
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("遍历字段：%w", err)
	}
	if len(columns) == 0 {
		return errors.New("当前数据库不存在 ns_power_outage_info 表")
	}
	fmt.Printf("ns_power_outage_info 字段：%s\n", strings.Join(columns, ", "))
	return nil
}

func printSummary(ctx context.Context, q *sql.Tx, daysBefore, daysAfter int) error {
	var total, distinctBlocks, recentRows, invalidDays, invalidBlocks, invalidJSON, nonEightStages int64
	var minDay, maxDay sql.NullString
	err := q.QueryRowContext(ctx, `
SELECT COUNT(*),
       COUNT(DISTINCT block_id),
       MIN(STR_TO_DATE(day, '%Y-%m-%d')),
       MAX(STR_TO_DATE(day, '%Y-%m-%d')),
       SUM(CASE WHEN STR_TO_DATE(day, '%Y-%m-%d') BETWEEN DATE_SUB(CURDATE(), INTERVAL ? DAY)
                    AND DATE_ADD(CURDATE(), INTERVAL ? DAY) THEN 1 ELSE 0 END),
       SUM(CASE WHEN STR_TO_DATE(day, '%Y-%m-%d') IS NULL THEN 1 ELSE 0 END),
       SUM(CASE WHEN block_id IS NULL OR block_id IN ('', 'null') THEN 1 ELSE 0 END),
       SUM(CASE WHEN stages IS NULL OR JSON_VALID(stages) = 0 THEN 1 ELSE 0 END),
       SUM(CASE WHEN JSON_VALID(stages) = 1 AND JSON_LENGTH(stages) <> 8 THEN 1 ELSE 0 END)
FROM ns_power_outage_info`, daysBefore, daysAfter).Scan(
		&total, &distinctBlocks, &minDay, &maxDay, &recentRows,
		&invalidDays, &invalidBlocks, &invalidJSON, &nonEightStages,
	)
	if err != nil {
		return fmt.Errorf("汇总停电计划：%w", err)
	}

	fmt.Printf("总记录：%d；distinct block_id：%d；计划日期范围：%s ~ %s\n",
		total, distinctBlocks, nullableDate(minDay), nullableDate(maxDay))
	fmt.Printf("核查窗口（前 %d 天到后 %d 天）记录：%d\n", daysBefore, daysAfter, recentRows)
	fmt.Printf("质量检查：非法日期=%d，空 block_id=%d，非法 stages JSON=%d，非 8 个 Stage=%d\n",
		invalidDays, invalidBlocks, invalidJSON, nonEightStages)
	return nil
}

func printRecentDays(ctx context.Context, q *sql.Tx, daysBefore, daysAfter int) error {
	rows, err := q.QueryContext(ctx, `
SELECT day,
       COUNT(*) AS plan_rows,
       COUNT(DISTINCT block_id) AS block_count,
       SUM(CASE WHEN JSON_VALID(stages) = 1 AND JSON_LENGTH(stages) = 8 THEN 0 ELSE 1 END) AS bad_stage_rows,
       SUM(CASE WHEN stage IS NULL THEN 0 ELSE 1 END) AS marked_stage_rows
FROM ns_power_outage_info
WHERE STR_TO_DATE(day, '%Y-%m-%d') BETWEEN DATE_SUB(CURDATE(), INTERVAL ? DAY)
                                          AND DATE_ADD(CURDATE(), INTERVAL ? DAY)
GROUP BY day
ORDER BY STR_TO_DATE(day, '%Y-%m-%d')`, daysBefore, daysAfter)
	if err != nil {
		return fmt.Errorf("按日期查询近期计划：%w", err)
	}
	defer rows.Close()

	fmt.Println("近期计划（日期 / 行数 / block 数 / 异常 Stage 行 / 已标记 stage 行）：")
	found := false
	for rows.Next() {
		found = true
		var day string
		var planRows, blockCount, badStageRows, markedStageRows int64
		if err := rows.Scan(&day, &planRows, &blockCount, &badStageRows, &markedStageRows); err != nil {
			return fmt.Errorf("读取近期计划：%w", err)
		}
		fmt.Printf("  %s / %d / %d / %d / %d\n", day, planRows, blockCount, badStageRows, markedStageRows)
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("遍历近期计划：%w", err)
	}
	if !found {
		fmt.Println("  无记录")
	}
	return nil
}

func printStageCoverage(ctx context.Context, q *sql.Tx, daysBefore, daysAfter int) error {
	rows, err := q.QueryContext(ctx, `
SELECT day,
       COALESCE(CAST(stage AS CHAR), 'NULL') AS current_stage,
       COUNT(*) AS row_count,
       SUM(CASE WHEN JSON_LENGTH(JSON_EXTRACT(stages, '$[1]')) > 0 THEN 1 ELSE 0 END) AS stage2_available,
       SUM(CASE WHEN JSON_LENGTH(JSON_EXTRACT(stages, '$[2]')) > 0 THEN 1 ELSE 0 END) AS stage3_available
FROM ns_power_outage_info
WHERE STR_TO_DATE(day, '%Y-%m-%d') BETWEEN DATE_SUB(CURDATE(), INTERVAL ? DAY)
                                          AND DATE_ADD(CURDATE(), INTERVAL ? DAY)
GROUP BY day, stage
ORDER BY STR_TO_DATE(day, '%Y-%m-%d'), stage`, daysBefore, daysAfter)
	if err != nil {
		return fmt.Errorf("查询 Stage 2/3 覆盖：%w", err)
	}
	defer rows.Close()

	fmt.Println("Stage 覆盖（日期 / 当前 stage 值 / 行数 / Stage 2 非空 / Stage 3 非空）：")
	for rows.Next() {
		var day, stage string
		var rowCount, stage2Available, stage3Available int64
		if err := rows.Scan(&day, &stage, &rowCount, &stage2Available, &stage3Available); err != nil {
			return fmt.Errorf("读取 Stage 2/3 覆盖：%w", err)
		}
		fmt.Printf("  %s / %s / %d / %d / %d\n", day, stage, rowCount, stage2Available, stage3Available)
	}
	return rows.Err()
}

func printBlockRows(ctx context.Context, q *sql.Tx, blockID string, daysBefore, daysAfter int) error {
	rows, err := q.QueryContext(ctx, `
SELECT day,
       COALESCE(CAST(stage AS CHAR), 'NULL'),
       CASE WHEN JSON_VALID(stages) = 1 THEN JSON_LENGTH(stages) ELSE -1 END
FROM ns_power_outage_info
WHERE block_id = ?
  AND STR_TO_DATE(day, '%Y-%m-%d') BETWEEN DATE_SUB(CURDATE(), INTERVAL ? DAY)
                                           AND DATE_ADD(CURDATE(), INTERVAL ? DAY)
ORDER BY STR_TO_DATE(day, '%Y-%m-%d')`, blockID, daysBefore, daysAfter)
	if err != nil {
		return fmt.Errorf("查询指定 block_id：%w", err)
	}
	defer rows.Close()

	fmt.Printf("指定 block_id 的近期计划（日期 / 已标记 stage / Stage 数）：\n")
	found := false
	for rows.Next() {
		found = true
		var day, stage string
		var stageCount int
		if err := rows.Scan(&day, &stage, &stageCount); err != nil {
			return fmt.Errorf("读取指定 block_id 计划：%w", err)
		}
		fmt.Printf("  %s / %s / %d\n", day, stage, stageCount)
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("遍历指定 block_id 计划：%w", err)
	}
	if !found {
		fmt.Println("  无记录")
	}
	return nil
}

func printTargetReadiness(ctx context.Context, q *sql.Tx, plantID, expectedBlockID, deviceID string) error {
	if err := printIdentifierDiagnostics(ctx, q, plantID, expectedBlockID, deviceID); err != nil {
		return err
	}

	var plantRows, zaRows, matchingBlockRows int64
	var timezone string
	err := q.QueryRowContext(ctx, `
SELECT COUNT(*),
       COALESCE(SUM(CASE WHEN country = 'ZA' THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN block_id = ? THEN 1 ELSE 0 END), 0),
       COALESCE(MAX(iana_timezone), '')
FROM plants
WHERE plant_id = ?`, expectedBlockID, plantID).Scan(&plantRows, &zaRows, &matchingBlockRows, &timezone)
	if err != nil {
		return fmt.Errorf("核查测试电站：%w", err)
	}

	var todayPlans, stage2Plans, stage3Plans int64
	err = q.QueryRowContext(ctx, `
SELECT COUNT(*),
       COALESCE(SUM(CASE WHEN JSON_LENGTH(JSON_EXTRACT(i.stages, '$[1]')) > 0 THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN JSON_LENGTH(JSON_EXTRACT(i.stages, '$[2]')) > 0 THEN 1 ELSE 0 END), 0)
FROM ns_power_outage_info i
INNER JOIN plants p ON p.block_id = i.block_id
WHERE p.plant_id = ?
  AND STR_TO_DATE(i.day, '%Y-%m-%d') = CURDATE()`, plantID).Scan(&todayPlans, &stage2Plans, &stage3Plans)
	if err != nil {
		return fmt.Errorf("核查测试电站当天计划：%w", err)
	}

	var settingRows, reserveEnabled, pushEnabled int64
	var preChargeH, preChargeM sql.NullInt64
	err = q.QueryRowContext(ctx, `
SELECT COUNT(*),
       COALESCE(SUM(CASE WHEN power_outage_reserve = 1 THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN pushFlag = 1 THEN 1 ELSE 0 END), 0),
       MAX(preChargeH),
       MAX(preChargeM)
FROM ns_power_outage_settings
WHERE plant_id = ?`, plantID).Scan(&settingRows, &reserveEnabled, &pushEnabled, &preChargeH, &preChargeM)
	if err != nil {
		return fmt.Errorf("核查测试电站停电设置：%w", err)
	}

	var deviceBelongs int64
	if deviceID != "" {
		err = q.QueryRowContext(ctx, `
SELECT COUNT(*)
FROM (
  SELECT d.device_id
  FROM devices d
  INNER JOIN modules m ON m.device_id = d.device_id
  WHERE m.plant_id = ? AND d.device_id = ?
  UNION
  SELECT d.device_id
  FROM devices d
  INNER JOIN devices_relation dr ON dr.device_id = d.device_id
  INNER JOIN ems e ON e.ems_id = dr.module_id
  WHERE e.plant_id = ? AND d.device_id = ?
) target_device`, plantID, deviceID, plantID, deviceID).Scan(&deviceBelongs)
		if err != nil {
			return fmt.Errorf("核查设备归属：%w", err)
		}
	}

	var batteryCount int64
	err = q.QueryRowContext(ctx, `
SELECT COUNT(DISTINCT bms_sn)
FROM (
  SELECT b.bms_sn
  FROM devices d
  INNER JOIN modules m ON m.device_id = d.device_id
  INNER JOIN batteries b ON b.device_id = d.device_id
  WHERE m.plant_id = ?
  UNION
  SELECT b.bms_sn
  FROM devices d
  INNER JOIN devices_relation dr ON dr.device_id = d.device_id
  INNER JOIN ems e ON e.ems_id = dr.module_id
  INNER JOIN batteries b ON b.device_id = d.device_id
  WHERE e.plant_id = ?
) plant_batteries`, plantID, plantID).Scan(&batteryCount)
	if err != nil {
		return fmt.Errorf("核查电站电池：%w", err)
	}

	var activePushRelations int64
	err = q.QueryRowContext(ctx, `
SELECT COUNT(DISTINCT CONCAT(d.registration_id, ':', r.msg_type_id))
FROM plants p
INNER JOIN ns_power_outage_settings pos
        ON pos.plant_id = p.plant_id AND pos.user_id = p.related_userid
INNER JOIN msg_push_device d
        ON d.user_id = pos.user_id AND d.state = 1
INNER JOIN msg_push_relations r
        ON r.user_id = d.user_id AND r.registration_id = d.registration_id AND r.state = 1
WHERE p.plant_id = ?
  AND pos.pushFlag = 1`, plantID).Scan(&activePushRelations)
	if err != nil {
		return fmt.Errorf("核查 APP 推送关系：%w", err)
	}

	var pushDevices, activePushDevices, pushRelations, enabledPushRelations int64
	err = q.QueryRowContext(ctx, `
SELECT
  (SELECT COUNT(*)
   FROM msg_push_device d
   WHERE EXISTS (
     SELECT 1 FROM ns_power_outage_settings s
     WHERE s.plant_id = ? AND s.user_id = d.user_id)),
  (SELECT COUNT(*)
   FROM msg_push_device d
   WHERE d.state = 1 AND EXISTS (
     SELECT 1 FROM ns_power_outage_settings s
     WHERE s.plant_id = ? AND s.user_id = d.user_id)),
  (SELECT COUNT(*)
   FROM msg_push_device d
   INNER JOIN msg_push_relations r
           ON r.user_id = d.user_id AND r.registration_id = d.registration_id
   WHERE EXISTS (
     SELECT 1 FROM ns_power_outage_settings s
     WHERE s.plant_id = ? AND s.user_id = d.user_id)),
  (SELECT COUNT(*)
   FROM msg_push_device d
   INNER JOIN msg_push_relations r
           ON r.user_id = d.user_id AND r.registration_id = d.registration_id
   WHERE d.state = 1 AND r.state = 1 AND EXISTS (
     SELECT 1 FROM ns_power_outage_settings s
     WHERE s.plant_id = ? AND s.user_id = d.user_id))`,
		plantID, plantID, plantID, plantID).Scan(
		&pushDevices, &activePushDevices, &pushRelations, &enabledPushRelations)
	if err != nil {
		return fmt.Errorf("诊断 APP 推送注册链路：%w", err)
	}

	var settingOwnerMatches, ownerPushDevices, globalPushDevices int64
	err = q.QueryRowContext(ctx, `
SELECT
  (SELECT COUNT(*)
   FROM plants p
   INNER JOIN ns_power_outage_settings s
           ON s.plant_id = p.plant_id AND s.user_id = p.related_userid
   WHERE p.plant_id = ?),
  (SELECT COUNT(*)
   FROM plants p
   INNER JOIN msg_push_device d ON d.user_id = p.related_userid
   WHERE p.plant_id = ?),
  (SELECT COUNT(*) FROM msg_push_device)`, plantID, plantID).Scan(
		&settingOwnerMatches, &ownerPushDevices, &globalPushDevices)
	if err != nil {
		return fmt.Errorf("诊断电站用户与推送设备：%w", err)
	}

	fmt.Println("测试目标就绪检查：")
	fmt.Printf("  电站存在=%t，南非电站=%t，block_id 匹配=%t，时区已配置=%t\n",
		plantRows > 0, zaRows > 0, expectedBlockID == "" || matchingBlockRows > 0, timezone != "")
	fmt.Printf("  当天计划=%d，Stage 2 可用=%t，Stage 3 可用=%t\n",
		todayPlans, stage2Plans > 0, stage3Plans > 0)
	fmt.Printf("  停电设置=%d，强充已开启=%t，推送已开启=%t，提前时间=%s小时%s分钟\n",
		settingRows, reserveEnabled > 0, pushEnabled > 0, nullableInt(preChargeH, "默认3"), nullableInt(preChargeM, "默认0"))
	if deviceID != "" {
		fmt.Printf("  device_id 属于电站=%t\n", deviceBelongs > 0)
	}
	fmt.Printf("  电池记录=%d，活跃 APP 推送关系=%d\n", batteryCount, activePushRelations)
	fmt.Printf("  APP 推送链路：登记设备=%d，启用设备=%d，订阅关系=%d，启用关系=%d\n",
		pushDevices, activePushDevices, pushRelations, enabledPushRelations)
	fmt.Printf("  用户关联：停电设置属于电站用户=%t，电站用户登记手机=%d，全库登记手机=%d\n",
		settingOwnerMatches > 0, ownerPushDevices, globalPushDevices)
	return nil
}

func printIdentifierDiagnostics(ctx context.Context, q *sql.Tx, plantValue, blockValue, deviceValue string) error {
	var plantIDMatches, plantPrimaryKeyMatches int64
	if err := q.QueryRowContext(ctx, `
SELECT SUM(CASE WHEN plant_id = ? THEN 1 ELSE 0 END),
       SUM(CASE WHEN CAST(id AS CHAR) = ? THEN 1 ELSE 0 END)
FROM plants`, plantValue, plantValue).Scan(&plantIDMatches, &plantPrimaryKeyMatches); err != nil {
		return fmt.Errorf("诊断 plant 标识类型：%w", err)
	}

	var deviceIDMatches, deviceSNMatches int64
	if err := q.QueryRowContext(ctx, `
SELECT SUM(CASE WHEN device_id = ? THEN 1 ELSE 0 END),
       SUM(CASE WHEN device_sn = ? THEN 1 ELSE 0 END)
FROM devices`, deviceValue, deviceValue).Scan(&deviceIDMatches, &deviceSNMatches); err != nil {
		return fmt.Errorf("诊断 device 标识类型：%w", err)
	}

	var plantBlockMatches, regionMatches, planMatches int64
	if err := q.QueryRowContext(ctx, `
SELECT
  (SELECT COUNT(*) FROM plants WHERE block_id = ?),
  (SELECT COUNT(*) FROM ns_region WHERE area_id = ?),
  (SELECT COUNT(*) FROM ns_power_outage_info WHERE block_id = ?)`,
		blockValue, blockValue, blockValue).Scan(&plantBlockMatches, &regionMatches, &planMatches); err != nil {
		return fmt.Errorf("诊断 block_id 来源：%w", err)
	}

	fmt.Println("标识类型诊断：")
	fmt.Printf("  TEST_PLANT_ID 命中 plants.plant_id=%d，命中 plants.id=%d\n", plantIDMatches, plantPrimaryKeyMatches)
	fmt.Printf("  TEST_DEVICE_ID 命中 devices.device_id=%d，命中 devices.device_sn=%d\n", deviceIDMatches, deviceSNMatches)
	fmt.Printf("  TEST_PLANT_BLOCK_ID 命中 plants/ns_region/计划表=%d/%d/%d\n",
		plantBlockMatches, regionMatches, planMatches)
	return nil
}

func nullableInt(value sql.NullInt64, fallback string) string {
	if !value.Valid {
		return fallback
	}
	return fmt.Sprintf("%d", value.Int64)
}

func nullableDate(value sql.NullString) string {
	if !value.Valid || value.String == "" {
		return "无"
	}
	return value.String
}

func loadConfig(path string) (config, string, error) {
	absolutePath, err := filepath.Abs(path)
	if err != nil {
		return config{}, "", fmt.Errorf("解析配置路径：%w", err)
	}
	file, err := os.Open(absolutePath)
	if err != nil {
		return config{}, "", fmt.Errorf("打开配置文件：%w", err)
	}
	defer file.Close()

	var cfg config
	if err := json.NewDecoder(file).Decode(&cfg); err != nil {
		return config{}, "", fmt.Errorf("解析配置文件：%w", err)
	}
	return cfg, filepath.Dir(absolutePath), nil
}

func validateConfig(cfg *config, configDir string) error {
	if cfg.SSH.Address == "" || cfg.SSH.Username == "" || cfg.SSH.PrivateKey == "" {
		return errors.New("SSH 配置不完整")
	}
	if cfg.MySQL.Address == "" || cfg.MySQL.Username == "" || cfg.MySQL.Password == "" || cfg.MySQL.Database == "" {
		return errors.New("MySQL 配置不完整")
	}
	if _, _, err := net.SplitHostPort(cfg.SSH.Address); err != nil {
		return fmt.Errorf("ssh.address 必须是 host:port：%w", err)
	}
	if _, _, err := net.SplitHostPort(cfg.MySQL.Address); err != nil {
		return fmt.Errorf("mysql.address 必须是 host:port：%w", err)
	}
	if _, err := os.Stat(resolvePath(configDir, cfg.SSH.PrivateKey)); err != nil {
		return fmt.Errorf("访问 SSH 私钥：%w", err)
	}
	if cfg.SSH.KnownHostsFile == "" {
		cfg.SSH.KnownHostsFile = "known_hosts"
	}
	if cfg.SSH.TimeoutSeconds <= 0 {
		cfg.SSH.TimeoutSeconds = 15
	}
	if cfg.MySQL.ConnectTimeoutSeconds <= 0 {
		cfg.MySQL.ConnectTimeoutSeconds = 15
	}
	if cfg.Query.TimeoutSeconds <= 0 {
		cfg.Query.TimeoutSeconds = 60
	}
	return nil
}

func resolvePath(baseDir, configuredPath string) string {
	if filepath.IsAbs(configuredPath) {
		return filepath.Clean(configuredPath)
	}
	return filepath.Clean(filepath.Join(baseDir, configuredPath))
}

func connectSSH(cfg sshConfig, configDir string) (*ssh.Client, error) {
	privateKey, err := os.ReadFile(resolvePath(configDir, cfg.PrivateKey))
	if err != nil {
		return nil, fmt.Errorf("读取 SSH 私钥：%w", err)
	}

	var signer ssh.Signer
	if cfg.PrivateKeyPassphrase == "" {
		signer, err = ssh.ParsePrivateKey(privateKey)
	} else {
		signer, err = ssh.ParsePrivateKeyWithPassphrase(privateKey, []byte(cfg.PrivateKeyPassphrase))
	}
	if err != nil {
		return nil, fmt.Errorf("解析 SSH 私钥：%w", err)
	}

	hostKeyCallback, err := makeHostKeyCallback(cfg, configDir)
	if err != nil {
		return nil, err
	}
	client, err := ssh.Dial("tcp", cfg.Address, &ssh.ClientConfig{
		User:            cfg.Username,
		Auth:            []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback: hostKeyCallback,
		Timeout:         time.Duration(cfg.TimeoutSeconds) * time.Second,
	})
	if err != nil {
		return nil, fmt.Errorf("连接 SSH：%w", err)
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
		return nil, fmt.Errorf("读取 known_hosts：%w", err)
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
			return errors.New("SSH 服务器主机密钥已变化，已拒绝连接")
		}

		file, openErr := os.OpenFile(knownHostsPath, os.O_APPEND|os.O_WRONLY, 0o600)
		if openErr != nil {
			return fmt.Errorf("写入 known_hosts：%w", openErr)
		}
		line := knownhosts.Line([]string{hostname}, key)
		if _, writeErr := fmt.Fprintln(file, line); writeErr != nil {
			file.Close()
			return fmt.Errorf("写入 known_hosts：%w", writeErr)
		}
		if closeErr := file.Close(); closeErr != nil {
			return fmt.Errorf("关闭 known_hosts：%w", closeErr)
		}
		fmt.Printf("首次连接，已记录 SSH 主机指纹：%s\n", ssh.FingerprintSHA256(key))
		return nil
	}, nil
}

func ensureKnownHostsFile(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("创建 known_hosts 目录：%w", err)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return fmt.Errorf("创建 known_hosts：%w", err)
	}
	return file.Close()
}

func connectMySQL(cfg config, sshClient *ssh.Client) (*sql.DB, error) {
	mysql.RegisterDialContext(networkName, func(ctx context.Context, address string) (net.Conn, error) {
		return sshClient.DialContext(ctx, "tcp", address)
	})

	dsn := mysql.NewConfig()
	dsn.User = cfg.MySQL.Username
	dsn.Passwd = cfg.MySQL.Password
	dsn.Net = networkName
	dsn.Addr = cfg.MySQL.Address
	dsn.DBName = cfg.MySQL.Database
	dsn.Timeout = time.Duration(cfg.MySQL.ConnectTimeoutSeconds) * time.Second
	dsn.Params = map[string]string{"charset": "utf8mb4"}

	db, err := sql.Open("mysql", dsn.FormatDSN())
	if err != nil {
		return nil, fmt.Errorf("创建 MySQL 连接：%w", err)
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(cfg.MySQL.ConnectTimeoutSeconds)*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("连接 MySQL：%w", err)
	}
	return db, nil
}
