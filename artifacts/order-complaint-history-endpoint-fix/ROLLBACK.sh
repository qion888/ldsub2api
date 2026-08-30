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
$expectedBaselineArchiveHash = '21CD32DBCDB9DCA44F2C4BD429C39BA1534A598270221F561C3CF95D116FF656'
$expectedModifiedArchiveHash = 'B938829F22514C117B58D6671F49E5F73B1C4F6E48B45BD08B09D0FE559683E2'
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
  'backend/main.py' = '05FFDB6274975691A528108FC1BF20DB1C4F00D86B9F646DC772A3E3F5218B67'
  'backend/order_query/client.py' = '0DA63A1EEC65B42340F8ECCC2F76790154AF67B647E9276AEDA3F4CCF5E06D2A'
  'backend/order_query/routes.py' = '69339EEDC5FE50E352AC64FA5C67D16E25CD3FF4C3A97FB8BDBBCBDD37A226CC'
  'backend/test_main.py' = 'EFD89B52DFC0BE6CD99C52699C14D6A71B475F0DD7F566F6FEB18CEFEF2AF1E8'
  'backend/test_order_query_modules.py' = 'B8C2801D3AE8E5D6F74F120F8BB6C263147AD05C620B3919CB2CA8BF8B99898E'
  'frontend/src/OrderQueryView.jsx' = 'C78B678993980D8D176D1C1D88DCC8E44A3A5662FF2A56C298B2A507CDA326D6'
  'frontend/src/orderQueryModel.js' = 'BB28AE2E90360BD242AF00EF0BBFDD0837BB886C90C1B0AE1CADCADC4B655F20'
  'frontend/src/orderQueryModel.test.js' = '663935E80F3CEAF489663AC3DB8A36EEC201A5ABE2B24E5379380F1781853B6C'
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
Write-Output "Restored $targetRootPath to main 1d1f5eb; verified $($expectedHashes.Count) files; MODIFIED_FILE unchanged $expectedModifiedArchiveHash"
