#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'

$target = if ($args.Count -gt 0) { $args[0] } else { 'MODIFIED_FILE' }
$repoRoot = if ($MyInvocation.MyCommand.Path) {
  Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
  (Get-Location).Path
}
$baseline = Join-Path $repoRoot 'ROLLBACK_BASELINE'
if (-not (Test-Path -LiteralPath $baseline)) { throw "Missing baseline: $baseline" }
$targetPath = if ([IO.Path]::IsPathRooted($target)) {
  [IO.Path]::GetFullPath($target)
} else {
  [IO.Path]::GetFullPath((Join-Path $repoRoot $target))
}
$targetDir = Split-Path -Parent $targetPath
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
[IO.File]::WriteAllBytes($targetPath, [IO.File]::ReadAllBytes($baseline))
$expectedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $baseline).Hash
$restoredHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetPath).Hash
if ($restoredHash -ne $expectedHash) {
  throw "Rollback verification failed: expected $expectedHash, got $restoredHash"
}
Write-Output "Restored $targetPath from ROLLBACK_BASELINE; SHA256=$restoredHash"
