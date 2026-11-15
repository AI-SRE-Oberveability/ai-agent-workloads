"""Exact-SHA private GHCR publication; no Azure or Kubernetes operations."""

import argparse
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from secret_delivery import private_directory, synthetic_env, validate_contract

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "AI-SRE-Oberveability/ai-agent-workloads"
REPOSITORY_ID = "1437529826"
IMAGE = "ghcr.io/ai-sre-oberveability/ai-agent-workloads"


def guard(env):
    if (
        env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or env.get("GITHUB_REPOSITORY") != REPOSITORY
        or env.get("GITHUB_REPOSITORY_ID") != REPOSITORY_ID
    ):
        raise ValueError("Only the owned manual workflow is accepted")
    if (
        env.get("GITHUB_ACTOR") != "javedkumail"
        or env.get("TRIGGERING_ACTOR") != "javedkumail"
        or env.get("GITHUB_REF")
        not in {"refs/heads/main", "refs/heads/feature/image-secret-delivery"}
    ):
        raise ValueError("Unapproved actor/ref")
    if (
        env.get("POC_IMAGE_PUBLICATION_ENABLED") != "true"
        or env.get("AZURE_DEPLOYMENT_ENABLED", "false") != "false"
    ):
        raise ValueError("Publication gate closed or Azure flag inconsistent")
    source = env.get("REQUESTED_SOURCE_SHA", "")
    workflow = env.get("GITHUB_SHA", "")
    if any(
        not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40
        for value in [source, workflow]
    ):
        raise ValueError("Immutable nonzero source/workflow commits required")
    if (
        source != env.get("POC_IMAGE_SOURCE_SHA")
        or workflow != env.get("POC_IMAGE_WORKFLOW_SHA")
        or workflow != env.get("WORKFLOW_SHA")
    ):
        raise ValueError("Source/workflow differs from administrator allowlist")
    if not re.fullmatch(r"[1-9][0-9]*", env.get("GITHUB_RUN_ID", "")) or not re.fullmatch(
        r"[1-9][0-9]*", env.get("GITHUB_RUN_ATTEMPT", "")
    ):
        raise ValueError("Workflow run identity required")
    return source, workflow


def api(path, absent=False):
    token = os.environ.get("GITHUB_TOKEN", "")
    if (
        not token
        or not path.startswith("/repos/" + REPOSITORY + "/")
        and not path.startswith("/orgs/AI-SRE-Oberveability/packages/container/ai-agent-workloads")
    ):
        raise ValueError("Fixed GitHub metadata scope/token required")
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-platform-private-publication",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        if absent and error.code == 404:
            return None
        raise ValueError("GitHub metadata request failed: " + str(error.code)) from None
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Metadata response exceeds bound")
    return json.loads(raw)


def preflight():
    source, workflow = guard(os.environ)
    validate_contract(ROOT / "contracts/secret-delivery.json")
    if api("/repos/" + REPOSITORY + "/branches/main")["commit"]["sha"] != source:
        raise ValueError("Requested image source is not current approved main")
    runs = api("/repos/" + REPOSITORY + "/actions/runs?head_sha=" + source + "&per_page=30")[
        "workflow_runs"
    ]
    runs = [
        r
        for r in runs
        if r["head_sha"] == source
        and r["event"] == "push"
        and r["head_branch"] == "main"
        and r["name"] == "CI"
    ]
    if not runs or any(r["conclusion"] != "success" for r in runs):
        raise ValueError("Exact main CI success required before registry access")
    package = api("/orgs/AI-SRE-Oberveability/packages/container/ai-agent-workloads", absent=True)
    if package and package["visibility"] != "private":
        raise ValueError("Existing package must already be private")
    return source, workflow


def docker(args, env, input_text=None, allow_failure=False):
    child = {k: v for k, v in env.items() if k != "GITHUB_TOKEN"}
    result = subprocess.run(
        ["docker", *args],
        env=child,
        input=input_text,
        capture_output=True,
        text=not isinstance(input_text, bytes),
        encoding=None if isinstance(input_text, bytes) else "utf-8",
        timeout=600,
        check=False,
    )
    if len(result.stdout) + len(result.stderr) > 8 * 1024 * 1024:
        raise ValueError("Docker output exceeds bound")
    if result.returncode and not allow_failure:
        raise ValueError("Docker step failed: " + args[0])
    stdout = result.stdout.decode("utf-8") if isinstance(result.stdout, bytes) else result.stdout
    stderr = result.stderr.decode("utf-8") if isinstance(result.stderr, bytes) else result.stderr
    return stdout + stderr if args[0] == "logs" else stdout


