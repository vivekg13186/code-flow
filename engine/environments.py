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
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

_ENVIRONMENTS_DIR: Path = Path("environments")

#: keys whose values are masked in the UI / reports / persisted run records
_SECRET_KEY_RE = re.compile(r"secret|token|passw|api_?key|credential|private", re.I)
#: ${VAR} references resolved from the server's OS environment at load time
_REF_RE = re.compile(r"\$\{(\w+)\}")
MASK = "••••••"


def set_environments_dir(folder: str | Path) -> None:
    global _ENVIRONMENTS_DIR
    _ENVIRONMENTS_DIR = Path(folder)


def get_environments_dir() -> Path:
    return _ENVIRONMENTS_DIR


def _resolve(value: Any, refs: List[str], missing: List[str]) -> Any:
    """Recursively substitute ${VAR} references from os.environ."""
    if isinstance(value, str):
        def sub(m):
            name = m.group(1)
            refs.append(name)
            if name in os.environ:
                return os.environ[name]
            missing.append(name)
            return m.group(0)  # leave the placeholder visible
        return _REF_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _resolve(v, refs, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, refs, missing) for v in value]
    return value


def is_secret_key(name: str) -> bool:
    """True if a key name looks like it holds a secret (used for masking)."""
    return bool(_SECRET_KEY_RE.search(name))


def secret_keys(values: Dict[str, Any]) -> Set[str]:
    """Keys that must be masked: secret-ish names + ${VAR}-resolved ones."""
    keys = set(values.get("__secrets__", []))
    for k in values:
        if k != "__secrets__" and _SECRET_KEY_RE.search(k):
            keys.add(k)
    return keys


def mask_env(values: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of an env dict safe for display/persistence: secrets masked,
    bookkeeping keys stripped."""
    secrets = secret_keys(values)
    return {
        k: (MASK if k in secrets else v)
        for k, v in values.items()
        if k != "__secrets__"
    }


def load_environments(folder: str | Path | None = None) -> Tuple[Dict[str, dict], List[dict]]:
    """Return ({env_name: values}, [errors/warnings]) from the environments
    folder. ${VAR} references are resolved from the OS environment; keys whose
    value contained a reference are tracked in values["__secrets__"] so they
    get masked everywhere they are displayed or persisted."""
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
            resolved: Dict[str, Any] = {}
            ref_keys: List[str] = []
            missing: List[str] = []
            for k, v in data.items():
                refs: List[str] = []
                resolved[k] = _resolve(v, refs, missing)
                if refs:
                    ref_keys.append(k)
            if ref_keys:
                resolved["__secrets__"] = sorted(ref_keys)
            if missing:
                errors.append({
                    "file": f.name,
                    "error": "warning: OS env var(s) not set: " + ", ".join(sorted(set(missing))),
                })
            envs[f.stem] = resolved
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": f.name, "error": f"{type(exc).__name__}: {exc}"})
    return envs, errors
