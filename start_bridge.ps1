# Starts (or restarts) the bridge in the background with no window.
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\start_bridge.ps1 [bridge flags...]
# It finds the radio by itself; pass e.g. --model qwen2.5:7b or --port COM5 to override.
# Logs (overwritten on each start): logs\bridge.log and logs\bridge.err.log
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$BridgeArgs)
$dir = $PSScriptRoot
& "$dir\stop_bridge.ps1" -Quiet
Start-Sleep -Milliseconds 500
New-Item -ItemType Directory -Force -Path "$dir\logs" | Out-Null
$argList = @('-u', '-m', 'meshllm') + @($BridgeArgs | Where-Object { $_ })
$py = if (Test-Path "$dir\.venv\Scripts\python.exe") { "$dir\.venv\Scripts\python.exe" } else { 'python' }   # the environment made by setup.ps1 if there is one
$p = Start-Process -FilePath $py -ArgumentList $argList -WorkingDirectory $dir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput "$dir\logs\bridge.log" -RedirectStandardError "$dir\logs\bridge.err.log"
"Bridge started in the background (PID $($p.Id)). Web UI: http://127.0.0.1:8080/   Log: $dir\logs\bridge.log"
