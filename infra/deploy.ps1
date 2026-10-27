# Deploy / remove the OCR pipeline stack (Windows PowerShell).
#
#   .\deploy.ps1 -AwsProfile my-poc -Region ap-northeast-2            # synth (+cdk-nag), confirm, deploy
#   .\deploy.ps1 -AwsProfile my-poc -Region ap-northeast-2 -Diff      # show changes only
#   .\deploy.ps1 -AwsProfile my-poc -Region ap-northeast-2 -Destroy   # delete the stack and its data
#   extra CDK context:  -Context modelId=global.anthropic.claude-opus-5-5,reviewTtlDays=3
param(
    [Parameter(Mandatory = $true)][string]$AwsProfile,
    [Parameter(Mandatory = $true)][string]$Region,
    [switch]$Diff,
    [switch]$Destroy,
    [switch]$Yes,
    [string[]]$Context = @()
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    python -m venv .venv
    & $py -m pip install -q -r requirements.txt
}
if (-not (Test-Path "node_modules\.bin\cdk.cmd")) { npm install --no-fund --no-audit --silent }

# cdk.json runs ".venv/bin/python"; point it at the Windows venv for this session.
$cdkArgs = @("--profile", $AwsProfile, "--region", $Region, "--app", "`"$py`" app.py")
foreach ($c in $Context) { foreach ($kv in $c.Split(",")) { $cdkArgs += @("-c", $kv) } }
$env:AWS_REGION = $Region; $env:AWS_DEFAULT_REGION = $Region; $env:CDK_DEFAULT_REGION = $Region
$cdk = "node_modules\.bin\cdk.cmd"

$account = aws sts get-caller-identity --profile $AwsProfile --query Account --output text
Write-Host "Account $account  Region $Region  Profile $AwsProfile"

function Confirm-Step($message) {
    if ($Yes) { return }
    $answer = Read-Host "$message [y/N]"
    if ($answer -ne "y" -and $answer -ne "Y") { Write-Host "Cancelled."; exit 1 }
}

if ($Diff) { & $cdk diff --method=template @cdkArgs; exit $LASTEXITCODE }

if ($Destroy) {
    Confirm-Step "Delete the stack, its buckets (all scans and results) and tables in $account/$Region?"
    & $cdk destroy --force @cdkArgs
    exit $LASTEXITCODE
}

aws cloudformation describe-stacks --stack-name CDKToolkit --profile $AwsProfile --region $Region *> $null
if ($LASTEXITCODE -ne 0) {
    Confirm-Step "CDK is not bootstrapped in $account/$Region. Bootstrap now?"
    & $cdk bootstrap "aws://$account/$Region" @cdkArgs
}
& $cdk synth --quiet @cdkArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $cdk diff --method=template @cdkArgs
Confirm-Step "Deploy these changes to $account/$Region?"
& $cdk deploy --require-approval never --outputs-file .build\outputs.json @cdkArgs
exit $LASTEXITCODE
