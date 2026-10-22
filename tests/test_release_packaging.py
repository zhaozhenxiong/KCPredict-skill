"""Portable skill releases must retain their shared theme and loader together."""
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_release.py"
THEME_FILES = ("assets/report-theme.json", "scripts/kcpredict_theme.py")


@pytest.fixture
def release_builder():
    spec = importlib.util.spec_from_file_location("_portable_skill_release_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_fixture(root, release_builder):
    for name in release_builder.ROOT_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"Synthetic packaging fixture\n")
    for name in release_builder.SOURCE_DIRECTORIES:
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in release_builder.REQUIRED_SKILL:
        target = root / "skill" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(("Portable fixture: " + name + "\n").encode())


@pytest.mark.parametrize("missing", THEME_FILES)
def test_missing_shared_theme_or_loader_prevents_a_portable_release(release_builder, tmp_path, missing):
    assert set(THEME_FILES) <= release_builder.REQUIRED_SKILL
    source_fixture(tmp_path, release_builder)
    (tmp_path / "skill" / missing).unlink()
    with pytest.raises(ValueError, match="Incomplete portable skill") as error:
        release_builder.source_inventory(tmp_path)
    assert missing in str(error.value)


def test_skill_zip_preserves_shared_theme_and_loader_source_bytes(release_builder, tmp_path):
    source_fixture(tmp_path, release_builder)
    files = release_builder.source_inventory(tmp_path)
    skill = {name.removeprefix("skill/"): data for name, data in files.items()
             if name.startswith("skill/")}
    archive = tmp_path / "portable-skill.zip"
    release_builder.write_zip(archive, {"kcpredict/" + name: data for name, data in skill.items()},
                              release_builder.MINIMUM_EPOCH)
    content = release_builder.read_archive(archive)
    for name in THEME_FILES:
        assert content["kcpredict/" + name] == (tmp_path / "skill" / name).read_bytes()


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("distribution", ["wheel", "sdist"])
@pytest.mark.parametrize("metadata_version", ["0.0.4", "0.0.40"], ids=["correct", "wrong"])
def test_metadata_version_audit_accepts_line_endings_but_rejects_mismatch(
        release_builder, tmp_path, line_ending, distribution, metadata_version):
    version = "0.0.4"
    prefix = "" if distribution == "wheel" else f"kcpredict-{version}/"
    key = f"kcpredict-{version}.dist-info/METADATA" if distribution == "wheel" else prefix + "PKG-INFO"
    metadata = line_ending.join(["Metadata-Version: 2.4", "Name: kcpredict",
                                 f"Version: {metadata_version}", ""])
    archive = tmp_path / "metadata.zip"
    release_builder.write_zip(archive, {key: metadata.encode("utf-8")}, release_builder.MINIMUM_EPOCH)
    if metadata_version == version:
        result = release_builder.audit(archive, {}, prefix=prefix, version=version, distribution=distribution)
        assert result["expected_files_identical"] is True
    else:
        with pytest.raises(ValueError, match="Incorrect package metadata version"):
            release_builder.audit(archive, {}, prefix=prefix, version=version, distribution=distribution)


@pytest.mark.parametrize("content_changed", [False, True], ids=["crlf-only", "different-content"])
def test_build_restores_only_setup_cfg_line_endings(release_builder, tmp_path, monkeypatch, content_changed):
    root, destination = tmp_path / "source", tmp_path / "release"
    source_fixture(root, release_builder)
    for name in ("src/kcpredict/__init__.py", "tests/test_fixture.py", "docs/fixture.md"):
        (root / name).write_bytes(b"# Synthetic packaging fixture\n")
    original = b"[egg_info]\ntag_build = \ntag_date = 0\n"
    (root / "setup.cfg").write_bytes(original)
    version = "0.0.4"
    prefix = f"kcpredict-{version}/"
    monkeypatch.setattr(release_builder, "project_version", lambda _: version)
    monkeypatch.setattr(release_builder, "verify_frozen_assets", lambda _: {"frozen_assets_verified": True})
    monkeypatch.setattr(release_builder.metadata, "version", lambda _: "fixture")

    def fake_build(command, **kwargs):
        raw = Path(command[command.index("--outdir") + 1])
        files = release_builder.source_inventory(Path(command[-1]))
        metadata = f"Metadata-Version: 2.4\r\nName: kcpredict\r\nVersion: {version}\r\n".encode()
        wheel = {name.removeprefix("src/"): data for name, data in files.items()
                 if name.startswith("src/kcpredict/")}
        wheel[f"kcpredict-{version}.dist-info/METADATA"] = metadata
        release_builder.write_zip(raw / f"kcpredict-{version}-py3-none-any.whl", wheel,
                                  release_builder.MINIMUM_EPOCH)
        sdist = {prefix + name: data for name, data in files.items()}
        sdist[prefix + "PKG-INFO"] = metadata
        rewritten = original.replace(b"\n", b"\r\n")
        sdist[prefix + "setup.cfg"] = rewritten + (b"tag_svn_revision = 1\r\n" if content_changed else b"")
        release_builder.write_tar(raw / f"kcpredict-{version}.tar.gz", sdist, release_builder.MINIMUM_EPOCH)
        return release_builder.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(release_builder.subprocess, "run", fake_build)
    if content_changed:
        with pytest.raises(ValueError, match=r"Missing or altered release file.*setup\.cfg"):
            release_builder.build(root, destination)
        assert not destination.exists()
    else:
        manifest = release_builder.build(root, destination)
        sdist = release_builder.read_archive(destination / f"kcpredict-{version}.tar.gz")
        assert sdist[prefix + "setup.cfg"] == original
        assert all(item["expected_files_identical"] for item in manifest["files"])
    assert (root / "setup.cfg").read_bytes() == original
