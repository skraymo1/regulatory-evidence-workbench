from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "azd_hooks", Path(__file__).resolve().parents[1] / "scripts" / "azd_hooks.py",
)
assert SPEC is not None and SPEC.loader is not None
hooks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hooks)

ENVIRONMENT = {
    "AZURE_ENV_NAME": "regwork-dev",
    "AZURE_RESOURCE_GROUP": "rg-regwork-dev",
    "AZURE_LOCATION": "northeurope",
    "AZURE_FOUNDRY_LOCATION": "swedencentral",
    "AZURE_TENANT_ID": "tenant",
    "REGULATORY_WORKBENCH_FOUNDATION_NAME": "regwork-dev-abcdef",
    "REGULATORY_WORKBENCH_PROFILE": "dev",
    "REGULATORY_WORKBENCH_AUTH_CLIENT_ID": "client",
    "REGULATORY_WORKBENCH_ALLOWED_OBJECT_ID": "user",
    "AZURE_KEY_VAULT_ID": "/subscriptions/sub/resourceGroups/group/providers/Microsoft.KeyVault/vaults/vault",
    "AZURE_KEY_VAULT_ENDPOINT": "https://vault.vault.azure.net/",
    "AZURE_CONTAINER_REGISTRY_NAME": "registry",
    "AZURE_CONTAINER_REGISTRY_ENDPOINT": "registry.azurecr.io",
    "AZURE_CONTAINER_APPS_ENVIRONMENT_NAME": "cae-net-regwork-dev-abcdef",
    "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.services.ai.azure.com/api/projects/project",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.4-mini",
    "POC_MODEL_VERSION": "2026-03-17",
    "AZURE_AI_EMBEDDING_ENDPOINT": "https://foundry.openai.azure.com/",
    "AZURE_AI_EMBEDDING_MODEL": "text-embedding-3-small",
    "AZURE_AI_SEARCH_ENDPOINT": "https://search.search.windows.net",
    "POC_AGENT_VERSION": "4",
    "POC_CHAT_AGENT_VERSION": "7",
}
SECRET_TEMPLATE = {"parameters": {"authClientSecret": {"type": "securestring"}}}


def save_env(key, value):
    os.environ[key] = value


def secret_metadata(client_id="client", expires=None, enabled=True):
    return {
        "id": f"{ENVIRONMENT['AZURE_KEY_VAULT_ID']}/secrets/entra-client-secret",
        "tags": {"appId": client_id},
        "properties": {"attributes": {
            "enabled": enabled, "exp": expires if expires is not None else time.time() + 30 * 86400,
        }},
    }


