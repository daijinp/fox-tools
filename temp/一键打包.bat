@echo off
chcp 65001 >nul
echo ========================================
echo    CSV数据清理工具 - 一键打包脚本
echo ========================================
echo.

:: 检查是否安装了 PyInstaller
echo [1/4] 检查环境...
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo ❌ 未检测到 PyInstaller，正在安装...
    pip install pyinstaller
    if errorlevel 1 (
        echo ❌ 安装失败！请手动运行: pip install pyinstaller
        pause
        exit /b 1
    )
) else (
    echo ✅ PyInstaller 已安装
)

:: 检查依赖
echo.
echo [2/4] 检查项目依赖...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo ❌ 依赖安装失败！
    pause
    exit /b 1
)
echo ✅ 依赖检查完成

:: 清理旧的构建文件
echo.
echo [3/4] 清理旧文件...
if exist "build" rmdir /s /q "build" 2>nul
if exist "dist" rmdir /s /q "dist" 2>nul
if exist "*.spec" del /q "*.spec" 2>nul
echo ✅ 清理完成

:: 开始打包
echo.
echo [4/4] 开始打包（这可能需要几分钟）...
pyinstaller --onefile --noconsole --name "CSV数据清理工具" run_gui.py

:: 检查打包结果
echo.
if exist "dist\CSV数据清理工具.exe" (
    echo ========================================
    echo ✅ 打包成功！
    echo ========================================
    echo.
    echo 📂 可执行文件位置：
    echo    %CD%\dist\CSV数据清理工具.exe
    echo.
    echo 💡 提示：
    echo    - 可以直接双击运行 EXE 文件
    echo    - 或者将其复制到任意位置使用
    echo    - build 和 __pycache__ 文件夹可以删除
    echo.
    
    :: 询问是否打开文件夹
    set /p open="是否打开 dist 文件夹？(Y/N): "
    if /i "%open%"=="Y" (
        explorer "dist"
    )
) else (
    echo ========================================
    echo ❌ 打包失败！
    echo ========================================
    echo.
    echo 请检查上方的错误信息
)

echo.
pause

