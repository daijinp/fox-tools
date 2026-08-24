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
	inspectPush := flag.Bool("inspect-push", false, "输出推送相关表结构和停电消息类型（只读）")
	paginationTestAction := flag.String("pagination-test-action", "", "分页缺陷临时数据操作：seed 或 cleanup；默认不写库")
	paginationBaselineStage := flag.Int("pagination-baseline-stage", -1, "分页测试前的目标当天 Stage；cleanup 时恢复；默认不处理")
	missingDayTestAction := flag.String("missing-day-test-action", "", "缺失当天计划测试操作：seed 或 cleanup；默认不写库")
	timezoneTestAction := flag.String("timezone-test-action", "", "恢复任务时区测试操作：seed 或 cleanup；默认不写库")
	timezoneBaselineStage := flag.Int("timezone-baseline-stage", -1, "时区测试前的目标当天 Stage；cleanup 时恢复")
	daysBefore := flag.Int("days-before", 3, "向前核查的天数")
	daysAfter := flag.Int("days-after", 7, "向后核查的天数")
	flag.Parse()

	if *daysBefore < 0 || *daysAfter < 0 {
		fatal(errors.New("days-before 和 days-after 不能小于 0"))
	}
	if err := run(*configPath, strings.TrimSpace(*plantID), strings.TrimSpace(*blockID),
		strings.TrimSpace(*deviceID), *daysBefore, *daysAfter, *inspectPush,
		strings.TrimSpace(*paginationTestAction), *paginationBaselineStage,
		strings.TrimSpace(*missingDayTestAction), strings.TrimSpace(*timezoneTestAction),
		*timezoneBaselineStage); err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "错误：", err)
	os.Exit(1)
}

