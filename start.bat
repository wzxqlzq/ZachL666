@echo off
setlocal EnableExtensions

cd /d "%~dp0" || exit /b 1

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv was not found in PATH.
    echo Install uv first, then run this script again:
    echo https://docs.astral.sh/uv/getting-started/installation/
    echo.
    pause
    exit /b 1
)

if not exist "config.json" (
    if exist "config.example.json" (
        copy /Y "config.example.json" "config.json" >nul
        echo Created config.json from config.example.json.
    ) else (
        echo [ERROR] config.example.json was not found.
        pause
        exit /b 1
    )
)

if not exist "portfolio.json" (
    if exist "portfolio.example.json" (
        copy /Y "portfolio.example.json" "portfolio.json" >nul
        echo Created portfolio.json from portfolio.example.json.
    ) else (
        echo [ERROR] portfolio.example.json was not found.
        pause
        exit /b 1
    )
)

if not exist ".venv" (
    echo Creating local virtual environment and installing dependencies...
    uv sync
    if errorlevel 1 (
        echo.
        echo [ERROR] uv sync failed.
        pause
        exit /b 1
    )
)

if not "%~1"=="" (
    set "APP_ARGS=%*"
    goto run_app
)

:menu
echo.
echo A-share Turtle Alert
echo ====================
echo 1. One dry-run scan
echo 2. One real alert scan
echo 3. Continuous dry-run scan
echo 4. Continuous real alert scan
echo 5. Scheduled service dry-run
echo 6. Scheduled service real alerts
echo 0. Exit
echo.
set "APP_ARGS="
set /p "CHOICE=Select an option: "

if "%CHOICE%"=="1" set "APP_ARGS=--once --dry-run"
if "%CHOICE%"=="2" set "APP_ARGS=--once"
if "%CHOICE%"=="3" set "APP_ARGS=--loop --interval-seconds 60 --dry-run"
if "%CHOICE%"=="4" set "APP_ARGS=--loop --interval-seconds 60"
if "%CHOICE%"=="5" set "APP_ARGS=--service --dry-run"
if "%CHOICE%"=="6" set "APP_ARGS=--service"
if "%CHOICE%"=="0" exit /b 0

if not defined APP_ARGS (
    echo Invalid option.
    goto menu
)

:run_app
echo.
echo Running: uv run python main.py %APP_ARGS%
echo.
uv run python main.py %APP_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"
echo.

if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Command failed with exit code %EXIT_CODE%.
) else (
    echo Done.
)

if /i "%START_BAT_NO_PAUSE%"=="1" exit /b %EXIT_CODE%
pause
exit /b %EXIT_CODE%
