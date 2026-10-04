# Put the disposable e2e library back to its pristine, indexed state and start its server on 8768.
$ErrorActionPreference = "Stop"
# Stop the e2e server AND any index job it spawned (a job's command line names the data folder, not the port).
$pids = @()
$listen = (Get-NetTCPConnection -LocalPort 8768 -State Listen -ErrorAction SilentlyContinue).OwningProcess | Select-Object -First 1
if ($listen) { $pids += $listen }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
  $_.CommandLine -match 'photointel' -and ($_.CommandLine -match '8768' -or $_.CommandLine -match 'pi_cache.e2e')
} | ForEach-Object { $pids += $_.ProcessId; $pids += $_.ParentProcessId }
foreach ($id in ($pids | Sort-Object -Unique)) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
  if ($p -and $p.CommandLine -match 'photointel|python') { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
}
Start-Sleep 4
foreach ($d in "lib", "data") {
  if (Test-Path "D:\pi_cache\e2e\$d") { Remove-Item "D:\pi_cache\e2e\$d" -Recurse -Force }
  Copy-Item "D:\pi_cache\e2e\pristine\$d" "D:\pi_cache\e2e\$d" -Recurse
}
$env:PHOTOINTEL_DATA = "D:\pi_cache\e2e\data"
$env:PHOTOINTEL_MODELS = "D:\claude_photos_intelligence\data\models"
$env:PHOTOINTEL_GEO = "D:\claude_photos_intelligence\data\geo"
$env:PHOTOINTEL_DEVICE = "cpu"
Start-Process -FilePath "D:\claude_photos_intelligence\.venv\Scripts\python.exe" `
  -ArgumentList "-m", "photointel", "serve", "--port", "8768" `
  -WorkingDirectory "D:\claude_photos_intelligence" -WindowStyle Hidden `
  -RedirectStandardOutput "D:\pi_cache\e2e\serve.log" -RedirectStandardError "D:\pi_cache\e2e\serve.err"
for ($i = 0; $i -lt 40; $i++) {
  try { $r = Invoke-WebRequest "http://127.0.0.1:8768/api/stats" -UseBasicParsing -TimeoutSec 3; if ($r.StatusCode -eq 200) { break } } catch {}
  Start-Sleep 2
}
"e2e library reset; server up: $($r.StatusCode); photos: $((ConvertFrom-Json $r.Content).photos)"