func run(configPath, plantID, blockID, deviceID string, daysBefore, daysAfter int, inspectPush bool,
	paginationTestAction string, paginationBaselineStage int, missingDayTestAction, timezoneTestAction string,
	timezoneBaselineStage int) error {
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
	if timezoneTestAction != "" {
		return runTimezoneTestAction(ctx, db, plantID, blockID, timezoneTestAction, timezoneBaselineStage)
	}
	if missingDayTestAction != "" {
		return runMissingDayTestAction(ctx, db, plantID, blockID, missingDayTestAction)
	}
	if paginationTestAction != "" {
		return runPaginationTestAction(ctx, db, blockID, paginationTestAction, paginationBaselineStage)
	}
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
	if err := printPaginationDiagnostic(ctx, tx); err != nil {
		return err
	}
	if err := printDuplicatePlanDiagnostic(ctx, tx); err != nil {
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
	if inspectPush {
		if err := printPushSchema(ctx, tx); err != nil {
			return err
		}
	}

	return tx.Commit()
}

func runTimezoneTestAction(ctx context.Context, db *sql.DB, plantID, blockID, action string, baselineStage int) error {
	if plantID == "" || blockID == "" || baselineStage < 0 {
		return errors.New("时区测试必须提供 -plant-id、-block-id 和非负的 -timezone-baseline-stage")
	}
	if action != "seed" && action != "cleanup" {
		return fmt.Errorf("不支持的 timezone-test-action：%s", action)
	}

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("开启时区测试事务：%w", err)
	}
	defer tx.Rollback()

	var matchingPlants int64
	if err := tx.QueryRowContext(ctx, `
SELECT COUNT(*) FROM plants WHERE plant_id = ? AND block_id = ?`, plantID, blockID).Scan(&matchingPlants); err != nil {
		return fmt.Errorf("核对时区测试电站：%w", err)
	}
	if matchingPlants != 1 {
		return fmt.Errorf("时区测试电站与 block_id 匹配数=%d，不是预期的 1", matchingPlants)
	}

	var currentStage int
	if err := tx.QueryRowContext(ctx, `
SELECT stage
FROM ns_power_outage_info
WHERE block_id = ? AND STR_TO_DATE(day, '%Y-%m-%d') = CURDATE()`, blockID).Scan(&currentStage); err != nil {
		return fmt.Errorf("读取时区测试目标当天 Stage：%w", err)
	}
	var settingRows, reserveEnabled int64
	if err := tx.QueryRowContext(ctx, `
SELECT COUNT(*), COALESCE(SUM(CASE WHEN power_outage_reserve = 1 THEN 1 ELSE 0 END), 0)
FROM ns_power_outage_settings
WHERE plant_id = ?`, plantID).Scan(&settingRows, &reserveEnabled); err != nil {
		return fmt.Errorf("读取时区测试强充开关：%w", err)
	}
	if settingRows != 1 {
		return fmt.Errorf("时区测试电站停电设置数=%d，不是预期的 1", settingRows)
	}

	if action == "seed" {
		if currentStage != baselineStage || reserveEnabled != 0 {
			return fmt.Errorf("时区测试基线不符：Stage=%d（预期 %d），强充开启记录=%d（预期 0）",
				currentStage, baselineStage, reserveEnabled)
		}
		if _, err := tx.ExecContext(ctx, `
UPDATE ns_power_outage_settings SET power_outage_reserve = 1 WHERE plant_id = ?`, plantID); err != nil {
			return fmt.Errorf("临时开启时区测试强充：%w", err)
		}
		if err := tx.Commit(); err != nil {
			return fmt.Errorf("提交时区测试准备：%w", err)
		}
		fmt.Printf("时区测试已准备：强充开关 0 -> 1；目标当天 Stage=%d\n", currentStage)
		return nil
	}

	if _, err := tx.ExecContext(ctx, `
UPDATE ns_power_outage_settings SET power_outage_reserve = 0 WHERE plant_id = ?`, plantID); err != nil {
		return fmt.Errorf("恢复时区测试强充开关：%w", err)
	}
	if currentStage != baselineStage {
		if _, err := tx.ExecContext(ctx, `
UPDATE ns_power_outage_info
SET stage = ?
WHERE block_id = ? AND STR_TO_DATE(day, '%Y-%m-%d') = CURDATE()`, baselineStage, blockID); err != nil {
			return fmt.Errorf("恢复时区测试目标当天 Stage：%w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("提交时区测试清理：%w", err)
	}
	fmt.Printf("时区测试数据已恢复：强充开关 -> 0；目标当天 Stage -> %d\n", baselineStage)
	return nil
}

const missingDayTestBackupDate = "2099-12-30"

func runMissingDayTestAction(ctx context.Context, db *sql.DB, plantID, blockID, action string) error {
	if plantID == "" || blockID == "" {
		return errors.New("缺失当天计划测试必须同时提供 -plant-id 和 -block-id")
	}
	if action != "seed" && action != "cleanup" {
		return fmt.Errorf("不支持的 missing-day-test-action：%s", action)
	}

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("开启缺失当天计划测试事务：%w", err)
	}
	defer tx.Rollback()

	var plantMatches int64
	if err := tx.QueryRowContext(ctx, `
SELECT COUNT(*) FROM plants WHERE plant_id = ? AND block_id = ?`, plantID, blockID).Scan(&plantMatches); err != nil {
		return fmt.Errorf("核对测试电站与 block_id：%w", err)
	}
	if plantMatches != 1 {
		return fmt.Errorf("测试电站与 block_id 匹配数=%d，不是预期的 1", plantMatches)
	}

	var currentDay string
	if err := tx.QueryRowContext(ctx, `SELECT DATE_FORMAT(CURDATE(), '%Y-%m-%d')`).Scan(&currentDay); err != nil {
		return fmt.Errorf("读取数据库当天日期：%w", err)
	}
	var currentRows, backupRows int64
	if err := tx.QueryRowContext(ctx, `
SELECT
  SUM(CASE WHEN day = ? THEN 1 ELSE 0 END),
  SUM(CASE WHEN day = ? THEN 1 ELSE 0 END)
FROM ns_power_outage_info
WHERE block_id = ?`, currentDay, missingDayTestBackupDate, blockID).Scan(&currentRows, &backupRows); err != nil {
		return fmt.Errorf("核对当天与备份日期计划：%w", err)
	}

	var settingRows, reserveEnabled int64
	if err := tx.QueryRowContext(ctx, `
SELECT COUNT(*), COALESCE(SUM(CASE WHEN power_outage_reserve = 1 THEN 1 ELSE 0 END), 0)
FROM ns_power_outage_settings
WHERE plant_id = ?`, plantID).Scan(&settingRows, &reserveEnabled); err != nil {
		return fmt.Errorf("核对测试电站强充设置：%w", err)
	}
	if settingRows != 1 {
		return fmt.Errorf("测试电站停电设置数=%d，不是预期的 1", settingRows)
	}

	if action == "seed" {
		if currentRows != 1 || backupRows != 0 {
			return fmt.Errorf("造数前当天计划=%d、备份日期计划=%d，不满足 1/0 安全条件", currentRows, backupRows)
		}
		if reserveEnabled != 0 {
			return errors.New("造数前强充开关不是关闭状态，拒绝覆盖原配置")
		}
		if _, err := tx.ExecContext(ctx, `
UPDATE ns_power_outage_info SET day = ? WHERE block_id = ? AND day = ?`,
			missingDayTestBackupDate, blockID, currentDay); err != nil {
			return fmt.Errorf("临时移走当天计划：%w", err)
		}
		if _, err := tx.ExecContext(ctx, `
UPDATE ns_power_outage_settings SET power_outage_reserve = 1 WHERE plant_id = ?`, plantID); err != nil {
			return fmt.Errorf("临时打开强充开关：%w", err)
		}
		if err := tx.Commit(); err != nil {
			return fmt.Errorf("提交缺失当天计划测试数据：%w", err)
		}
		fmt.Printf("缺失当天计划场景已构造：%s 计划已临时移至 %s；测试电站强充开关 0 -> 1\n",
			currentDay, missingDayTestBackupDate)
		return nil
	}

	if currentRows != 0 || backupRows != 1 {
		return fmt.Errorf("清理前当天计划=%d、备份日期计划=%d，不满足 0/1 安全条件", currentRows, backupRows)
	}
	if _, err := tx.ExecContext(ctx, `
UPDATE ns_power_outage_info SET day = ? WHERE block_id = ? AND day = ?`,
		currentDay, blockID, missingDayTestBackupDate); err != nil {
		return fmt.Errorf("恢复当天计划：%w", err)
	}
	if _, err := tx.ExecContext(ctx, `
UPDATE ns_power_outage_settings SET power_outage_reserve = 0 WHERE plant_id = ?`, plantID); err != nil {
		return fmt.Errorf("恢复强充开关：%w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("提交缺失当天计划测试清理：%w", err)
	}
	fmt.Printf("缺失当天计划场景已清理：%s 计划已恢复；测试电站强充开关 -> 0\n", currentDay)
	return nil
}

const (
	paginationTestStart = "2098-01-01"
	paginationTestEnd   = "2099-12-31"
)

func runPaginationTestAction(ctx context.Context, db *sql.DB, blockID, action string, baselineStage int) error {
	if blockID == "" {
		return errors.New("分页缺陷临时数据操作必须提供 -block-id")
	}
	if action != "seed" && action != "cleanup" {
		return fmt.Errorf("不支持的 pagination-test-action：%s", action)
	}

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("开启分页测试事务：%w", err)
	}
	defer tx.Rollback()

	var markerRows int64
	if err := tx.QueryRowContext(ctx, `
SELECT COUNT(*)
FROM ns_power_outage_info
WHERE block_id = ? AND day BETWEEN ? AND ?`, blockID, paginationTestStart, paginationTestEnd).Scan(&markerRows); err != nil {
		return fmt.Errorf("检查分页测试日期范围：%w", err)
	}

	if action == "cleanup" {
		if markerRows > 700 {
			return fmt.Errorf("分页测试日期范围内有 %d 条记录，超过安全删除上限 700，拒绝清理", markerRows)
		}
		result, err := tx.ExecContext(ctx, `
DELETE FROM ns_power_outage_info
WHERE block_id = ? AND day BETWEEN ? AND ?`, blockID, paginationTestStart, paginationTestEnd)
		if err != nil {
			return fmt.Errorf("清理分页测试数据：%w", err)
		}
		deleted, err := result.RowsAffected()
		if err != nil {
			return fmt.Errorf("读取分页测试清理数量：%w", err)
		}
		if baselineStage >= 0 {
			var currentStage int
			err := tx.QueryRowContext(ctx, `
SELECT stage
FROM ns_power_outage_info
WHERE block_id = ? AND STR_TO_DATE(day, '%Y-%m-%d') = CURDATE()`, blockID).Scan(&currentStage)
			if err != nil {
				return fmt.Errorf("读取分页测试目标当天 Stage：%w", err)
			}
			if currentStage != baselineStage {
				if _, err := tx.ExecContext(ctx, `
UPDATE ns_power_outage_info
SET stage = ?
WHERE block_id = ? AND STR_TO_DATE(day, '%Y-%m-%d') = CURDATE()`, baselineStage, blockID); err != nil {
					return fmt.Errorf("恢复分页测试目标当天 Stage：%w", err)
				}
			}
		}
		joinedRows, err := paginationJoinedRows(ctx, tx)
		if err != nil {
			return err
		}
		if err := tx.Commit(); err != nil {
			return fmt.Errorf("提交分页测试清理：%w", err)
		}
		fmt.Printf("分页测试数据已清理：删除=%d；清理后 JOIN 行数=%d，总页数=%d",
			deleted, joinedRows, (joinedRows+4999)/5000)
		if baselineStage >= 0 {
			fmt.Printf("；目标当天 Stage 已恢复为 %d", baselineStage)
		}
		fmt.Println()
		return nil
	}

	if markerRows != 0 {
		return fmt.Errorf("分页测试日期范围 %s~%s 已有 %d 条记录，拒绝写入", paginationTestStart, paginationTestEnd, markerRows)
	}
	joinedRows, err := paginationJoinedRows(ctx, tx)
	if err != nil {
		return err
	}
	if joinedRows > 5000 {
		return fmt.Errorf("当前 JOIN 行数已经为 %d，分页缺陷已触发，无需造数", joinedRows)
	}
	if baselineStage >= 0 {
		var matchingStageRows int64
		if err := tx.QueryRowContext(ctx, `
SELECT COUNT(*)
FROM ns_power_outage_info
WHERE block_id = ?
  AND STR_TO_DATE(day, '%Y-%m-%d') = CURDATE()
  AND stage = ?`, blockID, baselineStage).Scan(&matchingStageRows); err != nil {
			return fmt.Errorf("核对分页测试基线 Stage：%w", err)
		}
		if matchingStageRows != 1 {
			return fmt.Errorf("目标当天 Stage=%d 的记录数为 %d，不是预期的 1", baselineStage, matchingStageRows)
		}
	}

	var mappedPlants int64
	if err := tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM plants WHERE block_id = ?`, blockID).Scan(&mappedPlants); err != nil {
		return fmt.Errorf("查询 block_id 关联电站数：%w", err)
	}
	if mappedPlants <= 0 {
		return errors.New("指定 block_id 没有关联电站，无法构造 JOIN 分页数据")
	}
	rowsNeeded := (5000-joinedRows)/mappedPlants + 1
	if rowsNeeded <= 0 || rowsNeeded > 700 {
		return fmt.Errorf("计算得到需写入 %d 条，超出安全范围 1~700，拒绝写入", rowsNeeded)
	}

	var stages string
	if err := tx.QueryRowContext(ctx, `
SELECT stages
FROM ns_power_outage_info
WHERE block_id = ? AND JSON_VALID(stages) = 1 AND JSON_LENGTH(stages) = 8
ORDER BY STR_TO_DATE(day, '%Y-%m-%d') DESC
LIMIT 1`, blockID).Scan(&stages); err != nil {
		return fmt.Errorf("读取有效 Stage 模板：%w", err)
	}
	start, _ := time.Parse("2006-01-02", paginationTestStart)
	statement, err := tx.PrepareContext(ctx, `
INSERT INTO ns_power_outage_info (block_id, stages, day, stage)
VALUES (?, ?, ?, 0)`)
	if err != nil {
		return fmt.Errorf("准备分页测试数据写入：%w", err)
	}
	defer statement.Close()
	for i := int64(0); i < rowsNeeded; i++ {
		day := start.AddDate(0, 0, int(i)).Format("2006-01-02")
		if _, err := statement.ExecContext(ctx, blockID, stages, day); err != nil {
			return fmt.Errorf("写入第 %d 条分页测试数据：%w", i+1, err)
		}
	}

	afterRows, err := paginationJoinedRows(ctx, tx)
	if err != nil {
		return err
	}
	afterPages := (afterRows + 4999) / 5000
	if afterPages != 2 {
		return fmt.Errorf("写入后 JOIN 行数=%d、页数=%d，不是预期的 2 页，事务回滚", afterRows, afterPages)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("提交分页测试数据：%w", err)
	}
	fmt.Printf("分页测试数据已写入：新增=%d；block 关联电站=%d；JOIN 行数 %d -> %d；总页数=%d\n",
		rowsNeeded, mappedPlants, joinedRows, afterRows, afterPages)
	return nil
}

func paginationJoinedRows(ctx context.Context, q interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}) (int64, error) {
	var joinedRows int64
	err := q.QueryRowContext(ctx, `
SELECT COUNT(*)
FROM ns_power_outage_info a
INNER JOIN plants p ON p.block_id = a.block_id
WHERE STR_TO_DATE(a.day, '%Y-%m-%d') >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)`).Scan(&joinedRows)
	if err != nil {
		return 0, fmt.Errorf("统计分页查询 JOIN 行数：%w", err)
	}
	return joinedRows, nil
}

func printPaginationDiagnostic(ctx context.Context, q *sql.Tx) error {
	var joinedRows, pageCount, distinctPlants, distinctBlocks, distinctDays int64
	err := q.QueryRowContext(ctx, `
SELECT COUNT(*) AS joined_row_count,
       CEIL(COUNT(*) / 5000) AS page_count,
       COUNT(DISTINCT p.plant_id) AS distinct_plant_count,
       COUNT(DISTINCT a.block_id) AS distinct_block_count,
       COUNT(DISTINCT a.day) AS distinct_day_count
FROM ns_power_outage_info a
INNER JOIN plants p ON p.block_id = a.block_id
WHERE STR_TO_DATE(a.day, '%Y-%m-%d') >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)`).Scan(
		&joinedRows, &pageCount, &distinctPlants, &distinctBlocks, &distinctDays,
	)
	if err != nil {
		return fmt.Errorf("诊断停电计划分页：%w", err)
	}

	fmt.Printf("分页诊断（代码实际范围：昨天起）：JOIN 行数=%d，总页数=%d，电站=%d，block=%d，日期=%d\n",
		joinedRows, pageCount, distinctPlants, distinctBlocks, distinctDays)
	return nil
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

func printDuplicatePlanDiagnostic(ctx context.Context, q *sql.Tx) error {
	var duplicateGroups, duplicateRows int64
	err := q.QueryRowContext(ctx, `
