#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Set up Windows Task Scheduler for moomoo-trader.

.DESCRIPTION
    Registers 3 scheduled tasks:
      1. MoomooTrader-OpenD   Mon-Fri 23:20  Start OpenD.exe
      2. MoomooTrader-Bot     Mon-Fri 23:25  Start python src/main.py
      3. MoomooTrader-Stop    Tue-Sat 06:10  Stop Bot + OpenD
                              (Mon night trade -> Tue morning stop)

    US holidays are NOT auto-detected. Disable tasks manually (see commands below).

    Run as Administrator:
      PowerShell -ExecutionPolicy Bypass -File scripts\setup_scheduler.ps1

.NOTES
    Existing tasks with the same name will be overwritten.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$OpenDDir    = Join-Path $ProjectRoot "moomoo_OpenD_10.2.6208_Windows\moomoo_OpenD_10.2.6208_Windows"
$OpenDExe    = Join-Path $OpenDDir "OpenD.exe"
$PythonExe   = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$MainPy      = Join-Path $ProjectRoot "src\main.py"
$StopScript  = Join-Path $ProjectRoot "scripts\stop_all.ps1"

# Validation
if (-not (Test-Path $OpenDExe)) {
    Write-Error "OpenD.exe not found: $OpenDExe"
    exit 1
}
if (-not $PythonExe) {
    Write-Error "python not found in PATH"
    exit 1
}
if (-not (Test-Path $MainPy)) {
    Write-Error "src/main.py not found: $MainPy"
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " moomoo-trader Task Scheduler Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Project:  $ProjectRoot"
Write-Host "  OpenD:    $OpenDExe"
Write-Host "  Python:   $PythonExe"
Write-Host "  main.py:  $MainPy"
Write-Host ""

# Password for task scheduler (run whether logged on or not)
$TaskUser = Read-Host "Task user (default: $env:USERNAME)"
if (-not $TaskUser) { $TaskUser = $env:USERNAME }
$cred = Get-Credential -UserName $TaskUser -Message "Enter Windows password for Task Scheduler"
$UserPassword = $cred.GetNetworkCredential().Password

# ---------------------------------------------------------------------------
# Generate stop script
# ---------------------------------------------------------------------------

$StopScriptContent = @'
# moomoo-trader stop script (called by Task Scheduler)
$ErrorActionPreference = "SilentlyContinue"

# Stop Bot (python)
$bots = Get-Process python | Where-Object {
    $_.CommandLine -like "*src\main.py*" -or
    $_.CommandLine -like "*src/main.py*"
}
if ($bots) {
    $bots | ForEach-Object {
        Write-Host "[STOP] Bot stopped: PID $($_.Id)"
        Stop-Process -Id $_.Id -Force
    }
} else {
    # Fallback: stop all python processes
    Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "[STOP] Python stopped: PID $($_.Id)"
        Stop-Process -Id $_.Id -Force
    }
}

# Stop OpenD
Get-Process OpenD -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "[STOP] OpenD stopped: PID $($_.Id)"
    Stop-Process -Id $_.Id -Force
}

Write-Host "[STOP] All processes stopped"
'@

Set-Content -Path $StopScript -Value $StopScriptContent -Encoding ASCII
Write-Host "[OK] Stop script generated: $StopScript" -ForegroundColor Green

# ---------------------------------------------------------------------------
# Helper: Register task
# ---------------------------------------------------------------------------

function Register-MoomooTask {
    param(
        [string]$TaskName,
        [string]$Description,
        [string]$Time,
        [string[]]$DaysOfWeek,
        [string]$Execute,
        [string]$Arguments = "",
        [string]$WorkingDirectory = ""
    )

    # Remove existing task
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  Removed existing task: $TaskName" -ForegroundColor Yellow
    }

    # Trigger: weekly on specified days
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $Time

    # Action
    $actionParams = @{
        Execute = $Execute
    }
    if ($Arguments) {
        $actionParams.Argument = $Arguments
    }
    if ($WorkingDirectory) {
        $actionParams.WorkingDirectory = $WorkingDirectory
    }
    $action = New-ScheduledTaskAction @actionParams

    # Settings
    $taskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero)  # No time limit

    # Principal: Password mode — runs with full user credentials even when logged off
    # (S4U fails with futu SDK, Interactive freezes when monitor off)
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Password `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $Description `
        -Trigger $trigger `
        -Action $action `
        -Settings $taskSettings `
        -User $TaskUser `
        -Password $UserPassword `
        -RunLevel Highest | Out-Null

    Write-Host "  [OK] $TaskName ($Time)" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Schedule times (JST)
#   DST (Mar-Nov): US market 22:30-05:00 JST -> start 22:10, stop 05:10
#   EST (Nov-Mar): US market 23:30-06:00 JST -> start 23:10, stop 06:10
# Change these values when DST transitions occur, or run this script again.
# ---------------------------------------------------------------------------

$OpenDTime = "22:10"   # Start OpenD 20 min before market open
$BotTime   = "22:15"   # Start Bot 15 min before market open
$StopTime  = "05:10"   # Stop 10 min after market close

# ---------------------------------------------------------------------------
# Task 1: Start OpenD (Mon-Fri)
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "--- Registering tasks ---" -ForegroundColor Cyan

Register-MoomooTask `
    -TaskName "MoomooTrader-OpenD" `
    -Description "Start moomoo OpenD (Mon-Fri $OpenDTime JST, DST)" `
    -Time $OpenDTime `
    -DaysOfWeek @("Monday","Tuesday","Wednesday","Thursday","Friday") `
    -Execute $OpenDExe `
    -WorkingDirectory $OpenDDir

# ---------------------------------------------------------------------------
# Task 2: Start Bot (Mon-Fri)
# ---------------------------------------------------------------------------

Register-MoomooTask `
    -TaskName "MoomooTrader-Bot" `
    -Description "Start moomoo-trader Bot (Mon-Fri $BotTime JST, DST)" `
    -Time $BotTime `
    -DaysOfWeek @("Monday","Tuesday","Wednesday","Thursday","Friday") `
    -Execute $PythonExe `
    -Arguments $MainPy `
    -WorkingDirectory $ProjectRoot

