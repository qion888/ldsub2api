#!/usr/bin/env powershell
param([string]$TargetRoot = '.')
$ErrorActionPreference = 'Stop'
$artifactRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = [IO.Path]::GetFullPath($TargetRoot)
if (-not (Test-Path -LiteralPath $target -PathType Container)) { throw "Target root does not exist: $target" }
$archive = Join-Path $artifactRoot 'ROLLBACK_BASELINE'
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw "Missing rollback archive: $archive" }
tar -xf $archive -C $target
if ($LASTEXITCODE -ne 0) { throw "Rollback archive extraction failed with exit $LASTEXITCODE" }
$expected = @{
  'backend/order_query/errors.py' = '79DA405F53ABBC84EC62A3CB106D5CD1AF2D62646961784348A319EACC372B2D'
  'backend/order_query/routes.py' = '40EBA2ED68D59897F56DB531AF51FD51347C60C6C27832A51F123A0F6804FB92'
  'backend/order_query/service.py' = '6189A816B3F33DD04AB3A4961CDC58CD86E52936EB5FE98ACD0D6DAA445DF167'
  'backend/test_order_query_modules.py' = '46D11BCECA94F2FC7B705A50A5C7709D3AF9B2A7630769B55CE611DB0C04F1D6'
  'frontend/src/OrderQueryView.jsx' = '40D183A841AE2028C6D92409560716E7921409F68E9D9844E55B08E1EB96ED06'
}
foreach ($path in $expected.Keys) {
  $actualPath = Join-Path $target $path
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $actualPath).Hash
  if ($actual -ne $expected[$path]) { throw "Rollback verification failed for ${path}: $actual" }
}
Write-Output "Restored $target; verified $($expected.Count) files"