SELECT COUNT(*), COALESCE(SUM(row_count), 0)
FROM (
  SELECT COUNT(*) AS row_count
  FROM ns_power_outage_info
  WHERE STR_TO_DATE(day, '%Y-%m-%d') >= CURDATE()
  GROUP BY block_id, day
  HAVING COUNT(*) > 1
) duplicate_plans`).Scan(&duplicateGroups, &duplicateRows)
	if err != nil {
		return fmt.Errorf("诊断未来停电计划重复数据：%w", err)
	}

	var uniqueBlockDayIndexes int64
	err = q.QueryRowContext(ctx, `
SELECT COUNT(*)
FROM (
  SELECT index_name
  FROM information_schema.statistics
  WHERE table_schema = DATABASE()
    AND table_name = 'ns_power_outage_info'
    AND non_unique = 0
  GROUP BY index_name
  HAVING COUNT(*) = 2
     AND SUM(column_name = 'block_id') = 1
     AND SUM(column_name = 'day') = 1
) unique_indexes`).Scan(&uniqueBlockDayIndexes)
	if err != nil {
		return fmt.Errorf("诊断停电计划唯一约束：%w", err)
	}

	fmt.Printf("重复计划诊断（今天起）：重复 block_id+day 组合=%d，涉及记录=%d，block_id+day 唯一索引=%t\n",
		duplicateGroups, duplicateRows, uniqueBlockDayIndexes > 0)
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
	if found {
		var stagesJSON string
		err := q.QueryRowContext(ctx, `
