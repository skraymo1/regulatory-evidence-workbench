# Regulatory Evidence Workbench

An open-source Python workbench for comparing regulatory documents against a
reference standard. It preserves original-language quotations and source
citations, proposes English analysis and translations, and exports comparison
tables to Excel or Markdown. Streamlit provides the user interface and FastAPI
provides the API.

All generated analysis is provisional and requires qualified expert review.
The application does not make legal, safety or compliance determinations.

## Reference profiles

No standards documents, publication links, or organization-specific sample
catalogs are bundled. Users supply reference and national documents they are
authorized to download, process, and retain.

The storage, retrieval, API, deployment, and evidence-review components are
generic. The complete-inventory extractor and supporting-paragraph parser are
specific to the included profile; add and validate a new extraction profile
before using another reference standard.

## Setup

Install Python 3.11 or later (containers use Python 3.13). The commands below
use PowerShell 7 and run from the repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --pre -e .
```

`--pre` enables the preview Microsoft Agent Framework dependencies.
Configuration is read from environment variables; `.env.example` lists the
available settings but is not loaded automatically. Offline mode requires no
Azure account. Model-backed mode requires Azure CLI sign-in, a Microsoft Foundry
project and an existing model deployment that your account can use.

Source PDFs and saved results are not distributed with the code. Supply documents
you are authorized to use through **Comparison sources**. Local state is stored
under `.data` and is excluded from Git and container builds.

To run the tests:

```powershell
python -m unittest discover -s tests -q
```

Tests requiring optional local `support_docs` PDFs are skipped when those
documents are absent.

## Run locally

### Offline

Offline mode extracts documents and retrieves candidate clauses without model
analysis.

```powershell
$env:POC_AGENT_MODE = "offline"
$env:POC_SEARCH_MODE = "local"
python -m streamlit run src\regulatory_poc\ui\app.py
```

Open `http://localhost:8501`. To run the API in a second activated terminal:

```powershell
$env:POC_AGENT_MODE = "offline"
$env:POC_SEARCH_MODE = "local"
python -m uvicorn regulatory_poc.ui.api:app --host 127.0.0.1 --port 8000
```

API documentation is at `http://127.0.0.1:8000/docs`.

### With Microsoft Foundry

Set your own project endpoint and model deployment name:

```powershell
az login
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<foundry-account>.services.ai.azure.com/api/projects/<project>"
$env:AZURE_AI_MODEL_DEPLOYMENT_NAME = "<model-deployment>"
.\run-foundry.ps1
```

Run `.\run-api-foundry.ps1` in a second activated terminal with the same variables
to start the API. Both scripts use local storage and retrieval; only model calls
go to Foundry. The scripts accept `-Port`, `-ProjectEndpoint` and
`-ModelDeployment`. Model calls incur Azure charges.

## Run in Azure

The application runs as two Azure Container Apps with managed identities,
Azure Blob Storage, Azure AI Search and Microsoft Foundry. The Bicep templates
in `deployments` deploy a new environment in two stages: infrastructure first,
then application images and authentication.

### Prerequisites

- Azure CLI, Bicep 0.45 or later, and the Python environment from Setup.
- An approved subscription and a new resource group, with permission to deploy
  resources and assign the roles defined by the templates.
- An existing Foundry account/project, a generation model deployment, and a
  `text-embedding-3-small` deployment with 1,536-dimensional output.
- Permission to create prompt-agent versions in the Foundry project and to
  submit builds to Azure Container Registry.
- A single-tenant Entra app registration, a client secret, and the object ID of
  the approved user. The application currently restricts access to that one user.

The examples use Sweden Central for infrastructure. Choose regions, model
availability and network access that meet your organization's requirements.
The foundation uses authenticated public service endpoints with private Blob
containers; the optional `deployments\modules\network.bicep` is not wired into this deployment.
Organizations requiring private endpoints must adapt and review the networking
before deployment. The `prod` profile changes sizing, not production certification.

### Deploy infrastructure

Replace the placeholders before running these commands. The deployment commands
create billable Azure resources.

```powershell
az login
az account set --subscription "<subscription-id>"
$resourceGroup = "<new-resource-group>"
$env:REGULATORY_WORKBENCH_OPERATOR_OBJECT_ID = "<bootstrap-operator-object-id>"
$env:REGULATORY_WORKBENCH_FOUNDRY_RESOURCE_GROUP = "<existing-foundry-resource-group>"
$env:REGULATORY_WORKBENCH_FOUNDRY_ACCOUNT_NAME = "<existing-foundry-account>"

az group create --name $resourceGroup --location swedencentral
az deployment group what-if --resource-group $resourceGroup --parameters .\deployments\dev.bicepparam
az deployment group create --name foundation --resource-group $resourceGroup --parameters .\deployments\dev.bicepparam --output none
if ($LASTEXITCODE -ne 0) { throw "Foundation deployment failed." }
$outputs = az deployment group show --name foundation --resource-group $resourceGroup --query properties.outputs --output json | ConvertFrom-Json
```

