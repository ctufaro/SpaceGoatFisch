param([switch]$SkipUpdate, [switch]$SelfTest, [switch]$SmokeTest, [switch]$SetupOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    if (-not $SkipUpdate) {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git for Windows first.' }
        $changes = & git status --porcelain --untracked-files=no
        if ($LASTEXITCODE -ne 0) { throw 'Clone this repository with Git before running updates.' }
        if ($changes) { throw 'Commit or stash local source edits before updating, or use -SkipUpdate.' }
        & git pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw 'Update failed. Check your network/GitHub login, or use -SkipUpdate.' }
        $nextArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-SkipUpdate')
        if ($SelfTest) { $nextArgs += '-SelfTest' }
        if ($SmokeTest) { $nextArgs += '-SmokeTest' }
        if ($SetupOnly) { $nextArgs += '-SetupOnly' }
        & powershell.exe @nextArgs
        exit $LASTEXITCODE
    }
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $candidates = @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python310\python.exe'), 'C:\Program Files\Python310\python.exe', (Join-Path $PSScriptRoot '.spacegoat-build\Scripts\python.exe'))
        $basePython = $null
        foreach ($candidate in $candidates) {
            if (Test-Path -LiteralPath $candidate) { $basePython = $candidate; break }
        }
        if ($basePython) { & $basePython -m venv .venv }
        elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3.10 -m venv .venv }
        else { throw 'Install Python 3.10 x64 from python.org including its launcher, then retry.' }
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 x64 is required. Install it from python.org, then retry.' }
    }
    & $python -c "import sys,struct; assert sys.version_info[:2] == (3,10) and struct.calcsize('P') == 8, 'Python 3.10 x64 required'"
    if ($LASTEXITCODE -ne 0) { throw 'The .venv environment must use Python 3.10 x64.' }
    $lockHash = (Get-FileHash -LiteralPath 'spacegoat-requirements.lock' -Algorithm SHA256).Hash
    $stamp = Join-Path $PSScriptRoot '.venv\requirements.sha256'
    if (-not (Test-Path -LiteralPath $stamp) -or (Get-Content -LiteralPath $stamp -Raw).Trim() -ne $lockHash) {
        & $python -m pip install -r spacegoat-requirements.lock
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your Internet connection and retry.' }
        & $python -m pip check
        if ($LASTEXITCODE -ne 0) { throw 'Dependency validation failed.' }
        Set-Content -LiteralPath $stamp -Value $lockHash
    }
    if ($SetupOnly) { Write-Host 'Environment ready.'; exit 0 }
    $appArgs = @('spacegoat_fisch_server.py')
    if ($SelfTest -or $SmokeTest) {
        $appArgs += @('--data-dir', (Join-Path $PSScriptRoot '.verification\launcher'))
        if ($SelfTest) { $appArgs += '--self-test' } else { $appArgs += '--smoke-test' }
    }
    & $python @appArgs
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