PROBE = """import json, os, sys, urllib.request, urllib.error
request = json.load(sys.stdin)
mode = request['mode']
headers = {'Content-Type': 'application/json'}
body = None if mode == 'health' else {
    'question': 'How do I apply for repair assistance?',
    'scenario': 'server_error' if mode == 'fault' else 'healthy'
}
if mode in {'auth', 'fault', 'old'}:
    headers['X-Lab-Key'] = request['old_key'] if mode == 'old' else os.environ['LAB_API_KEY']
url = 'http://127.0.0.1:8080' + ('/healthz' if mode == 'health' else '/v1/ask')
req = urllib.request.Request(
    url, data=json.dumps(body).encode() if body is not None else None, headers=headers
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    response = opener.open(req, timeout=3)
except urllib.error.HTTPError as error:
    response = error
with response:
    raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('Bound exceeded')
    body = json.loads(raw)
    citations = body.get('citations', [])
    print(json.dumps({
        'status': response.code, 'model_mode': body.get('model_mode'),
        'outcome': body.get('outcome'),
        'citation_ok': bool(citations and citations[0].get('document_id') == 'SYN-REPAIR-001')
    }))
"""


def probe(container, mode, env, old_key=None):
    payload = {"mode": mode}
    if old_key is not None:
        payload["old_key"] = old_key
    data = docker(
        ["exec", "--interactive", container, "python", "-c", PROBE],
        env,
        input_text=json.dumps(payload),
    )
    result = json.loads(data)
    if (
        set(result) != {"status", "model_mode", "outcome", "citation_ok"}
        or result["model_mode"] not in {None, "deterministic-mock"}
        or result["outcome"] not in {None, "success"}
        or not isinstance(result["citation_ok"], bool)
        or not isinstance(result["status"], int)
    ):
        raise ValueError("Only sanitized probe result fields accepted")
    return result


def build_context(source_path, source):
    data = subprocess.check_output(
        [
            "git",
            "-C",
            str(source_path),
            "archive",
            "--format=tar",
            source,
            "Dockerfile",
            "requirements.lock.txt",
            "src",
        ],
        timeout=30,
    )
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Approved source build context exceeds bound")
    return data


def replay(reference, source, env, directory):
    network = "poc-image-" + env["GITHUB_RUN_ID"] + "-" + env["GITHUB_RUN_ATTEMPT"]
    docker(["network", "create", "--internal", network], env)
    containers = []
    logs = []
    keys = []

    def stop():
        for container in reversed(containers):
            logs.append(docker(["logs", container], env, allow_failure=True))
            docker(["rm", "--force", container], env, allow_failure=True)
        containers.clear()

    def start(key, phase):
        phase_dir = private_directory(directory / phase)
        file = synthetic_env(phase_dir, key, source)
        owned = {}
        for component in ["retrieval", "agent"]:
            args = [
                "run",
                "--detach",
                "--network",
                network,
                "--network-alias",
                component,
                "--read-only",
                "--user",
                "10001:10001",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16777216",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "128",
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--env-file",
                str(file),
                reference,
                "python",
                "-m",
                "uvicorn",
                "ai_lab." + component + ":create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                "8080",
                "--no-access-log",
                "--log-level",
                "warning",
            ]
            container = docker(args, env).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", container):
                raise ValueError("Unexpected owned container ID")
            containers.append(container)
            owned[component] = container
            ready = False
            for _ in range(60):
                try:
                    health = probe(container, "health", env)
                    if health["status"] == 200 and health["model_mode"] == "deterministic-mock":
                        ready = True
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.25)
            if not ready:
                raise ValueError("Owned replay service not ready")
        return owned["agent"]

    try:
        first = secrets.token_urlsafe(32)
        keys.append(first)
        agent = start(first, "initial")
        if probe(agent, "unauthorized", env)["status"] != 401:
            raise ValueError("Missing-key rejection failed")
        result = probe(agent, "auth", env)
        if result["status"] != 200 or result["outcome"] != "success" or not result["citation_ok"]:
            raise ValueError("Pulled digest did not reproduce synthetic citation")
        if probe(agent, "fault", env)["status"] != 403:
            raise ValueError("Faults must remain disabled")
        stop()
        second = secrets.token_urlsafe(32)
        keys.append(second)
        agent = start(second, "rotated")
        rotated = probe(agent, "auth", env)
        if (
            probe(agent, "old", env, old_key=first)["status"] != 401
            or rotated["status"] != 200
            or rotated["outcome"] != "success"
            or not rotated["citation_ok"]
        ):
            raise ValueError("Coordinated rotation/old-key rejection failed")
        stop()
        if any(key in log for key in keys for log in logs):
            raise ValueError("Synthetic key appeared in container logs")
        return {
            "mode": "pulled_digest_private_docker_network",
            "health_and_auth": True,
            "synthetic_citation": True,
            "faults_disabled": True,
            "coordinated_rotation": True,
            "old_key_rejected": True,
            "keys_absent_from_captured_logs": True,
            "kubernetes_secret_delivery_proved": False,
            "telemetry_backend_ingestion_proved": False,
        }
    finally:
        stop()
        docker(["network", "rm", network], env, allow_failure=True)


