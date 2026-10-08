from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from azure.core.exceptions import HttpResponseError


SPEC = importlib.util.spec_from_file_location(
    "bootstrap", Path(__file__).resolve().parents[1] / "scripts" / "bootstrap.py",
)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_access_probe_retries_reads_before_any_bootstrap_writes(self):
        forbidden = HttpResponseError(message="Role not propagated")
        forbidden.status_code = 403
        indexes, project = Mock(), Mock()
        indexes.list_index_names.side_effect = [forbidden, []]
        project.agents.list.return_value = []
        with patch.object(bootstrap.time, "sleep"):
            bootstrap.wait_for_access(indexes, project)
        self.assertEqual(2, indexes.list_index_names.call_count)
        indexes.create_or_update_index.assert_not_called()
        project.agents.create_version.assert_not_called()

    def test_access_probe_surfaces_non_authorization_errors(self):
        indexes, project = Mock(), Mock()
        indexes.list_index_names.side_effect = HttpResponseError(message="Service unavailable")
        with self.assertRaises(HttpResponseError):
            bootstrap.wait_for_access(indexes, project)
        project.agents.list.assert_not_called()

    def test_missing_configuration_fails_before_azure_access(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(bootstrap, "DefaultAzureCredential") as credential:
            with self.assertRaisesRegex(ValueError, "FOUNDRY_PROJECT_ENDPOINT"):
                bootstrap.main()
            credential.assert_not_called()

    def test_configured_model_and_returned_versions_are_persisted(self):
        environment = {
            "POC_AGENT_MODE": "foundry",
            "FOUNDRY_PROJECT_ENDPOINT": "https://example.test/api/projects/project",
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "custom-model",
            "AZURE_AI_SEARCH_ENDPOINT": "https://example.test",
            "AZURE_AI_EMBEDDING_ENDPOINT": "https://example.test",
            "AZURE_AI_EMBEDDING_MODEL": "custom-embedding",
            "POC_MODEL_VERSION": "custom-version",
            "POC_AGENT_NAME": "custom-analysis",
            "POC_CHAT_AGENT_NAME": "custom-chat",
        }
        project = Mock()
        project.agents.create_version.side_effect = [
            SimpleNamespace(version="4"), SimpleNamespace(version="7"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / ".azure" / "agent-versions.json"
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(bootstrap, "DefaultAzureCredential"), \
                    patch.object(bootstrap, "SearchIndexClient"), \
                    patch.object(bootstrap, "create_index") as create_index, \
                    patch.object(bootstrap, "AIProjectClient", return_value=project), \
                    patch.object(bootstrap, "Path", return_value=output), \
                    redirect_stdout(io.StringIO()):
                bootstrap.main()
            self.assertEqual(
                {"POC_AGENT_VERSION": "4", "POC_CHAT_AGENT_VERSION": "7"},
                json.loads(output.read_text(encoding="utf-8")),
            )
        self.assertEqual(["fidelity-sources", "fidelity-reports"],
                         [call.args[1] for call in create_index.call_args_list])
        for call in create_index.call_args_list:
            self.assertEqual("custom-embedding", call.kwargs["embedding_deployment"])
            self.assertEqual("https://example.test", call.kwargs["embedding_endpoint"])
        calls = project.agents.create_version.call_args_list
        self.assertEqual(["custom-analysis", "custom-chat"],
                         [call.kwargs["agent_name"] for call in calls])
        self.assertTrue(all(call.kwargs["definition"].model == "custom-model" for call in calls))


if __name__ == "__main__":
    unittest.main()
