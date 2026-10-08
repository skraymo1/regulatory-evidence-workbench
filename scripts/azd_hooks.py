"""azd initialization: resources stay in Bicep; credentials and data-plane work run here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = {
    "Microsoft.App", "Microsoft.CognitiveServices", "Microsoft.ContainerRegistry",
    "Microsoft.KeyVault", "Microsoft.ManagedIdentity", "Microsoft.OperationalInsights",
    "Microsoft.Network", "Microsoft.Search", "Microsoft.Storage",
}
SECRET_NAME = "entra-client-secret"
ARM_ENDPOINT = "https://management.azure.com"


def run(tool, *args, timeout=120, capture=True, env=None, input=None, sensitive=False):
    executable = shutil.which(tool)
    if not executable:
        raise RuntimeError(f"Install {tool} before running azd up.")
    try:
        result = subprocess.run(
            [executable, *map(str, args)], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None, timeout=timeout, input=input,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{tool} {args[0]} timed out. Check its remote state before rerunning; "
            "the operation may have succeeded."
        ) from None
    if result.returncode:
        if sensitive:
            raise RuntimeError(f"{tool} {args[0]} failed; sensitive response output was withheld.")
        raise RuntimeError(f"{tool} {args[0]} failed: {result.stderr or 'see command output'}")
    return result.stdout.strip() if capture else ""


def az(*args, timeout=120, input=None, sensitive=False):
    text = run(
        "az", *args, "--only-show-errors", "--output", "json",
        timeout=timeout, input=input, sensitive=sensitive,
    )
    return json.loads(text) if text else None


def save_env(key, value):
    run("azd", "env", "set", key, value, "--environment", required("AZURE_ENV_NAME"))
    os.environ[key] = str(value)


def load_env():
    args = ["env", "get-values", "--output", "json", "--no-prompt"]
    if os.environ.get("AZURE_ENV_NAME"):
        args.extend(("--environment", os.environ["AZURE_ENV_NAME"]))
    values = json.loads(run("azd", *args))
    os.environ.update(values)
    return values


def required(key):
    value = os.environ.get(key, "").strip()
    if not value:
        raise ValueError(f"Missing {key}; run azd provision before this hook.")
    return value


def auth_enabled():
    value = os.environ.get("AUTH_ENABLED", "true").strip().lower()
    if value not in ("true", "false"):
        raise ValueError("AUTH_ENABLED must be true or false.")
    return value == "true"


def register_providers():
    def states():
        return {
            item["namespace"]: item["registrationState"]
            for item in az("provider", "list")
            if item["namespace"] in PROVIDERS
        }
    current = states()
    for namespace in sorted(PROVIDERS):
        if current.get(namespace) != "Registered":
            az("provider", "register", "--namespace", namespace)
    deadline = time.monotonic() + 300
    previous = None
    while True:
        current = states()
        if current != previous:
            print(f"Resource provider states: {json.dumps(current, sort_keys=True)}", flush=True)
            previous = current
        if all(current.get(name) == "Registered" for name in PROVIDERS):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Provider registration is incomplete; inspect az provider list.")
        time.sleep(10)


def preprovision():
    load_env()
    if not auth_enabled():
        print(
            "WARNING: No-auth deployment exposes shared documents and processing endpoints "
            "publicly. Use non-sensitive demo data only.", flush=True,
        )
    name = required("AZURE_ENV_NAME")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,15}", name):
        raise ValueError("Use a 2-16 character environment name: lowercase letters, digits and hyphens.")
    subscription = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if subscription:
        az("account", "set", "--subscription", subscription)
    account = az("account", "show")
    user = az("ad", "signed-in-user", "show")
    principal = os.environ.get("AZURE_PRINCIPAL_ID")
    if principal and principal != user["id"]:
        raise ValueError("Sign in to az and azd as the same user in the subscription tenant.")
    save_env("AZURE_SUBSCRIPTION_ID", account["id"])
    save_env("AZURE_TENANT_ID", account["tenantId"])
    save_env("AZURE_PRINCIPAL_ID", user["id"])
    save_env("AZURE_LOCATION", os.environ.get("AZURE_LOCATION") or "northeurope")
    save_env("AZURE_FOUNDRY_LOCATION", os.environ.get("AZURE_FOUNDRY_LOCATION") or "swedencentral")
    register_providers()
    group = f"{name}"
    deployed = {}
    if az("group", "exists", "--name", group):
        for app in az("containerapp", "list", "--resource-group", group):
            service = (app.get("tags") or {}).get("azd-service-name")
            if service in ("api", "ui"):
                if service in deployed:
                    raise RuntimeError(f"Multiple Container Apps tagged for {service} in {group}.")
                environment_id = app["properties"].get("environmentId") or \
                    app["properties"].get("managedEnvironmentId")
                if not environment_id:
                    raise RuntimeError(f"Cannot determine the environment of the {service} Container App.")
                if not environment_id.rsplit("/", 1)[-1].startswith("cae-net-"):
                    raise RuntimeError(
                        f"The {service} Container App in {group} uses the legacy environment. "
                        "Container Apps cannot move between environments. Use a fresh azd "
                        "environment/resource group for private networking, then migrate data "
                        "and retire old resources separately; no apps were deleted."
                    )
                deployed[service] = app
    for service in ("api", "ui"):
        app = deployed.get(service)
        image = app["properties"]["template"]["containers"][0]["image"] if app else ""
        save_env(f"SERVICE_{service.upper()}_IMAGE", image)
    if deployed and not all(os.environ.get(key) for key in ("POC_AGENT_VERSION", "POC_CHAT_AGENT_VERSION")):
        app = next(iter(deployed.values()))
        variables = {
            item["name"]: item.get("value", "")
            for item in app["properties"]["template"]["containers"][0].get("env", [])
        }
        for key in ("POC_AGENT_VERSION", "POC_CHAT_AGENT_VERSION"):
            save_env(key, variables.get(key, ""))


def ensure_auth_secret():
    vault_id = required("AZURE_KEY_VAULT_ID")
    url = f"{ARM_ENDPOINT}{vault_id}/secrets?api-version=2025-05-01"
    secret = None
    while url:
        page = az("rest", "--method", "get", "--url", url, timeout=60)
        secret = next(
            (item for item in page["value"] if item["id"].rsplit("/", 1)[-1] == SECRET_NAME), None,
        )
        if secret:
            break
        url = page.get("nextLink")
    client_id = required("REGULATORY_WORKBENCH_AUTH_CLIENT_ID")
    if secret and (secret.get("tags") or {}).get("appId") == client_id and \
            secret["properties"].get("attributes", {}).get("enabled", True) and \
            secret["properties"].get("attributes", {}).get("exp", 0) > time.time() + 7 * 86400:
        return
    template = az(
        "bicep", "build", "--file", ROOT / "deployments" / "authentication-secret.bicep", "--stdout",
    )
    if template["parameters"]["authClientSecret"]["type"].lower() != "securestring":
        raise RuntimeError("Credential deployment must declare authClientSecret as a secure parameter.")
    expires = datetime.now(timezone.utc) + timedelta(days=180)
    credential = az(
        "ad", "app", "credential", "reset", "--id", client_id, "--append",
        "--display-name", f"regulatory-workbench-{uuid4().hex[:8]}",
        "--end-date", expires.isoformat(),
    )
    deployment = "authentication-secret"
    group_id = vault_id.rsplit("/providers/", 1)[0]
    url = (
        f"{ARM_ENDPOINT}{group_id}/providers/Microsoft.Resources/deployments/"
        f"{deployment}?api-version=2025-04-01"
    )
    body = {"properties": {
        "mode": "Incremental", "template": template,
        "parameters": {
            "vaultName": {"value": vault_id.rsplit("/", 1)[-1]},
            "secretName": {"value": SECRET_NAME}, "clientId": {"value": client_id},
            "authClientSecret": {"value": credential["password"]},
            "expires": {"value": int(expires.timestamp())},
        },
    }}
    # stdin keeps the secure ARM parameter out of arguments, files, and azd's .env.
    try:
        result = az(
            "rest", "--method", "put", "--url", url, "--body", "@-",
            input=json.dumps(body), sensitive=True, timeout=60,
        )
        deadline = time.monotonic() + 300
        previous = None
        while True:
            properties = result["properties"]
            status = properties["provisioningState"]
            if status != previous:
                print(f"Credential deployment {deployment}: {status}", flush=True)
                previous = status
            if status == "Succeeded":
                return
            if status in ("Failed", "Canceled") or time.monotonic() >= deadline:
                code = properties.get("error", {}).get("code", "see deployment operations")
                raise RuntimeError(f"Credential deployment {deployment} is {status} ({code}).")
            time.sleep(10)
            result = az("rest", "--method", "get", "--url", url, timeout=60)
    except RuntimeError as error:
        raise RuntimeError(
            f"Saving the Entra credential failed. Inspect deployment {deployment}, the vault "
            "secret and app credentials before rerunning; no write was retried."
        ) from error


def bootstrap(environment_dir):
    keys = (
        "FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_MODEL_DEPLOYMENT_NAME", "POC_MODEL_VERSION",
        "AZURE_AI_SEARCH_ENDPOINT", "AZURE_AI_EMBEDDING_ENDPOINT", "AZURE_AI_EMBEDDING_MODEL",
    )
    fingerprint = {key: required(key) for key in keys}
    state = environment_dir / "bootstrap-state.json"
    if state.exists():
        previous = json.loads(state.read_text(encoding="utf-8"))
        if previous["configuration"] == fingerprint:
            for key, value in previous["versions"].items():
                save_env(key, value)
            return
    executable = Path(sys.executable)
    probe = [str(executable), "-c", "import azure.ai.projects, azure.search.documents, regulatory_poc"]
    result = subprocess.run(probe, cwd=ROOT, capture_output=True, text=True, timeout=30)
    if result.returncode:
        if "ModuleNotFoundError" not in result.stderr:
            raise RuntimeError(f"Bootstrap dependency probe failed: {result.stderr}")
        venv = environment_dir / "bootstrap-venv"
        run(str(executable), "-m", "venv", venv, timeout=120)
        executable = venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        run(str(executable), "-m", "pip", "install", "--pre", "-e", ROOT, timeout=600, capture=False)
    output = environment_dir / "agent-versions.json"
    environment = {
        **os.environ, "POC_AGENT_MODE": "foundry", "POC_SEARCH_MODE": "azure",
        "POC_SOURCE_KB": "", "POC_REPORT_KB": "", "AZURE_CLIENT_ID": "",
        "PYTHONPATH": str(ROOT / "src"),
        "POC_AGENT_NAME": "security-requirements-analysis", "POC_CHAT_AGENT_NAME": "fidelity-evidence-chat",
    }
    run(
        str(executable), ROOT / "scripts" / "bootstrap.py", "--output", output,
        "--wait-for-rbac", timeout=600, capture=False, env=environment,
    )
    versions = json.loads(output.read_text(encoding="utf-8"))
    for key, value in versions.items():
        save_env(key, value)
    state.write_text(json.dumps({"configuration": fingerprint, "versions": versions}, indent=2), encoding="utf-8")


def build_image(service):
    registry = required("AZURE_CONTAINER_REGISTRY_NAME")
    tag = f"initial-{uuid4().hex}"
    image = f"regulatory-{service}:{tag}"
    deadline = time.monotonic() + 1020
    print(f"Building initial {service} image: {image}", flush=True)
    build = az(
        "acr", "build", "--registry", registry, "--image", image,
        "--file", f"Dockerfile.{service}", "--platform", "linux", "--timeout", "900",
        "--no-logs", ROOT, timeout=1020,
    )
    run_id = build.get("runId") if isinstance(build, dict) else None
    if not isinstance(run_id, str) or not run_id.strip():
        raise RuntimeError(
            f"ACR did not return a run ID for {image}. Inspect task runs in {registry} "
            "before rerunning; the build may have succeeded and was not retried."
        )
    print(f"Initial {service} image build: {run_id}", flush=True)
    previous = None
    while True:
        result = az("acr", "task", "show-run", "--registry", registry, "--run-id", run_id, timeout=60)
        status = result.get("status") if isinstance(result, dict) else None
        if not isinstance(status, str) or not status.strip():
            raise RuntimeError(
                f"ACR did not return a status for run {run_id} in {registry}. "
                "Inspect the run before rerunning; the build was not retried."
            )
        if status != previous:
            print(f"ACR run {run_id}: {status}", flush=True)
            previous = status
        if status == "Succeeded":
            return f"{required('AZURE_CONTAINER_REGISTRY_ENDPOINT')}/{image}"
        if status in ("Failed", "Canceled", "Error", "Timeout") or time.monotonic() >= deadline:
            raise RuntimeError(f"Inspect ACR run {run_id} ({status}); the build was not retried.")
        time.sleep(10)


def application_parameters():
    mapping = {
        "foundationName": "REGULATORY_WORKBENCH_FOUNDATION_NAME",
        "containerAppsEnvironmentName": "AZURE_CONTAINER_APPS_ENVIRONMENT_NAME",
        "location": "AZURE_LOCATION", "profile": "REGULATORY_WORKBENCH_PROFILE",
        "apiImage": "SERVICE_API_IMAGE", "uiImage": "SERVICE_UI_IMAGE",
        "projectEndpoint": "FOUNDRY_PROJECT_ENDPOINT",
        "embeddingEndpoint": "AZURE_AI_EMBEDDING_ENDPOINT",
        "embeddingDeployment": "AZURE_AI_EMBEDDING_MODEL",
        "modelDeployment": "AZURE_AI_MODEL_DEPLOYMENT_NAME", "modelVersion": "POC_MODEL_VERSION",
        "agentVersion": "POC_AGENT_VERSION", "chatAgentVersion": "POC_CHAT_AGENT_VERSION",
        "tenantId": "AZURE_TENANT_ID",
    }
    parameters = {key: {"value": required(value)} for key, value in mapping.items()}
    enabled = auth_enabled()
    parameters["authEnabled"] = {"value": enabled}
    if enabled:
        parameters["authClientId"] = {"value": required("REGULATORY_WORKBENCH_AUTH_CLIENT_ID")}
        parameters["allowedObjectId"] = {"value": required("REGULATORY_WORKBENCH_ALLOWED_OBJECT_ID")}
        parameters["authClientSecret"] = {
            "reference": {"keyVault": {"id": required("AZURE_KEY_VAULT_ID")}, "secretName": SECRET_NAME},
        }
    else:
        for key in ("authClientId", "allowedObjectId", "authClientSecret"):
            parameters[key] = {"value": ""}
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0", "parameters": parameters,
    }


def deploy_applications(parameters_file):
    group = required("AZURE_RESOURCE_GROUP")
    deployment = "applications"
    az(
        "deployment", "group", "create", "--name", deployment, "--resource-group", group,
        "--template-file", ROOT / "deployments" / "applications.bicep",
        "--parameters", f"@{parameters_file}", "--no-wait",
    )
    deadline = time.monotonic() + 900
    previous = None
    while True:
        result = az("deployment", "group", "show", "--name", deployment, "--resource-group", group)
        properties = result["properties"]
        operations = az("deployment", "operation", "group", "list", "--name", deployment, "--resource-group", group)
        progress = [
            (item["properties"].get("targetResource"), item["properties"].get("provisioningState"))
            for item in operations
        ]
        if progress != previous:
            print(f"Application deployment operations: {json.dumps(progress)}", flush=True)
            previous = progress
        status = properties["provisioningState"]
        if status == "Succeeded":
            return properties["outputs"]
        if status in ("Failed", "Canceled") or time.monotonic() >= deadline:
            raise RuntimeError(
                f"Deployment {deployment} is {status}: {json.dumps(properties.get('error', {}))}. "
                "Inspect the deployment before rerunning; it was not retried."
            )
        time.sleep(10)


def postprovision():
    load_env()
    environment_dir = ROOT / ".azure" / required("AZURE_ENV_NAME")
    environment_dir.mkdir(parents=True, exist_ok=True)
    if auth_enabled():
        print("Initializing Entra credential in Key Vault.", flush=True)
        ensure_auth_secret()
    else:
        print("Skipping Entra credential setup for explicit no-auth deployment.", flush=True)
    bootstrap(environment_dir)
    for service in ("api", "ui"):
        key = f"SERVICE_{service.upper()}_IMAGE"
        if not os.environ.get(key):
            save_env(key, build_image(service))
    parameters_file = environment_dir / "applications.parameters.json"
    parameters_file.write_text(json.dumps(application_parameters(), indent=2), encoding="utf-8")
    outputs = deploy_applications(parameters_file)
    urls = [outputs[key]["value"] for key in ("apiUrl", "uiUrl")]
    for service, url in zip(("API", "UI"), urls):
        save_env(f"{service}_URL", url)
        print(f"{service}: {url}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("preprovision", "postprovision"):
        raise SystemExit("Usage: azd_hooks.py preprovision|postprovision")
    {"preprovision": preprovision, "postprovision": postprovision}[sys.argv[1]]()
