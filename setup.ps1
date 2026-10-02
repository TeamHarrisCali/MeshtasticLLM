# One-step setup for Windows. Finds a Python (3.9 or newer), then runs setup_env.py, which builds the private environment,
# installs the dependencies and checks Ollama and the radio. Safe to run again any time.
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 [options]     (or just double-click setup.bat)
#   options are passed on:  --check   --recreate   --pull-model   --install-ollama   --start   --yes
# If no suitable Python is installed it offers to install one with winget.
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$SetupArgs)
$dir = $PSScriptRoot
Set-Location $dir

function Find-Python {
    # The Microsoft Store's "python" shortcut opens the Store instead of running Python, so every candidate is really run.
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3') }
    foreach ($name in 'python3', 'python') { if (Get-Command $name -ErrorAction SilentlyContinue) { $candidates += , @($name) } }
    foreach ($c in $candidates) {
        $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
        try {
            $ok = & $exe @pre -c "import sys; print(int(sys.version_info >= (3, 9)))" 2>$null
            if ("$ok".Trim() -eq '1') { return , @($exe) + $pre }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "No Python 3.9 or newer was found."
    $cmd = 'winget install -e --id Python.Python.3.12'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "It can be installed with:  $cmd"
        if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
            $answer = Read-Host "Run that now? [y/N]"
            if ($answer -match '^(y|yes)$') {
                Invoke-Expression $cmd
                $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
                $py = Find-Python
            }
        }
    } else {
        Write-Host "Install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
    }
    if (-not $py) { Write-Host "Install Python, then run setup.bat again."; exit 1 }
}
$exe = $py[0]; $pre = @($py | Select-Object -Skip 1)
& $exe @pre "$dir\setup_env.py" @SetupArgs
exit $LASTEXITCODE
