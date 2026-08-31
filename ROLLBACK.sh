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
  'backend/order_query/captcha.py' = '73233DF1A3C1BC44CE491C2D2A1DEE2B6BC2BD5CBC5690B6434863E0A228AAD5'
  'backend/test_order_query_modules.py' = '104B41662AAF9FA11842BDDFA7AFEEFB575EE7A465F1C16DB5823D2A4DC470C0'
}
foreach ($path in $expected.Keys) {
  $actualPath = Join-Path $target $path
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $actualPath).Hash
  if ($actual -ne $expected[$path]) { throw "Rollback verification failed for ${path}: $actual" }
}
Write-Output "Restored $target; verified $($expected.Count) files"
