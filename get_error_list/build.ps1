param()
$ErrorActionPreference = 'Stop'
$ToolRoot = $PSScriptRoot
$Python = Join-Path (Split-Path -Parent $ToolRoot) 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw "未找到上级目录虚拟环境：$Python" }
$Bits = & $Python -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0 -or $Bits -ne '64') { throw '必须使用 Windows 64 位 Python 构建。' }
& $Python -m PyInstaller --version
if ($LASTEXITCODE -ne 0) { throw '请先使用上级 venv 安装 requirements-build.txt。' }
$DistRoot = Join-Path $ToolRoot 'dist'
$ReleaseStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$Bundle = Join-Path $DistRoot ('FoxAlarmExport-win-x64_' + $ReleaseStamp)
New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
New-Item -ItemType Directory -Path $Bundle | Out-Null
& $Python -m PyInstaller --noconfirm --onefile --windowed --noupx `
    --name FoxAlarmExport --distpath $Bundle `
    --workpath (Join-Path $ToolRoot 'build') --specpath (Join-Path $ToolRoot 'build') `
    (Join-Path $ToolRoot 'run.py')
if ($LASTEXITCODE -ne 0) { throw 'EXE 构建失败。' }
Copy-Item -LiteralPath (Join-Path $ToolRoot 'config.json') -Destination (Join-Path $Bundle 'config.json') -Force
Copy-Item -LiteralPath (Join-Path $ToolRoot '使用说明.txt') -Destination (Join-Path $Bundle '使用说明.txt') -Force
# Never copy an operator's real SN list, credentials, or results into a release.
$SnFile = Join-Path $Bundle 'SN.csv'
if (-not (Test-Path -LiteralPath $SnFile)) { [IO.File]::WriteAllText($SnFile, '', [Text.UTF8Encoding]::new($true)) }
$Zip = Join-Path $DistRoot ('FoxAlarmExport-win-x64_' + $ReleaseStamp + '.zip')
Compress-Archive -LiteralPath $Bundle -DestinationPath $Zip
Get-Item -LiteralPath (Join-Path $Bundle 'FoxAlarmExport.exe'), $Zip | Select-Object FullName, Length