SELECT stages
FROM ns_power_outage_info
WHERE block_id = ?
  AND STR_TO_DATE(day, '%Y-%m-%d') = CURDATE()
LIMIT 1`, blockID).Scan(&stagesJSON)
		if errors.Is(err, sql.ErrNoRows) {
			fmt.Println("  当天无计划")
			return nil
		}
		if err != nil {
			return fmt.Errorf("读取指定 block_id 当天 Stage 时段：%w", err)
		}
		var allStages [][]string
		if err := json.Unmarshal([]byte(stagesJSON), &allStages); err != nil {
			return fmt.Errorf("解析指定 block_id 当天 Stage 时段：%w", err)
		}
		fmt.Println("  当天 Stage 1-8 时段：")
		for index, stageRanges := range allStages {
			fmt.Printf("    Stage %d：%v\n", index+1, stageRanges)
		}
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
	var todayStageValues sql.NullString
	err = q.QueryRowContext(ctx, `
SELECT COUNT(*),
       COALESCE(SUM(CASE WHEN JSON_LENGTH(JSON_EXTRACT(i.stages, '$[1]')) > 0 THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN JSON_LENGTH(JSON_EXTRACT(i.stages, '$[2]')) > 0 THEN 1 ELSE 0 END), 0),
       GROUP_CONCAT(DISTINCT COALESCE(CAST(i.stage AS CHAR), 'NULL') ORDER BY i.stage)
