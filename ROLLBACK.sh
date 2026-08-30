#!/usr/bin/env powershell
param(
  [Parameter(Position = 0)]
  [string]$TargetRoot = '.'
)

$ErrorActionPreference = 'Stop'
$artifactRoot = if ($MyInvocation.MyCommand.Path) {
  Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
  (Get-Location).Path
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
  'frontend/src/Sub2ApiView.jsx' = 'A410D7251BE00FDF381F12213676F0ABC09955D3535C247B27683858CA746AC7'
  'frontend/src/style.css' = 'DCE5204CD22EB848E666FA16EC2F897F6AE1C39748C8D1D10DCD828E8BA52F18'
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
