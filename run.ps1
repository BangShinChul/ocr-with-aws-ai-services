# .\run.ps1                    start the web UI on http://127.0.0.1:7860
# .\run.ps1 evaluate [...]     batch accuracy report over data\ (see --help)
# .\run.ps1 template <file>    empty truth file from data\schema\<file>.json
# .\run.ps1 check              re-run the AWS preflight check (no changes)
# .\run.ps1 pipeline <cmd>     deployed pipeline: upload | status | pending | show | approve | reject | result
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Error "Run .\setup.ps1 first."; exit 1 }

$cmd = if ($args.Count -gt 0) { $args[0] } else { "ui" }
$rest = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }

switch ($cmd) {
    "ui"       { & $venvPy app.py @rest }
    "evaluate" { & $venvPy evaluate.py @rest }
    "template" { & $venvPy scripts\make_truth_template.py @rest }
    "check"    { & $venvPy scripts\setup_env.py --check-only @rest }
    "pipeline" { & $venvPy pipeline_client.py @rest }
    default    { Write-Error "Unknown command: $cmd (use ui | evaluate | template | check | pipeline)"; exit 1 }
}
exit $LASTEXITCODE
