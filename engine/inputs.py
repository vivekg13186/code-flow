"""Typed inputs: declare them as a dataclass on the workflow class.

    from dataclasses import dataclass, field
    from typing import Literal, Optional

    @dataclass
    class OrderInputs:
        customer: str                                    # required
        amount: float = field(default=10.0,
                              metadata={"min": 1, "help": "order total in EUR"})
        priority: Literal["low", "normal", "high"] = "normal"
        notify: bool = False
        extra: Optional[dict] = None

    class OrderFlow(Workflow):
        inputs = OrderInputs        # <- the class itself, not an instance

``inputs`` may instead be a plain dict of defaults when you don't want
typing; that stays supported and simply skips validation.

Annotation -> field type:
    str -> string · int -> integer · float -> number · bool -> boolean
    Literal[...] / Enum subclass -> select · list/dict/Any/other -> json
    Optional[X] -> X, not required
A field with no default is required. Extras that annotations can't express
go in ``field(metadata={...})``: min, max, help, label, options, required,
type (to override the inferred one). ``Annotated[int, {"min": 1}]`` works too.

The UI renders a form from the derived schema; the API returns 422 with
per-field errors; the runner validates again at execution time (covers
scheduler, webhook and sub-workflow starts). Keys not declared pass through
untouched, and the flow body still receives a plain dict ``ctx``.
"""
from __future__ import annotations

import dataclasses
import enum
import json
import typing
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
                if isinstance(v, enum.Enum):     # accept the member, store the value
                    v = v.value
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


# --------------------------------------------------------------- dataclass

_MISSING = dataclasses.MISSING


def is_inputs_dataclass(obj: Any) -> bool:
    """True when a workflow's ``inputs`` is a dataclass TYPE (not instance)."""
    return isinstance(obj, type) and dataclasses.is_dataclass(obj)


def _unwrap_optional(tp: Any) -> Tuple[Any, bool]:
    """Optional[X] / Union[X, None] -> (X, True). Returns (tp, False) otherwise."""
    origin = typing.get_origin(tp)
    if origin is typing.Union or str(origin) == "types.UnionType":
        args = [a for a in typing.get_args(tp) if a is not type(None)]  # noqa: E721
        if len(args) != len(typing.get_args(tp)):
            return (args[0] if len(args) == 1 else Any), True
    return tp, False


def _spec_from_type(tp: Any) -> Dict[str, Any]:
    """Map a type annotation to a field spec ({"type": ..., "options": ...})."""
    extra: Dict[str, Any] = {}
    if typing.get_origin(tp) is typing.Annotated:
        args = typing.get_args(tp)
        tp = args[0]
        for meta in args[1:]:
            if isinstance(meta, dict):
                extra.update(meta)

    tp, optional = _unwrap_optional(tp)
    if optional:
        extra.setdefault("required", False)

    if typing.get_origin(tp) is typing.Literal:
        options = list(typing.get_args(tp))
        return {"type": "select", "options": options, **extra}
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        # the form/ctx carry the raw .value, so results stay JSON-serialisable
        return {"type": "select", "options": [m.value for m in tp], **extra}
    # bool first: bool is a subclass of int
    if tp is bool:
        return {"type": "boolean", **extra}
    if tp is int:
        return {"type": "integer", **extra}
    if tp is float:
        return {"type": "number", **extra}
    if tp is str:
        return {"type": "string", **extra}
    return {"type": "json", **extra}


def schema_from_dataclass(dc: type) -> Dict[str, Dict[str, Any]]:
    """Derive an inputs schema from a dataclass's fields and annotations."""
    try:
        hints = typing.get_type_hints(dc, include_extras=True)
    except Exception as exc:  # noqa: BLE001 — unresolvable forward reference
        raise TypeError(
            f"{dc.__name__}: cannot resolve type hints ({exc}). Make sure every "
            "annotation refers to a name importable at module level."
        ) from exc

    schema: Dict[str, Dict[str, Any]] = {}
    for f in dataclasses.fields(dc):
        if not f.init:
            continue
        spec = _spec_from_type(hints.get(f.name, f.type))
        if f.default is not _MISSING:
            spec["default"] = f.default
        elif f.default_factory is not _MISSING:      # type: ignore[misc]
            try:
                spec["default"] = f.default_factory()  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                pass
        else:
            spec.setdefault("required", True)
        # field(metadata=...) wins over anything inferred
        spec.update({k: v for k, v in dict(f.metadata).items() if v is not None})
        if isinstance(spec.get("default"), enum.Enum):
            # ctx and the journal must stay JSON-serialisable
            spec["default"] = spec["default"].value
        if spec.get("type") not in VALID_TYPES:
            raise TypeError(f"{dc.__name__}.{f.name}: unknown type {spec.get('type')!r}")
        schema[f.name] = spec
    return schema


def schema_for(cls) -> Dict[str, Dict[str, Any]]:
    """The schema for a workflow class — {} when inputs is a plain dict."""
    declared = getattr(cls, "inputs", None)
    if not is_inputs_dataclass(declared):
        return {}
    cached = cls.__dict__.get("_derived_schema")
    if cached is not None and cached[0] is declared:
        return cached[1]
    schema = schema_from_dataclass(declared)
    try:
        cls._derived_schema = (declared, schema)
    except Exception:  # noqa: BLE001 — e.g. __slots__
        pass
    return schema


def defaults_for(cls) -> Dict[str, Any]:
    """Default inputs — from the dataclass fields, or the plain dict."""
    declared = getattr(cls, "inputs", None)
    if is_inputs_dataclass(declared):
        return {n: spec["default"] for n, spec in schema_for(cls).items()
                if "default" in spec}
    return dict(declared or {})


def validate_for_class(cls, inputs: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Merge class default inputs with per-run inputs, then apply the schema."""
    merged = {**defaults_for(cls), **(inputs or {})}
    schema = schema_for(cls)
    if not schema:
        return merged, {}
    return apply_schema(schema, merged)
