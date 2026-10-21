param([switch]$PackageOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$bundle = Join-Path $PSScriptRoot 'dist\SpaceGoatFisch'
if (-not $PackageOnly) {
if (-not (Test-Path -LiteralPath '.spacegoat-build\Scripts\python.exe')) {
    py -3.10 -m venv .spacegoat-build
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 x64 is required on the build machine only.' }
}
$buildPython = Join-Path $PSScriptRoot '.spacegoat-build\Scripts\python.exe'
& $buildPython -m pip install -r spacegoat-requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $buildPython spacegoat_fisch_server.py --self-test --data-dir spacegoat-validation
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& $buildPython spacegoat_fisch_server.py --build --data-dir spacegoat-validation
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
$bundle = Join-Path $PSScriptRoot 'dist\SpaceGoatFisch'
Copy-Item -LiteralPath 'SpaceGoat-Install.ps1','Install-SpaceGoat.cmd','SpaceGoat-Setup.md' -Destination $bundle -Force
$validation = Join-Path $PSScriptRoot 'spacegoat-validation'
$exe = Join-Path $bundle 'SpaceGoatFisch.exe'
$process = Start-Process -FilePath $exe -ArgumentList @('--self-test','--data-dir',('"' + $validation + '"')) -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -ne 0) { throw 'Packaged self-test failed.' }
$process = Start-Process -FilePath $exe -ArgumentList @('--smoke-test','--data-dir',('"' + $validation + '"')) -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -ne 0) { throw 'Packaged smoke-test failed.' }
$originalTaskPath = $env:PATH
$originalPythonHome = $env:PYTHONHOME
$originalPythonPath = $env:PYTHONPATH
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    Remove-Item Env:PYTHONHOME,Env:PYTHONPATH -ErrorAction SilentlyContinue
    $isolatedData = Join-Path $validation 'isolated-path'
    $process = Start-Process -FilePath $exe -ArgumentList @('--smoke-test','--data-dir',('"' + $isolatedData + '"')) -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw 'Packaged smoke-test with Python removed from PATH failed.' }
    'PASS: Executable launched with only Windows system directories in PATH and no PYTHONHOME/PYTHONPATH. This is not a clean-machine test.' | Set-Content -LiteralPath (Join-Path $bundle 'isolated-path-results.txt') -Encoding UTF8
} finally {
    $env:PATH = $originalTaskPath
    $env:PYTHONHOME = $originalPythonHome
    $env:PYTHONPATH = $originalPythonPath
}
Copy-Item -LiteralPath 'spacegoat-validation\test-results.txt','spacegoat-validation\smoke-results.txt' -Destination $bundle -Force
}
$sitePackages = Join-Path $PSScriptRoot '.spacegoat-build\Lib\site-packages'
$licenseRoot = Join-Path $bundle 'licenses'
foreach ($metadata in (Get-ChildItem -LiteralPath $sitePackages -Directory -Filter '*.dist-info')) {
    foreach ($license in (Get-ChildItem -LiteralPath $metadata.FullName -Recurse -File | Where-Object { $_.Name -match '^(LICENSE|COPYING|NOTICE)' -or $_.FullName -match '\\licenses\\' })) {
        $relative = $license.FullName.Substring($sitePackages.Length + 1)
        $target = Join-Path $licenseRoot $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $license.FullName -Destination $target -Force
    }
}
$manifest = [ordered]@{
    application = 'SpaceGoat Fisch Tournament Server'
    target = 'Windows x64'
    source_sha256 = (Get-FileHash -LiteralPath 'spacegoat_fisch_server.py' -Algorithm SHA256).Hash
    executable_sha256 = (Get-FileHash -LiteralPath (Join-Path $bundle 'SpaceGoatFisch.exe') -Algorithm SHA256).Hash
    dependencies_sha256 = (Get-FileHash -LiteralPath 'spacegoat-requirements.lock' -Algorithm SHA256).Hash
    validation = '36 scoring/parser/lifecycle/cancellation/corrections/kick/rules/overlay tests, packaged OCR/HTTP/SSE/Qt smoke test, and launch with Python removed from PATH passed on build machine'
    built_utc = [DateTime]::UtcNow.ToString('o')
    outstanding = @('Clean Windows without Python', 'Real two-player Fisch observer visibility', 'Actual OBS and mixed-DPI target hardware')
}
$manifest | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $bundle 'build-manifest.json') -Encoding UTF8
Compress-Archive -LiteralPath $bundle -DestinationPath 'dist\SpaceGoatFisch-Windows-x64.zip' -Force
Get-FileHash -LiteralPath 'dist\SpaceGoatFisch-Windows-x64.zip' -Algorithm SHA256 | Format-List
exit 0
