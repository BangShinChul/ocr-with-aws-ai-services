# One-time setup on Windows: Python virtualenv, dependencies, AWS preflight check, .env.
#   .\setup.ps1 --profile <aws-profile> --region <aws-region> [--create-bucket | --bucket NAME]
# All arguments are passed to scripts/setup_env.py (see --help).
# If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\setup.ps1 ...
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = if ($env:PYTHON) { $env:PYTHON } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
$pyArgs = if ($py -eq "py") { @("-3") } else { @() }

& $py @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { Write-Error "Python 3.10+ is required (set `$env:PYTHON to its path)."; exit 1 }

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating virtual environment in .venv ..."
    & $py @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
Write-Host "Installing dependencies ..."
& $venvPy -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $venvPy scripts\setup_env.py @args
exit $LASTEXITCODE
