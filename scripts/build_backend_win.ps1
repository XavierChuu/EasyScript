<#
.SYNOPSIS
    Build the headless EasyScript backend for Windows (for the Premiere CEP panel).

.DESCRIPTION
    Produces dist_backend\EasyScript-backend\EasyScript-backend.exe — a console-less
    FastAPI server on 127.0.0.1:9876 that the panel launches automatically.
    Run on a real Windows PC (PyInstaller does NOT cross-compile from macOS).

    Usage (from anywhere):
        powershell -ExecutionPolicy Bypass -File scripts\build_backend_win.ps1

.NOTES
    Requires Python 3.11+ on PATH. Build needs ~6-8 GB free; final ~1.2-1.5 GB.
    After it finishes, copy dist_backend\EasyScript-backend\* into your Windows
    release folder's  backend\  directory.
#>

$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$BackendDir = Join-Path $ProjectDir "backend"
$DistDir    = Join-Path $BackendDir "dist_backend"
$BuildDir   = Join-Path $BackendDir "build_backend"

Write-Host "=== Building EasyScript headless backend (Windows) ===" -ForegroundColor Cyan
Write-Host "Backend: $BackendDir"

# Create venv if missing
# Separate from backendenv (the macOS dev venv) so both can coexist.
$VenvDir    = Join-Path $BackendDir "venv-win"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    $SysPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $SysPython) { Write-Error "Python not found on PATH. Install Python 3.11+ first."; exit 1 }
    & $SysPython -m venv $VenvDir
}

Write-Host "Installing dependencies (a few minutes)..." -ForegroundColor Yellow
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $BackendDir "requirements.txt")
& $VenvPython -m pip install pyinstaller

Write-Host "Verifying bundled ffmpeg..." -ForegroundColor Yellow
& $VenvPython -c "import imageio_ffmpeg, os; p = imageio_ffmpeg.get_ffmpeg_exe(); print('ffmpeg:', p)"

Write-Host "Running PyInstaller (easyscript_backend.spec)..." -ForegroundColor Yellow
Push-Location $BackendDir
try {
    $PyInstaller = Join-Path $VenvDir "Scripts\pyinstaller.exe"
    & $PyInstaller easyscript_backend.spec --distpath $DistDir --workpath $BuildDir -y
} finally { Pop-Location }

# Post-build: transformers .py -> .pyc (lazy loader needs the cache at runtime)
$TransformersDir = Join-Path $DistDir "EasyScript-backend\_internal\transformers"
if (Test-Path $TransformersDir) {
    Write-Host "Compiling transformers .py -> .pyc..." -ForegroundColor Yellow
    & $VenvPython -m compileall -q -b $TransformersDir | Out-Null
}

# Post-build: drop the duplicate _internal\nvidia subtree (~900 MB)
$NvidiaDup = Join-Path $DistDir "EasyScript-backend\_internal\nvidia"
if (Test-Path $NvidiaDup) {
    Write-Host "Removing duplicate _internal\nvidia (~900MB)..." -ForegroundColor Yellow
    Remove-Item $NvidiaDup -Recurse -Force
}

$ExePath = Join-Path $DistDir "EasyScript-backend\EasyScript-backend.exe"
Write-Host ""
if (Test-Path $ExePath) {
    $ExeFolder = Split-Path -Parent $ExePath
    $TotalSizeMB = [math]::Round(((Get-ChildItem -Path $ExeFolder -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
    Write-Host "=== Build complete ===" -ForegroundColor Green
    Write-Host "  Folder:     $ExeFolder"
    Write-Host "  Executable: $ExePath  ($TotalSizeMB MB)"
    Write-Host ""
    Write-Host "Next: copy the CONTENTS of '$ExeFolder' into your release backend\ folder."
} else {
    Write-Error "Build failed - $ExePath not found"; exit 1
}
