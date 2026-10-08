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

## Run in Azure with azd

**No existing Azure resources are required.** `azd up` uses the Bicep entry point
in `deployments\main.bicep` and lifecycle hooks to provision and initialize the
complete workbench:

- A resource group, Microsoft Foundry account/project, a pinned GPT-5.4 mini
  deployment, and `text-embedding-3-small` with 1,536-dimensional vectors.
- Azure AI Search, source/report indexes, knowledge sources and knowledge bases,
  and the two versioned Foundry prompt agents.
- Blob Storage with private document/report/state containers, Container Registry,
  Log Analytics, a VNet-integrated Container Apps environment, and API/UI
  Container Apps.
- A virtual network, delegated Container Apps subnet, private-endpoint subnet,
  network security groups, a Blob private endpoint and linked private DNS zone.
- Managed identities and scoped RBAC for the applications, Search vectorizer,
  Foundry project identity and bootstrap operator.
- A single-tenant Entra application/service principal, a generated client secret
  stored in Key Vault, and sign-in callbacks for both applications.

All infrastructure resources are defined in Bicep, including the Entra application
and service principal through the pinned
[Microsoft Graph extension](https://learn.microsoft.com/graph/templates/overview-bicep-templates-for-graph).
Bicep generates the application/client ID and exports
`REGULATORY_WORKBENCH_AUTH_CLIENT_ID`; no authentication ID is a required input.
The generated application and service-principal object IDs are also exported.
Both sign-in callback URLs are configured in Bicep from the Container Apps
environment's domain. Credential generation and Search/Foundry data-plane
initialization run automatically through azd hooks. Foundry uses the basic agent
setup with platform-managed agent storage; application documents and results use
the deployed Blob account. A separate Cosmos DB account is not required.

### Account requirements and tools

No pre-created resources, app registrations, authentication IDs or secrets are
required. You need an Azure account with an active subscription and access to its
Entra tenant. The deploying user must be able to create resources and assign
roles (for example, subscription **Owner**), register resource providers, and
create an Entra application and service principal in that subscription's tenant.
Microsoft Graph Bicep deployment requires a work/school tenant identity with
the delegated `Application.ReadWrite.All` permission; subscription Owner alone
does not grant directory permissions. Personal Microsoft account authentication
is not supported by the Graph extension. Use the tenant's work/school identity
for this deployment.
Tenant policies, subscription restrictions and model quotas cannot be bypassed
by a template. Restricted organizations may need an administrator to grant these
permissions or approve model quota first.

Run from this checkout with current [Azure Developer CLI
(azd)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd),
[Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), Python 3.11+
and PowerShell 7 on Windows. Linux/macOS hooks use `sh` and `python3`.
Azure Cloud Shell is also an option; ensure azd is installed and the checkout
is available there. No local Docker installation is needed: images are built
in Azure Container Registry. The hooks create an environment-specific Python
virtual environment and install the project's bootstrap dependencies only if
they are missing. Bicep 0.45+ is required; Azure CLI manages its Bicep installation.

### First deployment

These commands create **billable resources**. Sign in to both CLIs as the same
user in the subscription's tenant. Use a 2-16 character environment name with
lowercase letters, digits and hyphens.

```powershell
az login
azd auth login
azd env new regwork-dev
azd up
```

azd selects a subscription; if none has been selected, the hook uses the Azure
CLI's current subscription. Infrastructure defaults to **North Europe** and
Foundry defaults independently to **Sweden Central**. Container Apps uses
AKS-backed regional capacity, so Foundry model availability does not imply
Container Apps capacity in the same region.
To choose another subscription or region before `azd up`:

```powershell
azd env set AZURE_SUBSCRIPTION_ID "<subscription-id>"
azd env set AZURE_LOCATION "<azure-region>"
azd env set AZURE_FOUNDRY_LOCATION "<foundry-region>"
```

No Foundry endpoint, model deployment, resource group, user object ID, Entra
registration or secret needs to be prepared manually. The hook discovers the
deploying user and restricts both applications to that user by default. It
registers missing resource providers; Bicep creates the infrastructure and Entra
registration and records the generated client ID. The postprovision hook stores
the generated 180-day credential in Key Vault through a secure Bicep/ARM
deployment, initializes Search and Foundry after data-plane RBAC propagation,
builds initial images and deploys the application Bicep with the
generated client ID. **No placeholder containers are deployed.** The usual azd
service deployment then takes over image releases.

The credential never goes into CLI arguments, azd's `.env`, or local parameter
files. The credential deployment sends the value to ARM as an in-memory secure
parameter; the application deployment receives only a Key Vault secret reference.
Environment outputs,
pinned agent versions and bootstrap state are kept under `.azure` (Git-ignored);
keep that directory private and retain it for subsequent deployments.

Open the `UI_URL` printed by the hook, sign in as the deploying user, open
**Comparison sources** and import documents you are authorized to process.
Documents are not bundled or automatically imported. Hosted storage is separate
from local data. Verify that anonymous and unapproved users cannot access either
application. Retrieve the URLs later with `azd env get-value UI_URL` and
`azd env get-value API_URL`.

### Configuration

Set these before provisioning; unset values use the defaults in Bicep:

| azd environment variable | Default / purpose |
| --- | --- |
| `AZURE_LOCATION` | `northeurope`; base infrastructure and Container Apps region |
| `REGULATORY_WORKBENCH_PROFILE` | `dev`; `prod` increases replicas and logging limits |
| `AZURE_FOUNDRY_LOCATION` | `swedencentral`; independent Foundry account/project/model region |
| `AZURE_AI_MODEL_NAME` | `gpt-5.4-mini` |
| `AZURE_AI_MODEL_VERSION` | `2026-03-17`; model versions are pinned without automatic upgrades |
| `REGULATORY_WORKBENCH_ALLOWED_OBJECT_ID` | Deploying user's tenant object ID; override to approve a different single user |

For example, `azd env set REGULATORY_WORKBENCH_PROFILE prod` selects production
sizing. Generation and embedding deployment capacities default to 10 units each;
adjust the numeric `modelCapacity` and `embeddingCapacity` values in
`deployments\main.parameters.json` to match your subscription's quota. Capacity
units depend on the model. If Azure reports unavailable models, retired versions
or insufficient quota, select a supported model/version/region or request quota;
the deployment does not silently switch models.

The generation model is used by both workbench agents and live regulatory
analysis. Its deployment name follows `AZURE_AI_MODEL_NAME`; the embeddings model
remains `text-embedding-3-small`. Changing the generation model/version and
reprovisioning refreshes the pinned agent versions. If an older model deployment
already exists, incremental Bicep deployment does not remove it automatically.

The foundation keeps Storage public access disabled and wires
`deployments\modules\network.bicep` automatically. Both apps run in a
VNet-integrated Consumption environment and reach Blob Storage through a private
endpoint and `privatelink.blob.core.windows.net` DNS. Application code keeps using
the normal Blob URL and managed identity; DNS selects the private endpoint.
The Entra-protected API/UI URLs remain publicly reachable, so no VPN is required
to use the workbench. Bootstrap uses authenticated public Foundry/Search
endpoints and does not read Blob data. The apps upload extracted passages into
Search indexes, so no Search-to-Blob shared private link or Search tier upgrade
is needed.

Key Vault public access is also disabled; the deployment uses ARM secret creation
and the trusted Microsoft services allowance for ARM secret references. No local
connection to the vault's data-plane endpoint is required. Private Link and
private DNS add usage charges; the network does not add a NAT Gateway, VPN
gateway or dedicated compute profile. Direct Blob access from a developer
machine or Storage Explorer requires approved connectivity to the VNet; use
the workbench UI for ordinary document operations.
Organizations requiring private endpoints for Foundry/Search, fully private
ingress or a Network Security Perimeter must adapt and review that additional
networking.
The `prod` profile changes sizing, not production certification.

### Updates and recovery

Use `azd deploy` for code-only releases, or `azd deploy api` / `azd deploy ui`
for one service. Use `azd provision` for infrastructure changes, or `azd up`
for both. Reprovisioning discovers the currently deployed images rather than
rolling back to initial tags. Unchanged bootstrap configuration reuses the pinned
agent versions; model/project/Search changes initialize a new set of versions.
To deliberately rerun bootstrap after changing prompts or index definitions,
remove `.azure\<environment-name>\bootstrap-state.json` before provisioning.

Run `azd provision` periodically to renew the Entra credential when it has fewer
than seven days remaining, including after expiration. Application deployment
always follows credential initialization, including when Bicep recreates a
deleted registration and generates a new client ID. Credentials are appended
rather than destructively resetting unrelated credentials. Remove obsolete
credentials through your approved Entra administration process.

Hooks bound waits and stop on failures. They do not automatically repeat
ambiguous writes. If interrupted or timed out, inspect the named ARM deployment,
ACR run, Entra registration/credentials and Key Vault secret before rerunning.
Initial ACR builds use `--no-logs` to obtain structured run metadata, with a
900-second remote build limit and a 17-minute hook deadline that includes the
CLI wait. The hook prints the unique image tag before starting and checks the
reported run's status before using its image. Missing run metadata stops with
an explicit recovery message, never an automatic replacement build.
First deployment can build each image twice: once to create Container Apps with
real images, then through azd's normal service deployment.

If an older hook stops in `build_image` with `'NoneType' object is not
subscriptable`, the CLI returned no run metadata; that does not mean the remote
build failed. Inspect the registry's task runs and output images first.
After confirming the run is finished, rerun `azd up --environment
<environment-name>` from the updated checkout. Keep the existing resource group
and bootstrap state; current credentials and pinned agent versions are reused.
If no apps were deployed yet, the retry builds new initial images rather than
automatically adopting an image from the interrupted run.

If an older hook fails on Key Vault HTTPS access or `ForbiddenByConnection`
after infrastructure succeeds, keep the existing environment. A tenant policy
can disable vault public access even when an earlier template requested it.
The current hook reads secret metadata through ARM and creates the credential
secret with `deployments\authentication-secret.bicep`; it does not enable public
access, disable TLS verification, or require a policy exemption.
[Key Vault network restrictions](https://learn.microsoft.com/azure/key-vault/general/network-security)
do not block ARM secret deployment. After checking for interrupted credential
writes, rerun `azd up --environment <environment-name>` from the updated checkout.
This also applies configured model changes. If credential deployment fails,
inspect the `authentication-secret` resource-group deployment before rerunning.

If Storage access fails with a public-network restriction, the current foundation
provisions the Blob private endpoint, private DNS and VNet-integrated hosting;
do not enable Storage public access or exempt its network policy.
[Storage private endpoints](https://learn.microsoft.com/azure/storage/common/storage-private-endpoints)
work with public access disabled. The foundation exports
`AZURE_CONTAINER_APPS_ENVIRONMENT_NAME` and `AZURE_CONTAINER_APPS_ENVIRONMENT_ID`;
application deployment and Entra callback URLs use the new environment.

[Container Apps network type cannot be changed after creation](https://learn.microsoft.com/azure/container-apps/networking).
For a partial deployment that has no API/UI apps yet, rerunning `azd up` can keep
the existing Storage/Foundry/registry/Key Vault resources and create
`cae-net-<foundation-name>` alongside the old empty `cae-<foundation-name>`
environment. The old environment is retained, not deleted automatically.
Inspect it and confirm it has no apps before retiring it separately. If API/UI
apps already run in the legacy environment, the preprovision hook stops before
changing their images or callbacks: deploy a fresh azd environment/resource group,
migrate retained data, and retire the old deployment through an approved process.

If Container Apps provisioning fails with `AKSCapacityHeavyUsage`, choose another
base region. An existing Storage account, registry, managed identity or Container
Apps environment cannot be relocated by changing `AZURE_LOCATION`; after a
partial deployment, use a fresh azd environment and resource group:

```powershell
azd env new regwork-ne
azd env set AZURE_SUBSCRIPTION_ID "<subscription-id>"
azd env set AZURE_LOCATION northeurope
azd env set AZURE_FOUNDRY_LOCATION swedencentral
azd up
```

Current capacity is not guaranteed in any region. The template serializes Foundry
project, generation-model and embedding-model creation to avoid overlapping
account writes. A `RequestConflict` can still indicate another deployment is
active; inspect its state and wait for it to finish before retrying. Do not run
multiple provisioning operations against the same environment concurrently.

Failed deployments can leave billable resources behind. After checking for data
you need to retain, clean up the old environment separately with
`azd down --environment regwork-dev`; do not delete the new environment. Follow
the Entra and soft-delete cleanup considerations below.

`azd down` removes the ARM resources after confirmation. The tenant-level Entra
application and service principal are Graph resources and must be removed
separately through Entra administration using the generated
`REGULATORY_WORKBENCH_AUTH_CLIENT_ID`,
`REGULATORY_WORKBENCH_AUTH_APPLICATION_OBJECT_ID` and
`REGULATORY_WORKBENCH_AUTH_SERVICE_PRINCIPAL_OBJECT_ID` recorded in the azd
environment. Deleting an application removes its home-tenant service principal.
Key Vault soft deletion retains a deleted vault for seven days; recreating the same
environment during that period requires restoring the vault or using a new name.

For direct Azure CLI deployments, `foundation.bicep`, `dev.bicepparam`,
`prod.bicepparam` and `applications.example.bicepparam` remain available. The
foundation now provisions Foundry instead of reusing an existing account. That
manual path still requires running the equivalent authentication, bootstrap and
image-build steps; use its `environmentName` output for
`AZURE_CONTAINER_APPS_ENVIRONMENT_NAME`. `azd up` is the complete automated path.
