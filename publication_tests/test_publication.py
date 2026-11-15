"""Synthetic dispatch/Secret failures; no registry, cluster or Azure access."""

import copy
import importlib
import io
import json
import os
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
publication = importlib.import_module("publication")
delivery = importlib.import_module("secret_delivery")
VALID = {
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REPOSITORY": "AI-SRE-Oberveability/ai-agent-workloads",
    "GITHUB_REPOSITORY_ID": "1437529826",
    "GITHUB_ACTOR": "javedkumail",
    "TRIGGERING_ACTOR": "javedkumail",
    "GITHUB_REF": "refs/heads/feature/image-secret-delivery",
    "POC_IMAGE_PUBLICATION_ENABLED": "true",
    "POC_IMAGE_SOURCE_SHA": "b" * 40,
    "REQUESTED_SOURCE_SHA": "b" * 40,
    "POC_IMAGE_WORKFLOW_SHA": "a" * 40,
    "GITHUB_SHA": "a" * 40,
    "WORKFLOW_SHA": "a" * 40,
    "GITHUB_RUN_ID": "123",
    "GITHUB_RUN_ATTEMPT": "1",
}


class DispatchGuards(unittest.TestCase):
    def test_allowlisted_manual_identity(self):
        self.assertEqual(publication.guard(VALID), ("b" * 40, "a" * 40))

    def test_guard_rejects_unapproved_dispatches(self):
        cases = [
            {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_REPOSITORY": "someone/fork"},
            {"GITHUB_REPOSITORY_ID": "1"},
            {"GITHUB_ACTOR": "someone"},
            {"TRIGGERING_ACTOR": "someone"},
            {"GITHUB_REF": "refs/heads/unreviewed"},
            {"POC_IMAGE_PUBLICATION_ENABLED": "false"},
            {"AZURE_DEPLOYMENT_ENABLED": "true"},
            {"REQUESTED_SOURCE_SHA": "main"},
            {"REQUESTED_SOURCE_SHA": "0" * 40, "POC_IMAGE_SOURCE_SHA": "0" * 40},
            {"GITHUB_SHA": "c" * 40},
            {"WORKFLOW_SHA": "c" * 40},
            {"REQUESTED_SOURCE_SHA": "c" * 40},
            {"GITHUB_RUN_ID": "bad;command"},
        ]
        for case in cases:
            with self.subTest(case=list(case)), self.assertRaises(ValueError):
                publication.guard({**VALID, **case})

    def metadata(self, current=True, passing=True, private=True):
        return [
            {"commit": {"sha": "b" * 40 if current else "c" * 40}},
            {
                "workflow_runs": [
                    {
                        "head_sha": "b" * 40,
                        "event": "push",
                        "head_branch": "main",
                        "name": "CI",
                        "conclusion": "success" if passing else "failure",
                    }
                ]
            },
            {"visibility": "private" if private else "public"},
        ]

    def test_main_ci_checked_before_registry(self):
        with (
            patch.dict(os.environ, VALID, clear=True),
            patch.object(publication, "api", side_effect=self.metadata()) as api,
        ):
            self.assertEqual(publication.preflight()[0], "b" * 40)
            self.assertEqual(api.call_count, 3)

    def test_nonmain_stops_before_package(self):
        with (
            patch.dict(os.environ, VALID, clear=True),
            patch.object(publication, "api", side_effect=self.metadata(current=False)) as api,
        ):
            with self.assertRaises(ValueError):
                publication.preflight()
            self.assertEqual(api.call_count, 1)

    def test_failed_ci_stops_before_package(self):
        with (
            patch.dict(os.environ, VALID, clear=True),
            patch.object(publication, "api", side_effect=self.metadata(passing=False)) as api,
        ):
            with self.assertRaises(ValueError):
                publication.preflight()
            self.assertEqual(api.call_count, 2)

    def test_public_existing_package_stops(self):
        with (
            patch.dict(os.environ, VALID, clear=True),
            patch.object(publication, "api", side_effect=self.metadata(private=False)),
        ):
            with self.assertRaises(ValueError):
                publication.preflight()

    def test_no_token_in_docker_child_env(self):
        result = subprocess.CompletedProcess([], 0, "ok", "")
        with patch.object(publication.subprocess, "run", return_value=result) as run:
            publication.docker(["version"], {"GITHUB_TOKEN": "ephemeral-test", "PATH": "test"})
            self.assertNotIn("GITHUB_TOKEN", run.call_args.kwargs["env"])

    def test_stderr_logs_are_checked(self):
        result = subprocess.CompletedProcess([], 0, "stdout", "stderr")
        with patch.object(publication.subprocess, "run", return_value=result):
            self.assertEqual(publication.docker(["logs", "owned"], {}), "stdoutstderr")

    def test_context_contains_only_committed_runtime_inputs(self):
        source = (
            subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).decode().strip()
        )
        data = publication.build_context(ROOT, source)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            names = archive.getnames()
        self.assertIn("Dockerfile", names)
        self.assertIn("requirements.lock.txt", names)
        self.assertTrue(
            all(
                n in {"Dockerfile", "requirements.lock.txt", "src"} or n.startswith("src/")
                for n in names
            )
        )
        self.assertFalse(any(".git" in n or ".env" in n for n in names))

    def test_old_key_probe_uses_stdin_not_arguments(self):
        key = secrets.token_urlsafe(32)
        safe = {"status": 401, "model_mode": None, "outcome": None, "citation_ok": False}
        with patch.object(publication, "docker", return_value=json.dumps(safe)) as docker:
            self.assertEqual(publication.probe("owned", "old", {}, old_key=key), safe)
            self.assertNotIn(key, str(docker.call_args.args))
            self.assertEqual(json.loads(docker.call_args.kwargs["input_text"])["old_key"], key)

    def test_probe_rejects_extra_content(self):
        unsafe = {
            "status": 200,
            "model_mode": None,
            "outcome": None,
            "citation_ok": False,
            "raw_body": "not-allowed",
        }
        with patch.object(publication, "docker", return_value=json.dumps(unsafe)):
            with self.assertRaises(ValueError):
                publication.probe("owned", "health", {})

    def test_embedded_probe_compiles(self):
        compile(publication.PROBE, "in-container-probe", "exec")