FROM ns_power_outage_info i
INNER JOIN plants p ON p.block_id = i.block_id
WHERE p.plant_id = ?
  AND STR_TO_DATE(i.day, '%Y-%m-%d') = CURDATE()`, plantID).Scan(
		&todayPlans, &stage2Plans, &stage3Plans, &todayStageValues)
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
	if location, locationErr := time.LoadLocation(timezone); timezone != "" && locationErr == nil {
		fmt.Printf("  电站时区=%s，当前当地时间=%s\n", timezone, time.Now().In(location).Format("2006-01-02 15:04:05"))
	}
	fmt.Printf("  当天计划=%d，Stage 2 可用=%t，Stage 3 可用=%t，当前 stage=%s\n",
		todayPlans, stage2Plans > 0, stage3Plans > 0, nullableDate(todayStageValues))
	fmt.Printf("  停电设置=%d，强充已开启=%t，推送已开启=%t，提前时间=%s小时%s分钟\n",
		settingRows, reserveEnabled > 0, pushEnabled > 0, nullableInt(preChargeH, "默认3"), nullableInt(preChargeM, "默认0"))
	if deviceID != "" {
		fmt.Printf("  device_id 属于电站=%t\n", deviceBelongs > 0)
		if err := printForceChargeState(ctx, q, deviceID); err != nil {
			return err
		}
	}
	fmt.Printf("  电池记录=%d，活跃 APP 推送关系=%d\n", batteryCount, activePushRelations)
	fmt.Printf("  APP 推送链路：登记设备=%d，启用设备=%d，订阅关系=%d，启用关系=%d\n",
		pushDevices, activePushDevices, pushRelations, enabledPushRelations)
	fmt.Printf("  用户关联：停电设置属于电站用户=%t，电站用户登记手机=%d，全库登记手机=%d\n",
		settingOwnerMatches > 0, ownerPushDevices, globalPushDevices)
	if err := printTargetPushTypes(ctx, q, plantID); err != nil {
		return err
	}
	return nil
}

func printForceChargeState(ctx context.Context, q *sql.Tx, deviceID string) error {
	var totalLogs, activeLogs int64
	var latestEnd sql.NullInt64
	err := q.QueryRowContext(ctx, `
