#!/usr/bin/env pwsh
[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [string]$TargetRoot = '.'
)
$ErrorActionPreference = 'Stop'
$artifactRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$baselineArchive = Join-Path $artifactRoot 'ROLLBACK_BASELINE'
$modifiedArchive = Join-Path $artifactRoot 'MODIFIED_FILE'
if (-not (Test-Path -LiteralPath $baselineArchive -PathType Leaf)) { throw "Missing rollback archive: $baselineArchive" }
if (-not (Test-Path -LiteralPath $modifiedArchive -PathType Leaf)) { throw "Missing modified archive: $modifiedArchive" }
$expectedBaselineArchiveHash = '1E0749A823EB04B9EACAE3D3C658C7ACD7315BA1C2E4CD2BF89D5DD003CFA96B'
$expectedModifiedArchiveHash = 'E305CCA4DCBFD5E1731A6F7B527BDE94EC02B57058CC5A0AF6BE8E439B84A5BA'
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $baselineArchive).Hash -ne $expectedBaselineArchiveHash) { throw 'ROLLBACK_BASELINE changed unexpectedly' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $modifiedArchive).Hash -ne $expectedModifiedArchiveHash) { throw 'MODIFIED_FILE changed unexpectedly' }
$targetRootPath = [IO.Path]::GetFullPath($TargetRoot)
if (-not (Test-Path -LiteralPath $targetRootPath -PathType Container)) { throw "Target root does not exist: $targetRootPath" }
$targetPrefix = $targetRootPath.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
function Resolve-TargetFile([string]$RelativePath) {
  $candidate = [IO.Path]::GetFullPath((Join-Path $targetRootPath $RelativePath))
  if (-not $candidate.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Rollback path escapes target root: $RelativePath" }
  return $candidate
}
$expectedHashes = [ordered]@{
  'backend/main.py' = '37FD5B83FE9BDB02C37BAF96E475DD76F270575C72D39584388294E9D643F89E'
  'backend/order_query/client.py' = '5BC9737EF549271AA111FA10AF3C88A0F95E5417A9C229B2FADF6DD5FBC9CCAB'
  'backend/order_query/complaint.py' = 'A7EBA2EC49EEE5CD8521640D57ED1E57DA1DC96E62C496E65239644B6904F943'
  'backend/order_query/routes.py' = 'A242BF4036DC8905423BCDC58308D55DE0E5DD8CC3D1EECFBA2871B31CF1D0D8'
  'backend/order_query/service.py' = '1E9CFCF287AE405587E785353D83AA99BF34ECBA19F330973B90325B4242EC95'
  'backend/order_query/sessions.py' = '6AFFE1139A48FDB8FEC02AC82090206A7675B764EE2BB4B0F850E490D8121B15'
  'backend/test_order_query_modules.py' = '45B4115DAA756159A91CF90D353587CFA52E2A8D453E225760C8D5134E2FD846'
  'frontend/src/OrderQueryView.jsx' = '4F97BF867569AF74F7832C321AE51D3F901425F19098E1D6FEE0C0DFE71E9400'
  'frontend/src/orderQuery.css' = '1FF2B406C445FE98397970B37E8B9B2E64519D0677E67C13A89417509D649859'
  'frontend/src/orderQueryModel.js' = '8CB7C895B6B8957502F1ED4B9B3F9AFA6018685CD39297B4F7FD94A906559924'
  'frontend/src/orderQueryModel.test.js' = 'A4BEB4057E207749846815225F6BC7A8298B3A24154B4FF50A7F0E5ADFF86ECB'
}
$targetMarker = Resolve-TargetFile 'backend/main.py'
if (-not (Test-Path -LiteralPath $targetMarker -PathType Leaf)) { throw "Target root is not an LDXP source tree: $targetRootPath" }
tar -xf $baselineArchive -C $targetRootPath
if ($LASTEXITCODE -ne 0) { throw "Rollback archive extraction failed with exit $LASTEXITCODE" }
foreach ($relativePath in $expectedHashes.Keys) {
  $targetPath = Resolve-TargetFile $relativePath
  if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) { throw "Rollback verification failed; missing file: $relativePath" }
  $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetPath).Hash
  if ($actualHash -ne $expectedHashes[$relativePath]) { throw "Rollback verification failed for ${relativePath}: expected $($expectedHashes[$relativePath]), got $actualHash" }
}
Write-Output "Restored $targetRootPath to main 357b0ca; verified $($expectedHashes.Count) files; MODIFIED_FILE unchanged $expectedModifiedArchiveHash"
