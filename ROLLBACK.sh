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
$rootPrefix = $targetRootPath.TrimEnd(
  [IO.Path]::DirectorySeparatorChar,
  [IO.Path]::AltDirectorySeparatorChar
) + [IO.Path]::DirectorySeparatorChar

function Resolve-TargetFile([string]$RelativePath) {
  $candidate = [IO.Path]::GetFullPath((Join-Path $targetRootPath $RelativePath))
  if (-not $candidate.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Rollback path escapes target root: $RelativePath"
  }
  return $candidate
}

tar -xf $baselineArchive -C $targetRootPath
if ($LASTEXITCODE -ne 0) {
  throw "Rollback archive extraction failed with exit $LASTEXITCODE"
}

$createdFile = Resolve-TargetFile 'backend/order_query/detail.py'
if (Test-Path -LiteralPath $createdFile -PathType Leaf) {
  Remove-Item -LiteralPath $createdFile -Force
}

$expectedHashes = @{
  'backend/main.py' = '1F255D654CBD3BD086230FDA5AB879D863E28C9F29F093CC8928881D6F848D50'
  'backend/order_query/client.py' = '0212C09FF5206853C37C30A7A1FA52CA483325192D7068998DC7BC1961A1EA5F'
  'backend/order_query/errors.py' = '03522AA8D9EB61DCA85EBEDBB495E1EF11D86E1DAEA5EAC2B33451DD10FD2A5B'
  'backend/order_query/routes.py' = 'DC22B67CF6A1940432A50385056C5B30BFD6D26673B3A07F8EEEE8BA1DC2EDAB'
  'backend/order_query/service.py' = '81503D2722238CFE1B151ADB7211C0D2E609FB4C75285FCC9B120A9FB620CFED'
  'backend/order_query/sessions.py' = '066713266FC5052B8A4C62267EE0E08EFD3B3E8BCF673CC55A3A2E4DDC0FEA16'
  'backend/test_order_query_modules.py' = '6BAA6B090FAE2FD64AE51B341F61E93907E080D145575DA5AD94074CB239EFD9'
  'frontend/src/OrderQueryView.jsx' = 'B475E6BDFDA86ABD748BA582B3DA5A989C29FFB076F21BB4D0E8291044CB7860'
  'frontend/src/orderQuery.css' = 'C24BB923C512BBEBEED8FBE085A29C2A21CCA3696C8442D272D3720FDF722352'
  'frontend/src/orderQueryModel.js' = '72BA317193DE0C44C5E86EDF1A80D520C5A6AE847D3ADA7E29D574BEA20309CB'
  'frontend/src/orderQueryModel.test.js' = 'E21C78B3F8F3D048C558CFD9BF6E763FFBA88A1940E5882FCFD5F30B59206E39'
}

foreach ($relativePath in $expectedHashes.Keys) {
  $targetPath = Resolve-TargetFile $relativePath
  $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetPath).Hash
  if ($actualHash -ne $expectedHashes[$relativePath]) {
    throw "Rollback verification failed for ${relativePath}: expected $($expectedHashes[$relativePath]), got $actualHash"
  }
}
if (Test-Path -LiteralPath $createdFile) {
  throw 'Rollback verification failed: backend/order_query/detail.py still exists'
}

Write-Output "Restored $targetRootPath to main c9650eb; removed backend/order_query/detail.py; verified $($expectedHashes.Count) files"
