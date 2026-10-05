#Requires -Version 5.1
[CmdletBinding()]
param([string]$Destination = (Join-Path (Split-Path $PSScriptRoot -Parent) "tools"))

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$version = "8.1.2"
$archiveName = "ffmpeg-$version-essentials_build.zip"
$downloadUrl = "https://github.com/GyanD/codexffmpeg/releases/download/$version/$archiveName"
# Publisher's GitHub release asset digest, verified on 2026-10-05:
# https://github.com/GyanD/codexffmpeg/releases/expanded_assets/8.1.2
$expectedSha256 = "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec"
$toolCache = [IO.Path]::GetFullPath($Destination)
New-Item -ItemType Directory -Path $toolCache -Force | Out-Null
$archive = Join-Path $toolCache $archiveName
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $downloadUrl -OutFile $archive -UseBasicParsing
}
$actualSha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $expectedSha256) {
    throw "FFmpeg SHA256 mismatch. The archive has not been extracted: $archive"
}
$extractedRoot = Join-Path $toolCache "ffmpeg-$version-essentials_build"
if (-not (Test-Path -LiteralPath $extractedRoot -PathType Container)) {
    Expand-Archive -LiteralPath $archive -DestinationPath $toolCache
}
$bin = Join-Path $extractedRoot "bin"
foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $bin $tool) -PathType Leaf)) {
        throw "Verified archive did not contain $tool."
    }
}
$sourceNotice = @"
FFmpeg executable build: Gyan essentials $version (Windows x64).
Binary archive: $downloadUrl
Archive SHA256: $expectedSha256
Publisher and build details: https://www.gyan.dev/ffmpeg/builds/
Release details: https://github.com/GyanD/codexffmpeg/releases/tag/$version
FFmpeg upstream source commit: https://github.com/FFmpeg/FFmpeg/commit/38b88335f9
FFmpeg upstream source archive: https://github.com/FFmpeg/FFmpeg/archive/38b88335f9.tar.gz
FFmpeg license/build information is also available using ffmpeg.exe -L and -buildconf.
The original LICENSE and readme from the binary archive must accompany redistribution.
The upstream source link alone is not a complete corresponding-source package for the
external libraries enabled by this static build. Before publishing a binary release,
the distributor must provide corresponding source and build instructions for all
applicable components as required by the shipped licenses.
"@
$sourceNotice | Set-Content -LiteralPath (Join-Path $extractedRoot "BILI-MP4-FFMPEG-SOURCE.txt") -Encoding UTF8
Write-Output $bin
