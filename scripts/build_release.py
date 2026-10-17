#!/usr/bin/env python3
"""Build audited release assets from an allowlisted, clean source snapshot.

Requires Python 3.12 and the project's build requirements, plus ``build``.
Builds never install dependencies or modify the working source tree. Existing
release assets are never overwritten. SOURCE_DATE_EPOCH controls normalized
archive timestamps; identical sources and build tools produce identical assets.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import gzip
import hashlib
from importlib import metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile


ROOT_FILES = (
    "README.md", "CHANGELOG.md", "VERIFICATION.md", "pyproject.toml", "setup.cfg",
    "MANIFEST.in", "LICENSE", "NOTICE", "install.py", "install.sh", "install.bat",
    "scripts/build_release.py",
)
SOURCE_DIRECTORIES = ("src/kcpredict", "skill", "tests", "docs")
EXCLUDED_PARTS = {".git", ".venv", "venv", "outputs", "build", "dist", "wheelhouse",
                  "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}
PRIVATE_SUFFIXES = {".csv", ".tsv", ".db", ".sqlite", ".sqlite3", ".safetensors", ".pt", ".pth",
                    ".ckpt", ".bin", ".parquet", ".npy", ".npz", ".pkl", ".pickle", ".pem",
                    ".key", ".p12", ".pfx", ".log"}
MINIMUM_EPOCH = 315532800  # ZIP's earliest representable date: 1980-01-01 UTC.
REQUIRED_SKILL = {"SKILL.md", "LICENSE", "agents/openai.yaml", "assets/composite.html", "assets/report-theme.json",
                  "scripts/kcpredict.py", "scripts/ict_scan.py", "scripts/kcpredict_ict.py",
                  "scripts/kcpredict_chart_data.py", "scripts/kcpredict_chart.py",
                  "scripts/kcpredict_html.py", "scripts/kcpredict_report.py", "scripts/kcpredict_theme.py",
                  "references/ict-LICENSE.txt", "references/ict-provenance.md"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def forbidden(path: str, *, generated_metadata: bool = False) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts or "\\" in path:
        return True
    for part in parts:
        if part in EXCLUDED_PARTS or part in {".DS_Store", "preview.png", "id_rsa", "id_ed25519"}:
            return True
        if part.startswith(".env") or part.endswith((".pyc", ".pyo")):
            return True
        if part.endswith(".egg-info") and not (generated_metadata and part == "kcpredict.egg-info"):
            return True
    if parts[-1] == "PKG-INFO" and not generated_metadata:
        return True
    if PurePosixPath(path).suffix.lower() in PRIVATE_SUFFIXES:
        return True
    if path.endswith(".whl") and "resources/czsc-wheels/" not in path:
        return True
    return False


def source_inventory(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in ROOT_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or symlinked required source file: {name}")
        files[name] = path.read_bytes()
    for name in SOURCE_DIRECTORIES:
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"Missing or symlinked required source directory: {name}")
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if forbidden(relative):
                continue
            if path.is_symlink():
                raise ValueError(f"Refusing a symlink in the release snapshot: {relative}")
            if path.is_file():
                files[relative] = path.read_bytes()
    missing_skill = REQUIRED_SKILL - {name.removeprefix("skill/") for name in files if name.startswith("skill/")}
    if missing_skill:
        raise ValueError(f"Incomplete portable skill: {sorted(missing_skill)}")
    return dict(sorted(files.items()))


def project_version(files: dict[str, bytes]) -> str:
    version = tomllib.loads(files["pyproject.toml"].decode("utf-8"))["project"]["version"]
    if not isinstance(version, str) or not version or any(c not in "0123456789." for c in version):
        raise ValueError("Release version must be a numeric dotted project version")
    tree = ast.parse(files["src/kcpredict/__init__.py"].decode("utf-8"))
    runtime_version = next((ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)), None)
    if runtime_version != version:
        raise ValueError(f"Package and runtime versions differ: {version!r} versus {runtime_version!r}")
    return version


def verify_frozen_assets(files: dict[str, bytes]) -> dict:
    base = "src/kcpredict/resources/"
    manifest = json.loads(files[base + "czsc-source-manifest.json"])
    for entry in manifest["files"]:
        relative = base + "czsc-source/" + entry["path"]
        if relative not in files or sha256(files[relative]) != entry["sha256"]:
            raise ValueError(f"Frozen CZSC source identity mismatch: {entry['path']}")
    tree_sha = sha256("\n".join(f"{entry['path']} {entry['sha256']}" for entry in manifest["files"]).encode())
    index = json.loads(files[base + "czsc-wheels/index.json"])
    if tree_sha != index["source_tree_sha256"]:
        raise ValueError("Frozen CZSC source and native wheel index disagree")
    for entry in index["wheels"]:
        binary = files[base + "czsc-wheels/" + entry["filename"]]
        if sha256(binary) != entry["sha256"]:
            raise ValueError(f"Frozen native wheel mismatch: {entry['filename']}")
        with zipfile.ZipFile(io.BytesIO(binary)) as wheel:
            if sha256(wheel.read(entry["native_member"])) != entry["native_sha256"]:
                raise ValueError(f"Frozen native binary mismatch: {entry['filename']}")
    kronos_base = "src/kcpredict/vendor/kronos/"
    kronos = json.loads(files[kronos_base + "source_manifest.json"])
    for entry in kronos["files"]:
        if sha256(files[kronos_base + entry["path"]]) != entry["sha256"]:
            raise ValueError(f"Frozen Kronos source mismatch: {entry['path']}")
    for name in ("LICENSE", "NOTICE", base + "CZSC-LICENSE", base + "CZSC-APACHE-2.0.txt",
                 kronos_base + "LICENSE", "skill/LICENSE", "skill/references/ict-LICENSE.txt"):
        if not files.get(name):
            raise ValueError(f"Missing required license/notice: {name}")
    if b"END OF TERMS AND CONDITIONS" not in files[base + "CZSC-APACHE-2.0.txt"]:
        raise ValueError("The supplemental Apache-2.0 license text is incomplete")
    if files["skill/LICENSE"] != files["LICENSE"]:
        raise ValueError("The independent skill must retain the project's complete MIT license")
    return {"czsc_source_tree_sha256": tree_sha, "czsc_source_files": len(manifest["files"]),
            "native_wheels": len(index["wheels"]), "kronos_source_files": len(kronos["files"]),
            "frozen_assets_verified": True}


def write_zip(path: Path, files: dict[str, bytes], epoch: int, *, executable_names: set[str] | None = None) -> None:
    moment = datetime.fromtimestamp(epoch, timezone.utc).timetuple()[:6]
    executable_names = executable_names or set()
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, moment)
            info.create_system = 3
            info.external_attr = (0o755 if name in executable_names else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=9)


def read_archive(path: Path) -> dict[str, bytes]:
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            files = {}
            for member in archive.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(f"Non-file archive member: {member.name}")
                stream = archive.extractfile(member)
                files[member.name] = stream.read()
            return files
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def write_tar(path: Path, files: dict[str, bytes], epoch: int) -> None:
    with path.open("xb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=epoch, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, data in sorted(files.items()):
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mtime = len(data), epoch
                    entry.uid, entry.gid, entry.uname, entry.gname = 0, 0, "", ""
                    entry.mode = 0o755 if name.endswith(("install.sh", "scripts/build_release.py")) else 0o644
                    archive.addfile(entry, io.BytesIO(data))


def audit(path: Path, expected: dict[str, bytes], *, prefix: str = "", version: str | None = None,
          distribution: str) -> dict:
    content = read_archive(path)
    for name in content:
        if forbidden(name, generated_metadata=distribution in {"sdist", "wheel"}):
            raise ValueError(f"Private/generated source path in {path.name}: {name}")
    for relative, data in expected.items():
        name = prefix + relative
        if content.get(name) != data:
            raise ValueError(f"Missing or altered release file in {path.name}: {name}")
    if version is not None:
        key = f"kcpredict-{version}.dist-info/METADATA" if distribution == "wheel" else prefix + "PKG-INFO"
        if f"Version: {version}\n" not in content[key].decode("utf-8"):
            raise ValueError(f"Incorrect package metadata version in {path.name}")
    raw = path.read_bytes()
    return {"filename": path.name, "distribution": distribution, "bytes": len(raw), "sha256": sha256(raw),
            "file_count": len(content), "expected_files_identical": True, "private_assets_absent": True,
            "full_skill_included": distribution != "wheel"}


def build(root: Path, destination: Path | None = None, *, epoch: int = MINIMUM_EPOCH) -> dict:
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Build releases using Python 3.12, matching the supported runtime")
    root = root.expanduser().resolve()
    files = source_inventory(root)
    version = project_version(files)
    frozen = verify_frozen_assets(files)
    epoch = max(int(epoch), MINIMUM_EPOCH)
    if datetime.fromtimestamp(epoch, timezone.utc).year > 2107:
        raise ValueError("SOURCE_DATE_EPOCH exceeds ZIP's supported year range")
    destination = (destination or root / "dist" / ("v" + version)).expanduser().absolute()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Refusing a nonempty release directory: {destination}")
    tool_versions = {name: metadata.version(name) for name in ("build", "setuptools", "wheel")}
    with tempfile.TemporaryDirectory(prefix="kcpredict-release-") as temporary:
        temp = Path(temporary)
        clean = temp / ("kcpredict-" + version)
        raw_assets, assets = temp / "raw", temp / "assets"
        clean.mkdir(); raw_assets.mkdir(); assets.mkdir()
        for name, data in files.items():
            path = clean / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755 if name in {"install.sh", "scripts/build_release.py"} else 0o644)
            os.utime(path, (epoch, epoch))
        environment = dict(os.environ)
        environment.update(SOURCE_DATE_EPOCH=str(epoch), PYTHONHASHSEED="0")
        command = [sys.executable, "-m", "build", "--no-isolation", "--sdist", "--wheel",
                   "--outdir", str(raw_assets), str(clean)]
        completed = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
        if completed.returncode:
            raise RuntimeError("Package build failed:\n" + (completed.stderr or completed.stdout)[-8000:])
        wheel_name = f"kcpredict-{version}-py3-none-any.whl"
        sdist_name = f"kcpredict-{version}.tar.gz"
        write_zip(assets / wheel_name, read_archive(raw_assets / wheel_name), epoch)
        write_tar(assets / sdist_name, read_archive(raw_assets / sdist_name), epoch)
        source_prefix = f"KCPredict-{version}/"
        standalone_name = f"KCPredict-{version}-standalone.zip"
        write_zip(assets / standalone_name, {source_prefix + name: data for name, data in files.items()}, epoch,
                  executable_names={source_prefix + name for name in ("install.sh", "scripts/build_release.py")})
        skill_name = f"kcpredict-skill-{version}.zip"
        skill = {name.removeprefix("skill/"): data for name, data in files.items() if name.startswith("skill/")}
        write_zip(assets / skill_name, {"kcpredict/" + name: data for name, data in skill.items()}, epoch)
        package = {name.removeprefix("src/"): data for name, data in files.items() if name.startswith("src/kcpredict/")}
        audits = [audit(assets / wheel_name, package, version=version, distribution="wheel"),
                  audit(assets / sdist_name, files, prefix=f"kcpredict-{version}/", version=version, distribution="sdist"),
                  audit(assets / standalone_name, files, prefix=source_prefix, distribution="standalone"),
                  audit(assets / skill_name, skill, prefix="kcpredict/", distribution="skill")]
        snapshot = [{"path": name, "sha256": sha256(data)} for name, data in files.items()]
        manifest = {"schema": "kcpredict_release_assets_v1", "version": version,
                    "source_date_epoch": epoch, "build_tools": tool_versions,
                    "source_snapshot_sha256": sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()),
                    "source_file_count": len(files), "source_roots": list(SOURCE_DIRECTORIES),
                    "source_files_allowlist": list(ROOT_FILES), "files": audits, **frozen,
                    "contains_private_data": False, "contains_model_weights": False,
                    "wheelhouse_in_public_archives": False}
        manifest_path = assets / "assets-manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sums = "".join(f"{sha256(path.read_bytes())}  {path.name}\n" for path in sorted(assets.iterdir()))
        (assets / "SHA256SUMS").write_text(sums, encoding="utf-8")
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise FileExistsError(f"Release directory changed while building: {destination}")
        for path in sorted(assets.iterdir()):
            with (destination / path.name).open("xb") as output:
                with path.open("rb") as source:
                    shutil.copyfileobj(source, output)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, help="New or empty asset directory; default dist/v<version>")
    parser.add_argument("--source-date-epoch", type=int, default=int(os.environ.get("SOURCE_DATE_EPOCH", MINIMUM_EPOCH)))
    options = parser.parse_args(argv)
    try:
        manifest = build(options.project_root, options.output_dir, epoch=options.source_date_epoch)
        destination = options.output_dir or options.project_root / "dist" / ("v" + manifest["version"])
        print(json.dumps({"version": manifest["version"], "output_dir": str(destination.absolute()),
                          "files": [item["filename"] for item in manifest["files"]],
                          "frozen_assets_verified": manifest["frozen_assets_verified"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, metadata.PackageNotFoundError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc),
            "build_dependency_install": [sys.executable, "-m", "pip", "install", "--index-url", "https://pypi.org/simple",
                                         "build>=1,<2", "setuptools>=77,<83", "wheel>=0.45,<1"]}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
