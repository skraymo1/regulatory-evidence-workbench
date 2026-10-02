from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from regulatory_poc.ui import api


class ApiTests(unittest.TestCase):
    def test_health_and_validation_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "POC_AGENT_MODE": "offline",
                    "POC_SEARCH_MODE": "local",
                    "POC_INDEX_PATH": f"{directory}/index.json",
                },
                clear=True,
            ):
                api._runtime.cache_clear()
                with TestClient(api.app) as client:
                    health = client.get("/health")
                    invalid = client.post(
                        "/comparisons",
                        json={
                            "question": "Compare requirements",
                            "document_a_id": "same",
                            "document_b_id": "same",
                        },
                    )

        self.assertEqual(200, health.status_code)
        self.assertEqual("ok", health.json()["status"])
        self.assertEqual(422, invalid.status_code)
        self.assertIn("different language editions", invalid.json()["detail"])


if __name__ == "__main__":
    unittest.main()
