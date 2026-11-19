# Stops everything started by run_all.ps1 (3 MCP servers, API, UI) and closes their windows.
# Usage: .\stop_all.ps1
$names = "claims-mcp", "policy-mcp", "fraud-mcp", "api", "ui"
$ports = 8000, 8001, 8002, 8003, 8501

# 1. Close the service windows opened by run_all.ps1, killing each window's whole process tree
#    (uv -> python / uvicorn reloader / streamlit).
$windows = Get-Process powershell, pwsh -ErrorAction SilentlyContinue |
    Where-Object { $names -contains $_.MainWindowTitle }
foreach ($w in $windows) {
    Write-Host "Stopping $($w.MainWindowTitle) (PID $($w.Id))"
    taskkill /T /F /PID $w.Id | Out-Null
}

# 2. Fallback: stop anything still listening on the app ports (e.g. a window that was renamed).
$pids = Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $pids) {
    $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "Stopping $($proc.ProcessName) on app port (PID $p)"
        taskkill /T /F /PID $p | Out-Null
    }
}

if (-not $windows -and -not $pids) { Write-Host "Nothing running." } else { Write-Host "Stopped." }
