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
  'backend/main.py' = '373C2078ED58331634E001568EC7A22332FEAC6273742B574619B23841CEE953'
  'backend/monitor_core/settings.py' = 'D4D1273380C601EC3A69A3ED3E2B84E9104EA929BD32E786E0F06D2EC875A827'
  'backend/monitor_core/preorders.py' = 'F5408F5F3353D5D2CB7FC9CBA629619031B07B4D3465DDAB57460DC776AA33EE'
  'backend/test_main.py' = 'ACF8CFF88213C814446FC40C3054BA2D912E9670C61D2599FCD8D75EAAE65875'
  'frontend/src/main.jsx' = 'F77222831689AD60936578ADA3F3D17CE85BC26045D12761859F322E5C3D9B70'
  'frontend/src/OrderQueryView.jsx' = '0091FEDE2A77007706A3E88C6E85B595E50B826E46A14127881524EB89279128'
  'frontend/src/orderQuery.css' = 'D58953E6E8B4459745B8BFB1E5692F642A8C8D80084F7833AD15544DE67D1430'
}
foreach ($path in $expected.Keys) {
  $actualPath = Join-Path $target $path
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $actualPath).Hash
  if ($actual -ne $expected[$path]) { throw "Rollback verification failed for ${path}: $actual" }
}
Write-Output "Restored $target; verified $($expected.Count) files"
