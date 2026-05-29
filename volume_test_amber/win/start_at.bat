@echo off
setlocal

set "EXE_NAME=run.exe"
set "START_CONFIG=start_config.json"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$scriptDir = Split-Path -Parent '%~f0';" ^
  "$configPath = Join-Path $scriptDir '%START_CONFIG%';" ^
  "$exePath = Join-Path $scriptDir '%EXE_NAME%';" ^
  "if (-not (Test-Path $configPath)) { Write-Host '[ERROR] Cannot find start_config.json:'; Write-Host ('        ' + $configPath); exit 1 };" ^
  "if (-not (Test-Path $exePath)) { Write-Host '[ERROR] Cannot find run.exe:'; Write-Host ('        ' + $exePath); exit 1 };" ^
  "$cfg = Get-Content -Raw $configPath | ConvertFrom-Json;" ^
  "if ([string]::IsNullOrWhiteSpace($cfg.start_date)) { Write-Host '[ERROR] start_date is missing in start_config.json'; exit 1 };" ^
  "if ([string]::IsNullOrWhiteSpace($cfg.start_time)) { Write-Host '[ERROR] start_time is missing in start_config.json'; exit 1 };" ^
  "$targetText = ($cfg.start_date.Trim() + ' ' + $cfg.start_time.Trim());" ^
  "try { $target = [datetime]::ParseExact($targetText, 'yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture) } catch { Write-Host '[ERROR] Invalid target time format. Use yyyy-MM-dd and HH:mm:ss'; Write-Host ('        Current value: ' + $targetText); exit 1 };" ^
  "Write-Host ('[INFO] Working directory: ' + $scriptDir);" ^
  "Write-Host ('[INFO] Config file      : ' + $configPath);" ^
  "Write-Host ('[INFO] Target start time: ' + $target.ToString('yyyy-MM-dd HH:mm:ss'));" ^
  "Write-Host '[INFO] Waiting for launch...';" ^
  "Write-Host '';" ^
  "while ($true) { $now = Get-Date; Write-Host ('[INFO] Current time: ' + $now.ToString('yyyy-MM-dd HH:mm:ss')); if ($now -ge $target) { break }; Start-Sleep -Seconds 1 };" ^
  "Write-Host '';" ^
  "Write-Host ('[INFO] Target time reached. Starting ' + '%EXE_NAME%' + ' ...');" ^
  "Start-Process -FilePath $exePath -WorkingDirectory $scriptDir;" ^
  "Write-Host '[INFO] Launch command sent.'"

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] start_at.bat failed with exit code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
)

endlocal
exit /b 0
