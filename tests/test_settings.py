from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from regulatory_poc.config.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_offline_defaults_are_valid(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
            settings.validate()
            self.assertEqual("offline", settings.agent_mode)
            self.assertEqual("local", settings.search_mode)
            self.assertEqual("", settings.foundry_project_endpoint)

    def test_foundry_mode_requires_explicit_project_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {"POC_AGENT_MODE": "foundry", "AZURE_AI_MODEL_DEPLOYMENT_NAME": "test-model"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "FOUNDRY_PROJECT_ENDPOINT"):
                Settings.from_env().validate()

    def test_foundry_mode_requires_model_deployment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "POC_AGENT_MODE": "foundry",
                "FOUNDRY_PROJECT_ENDPOINT": "https://example.test/api/projects/project",
                "AZURE_AI_MODEL_DEPLOYMENT_NAME": "",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "AZURE_AI_MODEL_DEPLOYMENT_NAME"):
                Settings.from_env().validate()


if __name__ == "__main__":
    unittest.main()
