"""Standard step library — batteries for the things almost every flow does.

Reach them as namespaces on the workflow; each call is a real journaled step,
so it shows up in the report with its arguments and result, obeys retries and
timeouts, and is skipped on a resume that already got past it::

    class NightlyImport(Workflow):
        @flow
        def main(self, ctx):
            orders = self.http.get(ctx["api"] + "/orders")["json"]
            self.fs.write_json("orders.json", orders)
            self.db.executemany("ops.db",
                "INSERT INTO orders(id, total) VALUES (?, ?)",
                [(o["id"], o["total"]) for o in orders])
            self.sh.run("./reconcile.sh --today")

Namespaces: ``self.http`` · ``self.fs`` · ``self.sh`` · ``self.db``.

Two rules the library follows so resume keeps working, and which your own
steps should follow too:

1. **Every return value is JSON-serializable.** The journal stores it and
   restores it verbatim on resume, so nothing returns a file handle, a
   connection or a response object.
2. **No argument is a callable.** A function's ``repr()`` contains its memory
   address, which changes every process — a step keyed on one would never
   match its journal entry, and resume would silently re-run it.

Large payloads are truncated before they reach the journal (see MAX_* below);
the step's report entry says so when it happens. If you are moving more data
than that, do the work inside one of your own steps and return a summary
rather than pulling it through the flow body.
"""
from __future__ import annotations

import csv
import glob as _glob
import io
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from .decorators import step

# --- caps that keep the journal and the resume sidecar a sane size ----------
MAX_TEXT = 200_000        # chars of an HTTP body / file kept in a result
MAX_OUTPUT = 100_000      # chars of stdout / stderr kept from a command
MAX_ROWS = 50_000         # rows returned by read_csv / db.query

# requests raises its own ConnectionError/Timeout that do NOT subclass the
# builtins, so retry_on has to name the real classes to be effective.
try:  # pragma: no cover - requests is in requirements.txt
    import requests as _requests
    _NET_ERRORS: tuple = (_requests.exceptions.ConnectionError,
                          _requests.exceptions.Timeout,
                          _requests.exceptions.ChunkedEncodingError,
                          ConnectionError, TimeoutError)
except Exception:  # noqa: BLE001
    _requests = None
    _NET_ERRORS = (ConnectionError, TimeoutError)


class HttpError(RuntimeError):
    """Raised for a non-2xx response when check=True."""

    def __init__(self, status: int, url: str, body: str = ""):
        self.status, self.url, self.body = status, url, body
        super().__init__(f"HTTP {status} for {url}"
                         + (f" — {body[:300]}" if body else ""))


class HttpServerError(HttpError):
    """5xx or 429 — transient, so the library's retry_on includes it."""


class HttpClientError(HttpError):
    """4xx (except 429) — your request is wrong, retrying won't fix it."""


class ShellError(RuntimeError):
    def __init__(self, cmd: str, returncode: int, stderr: str = ""):
        self.cmd, self.returncode, self.stderr = cmd, returncode, stderr
        super().__init__(f"command exited {returncode}: {cmd}"
                         + (f"\n{stderr[-2000:]}" if stderr else ""))


_RETRYABLE = _NET_ERRORS + (HttpServerError,)


def _truncate(text: str, cap: int) -> tuple:
    if text is None:
        return None, False
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _p(path: Union[str, Path]) -> Path:
    """Expand ~ and env vars; leave relative paths relative to the cwd."""
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


class _Namespace:
    """Base for the step namespaces.

    The ``@step`` wrapper looks for ``_program_runner`` on the object it is
    called against, so exposing the workflow's runner here is all it takes for
    ``self.http.get(...)`` to be journaled exactly like a step you wrote.
    """

    __slots__ = ("_wf",)

    def __init__(self, wf):
        self._wf = wf

    @property
    def _program_runner(self):
        return getattr(self._wf, "_program_runner", None)

    # convenience passthroughs so library steps can log like any other step
    def log(self, message: Any) -> None:
        self._wf.log(message)

    @property
    def env(self) -> Dict[str, Any]:
        return self._wf.env


