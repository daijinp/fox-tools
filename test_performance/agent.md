帮我写一个性能测试脚本：
请求路径：https://test.maitian-yun.com/generic/v0/device/list
请求方法：post
请求参数：
{"pageSize":1000,"currentPage":1,"total":165039,"condition":{"status":0,"plantName":"","deviceSN":"","odmSN":"","moduleSN":"","country":"","deviceType":"","productType":"","queryDate":{"begin":0,"end":0}}}


要求：
1.请求参数写死即可、域名使用上面提供的写死即可，可以用一个变量控制写在代码的最上面
2.签名与：get_setting\run.py 这个脚本一致
3.使用go语言写，使用协程调度
4.不要用位置参数，运行脚本之后弹出命令行或者弹框（方便即可，可能在windows或者mac运行的）。弹框需要依次填写:token，并发数（默认1000），持续时间单位分钟
5.运行之后可以生成简单的日志即可，不要影响性能。
6.请求的时候可以弹框命令行滚动显示响应参数，以方便观察请求是否出问题