class SecretContract(unittest.TestCase):
    def setUp(self):
        cache = ROOT.parent / ".runtime"
        cache.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.contract = json.loads((ROOT / "contracts/secret-delivery.json").read_text())

    def test_no_values_contract(self):
        self.assertTrue(delivery.validate_contract(ROOT / "contracts/secret-delivery.json"))

    def test_contract_rejects_unsafe_delivery(self):
        mutations = [
            lambda d: d.update(secret_values_in_terraform_state=True),
            lambda d: d.update(stringData={"lab-api-key": "synthetic-forbidden"}),
            lambda d: d.update(api_key="synthetic-forbidden"),
            lambda d: d.update(azure_deployment_enabled=True),
            lambda d: d["future_csi"].update(volume_mount_required_for_sync=False),
            lambda d: d["rotation"].update(environment_reload="automatic"),
            lambda d: d["private_image_pull"].update(actions_token_reused_in_aks=True),
        ]
        for index, mutate in enumerate(mutations):
            value = copy.deepcopy(self.contract)
            mutate(value)
            path = self.path / "contract.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(index=index), self.assertRaises(ValueError):
                delivery.validate_contract(path)

    def test_private_env_and_no_overwrite(self):
        key = secrets.token_urlsafe(32)
        path = delivery.synthetic_env(self.path, key, "b" * 40)
        self.assertIn("LAB_API_KEY=" + key, path.read_text())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            delivery.synthetic_env(self.path, key, "b" * 40)

    def test_env_in_git_or_bare_repo_denied(self):
        for bare in [False, True]:
            directory = self.path / str(bare)
            directory.mkdir()
            if bare:
                (directory / "HEAD").write_text("ref: refs/heads/main")
                (directory / "objects").mkdir()
            else:
                (directory / ".git").mkdir()
            with self.subTest(bare=bare), self.assertRaises(ValueError):
                delivery.synthetic_env(directory, secrets.token_urlsafe(32), "b" * 40)

    def test_env_newline_injection_denied_without_value_in_error(self):
        key = secrets.token_urlsafe(32) + "\nBAD=1"
        with self.assertRaises(ValueError) as error:
            delivery.synthetic_env(self.path, key, "b" * 40)
        self.assertNotIn(key, str(error.exception))


if __name__ == "__main__":
    unittest.main()
