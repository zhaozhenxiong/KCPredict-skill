#!/usr/bin/env python3
"""Dispatch to an explicitly installed standalone KCPredict CLI, using stdlib."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


class KCPredictUnavailable(RuntimeError):
    """The requested standalone executable could not be resolved."""


INSTALL_HELP = (
    "未发现独立 KCPredict 运行时。请解压独立发行包，在发行目录运行 "
    "python3.12 install.py（Windows: py -3.12 install.py）。然后设置 "
    "KCPREDICT_PYTHON 为该目录 .venv 中的 Python，或设置 "
    "KCPREDICT_EXECUTABLE 为 kcpredict 可执行文件，或激活虚拟环境后重试。"
    "技能目录只包含调用说明，不包含行情、交易日历或模型权重。"
)


def _explicit_executable(value: str, variable: str) -> str:
    value = value.strip()
    if not value:
        raise KCPredictUnavailable(f"{variable} 为空；请指定完整可执行路径。")
    expanded = str(Path(value).expanduser())
    candidate = Path(expanded)
    if not candidate.is_file():
        found = shutil.which(expanded)
        if not found:
            raise KCPredictUnavailable(f"{variable} 指定的可执行文件不存在。" + INSTALL_HELP)
        candidate = Path(found)
    if candidate.resolve() == Path(__file__).resolve():
        raise KCPredictUnavailable(f"{variable} 指向技能分发脚本本身；请指向独立运行时。")
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        raise KCPredictUnavailable(f"{variable} 指定的文件不可执行。")
    # A venv's Python may be a symlink. Resolving it to the base interpreter
    # would discard the explicitly selected environment and its installed CLI.
    return str(candidate.absolute())


def _current_package_available() -> bool:
    # This helper is also named kcpredict.py. Exclude its own directory while
    # looking up the installed package, otherwise it shadows that package.
    script_directory = Path(__file__).resolve().parent
    original = list(sys.path)
    try:
        sys.path[:] = [entry for entry in original
                       if Path(entry or os.curdir).resolve() != script_directory]
        spec = importlib.util.find_spec("kcpredict")
        return spec is not None and spec.submodule_search_locations is not None
    except (ImportError, AttributeError, ValueError, OSError):
        return False
    finally:
        sys.path[:] = original


def resolve_command() -> list[str]:
    """Honor explicit destinations; never fall back after an invalid override."""
    if "KCPREDICT_PYTHON" in os.environ:
        interpreter = _explicit_executable(os.environ["KCPREDICT_PYTHON"], "KCPREDICT_PYTHON")
        return [interpreter, "-m", "kcpredict"]
    if "KCPREDICT_EXECUTABLE" in os.environ:
        executable = _explicit_executable(os.environ["KCPREDICT_EXECUTABLE"], "KCPREDICT_EXECUTABLE")
        return [executable]
    installed = shutil.which("kcpredict")
    if installed:
        if Path(installed).resolve() == Path(__file__).resolve():
            raise KCPredictUnavailable("PATH 的 kcpredict 指向技能脚本本身。" + INSTALL_HELP)
        return [installed]
    if _current_package_available():
        return [sys.executable, "-m", "kcpredict"]
    raise KCPredictUnavailable(INSTALL_HELP)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        completed = subprocess.run([*resolve_command(), *arguments], check=False)
        return completed.returncode
    except (KCPredictUnavailable, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
