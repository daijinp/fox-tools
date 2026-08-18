package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/csv"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"
)

type cleanResult struct {
	read         int
	written      int
	extraColumns int
	emptySkipped int
	backupPath   string
	changed      bool
}

func main() {
	flag.Usage = func() {
		fmt.Fprintf(flag.CommandLine.Output(), "用法：\n  go run ./cmd/clean_csv [CSV 文件或目录...]\n\n")
		fmt.Fprintln(flag.CommandLine.Output(), "不传参数时，清洗脚本同目录 sns 文件夹下的全部 CSV。")
		fmt.Fprintln(flag.CommandLine.Output(), "传入目录时，清洗该目录下的全部 CSV（不递归）。")
	}
	flag.Parse()

	paths, err := collectCSVPaths(flag.Args())
	if err != nil {
		fmt.Fprintln(os.Stderr, "错误：", err)
		os.Exit(1)
	}

	failed := false
	for _, path := range paths {
		result, err := cleanCSV(path)
		if err != nil {
			failed = true
			fmt.Fprintf(os.Stderr, "清洗失败 %s：%v\n", path, err)
			continue
		}

		if !result.changed {
			fmt.Printf("无需清洗 %s（有效 SN：%d）\n", path, result.written)
			continue
		}

		fmt.Printf(
			"清洗完成 %s（读取：%d，写入：%d，忽略后续列：%d 行，跳过空 SN：%d，备份：%s）\n",
			path,
			result.read,
			result.written,
			result.extraColumns,
			result.emptySkipped,
			result.backupPath,
		)
	}

	if failed {
		os.Exit(1)
	}
}

func collectCSVPaths(args []string) ([]string, error) {
	if len(args) == 0 {
		defaultDir, err := defaultSNSDir()
		if err != nil {
			return nil, err
		}
		args = []string{defaultDir}
	}

	seen := make(map[string]struct{})
	var paths []string
	for _, arg := range args {
		path, err := filepath.Abs(arg)
		if err != nil {
			return nil, fmt.Errorf("解析路径 %q：%w", arg, err)
		}

		info, err := os.Stat(path)
		if err != nil {
			return nil, fmt.Errorf("访问 %q：%w", arg, err)
		}

		if !info.IsDir() {
			if !strings.EqualFold(filepath.Ext(path), ".csv") {
				return nil, fmt.Errorf("%q 不是 CSV 文件", arg)
			}
			if _, ok := seen[path]; !ok {
				seen[path] = struct{}{}
				paths = append(paths, path)
			}
			continue
		}

		entries, err := os.ReadDir(path)
		if err != nil {
			return nil, fmt.Errorf("读取目录 %q：%w", arg, err)
		}
		for _, entry := range entries {
			if entry.IsDir() || !strings.EqualFold(filepath.Ext(entry.Name()), ".csv") {
				continue
			}
			csvPath := filepath.Join(path, entry.Name())
			if _, ok := seen[csvPath]; ok {
				continue
			}
			seen[csvPath] = struct{}{}
			paths = append(paths, csvPath)
		}
	}

	if len(paths) == 0 {
		return nil, errors.New("没有找到 CSV 文件")
	}
	sort.Strings(paths)
	return paths, nil
}

func defaultSNSDir() (string, error) {
	var candidates []string
	if _, sourceFile, _, ok := runtime.Caller(0); ok {
		if absoluteSource, err := filepath.Abs(sourceFile); err == nil {
			candidates = append(candidates, filepath.Join(filepath.Dir(absoluteSource), "sns"))
		}
	}
	if workingDir, err := os.Getwd(); err == nil {
		candidates = append(
			candidates,
			filepath.Join(workingDir, "sns"),
			filepath.Join(workingDir, "get_data_for_mysql", "sns"),
		)
	}

	seen := make(map[string]struct{})
	for _, candidate := range candidates {
		candidate = filepath.Clean(candidate)
		if _, ok := seen[candidate]; ok {
			continue
		}
		seen[candidate] = struct{}{}

		info, err := os.Stat(candidate)
		if err == nil && info.IsDir() {
			return candidate, nil
		}
	}

	return "", errors.New("找不到 sns 目录；请显式传入 CSV 文件或目录")
}