# =========================================================================
# http
# =========================================================================
class HttpSteps(_Namespace):
    """HTTP calls that return a plain dict.

    Every method returns::

        {"status": 200, "ok": True, "url": "...", "method": "GET",
         "headers": {...}, "json": <parsed or None>, "text": "...",
         "truncated": False, "elapsed_ms": 41.2}

    ``json`` is the decoded body when the response parses as JSON, else None —
    so ``self.http.get(url)["json"]`` is the common path and ``["text"]`` is
    there when you need the raw body.

    Defaults: 30s connect/read timeout, 2 retries with 1s/2s backoff on
    network errors and 5xx/429, and ``check=True`` which raises HttpError on
    any other non-2xx. A 4xx raises immediately without burning retries.
    """

    def _do(self, method: str, url: str, *, params=None, json_body=None,
            data=None, headers=None, timeout=30.0, check=True,
            allow_redirects=True) -> Dict[str, Any]:
        if _requests is None:  # pragma: no cover
            raise RuntimeError("the http namespace needs `requests` "
                               "(pip install -r requirements.txt)")
        t0 = time.monotonic()
        resp = _requests.request(method, url, params=params, json=json_body,
                                 data=data, headers=headers, timeout=timeout,
                                 allow_redirects=allow_redirects)
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        text, truncated = _truncate(resp.text, MAX_TEXT)
        try:
            parsed = resp.json()
        except Exception:  # noqa: BLE001 - not JSON, that's fine
            parsed = None

        self.log(f"{method} {url} -> {resp.status_code} ({elapsed} ms, "
                 f"{len(resp.content)} bytes)")
        if check and not resp.ok:
            cls = (HttpServerError if resp.status_code >= 500
                   or resp.status_code == 429 else HttpClientError)
            raise cls(resp.status_code, url, resp.text[:2000])
        return {"status": resp.status_code, "ok": resp.ok, "url": url,
                "method": method, "headers": dict(resp.headers),
                "json": parsed, "text": text, "truncated": truncated,
                "elapsed_ms": elapsed}

    @step(name="http.get", retry=2, retry_delay=1, retry_backoff=2,
          retry_on=_RETRYABLE, timeout=120)
    def get(self, url: str, params: Optional[dict] = None,
            headers: Optional[dict] = None, timeout: float = 30.0,
            check: bool = True) -> Dict[str, Any]:
        """GET a URL."""
        return self._do("GET", url, params=params, headers=headers,
                        timeout=timeout, check=check)

    @step(name="http.post", retry=2, retry_delay=1, retry_backoff=2,
          retry_on=_RETRYABLE, timeout=120)
    def post(self, url: str, json: Optional[Any] = None, data: Any = None,
             headers: Optional[dict] = None, timeout: float = 30.0,
             check: bool = True) -> Dict[str, Any]:
        """POST JSON (or form ``data``) to a URL.

        Retries: only safe if the endpoint is idempotent. Pass a request id /
        idempotency key, or drop to ``self.http.request("POST", ...)`` which
        does not retry.
        """
        return self._do("POST", url, json_body=json, data=data,
                        headers=headers, timeout=timeout, check=check)

    @step(name="http.put", retry=2, retry_delay=1, retry_backoff=2,
          retry_on=_RETRYABLE, timeout=120)
    def put(self, url: str, json: Optional[Any] = None, data: Any = None,
            headers: Optional[dict] = None, timeout: float = 30.0,
            check: bool = True) -> Dict[str, Any]:
        """PUT JSON (or form ``data``) to a URL."""
        return self._do("PUT", url, json_body=json, data=data,
                        headers=headers, timeout=timeout, check=check)

    @step(name="http.patch", retry=2, retry_delay=1, retry_backoff=2,
          retry_on=_RETRYABLE, timeout=120)
    def patch(self, url: str, json: Optional[Any] = None, data: Any = None,
              headers: Optional[dict] = None, timeout: float = 30.0,
              check: bool = True) -> Dict[str, Any]:
        """PATCH a URL."""
        return self._do("PATCH", url, json_body=json, data=data,
                        headers=headers, timeout=timeout, check=check)

    @step(name="http.delete", retry=2, retry_delay=1, retry_backoff=2,
          retry_on=_RETRYABLE, timeout=120)
    def delete(self, url: str, headers: Optional[dict] = None,
               timeout: float = 30.0, check: bool = True) -> Dict[str, Any]:
        """DELETE a URL."""
        return self._do("DELETE", url, headers=headers, timeout=timeout,
                        check=check)

    @step(name="http.request", timeout=120)
    def request(self, method: str, url: str, params: Optional[dict] = None,
                json: Optional[Any] = None, data: Any = None,
                headers: Optional[dict] = None, timeout: float = 30.0,
                check: bool = True) -> Dict[str, Any]:
        """Any method, **no retries** — for calls that must happen at most once."""
        return self._do(method.upper(), url, params=params, json_body=json,
                        data=data, headers=headers, timeout=timeout, check=check)

    @step(name="http.download", retry=2, retry_delay=1, retry_backoff=2,
          retry_on=_RETRYABLE, timeout=600)
    def download(self, url: str, dest: str, headers: Optional[dict] = None,
                 timeout: float = 60.0, chunk_size: int = 65536) -> Dict[str, Any]:
        """Stream a URL to a file. Returns ``{path, bytes, status}``.

        Writes to ``<dest>.part`` and renames on success, so a failed or
        abandoned attempt never leaves a half file at the real path.
        """
        if _requests is None:  # pragma: no cover
            raise RuntimeError("the http namespace needs `requests`")
        target = _p(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        total = 0
        with _requests.get(url, headers=headers, timeout=timeout,
                           stream=True) as resp:
            if not resp.ok:
                cls = (HttpServerError if resp.status_code >= 500
                       or resp.status_code == 429 else HttpClientError)
                raise cls(resp.status_code, url)
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(chunk_size):
                    if chunk:
                        fh.write(chunk)
                        total += len(chunk)
            status = resp.status_code
        part.replace(target)
        self.log(f"downloaded {url} -> {target} ({total} bytes)")
        return {"path": str(target), "bytes": total, "status": status}


# =========================================================================
# fs
# =========================================================================
class FsSteps(_Namespace):
    """Files: text, JSON, CSV, YAML, globbing, copy/move/remove, archives.

    Reads are journaled like everything else, which means a resumed run gets
    the *recorded* content rather than re-reading the file — that is what
    makes replay consistent. It also means the content sits in the journal, so
    ``read_csv`` and ``read_text`` cap out (see MAX_ROWS / MAX_TEXT). For
    genuinely large files, read and reduce them inside one of your own steps
    and return a summary.
    """

    # ---------------------------------------------------------- reading
    @step(name="fs.read_text", timeout=60)
    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Read a whole text file (truncated at MAX_TEXT chars)."""
        text = _p(path).read_text(encoding=encoding)
        out, truncated = _truncate(text, MAX_TEXT)
        self.log(f"read {path} ({len(text)} chars"
                 + (", truncated" if truncated else "") + ")")
        return out

    @step(name="fs.read_json", timeout=60)
    def read_json(self, path: str, encoding: str = "utf-8",
                  dirty: bool = False) -> Any:
        """Parse a JSON file.

        ``dirty=True`` falls back to :mod:`dirtyjson` for files with trailing
        commas, comments or single quotes — the kind of JSON that comes out of
        a hand-edited config.
        """
        raw = _p(path).read_text(encoding=encoding)
        try:
            data = json.loads(raw)
        except ValueError:
            if not dirty:
                raise
            import dirtyjson
            data = json.loads(json.dumps(dirtyjson.loads(raw)))
        self.log(f"parsed {path}")
        return data

    @step(name="fs.read_csv", timeout=120)
    def read_csv(self, path: str, encoding: str = "utf-8",
                 delimiter: str = ",", limit: Optional[int] = None) -> List[dict]:
        """Read a CSV into a list of dicts, keyed by the header row."""
        rows: List[dict] = []
        cap = min(limit or MAX_ROWS, MAX_ROWS)
        with open(_p(path), newline="", encoding=encoding) as fh:
            for i, row in enumerate(csv.DictReader(fh, delimiter=delimiter)):
                if i >= cap:
                    self.log(f"stopped at {cap} rows (cap)")
                    break
                rows.append(dict(row))
        self.log(f"read {len(rows)} rows from {path}")
        return rows

    @step(name="fs.read_yaml", timeout=60)
    def read_yaml(self, path: str, encoding: str = "utf-8") -> Any:
        """Parse a YAML file (safe_load)."""
        import yaml
        data = yaml.safe_load(_p(path).read_text(encoding=encoding))
        self.log(f"parsed {path}")
        return data

    @step(name="fs.glob", timeout=60)
    def glob(self, pattern: str, recursive: bool = True) -> List[str]:
        """Paths matching a glob, sorted. ``**`` needs recursive=True."""
        found = sorted(_glob.glob(os.path.expanduser(pattern),
                                  recursive=recursive))
        self.log(f"{len(found)} match(es) for {pattern}")
        return found

    @step(name="fs.stat", timeout=30)
    def stat(self, path: str) -> Dict[str, Any]:
        """``{exists, path, bytes, modified, is_dir}`` — never raises for a
        missing path, so it is the way to branch on existence."""
        p = _p(path)
        if not p.exists():
            return {"exists": False, "path": str(p), "bytes": None,
                    "modified": None, "is_dir": False}
        st = p.stat()
        return {"exists": True, "path": str(p), "bytes": st.st_size,
                "modified": time.strftime("%Y-%m-%dT%H:%M:%S",
                                          time.localtime(st.st_mtime)),
                "is_dir": p.is_dir()}

    # ---------------------------------------------------------- writing
    @step(name="fs.write_text", timeout=60)
    def write_text(self, path: str, content: str,
                   encoding: str = "utf-8", append: bool = False) -> str:
        """Write text, creating parent folders. Returns the path."""
        p = _p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding=encoding) as fh:
            fh.write(content)
        self.log(f"{'appended to' if append else 'wrote'} {p} "
                 f"({len(content)} chars)")
        return str(p)

    @step(name="fs.write_json", timeout=60)
    def write_json(self, path: str, data: Any, indent: int = 2,
                   encoding: str = "utf-8") -> str:
        """Serialize to JSON, creating parent folders. Returns the path."""
        p = _p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=indent, default=str),
                     encoding=encoding)
        self.log(f"wrote {p}")
        return str(p)

    @step(name="fs.write_csv", timeout=120)
    def write_csv(self, path: str, rows: Sequence[dict],
                  columns: Optional[Sequence[str]] = None,
                  encoding: str = "utf-8", delimiter: str = ",") -> str:
        """Write dicts to CSV. Columns default to the first row's keys."""
        p = _p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = list(rows)
        cols = list(columns) if columns else (list(rows[0].keys()) if rows else [])
        with open(p, "w", newline="", encoding=encoding) as fh:
            writer = csv.DictWriter(fh, fieldnames=cols, delimiter=delimiter,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        self.log(f"wrote {len(rows)} rows to {p}")
        return str(p)

    # ---------------------------------------------------------- moving
    @step(name="fs.ensure_dir", timeout=30)
    def ensure_dir(self, path: str) -> str:
        """mkdir -p. Returns the path."""
        p = _p(path)
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    @step(name="fs.copy", timeout=300)
    def copy(self, src: str, dest: str, overwrite: bool = False) -> str:
        """Copy a file (metadata preserved). Returns the destination."""
        s, d = _p(src), _p(dest)
        if d.is_dir():
            d = d / s.name
        if d.exists() and not overwrite:
            raise FileExistsError(f"{d} exists (pass overwrite=True)")
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        self.log(f"copied {s} -> {d}")
        return str(d)

    @step(name="fs.move", timeout=300)
    def move(self, src: str, dest: str, overwrite: bool = False) -> str:
        """Move/rename a file. Returns the destination."""
        s, d = _p(src), _p(dest)
        if d.is_dir():
            d = d / s.name
        if d.exists():
            if not overwrite:
                raise FileExistsError(f"{d} exists (pass overwrite=True)")
            d.unlink()
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        self.log(f"moved {s} -> {d}")
        return str(d)

    @step(name="fs.remove", timeout=120)
    def remove(self, path: str, missing_ok: bool = True) -> bool:
        """Delete a file or a whole directory tree. True if it removed something."""
        p = _p(path)
        if not p.exists():
            if missing_ok:
                return False
            raise FileNotFoundError(str(p))
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        self.log(f"removed {p}")
        return True

    @step(name="fs.archive", timeout=600)
    def archive(self, src_dir: str, dest: str, format: str = "zip") -> Dict[str, Any]:
        """Zip (or tar/gztar) a folder. Returns ``{path, bytes}``."""
        base = str(_p(dest))
        for suffix in (".zip", ".tar", ".tar.gz", ".tgz"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        out = shutil.make_archive(base, format, root_dir=str(_p(src_dir)))
        size = os.path.getsize(out)
        self.log(f"archived {src_dir} -> {out} ({size} bytes)")
        return {"path": out, "bytes": size}

    @step(name="fs.unpack", timeout=600)
    def unpack(self, archive: str, dest_dir: str) -> str:
        """Extract a zip/tar into a folder. Returns the folder."""
        d = _p(dest_dir)
        d.mkdir(parents=True, exist_ok=True)
        shutil.unpack_archive(str(_p(archive)), str(d))
        self.log(f"unpacked {archive} -> {d}")
        return str(d)


# =========================================================================
# sh
# =========================================================================
class ShSteps(_Namespace):
    """Run external commands.

    Returns ``{cmd, returncode, ok, stdout, stderr, truncated, duration_ms}``
    and raises ShellError on a non-zero exit unless ``check=False``.

    A string command runs through the shell (so pipes and globs work); a list
    does not. Prefer the list form whenever any part comes from run inputs —
    a string built from user input is a shell injection.
    """

    @step(name="sh.run", timeout=900)
    def run(self, cmd: Union[str, Sequence[str]], cwd: Optional[str] = None,
            env: Optional[Dict[str, str]] = None, timeout: float = 600.0,
            check: bool = True, shell: Optional[bool] = None,
            input: Optional[str] = None) -> Dict[str, Any]:
        """Run a command and capture its output.

        ``env`` is merged over the current environment, not a replacement.
        ``timeout`` kills the process (unlike the step-level timeout, which
        only abandons it), so prefer this one for anything that can hang.
        """
        use_shell = isinstance(cmd, str) if shell is None else shell
        printable = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
        run_env = {**os.environ, **(env or {})} if env else None
        self.log(f"$ {printable}")
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, cwd=_p(cwd) if cwd else None, env=run_env,
                shell=use_shell, input=input, capture_output=True,
                text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"command timed out after {timeout}s: {printable}") from exc
        duration = round((time.monotonic() - t0) * 1000, 1)
        stdout, t1 = _truncate(proc.stdout or "", MAX_OUTPUT)
        stderr, t2 = _truncate(proc.stderr or "", MAX_OUTPUT)

        for line in (stdout or "").splitlines()[:40]:
            self.log(f"  {line}")
        self.log(f"exit {proc.returncode} ({duration} ms)")
        if check and proc.returncode != 0:
            raise ShellError(printable, proc.returncode, stderr or "")
        return {"cmd": printable, "returncode": proc.returncode,
                "ok": proc.returncode == 0, "stdout": stdout,
                "stderr": stderr, "truncated": t1 or t2,
                "duration_ms": duration}

    @step(name="sh.which", timeout=30)
    def which(self, program: str) -> Optional[str]:
        """Absolute path of an executable, or None. Handy as a preflight check."""
        return shutil.which(program)


# =========================================================================
# db (sqlite)
# =========================================================================
class DbSteps(_Namespace):
    """SQLite, from the standard library — no server, no extra dependency.

    Good for the local state an internal flow needs: a ledger of what has been
    processed, a dedupe table, a small reporting store. Each call opens and
    closes its own connection, which keeps it safe under ``parallel_map``;
    SQLite still serialises writers, so heavy concurrent writing is not what
    this is for.

    Parameters are always bound (``?`` placeholders) — never build SQL by
    string formatting.
    """

    def _connect(self, db: str, timeout: float = 30.0) -> sqlite3.Connection:
        p = _p(db)
        if p.parent and str(p.parent) not in ("", "."):
            p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
        return conn

    @step(name="db.query", timeout=300)
    def query(self, db: str, sql: str, params: Union[Sequence, dict] = (),
              limit: Optional[int] = None) -> List[dict]:
        """SELECT, returning a list of dicts.

            rows = self.db.query("ops.db",
                                 "SELECT id, total FROM orders WHERE day = ?",
                                 [ctx["day"]])
        """
        cap = min(limit or MAX_ROWS, MAX_ROWS)
        conn = self._connect(db)
        try:
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchmany(cap)]
        finally:
            conn.close()
        self.log(f"{len(rows)} row(s)")
        return rows

    @step(name="db.query_one", timeout=300)
    def query_one(self, db: str, sql: str,
                  params: Union[Sequence, dict] = ()) -> Optional[dict]:
        """First row as a dict, or None."""
        conn = self._connect(db)
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None

    @step(name="db.execute", timeout=300)
    def execute(self, db: str, sql: str,
                params: Union[Sequence, dict] = ()) -> Dict[str, Any]:
        """INSERT/UPDATE/DELETE/DDL. Returns ``{rowcount, lastrowid}``."""
        conn = self._connect(db)
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            out = {"rowcount": cur.rowcount, "lastrowid": cur.lastrowid}
        finally:
            conn.close()
        self.log(f"{out['rowcount']} row(s) affected")
        return out

    @step(name="db.executemany", timeout=600)
    def executemany(self, db: str, sql: str,
                    rows: Iterable[Sequence]) -> Dict[str, Any]:
        """Run one statement over many parameter tuples, in a transaction."""
        rows = [tuple(r) for r in rows]
        conn = self._connect(db)
        try:
            cur = conn.executemany(sql, rows)
            conn.commit()
            out = {"rowcount": cur.rowcount, "batch": len(rows)}
        finally:
            conn.close()
        self.log(f"{len(rows)} statement(s), {out['rowcount']} row(s) affected")
        return out

    @step(name="db.script", timeout=600)
    def script(self, db: str, sql: str) -> bool:
        """Run several statements — schema setup and migrations."""
        conn = self._connect(db)
        try:
            conn.executescript(sql)
            conn.commit()
        finally:
            conn.close()
        self.log("script executed")
        return True

    @step(name="db.table_exists", timeout=60)
    def table_exists(self, db: str, table: str) -> bool:
        conn = self._connect(db)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (table,)).fetchone()
        finally:
            conn.close()
        return row is not None


#: namespace attribute -> class, used by Workflow and by the linter
NAMESPACES = {"http": HttpSteps, "fs": FsSteps, "sh": ShSteps, "db": DbSteps}

__all__ = ["HttpSteps", "FsSteps", "ShSteps", "DbSteps", "NAMESPACES",
           "HttpError", "HttpClientError", "HttpServerError", "ShellError",
           "MAX_TEXT", "MAX_OUTPUT", "MAX_ROWS"]
