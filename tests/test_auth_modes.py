from __future__ import annotations

import base64
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import runpy
import types
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from regulatory_poc.config.settings import Settings
from regulatory_poc.ui import api


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("azd_hooks", ROOT / "scripts" / "azd_hooks.py")
assert SPEC is not None and SPEC.loader is not None
hooks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hooks)


class AuthModeTests(unittest.TestCase):
    def test_deployment_auth_setting_uses_short_name_and_defaults_secure(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(hooks.auth_enabled())
        for value in ("true", " TRUE ", "false", " FALSE "):
            with self.subTest(value=value), patch.dict(os.environ, {"AUTH_ENABLED": value}, clear=True):
                self.assertEqual(value.strip().lower() == "true", hooks.auth_enabled())
        for value in ("", "0", "no", "disabled"):
            with self.subTest(value=value), patch.dict(os.environ, {"AUTH_ENABLED": value}, clear=True):
                with self.assertRaisesRegex(ValueError, "AUTH_ENABLED must be true or false"):
                    hooks.auth_enabled()

    def test_deployment_auth_setting_is_wired_to_bicep(self):
        parameters = json.loads((ROOT / "deployments" / "main.parameters.json").read_text())
        self.assertEqual({"value": "${AUTH_ENABLED=true}"}, parameters["parameters"]["authEnabled"])
        self.assertIn(
            "output AUTH_ENABLED bool = authEnabled",
            (ROOT / "deployments" / "main.bicep").read_text(),
        )

    def test_runtime_defaults_to_authentication_and_rejects_invalid_switches(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(Settings.from_env().auth_enabled)
        for value in ("", "0", "no", "disabled"):
            with self.subTest(value=value), patch.dict(os.environ, {"POC_AUTH_ENABLED": value}, clear=True):
                with self.assertRaisesRegex(ValueError, "must be true or false"):
                    Settings.from_env()

    def test_hosted_no_auth_requires_explicit_switch_and_keeps_cloud_storage_requirements(self):
        with patch.dict(os.environ, {"CONTAINER_APP_NAME": "app"}, clear=True):
            settings = replace(
                Settings.from_env(), search_mode="azure", tenant_id="tenant",
                source_knowledge_base="sources-kb", report_knowledge_base="reports-kb",
                blob_account_url="https://storage.blob.core.windows.net", blob_container="documents",
                search_endpoint="https://search.search.windows.net",
                embedding_endpoint="https://foundry.openai.azure.com", embedding_model="embedding",
                agent_version="1", chat_agent_version="1", model_version="version",
            )
            with self.assertRaisesRegex(ValueError, "Entra account restriction"):
                settings.validate()
            replace(settings, allowed_oid="approved").validate()
            anonymous = replace(settings, auth_enabled=False)
            anonymous.validate()
            for changed in (
                replace(anonymous, tenant_id=""),
                replace(anonymous, search_mode="local"),
                replace(anonymous, source_knowledge_base=""),
                replace(anonymous, blob_account_url=""),
            ):
                with self.subTest(settings=changed), self.assertRaises(ValueError):
                    changed.validate()

    def test_api_mode_switch_allows_anonymous_then_restores_account_restriction(self):
        principal = base64.b64encode(json.dumps({
            "auth_typ": "aad", "claims": [
                {"typ": "oid", "val": "approved"}, {"typ": "tid", "val": "tenant"},
            ],
        }).encode()).decode()
        with patch.dict(os.environ, {
            "POC_ALLOWED_OID": "approved", "AZURE_TENANT_ID": "tenant",
        }, clear=True), patch.object(api, "_runtime") as runtime, TestClient(api.app) as client:
            runtime.side_effect = lambda: (Settings.from_env(), None, None, None)
            self.assertEqual(403, client.get("/health").status_code)
            self.assertEqual(200, client.get("/health", headers={"x-ms-client-principal": principal}).status_code)
            with patch.dict(os.environ, {"POC_AUTH_ENABLED": "false"}):
                self.assertEqual(200, client.get("/health").status_code)
                self.assertEqual(200, client.get("/health", headers={"x-ms-client-principal": "invalid"}).status_code)
            self.assertEqual(403, client.get("/health").status_code)

    def test_ui_mode_switch_warns_without_login_and_restores_login_gate(self):
        class Stopped(Exception):
            pass

        streamlit = types.SimpleNamespace(
            cache_resource=lambda function: function,
            context=types.SimpleNamespace(headers={}),
            set_page_config=Mock(), title=Mock(), warning=Mock(), error=Mock(),
            stop=Mock(side_effect=Stopped),
        )
        path = Path(__file__).resolve().parents[1] / "src" / "regulatory_poc" / "ui" / "app.py"
        with patch.dict(os.environ, {
            "POC_ALLOWED_OID": "approved", "AZURE_TENANT_ID": "tenant",
            "POC_AUTH_ENABLED": "false",
        }, clear=True), patch.dict("sys.modules", {"streamlit": streamlit}), \
                patch("regulatory_poc.runtime.container.build_search_repository", return_value=Mock()), \
                patch("regulatory_poc.ui.regulatory.regulatory_panel") as panel:
            runpy.run_path(str(path))
            panel.assert_called_once()
            self.assertFalse(panel.call_args.args[0].auth_enabled)
            streamlit.warning.assert_called_once()
            streamlit.stop.assert_not_called()
            with patch.dict(os.environ, {"POC_AUTH_ENABLED": "true"}):
                with self.assertRaises(Stopped):
                    runpy.run_path(str(path))
            self.assertEqual(1, panel.call_count)
            streamlit.error.assert_called_once_with("Access denied. Sign in with the approved Entra account.")


if __name__ == "__main__":
    unittest.main()