SELECT COUNT(*),
       COALESCE(SUM(CASE WHEN f.state = 0 THEN 1 ELSE 0 END), 0),
       MAX(f.end_time)
FROM forcecharge_device_log f
INNER JOIN devices d ON d.device_sn = f.device_sn
WHERE d.device_id = ?`, deviceID).Scan(&totalLogs, &activeLogs, &latestEnd)
	if err != nil {
		return fmt.Errorf("核查目标设备强充记录：%w", err)
	}
	latestEndText := "无"
	if latestEnd.Valid {
		latestEndText = time.UnixMilli(latestEnd.Int64).Format("2006-01-02 15:04:05")
	}
	fmt.Printf("  强充记录：总数=%d，未恢复=%d，最近结束时间=%s\n", totalLogs, activeLogs, latestEndText)
	return nil
}

func printTargetPushTypes(ctx context.Context, q *sql.Tx, plantID string) error {
	rows, err := q.QueryContext(ctx, `
SELECT t.msg_type_code, r.state, t.state
FROM plants p
INNER JOIN msg_push_device d
        ON d.user_id = p.related_userid AND d.state = 1
INNER JOIN msg_push_relations r
        ON r.user_id = d.user_id AND r.registration_id = d.registration_id
INNER JOIN msg_push_type t
        ON t.msg_type_id = r.msg_type_id
