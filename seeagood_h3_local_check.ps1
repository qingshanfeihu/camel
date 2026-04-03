# QUIC + ALPN h3 本机探测（需 Git for Windows 的 openssl，带 -quic）
$ErrorActionPreference = 'Continue'
$openssl = 'C:\Program Files\Git\usr\bin\openssl.exe'
if (-not (Test-Path -LiteralPath $openssl)) {
    Write-Host "Missing: $openssl" -ForegroundColor Red
    exit 2
}

Write-Host "`n=== JP IPv4 45.137.180.97 SNI a_jp ===" -ForegroundColor Cyan
'' | & $openssl s_client -connect '45.137.180.97:443' -quic -alpn h3 -servername 'a_jp.qingshanfeihu.eu.org' 2>&1 | Select-Object -First 22

Write-Host "`n=== US IPv4 154.26.183.67 SNI us ===" -ForegroundColor Cyan
'' | & $openssl s_client -connect '154.26.183.67:443' -quic -alpn h3 -servername 'us.qingshanfeihu.uk' 2>&1 | Select-Object -First 22
