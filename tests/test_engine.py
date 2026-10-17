"""Independent native loading and frozen resource tamper boundaries."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from kcpredict import engine


@pytest.fixture(autouse=True)
def clear_engine_caches():
    engine.engine_contract.cache_clear()
    engine.native_runtime.cache_clear()
    yield
    engine.engine_contract.cache_clear()
    engine.native_runtime.cache_clear()


def _resource():
    status = engine.engine_status()
    if not status["supported"]:
        pytest.skip("No frozen native binary for this platform")
    return dict(status["resource"])


def test_frozen_source_identity_is_verified():
    contract = engine.engine_contract()
    assert contract["build_version"] == "1.0.1"
    assert contract["source_tree_sha256"] == engine.SOURCE_SHA
    assert len(contract["archive_sha256"]) == 64
    assert contract["algorithm_patches"] == []


def test_source_file_tamper_is_rejected(tmp_path, monkeypatch):
    original = json.loads((engine.RESOURCES / "czsc-source-manifest.json").read_text())
    item = original["files"][0]
    shutil.copytree(engine.RESOURCES / "czsc-source", tmp_path / "czsc-source")
    target = tmp_path / "czsc-source" / item["path"]
    target.write_bytes(b"changed frozen source")
    (tmp_path / "czsc-source-manifest.json").write_text(json.dumps(original))
    monkeypatch.setattr(engine, "RESOURCES", tmp_path)
    with pytest.raises(RuntimeError, match="Frozen CZSC source mismatch"):
        engine.engine_contract()


def test_rewritten_manifest_cannot_grant_a_new_source_identity(tmp_path, monkeypatch):
    original = json.loads((engine.RESOURCES / "czsc-source-manifest.json").read_text())
    relative = "Cargo.toml"
    content = b"changed source with a newly calculated file hash"
    target = tmp_path / "czsc-source" / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    manifest = {**original, "files": [{"path": relative, "sha256": hashlib.sha256(content).hexdigest()}]}
    (tmp_path / "czsc-source-manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(engine, "RESOURCES", tmp_path)
    with pytest.raises(RuntimeError, match="Unexpected frozen CZSC source identity"):
        engine.engine_contract()


def test_unsupported_platform_is_explicit(monkeypatch):
    monkeypatch.setattr(engine.platform, "system", lambda: "Linux")
    monkeypatch.setattr(engine.platform, "machine", lambda: "x86_64")
    status = engine.engine_status()
    assert status["supported"] is False
    with pytest.raises(RuntimeError, match="requires a build"):
        engine.native_runtime()


def test_rewritten_native_index_is_rejected(tmp_path, monkeypatch):
    index = json.loads((engine.RESOURCES / "czsc-wheels/index.json").read_text())
    index["wheels"][0]["sha256"] = "0" * 64
    (tmp_path / "czsc-wheels").mkdir()
    (tmp_path / "czsc-wheels/index.json").write_text(json.dumps(index))
    monkeypatch.setattr(engine, "RESOURCES", tmp_path)
    monkeypatch.setattr(engine, "engine_contract", lambda: {"source_tree_sha256": engine.SOURCE_SHA})
    with pytest.raises(RuntimeError, match="index"):
        engine.engine_status()


def test_native_index_must_belong_to_the_frozen_source(tmp_path, monkeypatch):
    index = json.loads((engine.RESOURCES / "czsc-wheels/index.json").read_text())
    index["source_tree_sha256"] = "0" * 64
    (tmp_path / "czsc-wheels").mkdir()
    target = tmp_path / "czsc-wheels/index.json"
    target.write_text(json.dumps(index))
    monkeypatch.setattr(engine, "RESOURCES", tmp_path)
    monkeypatch.setattr(engine, "INDEX_SHA", hashlib.sha256(target.read_bytes()).hexdigest())
    monkeypatch.setattr(engine, "engine_contract", lambda: {"source_tree_sha256": engine.SOURCE_SHA})
    with pytest.raises(RuntimeError, match="another source"):
        engine.engine_status()


def test_native_wheel_tamper_is_rejected(tmp_path, monkeypatch):
    item = _resource()
    (tmp_path / "czsc-wheels").mkdir()
    (tmp_path / "czsc-wheels" / item["filename"]).write_bytes(b"tampered wheel")
    monkeypatch.setattr(engine, "RESOURCES", tmp_path)
    monkeypatch.setattr(engine, "engine_status", lambda: {"supported": True, "resource": item})
    monkeypatch.setenv("KCPREDICT_HOME", str(tmp_path / "state"))
    with pytest.raises(RuntimeError, match="Frozen native wheel SHA mismatch"):
        engine.native_runtime()
    assert not (tmp_path / "state").exists()


def test_extracted_native_cache_tamper_is_rejected(tmp_path, monkeypatch):
    item = _resource()
    folder = tmp_path / "native" / item["native_sha256"]
    folder.mkdir(parents=True)
    (folder / Path(item["native_member"]).name).write_bytes(b"corrupt extracted native")
    monkeypatch.setenv("KCPREDICT_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="Extracted native engine SHA mismatch"):
        engine.native_runtime()


def test_archive_member_hash_is_verified_before_loading(tmp_path, monkeypatch):
    item = _resource()
    (tmp_path / "czsc-wheels").mkdir()
    wheel = tmp_path / "czsc-wheels" / item["filename"]
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(item["native_member"], b"changed archive member")
    item["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    monkeypatch.setattr(engine, "RESOURCES", tmp_path)
    monkeypatch.setattr(engine, "engine_status", lambda: {"supported": True, "resource": item})
    monkeypatch.setenv("KCPREDICT_HOME", str(tmp_path / "state"))
    with pytest.raises(RuntimeError, match="Native archive member SHA mismatch"):
        engine.native_runtime()
    assert list((tmp_path / "state").rglob("*.so")) == []
    assert list((tmp_path / "state").rglob("*.pyd")) == []


def test_real_native_extension_loads_without_czsc_package(tmp_path):
    _resource()
    # A separate interpreter avoids extension initialization state from other
    # structure tests and verifies that no KHQuant/czsc module is consulted.
    script = """
import importlib.abc, json, sys
from datetime import datetime
class DenyLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'czsc', 'my_strategy'}:
            raise AssertionError('unexpected legacy import: ' + fullname)
sys.meta_path.insert(0, DenyLegacy())
from kcpredict.engine import native_runtime, engine_contract
native = native_runtime()
bar = native.RawBar(symbol='000001.SZ', dt=datetime(2020,1,2,15), freq=native.Freq.D,
                    id=1, open=10., close=10.2, high=10.3, low=9.9,
                    vol=1000000., amount=10200000.)
analysis = native.CZSC([bar], min_bi_len=6, max_bi_num=50)
assert analysis.bars_raw[-1].close == 10.2
# The original extension registers built-in submodule labels such as
# czsc._native.ta; no external top-level package is imported or installed.
assert not {'czsc', 'my_strategy'}.intersection(sys.modules)
print(json.dumps({'ready': True, 'source': engine_contract()['source_tree_sha256']}))
"""
    environment = dict(os.environ)
    environment["KCPREDICT_HOME"] = str(tmp_path / "native-home")
    # For a source checkout only, preserve its explicit source package path;
    # installed wheel runs use their own interpreter's site-packages.
    environment["PYTHONPATH"] = str(Path(engine.__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ready"] is True
