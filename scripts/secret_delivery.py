"""No-values Secret contract and private synthetic runner injection only."""

import json
import os
import re
from pathlib import Path


def private_directory(path):
    path = Path(path).resolve()
    if any(
        (p / ".git").exists() or ((p / "HEAD").is_file() and (p / "objects").is_dir())
        for p in (path, *path.parents)
    ):
        raise ValueError("Secret working directory must be outside Git")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def synthetic_env(directory, key, source):
    directory = private_directory(directory)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
        raise ValueError("Only bounded synthetic runner keys accepted")
    if not re.fullmatch(r"[0-9a-f]{40}", source) or source == "0" * 40:
        raise ValueError("Actual source revision required")
    path = directory / "runtime.env"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(
            "LAB_API_KEY="
            + key
            + "\nLAB_VERSION="
            + source
            + "\nALLOW_FAULTS=0\nPYTHONDONTWRITEBYTECODE=1\n"
            "PYTHONUNBUFFERED=1\nRETRIEVAL_URL=http://retrieval:8080\n"
        )
    return path


def validate_contract(path):
    contract = json.loads(Path(path).read_text(encoding="utf-8"))

    def no_values(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"data", "stringdata", "value", "secretvalue", "password"}:
                    raise ValueError("Secret values forbidden")
                no_values(child)
        elif isinstance(value, list):
            for child in value:
                no_values(child)

    no_values(contract)
    if set(contract) != {
        "schema_version",
        "status",
        "azure_deployment_enabled",
        "provider",
        "resource_owner",
        "secret_values_in_terraform_state",
        "secret_values_in_git_or_artifacts",
        "namespaces",
        "application_consumption",
        "future_csi",
        "rotation",
        "private_image_pull",
    }:
        raise ValueError("Unknown Secret contract field")
    if contract["schema_version"] != 1 or contract["azure_deployment_enabled"] is not False:
        raise ValueError("Only disabled offline delivery contract accepted")
    if (
        contract["status"] != "offline_contract_not_live_delivery"
        or contract["provider"] != "azure_key_vault_csi_workload_identity"
        or contract["resource_owner"] != "terraform_infra"
    ):
        raise ValueError("Unapproved delivery owner/provider")
    if (
        contract["secret_values_in_terraform_state"] is not False
        or contract["secret_values_in_git_or_artifacts"] is not False
        or contract["namespaces"] != ["ai-lab-dev", "ai-lab-qa"]
    ):
        raise ValueError("Secret storage/scope escape")
    if contract["application_consumption"] != {
        "mode": "existing_secret_key_ref",
        "key": "lab-api-key",
        "minimum_characters": 24,
        "optional": False,
        "shared_consumers": ["agent", "retrieval"],
    }:
        raise ValueError("Application reference contract mismatch")
    csi = contract["future_csi"]
    if csi != {
        "volume_mount_required_for_sync": True,
        "sync_to_namespaced_secret": True,
        "federated_identity_audience": "api://AzureADTokenExchange",
        "kubernetes_api_token_automount": False,
        "target_confirmed": False,
        "tenant_id": None,
        "vault_name": None,
        "client_id": None,
    }:
        raise ValueError("CSI mount/identity/target gate mismatch")
    if contract["rotation"] != {
        "environment_reload": "coordinated_restart",
        "quiesce_agent_before_retrieval_change": True,
        "old_key_rejected_after_restart": True,
        "zero_downtime_claimed": False,
    }:
        raise ValueError("Environment/shared-key rotation requires coordinated restart")
    if contract["private_image_pull"] != {
        "registry": "ghcr.io/ai-sre-oberveability/ai-agent-workloads",
        "visibility": "private",
        "aks_pull_credential_delivery": "pending_separate_design",
        "actions_token_reused_in_aks": False,
    }:
        raise ValueError("Private registry credential delivery remains separately gated")
    return True