Use `prod.bicepparam` instead of `dev.bicepparam` for the production sizing
profile. Allow time for role assignments to propagate before the next steps.

### Build images and initialize Search and Foundry

Use a unique release tag for each build.

```powershell
$releaseTag = "<unique-release-tag>"
az acr build --registry $outputs.registryName.value --image "regulatory-api:$releaseTag" --file Dockerfile.api .
if ($LASTEXITCODE -ne 0) { throw "API image build failed." }
az acr build --registry $outputs.registryName.value --image "regulatory-ui:$releaseTag" --file Dockerfile.ui .
if ($LASTEXITCODE -ne 0) { throw "UI image build failed." }

$env:POC_AGENT_MODE = "foundry"
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<foundry-account>.services.ai.azure.com/api/projects/<project>"
$env:AZURE_AI_MODEL_DEPLOYMENT_NAME = "<generation-deployment>"
$env:POC_MODEL_VERSION = "<deployed-generation-model-version>"
$env:AZURE_AI_SEARCH_ENDPOINT = $outputs.searchEndpoint.value
$env:AZURE_AI_EMBEDDING_ENDPOINT = "https://<foundry-account>.openai.azure.com/"
$env:AZURE_AI_EMBEDDING_MODEL = "<text-embedding-3-small-deployment>"
python .\scripts\bootstrap.py
if ($LASTEXITCODE -ne 0) { throw "Search and Foundry bootstrap failed." }
$versions = Get-Content .\.azure\agent-versions.json -Raw | ConvertFrom-Json
$env:POC_AGENT_VERSION = $versions.POC_AGENT_VERSION
$env:POC_CHAT_AGENT_VERSION = $versions.POC_CHAT_AGENT_VERSION
```

Bootstrap creates or updates the source/report indexes and knowledge bases and
creates two prompt-agent versions. Run it once for initial setup; rerunning it
creates new agent versions. It does not import documents. Keep `.azure` local.

### Deploy and run the applications

Inject the Entra secret through your approved secret-management process; do not
write it into a committed parameter file or print compiled parameters.

```powershell
$env:REGULATORY_WORKBENCH_FOUNDATION_NAME = $outputs.foundationName.value
$env:REGULATORY_WORKBENCH_PROFILE = "dev" # Use "prod" with prod.bicepparam.
$env:REGULATORY_WORKBENCH_API_IMAGE = "$($outputs.registryEndpoint.value)/regulatory-api:$releaseTag"
$env:REGULATORY_WORKBENCH_UI_IMAGE = "$($outputs.registryEndpoint.value)/regulatory-ui:$releaseTag"
$env:REGULATORY_WORKBENCH_PROJECT_ENDPOINT = $env:FOUNDRY_PROJECT_ENDPOINT
$env:REGULATORY_WORKBENCH_EMBEDDING_ENDPOINT = $env:AZURE_AI_EMBEDDING_ENDPOINT
$env:REGULATORY_WORKBENCH_TENANT_ID = "<tenant-id>"
$env:REGULATORY_WORKBENCH_AUTH_CLIENT_ID = "<entra-application-client-id>"
$env:REGULATORY_WORKBENCH_ALLOWED_OBJECT_ID = "<approved-user-object-id>"
# REGULATORY_WORKBENCH_AUTH_CLIENT_SECRET must already be set by your secret-management process.
az deployment group what-if --resource-group $resourceGroup --parameters .\deployments\applications.example.bicepparam
az deployment group create --name applications --resource-group $resourceGroup --parameters .\deployments\applications.example.bicepparam --output none
if ($LASTEXITCODE -ne 0) { throw "Application deployment failed." }
$urls = az deployment group show --name applications --resource-group $resourceGroup --query properties.outputs --output json | ConvertFrom-Json
$urls.apiUrl.value
$urls.uiUrl.value
```

Add both URLs followed by `/.auth/login/aad/callback` as **Web** redirect URIs on
the Entra registration. Sign in as the approved user, open **Comparison sources**
and import the approved documents. Hosted storage is separate from local data.
Verify that anonymous and unapproved users cannot access either application.

For subsequent releases, build new image tags and redeploy the applications
stage. Do not rerun bootstrap unless changing indexes, knowledge bases or agents.
`azure.yaml` also supports `azd deploy api` and `azd deploy ui` after configuring
an azd environment with `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`,
`AZURE_CONTAINER_REGISTRY_ENDPOINT`, `SERVICE_API_RESOURCE_NAME` and
`SERVICE_UI_RESOURCE_NAME` from the foundation outputs. Infrastructure is managed
by the staged Bicep commands above, not `azd provision` or `azd up`.
