package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"golang.org/x/crypto/ssh"
)

func TestReadDeviceSNsUsesFirstColumnAndDeduplicates(t *testing.T) {
	tempDir := t.TempDir()
	firstPath := filepath.Join(tempDir, "first.csv")
	secondPath := filepath.Join(tempDir, "second.csv")
	if err := os.WriteFile(firstPath, []byte("\uFEFFSN-001,,,\r\nSN-002,x,y\r\n,ignored\r\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(secondPath, []byte("SN-002,duplicate\nSN-003\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	deviceSNs, stats, err := readDeviceSNs([]string{"first.csv", "second.csv"}, tempDir)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"SN-001", "SN-002", "SN-003"}
	if !reflect.DeepEqual(deviceSNs, want) {
		t.Fatalf("device SNs = %#v, want %#v", deviceSNs, want)
	}
	if stats.Rows != 5 || stats.Empty != 1 || stats.Duplicates != 1 {
		t.Fatalf("stats = %+v", stats)
	}
}

func TestReadDeviceSNGroupsKeepsSourcesSeparate(t *testing.T) {
	tempDir := t.TempDir()
	firstPath := filepath.Join(tempDir, "单相.csv")
	secondPath := filepath.Join(tempDir, "三相.csv")
	if err := os.WriteFile(firstPath, []byte("SHARED\nSINGLE\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(secondPath, []byte("SHARED\nTHREE\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	groups, err := readDeviceSNGroups([]string{"单相.csv", "三相.csv"}, tempDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(groups) != 2 {
		t.Fatalf("got %d groups, want 2", len(groups))
	}
	if groups[0].Name != "单相" || !reflect.DeepEqual(groups[0].DeviceSNs, []string{"SHARED", "SINGLE"}) {
		t.Fatalf("first group = %+v", groups[0])
	}
	if groups[1].Name != "三相" || !reflect.DeepEqual(groups[1].DeviceSNs, []string{"SHARED", "THREE"}) {
		t.Fatalf("second group = %+v", groups[1])
	}
}

func TestPlaceholders(t *testing.T) {
	if got := placeholders(3); got != "?,?,?" {
		t.Fatalf("placeholders(3) = %q", got)
	}
	if got := placeholders(0); got != "" {
		t.Fatalf("placeholders(0) = %q", got)
	}
}

func TestDeviceIDLess(t *testing.T) {
	if !deviceIDLess("9", "10", true) {
		t.Fatal("numeric device ID comparison should treat 9 as less than 10")
	}
	if deviceIDLess("9", "10", false) {
		t.Fatal("string device ID comparison should treat 9 as greater than 10")
	}
}

func TestNumericDatabaseType(t *testing.T) {
	for _, databaseType := range []string{"BIGINT", "BIGINT UNSIGNED", "UNSIGNED BIGINT", "DECIMAL(20,0)"} {
		if !isNumericDatabaseType(databaseType) {
			t.Fatalf("%q should be treated as numeric", databaseType)
		}
	}
	if isNumericDatabaseType("VARCHAR") {
		t.Fatal("VARCHAR should not be treated as numeric")
	}
}

func TestArchiveLegacyCombinedOutputs(t *testing.T) {
	outputDir := t.TempDir()
	for _, name := range []string{allDeviceOutputName, onlineDeviceOutputName} {
		if err := os.WriteFile(filepath.Join(outputDir, name), []byte("old"), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	archivedPaths, err := archiveLegacyCombinedOutputs(outputDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(archivedPaths) != 2 {
		t.Fatalf("got %d archived paths, want 2", len(archivedPaths))
	}
	for _, name := range []string{allDeviceOutputName, onlineDeviceOutputName} {
		if _, err := os.Stat(filepath.Join(outputDir, name)); !os.IsNotExist(err) {
			t.Fatalf("legacy output %s still exists in output root", name)
		}
		if _, err := os.Stat(filepath.Join(outputDir, "legacy_combined", name)); err != nil {
			t.Fatalf("archived output %s is missing: %v", name, err)
		}
	}
}

func TestOutputCSVsHaveNoHeader(t *testing.T) {
	tempDir := t.TempDir()
	allDevicePath := filepath.Join(tempDir, "all.csv")
	if err := writeAllDevices(allDevicePath, []allDeviceRecord{{
		DeviceID:        "1",
		DeviceSN:        "SN-1",
		ProtocolVersion: "P1",
		MasterVersion:   "M1",
		ProductType:     "H3",
	}}); err != nil {
		t.Fatal(err)
	}
	allDeviceContent, err := os.ReadFile(allDevicePath)
	if err != nil {
		t.Fatal(err)
	}
	if string(allDeviceContent) != "1,SN-1,P1,M1,H3\n" {
		t.Fatalf("all-device CSV = %q", allDeviceContent)
	}

	onlineDevicePath := filepath.Join(tempDir, "online.csv")
	if err := writeOnlineDevices(onlineDevicePath, []onlineDeviceRecord{{
		ProtocolVersion: "P1",
		DeviceID:        "1",
	}}); err != nil {
		t.Fatal(err)
	}
	onlineDeviceContent, err := os.ReadFile(onlineDevicePath)
	if err != nil {
		t.Fatal(err)
	}
	if string(onlineDeviceContent) != "P1,1\n" {
		t.Fatalf("online-device CSV = %q", onlineDeviceContent)
	}
}

func TestHostKeyIsRememberedAndChecked(t *testing.T) {
	firstPublicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	firstSSHKey, err := ssh.NewPublicKey(firstPublicKey)
	if err != nil {
		t.Fatal(err)
	}

	tempDir := t.TempDir()
	cfg := sshConfig{KnownHostsFile: "known_hosts"}
	callback, err := makeHostKeyCallback(cfg, tempDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := callback("example:22", &net.TCPAddr{}, firstSSHKey); err != nil {
		t.Fatalf("first host key rejected: %v", err)
	}

	callback, err = makeHostKeyCallback(cfg, tempDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := callback("example:22", &net.TCPAddr{}, firstSSHKey); err != nil {
		t.Fatalf("remembered host key rejected: %v", err)
	}

	secondPublicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	secondSSHKey, err := ssh.NewPublicKey(secondPublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := callback("example:22", &net.TCPAddr{}, secondSSHKey); err == nil {
		t.Fatal("changed host key was accepted")
	}
}
