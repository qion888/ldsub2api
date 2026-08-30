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

tar -xf $baselineArchive -C $targetRootPath frontend/src/Sub2ApiView.jsx frontend/src/style.css
if ($LASTEXITCODE -ne 0) {
  throw "Rollback archive extraction failed with exit $LASTEXITCODE"
}

$expectedHashes = @{
  'frontend/src/Sub2ApiView.jsx' = '70113F6BA69F1B4BC3E341E6F5087093D21A0347EF23CFF8CC603369423F8FAF'
  'frontend/src/style.css' = '892E2B25E0C0AF51932B68B66BFF3F55412B837967D84D475AF2A99693E588EB'
}

$results = foreach ($relativePath in $expectedHashes.Keys) {
  $targetPath = Join-Path $targetRootPath $relativePath
  $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetPath).Hash
  $expectedHash = $expectedHashes[$relativePath]
  if ($actualHash -ne $expectedHash) {
    throw "Rollback verification failed for ${relativePath}: expected $expectedHash, got $actualHash"
  }
  "${relativePath}=$actualHash"
}

Write-Output "Restored $targetRootPath from ROLLBACK_BASELINE; $($results -join '; ')"
