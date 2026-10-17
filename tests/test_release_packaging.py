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
