<#
.SYNOPSIS
    Assemble the Windows distribution of EasyScript (panel + backend + guides).

.DESCRIPTION
    Output: release\EasyScript-<version>-Windows\  and  release\EasyScript-<version>-Windows.zip
    Uses the signed panel in dist\EasyScript-Premiere.zxp and the backend in
    backend\dist_backend\EasyScript-backend (build them first:
    cep-extension-v2.1\package_zxp.ps1 and scripts\build_backend_win.ps1).

    Usage:  powershell -ExecutionPolicy Bypass -File scripts\make_release_win.ps1
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$Zxp = Join-Path $Root "dist\EasyScript-Premiere.zxp"
$Backend = Join-Path $Root "backend\dist_backend\EasyScript-backend"
foreach ($p in @($Zxp, (Join-Path $Backend "EasyScript-backend.exe"))) {
    if (-not (Test-Path $p)) { Write-Error "Missing $p - build it first."; exit 1 }
}
$Version = ([xml](Get-Content (Join-Path $Root "cep-extension-v2.1\CSXS\manifest.xml"))).ExtensionManifest.ExtensionBundleVersion
$Name = "EasyScript-$Version-Windows"
$Out = Join-Path $Root "release\$Name"

Write-Host "[release] Assembling $Out" -ForegroundColor Cyan
if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force (Join-Path $Out "backend") | Out-Null
Copy-Item $Zxp $Out
Copy-Item (Join-Path $Root "release-template\install-win.bat") $Out
Copy-Item (Join-Path $Root "release-template\*.pdf") $Out
robocopy $Backend (Join-Path $Out "backend") /E /NFL /NDL /NJH /NJS /NP /MT:16 | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Error "Copying the backend failed (robocopy $LASTEXITCODE)"; exit 1 }

$Zip = Join-Path $Root "release\$Name.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
Write-Host "[release] Zipping (a few minutes, ~3 GB)..." -ForegroundColor Yellow
# Windows' bundled bsdtar writes zip archives without Compress-Archive's 2 GB limit.
Push-Location (Join-Path $Root "release")
try { & tar.exe -a -c -f "$Name.zip" $Name } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { Write-Error "zip failed"; exit 1 }

$SizeMB = [math]::Round((Get-Item $Zip).Length / 1MB)
Write-Host "[release] Done: $Zip ($SizeMB MB)" -ForegroundColor Green
Write-Host "  Users unzip it and double-click install-win.bat."
