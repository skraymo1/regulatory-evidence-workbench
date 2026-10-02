param(
    [int]$Port = 8501,
    [string]$ProjectEndpoint = $env:FOUNDRY_PROJECT_ENDPOINT,
    [string]$ModelDeployment = $env:AZURE_AI_MODEL_DEPLOYMENT_NAME
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($ProjectEndpoint) -or [string]::IsNullOrWhiteSpace($ModelDeployment)) {
    throw "Set FOUNDRY_PROJECT_ENDPOINT and AZURE_AI_MODEL_DEPLOYMENT_NAME, or pass -ProjectEndpoint and -ModelDeployment."
}

$env:POC_AGENT_MODE = "foundry"
$env:POC_SEARCH_MODE = "local"
$env:POC_SOURCE_KB = ""
$env:POC_REPORT_KB = ""
$env:FOUNDRY_PROJECT_ENDPOINT = $ProjectEndpoint.Trim()
$env:AZURE_AI_MODEL_DEPLOYMENT_NAME = $ModelDeployment.Trim()
$env:PYTHONPATH = Join-Path $projectRoot "src"

Set-Location $projectRoot
python -m streamlit run src\regulatory_poc\ui\app.py --server.address 127.0.0.1 --server.port $Port --browser.gatherUsageStats false
exit $LASTEXITCODE
