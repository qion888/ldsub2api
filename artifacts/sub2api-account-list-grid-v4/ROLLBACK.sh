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
  'frontend/src/Sub2ApiView.jsx' = '754108B5210D459FE24299AF0BCEDD97F95418351A190056C6BFA88871308E12'
  'frontend/src/style.css' = '3EB5E2CE871B4D5AB7B7B8D40D9DDC5367017B69261C8D41F255AE3F8C88604A'
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
