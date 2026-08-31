#!/usr/bin/env pwsh
$ErrorActionPreference = 'Stop'

$target = if ($args.Count -gt 0) { $args[0] } else { 'MODIFIED_FILE' }
$artifactRoot = if ($MyInvocation.MyCommand.Path) {
  Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
  (Get-Location).Path
}
$baseline = Join-Path $artifactRoot 'ROLLBACK_BASELINE'
if (-not (Test-Path -LiteralPath $baseline)) { throw "Missing baseline: $baseline" }

$targetPath = if ([IO.Path]::IsPathRooted($target)) {
  [IO.Path]::GetFullPath($target)
} else {
  [IO.Path]::GetFullPath((Join-Path $artifactRoot $target))
}
$targetDirectory = Split-Path -Parent $targetPath
New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
[IO.File]::WriteAllBytes($targetPath, [IO.File]::ReadAllBytes($baseline))
Write-Output "Restored $targetPath from ROLLBACK_BASELINE"
