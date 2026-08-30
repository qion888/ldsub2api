#!/usr/bin/env pwsh
[CmdletBinding()]
param(
  [string]$Target = 'MODIFIED_FILE'
)

$ErrorActionPreference = 'Stop'
$artifactRoot = if ($MyInvocation.MyCommand.Path) {
  Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
  (Get-Location).Path
}
$baseline = Join-Path $artifactRoot 'ROLLBACK_BASELINE'
if (-not (Test-Path -LiteralPath $baseline)) {
  throw "Missing baseline: $baseline"
}

$targetPath = if ([IO.Path]::IsPathRooted($Target)) {
  [IO.Path]::GetFullPath($Target)
} else {
  [IO.Path]::GetFullPath((Join-Path $artifactRoot $Target))
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetPath) | Out-Null
[IO.File]::WriteAllBytes($targetPath, [IO.File]::ReadAllBytes($baseline))
Write-Output "Restored $targetPath from ROLLBACK_BASELINE"
