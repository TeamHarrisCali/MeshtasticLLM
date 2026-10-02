# Stops the background bridge, if one is running.
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\stop_bridge.ps1
param([switch]$Quiet)
$procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -match ' -m meshllm( |$)' })
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force }
if (-not $Quiet) {
    if ($procs.Count) { "Stopped bridge (PID $($procs.ProcessId -join ', '))." } else { "Bridge was not running." }
}
