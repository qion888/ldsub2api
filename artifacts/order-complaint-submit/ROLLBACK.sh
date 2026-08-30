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
$modifiedArchive = Join-Path $artifactRoot 'MODIFIED_FILE'
if (-not (Test-Path -LiteralPath $baselineArchive -PathType Leaf)) {
  throw "Missing rollback archive: $baselineArchive"
}
if (-not (Test-Path -LiteralPath $modifiedArchive -PathType Leaf)) {
  throw "Missing modified archive: $modifiedArchive"
}

$targetRootPath = [IO.Path]::GetFullPath($TargetRoot)
if (-not (Test-Path -LiteralPath $targetRootPath -PathType Container)) {
  throw "Target root does not exist: $targetRootPath"
}
$targetPrefix = $targetRootPath.TrimEnd(
  [IO.Path]::DirectorySeparatorChar,
  [IO.Path]::AltDirectorySeparatorChar
) + [IO.Path]::DirectorySeparatorChar

function Resolve-TargetFile([string]$RelativePath) {
  $candidate = [IO.Path]::GetFullPath((Join-Path $targetRootPath $RelativePath))
  if (-not $candidate.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Rollback path escapes target root: $RelativePath"
  }
  return $candidate
}

$expectedHashes = @{
  'backend/main.py' = '4487D3B57A2E312623855BBA2AA98BBAF75B735B016C87941B070B6A99D2EB2E'
  'backend/order_query/client.py' = '6C936D0AF7AA7FF1FAFD284FAC3293F1A87BCC1F57130017133073F4AE929CF4'
  'backend/order_query/complaint.py' = '01C9131AF8CCB4BFAE814657A892E518CD93262DFFF324C6F16E1DA2F192B17A'
  'backend/order_query/errors.py' = '942248BA0553E746DF396A65D33318E78136F76E265786D68603309AAC46556A'
  'backend/order_query/routes.py' = 'EF73A791B1D9D5368BF454C1D34326FA0F2FBF136EB5B9150DB93149C82EDECE'
  'backend/order_query/service.py' = '212C7D6535F9FEBC97C405235C5AC4153E506E797C09ECE4B3F9C4DEF2CA7E05'
  'backend/order_query/sessions.py' = 'A3EE0AE735064771A6CAF1F33F6BFD708464AFA3BD5317F7C86CDE658992B09A'
  'backend/test_main.py' = 'F18A58BDDA5918D0319850DBBEC9864E8E416FC54E65CC3486DA92E0294EF529'
  'backend/test_order_query_modules.py' = '874D78203E60566C843A2D854A45BC5DD3697E0220BE2998BD599A9FB4669D1A'
  'frontend/src/OrderQueryView.jsx' = '431547BAAF5C9257C88EA06152B72DEF962C88B7F4A1B46BDD0299C4D27A7138'
  'frontend/src/main.jsx' = '954E4705A84C942AED0CAF440ED94FE7D23F6DCA1EBB72B98FB06BD6F895AA9D'
  'frontend/src/orderQuery.css' = '2BF5D97C6F603C9D7DA1817142E13148A341F6AF2D93B8F5F2BCA0CF8733AA87'
  'frontend/src/orderQueryModel.js' = '0873C8A08DC36819BD522B980B23F9FD1A42FCB4D1B57B26108085FCC112AF58'
  'frontend/src/orderQueryModel.test.js' = 'C816664415AFA909E2213D12694304F59DCD88D0AE49B717EB9CCDD5D183EF3E'
  'start.ps1' = '895D82F7D3A239DEEF633428E7662786DD14AA31ACFF24438F38EB7186705F71'
}

foreach ($relativePath in $expectedHashes.Keys) {
  Resolve-TargetFile $relativePath | Out-Null
}

tar -xf $baselineArchive -C $targetRootPath
if ($LASTEXITCODE -ne 0) {
  throw "Rollback archive extraction failed with exit $LASTEXITCODE"
}

foreach ($relativePath in $expectedHashes.Keys) {
  $targetPath = Resolve-TargetFile $relativePath
  if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
    throw "Rollback verification failed; missing file: $relativePath"
  }
  $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetPath).Hash
  if ($actualHash -ne $expectedHashes[$relativePath]) {
    throw "Rollback verification failed for ${relativePath}: expected $($expectedHashes[$relativePath]), got $actualHash"
  }
}

$modifiedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $modifiedArchive).Hash
$expectedModifiedHash = 'A169373F4B930F3FDAD776CC1FFEED0B4016317AEF1DA3FA701CD1FD31EC744E'
if ($modifiedHash -ne $expectedModifiedHash) {
  throw "MODIFIED_FILE changed unexpectedly: expected $expectedModifiedHash, got $modifiedHash"
}

Write-Output "Restored $targetRootPath to baseline main c8f214f; verified $($expectedHashes.Count) files; MODIFIED_FILE unchanged $modifiedHash"