class AzdDeploymentTests(unittest.TestCase):
    def test_preprovision_defaults_base_and_foundry_to_independent_regions(self):
        with patch.dict(os.environ, {"AZURE_ENV_NAME": "regwork-ne"}, clear=True), \
                patch.object(hooks, "load_env"), \
                patch.object(hooks, "save_env", side_effect=save_env), \
                patch.object(hooks, "register_providers"), \
                patch.object(hooks, "az", side_effect=[
                    {"id": "subscription", "tenantId": "tenant"}, {"id": "user"}, False,
                ]):
            hooks.preprovision()
            self.assertEqual("northeurope", os.environ["AZURE_LOCATION"])
            self.assertEqual("swedencentral", os.environ["AZURE_FOUNDRY_LOCATION"])

    def test_preprovision_preserves_explicit_region_overrides(self):
        with patch.dict(os.environ, {
            "AZURE_ENV_NAME": "regwork-ne", "AZURE_LOCATION": "eastus2",
            "AZURE_FOUNDRY_LOCATION": "westus3",
        }, clear=True), \
                patch.object(hooks, "load_env"), \
                patch.object(hooks, "save_env", side_effect=save_env), \
                patch.object(hooks, "register_providers"), \
                patch.object(hooks, "az", side_effect=[
                    {"id": "subscription", "tenantId": "tenant"}, {"id": "user"}, False,
                ]):
            hooks.preprovision()
            self.assertEqual("eastus2", os.environ["AZURE_LOCATION"])
            self.assertEqual("westus3", os.environ["AZURE_FOUNDRY_LOCATION"])

    def test_hooks_use_selected_environment_not_workspace_default(self):
        with patch.dict(os.environ, {"AZURE_ENV_NAME": "staging"}, clear=True), \
                patch.object(hooks, "run", return_value='{"AZURE_ENV_NAME": "staging"}') as run:
            hooks.load_env()
            self.assertEqual(("--environment", "staging"), run.call_args.args[-2:])
            hooks.save_env("POC_AGENT_VERSION", "12")
            self.assertEqual(("--environment", "staging"), run.call_args.args[-2:])
            self.assertEqual("12", os.environ["POC_AGENT_VERSION"])

    def test_parameters_reference_vault_without_copying_secret(self):
        with patch.dict(os.environ, {
            **ENVIRONMENT, "SERVICE_API_IMAGE": "registry/api:tag",
            "SERVICE_UI_IMAGE": "registry/ui:tag", "UNRELATED_PASSWORD": "secret-value",
        }, clear=True):
            parameters = hooks.application_parameters()["parameters"]
        self.assertEqual(parameters["authClientSecret"], {
            "reference": {"keyVault": {"id": ENVIRONMENT["AZURE_KEY_VAULT_ID"]},
                          "secretName": "entra-client-secret"},
        })
        self.assertNotIn("secret-value", json.dumps(parameters))
        self.assertEqual("4", parameters["agentVersion"]["value"])
        self.assertEqual("user", parameters["allowedObjectId"]["value"])
        self.assertEqual("gpt-5.4-mini", parameters["modelDeployment"]["value"])
        self.assertEqual("2026-03-17", parameters["modelVersion"]["value"])
        self.assertEqual("cae-net-regwork-dev-abcdef", parameters["containerAppsEnvironmentName"]["value"])

    def test_missing_required_configuration_fails_explicitly(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "FOUNDATION_NAME"):
                hooks.application_parameters()

    def test_current_credential_is_reused_without_reading_its_value(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", return_value={"value": [secret_metadata()]}) as az:
            hooks.ensure_auth_secret()
        az.assert_called_once_with(
            "rest", "--method", "get", "--url",
            f"https://management.azure.com{ENVIRONMENT['AZURE_KEY_VAULT_ID']}/secrets?api-version=2025-05-01",
            timeout=60,
        )

    def test_secret_metadata_pagination_is_followed_without_credential_creation(self):
        next_url = "https://management.azure.com/vault/secrets?nextLink=next-page"
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", side_effect=[
                    {"value": [], "nextLink": next_url}, {"value": [secret_metadata()]},
                ]) as az:
            hooks.ensure_auth_secret()
        self.assertEqual(2, az.call_count)
        az.assert_called_with("rest", "--method", "get", "--url", next_url, timeout=60)

    def test_expired_or_disabled_credential_is_renewed_with_secure_arm_parameter(self):
        template = SECRET_TEMPLATE
        for metadata in (secret_metadata(expires=time.time() - 1), secret_metadata(enabled=False)):
            with self.subTest(metadata=metadata), \
                    patch.dict(os.environ, ENVIRONMENT, clear=True), \
                    patch.object(hooks, "az", side_effect=[
                        {"value": [metadata]}, template, {"password": "new-secret"},
                        {"properties": {"provisioningState": "Succeeded"}},
                    ]) as az:
                hooks.ensure_auth_secret()
            args = [call.args for call in az.call_args_list]
            self.assertNotIn("new-secret", str(args))
            self.assertEqual(("bicep", "build"), args[1][:2])
            self.assertIn("--append", args[2])
            self.assertEqual(("rest", "--method", "put"), args[3][:3])
            self.assertEqual(("--body", "@-"), args[3][-2:])
            self.assertTrue(az.call_args.kwargs["sensitive"])
            body = json.loads(az.call_args.kwargs["input"])["properties"]
            self.assertEqual(template, body["template"])
            self.assertEqual("Incremental", body["mode"])
            self.assertEqual("new-secret", body["parameters"]["authClientSecret"]["value"])
            self.assertEqual("client", body["parameters"]["clientId"]["value"])
            self.assertGreater(body["parameters"]["expires"]["value"], time.time() + 179 * 86400)

    def test_recreated_application_does_not_reuse_previous_registration_secret(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", side_effect=[
                    {"value": [secret_metadata(client_id="previous-client")]}, SECRET_TEMPLATE,
                    {"password": "new-secret"}, {"properties": {"provisioningState": "Succeeded"}},
                ]) as az:
            hooks.ensure_auth_secret()
        credential_args = az.call_args_list[2].args
        self.assertEqual("client", credential_args[credential_args.index("--id") + 1])
        body = json.loads(az.call_args.kwargs["input"])["properties"]
        self.assertEqual({"value": "client"}, body["parameters"]["clientId"])

    def test_metadata_transport_or_access_failure_does_not_create_credentials(self):
        for message in ("Connection reset by peer (WinError 10054)", "ForbiddenByConnection"):
            with self.subTest(message=message), \
                    patch.dict(os.environ, ENVIRONMENT, clear=True), \
                    patch.object(hooks, "az", side_effect=RuntimeError(message)) as az:
                with self.assertRaisesRegex(RuntimeError, message.split(" ")[0]):
                    hooks.ensure_auth_secret()
            self.assertEqual(1, az.call_count)
            self.assertEqual(("rest", "--method", "get"), az.call_args.args[:3])

    def test_template_compilation_failure_does_not_create_credentials(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", side_effect=[
                    {"value": []}, RuntimeError("Bicep compilation failed"),
                ]) as az:
            with self.assertRaisesRegex(RuntimeError, "compilation failed"):
                hooks.ensure_auth_secret()
        self.assertEqual(2, az.call_count)

    def test_insecure_template_does_not_create_credentials(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", side_effect=[
                    {"value": []}, {"parameters": {"authClientSecret": {"type": "string"}}},
                ]) as az:
            with self.assertRaisesRegex(RuntimeError, "secure parameter"):
                hooks.ensure_auth_secret()
        self.assertEqual(2, az.call_count)

    def test_ambiguous_secret_write_is_not_retried(self):
        for message in ("az rest timed out", "Connection reset by peer"):
            with self.subTest(message=message), \
                    patch.dict(os.environ, ENVIRONMENT, clear=True), \
                    patch.object(hooks, "az", side_effect=[
                        {"value": []}, SECRET_TEMPLATE, {"password": "new-secret"}, RuntimeError(message),
                    ]) as az:
                with self.assertRaisesRegex(RuntimeError, "no write was retried"):
                    hooks.ensure_auth_secret()
            self.assertEqual(4, az.call_count)
            self.assertEqual(1, sum(call.args[:3] == ("rest", "--method", "put")
                                    for call in az.call_args_list))

    def test_secret_deployment_waits_for_success_without_repeating_write(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", side_effect=[
                    {"value": []}, SECRET_TEMPLATE, {"password": "new-secret"},
                    {"properties": {"provisioningState": "Accepted"}},
                    {"properties": {"provisioningState": "Succeeded"}},
                ]) as az, \
                patch.object(hooks.time, "sleep"):
            hooks.ensure_auth_secret()
        self.assertEqual(("rest", "--method", "get"), az.call_args.args[:3])
        self.assertEqual(1, sum(call.args[:3] == ("rest", "--method", "put")
                                for call in az.call_args_list))
        self.assertEqual(az.call_args_list[3].args[4], az.call_args_list[4].args[4])

    def test_failed_secret_deployment_does_not_continue_or_repeat_write(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", side_effect=[
                    {"value": []}, SECRET_TEMPLATE, {"password": "new-secret"},
                    {"properties": {
                        "provisioningState": "Failed", "error": {"code": "AuthorizationFailed"},
                    }},
                ]) as az:
            with self.assertRaisesRegex(RuntimeError, "authentication-secret"):
                hooks.ensure_auth_secret()
        self.assertEqual(4, az.call_count)

    def test_secret_deployment_polling_has_a_bounded_runtime(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", side_effect=[
                    {"value": []}, SECRET_TEMPLATE, {"password": "new-secret"},
                    {"properties": {"provisioningState": "Accepted"}},
                ]) as az, \
                patch.object(hooks.time, "monotonic", side_effect=[0, 301]), \
                patch.object(hooks.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "no write was retried"):
                hooks.ensure_auth_secret()
        self.assertEqual(4, az.call_count)
        sleep.assert_not_called()

    def test_secret_poll_transport_failure_does_not_repeat_write(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", side_effect=[
                    {"value": []}, SECRET_TEMPLATE, {"password": "new-secret"},
                    {"properties": {"provisioningState": "Accepted"}},
                    RuntimeError("Connection reset by peer"),
                ]) as az, \
                patch.object(hooks.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "no write was retried"):
                hooks.ensure_auth_secret()
        self.assertEqual(1, sum(call.args[:3] == ("rest", "--method", "put")
                                for call in az.call_args_list))

    def test_preprovision_preserves_current_images_without_requiring_or_creating_auth_identity(self):
        def azure(*args, **kwargs):
            if args[:2] == ("account", "show"):
                return {"id": "subscription", "tenantId": "tenant"}
            if args[:3] == ("ad", "signed-in-user", "show"):
                return {"id": "user"}
            if args[:2] == ("group", "exists"):
                return True
            if args[:2] == ("containerapp", "list"):
                return [
                    {"tags": {"azd-service-name": service},
                     "properties": {
                         "environmentId": "/environments/cae-net-regwork-dev-abcdef",
                         "template": {"containers": [{"image": f"registry/{service}:current"}]},
                     }}
                    for service in ("api", "ui")
                ]
            self.fail(f"Unexpected Azure call: {args}")
        environment = {key: value for key, value in ENVIRONMENT.items()
                       if key != "REGULATORY_WORKBENCH_AUTH_CLIENT_ID"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(hooks, "load_env"), \
                patch.object(hooks, "save_env", side_effect=save_env), \
                patch.object(hooks, "register_providers"), \
                patch.object(hooks, "ensure_auth_secret") as ensure, \
                patch.object(hooks, "az", side_effect=azure):
            hooks.preprovision()
            self.assertEqual("registry/api:current", os.environ["SERVICE_API_IMAGE"])
            self.assertEqual("registry/ui:current", os.environ["SERVICE_UI_IMAGE"])
            self.assertNotIn("REGULATORY_WORKBENCH_AUTH_CLIENT_ID", os.environ)
        ensure.assert_not_called()

    def test_legacy_apps_stop_before_image_or_callback_changes(self):
        app = {
            "tags": {"azd-service-name": "api"},
            "properties": {
                "environmentId": "/environments/cae-regwork-dev-abcdef",
                "template": {"containers": [{"image": "registry/api:current"}]},
            },
        }
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "load_env"), \
                patch.object(hooks, "save_env", side_effect=save_env) as save, \
                patch.object(hooks, "register_providers"), \
                patch.object(hooks, "az", side_effect=[
                    {"id": "subscription", "tenantId": "tenant"}, {"id": "user"}, True, [app],
                ]):
            with self.assertRaisesRegex(RuntimeError, "cannot move between environments"):
                hooks.preprovision()
        self.assertFalse(any(call.args[0].startswith("SERVICE_") for call in save.call_args_list))

    def test_preprovision_registers_network_provider(self):
        self.assertIn("Microsoft.Network", hooks.PROVIDERS)

    def test_first_provision_initializes_credential_before_apps_and_uses_generated_auth_id(self):
        outputs = {"apiUrl": {"value": "https://api.test"}, "uiUrl": {"value": "https://ui.test"}}
        order = Mock()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "ROOT", Path(directory)), \
                patch.object(hooks, "load_env"), \
                patch.object(hooks, "save_env", side_effect=save_env), \
                patch.object(hooks, "ensure_auth_secret") as ensure, \
                patch.object(hooks, "bootstrap"), \
                patch.object(hooks, "build_image", side_effect=lambda service: f"registry/{service}:real") as build, \
                patch.object(hooks, "deploy_applications", return_value=outputs) as deploy, \
                patch.object(hooks, "az") as az:
            order.attach_mock(ensure, "credential")
            order.attach_mock(deploy, "applications")
            hooks.postprovision()
            parameters = json.loads(
                (Path(directory) / ".azure" / "regwork-dev" / "applications.parameters.json").read_text()
            )["parameters"]
        self.assertEqual(["api", "ui"], [call.args[0] for call in build.call_args_list])
        self.assertEqual("registry/api:real", parameters["apiImage"]["value"])
        self.assertEqual("registry/ui:real", parameters["uiImage"]["value"])
        self.assertEqual("client", parameters["authClientId"]["value"])
        self.assertEqual(["credential", "applications"], [call[0] for call in order.mock_calls])
        az.assert_not_called()

    def test_reprovision_does_not_rebuild_initial_images(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {
                    **ENVIRONMENT, "SERVICE_API_IMAGE": "registry/api:current",
                    "SERVICE_UI_IMAGE": "registry/ui:current",
                }, clear=True), \
                patch.object(hooks, "ROOT", Path(directory)), \
                patch.object(hooks, "load_env"), \
                patch.object(hooks, "save_env", side_effect=save_env), \
                patch.object(hooks, "ensure_auth_secret"), \
                patch.object(hooks, "bootstrap"), \
                patch.object(hooks, "build_image") as build, \
                patch.object(hooks, "deploy_applications", return_value={
                    "apiUrl": {"value": "https://api.test"}, "uiUrl": {"value": "https://ui.test"},
                }), \
                patch.object(hooks, "az"):
            hooks.postprovision()
        build.assert_not_called()

    def test_bootstrap_cache_is_environment_specific(self):
        configuration = {key: ENVIRONMENT[key] for key in (
            "FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_MODEL_DEPLOYMENT_NAME", "POC_MODEL_VERSION",
            "AZURE_AI_SEARCH_ENDPOINT", "AZURE_AI_EMBEDDING_ENDPOINT", "AZURE_AI_EMBEDDING_MODEL",
        )}
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "save_env") as save, \
                patch.object(hooks, "run") as run:
            path = Path(directory)
            (path / "bootstrap-state.json").write_text(json.dumps({
                "configuration": configuration,
                "versions": {"POC_AGENT_VERSION": "11", "POC_CHAT_AGENT_VERSION": "12"},
            }))
            hooks.bootstrap(path)
        run.assert_not_called()
        self.assertEqual([("POC_AGENT_VERSION", "11"), ("POC_CHAT_AGENT_VERSION", "12")],
                         [call.args for call in save.call_args_list])

    def test_model_upgrade_refreshes_cached_agent_versions(self):
        configuration = {key: ENVIRONMENT[key] for key in (
            "FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_MODEL_DEPLOYMENT_NAME", "POC_MODEL_VERSION",
            "AZURE_AI_SEARCH_ENDPOINT", "AZURE_AI_EMBEDDING_ENDPOINT", "AZURE_AI_EMBEDDING_MODEL",
        )}
        previous = {
            **configuration, "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-4.1",
            "POC_MODEL_VERSION": "2025-04-14",
        }

        def run_bootstrap(*args, **kwargs):
            output = Path(args[args.index("--output") + 1])
            output.write_text(json.dumps({
                "POC_AGENT_VERSION": "11", "POC_CHAT_AGENT_VERSION": "12",
            }), encoding="utf-8")
            return ""

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "save_env", side_effect=save_env), \
                patch.object(hooks.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="", stderr="",
                )), \
                patch.object(hooks, "run", side_effect=run_bootstrap) as run:
            path = Path(directory)
            state = path / "bootstrap-state.json"
            state.write_text(json.dumps({
                "configuration": previous,
                "versions": {"POC_AGENT_VERSION": "4", "POC_CHAT_AGENT_VERSION": "7"},
            }), encoding="utf-8")
            hooks.bootstrap(path)
            self.assertEqual("11", os.environ["POC_AGENT_VERSION"])
            self.assertEqual("12", os.environ["POC_CHAT_AGENT_VERSION"])
            self.assertEqual(configuration, json.loads(state.read_text())["configuration"])
        run.assert_called_once()

    def test_initial_build_requests_structured_metadata_with_bounded_wait(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "uuid4", return_value=Mock(hex="build-tag")), \
                patch.object(hooks, "az", side_effect=[
                    {"runId": "run1"}, {"status": "Succeeded"},
                ]) as az:
            image = hooks.build_image("api")
        self.assertEqual("registry.azurecr.io/regulatory-api:initial-build-tag", image)
        self.assertEqual(2, az.call_count)
        build = az.call_args_list[0]
        self.assertIn("--no-logs", build.args)
        self.assertNotIn("--no-wait", build.args)
        self.assertEqual(1020, build.kwargs["timeout"])
        self.assertEqual("900", build.args[build.args.index("--timeout") + 1])
        self.assertEqual(("acr", "task", "show-run"), az.call_args.args[:3])
        self.assertEqual("run1", az.call_args.args[-1])
        self.assertEqual(60, az.call_args.kwargs["timeout"])

    def test_missing_build_metadata_reports_possible_success_without_requeueing(self):
        for response in (None, {}, [], {"runId": None}, {"runId": ""}, {"runId": " "}, {"runId": 1}):
            with self.subTest(response=response), \
                    patch.dict(os.environ, ENVIRONMENT, clear=True), \
                    patch.object(hooks, "uuid4", return_value=Mock(hex="build-tag")), \
                    patch.object(hooks, "az", return_value=response) as az:
                with self.assertRaisesRegex(RuntimeError, "did not return a run ID") as raised:
                    hooks.build_image("api")
            self.assertIn("regulatory-api:initial-build-tag", str(raised.exception))
            self.assertIn("may have succeeded", str(raised.exception))
            self.assertIn("not retried", str(raised.exception))
            az.assert_called_once()

    def test_build_polling_returns_image_only_after_success(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "uuid4", return_value=Mock(hex="build-tag")), \
                patch.object(hooks, "az", side_effect=[
                    {"runId": "run2"}, {"status": "Queued"}, {"status": "Running"},
                    {"status": "Succeeded"},
                ]) as az, \
                patch.object(hooks.time, "sleep") as sleep:
            image = hooks.build_image("ui")
        self.assertEqual("registry.azurecr.io/regulatory-ui:initial-build-tag", image)
        self.assertEqual(2, sleep.call_count)
        self.assertEqual(1, sum(call.args[:2] == ("acr", "build") for call in az.call_args_list))

    def test_failed_remote_build_is_not_retried(self):
        for status in ("Failed", "Canceled", "Error", "Timeout"):
            with self.subTest(status=status), \
                    patch.dict(os.environ, ENVIRONMENT, clear=True), \
                    patch.object(hooks, "az", side_effect=[
                        {"runId": "run1"}, {"status": status},
                    ]) as az:
                with self.assertRaisesRegex(RuntimeError, f"run1 .*{status}.*not retried"):
                    hooks.build_image("api")
            self.assertEqual(2, az.call_count)

    def test_missing_run_status_reports_exact_run_without_requeueing(self):
        for response in (None, {}, {"status": None}, {"status": ""}, {"status": 1}):
            with self.subTest(response=response), \
                    patch.dict(os.environ, ENVIRONMENT, clear=True), \
                    patch.object(hooks, "az", side_effect=[{"runId": "run1"}, response]) as az:
                with self.assertRaisesRegex(RuntimeError, "did not return a status for run run1"):
                    hooks.build_image("api")
            self.assertEqual(2, az.call_count)

    def test_build_deadline_includes_queue_and_cli_wait(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks.time, "monotonic", side_effect=[0, 1021]), \
                patch.object(hooks, "az", side_effect=[
                    {"runId": "run1"}, {"status": "Running"},
                ]) as az, \
                patch.object(hooks.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "run1 .*Running.*not retried"):
                hooks.build_image("api")
        self.assertEqual(2, az.call_count)
        sleep.assert_not_called()

    def test_build_transport_timeout_is_not_retried(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(hooks, "az", side_effect=RuntimeError(
                    "az acr timed out. Check its remote state before rerunning."
                )) as az:
            with self.assertRaisesRegex(RuntimeError, "Check its remote state"):
                hooks.build_image("api")
        az.assert_called_once()

    def test_process_failure_does_not_disclose_sensitive_stdout(self):
        with patch.object(hooks.shutil, "which", return_value="az"), \
                patch.object(hooks.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["az"], 1, stdout="secret-value", stderr="permission denied",
                )):
            with self.assertRaisesRegex(RuntimeError, "permission denied") as raised:
                hooks.run("az", "ad")
        self.assertNotIn("secret-value", str(raised.exception))

    def test_sensitive_process_uses_stdin_and_never_reports_response_contents(self):
        body = '{"authClientSecret":"secret-value"}'
        with patch.object(hooks.shutil, "which", return_value="az"), \
                patch.object(hooks.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["az"], 1, stdout=body, stderr=f"Request failed: {body}",
                )) as run:
            with self.assertRaisesRegex(RuntimeError, "sensitive response output was withheld") as raised:
                hooks.run("az", "rest", "--body", "@-", input=body, sensitive=True)
        self.assertEqual(body, run.call_args.kwargs["input"])
        self.assertNotIn("secret-value", str(run.call_args.args))
        self.assertNotIn("secret-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
