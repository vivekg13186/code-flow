"""Discovers workflow classes in the workflows/ folder.

Every ``*.py`` file in the folder — including subfolders, any depth — is
imported (fresh on each call, so edits are picked up without restarting the
server) and every subclass of ``Workflow`` with a ``@start`` step is
registered. Files or folders whose name starts with ``_`` (and
``__pycache__``) are skipped.

Subfolders double as organization: each folder on a flow's path is added to
the flow's tags automatically, so ``workflows/billing/Invoices.py`` gets a
``billing`` tag and can be grouped/filtered in the UI without declaring
anything.
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Dict, List, Tuple, Type

from .workflow import Workflow

# default folder scanned when code (e.g. Workflow.call_workflow) needs the
# registry without being handed a path; the app overrides this at startup.
_WORKFLOWS_DIR: Path = Path("workflows")


def set_workflows_dir(folder: str | Path) -> None:
    global _WORKFLOWS_DIR
    _WORKFLOWS_DIR = Path(folder)


def get_workflows_dir() -> Path:
    return _WORKFLOWS_DIR


def discover_workflows(folder: str | Path) -> Tuple[Dict[str, Type[Workflow]], List[dict]]:
    """Return ({workflow_name: class}, [load_errors])."""
    folder = Path(folder)
    registry: Dict[str, Type[Workflow]] = {}
    errors: List[dict] = []
    if not folder.is_dir():
        return registry, errors

    # make the workflows root importable so flow files can import each other
    # and shared helper modules:  `from _lib.helpers import x`  or
    # `from billing.common import y`  (subfolders work as namespace packages)
    folder_str = str(folder.resolve())
    if folder_str not in sys.path:
        sys.path.insert(0, folder_str)

    for py_file in sorted(folder.rglob("*.py")):
        rel = py_file.relative_to(folder)
        if any(part.startswith("_") for part in rel.parts):  # _foo.py, _drafts/, __pycache__/
            continue
        module_name = "codeflow_workflows." + ".".join(rel.with_suffix("").parts)
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            sys.modules[module_name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception:
            errors.append({"file": py_file.name, "error": traceback.format_exc()})
            continue

        folder_tags = [p for p in rel.parent.parts]  # subfolders become tags
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, Workflow)
                and attr is not Workflow
                and attr.is_workflow()
            ):
                name = getattr(attr, "name_override", None) or attr.__name__
                attr._source_file = str(rel)
                attr.tags = sorted(set(getattr(attr, "tags", []) or []) | set(folder_tags))
                registry[name] = attr
    return registry, errors


def workflow_summary(cls: Type[Workflow]) -> dict:
    """JSON-friendly description of a workflow class for the UI."""
    steps = cls.collect_steps()
    return {
        "name": getattr(cls, "name_override", None) or cls.__name__,
        "description": (getattr(cls, "description", "") or (cls.__doc__ or "")).strip(),
        "tags": sorted(getattr(cls, "tags", []) or []),
        "file": getattr(cls, "_source_file", "?"),
        "inputs": dict(getattr(cls, "inputs", {}) or {}),
        "start": cls.start_step(),
        "steps": [
            {
                "name": meta["name"],
                "next": meta.get("next"),
                "condition": meta.get("condition"),
                "loop": meta.get("loop"),
                "retry": meta.get("retry", 0),
                "retry_delay": meta.get("retry_delay", 0),
                "retry_on": [c.__name__ for c in meta["retry_on"]] if meta.get("retry_on") else None,
                "continue_on_error": meta.get("continue_on_error", False),
                "timeout": meta.get("timeout"),
                "is_start": meta.get("is_start", False),
                "doc": meta.get("doc", ""),
            }
            for meta in steps.values()
        ],
    }
