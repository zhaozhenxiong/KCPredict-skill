#!/usr/bin/env python3
"""Install this standalone distribution into its own .venv, never system Python."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-predict", action="store_true", help="Install structure analysis without PyTorch prediction dependencies")
    parser.add_argument("--offline", action="store_true", help="Use only packages in --wheelhouse; never contact a package index")
    parser.add_argument("--wheelhouse", type=Path, help="Directory containing dependency wheels (required with --offline)")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        parser.error("KCPredict requires Python 3.12. Run this script using Python 3.12.")
    root = Path(__file__).resolve().parent
    if not (root / "pyproject.toml").is_file() or not (root / "src" / "kcpredict").is_dir():
        parser.error("Keep install.py inside the extracted KCPredict source distribution.")
    wheelhouse = args.wheelhouse.expanduser().resolve() if args.wheelhouse else None
    if args.offline and wheelhouse is None:
        parser.error("--offline requires --wheelhouse DIR containing all dependencies and build wheels.")
    if wheelhouse is not None and not wheelhouse.is_dir():
        parser.error(f"Wheelhouse directory does not exist: {wheelhouse}")
    env_dir = root / ".venv"
    if env_dir.is_symlink():
        parser.error("Refusing a symlink at .venv. Choose a normal local package directory.")
    if env_dir.exists() and not (env_dir / "pyvenv.cfg").is_file():
        parser.error(".venv exists but is not a virtual environment; move it aside before installation.")
    if not env_dir.exists():
        venv.EnvBuilder(with_pip=True).create(env_dir)
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        parser.error("Existing .venv is incomplete; move it aside and retry.")
    check = subprocess.run([str(python), "-I", "-c", "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) and sys.prefix != sys.base_prefix else 1)"], check=False)
    if check.returncode:
        parser.error("Existing .venv is not a Python 3.12 virtual environment; move it aside and retry.")
    command = [str(python), "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-cache-dir", "install"]
    if args.offline:
        command += ["--no-index"]
    else:
        command += ["--index-url", "https://pypi.org/simple"]
    if wheelhouse is not None:
        command += ["--find-links", str(wheelhouse)]
    command += [str(root) + ("[charts]" if args.no_predict else "[predict,charts]")]
    result = subprocess.run(command, check=False)
    if result.returncode:
        return result.returncode
    print("\nKCPredict installed in", env_dir)
    print("Check installation:")
    print(f'  "{python}" -m kcpredict status')
    print("Models, daily data and verified trading calendar are prepared separately; see README.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
