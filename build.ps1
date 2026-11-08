<#
.SYNOPSIS
    Compila Audio Manager Pro: pruebas -> PyInstaller -> instalador.

.DESCRIPTION
    1. Corre las pruebas de tests/ (se puede saltar con -SkipTests).
    2. Genera dist\AudioManagerPro\ con PyInstaller (AudioManagerPro.spec).
    3. Genera dist\installer\AudioManagerPro-Setup-<versión>.exe con Inno Setup
       (se puede saltar con -SkipInstaller).
    La versión sale de version.py.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\build.ps1
#>
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $PSScriptRoot '.venv\Scripts\pyinstaller.exe'
if (-not (Test-Path $python)) {
    throw "No se encontró el entorno virtual (.venv). Crealo con: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
}

function Invoke-Step([string]$Name, [scriptblock]$Block) {
    Write-Host "==> $Name" -ForegroundColor Cyan
    # En PowerShell 5.1 lo que un .exe escribe en stderr (PyInstaller,
    # unittest) se vuelve un "error" si la salida está redirigida; el éxito
    # se decide solo por el código de salida.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Block 2>&1 | ForEach-Object { "$_" }
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0) { throw "Falló: $Name (código $LASTEXITCODE)" }
}

# Se lee version.py como UTF-8 en vez de pedírselo a Python: la salida de un
# .exe pasa por la página de códigos de la consola y rompería las tildes
# (p. ej. en el autor) en consolas no UTF-8 o en GitHub Actions.
$versionPy = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'version.py') -Raw -Encoding UTF8
function Get-VersionField([string]$Name) {
    if ($versionPy -match "(?m)^$Name\s*=\s*[""']([^""']*)[""']") { return $Matches[1] }
    throw "No se encontró $Name en version.py"
}
$version = Get-VersionField '__version__'
$appName = Get-VersionField 'APP_NAME'
$publisher = Get-VersionField 'PUBLISHER'
Write-Host "$appName $version" -ForegroundColor Green

if (-not $SkipTests) {
    Invoke-Step 'Pruebas' { & $python -m unittest discover -s tests }
}

Invoke-Step 'PyInstaller' { & $pyinstaller --noconfirm --clean AudioManagerPro.spec }

if (-not $SkipInstaller) {
    $iscc = @(
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source,
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $iscc) {
        throw "No se encontró Inno Setup 6 (ISCC.exe). Instalalo con: winget install JRSoftware.InnoSetup"
    }
    Invoke-Step 'Instalador (Inno Setup)' {
        & $iscc /Q "/DMyAppVersion=$version" "/DMyAppName=$appName" "/DMyAppPublisher=$publisher" installer\AudioManagerPro.iss
    }
    Write-Host "Listo: dist\installer\AudioManagerPro-Setup-$version.exe" -ForegroundColor Green
} else {
    Write-Host "Listo: dist\AudioManagerPro\AudioManagerPro.exe" -ForegroundColor Green
}
