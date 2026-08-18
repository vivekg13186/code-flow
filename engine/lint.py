"""codeflow lint — static checks for flow files.

    python -m engine.lint [path] [--strict] [--json]

Catches mistakes that otherwise surface at runtime (a duplicate step name
silently overriding another, leftover graph-era syntax) plus the reliability
habits that are easy to forget (timeout on retried calls, secrets in logs,
non-determinism in a flow body, import-time side effects).

Exit code 1 if any ERROR is found (or any WARN with --strict).
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

RULES = {
    "CF001": "duplicate step name — the later definition silently wins",
    "CF002": "workflow has no @flow method — it will not be discovered",
    "CF003": "workflow has more than one @flow method",
    "CF004": "removed graph-era argument — use Python control flow instead",
    "CF005": "@start/@wait were removed — put the logic in the @flow body",
    "CF010": "retry= without timeout= — a hung call blocks a worker forever",
    "CF011": "exception swallowed inside a step — failures vanish from the report",
    "CF012": "possible secret in a log line — log values are not masked",
    "CF013": "call at import time — flow files are re-imported on every scan",
    "CF014": "workflow has no description",
    "CF020": "inputs_schema was removed — declare a @dataclass and set inputs = It",
    "CF021": "inputs is a class but not a @dataclass — add the decorator",
    "CF030": "non-determinism in a @flow body — resume replays the body",
    "CF031": "side effect in a @flow body — it re-runs on resume; use a @step",
}

SECRETISH = ("token", "secret", "password", "passwd", "api_key", "apikey",
             "credential", "private")
NONDET = ("datetime.now", "datetime.utcnow", "date.today", "time.time",
          "uuid.uuid4", "uuid.uuid1", "random.random", "random.randint",
          "random.choice", "random.shuffle", "random.sample")
BODY_SIDE_EFFECTS = ("open", "os.remove", "os.unlink", "os.mkdir", "shutil.rmtree",
                     "write_text", "write_bytes", "unlink", "mkdir",
                     "urlopen", "requests.get", "requests.post", "subprocess.run")
IMPORT_TIME = ("session", "connect", "create_engine", "client", "open",
               "request", "urlopen", "run", "popen", "write_text", "write_bytes")
GRAPH_ARGS = {"next", "condition", "loop", "resumable", "parallel", "seconds"}


class Finding:
    def __init__(self, path: Path, line: int, rule: str, detail: str = "",
                 level: str = "ERROR"):
        self.path, self.line, self.rule = path, line, rule
        self.detail, self.level = detail, level

    def __str__(self) -> str:
        extra = f" ({self.detail})" if self.detail else ""
        return f"{self.path}:{self.line}: {self.level} {self.rule} {RULES.get(self.rule, '')}{extra}"

    def to_dict(self) -> Dict[str, Any]:
        return {"file": str(self.path), "line": self.line, "rule": self.rule,
                "level": self.level, "message": RULES.get(self.rule, ""),
                "detail": self.detail}


# ----------------------------------------------------------------- helpers
def _deco_name(node: ast.expr) -> str:
    f = node.func if isinstance(node, ast.Call) else node
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _dotted(node: ast.expr) -> str:
    """datetime.datetime.now() -> 'datetime.datetime.now'."""
    f = node.func if isinstance(node, ast.Call) else node
    parts: List[str] = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def _kwargs_of(node: ast.expr) -> Dict[str, ast.expr]:
    if not isinstance(node, ast.Call):
        return {}
    return {k.arg: k.value for k in node.keywords if k.arg}


def _const(node: Optional[ast.expr]) -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def _secret_key_in(node: ast.AST) -> Optional[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript):
            key = _const(sub.slice)
            if isinstance(key, str) and any(s in key.lower() for s in SECRETISH):
                return key
        if isinstance(sub, ast.Call) and _deco_name(sub) in ("get", "secret"):
            for a in sub.args:
                key = _const(a)
                if isinstance(key, str) and any(s in key.lower() for s in SECRETISH):
                    return key
    return None


# -------------------------------------------------------------------- lint
def lint_file(path: Path) -> List[Finding]:
    out: List[Finding] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 1, "CF000", str(exc.msg))]

    for node in tree.body:                      # CF013 import-time work
        value = (node.value if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign))
                 else None)
        if isinstance(value, ast.Call):
            name = _deco_name(value).lower()
            if any(k == name for k in IMPORT_TIME):
                out.append(Finding(path, node.lineno, "CF013",
                                   _dotted(value) + "()", "WARN"))

    # {class name: is it decorated with @dataclass} — for CF021
    dataclasses_seen = {
        n.name: any(_deco_name(d) == "dataclass" for d in n.decorator_list)
        for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
    }

    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        out.extend(_lint_class(path, cls, dataclasses_seen))
    return out


def _lint_class(path: Path, cls: ast.ClassDef,
                DATACLASSES: Optional[Dict[str, bool]] = None) -> List[Finding]:
    DATACLASSES = DATACLASSES or {}
    out: List[Finding] = []
    steps: Dict[str, Dict[str, Any]] = {}
    entries: List[ast.FunctionDef] = []
    legacy: List[ast.FunctionDef] = []

    for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
        for deco in fn.decorator_list:
            kind = _deco_name(deco)
            if kind == "flow":
                entries.append(fn)
            elif kind in ("start", "wait"):
                legacy.append(fn)
                out.append(Finding(path, fn.lineno, "CF005", f"@{kind}"))
            elif kind == "step":
                kw = _kwargs_of(deco)
                name = _const(kw.get("name")) or fn.name
                if name in steps:
                    out.append(Finding(path, fn.lineno, "CF001", name))
                steps[name] = {"line": fn.lineno, "kw": kw, "fn": fn}
                for arg in GRAPH_ARGS & set(kw):
                    out.append(Finding(path, fn.lineno, "CF004", f"{name}: {arg}="))

    if not steps and not entries and not legacy:
        return []                               # not a workflow class

    assigns = {}
    for n in cls.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    assigns[t.id] = n
    if "description" not in assigns:
        out.append(Finding(path, cls.lineno, "CF014", cls.name, "WARN"))
    if "inputs_schema" in assigns:
        out.append(Finding(path, assigns["inputs_schema"].lineno, "CF020", cls.name))
    node = assigns.get("inputs")
    value = getattr(node, "value", None)
    if isinstance(value, ast.Name) and value.id in DATACLASSES:
        if not DATACLASSES[value.id]:
            out.append(Finding(path, node.lineno, "CF021", value.id))
    if not entries:
        out.append(Finding(path, cls.lineno, "CF002", cls.name))
    elif len(entries) > 1:
        out.append(Finding(path, entries[1].lineno, "CF003", cls.name))

    step_names = {s["fn"].name for s in steps.values()}
    for entry in entries:                       # CF030 / CF031 — body purity
        for sub in ast.walk(entry):
            if not isinstance(sub, ast.Call):
                continue
            dotted = _dotted(sub)
            if any(dotted == n or dotted.endswith("." + n) for n in NONDET):
                out.append(Finding(path, sub.lineno, "CF030", dotted + "()"))
                continue
            called = _deco_name(sub)
            is_self_step = (isinstance(sub.func, ast.Attribute)
                            and isinstance(sub.func.value, ast.Name)
                            and sub.func.value.id == "self"
                            and called in step_names)
            if is_self_step:
                continue
            if any(dotted == n or dotted.endswith("." + n) or called == n
                   for n in BODY_SIDE_EFFECTS):
                out.append(Finding(path, sub.lineno, "CF031", dotted + "()", "WARN"))

    for name, s in steps.items():               # per-step rules
        kw, fn = s["kw"], s["fn"]
        if (_const(kw.get("retry")) or 0) and kw.get("timeout") is None:
            out.append(Finding(path, s["line"], "CF010", name, "WARN"))
        for sub in ast.walk(fn):
            if isinstance(sub, ast.ExceptHandler):
                if not [b for b in sub.body if not isinstance(b, ast.Pass)]:
                    out.append(Finding(path, sub.lineno, "CF011", name))
            if isinstance(sub, ast.Call) and _deco_name(sub) in (
                    "log", "log_json", "log_table"):
                for a in list(sub.args) + [k.value for k in sub.keywords]:
                    key = _secret_key_in(a)
                    if key:
                        out.append(Finding(path, sub.lineno, "CF012", key))
    return out


def lint_path(target: Path) -> List[Finding]:
    files = ([target] if target.is_file()
             else [p for p in sorted(target.rglob("*.py"))
                   if not any(part.startswith("_") or part == "__pycache__"
                              for part in p.relative_to(target).parts[:-1])
                   and not p.name.startswith("_")])
    findings: List[Finding] = []
    for f in files:
        findings.extend(lint_file(f))
    return findings


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="codeflow lint")
    ap.add_argument("path", nargs="?", default="workflows")
    ap.add_argument("--strict", action="store_true", help="fail on warnings too")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    findings = lint_path(Path(args.path))
    errors = [f for f in findings if f.level == "ERROR"]
    warns = [f for f in findings if f.level == "WARN"]
    if args.as_json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        for f in sorted(findings, key=lambda x: (str(x.path), x.line)):
            print(f)
        print(f"\n{len(errors)} error(s), {len(warns)} warning(s) in {args.path}")
    return 1 if errors or (args.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
