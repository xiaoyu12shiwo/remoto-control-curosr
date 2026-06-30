# 一键启动 remote_control（自动清理 5000-5003 旧实例）
$ErrorActionPreference = "SilentlyContinue"
$Port = if ($env:CURSOR_REMOTE_PORT) { [int]$env:CURSOR_REMOTE_PORT } else { 5002 }

foreach ($p in 5000, 5001, 5002, 5003) {
    Get-NetTCPConnection -LocalPort $p -State Listen |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object { Stop-Process -Id $_ -Force }
}
Start-Sleep -Seconds 1

$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Script = Join-Path $PSScriptRoot "remote_control_server.py"

$LocalEnv = Join-Path $PSScriptRoot "local_env.ps1"
if (Test-Path $LocalEnv) {
    . $LocalEnv
    Write-Host "已加载 local_env.ps1 (SDK/API Key)"
}

$env:CURSOR_REMOTE_PORT = "$Port"
Set-Location $Root
Write-Host "启动 remote_control: http://127.0.0.1:$Port"
& $Python $Script
