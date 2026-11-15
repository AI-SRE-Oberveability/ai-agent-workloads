"""Offline chart checks with explicit render-only fixtures, never rollout acceptance."""

import argparse
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

import yaml
from chart_tools import DEFAULT_CACHE, private, renderer

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/ai-lab"
FIXTURE_IMAGE = (
    "example.invalid/ai-lab/render-fixture@sha256:"
    + hashlib.sha256(b"render-fixture-only-not-an-image").hexdigest()
)
FIXTURE_SECRET = "render-fixture-secret-ref"


def inspect(objects, profile, source):
    namespace = "ai-lab-" + profile
    expected = {
        ("v1", "ServiceAccount", "ai-agent"),
        ("v1", "ServiceAccount", "ai-retrieval"),
        ("v1", "Service", "agent"),
        ("v1", "Service", "retrieval"),
        ("apps/v1", "Deployment", "agent"),
        ("apps/v1", "Deployment", "retrieval"),
    }
    if (
        len(objects) != 6
        or {(o["apiVersion"], o["kind"], o["metadata"]["name"]) for o in objects} != expected
        or any(o["metadata"]["namespace"] != namespace for o in objects)
    ):
        raise ValueError("Six unique namespaced application-owned objects required")
    by_key = {(o["kind"], o["metadata"]["name"]): o for o in objects}
    for component in ["agent", "retrieval"]:
        labels = {"app.kubernetes.io/part-of": "ai-lab", "app.kubernetes.io/component": component}
        sa = by_key["ServiceAccount", "ai-" + component]
        if sa.get("automountServiceAccountToken") is not False:
            raise ValueError("Tokenless identity required")
        service = by_key["Service", component]["spec"]
        if service != {
            "type": "ClusterIP",
            "selector": labels,
            "ports": [{"name": "http", "port": 8080, "targetPort": "http"}],
        }:
            raise ValueError("Internal labelled application service required")
        deploy = by_key["Deployment", component]["spec"]
        pod = deploy["template"]["spec"]
        if (
            deploy["replicas"] != 1
            or deploy["selector"]["matchLabels"] != labels
            or deploy["template"]["metadata"]["labels"]
            != {**labels, "ai-lab.example/profile": profile}
        ):
            raise ValueError("Matching config-network labels required")
        if (
            pod.get("automountServiceAccountToken") is not False
            or pod["serviceAccountName"] != "ai-" + component
            or any(
                pod.get(k)
                for k in [
                    "hostNetwork",
                    "hostPID",
                    "hostIPC",
                    "initContainers",
                    "ephemeralContainers",
                ]
            )
        ):
            raise ValueError("Pod privilege or sidecar forbidden")
        if (
            pod["securityContext"]
            != {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            }
            or len(pod["containers"]) != 1
        ):
            raise ValueError("Restricted pod identity required")
        container = pod["containers"][0]
        if container["image"] != FIXTURE_IMAGE or container.get("envFrom") or container.get("args"):
            raise ValueError("Explicit isolated render fixture required")
        command = [
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
        if container["command"] != command:
            raise ValueError("Existing bounded service factory command required")
        env = [
            {
                "name": "LAB_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": FIXTURE_SECRET,
                        "key": "lab-api-key",
                        "optional": False,
                    }
                },
            },
            {"name": "ALLOW_FAULTS", "value": "0"},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            {"name": "PYTHONUNBUFFERED", "value": "1"},
            {"name": "RETRIEVAL_URL", "value": "http://retrieval:8080"},
            {
                "name": "OTEL_EXPORTER_OTLP_ENDPOINT",
                "value": "http://otel-collector.ai-observability.svc.cluster.local:4318",
            },
            {"name": "LAB_VERSION", "value": source},
        ]
        if container["env"] != env:
            raise ValueError("Secret reference/internal endpoints/fault bounds differ")
        if container["securityContext"] != {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        } or container["resources"] != {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "256Mi"},
        }:
            raise ValueError("Restricted bounded container required")
        if container["ports"] != [{"name": "http", "containerPort": 8080}] or any(
            container[k]["httpGet"] != {"path": "/healthz", "port": "http"}
            for k in ["startupProbe", "readinessProbe", "livenessProbe"]
        ):
            raise ValueError("Existing HTTP health route required")
        if container["volumeMounts"] != [{"name": "tmp", "mountPath": "/tmp"}] or pod[
            "volumes"
        ] != [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}}]:
            raise ValueError("Only bounded private memory tmp permitted")