func cleanCSV(path string) (result cleanResult, returnedErr error) {
	source, err := os.Open(path)
	if err != nil {
		return result, err
	}

	info, err := source.Stat()
	if err != nil {
		source.Close()
		return result, err
	}

	temp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".clean-*")
	if err != nil {
		source.Close()
		return result, err
	}
	tempPath := temp.Name()
	defer func() {
		if returnedErr != nil || !result.changed {
			_ = os.Remove(tempPath)
		}
	}()

	reader := csv.NewReader(bufio.NewReaderSize(source, 64*1024))
	reader.FieldsPerRecord = -1

	bufferedWriter := bufio.NewWriterSize(temp, 64*1024)
	writer := csv.NewWriter(bufferedWriter)

	for {
		record, readErr := reader.Read()
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			source.Close()
			temp.Close()
			return result, fmt.Errorf("解析 CSV：%w", readErr)
		}

		result.read++
		if len(record) > 1 {
			result.extraColumns++
		}
		if len(record) == 0 {
			result.emptySkipped++
			continue
		}

		deviceSN := strings.TrimSpace(record[0])
		deviceSN = strings.TrimSpace(strings.TrimPrefix(deviceSN, "\uFEFF"))
		if deviceSN == "" {
			result.emptySkipped++
			continue
		}

		if err := writer.Write([]string{deviceSN}); err != nil {
			source.Close()
			temp.Close()
			return result, fmt.Errorf("写入临时文件：%w", err)
		}
		result.written++
	}

	writer.Flush()
	if err := writer.Error(); err != nil {
		source.Close()
		temp.Close()
		return result, fmt.Errorf("写入临时文件：%w", err)
	}
	if err := bufferedWriter.Flush(); err != nil {
		source.Close()
		temp.Close()
		return result, fmt.Errorf("刷新临时文件：%w", err)
	}
	if err := temp.Sync(); err != nil {
		source.Close()
		temp.Close()
		return result, fmt.Errorf("同步临时文件：%w", err)
	}
	if err := temp.Chmod(info.Mode()); err != nil {
		source.Close()
		temp.Close()
		return result, fmt.Errorf("设置临时文件权限：%w", err)
	}
	if err := source.Close(); err != nil {
		temp.Close()
		return result, fmt.Errorf("关闭源文件：%w", err)
	}
	if err := temp.Close(); err != nil {
		return result, fmt.Errorf("关闭临时文件：%w", err)
	}

	same, err := filesEqual(path, tempPath)
	if err != nil {
		return result, fmt.Errorf("比较清洗结果：%w", err)
	}
	if same {
		return result, nil
	}

	backupPath, err := availableBackupPath(path)
	if err != nil {
		return result, err
	}
	if err := os.Rename(path, backupPath); err != nil {
		return result, fmt.Errorf("创建备份：%w", err)
	}
	if err := os.Rename(tempPath, path); err != nil {
		restoreErr := os.Rename(backupPath, path)
		if restoreErr != nil {
			return result, fmt.Errorf("替换源文件失败：%v；恢复备份也失败：%w", err, restoreErr)
		}
		return result, fmt.Errorf("替换源文件失败，已恢复原文件：%w", err)
	}

	result.changed = true
	result.backupPath = backupPath
	return result, nil
}

func filesEqual(firstPath, secondPath string) (bool, error) {
	first, err := os.Open(firstPath)
	if err != nil {
		return false, err
	}
	defer first.Close()

	second, err := os.Open(secondPath)
	if err != nil {
		return false, err
	}
	defer second.Close()

	firstInfo, err := first.Stat()
	if err != nil {
		return false, err
	}
	secondInfo, err := second.Stat()
	if err != nil {
		return false, err
	}
	if firstInfo.Size() != secondInfo.Size() {
		return false, nil
	}

	firstHash := sha256.New()
	if _, err := io.Copy(firstHash, first); err != nil {
		return false, err
	}
	secondHash := sha256.New()
	if _, err := io.Copy(secondHash, second); err != nil {
		return false, err
	}

	return bytes.Equal(firstHash.Sum(nil), secondHash.Sum(nil)), nil
}

func availableBackupPath(path string) (string, error) {
	backupPath := path + ".bak"
	if _, err := os.Stat(backupPath); errors.Is(err, os.ErrNotExist) {
		return backupPath, nil
	} else if err != nil {
		return "", fmt.Errorf("检查备份路径：%w", err)
	}

	timestamp := time.Now().Format("20060102-150405")
	for index := 1; index <= 999; index++ {
		backupPath = fmt.Sprintf("%s.bak.%s.%03d", path, timestamp, index)
		if _, err := os.Stat(backupPath); errors.Is(err, os.ErrNotExist) {
			return backupPath, nil
		} else if err != nil {
			return "", fmt.Errorf("检查备份路径：%w", err)
		}
	}

	return "", errors.New("无法生成可用的备份文件名")
}
