#!/usr/bin/env pwsh
param(
  [Parameter(Position = 0)]
  [string]$TargetRoot = '.'
)

$ErrorActionPreference = 'Stop'
$artifactRoot = if ($PSScriptRoot) {
  $PSScriptRoot
} elseif (Test-Path -LiteralPath (Join-Path (Get-Location) 'ROLLBACK_BASELINE')) {
  (Get-Location).Path
} elseif ($MyInvocation.MyCommand.Path) {
  Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
  throw 'Run this script from its artifact directory.'
}

$baselineArchive = Join-Path $artifactRoot 'ROLLBACK_BASELINE'
if (-not (Test-Path -LiteralPath $baselineArchive -PathType Leaf)) {
  throw "Missing rollback archive: $baselineArchive"
}

$targetRootPath = [IO.Path]::GetFullPath($TargetRoot)
if (-not (Test-Path -LiteralPath $targetRootPath -PathType Container)) {
  throw "Target root does not exist: $targetRootPath"
}

$targetPath = [IO.Path]::GetFullPath((Join-Path $targetRootPath 'frontend/src/style.css'))
$targetPrefix = $targetRootPath.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $targetPath.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Rollback target escaped root: $targetPath"
}
if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
  Remove-Item -LiteralPath $targetPath -Force
}

tar -xf $baselineArchive -C $targetRootPath
if ($LASTEXITCODE -ne 0) {
  throw "Rollback archive extraction failed with exit $LASTEXITCODE"
}

$expectedHash = 'ADDC61ACDDD020D0433B1442A8F642FD871A4B8E6E94F6FD3EED0F5E65F01098'
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetPath).Hash
if ($actualHash -ne $expectedHash) {
  throw "Rollback verification failed for frontend/src/style.css: expected $expectedHash, got $actualHash"
}

Write-Output "Restored $targetRootPath from ROLLBACK_BASELINE; frontend/src/style.css=$actualHash"