def validate(cache=DEFAULT_CACHE, restore=False):
    cache = private(cache)
    binary = renderer(cache, restore)
    source = (
        subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], timeout=30)
        .decode()
        .strip()
    )
    env = {
        **os.environ,
        "HELM_CONFIG_HOME": str(cache / "app-config"),
        "HELM_CACHE_HOME": str(cache / "app-cache"),
        "HELM_DATA_HOME": str(cache / "app-data"),
    }

    def helm(*args):
        return subprocess.run(
            [str(binary), *args],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

    fixture = [
        "--set",
        "enabled=true",
        "--set-string",
        "image=" + FIXTURE_IMAGE,
        "--set-string",
        "sourceRevision=" + source,
        "--set-string",
        "existingSecretName=" + FIXTURE_SECRET,
    ]
    if helm("lint", str(CHART), "--strict", *fixture).returncode:
        raise ValueError("Application Helm lint failed")
    reports = {}
    positive = None
    for profile in ["disabled", "dev", "qa"]:
        extra = [] if profile == "disabled" else [*fixture, "--set", "profile=" + profile]
        result = helm(
            "template",
            "ai-lab",
            str(CHART),
            "--namespace",
            "ai-lab-" + ("dev" if profile == "disabled" else profile),
            "--kube-version",
            "1.35.0",
            *extra,
        )
        if result.returncode:
            raise ValueError("Application render failed")
        objects = [o for o in yaml.safe_load_all(result.stdout) if o]
        if profile == "disabled":
            if objects:
                raise ValueError("Disabled default must emit zero objects")
        else:
            inspect(objects, profile, source)
            positive = objects
        reports[profile] = {
            "objects": len(objects),
            "sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        }
    rejected = []
    cases = [
        ("missing_rollout_inputs", ["--set", "enabled=true"]),
        ("tag_only_image", [*fixture, "--set-string", "image=example.invalid/app:latest"]),
        ("zero_image", [*fixture, "--set-string", "image=example.invalid/app@sha256:" + "0" * 64]),
        ("zero_source", [*fixture, "--set-string", "sourceRevision=" + "0" * 40]),
        ("missing_secret", [*fixture, "--set-string", "existingSecretName="]),
        ("inline_key_option", [*fixture, "--set-string", "apiKey=not-allowed"]),
    ]
    for name, extra in cases:
        if helm("template", "negative", str(CHART), *extra).returncode == 0:
            raise ValueError("Unsafe/missing value accepted: " + name)
        rejected.append(name)
    for name in [
        "inline_secret",
        "public_service",
        "token_mount",
        "privilege",
        "external_tool",
        "faults_enabled",
        "namespace_escape",
        "duplicate_owner",
        "unsafe_tmp",
    ]:
        objects = copy.deepcopy(positive)
        deployment = next(o for o in objects if o["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        if name == "inline_secret":
            container["env"][0] = {"name": "LAB_API_KEY", "value": "forbidden-inline-test-value"}
        elif name == "public_service":
            next(o for o in objects if o["kind"] == "Service")["spec"]["type"] = "LoadBalancer"
        elif name == "token_mount":
            pod["automountServiceAccountToken"] = True
        elif name == "privilege":
            container["securityContext"]["allowPrivilegeEscalation"] = True
        elif name == "external_tool":
            next(e for e in container["env"] if e["name"] == "RETRIEVAL_URL")["value"] = (
                "https://example.invalid"
            )
        elif name == "faults_enabled":
            next(e for e in container["env"] if e["name"] == "ALLOW_FAULTS")["value"] = "1"
        elif name == "namespace_escape":
            objects[0]["metadata"]["namespace"] = "argocd"
        elif name == "duplicate_owner":
            objects.append(copy.deepcopy(objects[0]))
        else:
            pod["volumes"] = [{"name": "tmp", "hostPath": {"path": "/"}}]
        try:
            inspect(objects, "qa", source)
        except ValueError:
            rejected.append(name)
        else:
            raise ValueError("Unsafe rendered mutation accepted: " + name)
    return {
        "status": "passed_offline_application_chart_contract",
        "profiles": reports,
        "negative_cases_rejected": rejected,
        "positive_mode": "private_synthetic_render_fixtures_only",
        "published_workload_image_proved": False,
        "secret_delivery_or_startup_proved": False,
        "source_revision_used_for_fixture": source,
        "telemetry_environment_gap": (
            "Existing Python code explicitly emits local; chart labels do not fix attribution"
        ),
        "api_schema_baseline": "1.35.0_offline_only",
        "deployable": False,
        "cluster_operations": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate(args.cache, args.restore)
    if args.output:
        private(args.output).write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
