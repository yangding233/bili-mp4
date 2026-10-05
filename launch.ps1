#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Setup,
    [string]$FfmpegDirectory,
    [switch]$SmokeTest
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if ($Setup) {
    Push-Location $projectRoot
    try {
        & py -3.11 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Python 3.11 x64 is required." }
        & $python -m pip install -r requirements-dev.txt
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    } finally {
        Pop-Location
    }
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Virtual environment missing. Run .\launch.ps1 -Setup after installing Python 3.11 x64."
}
if ($FfmpegDirectory) {
    $ffmpegBin = (Resolve-Path -LiteralPath $FfmpegDirectory).Path
    foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
        if (-not (Test-Path -LiteralPath (Join-Path $ffmpegBin $tool) -PathType Leaf)) {
            throw "$tool was not found in FfmpegDirectory."
        }
    }
    $env:BILI_MP4_FFMPEG_DIR = $ffmpegBin
    $env:PATH = "$ffmpegBin;$env:PATH"
}
Push-Location $projectRoot
try {
    if ($SmokeTest) {
        $env:QT_QPA_PLATFORM = "offscreen"
        & $python -m bili_mp4 --smoke-test
    } else {
        & $python -m bili_mp4
    }
    if ($LASTEXITCODE -ne 0) { throw "BiliMP4 exited with code $LASTEXITCODE." }
} finally {
    Pop-Location
}