WHERE p.plant_id = ?
ORDER BY t.msg_type_code`, plantID)
	if err != nil {
		return fmt.Errorf("查询测试账号停电订阅类型：%w", err)
	}
	defer rows.Close()
	fmt.Println("  目标账号全部订阅类型（类型 / relation state / type state）：")
	for rows.Next() {
		var code string
		var relationState, typeState int
		if err := rows.Scan(&code, &relationState, &typeState); err != nil {
			return err
		}
		fmt.Printf("    %s / %d / %d\n", code, relationState, typeState)
	}
	return rows.Err()
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

func printPushSchema(ctx context.Context, q *sql.Tx) error {
	rows, err := q.QueryContext(ctx, `
SELECT table_name, column_name, column_type, is_nullable, column_default, extra
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name IN ('msg_push_device', 'msg_push_relations', 'msg_push_type')
ORDER BY FIELD(table_name, 'msg_push_device', 'msg_push_relations', 'msg_push_type'), ordinal_position`)
	if err != nil {
		return fmt.Errorf("查询推送表结构：%w", err)
	}
	fmt.Println("推送表字段（表 / 字段 / 类型 / 可空 / 默认值 / extra）：")
	for rows.Next() {
		var table, column, columnType, nullable, extra string
		var defaultValue sql.NullString
		if err := rows.Scan(&table, &column, &columnType, &nullable, &defaultValue, &extra); err != nil {
			rows.Close()
			return fmt.Errorf("读取推送表结构：%w", err)
		}
		defaultText := "NULL"
		if defaultValue.Valid {
			defaultText = defaultValue.String
		}
		fmt.Printf("  %s / %s / %s / %s / %s / %s\n",
			table, column, columnType, nullable, defaultText, extra)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()

	indexRows, err := q.QueryContext(ctx, `
SELECT table_name, index_name, non_unique,
       GROUP_CONCAT(column_name ORDER BY seq_in_index SEPARATOR ',')
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name IN ('msg_push_device', 'msg_push_relations')
GROUP BY table_name, index_name, non_unique
ORDER BY table_name, index_name`)
	if err != nil {
		return fmt.Errorf("查询推送表索引：%w", err)
	}
	fmt.Println("推送表索引（表 / 索引 / non_unique / 字段）：")
	for indexRows.Next() {
		var table, indexName, columns string
		var nonUnique int
		if err := indexRows.Scan(&table, &indexName, &nonUnique, &columns); err != nil {
			indexRows.Close()
			return err
		}
		fmt.Printf("  %s / %s / %d / %s\n", table, indexName, nonUnique, columns)
	}
	if err := indexRows.Err(); err != nil {
		indexRows.Close()
		return err
	}
	indexRows.Close()

	typeRows, err := q.QueryContext(ctx, `
SELECT CAST(msg_type_id AS CHAR), msg_type_code
FROM msg_push_type
WHERE LOWER(COALESCE(msg_type_code, '')) REGEXP 'power|outage|reserve|eskom|system'
ORDER BY msg_type_code`)
	if err != nil {
		return fmt.Errorf("查询停电消息类型：%w", err)
	}
	fmt.Println("停电相关消息类型（msg_type_id / msg_type_code）：")
	for typeRows.Next() {
		var id, code string
		if err := typeRows.Scan(&id, &code); err != nil {
			typeRows.Close()
			return err
		}
		fmt.Printf("  %s / %s\n", id, code)
	}
	if err := typeRows.Err(); err != nil {
		typeRows.Close()
		return err
	}
	return typeRows.Close()
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
