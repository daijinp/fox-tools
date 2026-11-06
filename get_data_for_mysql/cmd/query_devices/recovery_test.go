package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"database/sql"
	"database/sql/driver"
	"encoding/csv"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/go-sql-driver/mysql"
	"golang.org/x/crypto/ssh"
)

func fixtureCheckpoint(t *testing.T) (config, deviceSNGroup, string, *checkpointStore) {
	t.Helper()
	cfg := config{MySQL: mysqlConfig{Address: "db:3306", Database: "test", Username: "reader"}, Query: queryConfig{BatchSize: 2, MaxAttempts: 3}}
	group := deviceSNGroup{Name: "test", SourceCSV: "input.csv", DeviceSNs: []string{"A", "B", "C", "D", "E"}}
	dir := t.TempDir()
	store, err := openCheckpoint(cfg, group, dir, false)
	if err != nil {
		t.Fatal(err)
	}
	return cfg, group, dir, store
}

func TestResumeIncludesEmptyBatchesAndDoesNotDuplicate(t *testing.T) {
	cfg, group, dir, store := fixtureCheckpoint(t)
	calls := 0
	_, err := queryGroup(context.Background(), group, cfg.Query, store, func(_ context.Context, sns []string) (batchResult, error) {
		calls++
		switch calls {
		case 1:
			return batchResult{DeviceIDIsNumeric: true, AllDevices: []allDeviceRecord{{DeviceSN: "A", DeviceID: "10", ProtocolVersion: "P1"}}, OnlineDevices: []onlineDeviceRecord{{ProtocolVersion: "P1", DeviceID: "10"}}}, nil
		case 2:
			return batchResult{}, nil // A successful empty batch is committed too.
		default:
			return batchResult{AllDevices: []allDeviceRecord{{DeviceSN: "partial"}}}, io.ErrUnexpectedEOF
		}
	})
	if !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("expected disconnect, got %v", err)
	}
	if _, found, err := store.load(4, 5); err != nil || found {
		t.Fatalf("failed batch was committed: found=%v err=%v", found, err)
	}
	// Reopen from disk, as a newly started process would.
	store, err = openCheckpoint(cfg, group, dir, false)
	if err != nil {
		t.Fatal(err)
	}
	recovered, err := queryGroup(context.Background(), group, cfg.Query, store, func(_ context.Context, sns []string) (batchResult, error) {
		if !reflect.DeepEqual(sns, []string{"E"}) {
			t.Fatalf("requeried a completed batch: %v", sns)
		}
		return batchResult{DeviceIDIsNumeric: true, AllDevices: []allDeviceRecord{{DeviceSN: "E", DeviceID: "9", ProtocolVersion: "P1"}}, OnlineDevices: []onlineDeviceRecord{{ProtocolVersion: "P1", DeviceID: "9"}}}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(recovered.AllDevices) != 2 || recovered.OnlineDevices[0].DeviceID != "9" {
		t.Fatalf("wrong merged result: %+v", recovered)
	}
	allCached, err := queryGroup(context.Background(), group, cfg.Query, store, func(context.Context, []string) (batchResult, error) {
		t.Fatal("completed task queried again")
		return batchResult{}, nil
	})
	if err != nil || !reflect.DeepEqual(recovered, allCached) {
		t.Fatalf("completed result changed: %v", err)
	}
}

func TestCheckpointIdentityFreshAndCorruption(t *testing.T) {
	cfg, group, dir, store := fixtureCheckpoint(t)
	if err := store.save(0, 2, batchResult{}); err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*config, *deviceSNGroup){
		func(c *config, g *deviceSNGroup) { c.MySQL.Database = "other" },
		func(c *config, g *deviceSNGroup) { c.Query.BatchSize = 1 },
		func(c *config, g *deviceSNGroup) { g.DeviceSNs = []string{"B", "A", "C", "D", "E"} },
	} {
		changedCfg, changedGroup := cfg, group
		change(&changedCfg, &changedGroup)
		other, err := openCheckpoint(changedCfg, changedGroup, dir, false)
		if err != nil {
			t.Fatal(err)
		}
		if other.taskID == store.taskID {
			t.Fatal("changed input/target reused checkpoint")
		}
	}
	cfg.MySQL.Password = "rotated-secret"
	same, err := openCheckpoint(cfg, group, dir, false)
	if err != nil || same.taskID != store.taskID {
		t.Fatal("password rotation invalidated checkpoint")
	}
	data, err := os.ReadFile(store.path(0))
	if err != nil {
		t.Fatal(err)
	}
	var envelope checkpointEnvelope
	if err := json.Unmarshal(data, &envelope); err != nil {
		t.Fatal(err)
	}
	envelope.Payload = []byte(strings.Replace(string(envelope.Payload), `"End":2`, `"End":1`, 1))
	data, err = json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(store.path(0), data, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := store.load(0, 2); err == nil {
		t.Fatal("corruption accepted")
	}
	clean, err := openCheckpoint(cfg, group, dir, true)
	if err != nil {
		t.Fatal(err)
	}
	if _, found, err := clean.load(0, 2); err != nil || found {
		t.Fatal("fresh run reused old checkpoint")
	}
	archives, err := filepath.Glob(store.directory + ".archive-*")
	if err != nil || len(archives) != 1 {
		t.Fatalf("old checkpoint not preserved: %v %v", archives, err)
	}
}

func TestUncommittedTemporaryFileIgnored(t *testing.T) {
	_, _, _, store := fixtureCheckpoint(t)
	if err := os.WriteFile(filepath.Join(store.directory, ".batch-interrupted"), []byte(`{"partial":`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, found, err := store.load(0, 2); err != nil || found {
		t.Fatal("temporary file treated as committed")
	}
}

// A small database/sql driver exercises rows.Err(), not just QueryContext errors.
type scriptedConnector struct {
	query func(context.Context, string) (driver.Rows, error)
}

func (c scriptedConnector) Connect(context.Context) (driver.Conn, error) {
	return &scriptedConn{c.query}, nil
}
func (c scriptedConnector) Driver() driver.Driver { return scriptedDriver{} }

type scriptedDriver struct{}

func (scriptedDriver) Open(string) (driver.Conn, error) { return nil, errors.New("use connector") }

type scriptedConn struct {
	query func(context.Context, string) (driver.Rows, error)
}

func (*scriptedConn) Prepare(string) (driver.Stmt, error) { return nil, errors.New("unsupported") }
func (*scriptedConn) Close() error                        { return nil }
func (*scriptedConn) Begin() (driver.Tx, error)           { return nil, errors.New("unsupported") }
func (c *scriptedConn) QueryContext(ctx context.Context, q string, _ []driver.NamedValue) (driver.Rows, error) {
	return c.query(ctx, q)
}

type scriptedRows struct {
	values [][]driver.Value
	index  int
	after  error
	block  <-chan struct{}
}

func (*scriptedRows) Columns() []string {
	return []string{"device_id", "device_sn", "protocol_version", "master_version", "product_type", "communication"}
}
func (*scriptedRows) Close() error { return nil }
func (r *scriptedRows) ColumnTypeDatabaseTypeName(i int) string {
	if i == 0 {
		return "BIGINT"
	}
	return "VARCHAR"
}
func (r *scriptedRows) Next(dest []driver.Value) error {
	if r.index < len(r.values) {
		copy(dest, r.values[r.index])
		r.index++
		return nil
	}
	if r.block != nil {
		<-r.block
	}
	if r.after != nil {
		return r.after
	}
	return io.EOF
}

func TestPartialRowDisconnectRetriesWholeBatch(t *testing.T) {
	cfg, group, _, store := fixtureCheckpoint(t)
	group.DeviceSNs = group.DeviceSNs[:2]
	attempts := 0
	db := sql.OpenDB(scriptedConnector{query: func(context.Context, string) (driver.Rows, error) {
		attempts++
		r := &scriptedRows{values: [][]driver.Value{{"10", "A", "P1", "M1", "H3", int64(1)}}}
		if attempts == 1 {
			r.after = io.ErrUnexpectedEOF
		} else {
			r.values = append(r.values, []driver.Value{"9", "B", "P1", "M1", "H3", int64(1)})
		}
		return r, nil
	}})
	defer db.Close()
	result, err := queryGroup(context.Background(), group, cfg.Query, store, func(ctx context.Context, sns []string) (batchResult, error) {
		var result batchResult
		err := retryOperation(ctx, cfg.Query, func() error { var err error; result, err = queryBatch(ctx, db, sns); return err })
		return result, err
	})
	if err != nil {
		t.Fatal(err)
	}
	if attempts != 2 || len(result.AllDevices) != 2 || result.OnlineDevices[0].DeviceID != "9" {
		t.Fatalf("partial result duplicated: attempts=%d result=%+v", attempts, result)
	}
	saved, found, err := store.load(0, 2)
	if err != nil || !found || len(saved.AllDevices) != 2 {
		t.Fatalf("bad committed result: %+v %v", saved, err)
	}
}

func TestRetryLimitsPermanentErrorsAndCancellation(t *testing.T) {
	cfg := queryConfig{MaxAttempts: 3}
	for _, tc := range []struct {
		name string
		err  error
		want int
	}{
		{"network", io.ErrUnexpectedEOF, 3},
		{"timeout", context.DeadlineExceeded, 3},
		{"access denied", &mysql.MySQLError{Number: 1045}, 1},
		{"syntax", &mysql.MySQLError{Number: 1064}, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			count := 0
			err := retryOperation(context.Background(), cfg, func() error { count++; return tc.err })
			if !errors.Is(err, tc.err) || count != tc.want {
				t.Fatalf("attempts=%d err=%v", count, err)
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	err := retryOperation(ctx, queryConfig{MaxAttempts: 5, RetryIntervalSeconds: 30}, func() error { cancel(); return io.EOF })
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel ignored: %v", err)
	}
}

func TestCancellationDoesNotCommitBatch(t *testing.T) {
	cfg, group, _, store := fixtureCheckpoint(t)
	ctx, cancel := context.WithCancel(context.Background())
	_, err := queryGroup(ctx, group, cfg.Query, store, func(context.Context, []string) (batchResult, error) { cancel(); return batchResult{}, nil })
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if _, found, err := store.load(0, 2); err != nil || found {
		t.Fatal("canceled batch committed")
	}
}

func TestOutputLockAndReleaseAfterProcessExit(t *testing.T) {
	if dir := os.Getenv("QUERY_DEVICES_LOCK_TEST"); dir != "" {
		if _, err := lockOutput(dir); err != nil {
			os.Exit(2)
		}
		os.Exit(0) // Deliberately bypass unlock to simulate an abrupt exit.
	}
	dir := t.TempDir()
	unlock, err := lockOutput(dir)
	if err != nil {
		t.Fatal(err)
	}
	if release, err := lockOutput(dir); err == nil {
		release()
		t.Fatal("second process lock accepted")
	}
	unlock()
	cmd := exec.Command(os.Args[0], "-test.run=^TestOutputLockAndReleaseAfterProcessExit$")
	cmd.Env = append(os.Environ(), "QUERY_DEVICES_LOCK_TEST="+dir)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("child: %s %v", output, err)
	}
	unlock, err = lockOutput(dir)
	if err != nil {
		t.Fatal("lock survived process exit:", err)
	}
	unlock()
}

func TestCSVReplacementPreservesOldFileOnFailure(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "out.csv")
	if err := os.WriteFile(path, []byte("old\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := writeCSV(path, [][]string{{"new"}}); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "new\n" {
		t.Fatalf("replacement failed: %q %v", data, err)
	}
	blocked := filepath.Join(dir, "blocked.csv")
	if err := os.Mkdir(blocked, 0o700); err != nil {
		t.Fatal(err)
	}
	sentinel := filepath.Join(blocked, "keep")
	if err := os.WriteFile(sentinel, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := writeCSV(blocked, [][]string{{"data"}}); err == nil {
		t.Fatal("expected replacement failure")
	}
	if _, err := os.Stat(sentinel); err != nil {
		t.Fatal("old destination was removed")
	}
	temps, _ := filepath.Glob(filepath.Join(dir, ".query-devices-csv-*"))
	if len(temps) != 0 {
		t.Fatalf("temporary files leaked: %v", temps)
	}
}

func TestSSHHandshakeCancellation(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	done := make(chan struct{})
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		io.Copy(io.Discard, conn)
		close(done)
	}()
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	block, err := ssh.MarshalPrivateKey(key, "")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "id_rsa"), pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	client, err := connectSSH(ctx, sshConfig{Address: listener.Addr().String(), Username: "test", PrivateKey: "id_rsa", KnownHostsFile: "known_hosts", TimeoutSeconds: 30}, dir)
	if client != nil {
		client.Close()
	}
	if err == nil {
		t.Fatal("silent server handshake succeeded")
	}
	if ctx.Err() == nil {
		t.Fatalf("failed before cancellation: %v", err)
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("canceled handshake transport stayed open")
	}
}

func TestLargeInputInterruptedResumeAndCSV(t *testing.T) {
	cfg, group, dir, store := fixtureCheckpoint(t)
	cfg.Query.BatchSize = 200
	group.DeviceSNs = make([]string, 150001)
	for i := range group.DeviceSNs {
		group.DeviceSNs[i] = fmt.Sprintf("SN-%06d", i)
	}
	var err error
	store, err = openCheckpoint(cfg, group, dir, false)
	if err != nil {
		t.Fatal(err)
	}
	queries := 0
	produce := func(_ context.Context, sns []string) (batchResult, error) {
		queries++
		result := batchResult{DeviceIDIsNumeric: true}
		for _, sn := range sns {
			result.AllDevices = append(result.AllDevices, allDeviceRecord{DeviceSN: sn, DeviceID: sn})
		}
		return result, nil
	}
	_, err = queryGroup(context.Background(), group, cfg.Query, store, func(ctx context.Context, sns []string) (batchResult, error) {
		if queries == 700 {
			return batchResult{}, io.ErrUnexpectedEOF
		}
		return produce(ctx, sns)
	})
	if !errors.Is(err, io.ErrUnexpectedEOF) || queries != 700 {
		t.Fatalf("interruption: queries=%d err=%v", queries, err)
	}
	store, err = openCheckpoint(cfg, group, dir, false)
	if err != nil {
		t.Fatal(err)
	}
	queries = 0
	result, err := queryGroup(context.Background(), group, cfg.Query, store, produce)
	if err != nil || queries != 51 || len(result.AllDevices) != 150001 {
		t.Fatalf("resume: queries=%d rows=%d err=%v", queries, len(result.AllDevices), err)
	}
	path := filepath.Join(dir, "large.csv")
	if err := writeAllDevices(path, result.AllDevices); err != nil {
		t.Fatal(err)
	}
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	reader := csv.NewReader(file)
	for i, want := range group.DeviceSNs {
		row, err := reader.Read()
		if err != nil || len(row) != 5 || row[1] != want {
			t.Fatalf("CSV row %d: %v %v", i, row, err)
		}
	}
	if _, err := reader.Read(); !errors.Is(err, io.EOF) {
		t.Fatalf("extra CSV rows: %v", err)
	}
}

func TestSessionTimeoutClosesSSHAndClearsConnection(t *testing.T) {
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(key)
	if err != nil {
		t.Fatal(err)
	}
	serverConfig := &ssh.ServerConfig{NoClientAuth: true}
	serverConfig.AddHostKey(signer)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	disconnected := make(chan struct{})
	go func() {
		defer close(disconnected)
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		transport, channels, requests, err := ssh.NewServerConn(conn, serverConfig)
		if err != nil {
			return
		}
		defer transport.Close()
		go ssh.DiscardRequests(requests)
		go func() {
			for channel := range channels {
				channel.Reject(ssh.Prohibited, "test")
			}
		}()
		transport.Wait()
	}()
	publicKey := signer.PublicKey()
	client, err := ssh.Dial("tcp", listener.Addr().String(), &ssh.ClientConfig{User: "test", HostKeyCallback: ssh.FixedHostKey(publicKey), Timeout: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	db := sql.OpenDB(scriptedConnector{query: func(context.Context, string) (driver.Rows, error) {
		return &scriptedRows{block: disconnected, after: io.ErrUnexpectedEOF}, nil
	}})
	session := &querySession{cfg: config{Query: queryConfig{MaxAttempts: 1, TimeoutSeconds: 1}}, ssh: client, db: db, planChecked: true}
	defer session.close()
	err = session.execute(context.Background(), []string{"A"}, func(ctx context.Context, q queryer) error {
		_, err := queryBatch(ctx, q, []string{"A"})
		return err
	})
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("timeout not returned: %v", err)
	}
	if session.ssh != nil || session.db != nil || session.planChecked {
		t.Fatal("dead connection retained for next attempt")
	}
}
