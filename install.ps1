# One-time setup for doc_capture_harness.
# Right-click this file > "Run with PowerShell", or run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Find-Python {
    foreach ($v in @("3.12", "3.13", "3.11", "3.10")) {
        try {
            & py "-$v" -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { return @{ Exe = "py"; Args = @("-$v") } }
        } catch { }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        & python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return @{ Exe = "python"; Args = @() } }
    }
    return $null
}

Write-Host "== doc_capture_harness setup ==" -ForegroundColor Cyan

$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.10+ not found - installing Python 3.12 with winget..."
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $py = Find-Python
    if (-not $py) { throw "Python was installed. Close this window, open a new one and run install.ps1 again." }
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment (.venv)..."
    & $py.Exe @($py.Args) -m venv .venv
}
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip --quiet
Write-Host "Installing Python packages..."
& $venvPy -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$tess = @("$env:ProgramFiles\Tesseract-OCR\tesseract.exe", "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe",
          "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe") | Where-Object { Test-Path $_ }
if (-not $tess -and -not (Get-Command tesseract -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Tesseract OCR with winget..."
    winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements
}

Write-Host ""
Write-Host "Checking this machine..." -ForegroundColor Cyan
& $venvPy -m doc_capture_harness doctor
Write-Host ""
Write-Host "Done. Drag a PDF or a folder onto Capture.bat to start." -ForegroundColor Green