def publish(source_path, output):
    source, workflow = preflight()
    source_path = source_path.resolve()
    if not source_path.is_relative_to(ROOT) or source_path == ROOT:
        raise ValueError("Separate checked-out source directory required")
    actual = (
        subprocess.check_output(["git", "-C", str(source_path), "rev-parse", "HEAD"], timeout=30)
        .decode()
        .strip()
    )
    if (
        actual != source
        or subprocess.check_output(
            ["git", "-C", str(source_path), "status", "--porcelain"], timeout=30
        ).strip()
    ):
        raise ValueError("Build source differs from approved immutable commit")
    temp_root = private_directory(Path(os.environ["RUNNER_TEMP"]))
    with tempfile.TemporaryDirectory(dir=temp_root) as temp:
        directory = private_directory(Path(temp))
        config = private_directory(directory / "docker-auth")
        env = {**os.environ, "DOCKER_CONFIG": str(config)}
        tag = (
            IMAGE
            + ":run-"
            + env["GITHUB_RUN_ID"]
            + "-"
            + env["GITHUB_RUN_ATTEMPT"]
            + "-sha-"
            + source[:12]
        )
        docker(
            [
                "build",
                "--pull",
                "--label",
                "org.opencontainers.image.source=https://github.com/" + REPOSITORY,
                "--label",
                "org.opencontainers.image.revision=" + source,
                "--tag",
                tag,
                "-",
            ],
            env,
            input_text=build_context(source_path, source),
        )
        docker(
            ["login", "ghcr.io", "--username", "javedkumail", "--password-stdin"],
            env,
            input_text=env["GITHUB_TOKEN"] + "\n",
        )
        try:
            docker(["push", tag], env)
            built = json.loads(docker(["image", "inspect", tag], env))[0]
            refs = [
                r
                for r in built.get("RepoDigests", [])
                if re.fullmatch(re.escape(IMAGE) + r"@sha256:[0-9a-f]{64}", r)
            ]
            if len(refs) != 1:
                raise ValueError("Unique observed registry digest required")
            reference = refs[0]
            package = api("/orgs/AI-SRE-Oberveability/packages/container/ai-agent-workloads")
            if (
                package["visibility"] != "private"
                or str((package.get("repository") or {}).get("id")) != REPOSITORY_ID
            ):
                raise ValueError("Published package privacy/repository association failed")
            docker(["pull", reference], env)
            image = json.loads(docker(["image", "inspect", reference], env))[0]
            labels = image["Config"]["Labels"]
            if (
                image["Os"] != "linux"
                or image["Architecture"] != "amd64"
                or labels.get("org.opencontainers.image.revision") != source
                or labels.get("org.opencontainers.image.source")
                != "https://github.com/" + REPOSITORY
            ):
                raise ValueError("Pulled image platform/source labels mismatch")
            smoke = replay(reference, source, env, directory)
            receipt = {
                "schema_version": 1,
                "status": "private_image_published_digest_pulled_synthetic_secret_replay_passed",
                "repository": REPOSITORY,
                "repository_id": int(REPOSITORY_ID),
                "source_commit": source,
                "workflow_commit": workflow,
                "run_id": int(env["GITHUB_RUN_ID"]),
                "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
                "image": reference,
                "discovery_tag": tag,
                "package_visibility": "private",
                "package_repository_id": package["repository"]["id"],
                "runtime_platform": "linux/amd64",
                "source_oci_labels_verified": True,
                "signed_attestation_proved": False,
                "vulnerability_acceptance_proved": False,
                "replay": smoke,
                "secret_values_or_hashes_in_receipt": False,
                "azure_deployment_enabled": False,
                "kubernetes_operations": False,
            }
            output = output.resolve()
            if output.parent != temp_root:
                raise ValueError("Only sanitized runner-temp receipt output accepted")
            output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
            print(
                "Private image/digest and synthetic Secret replay passed; sanitized receipt saved"
            )
        finally:
            docker(["logout", "ghcr.io"], env, allow_failure=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["preflight", "publish"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "preflight":
            preflight()
            print("Manual publication source/workflow/main/private-package guards passed")
        elif args.source and args.output:
            publish(args.source, args.output)
        else:
            raise ValueError("Source/output required")
    except Exception as error:
        raise SystemExit(
            "Publication stopped: " + type(error).__name__ + "; " + str(error)[:160]
        ) from None


if __name__ == "__main__":
    main()
