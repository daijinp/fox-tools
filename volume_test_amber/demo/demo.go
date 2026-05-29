package main

import (
	"bytes"
	"crypto/md5"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	debug     = true
	sleepTime = 0 * time.Second
	domain    = "https://www.foxesscloud.com"
	key       = "306e0fb2-e812-4b76-ad76-90065f697327"
)

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

var httpClient = &http.Client{
	Transport: &http.Transport{
		TLSClientConfig:   &tls.Config{InsecureSkipVerify: true},
		ForceAttemptHTTP2: false,
	},
	Timeout: 30 * time.Second,
}

func frRequests(method, path string, param any) (*http.Response, []byte, error) {
	fullURL := domain + path
	headers := auth{}.getSignature(key, path, "en")

	time.Sleep(sleepTime)

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
		req.Header["Content-Type"] = []string{"application/json"}
	default:
		return nil, nil, fmt.Errorf("request method error")
	}

	if err != nil {
		return nil, nil, err
	}

	for k, v := range headers {
		// This API appears to require lowercase custom header names exactly as sent by Python requests.
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

type device struct{}

func (d device) deviceV2SchedulerGet() (*http.Response, []byte, error) {
	path := "/op/v2/device/scheduler/get"
	requestParam := rawJSON(`{"deviceSN": "60MJ30303ALP026"}`)
	return frRequests("post", path, requestParam)
}

func (d device) deviceV2SchedulerSet() (*http.Response, []byte, error) {
	path := "/op/v2/device/scheduler/enable"
	requestParam := rawJSON(`{"groups": [{"fdPwr": 0, "fdSoc": 6, "enable": 0, "maxSoc": 80, "endHour": 3, "workMode": "ForceDischarge", "endMinute": 4, "startHour": 1, "startMinute": 2, "minSocOnGrid": 5}, {"fdPwr": 2422, "fdSoc": 7, "enable": 1, "maxSoc": 92, "endHour": 3, "workMode": "Feedin", "endMinute": 30, "startHour": 2, "startMinute": 22, "minSocOnGrid": 6}], "deviceSN": "your_device_sn"}`)
	return frRequests("post", path, requestParam)
}

func main() {
	d := device{}
	if _, _, err := d.deviceV2SchedulerGet(); err != nil {
		fmt.Printf("request failed: %v\n", err)
	}
	// if _, _, err := d.deviceV2SchedulerSet(); err != nil {
	// 	fmt.Printf("request failed: %v\n", err)
	// }
}
