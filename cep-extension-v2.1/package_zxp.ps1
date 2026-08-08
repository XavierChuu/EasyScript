# Package the EasyScript CEP extension into a signed .zxp (self-signed cert).
# Windows counterpart of package_zxp.sh — the ZXPSignCmd checked into this
# folder is a macOS binary, and Git Bash here has no rsync.
#
# Requires Adobe's ZXPSignCmd.exe:
#   https://github.com/Adobe-CEP/CEP-Resources  (folder ZXPSignCMD-*)
# Put it in PATH, or next to this script, or pass -ZxpSign C:\path\ZXPSignCmd.exe
param(
    [string]$ZxpSign = ""
)

$ErrorActionPreference = "Stop"
$DIR  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ROOT = Split-Path -Parent $DIR

if (-not $ZxpSign) {
    $cmd = Get-Command ZXPSignCmd.exe -ErrorAction SilentlyContinue
    if ($cmd) { $ZxpSign = $cmd.Source }
    elseif (Test-Path "$DIR\ZXPSignCmd.exe") { $ZxpSign = "$DIR\ZXPSignCmd.exe" }
}
if (-not $ZxpSign -or -not (Test-Path $ZxpSign)) {
    Write-Error "ZXPSignCmd.exe not found. Download it from https://github.com/Adobe-CEP/CEP-Resources (ZXPSignCMD), then put it in PATH or next to this script, or pass -ZxpSign <path>."
}

# Staging: copy only the runtime files (drop dev-only files).
$STAGE = Join-Path ([System.IO.Path]::GetTempPath()) ("EasyScriptZxp_" + [guid]::NewGuid().ToString("N"))
$PAYLOAD = Join-Path $STAGE "EasyScript"
New-Item -ItemType Directory -Path $PAYLOAD -Force | Out-Null

# Keep this list in sync with the --exclude flags in package_zxp.sh.
$excludeNames = @(".debug", "install.sh", "install.bat", "package_zxp.sh", "package_zxp.ps1",
                  "ZXPSignCmd", "ZXPSignCmd.exe", ".DS_Store")
$excludeExt   = @(".zxp", ".p12")

Get-ChildItem -Path $DIR -Force | Where-Object {
    ($excludeNames -notcontains $_.Name) -and ($excludeExt -notcontains $_.Extension)
} | ForEach-Object {
    Copy-Item -Path $_.FullName -Destination $PAYLOAD -Recurse -Force
}
# Nested junk that survives the top-level filter.
Get-ChildItem -Path $PAYLOAD -Recurse -Force -Include ".DS_Store", "__MACOSX" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$CERT = Join-Path $DIR "easyscript_cert.p12"
$PW   = "easyscript"
if (-not (Test-Path $CERT)) {
    Write-Host "[zxp] Creating self-signed certificate..."
    & $ZxpSign -selfSignedCert VN HCM "EasyScript" "EasyScript" $PW $CERT
    if ($LASTEXITCODE -ne 0) { Write-Error "Certificate creation failed (exit $LASTEXITCODE)" }
}

$DIST = Join-Path $ROOT "dist"
New-Item -ItemType Directory -Path $DIST -Force | Out-Null
$OUT = Join-Path $DIST "EasyScript-Premiere.zxp"
if (Test-Path $OUT) { Remove-Item $OUT -Force }

Write-Host "[zxp] Signing..."
& $ZxpSign -sign $PAYLOAD $OUT $CERT $PW -tsa http://timestamp.digicert.com
if ($LASTEXITCODE -ne 0) {
    Write-Host "[zxp] Timestamping failed, retrying without TSA..."
    & $ZxpSign -sign $PAYLOAD $OUT $CERT $PW
    if ($LASTEXITCODE -ne 0) { Write-Error "Signing failed (exit $LASTEXITCODE)" }
}

Remove-Item $STAGE -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "[zxp] Done: $OUT"
Write-Host "Install it with a ZXP installer (Anastasiy's Extension Manager / ZXP Installer / aescripts)."
