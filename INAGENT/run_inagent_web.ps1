param(
    [string]$Host = "127.0.0.1",
    [int]$Port = 8010,
    [switch]$Reload
)

Push-Location (Split-Path $PSScriptRoot -Parent)

[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
if ($PSVersionTable.PSVersion.Major -le 5) {
    chcp 65001 | Out-Null
}

$argsList = @("INAGENT/run_inagent_web.py", "--host", $Host, "--port", $Port)
if ($Reload) {
    $argsList += "--reload"
}

Write-Host "Starting INAGENT Web..." -ForegroundColor Cyan
Write-Host "URL: http://$Host`:$Port" -ForegroundColor Green
python @argsList

Pop-Location
