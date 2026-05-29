package main
import (
  "crypto/md5"
  "encoding/hex"
  "fmt"
)
func main() {
  path := "/op/v2/device/scheduler/get"
  token := "306e0fb2-e812-4b76-ad76-90065f697327"
  ts := int64(1776772113879)
  s := fmt.Sprintf(`%s\r\n%s\r\n%d`, path, token, ts)
  sum := md5.Sum([]byte(s))
  fmt.Println(hex.EncodeToString(sum[:]))
}
