package main
import (
  "bytes"
  "crypto/md5"
  "crypto/tls"
  "encoding/hex"
  "encoding/json"
  "fmt"
  "net/http"
  "net/http/httputil"
  "time"
)
func main(){
  path := "/op/v2/device/scheduler/get"
  key := "306e0fb2-e812-4b76-ad76-90065f697327"
  now := time.Now().UnixNano()
  timestamp := (now + int64(500*time.Microsecond)) / int64(time.Millisecond)
  sigText := fmt.Sprintf("%s\r\n%s\r\n%d", path, key, timestamp)
  sum := md5.Sum([]byte(sigText))
  sig := hex.EncodeToString(sum[:])
  body, _ := json.Marshal(map[string]any{"deviceSN":"60MJ30303ALP026"})
  req, _ := http.NewRequest(http.MethodPost, "https://www.foxesscloud.com"+path, bytes.NewReader(body))
  req.Header["Content-Type"] = []string{"application/json"}
  req.Header["token"] = []string{key}
  req.Header["lang"] = []string{"en"}
  req.Header["timestamp"] = []string{fmt.Sprintf("%d", timestamp)}
  req.Header["signature"] = []string{sig}
  req.Header["User-Agent"] = []string{"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"}
  _ = &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}
  dump, _ := httputil.DumpRequestOut(req, true)
  fmt.Print(string(dump))
}
