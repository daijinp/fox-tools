param([string]$PythonPath)

$ErrorActionPreference = 'Stop'
$scriptDirectory = $PSScriptRoot
$workspaceDirectory = Split-Path -Parent $scriptDirectory
$scriptPath = Join-Path $scriptDirectory 'run.py'
$outputDirectory = Join-Path $scriptDirectory 'device_data'
if (-not $PythonPath) {
    foreach ($candidate in @(
        (Join-Path $workspaceDirectory 'venv\Scripts\python.exe'),
        (Join-Path $workspaceDirectory '.venv\Scripts\python.exe')
    )) {
        if (Test-Path -LiteralPath $candidate) { $PythonPath = $candidate; break }
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw '没有找到 Python 环境，请通过 -PythonPath 指定解释器。'
}
& $PythonPath -c 'import aiohttp, urllib3, jsonpath, requests'
if ($LASTEXITCODE -ne 0) { throw 'Python 环境缺少脚本依赖。' }

$running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($scriptPath) }
if ($running) { throw "脚本已经运行，PID: $($running.ProcessId -join ', ')" }
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$stdoutPath = Join-Path $outputDirectory "runner-$stamp.stdout.log"
$stderrPath = Join-Path $outputDirectory "runner-$stamp.stderr.log"
$process = Start-Process -FilePath $PythonPath -ArgumentList @('-u', ('"' + $scriptPath + '"')) `
    -WorkingDirectory $scriptDirectory -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
$launch = [ordered]@{
    pid = $process.Id
    started_at = (Get-Date).ToString('o')
    python_path = $PythonPath
    script_path = $scriptPath
    stdout = $stdoutPath
    stderr = $stderrPath
}
$launch | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $outputDirectory 'launch.json') -Encoding utf8
Start-Sleep -Seconds 3
$process.Refresh()
if ($process.HasExited) {
    throw "脚本已退出，退出码 $($process.ExitCode)。请查看 $stderrPath 和 $stdoutPath"
}
$launch | ConvertTo-Json
