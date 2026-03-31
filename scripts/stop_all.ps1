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
