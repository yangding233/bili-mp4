#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Python = (Join-Path (Split-Path $PSScriptRoot -Parent) ".venv\Scripts\python.exe"),
    [Parameter(Mandatory = $true)]
    [string]$FfmpegDirectory,
    [string]$FfmpegLicenseDirectory,
    [string]$FfmpegSourceUrl = "https://github.com/FFmpeg/FFmpeg/commit/38b88335f9",
    [string]$Version = "0.1.0"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Split-Path $PSScriptRoot -Parent)).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$ffmpegBin = (Resolve-Path -LiteralPath $FfmpegDirectory).Path
if (-not $FfmpegLicenseDirectory) {
    $FfmpegLicenseDirectory = Split-Path $ffmpegBin -Parent
}
$ffmpegLicenseRoot = (Resolve-Path -LiteralPath $FfmpegLicenseDirectory).Path
$licenseFiles = @(Get-ChildItem -LiteralPath $ffmpegLicenseRoot -File | Where-Object { $_.Name -match "(?i)^(LICENSE|COPYING)" })
if ($licenseFiles.Count -eq 0) {
    throw "FFmpeg LICENSE/COPYING file missing. Pass the original archive root via -FfmpegLicenseDirectory."
}
$ffmpeg = Join-Path $ffmpegBin "ffmpeg.exe"
$ffprobe = Join-Path $ffmpegBin "ffprobe.exe"
foreach ($tool in @($ffmpeg, $ffprobe)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "Required tool missing: $tool" }
}
if ($Version -notmatch "^\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?$") {
    throw "Invalid version."
}
$buildId = [Guid]::NewGuid().ToString("N")
$workingRoot = Join-Path $projectRoot "build\windows\$buildId"
$distRoot = Join-Path $workingRoot "dist"
$bundleRoot = Join-Path $distRoot "BiliMP4"
$publicDist = Join-Path $projectRoot "dist"
$zipPath = Join-Path $publicDist "BiliMP4-$Version-windows-x64.zip"
New-Item -ItemType Directory -Path $workingRoot -Force | Out-Null
New-Item -ItemType Directory -Path $publicDist -Force | Out-Null
if (Test-Path -LiteralPath $zipPath) {
    throw "Output already exists: $zipPath. Choose a different Version or move the old archive explicitly."
}

Push-Location $projectRoot
try {
    & $pythonPath -c "import struct, sys; assert sys.platform == 'win32' and struct.calcsize('P') == 8, 'Windows x64 Python required'"
    if ($LASTEXITCODE -ne 0) { throw "Build requires Windows x64 Python." }
    $pyinstallerArgs = @(
        "-m", "PyInstaller",
        "--noconfirm", "--onedir", "--windowed", "--noupx",
        "--name", "BiliMP4",
        "--paths", (Join-Path $projectRoot "src"),
        "--collect-all", "yt_dlp",
        "--add-binary", "$($ffmpeg):tools",
        "--add-binary", "$($ffprobe):tools",
        "--distpath", $distRoot,
        "--workpath", (Join-Path $workingRoot "pyinstaller"),
        "--specpath", $workingRoot,
        (Join-Path $PSScriptRoot "entrypoint.py")
    )
    & $pythonPath @pyinstallerArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
        $bundledTool = Join-Path $bundleRoot "_internal\tools\$tool"
        if (-not (Test-Path -LiteralPath $bundledTool -PathType Leaf)) {
            throw "Bundled tool missing: $tool"
        }
        & $bundledTool -version | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Bundled tool cannot run: $tool" }
    }
    $qtPlugin = @(Get-ChildItem -LiteralPath $bundleRoot -Recurse -File -Filter "qwindows.dll")
    if ($qtPlugin.Count -eq 0) { throw "Qt Windows platform plugin missing." }

    foreach ($document in @("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md")) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $document) -Destination $bundleRoot
    }
    $thirdPartyRoot = Join-Path $bundleRoot "licenses"
    & $pythonPath (Join-Path $PSScriptRoot "collect_licenses.py") $thirdPartyRoot
    if ($LASTEXITCODE -ne 0) { throw "Dependency license collection failed." }
    $ffmpegNotices = Join-Path $thirdPartyRoot "FFmpeg"
    New-Item -ItemType Directory -Path $ffmpegNotices -Force | Out-Null
    foreach ($notice in (Get-ChildItem -LiteralPath $ffmpegLicenseRoot -File | Where-Object { $_.Name -match "(?i)(LICENSE|COPYING|README|SOURCE)" })) {
        Copy-Item -LiteralPath $notice.FullName -Destination $ffmpegNotices
    }
    & $ffmpeg -version 2>&1 | Set-Content -LiteralPath (Join-Path $ffmpegNotices "version-and-configuration.txt") -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw "Cannot collect FFmpeg build configuration." }
    @"
FFmpeg supplied directory: $ffmpegBin
Source retrieval: $FfmpegSourceUrl
The FFmpeg executable is a separate program; its license is independent of this application's MIT license.
Read THIRD_PARTY_NOTICES.md and the original license files before redistributing binaries.
"@ | Set-Content -LiteralPath (Join-Path $ffmpegNotices "distribution.txt") -Encoding UTF8
    & $pythonPath -m pip freeze | Set-Content -LiteralPath (Join-Path $bundleRoot "build-dependencies.txt") -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw "Cannot record dependency versions." }

    $previousQtPlatform = $env:QT_QPA_PLATFORM
    $previousToolDirectory = $env:BILI_MP4_FFMPEG_DIR
    try {
        $env:QT_QPA_PLATFORM = "offscreen"
        $env:BILI_MP4_FFMPEG_DIR = ""
        $smokeData = Join-Path $workingRoot "gui-smoke-data"
        $smokeArguments = @("--smoke-test", "--data-dir", ('"' + $smokeData + '"'))
        $smokeProcess = Start-Process -FilePath (Join-Path $bundleRoot "BiliMP4.exe") -ArgumentList $smokeArguments -WindowStyle Hidden -PassThru
        if (-not $smokeProcess.WaitForExit(30000)) {
            $smokeProcess.Kill()
            throw "Packaged GUI smoke test timed out."
        }
        if ($smokeProcess.ExitCode -ne 0) { throw "Packaged GUI smoke test failed: $($smokeProcess.ExitCode)" }
    } finally {
        $env:QT_QPA_PLATFORM = $previousQtPlatform
        $env:BILI_MP4_FFMPEG_DIR = $previousToolDirectory
    }
    Compress-Archive -LiteralPath $bundleRoot -DestinationPath $zipPath -CompressionLevel Optimal
    $digest = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$digest  $([IO.Path]::GetFileName($zipPath))" | Set-Content -LiteralPath "$zipPath.sha256" -Encoding ASCII
    Write-Output "Built: $zipPath"
} finally {
    Pop-Location
}
