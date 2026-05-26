<#
.SYNOPSIS
    Build EasyScript standalone app for Windows (bundled FFmpeg + ML libs).

.DESCRIPTION
    - Uses the existing backend/venv (or creates one if missing)
    - Installs/updates dependencies including imageio-ffmpeg
    - Runs PyInstaller against easyscript.spec
    - Outputs to dist/EasyScript/EasyScript.exe

    Run from anywhere:
        powershell -ExecutionPolicy Bypass -File scripts\build_app.ps1

.NOTES
    Requires Python 3.11 on PATH. Build folder ~6-8GB during build, final ~3-4GB.
#>

$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$BackendDir = Join-Path $ProjectDir "backend"
$DistDir    = Join-Path $ProjectDir "dist"
$BuildDir   = Join-Path $BackendDir "build"

Write-Host "=== Building EasyScript Standalone App (Windows) ===" -ForegroundColor Cyan
Write-Host "Project: $ProjectDir"
Write-Host "Backend: $BackendDir"
Write-Host "Dist:    $DistDir"
Write-Host ""

# ── Create venv if missing ──
$VenvDir = Join-Path $BackendDir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    $SysPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $SysPython) {
        Write-Error "Python not found on PATH. Install Python 3.11+ first."
        exit 1
    }
    & $SysPython -m venv $VenvDir
}

# ── Install dependencies ──
Write-Host "Installing dependencies (this may take a few minutes)..." -ForegroundColor Yellow
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $BackendDir "requirements.txt")
& $VenvPython -m pip install pyinstaller pywebview

# ── Pre-fetch imageio-ffmpeg binary so PyInstaller can bundle it ──
Write-Host "Verifying bundled ffmpeg binary..." -ForegroundColor Yellow
& $VenvPython -c "import imageio_ffmpeg, os; p = imageio_ffmpeg.get_ffmpeg_exe(); print('ffmpeg:', p, 'size:', round(os.path.getsize(p)/1024/1024,1), 'MB')"

# ── Run PyInstaller ──
Write-Host ""
Write-Host "Running PyInstaller..." -ForegroundColor Yellow
Push-Location $BackendDir
try {
    $PyInstaller = Join-Path $VenvDir "Scripts\pyinstaller.exe"
    & $PyInstaller easyscript.spec --distpath $DistDir --workpath $BuildDir -y
} finally {
    Pop-Location
}

# ── Post-build: compile transformers .py → .pyc ──
# transformers' lazy loader looks for cached .pyc files at runtime; without
# them, Hy-MT2 / HunYuan model loading fails inside the frozen bundle.
$TransformersDir = Join-Path $DistDir "EasyScript\_internal\transformers"
if (Test-Path $TransformersDir) {
    Write-Host ""
    Write-Host "Compiling transformers .py -> .pyc..." -ForegroundColor Yellow
    & $VenvPython -m compileall -q -b $TransformersDir | Out-Null
}

# ── Post-build: drop the duplicate _internal/nvidia/ subtree ──
# PyInstaller bundles nvidia DLLs twice — once at _internal root (our spec)
# and once under _internal/nvidia/<pkg>/bin/ (auto-pulled by deps). The root
# copies are what CTranslate2's LoadLibrary("cublas64_12.dll") finds, so the
# subfolder copies are dead weight (~900 MB).
$NvidiaDup = Join-Path $DistDir "EasyScript\_internal\nvidia"
if (Test-Path $NvidiaDup) {
    Write-Host ""
    Write-Host "Removing duplicate _internal/nvidia/ (~900MB)..." -ForegroundColor Yellow
    Remove-Item $NvidiaDup -Recurse -Force
}

# ── Report result ──
$ExePath = Join-Path $DistDir "EasyScript\EasyScript.exe"
Write-Host ""
if (Test-Path $ExePath) {
    $ExeFolder = Split-Path -Parent $ExePath
    $TotalSizeMB = [math]::Round(((Get-ChildItem -Path $ExeFolder -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
    Write-Host "=== Build complete ===" -ForegroundColor Green
    Write-Host "  Folder:     $ExeFolder"
    Write-Host "  Executable: $ExePath"
    Write-Host "  Size:       $TotalSizeMB MB"
    Write-Host ""
    Write-Host "To run: double-click EasyScript.exe or:"
    Write-Host "  & '$ExePath'"
} else {
    Write-Error "Build failed - executable not found at $ExePath"
    exit 1
}
