# Per-user installer. No Python, pip, OCR download, or administrator rights required.
$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath($PSScriptRoot)
$programRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs'))
$destination = [IO.Path]::GetFullPath((Join-Path $programRoot 'SpaceGoatFisch'))
if (-not $destination.StartsWith($programRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Installation path is outside the per-user Programs directory.'
}
if (-not (Test-Path -LiteralPath (Join-Path $source 'SpaceGoatFisch.exe'))) { throw 'Extract the complete distribution first.' }
if (Get-Process -Name SpaceGoatFisch -ErrorAction SilentlyContinue) { throw 'Close SpaceGoatFisch before installing an update.' }
if ($source -ne $destination) {
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    Get-ChildItem -LiteralPath $source | Copy-Item -Destination $destination -Recurse -Force
}
$shortcutName = 'SpaceGoat Fisch Tournament Server.lnk'
$shellObject = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $shortcut = $shellObject.CreateShortcut((Join-Path $folder $shortcutName))
    $shortcut.TargetPath = Join-Path $destination 'SpaceGoatFisch.exe'
    $shortcut.WorkingDirectory = $destination
    $shortcut.Description = 'LAN tournament capture and OBS leaderboard'
    $shortcut.Save()
}
Write-Host "Installed to $destination. Launch the SpaceGoat shortcut on your desktop."
