#Requires -RunAsAdministrator
<#
.SYNOPSIS
    moomoo-trader のタスクスケジューラを一発設定するスクリプト。

.DESCRIPTION
    以下の3タスクを登録する:
      1. MoomooTrader-OpenD     毎日 23:20 に OpenD.exe を起動
      2. MoomooTrader-Bot       毎日 23:25 に python src/main.py を起動
      3. MoomooTrader-Stop      毎日 06:10 に Bot と OpenD を停止

    管理者権限で実行すること:
      PowerShell -ExecutionPolicy Bypass -File scripts\setup_scheduler.ps1

.NOTES
    既に同名タスクが存在する場合は上書き（再登録）する。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# パス設定
# ---------------------------------------------------------------------------

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$OpenDDir    = Join-Path $ProjectRoot "moomoo_OpenD_10.2.6208_Windows\moomoo_OpenD_10.2.6208_Windows"
$OpenDExe    = Join-Path $OpenDDir "OpenD.exe"
$PythonExe   = (Get-Command python -ErrorAction SilentlyContinue).Source
$MainPy      = Join-Path $ProjectRoot "src\main.py"
$StopScript  = Join-Path $ProjectRoot "scripts\stop_all.ps1"

# バリデーション
if (-not (Test-Path $OpenDExe)) {
    Write-Error "OpenD.exe が見つかりません: $OpenDExe"
    exit 1
}
if (-not $PythonExe) {
    Write-Error "python が PATH にありません"
    exit 1
}
if (-not (Test-Path $MainPy)) {
    Write-Error "src/main.py が見つかりません: $MainPy"
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " moomoo-trader タスクスケジューラ設定" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Project:  $ProjectRoot"
Write-Host "  OpenD:    $OpenDExe"
Write-Host "  Python:   $PythonExe"
Write-Host "  main.py:  $MainPy"
Write-Host ""

# ---------------------------------------------------------------------------
# 停止スクリプトを生成
# ---------------------------------------------------------------------------

$StopScriptContent = @'
# moomoo-trader 停止スクリプト（タスクスケジューラから呼び出される）
$ErrorActionPreference = "SilentlyContinue"

# Bot (python) を停止
$bots = Get-Process python | Where-Object {
    $_.CommandLine -like "*src\main.py*" -or
    $_.CommandLine -like "*src/main.py*"
}
if ($bots) {
    $bots | ForEach-Object {
        Write-Host "[STOP] Bot 停止: PID $($_.Id)"
        Stop-Process -Id $_.Id -Force
    }
} else {
    # main.py を特定できない場合は名前で探す
    Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "[STOP] Python 停止: PID $($_.Id)"
        Stop-Process -Id $_.Id -Force
    }
}

# OpenD を停止
Get-Process OpenD -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "[STOP] OpenD 停止: PID $($_.Id)"
    Stop-Process -Id $_.Id -Force
}

Write-Host "[STOP] 全プロセス停止完了"
'@

Set-Content -Path $StopScript -Value $StopScriptContent -Encoding UTF8
Write-Host "[OK] 停止スクリプト生成: $StopScript" -ForegroundColor Green

# ---------------------------------------------------------------------------
# ヘルパー: タスク登録
# ---------------------------------------------------------------------------

function Register-MoomooTask {
    param(
        [string]$TaskName,
        [string]$Description,
        [string]$Time,
        [string]$Execute,
        [string]$Arguments = "",
        [string]$WorkingDirectory = ""
    )

    # 既存タスクを削除
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  既存タスク削除: $TaskName" -ForegroundColor Yellow
    }

    # トリガー: 毎日指定時刻
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time

    # アクション
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

    # 設定
    $taskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 8)

    # 登録（現在のユーザーで実行）
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $Description `
        -Trigger $trigger `
        -Action $action `
        -Settings $taskSettings `
        -Principal $principal | Out-Null

    Write-Host "  [OK] $TaskName ($Time)" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# タスク1: OpenD 起動 (23:20)
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "--- タスク登録 ---" -ForegroundColor Cyan

Register-MoomooTask `
    -TaskName "MoomooTrader-OpenD" `
    -Description "moomoo OpenD を起動する (23:20)" `
    -Time "23:20" `
    -Execute $OpenDExe `
    -WorkingDirectory $OpenDDir

# ---------------------------------------------------------------------------
# タスク2: Bot 起動 (23:25)
# ---------------------------------------------------------------------------

Register-MoomooTask `
    -TaskName "MoomooTrader-Bot" `
    -Description "moomoo-trader Bot を起動する (23:25)" `
    -Time "23:25" `
    -Execute $PythonExe `
    -Arguments $MainPy `
    -WorkingDirectory $ProjectRoot

# ---------------------------------------------------------------------------
# タスク3: 全停止 (06:10)
# ---------------------------------------------------------------------------

Register-MoomooTask `
    -TaskName "MoomooTrader-Stop" `
    -Description "moomoo-trader Bot と OpenD を停止する (06:10)" `
    -Time "06:10" `
    -Execute "powershell.exe" `
    -Arguments "-ExecutionPolicy Bypass -File `"$StopScript`"" `
    -WorkingDirectory $ProjectRoot

# ---------------------------------------------------------------------------
# 結果表示
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " 登録完了" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  23:20  MoomooTrader-OpenD   OpenD.exe 起動"
Write-Host "  23:25  MoomooTrader-Bot     python src/main.py 起動"
Write-Host "  06:10  MoomooTrader-Stop    Bot + OpenD 停止"
Write-Host ""
Write-Host "確認コマンド:" -ForegroundColor Yellow
Write-Host "  Get-ScheduledTask -TaskName 'MoomooTrader-*' | Format-Table TaskName, State"
Write-Host ""
Write-Host "手動実行:" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName 'MoomooTrader-OpenD'"
Write-Host "  Start-ScheduledTask -TaskName 'MoomooTrader-Bot'"
Write-Host "  Start-ScheduledTask -TaskName 'MoomooTrader-Stop'"
Write-Host ""
Write-Host "削除:" -ForegroundColor Yellow
Write-Host "  Get-ScheduledTask -TaskName 'MoomooTrader-*' | Unregister-ScheduledTask -Confirm:`$false"
Write-Host ""
