# 在 INAGENT/docs 起静态页；先释放 8008，避免僵死 python 占端口
$port = 8008
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($line in (netstat -ano)) {
  if ($line -like "*127.0.0.1:$port *" -and $line -match "LISTENING\s+(\d+)\s*$") {
    Stop-Process -Id $Matches[1] -Force -ErrorAction SilentlyContinue
  }
}
Start-Sleep -Milliseconds 500
Set-Location $here
Write-Host "Serving $here on http://127.0.0.1:$port/ (Ctrl+C to stop)"
python -m http.server $port --bind 127.0.0.1
