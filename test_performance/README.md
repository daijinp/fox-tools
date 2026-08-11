# 设备列表接口性能测试

这是一个仅使用 Go 标准库的交互式压测脚本。接口域名、路径和 POST 参数都已写在 `main.go` 顶部，签名算法与 `get_setting/fr_requests.py` 一致。

## 运行

请先安装 Go 1.21 或更高版本，然后在当前目录运行：

```bash
go run .
```

程序会依次提示输入：

1. token（必填）
2. 并发数（直接回车使用默认值 1000）
3. 持续时间（单位为分钟，支持 `0.5` 这样的数值）

运行中每秒显示一次请求数、RPS、HTTP 状态统计，并抽样滚动展示最近一次响应的前 2048 bytes。这样可以观察接口响应，同时避免逐请求打印明显干扰压测结果。结束后会在运行目录生成 `performance_YYYYMMDD_HHMMSS.log` 汇总日志。

## 编译后运行（可选）

Windows：

```powershell
go build -o performance_test.exe .
.\performance_test.exe
```

macOS：

```bash
go build -o performance_test .
./performance_test
```

程序不读取位置参数，编译后的可执行文件也会使用相同的交互流程。当前实现按 `get_setting/run.py` 的行为关闭了测试环境 TLS 证书校验；不要把这一配置复制到生产请求中。