# ---------------------------------------------------------------------------
# Task 3: Stop all (Tue-Sat)
# ---------------------------------------------------------------------------

Register-MoomooTask `
    -TaskName "MoomooTrader-Stop" `
    -Description "Stop moomoo-trader Bot and OpenD (Tue-Sat $StopTime JST, DST)" `
    -Time $StopTime `
    -DaysOfWeek @("Tuesday","Wednesday","Thursday","Friday","Saturday") `
    -Execute "powershell.exe" `
    -Arguments "-ExecutionPolicy Bypass -File `"$StopScript`"" `
    -WorkingDirectory $ProjectRoot

# ---------------------------------------------------------------------------
# Task 4: Screener (Tue-Sat, after market close)
# ---------------------------------------------------------------------------

$ScreenerTime = "06:30"  # JST: after Stop (05:10) and before next Open (22:10)
$ScreenerPy = Join-Path $ProjectRoot "scripts\screener.py"

Register-MoomooTask `
    -TaskName "MoomooTrader-Screener" `
    -Description "Run daily stock screener (Tue-Sat $ScreenerTime JST)" `
    -Time $ScreenerTime `
    -DaysOfWeek @("Tuesday","Wednesday","Thursday","Friday","Saturday") `
    -Execute $PythonExe `
    -Arguments $ScreenerPy `
    -WorkingDirectory $ProjectRoot

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Registration complete" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  $OpenDTime  MoomooTrader-OpenD     Start OpenD.exe        (Mon-Fri)"
Write-Host "  $BotTime  MoomooTrader-Bot       Start python main.py  (Mon-Fri)"
Write-Host "  $StopTime  MoomooTrader-Stop      Stop Bot + OpenD      (Tue-Sat)"
Write-Host "  $ScreenerTime  MoomooTrader-Screener  Daily stock screener  (Tue-Sat)"
Write-Host ""
Write-Host "  Current: DST (summer time) schedule" -ForegroundColor Cyan
Write-Host "  When EST resumes (Nov): change to 23:10 / 23:15 / 06:10"
Write-Host ""
Write-Host "Verify:" -ForegroundColor Yellow
Write-Host "  Get-ScheduledTask -TaskName 'MoomooTrader-*' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Manual run:" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName 'MoomooTrader-OpenD'"
Write-Host "  Start-ScheduledTask -TaskName 'MoomooTrader-Bot'"
Write-Host "  Start-ScheduledTask -TaskName 'MoomooTrader-Stop'"
Write-Host ""
Write-Host "US holiday - disable before holiday:" -ForegroundColor Yellow
Write-Host "  Disable-ScheduledTask -TaskName 'MoomooTrader-OpenD'"
Write-Host "  Disable-ScheduledTask -TaskName 'MoomooTrader-Bot'"
Write-Host "  Disable-ScheduledTask -TaskName 'MoomooTrader-Stop'"
Write-Host ""
Write-Host "US holiday - re-enable before next trading day:" -ForegroundColor Yellow
Write-Host "  Enable-ScheduledTask -TaskName 'MoomooTrader-OpenD'"
Write-Host "  Enable-ScheduledTask -TaskName 'MoomooTrader-Bot'"
Write-Host "  Enable-ScheduledTask -TaskName 'MoomooTrader-Stop'"
Write-Host ""
Write-Host "Remove all tasks:" -ForegroundColor Yellow
Write-Host "  Get-ScheduledTask -TaskName 'MoomooTrader-*' | Unregister-ScheduledTask -Confirm:`$false"
Write-Host ""
