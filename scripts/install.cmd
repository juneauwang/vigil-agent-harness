@echo off
REM ============================================================================
REM Hermes Agent Installer for Windows (CMD wrapper)
REM ============================================================================
REM This batch file launches the PowerShell installer for users running CMD.
REM
REM Usage:
REM   curl -fsSL https://raw.githubusercontent.com/juneauwang/vigil-agent-harness/main/scripts/install.cmd -o install.cmd && install.cmd && del install.cmd
REM
REM Or if you're already in PowerShell, use the direct command instead:
REM   iex (irm https://github.com/juneauwang/vigil-agent-harness)
REM ============================================================================

echo.
echo  Hermes Agent Installer
echo  Launching PowerShell installer...
echo.

powershell -ExecutionPolicy ByPass -NoProfile -Command "iex (irm https://github.com/juneauwang/vigil-agent-harness)"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  Installation failed. Please try running PowerShell directly:
    echo    powershell -ExecutionPolicy ByPass -c "iex (irm https://github.com/juneauwang/vigil-agent-harness)"
    echo.
    pause
    exit /b 1
)
