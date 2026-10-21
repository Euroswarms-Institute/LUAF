"""Smoke tests for RapidAPI bundle generation and canonical model."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from luaf.publishing.model import canonical_from_designer_payload, get_publish_target, rapid_openapi_spec
from luaf.publishing.rapid import write_rapid_bundle


def _minimal_payload() -> dict:
    code = "\n".join(
        [
            "import argparse",
            "def main() -> None:",
            "    p = argparse.ArgumentParser()",
            "    p.add_argument('--task', default='ok')",
            "    args = p.parse_args()",
            "    print(args.task)",
            'if __name__ == "__main__":',
            "    main()",
        ]
    )
    return {
        "name": "Test API Agent",
        "ticker": "TEST",
        "description": "Unit test agent for Rapid bundle.",
        "agent": code,
        "useCases": [
            {"title": "Run task", "description": "Invoke with a task string."},
            {"title": "Health", "description": "Check service."},
            {"title": "Batch", "description": "Process batch."},
        ],
        "tags": "test,api,python",
        "requirements": [
            {"package": "requests", "installation": "pip install requests"},
            {"package": "loguru", "installation": "pip install loguru"},
        ],
        "language": "python",
        "is_free": True,
    }


class TestCanonicalAndOpenAPI(unittest.TestCase):
    def test_canonical_from_payload(self) -> None:
        c = canonical_from_designer_payload(_minimal_payload())
        self.assertEqual(c.name, "Test API Agent")
        self.assertEqual(c.ticker, "TEST")
        self.assertIn("argparse", c.agent_code)

    def test_openapi_spec_shape(self) -> None:
        spec = rapid_openapi_spec("https://api.example.com", "T", "D")
        self.assertEqual(spec["openapi"], "3.0.3")
        self.assertIn("/health", spec["paths"])
        self.assertIn("/run", spec["paths"])


class TestRapidBundleWrite(unittest.TestCase):
    def test_write_bundle_files(self) -> None:
        canonical = canonical_from_designer_payload(_minimal_payload())
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "bundle"
            write_rapid_bundle(canonical, out, placeholder_base_url="https://test.example")
            self.assertTrue((out / "generated_agent.py").is_file())
            self.assertTrue((out / "app.py").is_file())
            self.assertTrue((out / "Dockerfile").is_file())
            self.assertTrue((out / "openapi.json").is_file())
            self.assertTrue((out / "rapid_listing.json").is_file())
            self.assertTrue((out / "ASSISTED_PUBLISH.md").is_file())
            spec = json.loads((out / "openapi.json").read_text(encoding="utf-8"))
            self.assertEqual(spec["servers"][0]["url"], "https://test.example")


class TestPublishTarget(unittest.TestCase):
    def test_default_swarms(self) -> None:
        import os

        old = os.environ.get("LUAF_PUBLISH_TARGET")
        try:
            os.environ.pop("LUAF_PUBLISH_TARGET", None)
            self.assertEqual(get_publish_target(), "swarms")
        finally:
            if old is not None:
                os.environ["LUAF_PUBLISH_TARGET"] = old
            elif "LUAF_PUBLISH_TARGET" in os.environ:
                os.environ.pop("LUAF_PUBLISH_TARGET", None)

    def test_rapidapi_aliases(self) -> None:
        import os

        for v in ("rapidapi", "rapid", "rapid_api"):
            os.environ["LUAF_PUBLISH_TARGET"] = v
            try:
                self.assertEqual(get_publish_target(), "rapidapi")
            finally:
                os.environ.pop("LUAF_PUBLISH_TARGET", None)


if __name__ == "__main__":
    unittest.main()
