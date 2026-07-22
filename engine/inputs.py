"""Typed inputs: optional per-flow schema validation and coercion.

Declare on the workflow class::

    class OrderFlow(Workflow):
        inputs_schema = {
            "amount":   {"type": "number", "min": 1, "required": True,
                         "help": "order total in EUR"},
            "customer": {"type": "string", "required": True},
            "priority": {"type": "select", "options": ["low", "normal", "high"],
                         "default": "normal"},
            "notify":   {"type": "boolean", "default": False},
            "extra":    {"type": "json"},
        }

Types: string | number | integer | boolean | select | json.
Spec keys: required, default, min/max (numbers), options (select),
help (shown in the run form), label (display name).

The UI renders a form from the schema; the API returns 422 with per-field
errors; the runner validates again at execution time (covers scheduler,
webhook and sub-workflow starts). Keys not in the schema pass through
untouched.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Tuple

VALID_TYPES = {"string", "number", "integer", "boolean", "select", "json"}


def apply_schema(schema: Dict[str, Dict[str, Any]],
                 inputs: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Return (cleaned_inputs, errors). Coerces string forms of numbers,
    booleans and JSON; fills defaults; checks required/min/max/options."""
    cleaned = dict(inputs or {})
    errors: Dict[str, str] = {}
    for name, spec in (schema or {}).items():
        typ = spec.get("type", "string")
        missing = name not in cleaned or cleaned[name] is None or cleaned[name] == ""
        if missing:
            if "default" in spec:
                cleaned[name] = spec["default"]
            elif spec.get("required"):
                errors[name] = "required"
            continue
        v = cleaned[name]
        try:
            if typ in ("number", "integer"):
                if isinstance(v, bool):
                    raise ValueError("must be a number")
                if isinstance(v, str):
                    v = float(v.strip())
                if not isinstance(v, (int, float)):
                    raise ValueError("must be a number")
                if typ == "integer":
                    if float(v) != int(float(v)):
                        raise ValueError("must be an integer")
                    v = int(v)
                if "min" in spec and v < spec["min"]:
                    raise ValueError(f"must be >= {spec['min']}")
                if "max" in spec and v > spec["max"]:
                    raise ValueError(f"must be <= {spec['max']}")
            elif typ == "boolean":
                if isinstance(v, str):
                    v = v.strip().lower() in ("1", "true", "yes", "on")
                v = bool(v)
            elif typ == "select":
                options = spec.get("options") or []
                if v not in options:
                    raise ValueError(f"must be one of {options}")
            elif typ == "json":
                if isinstance(v, str) and v.strip():
                    v = json.loads(v)
            elif typ == "string":
                v = str(v)
            cleaned[name] = v
        except json.JSONDecodeError:
            errors[name] = "invalid JSON"
        except (ValueError, TypeError) as exc:
            errors[name] = str(exc) or "invalid"
    return cleaned, errors


def validate_for_class(cls, inputs: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Merge class default inputs with per-run inputs, then apply the schema."""
    merged = {**(getattr(cls, "inputs", {}) or {}), **(inputs or {})}
    schema = getattr(cls, "inputs_schema", None) or {}
    if not schema:
        return merged, {}
    return apply_schema(schema, merged)
