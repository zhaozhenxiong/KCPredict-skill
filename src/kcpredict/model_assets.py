"""Pinned, offline Kronos assets. Downloading is an explicit maintenance action."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
import threading
import urllib.request


PINNED_ASSETS = (
    {"path": "model.safetensors", "repo": "NeoQuasar/Kronos-base", "revision": "2b554741eca47781b64468546e77fef3e85130e6", "remote": "model.safetensors", "size": 409264008, "sha256": "abff193acab6db1a0368e9773e75799d11403b6d054ee6d5f0a11aeabc5f4b83"},
    {"path": "config.json", "repo": "NeoQuasar/Kronos-base", "revision": "2b554741eca47781b64468546e77fef3e85130e6", "remote": "config.json", "size": 228, "git_blob": "b38b53ec92facc89fc178f641a951cbe9730ecc7"},
    {"path": "tokenizer/model.safetensors", "repo": "NeoQuasar/Kronos-Tokenizer-base", "revision": "0e0117387f39004a9016484a186a908917e22426", "remote": "model.safetensors", "size": 15842368, "sha256": "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee"},
    {"path": "tokenizer/config.json", "repo": "NeoQuasar/Kronos-Tokenizer-base", "revision": "0e0117387f39004a9016484a186a908917e22426", "remote": "config.json", "size": 301, "git_blob": "050b676e14720956dc6ff93a755e65dee5536807"},
)
_LOCK = threading.RLock()
_VERIFIED: dict[tuple, dict] = {}
# Configuration bytes are cheap and must never rely on timestamp precision.
# Only large pinned weights may reuse a full SHA, within this process, for the
# same file/version and pin contract. No handles or model bytes are retained.
_MEMO_MIN_BYTES = 1024 * 1024
_MEMO_LIMIT = 8
_WINDOWS = os.name == 'nt'


def asset_root(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    home = Path(os.environ.get("KCPREDICT_HOME") or Path.home() / ".kcpredict").expanduser()
    return (home / "models" / "kronos").resolve()


def _stat_identity(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _memo_version(info) -> tuple | None:
    # Windows st_ctime is creation time; a same-size edit may also retain the
    # observed write timestamp. Read the entire pinned weight on each check
    # instead of treating that tuple as proof that its contents are unchanged.
    return None if _WINDOWS else _stat_identity(info)


def _verify(path: Path, entry: dict) -> dict:
    # Bind every cached success to the expected official contract as well as to
    # the physical file. A changed pin cannot inherit an earlier verification.
    contract = json.dumps(entry, sort_keys=True, separators=(',', ':'), allow_nan=False)
    memo_weight = (entry['path'].endswith('.safetensors') and 'sha256' in entry
                   and 'git_blob' not in entry and entry['size'] >= _MEMO_MIN_BYTES)
    with _LOCK, path.open('rb') as handle:
        before = os.fstat(handle.fileno())
        if before.st_size != entry['size']:
            raise ValueError(f"Kronos asset size mismatch: {entry['path']}")
        version = _memo_version(before) if memo_weight else None
        key = (str(path), contract, version) if version is not None else None
        cached = _VERIFIED.get(key) if key is not None else None
        raw = None
        if cached is None:
            if not memo_weight:
                raw = handle.read()
                sha = hashlib.sha256(raw).hexdigest()
            else:
                sha = hashlib.file_digest(handle, 'sha256').hexdigest()
            if 'sha256' in entry and sha != entry['sha256']:
                raise ValueError(f"Kronos asset SHA mismatch: {entry['path']}")
            if 'git_blob' in entry:
                blob = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
                if blob != entry['git_blob']:
                    raise ValueError(f"Kronos configuration identity mismatch: {entry['path']}")
                json.loads(raw)
            result = {'path': entry['path'], 'sha256': sha, 'size': before.st_size,
                      'repo': entry['repo'], 'revision': entry['revision']}
        else:
            result = cached
        after = os.fstat(handle.fileno())
        changed = _stat_identity(after) != _stat_identity(before)
        if not _WINDOWS:
            changed = changed or _stat_identity(path.stat()) != _stat_identity(after)
        changed = (changed
                   or (version is not None and _memo_version(after) != version)
                   or (raw is not None and path.read_bytes() != raw))
        if changed:
            raise ValueError('Kronos asset changed during verification')
        if key is not None and cached is None:
            for old in [item for item in _VERIFIED if item[0] == str(path)]:
                del _VERIFIED[old]
            while len(_VERIFIED) >= _MEMO_LIMIT:
                del _VERIFIED[next(iter(_VERIFIED))]
            _VERIFIED[key] = result
        return dict(result)


def asset_status(path: str | Path | None = None) -> dict:
    """Verify actual bytes against pinned official identities; never fetch a model."""
    root = asset_root(path)
    files, missing, errors = [], [], []
    for entry in PINNED_ASSETS:
        target = root / entry["path"]
        if not target.is_file():
            missing.append(entry["path"])
            continue
        try:
            files.append(_verify(target, entry))
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    return {"schema": "kronos_assets_v1", "ready": not missing and not errors,
            "model": "Kronos-base", "tokenizer": "Kronos-Tokenizer-base", "files": files,
            "missing": missing, "errors": errors, "offline_only": True}


def asset_manifest() -> dict:
    """Return the frozen public artifact contract without touching disk or network."""
    return {"schema": "kcpredict_model_manifest_v1", "model": "Kronos-base",
            "tokenizer": "Kronos-Tokenizer-base", "files": [
                {**entry, "url": _asset_url(entry)} for entry in PINNED_ASSETS],
            "total_size": sum(entry["size"] for entry in PINNED_ASSETS)}


def _asset_url(entry: dict) -> str:
    return f"https://huggingface.co/{entry['repo']}/resolve/{entry['revision']}/{entry['remote']}"


def _publish_verified(temporary: Path, target: Path, entry: dict) -> None:
    """Publish complete verified bytes atomically, never overwrite an existing asset."""
    _verify(temporary, entry)
    try:
        # Both paths share a directory/filesystem. Hard-link publication has
        # atomic create-only semantics, including when two installers race.
        os.link(temporary, target)
    except FileExistsError:
        _verify(target, entry)


def prepare_assets(path: str | Path | None = None, *, source: str | Path | None = None,
                   download: bool = False, allow_base_download: bool = False) -> dict:
    """Explicitly import local frozen assets or fetch fixed public revisions.

    A normal call only verifies. No missing file is downloaded unless download
    (or the legacy explicit allow_base_download flag) is true. Existing invalid
    files are never replaced. Local source must contain the pinned model and
    tokenizer directory layout; sources are validated before any copy is made.
    """
    if source is not None and (download or allow_base_download):
        raise ValueError("Choose either a local source directory or explicit download")
    root = asset_root(path)
    downloading = bool(download or allow_base_download)
    source_root = Path(source).expanduser().resolve() if source is not None else None
    # Preflight every existing destination, so a corrupt asset cannot cause a
    # partially updated model directory before the error becomes visible.
    for entry in PINNED_ASSETS:
        target = root / entry["path"]
        if target.exists() or target.is_symlink():
            _verify(target, entry)
    if source_root is not None:
        for entry in PINNED_ASSETS:
            _verify(source_root / entry["path"], entry)
    if source_root is None and not downloading:
        result = asset_status(root)
        if not result["ready"]:
            raise FileNotFoundError("Kronos local assets unavailable; import a model directory or explicitly download pinned assets")
        return {**result, "downloaded": [], "imported": []}

    downloaded, imported = [], []
    for entry in PINNED_ASSETS:
        target = root / entry["path"]
        if target.exists():
            _verify(target, entry)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix=target.name + ".", suffix=".prepare", dir=target.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as output:
                if source_root is not None:
                    with (source_root / entry["path"]).open("rb") as input_file:
                        shutil.copyfileobj(input_file, output, length=1024 * 1024)
                else:
                    total = 0
                    with urllib.request.urlopen(_asset_url(entry), timeout=60) as response:
                        while chunk := response.read(1024 * 1024):
                            total += len(chunk)
                            if total > entry["size"]:
                                raise ValueError("Kronos asset exceeds pinned size")
                            output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            _publish_verified(temporary, target, entry)
            (imported if source_root is not None else downloaded).append(entry["path"])
        finally:
            temporary.unlink(missing_ok=True)
    result = asset_status(root)
    result.update(downloaded=downloaded, imported=imported)
    return result
