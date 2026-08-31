#!/usr/bin/env powershell
param([string]$TargetRoot = '.')
$ErrorActionPreference = 'Stop'
$artifactRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = [IO.Path]::GetFullPath($TargetRoot)
if (-not (Test-Path -LiteralPath $target -PathType Container)) { throw "Target root does not exist: $target" }
$archive = Join-Path $artifactRoot 'ROLLBACK_BASELINE'
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw "Missing rollback archive: $archive" }
$newPath = Join-Path $target 'backend/order_query/browser_verification.py'
if (Test-Path -LiteralPath $newPath -PathType Leaf) { Remove-Item -LiteralPath $newPath -Force }
tar -xf $archive -C $target
if ($LASTEXITCODE -ne 0) { throw "Rollback archive extraction failed with exit $LASTEXITCODE" }
$expected = @{
  '.gitignore' = '77C1E3F955B69EC777A3D84BE21398983298C99BEF709D1C1C1D0DDF5AD2D83F'
  'backend/main.py' = 'B72CC7128A14F261F3A17EE742E5E3F249971F92160A8225B3329BD696368594'
  'backend/monitor_core/storefront.py' = '4E3227F3E5671D4F834D2D3C3FF7CAC961245DDB04EF863F01682FEE52034AD8'
  'backend/order_query/client.py' = 'E400DCC817CED1B37CA525C3E6B83A95EEFDD24FFB3D17ED3DAADE48D3FDC35F'
  'backend/order_query/routes.py' = 'A2EFD070B7E0EE80DA01E74264333B41877560BB327C89C077B6FDE7051CE3A9'
  'backend/test_order_query_modules.py' = '4F24DE6C195F25790047040E702B35D504F0F90861CA7CA43760B8049F6CBFB3'
  'frontend/src/OrderQueryView.jsx' = '20E540F8D6432820786919430F93DAC8763A3F54C0D08D9240344FC2486E4BC0'
  'frontend/src/orderQuery.css' = '0A5275AED3C4A3EB194C96AC48AA3F5E672C7F7922667EE963FEEFFE6CA821B1'
  'frontend/src/orderQueryModel.js' = '2C42FAEC39D47C0F65846D4198AD143DD62856D8FCC0320F419A9B57F47B904A'
}
foreach ($path in $expected.Keys) {
  $actualPath = Join-Path $target $path
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $actualPath).Hash
  if ($actual -ne $expected[$path]) { throw "Rollback verification failed for ${path}: $actual" }
}
Write-Output "Restored $target; verified $($expected.Count) files; removed backend/order_query/browser_verification.py"
