"""Load only the verified frozen native extension, without a czsc installation."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import zipfile

RESOURCES = Path(__file__).parent / "resources"
SOURCE_SHA = "23cc880c2eccb8505a011fe61a95dd9a93cb3c8fb58ae09410a37f1e776958a7"
INDEX_SHA = "27691085240c64531279c157a9bc44568d1962240eacedba465716da61a31f94"


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=1)
def engine_contract():
    manifest = json.loads((RESOURCES / "czsc-source-manifest.json").read_text())
    actual = hashlib.sha256("\n".join(f"{x['path']} {x['sha256']}" for x in manifest["files"]).encode()).hexdigest()
    if actual != SOURCE_SHA:
        raise RuntimeError("Unexpected frozen CZSC source identity")
    root = (RESOURCES / "czsc-source").resolve()
    for entry in manifest["files"]:
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root) or _sha(path) != entry["sha256"]:
            raise RuntimeError("Frozen CZSC source mismatch: " + entry["path"])
    return {"build_version": manifest["version"], "source_tree_sha256": actual,
            "archive_sha256": manifest["archive_sha256"], "algorithm_patches": []}


def engine_status():
    contract = dict(engine_contract())
    index_path = RESOURCES / "czsc-wheels/index.json"
    if _sha(index_path) != INDEX_SHA:
        raise RuntimeError("Frozen native wheel index SHA mismatch")
    index = json.loads(index_path.read_text())
    if index["source_tree_sha256"] != SOURCE_SHA:
        raise RuntimeError("Native index belongs to another source")
    system, machine = platform.system(), platform.machine().lower()
    entry = next((x for x in index["wheels"] if x["system"] == system and
                  x["machine"].lower() == machine), None)
    return {**contract, "system": system, "machine": machine, "supported": entry is not None,
            "resource": entry,
            "reason": None if entry else "This platform requires a build of the included frozen source"}


@lru_cache(maxsize=1)
def native_runtime():
    status = engine_status()
    if not status["supported"]:
        raise RuntimeError(status["reason"])
    entry = status["resource"]
    wheel = RESOURCES / "czsc-wheels" / entry["filename"]
    if _sha(wheel) != entry["sha256"]:
        raise RuntimeError("Frozen native wheel SHA mismatch")
    home = Path(os.environ.get("KCPREDICT_HOME", Path.home() / ".kcpredict")).expanduser().resolve()
    folder = home / "native" / entry["native_sha256"]
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / Path(entry["native_member"]).name
    if binary.exists():
        if _sha(binary) != entry["native_sha256"]:
            raise RuntimeError("Extracted native engine SHA mismatch; remove the corrupt cache explicitly")
    else:
        with zipfile.ZipFile(wheel) as archive:
            data = archive.read(entry["native_member"])
        if hashlib.sha256(data).hexdigest() != entry["native_sha256"]:
            raise RuntimeError("Native archive member SHA mismatch")
        fd, temporary = tempfile.mkstemp(dir=folder)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, binary)
        finally:
            Path(temporary).unlink(missing_ok=True)
    name = "kcpredict._native"
    spec = importlib.util.spec_from_file_location(name, binary)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module._native = module
    return module
