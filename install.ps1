# One-time setup for doc_capture_harness (Capture.bat runs this automatically).
# Manual: right-click > "Run with PowerShell", or
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Versions with ready-made Windows packages for every dependency. Newer Pythons
# (e.g. 3.14/3.15) are skipped: some packages would have to be compiled.
$Supported = @("3.12", "3.13", "3.11", "3.10")
$Marker = Join-Path $PSScriptRoot ".venv\setup-ok.txt"
$VenvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Get-PyVersion([string]$Exe, [string[]]$PyArgs) {
    try {
        $out = & $Exe @PyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ("$out").Trim() }
    } catch { }
    return $null
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in $Supported) {
            if ((Get-PyVersion "py" @("-$v")) -eq $v) { return @{ Exe = "py"; Args = @("-$v"); Ver = $v } }
        }
    }
    foreach ($v in $Supported) {
        $n = $v.Replace(".", "")
        foreach ($p in @("$env:LOCALAPPDATA\Python\pythoncore-$v-64\python.exe",
                         "$env:LOCALAPPDATA\Programs\Python\Python$n\python.exe",
                         "$env:ProgramFiles\Python$n\python.exe")) {
            if ((Test-Path $p) -and ((Get-PyVersion $p @()) -eq $v)) { return @{ Exe = $p; Args = @(); Ver = $v } }
        }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            $ver = Get-PyVersion $cmd.Source @()
            if ($Supported -contains $ver) { return @{ Exe = $cmd.Source; Args = @(); Ver = $ver } }
        }
    }
    return $null
}

function Update-PathFromRegistry {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Install-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        # The Python install manager (python.org, 2025+) can add 3.12 next to newer versions.
        Write-Host "Trying: py install 3.12"
        try { & py install 3.12 2>&1 | Out-Host } catch { }
        $found = Find-Python
        if ($found) { return $found }
    }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Trying: winget install Python.Python.3.12"
        try { & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements | Out-Host } catch { }
        Update-PathFromRegistry
        $found = Find-Python
        if ($found) { return $found }
    }
    return $null
}

Write-Host "== doc_capture_harness setup ==" -ForegroundColor Cyan
if (Test-Path $Marker) { Remove-Item $Marker -Force }

$py = Find-Python
if (-not $py) {
    Write-Host "No Python $($Supported -join '/') found (newer versions such as 3.14+ are not supported yet). Installing Python 3.12..."
    $py = Install-Python
    if (-not $py) {
        throw ("Could not install Python 3.12 automatically. Install it from https://www.python.org/downloads/release/python-3128/ " +
               "(64-bit, 'Add to PATH'), then run Capture.bat again. Your other Python versions can stay installed.")
    }
}
Write-Host "Using Python $($py.Ver) ($($py.Exe) $($py.Args -join ' '))"

# Rebuild a virtual environment made with an unsupported Python or left half-installed.
if (Test-Path $VenvPy) {
    $vv = Get-PyVersion $VenvPy @()
    if (-not ($Supported -contains $vv)) {
        Write-Host "Removing old .venv (Python $vv)..."
        Remove-Item -Recurse -Force (Join-Path $PSScriptRoot ".venv")
    }
}
if (-not (Test-Path $VenvPy)) {
    Write-Host "Creating virtual environment (.venv)..."
    & $py.Exe @($py.Args) -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}

& $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
Write-Host "Installing Python packages..."
& $VenvPy -m pip install -r requirements.txt --prefer-binary --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Installing Python packages failed (see the messages above)." }

$tess = @("$env:ProgramFiles\Tesseract-OCR\tesseract.exe", "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe",
          "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe") | Where-Object { $_ -and (Test-Path $_) }
if (-not $tess -and -not (Get-Command tesseract -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing Tesseract OCR..."
        try { & winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements | Out-Host } catch { }
    } else {
        Write-Host "Tesseract OCR not found and winget is unavailable. Install it from https://github.com/UB-Mannheim/tesseract/wiki" -ForegroundColor Yellow
    }
}

"setup completed $(Get-Date -Format s) with Python $($py.Ver)" | Set-Content -Path $Marker -Encoding ASCII

Write-Host ""
Write-Host "Checking this machine..." -ForegroundColor Cyan
& $VenvPy -m doc_capture_harness doctor
Write-Host ""
Write-Host "Setup done. Drag a PDF or a folder onto Capture.bat to start." -ForegroundColor Green
exit 0
