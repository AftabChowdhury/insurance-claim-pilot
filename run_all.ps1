# Starts the 3 MCP servers, the API and the UI, each in its own PowerShell window.
# Usage: .\run_all.ps1
$root = $PSScriptRoot
$services = @(
    @{ Name = "claims-mcp"; Cmd = "uv run python -m mcp_servers.claims_server" },
    @{ Name = "policy-mcp"; Cmd = "uv run python -m mcp_servers.policy_server" },
    @{ Name = "fraud-mcp";  Cmd = "uv run python -m mcp_servers.fraud_server" },
    @{ Name = "api";        Cmd = "uv run uvicorn api.main:app --port 8000 --reload" },
    @{ Name = "ui";         Cmd = "uv run streamlit run ui/app.py" }
)
foreach ($s in $services) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='$($s.Name)'; Set-Location '$root'; $($s.Cmd)"
    Start-Sleep -Milliseconds 500
}
Write-Host "Started. API docs: http://localhost:8000/docs   UI: http://localhost:8501"
