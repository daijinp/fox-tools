package main

import (
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	defaultConfigName = "config.json"
	defaultPort       = 14435
	defaultPath       = "/aba/bcb/cac"
	defaultUserAgent  = "esp-idf/1.0 esp32"
	defaultSaveName   = "downloaded_cert.bin"
	defaultJSONName   = "cert_response.json"
)

type Config struct {
	Domain    string `json:"domain"`
	SN        string `json:"sn"`
	AParam    string `json:"a_param"`
	Port      int    `json:"port"`
	Path      string `json:"path"`
	UserAgent string `json:"user_agent"`
	Timeout   int    `json:"timeout"`
	VerifySSL bool   `json:"verify_ssl"`
}

type ResponsePayload struct {
	Errno  int                    `json:"errno"`
	Result map[string]interface{} `json:"result"`
}

func getAppDir() (string, error) {
	exePath, err := os.Executable()
	if err != nil {
		return "", err
	}
	return filepath.Dir(exePath), nil
}

func getConfigPath() (string, error) {
	appDir, err := getAppDir()
	if err != nil {
		return "", err
	}

	exeConfigPath := filepath.Join(appDir, defaultConfigName)
	if _, err := os.Stat(exeConfigPath); err == nil {
		return exeConfigPath, nil
	}

	wd, err := os.Getwd()
	if err != nil {
		return exeConfigPath, nil
	}
	return filepath.Join(wd, defaultConfigName), nil
}

func loadConfig() (Config, string, error) {
	var cfg Config

	configPath, err := getConfigPath()
	if err != nil {
		return cfg, "", err
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		return cfg, configPath, err
	}

	if err := json.Unmarshal(data, &cfg); err != nil {
		return cfg, configPath, err
	}

	if cfg.Port == 0 {
		cfg.Port = defaultPort
	}
	if cfg.Path == "" {
		cfg.Path = defaultPath
	}
	if cfg.UserAgent == "" {
		cfg.UserAgent = defaultUserAgent
	}
	if cfg.Timeout == 0 {
		cfg.Timeout = 15
	}

	return cfg, configPath, nil
}

func buildURL(cfg Config) string {
	return fmt.Sprintf("https://%s:%d%s?a=%s&b=%s", cfg.Domain, cfg.Port, cfg.Path, cfg.AParam, cfg.SN)
}

func newHTTPClient(verifySSL bool, timeoutSeconds int) *http.Client {
	transport := &http.Transport{
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: !verifySSL,
		},
	}

	return &http.Client{
		Transport: transport,
		Timeout:   time.Duration(timeoutSeconds) * time.Second,
	}
}

func requestCert(cfg Config) (*http.Response, error) {
	req, err := http.NewRequest(http.MethodGet, buildURL(cfg), nil)
	if err != nil {
		return nil, err
	}

	req.Host = fmt.Sprintf("%s:%d", cfg.Domain, cfg.Port)
	req.Header.Set("User-Agent", cfg.UserAgent)

	client := newHTTPClient(cfg.VerifySSL, cfg.Timeout)
	return client.Do(req)
}

func getFilenameFromHeaders(header http.Header) string {
	contentDisposition := header.Get("Content-Disposition")
	marker := "filename="
	index := strings.Index(contentDisposition, marker)
	if index == -1 {
		return defaultSaveName
	}

	filename := strings.TrimSpace(contentDisposition[index+len(marker):])
	filename = strings.Trim(filename, "\"")
	if filename == "" {
		return defaultSaveName
	}
	return filepath.Base(filename)
}

func shouldSaveFile(header http.Header, body []byte) bool {
	contentType := strings.ToLower(header.Get("Content-Type"))
	contentDisposition := strings.ToLower(header.Get("Content-Disposition"))

	if strings.Contains(contentDisposition, "attachment") || strings.Contains(contentDisposition, "filename=") {
		return true
	}

	textTypes := []string{"text/", "application/json", "application/xml", "application/javascript"}
	for _, item := range textTypes {
		if strings.HasPrefix(contentType, item) {
			return false
		}
	}

	if contentType == "" && isUTF8Like(body) {
		return false
	}

	return true
}

func isUTF8Like(body []byte) bool {
	return utf8.Valid(body)
}

func saveJSONResponse(responseText string) (string, error) {
	savePath := filepath.Join(mustGetwd(), defaultJSONName)
	err := os.WriteFile(savePath, []byte(responseText), 0o644)
	return savePath, err
}

func saveCertField(certValue string) (string, error) {
	suffix := ".txt"
	if strings.Contains(certValue, "BEGIN CERTIFICATE") {
		suffix = ".pem"
	}
	savePath := filepath.Join(mustGetwd(), "downloaded_cert"+suffix)
	err := os.WriteFile(savePath, []byte(certValue), 0o644)
	return savePath, err
}

func saveBinaryFile(filename string, body []byte) (string, error) {
	savePath := filepath.Join(mustGetwd(), filename)
	err := os.WriteFile(savePath, body, 0o644)
	return savePath, err
}

func mustGetwd() string {
	wd, err := os.Getwd()
	if err != nil {
		return "."
	}
	return wd
}

func main() {
	cfg, configPath, err := loadConfig()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to load config: %v\n", err)
		os.Exit(1)
	}

	if strings.TrimSpace(cfg.Domain) == "" || strings.TrimSpace(cfg.SN) == "" || strings.TrimSpace(cfg.AParam) == "" {
		fmt.Fprintln(os.Stderr, "Please fill in domain, sn and a_param in config.json.")
		os.Exit(1)
	}

	fmt.Printf("Config file: %s\n", configPath)
	fmt.Printf("Request URL: %s\n", buildURL(cfg))

	response, err := requestCert(cfg)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Request failed: %v\n", err)
		os.Exit(1)
	}
	defer response.Body.Close()

	body, err := io.ReadAll(response.Body)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to read response: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("HTTP %d\n", response.StatusCode)
	fmt.Println("Response headers:")
	for key, values := range response.Header {
		fmt.Printf("  %s: %s\n", key, strings.Join(values, ", "))
	}

	if shouldSaveFile(response.Header, body) {
		savePath, err := saveBinaryFile(getFilenameFromHeaders(response.Header), body)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Failed to save file: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("\nFile saved to: %s\n", savePath)
		fmt.Printf("Saved size: %d bytes\n", len(body))
		return
	}

	responseText := string(body)
	jsonPath, err := saveJSONResponse(responseText)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to save JSON: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("\nJSON saved to: %s\n", jsonPath)

	var payload ResponsePayload
	if err := json.Unmarshal(body, &payload); err != nil {
		fmt.Println("Response body:")
		fmt.Println(responseText)
		return
	}

	certValue, err := extractCertField(payload)
	if err != nil {
		fmt.Println("Response body:")
		fmt.Println(responseText)
		return
	}

	certPath, err := saveCertField(certValue)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to save certificate content: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Certificate content saved to: %s\n", certPath)
	fmt.Printf("Certificate text length: %d\n", len(certValue))
}

func extractCertField(payload ResponsePayload) (string, error) {
	if payload.Errno != 0 || payload.Result == nil {
		return "", errors.New("no certificate in response")
	}

	value, ok := payload.Result["c"]
	if !ok {
		return "", errors.New("missing result.c")
	}

	certValue, ok := value.(string)
	if !ok || certValue == "" {
		return "", errors.New("invalid result.c")
	}

	return certValue, nil
}
