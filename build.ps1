# Build the single-file executable next to the sources.
#
# Intermediate output goes to a temporary directory so the working tree keeps
# only the finished binary, which .gitignore excludes.
#
#   .\build.ps1            build
#   .\build.ps1 -Icon      regenerate the icon first

param(
    [switch] $Icon
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Work = Join-Path $env:TEMP 'clash-traffic-build'

# PyInstaller reports progress on stderr. Windows PowerShell turns any line a
# native command writes there into a terminating error while the preference
# above is in force, so native calls run with it relaxed and are judged by
# their exit code instead.
function Invoke-Native {
    param([string] $What, [scriptblock] $Command)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What exited with code $LASTEXITCODE" }
}

function Test-Module {
    param([string] $Name)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { python -c "import $Name" *> $null } finally { $ErrorActionPreference = $previous }
    return $LASTEXITCODE -eq 0
}

Push-Location $Here
try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw 'python not found on PATH'
    }
    if (-not (Test-Module 'PyInstaller')) {
        throw 'PyInstaller not installed: python -m pip install pyinstaller'
    }
    if (-not (Test-Module 'webview')) {
        throw 'pywebview not installed: python -m pip install pywebview'
    }

    if ($Icon) {
        Write-Host '  generating icon...'
        Invoke-Native 'icon generation' {
            python (Join-Path $Here 'assets\make-icon.py') `
                (Join-Path $Here 'clash-traffic.ico') asym
        }
    }

    Write-Host '  building...'
    Invoke-Native 'PyInstaller' {
        python -m PyInstaller --onefile --windowed --name ClashTraffic `
            --icon (Join-Path $Here 'clash-traffic.ico') `
            --distpath (Join-Path $Work 'dist') `
            --workpath (Join-Path $Work 'work') `
            --specpath $Work `
            --add-data "$(Join-Path $Here 'ui.html');." `
            --collect-all webview --collect-all clr_loader `
            --hidden-import paths --hidden-import ui --hidden-import collector `
            --hidden-import dashboard --hidden-import report --hidden-import status `
            --hidden-import live --hidden-import mihomo_api `
            --noconfirm app.py
    }

    Copy-Item (Join-Path $Work 'dist\ClashTraffic.exe') $Here -Force
    $exe = Join-Path $Here 'ClashTraffic.exe'
    Write-Host ("  built {0} ({1:N1} MB)" -f $exe, ((Get-Item $exe).Length / 1MB)) -ForegroundColor Green
}
finally {
    Pop-Location
}
