"""Checksum-locked private Helm renderer; adapted from the approved charts helpers."""

import hashlib
import io
import json
import os
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = Path(os.environ.get("CHART_INPUT_CACHE", ROOT.parent / ".runtime/chart-inputs"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private(path):
    path = Path(path).resolve()
    for parent in (path, *path.parents):
        if (parent / ".git").exists() or (
            (parent / "HEAD").is_file() and (parent / "objects").is_dir()
        ):
            raise ValueError("Renderer cache must remain outside Git")
    return path


def downloaded(url, expected, target, restore):
    target = private(target)
    if not target.exists():
        if not restore:
            raise ValueError("Locked cache missing; explicit --restore required")
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(100 * 1024 * 1024 + 1)
        if len(data) > 100 * 1024 * 1024 or sha(data) != expected:
            raise ValueError("Downloaded input checksum/size mismatch")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    data = target.read_bytes()
    if sha(data) != expected:
        raise ValueError("Cached input checksum mismatch")
    return data


def renderer(cache, restore=False):
    tool = json.loads((ROOT / "locks/chart-toolchain.json").read_text())["tools"]["helm"]
    system = platform.system().lower()
    if system not in {"windows", "linux"} or platform.machine().lower() not in {
        "amd64",
        "x86_64",
    }:
        raise ValueError("Unsupported renderer platform")
    row = tool["platforms"][system + "_amd64"]
    suffix = "zip" if system == "windows" else "tar.gz"
    filename = f"helm-v{tool['version']}-{system}-amd64.{suffix}"
    if row["url"] != "https://get.helm.sh/" + filename:
        raise ValueError("Unreviewed renderer URL")
    data = downloaded(row["url"], row["archive_sha256"], cache / filename, restore)
    if system == "windows":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            binary = archive.read(row["member"])
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            binary = archive.extractfile(row["member"]).read()
    if hashlib.sha256(binary).hexdigest() != row["binary_sha256"]:
        raise ValueError("Renderer binary integrity mismatch")
    path = private(cache / ("helm-collector.exe" if system == "windows" else "helm-collector"))
    path.write_bytes(binary)
    path.chmod(0o700)
    return path
