package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"
)

const checkpointVersion = 1

type batchResult struct {
	AllDevices        []allDeviceRecord
	OnlineDevices     []onlineDeviceRecord
	DeviceIDIsNumeric bool
}

type savedBatch struct {
	Version int
	TaskID  string
	Start   int
	End     int
	Result  batchResult
}

// The checksum covers the exact payload bytes, including empty successful batches.
type checkpointEnvelope struct {
	Checksum string          `json:"sha256"`
	Payload  json.RawMessage `json:"payload"`
}

type checkpointStore struct {
	directory string
	taskID    string
}

func digest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func openCheckpoint(cfg config, group deviceSNGroup, outputDir string, fresh bool) (*checkpointStore, error) {
	// Passwords and keys are deliberately excluded. Changing credentials must not
	// invalidate work, but changing the effective input, SQL or target must do so.
	identity := struct {
		Version                                                int
		SSHAddress, SSHUser, MySQLAddress, Database, MySQLUser string
		Source, Group, SQL                                     string
		BatchSize                                              int
		SNs                                                    []string
	}{checkpointVersion, cfg.SSH.Address, cfg.SSH.Username, cfg.MySQL.Address,
		cfg.MySQL.Database, cfg.MySQL.Username, group.SourceCSV, group.Name,
		buildDeviceQuery(1), cfg.Query.BatchSize, group.DeviceSNs}
	encoded, err := json.Marshal(identity)
	if err != nil {
		return nil, err
	}
	id := digest(encoded)
	directory := filepath.Join(outputDir, "checkpoints", "query_devices", id)
	if fresh {
		if _, err := os.Stat(directory); err == nil {
			archive := directory + ".archive-" + time.Now().Format("20060102-150405.000000000")
			if err := os.Rename(directory, archive); err != nil {
				return nil, fmt.Errorf("归档断点：%w", err)
			}
			fmt.Printf("旧断点已归档：%s\n", archive)
		} else if !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return nil, fmt.Errorf("创建断点目录：%w", err)
	}
	return &checkpointStore{directory: directory, taskID: id}, nil
}

func (s *checkpointStore) path(start int) string {
	return filepath.Join(s.directory, fmt.Sprintf("batch_%09d.json", start))
}

func (s *checkpointStore) load(start, end int) (batchResult, bool, error) {
	data, err := os.ReadFile(s.path(start))
	if errors.Is(err, os.ErrNotExist) {
		return batchResult{}, false, nil
	}
	if err != nil {
		return batchResult{}, false, err
	}
	var envelope checkpointEnvelope
	if err := json.Unmarshal(data, &envelope); err != nil {
		return batchResult{}, false, fmt.Errorf("断点损坏 %s：%w", s.path(start), err)
	}
	if digest(envelope.Payload) != envelope.Checksum {
		return batchResult{}, false, fmt.Errorf("断点校验失败：%s；请保留现场并使用 -fresh 重新查询", s.path(start))
	}
	var saved savedBatch
	if err := json.Unmarshal(envelope.Payload, &saved); err != nil {
		return batchResult{}, false, err
	}
	if saved.Version != checkpointVersion || saved.TaskID != s.taskID || saved.Start != start || saved.End != end {
		return batchResult{}, false, fmt.Errorf("断点与任务不匹配：%s", s.path(start))
	}
	return saved.Result, true, nil
}

func (s *checkpointStore) save(start, end int, result batchResult) error {
	payload, err := json.Marshal(savedBatch{checkpointVersion, s.taskID, start, end, result})
	if err != nil {
		return err
	}
	data, err := json.Marshal(checkpointEnvelope{digest(payload), payload})
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(s.directory, ".batch-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	if _, err := file.Write(data); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := replaceFile(file.Name(), s.path(start)); err != nil {
		return err
	}
	return nil
}

type batchFetcher func(context.Context, []string) (batchResult, error)

func queryGroup(ctx context.Context, group deviceSNGroup, cfg queryConfig, store *checkpointStore, fetch batchFetcher) (groupQueryResult, error) {
	result := groupQueryResult{Group: group}
	minimums := make(map[string]string)
	total := (len(group.DeviceSNs) + cfg.BatchSize - 1) / cfg.BatchSize
	queried := false
	for start := 0; start < len(group.DeviceSNs); start += cfg.BatchSize {
		if err := ctx.Err(); err != nil {
			return result, err
		}
		end := min(start+cfg.BatchSize, len(group.DeviceSNs))
		batch, cached, err := store.load(start, end)
		if err != nil {
			return result, err
		}
		if !cached {
			if queried {
				if err := waitContext(ctx, time.Duration(cfg.BatchIntervalSeconds)*time.Second); err != nil {
					return result, err
				}
			}
			batch, err = fetch(ctx, group.DeviceSNs[start:end])
			if err != nil {
				return result, fmt.Errorf("批次 %d/%d（SN %d-%d）：%w", start/cfg.BatchSize+1, total, start+1, end, err)
			}
			if err := ctx.Err(); err != nil {
				return result, err
			}
			if err := store.save(start, end, batch); err != nil {
				return result, fmt.Errorf("保存批次断点：%w", err)
			}
			queried = true
		}
		result.AllDevices = append(result.AllDevices, batch.AllDevices...)
		for _, row := range batch.OnlineDevices {
			current, exists := minimums[row.ProtocolVersion]
			if !exists || deviceIDLess(row.DeviceID, current, batch.DeviceIDIsNumeric) {
				minimums[row.ProtocolVersion] = row.DeviceID
			}
		}
		status := "已查询并保存"
		if cached {
			status = "已从断点恢复"
		}
		fmt.Printf("分组 %s 批次 %d/%d %s（SN：%d，结果：%d）\n", group.Name, start/cfg.BatchSize+1, total, status, end-start, len(batch.AllDevices))
	}
	sort.Slice(result.AllDevices, func(i, j int) bool {
		a, b := result.AllDevices[i], result.AllDevices[j]
		if a.DeviceSN != b.DeviceSN {
			return a.DeviceSN < b.DeviceSN
		}
		if a.ProtocolVersion != b.ProtocolVersion {
			return a.ProtocolVersion < b.ProtocolVersion
		}
		return a.DeviceID < b.DeviceID
	})
	for protocol, id := range minimums {
		result.OnlineDevices = append(result.OnlineDevices, onlineDeviceRecord{ProtocolVersion: protocol, DeviceID: id})
	}
	sort.Slice(result.OnlineDevices, func(i, j int) bool {
		return result.OnlineDevices[i].ProtocolVersion < result.OnlineDevices[j].ProtocolVersion
	})
	return result, nil
}

func waitContext(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return ctx.Err()
	}
}
