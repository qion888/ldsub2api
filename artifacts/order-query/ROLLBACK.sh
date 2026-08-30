#!/usr/bin/env pwsh
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
foreach ($marker in @('backend/main.py', 'frontend/package.json')) {
  if (-not (Test-Path -LiteralPath (Join-Path $targetRootPath $marker) -PathType Leaf)) {
    throw "Target root is not an LDXP source tree: missing $marker"
  }
}

function Resolve-ContainedPath([string]$RelativePath) {
  $candidate = [IO.Path]::GetFullPath((Join-Path $targetRootPath $RelativePath))
  $prefix = $targetRootPath.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
  if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Rollback path escaped target root: $RelativePath"
  }
  return $candidate
}

tar -xf $baselineArchive -C $targetRootPath backend/main.py backend/requirements.txt frontend/src/main.jsx frontend/src/style.css start.ps1
if ($LASTEXITCODE -ne 0) {
  throw "Rollback archive extraction failed with exit $LASTEXITCODE"
}

$addedPaths = @(
  'backend/order_query',
  'backend/test_order_query_modules.py',
  'frontend/src/OrderQueryView.jsx',
  'frontend/src/orderQuery.css',
  'frontend/src/orderQueryModel.js',
  'frontend/src/orderQueryModel.test.js'
)
foreach ($relativePath in $addedPaths) {
  $targetPath = Resolve-ContainedPath $relativePath
  if (Test-Path -LiteralPath $targetPath) {
    Remove-Item -LiteralPath $targetPath -Recurse -Force
  }
}

$expectedHashes = @{
  'backend/main.py' = 'E6728803A0D3982D69EA0A20EF7D043091BB43E90E052F36C23E216C13719CD8'
  'backend/requirements.txt' = '02AC48FF17DE5F3A76F8EDE14BDCEE6D9010FD263CE40AE24A36486FC8C2C1AD'
  'frontend/src/main.jsx' = '60570C3A34F5E6E8473CF527E05A939EC14EF8C38E2734B52257A49504DEE4E8'
  'frontend/src/style.css' = 'CF73AF2B5188903BF0267CDCCE7E213812A075EF8163A92355A35A58753FD4FC'
  'start.ps1' = '1E915B5F29B51C98B42A5FEE2904B9F9DD0447CDA6518CCF0DAF892FB1D09A03'
}

$results = foreach ($relativePath in $expectedHashes.Keys | Sort-Object) {
  $targetPath = Resolve-ContainedPath $relativePath
  $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetPath).Hash
  $expectedHash = $expectedHashes[$relativePath]
  if ($actualHash -ne $expectedHash) {
    throw "Rollback verification failed for ${relativePath}: expected $expectedHash, got $actualHash"
  }
  "${relativePath}=$actualHash"
}

Write-Output "Restored $targetRootPath to main baseline; $($results -join '; '); order query files removed"
