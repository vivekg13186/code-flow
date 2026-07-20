"""Environments: named JSON files with settings a run executes against.

Drop ``*.json`` files into the ``environments/`` folder, e.g.::

    environments/dev.json      {"api_url": "https://dev.api.local", "dry_run": true}
    environments/prod.json     {"api_url": "https://api.example.com", "dry_run": false}

The file name (without .json) is the environment name shown in the UI.
Inside steps the selected environment is available as ``self.env`` and
``ctx["env"]``, and conditions can use it too: ``condition="not env['dry_run']"``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

_ENVIRONMENTS_DIR: Path = Path("environments")


def set_environments_dir(folder: str | Path) -> None:
    global _ENVIRONMENTS_DIR
    _ENVIRONMENTS_DIR = Path(folder)


def get_environments_dir() -> Path:
    return _ENVIRONMENTS_DIR


def load_environments(folder: str | Path | None = None) -> Tuple[Dict[str, dict], List[dict]]:
    """Return ({env_name: values}, [errors]) from the environments folder."""
    folder = Path(folder) if folder else _ENVIRONMENTS_DIR
    envs: Dict[str, dict] = {}
    errors: List[dict] = []
    if not folder.is_dir():
        return envs, errors
    for f in sorted(folder.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("environment file must contain a JSON object")
            envs[f.stem] = data
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": f.name, "error": f"{type(exc).__name__}: {exc}"})
    return envs, errors
