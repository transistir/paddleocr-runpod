from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "deploy.py"
SPEC = importlib.util.spec_from_file_location("deploy", MODULE_PATH)
assert SPEC and SPEC.loader
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


class DeployTests(unittest.TestCase):
    def test_rejects_latest_or_invalid_digest(self) -> None:
        with self.assertRaises(SystemExit):
            DEPLOY.validate_image_reference("ghcr.io/example/worker:latest")
        with self.assertRaises(SystemExit):
            DEPLOY.validate_image_reference("ghcr.io/example/worker@sha256:bad")
        DEPLOY.validate_image_reference("ghcr.io/example/worker:3.0.0")
        DEPLOY.validate_image_reference("ghcr.io/example/worker@sha256:" + "a" * 64)

    def test_deployment_image_prefers_digest_and_falls_back_to_build_tag(self) -> None:
        digest = "ghcr.io/example/worker@sha256:" + "b" * 64
        with patch.dict(
            os.environ,
            {"DOCKER_IMAGE": "ghcr.io/example/worker:3.0.0", "RUNPOD_IMAGE": digest},
            clear=True,
        ):
            self.assertEqual(DEPLOY.deployment_image(), digest)
        with patch.dict(
            os.environ,
            {"DOCKER_IMAGE": "ghcr.io/example/worker:3.0.0"},
            clear=True,
        ):
            self.assertEqual(DEPLOY.deployment_image(), "ghcr.io/example/worker:3.0.0")

    def test_v2_endpoint_uses_gpu_pools_and_shared_container_config(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ALLOWED_SOURCE_HOSTS": "files.example.org",
                "RUNPOD_GPU_POOLS": "AMPERE_24,ADA_24",
                "RUNPOD_WORKERS_MIN": "0",
                "RUNPOD_WORKERS_MAX": "2",
            },
            clear=True,
        ):
            endpoint = DEPLOY.build_endpoint_body("ghcr.io/x/y:3.0.0", "endpoint")
            template = DEPLOY.build_template_body("ghcr.io/x/y:3.0.0", "template")
        self.assertNotIn("type", endpoint)
        self.assertEqual(endpoint["gpu"], {"pools": ["AMPERE_24", "ADA_24"], "count": 1})
        self.assertEqual(endpoint["flashboot"], "OFF")
        self.assertEqual(endpoint["image"], template["image"])
        self.assertEqual(endpoint["env"], template["env"])
        self.assertNotIn("templateId", endpoint)

    def test_rejects_unknown_gpu_pool(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"ALLOWED_SOURCE_HOSTS": "files.example.org", "RUNPOD_GPU_POOLS": "MADE_UP"},
                clear=True,
            ),
            self.assertRaises(SystemExit),
        ):
            DEPLOY.build_endpoint_body("ghcr.io/x/y:3.0.0", "endpoint")

    def test_runtime_env_requires_allowlist_and_matches_defaults(self) -> None:
        with patch.dict(os.environ, {"ALLOWED_SOURCE_HOSTS": "files.example.org"}, clear=True):
            values = DEPLOY.runtime_env()
        self.assertEqual(values["USE_SEAL_RECOGNITION"], "false")
        self.assertEqual(values["MAX_REDIRECTS"], "0")
        self.assertEqual(values["MAX_PDF_PAGE_EDGE_POINTS"], "3600")
        self.assertEqual(values["ALLOWED_SOURCE_HOSTS"], "files.example.org")
        self.assertEqual(values["RUNPOD_LOG_LEVEL"], "ERROR")

    def test_rejects_existing_load_balancing_endpoint(self) -> None:
        with self.assertRaises(SystemExit):
            DEPLOY.validate_existing_endpoint_type({"type": "LOAD_BALANCING"})
        DEPLOY.validate_existing_endpoint_type({"type": "QUEUE_BASED"})
        with self.assertRaises(SystemExit):
            DEPLOY.validate_existing_endpoint_type({})

    def test_extracts_and_evaluates_rollout(self) -> None:
        rollout = {
            "workersOnLatest": 1,
            "workersTotal": 2,
            "inProgress": True,
        }
        self.assertEqual(DEPLOY.extract_rollout({"data": {"rollout": rollout}}), rollout)
        self.assertFalse(DEPLOY.rollout_is_complete(rollout))
        self.assertTrue(
            DEPLOY.rollout_is_complete(
                {"workersOnLatest": 2, "workersTotal": 2, "inProgress": False}
            )
        )
        self.assertFalse(
            DEPLOY.rollout_is_complete(
                {"workersOnLatest": 0, "workersTotal": 0, "inProgress": True}
            )
        )
        self.assertTrue(
            DEPLOY.rollout_is_complete(
                {"workersOnLatest": 0, "workersTotal": 0, "inProgress": False}
            )
        )

    def test_subset_comparison(self) -> None:
        self.assertTrue(DEPLOY.subset_equal({"a": 1, "b": 2}, {"a": 1}))
        self.assertFalse(DEPLOY.subset_equal({"a": 2}, {"a": 1}))


if __name__ == "__main__":
    unittest.main()
