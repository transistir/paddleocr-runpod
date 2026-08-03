from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test.py"
SPEC = importlib.util.spec_from_file_location("smoke_test", MODULE_PATH)
assert SPEC and SPEC.loader
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class SmokeTestScriptTests(unittest.TestCase):
    def test_builds_bounded_async_job_payload(self) -> None:
        payload = SMOKE.build_job_payload(
            "https://files.example.org/document.pdf?signature=redacted",
            1_800_000,
            7_200_000,
        )
        self.assertEqual(payload["policy"]["executionTimeout"], 1_800_000)
        self.assertEqual(payload["policy"]["ttl"], 7_200_000)
        self.assertEqual(
            payload["input"],
            {"file_url": "https://files.example.org/document.pdf?signature=redacted"},
        )

    def test_rejects_ttl_shorter_than_execution_timeout(self) -> None:
        with self.assertRaises(SystemExit):
            SMOKE.build_job_payload(
                "https://files.example.org/document.pdf",
                1_800_000,
                1_000,
            )


if __name__ == "__main__":
    unittest.main()
