@echo off
REM ==============================================================================
REM Automated Samsung Laptop Scraper - Windows Task Scheduler Setup
REM Creates a background scheduled task that runs every day at 02:00 AM
REM ==============================================================================

echo [INFO] Setting up Windows Scheduled Task for Samsung Laptop Scraper...

set TASK_NAME=SamsungLaptopScraper
set PYTHON_PATH=python
set SCRIPT_PATH=c:\Scraping\main.py
set LOG_PATH=c:\Scraping\output\task_scheduler.log

REM Check if output directory exists
if not exist "c:\Scraping\output" mkdir "c:\Scraping\output"

REM Create scheduled task to run daily at 02:00 AM
schtasks /create /tn "%TASK_NAME%" /tr "cmd.exe /c cd /d c:\Scraping && %PYTHON_PATH% %SCRIPT_PATH% >> %LOG_PATH% 2>&1" /sc daily /st 02:00 /f

if %ERRORLEVEL% equ 0 (
    echo.
    echo ==============================================================================
    echo [SUCCESS] Scheduled Task '%TASK_NAME%' created successfully!
    echo It will automatically execute daily at 02:00 AM in the background.
    echo Logs will be appended to: %LOG_PATH%
    echo.
    echo Commands to manage the task:
    echo   - Run immediately:  schtasks /run /tn "%TASK_NAME%"
    echo   - Check status:     schtasks /query /tn "%TASK_NAME%"
    echo   - Delete task:      schtasks /delete /tn "%TASK_NAME%" /f
    echo ==============================================================================
) else (
    echo.
    echo [ERROR] Failed to create scheduled task. Please run this script as Administrator.
)

pause

