"""Built-in workflow scheduler.

Schedules are persisted to a JSON file (default: history/schedules.json, so
in Docker they survive restarts via the history volume). A background thread
checks every TICK_SECONDS whether a schedule is due and launches the flow
through the launcher callback provided by the app.

Two schedule types:
  interval — {"type": "interval", "every_minutes": 30}
  daily    — {"type": "daily", "time": "07:30", "days": [0,1,2,3,4]}
              (days: 0=Mon … 6=Sun; empty/omitted = every day)

Times are the SERVER's local time. If the server was down when a schedule
was due, it fires once at startup catch-up (not once per missed period).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

TICK_SECONDS = 15
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

Launcher = Callable[[str, Optional[Dict[str, Any]], Optional[str]], str]


def _parse(ts: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(ts) if ts else None


class Scheduler:
    def __init__(self, file: str | Path, launcher: Launcher, start_thread: bool = True):
        self.file = Path(file)
        self.launcher = launcher
        self._lock = threading.Lock()
        self._schedules: List[Dict[str, Any]] = []
        self._load()
        if start_thread:
            threading.Thread(target=self._loop, daemon=True,
                             name="codeflow-scheduler").start()

    # ---------------------------------------------------------------- store
    def _load(self) -> None:
        if self.file.exists():
            try:
                self._schedules = json.loads(self.file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self._schedules = []

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(
            json.dumps(self._schedules, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------ validation
    @staticmethod
    def _validate(spec: Dict[str, Any]) -> Dict[str, Any]:
        flow = spec.get("flow")
        if not flow:
            raise ValueError("flow is required")
        stype = spec.get("type")
        out: Dict[str, Any] = {
            "flow": flow,
            "type": stype,
            "inputs": spec.get("inputs") or {},
            "env": spec.get("env") or None,
            "enabled": bool(spec.get("enabled", True)),
        }
        if stype == "interval":
            minutes = float(spec.get("every_minutes") or 0)
            if minutes < 1:
                raise ValueError("every_minutes must be >= 1")
            out["every_minutes"] = minutes
        elif stype == "daily":
            t = str(spec.get("time") or "")
            hh, mm = t.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError("time must be HH:MM")
            out["time"] = f"{int(hh):02d}:{int(mm):02d}"
            days = spec.get("days") or []
            if not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
                raise ValueError("days must be integers 0 (Mon) … 6 (Sun)")
            out["days"] = sorted(set(days))
        else:
            raise ValueError("type must be 'interval' or 'daily'")
        return out

    # -------------------------------------------------------------- schedule
    @staticmethod
    def describe(s: Dict[str, Any]) -> str:
        if s["type"] == "interval":
            m = s["every_minutes"]
            return f"every {int(m) if m == int(m) else m} min"
        days = s.get("days") or []
        dtxt = "/".join(DAY_NAMES[d] for d in days) if days else "every day"
        return f"daily at {s['time']} ({dtxt})"

    def _next_after(self, s: Dict[str, Any], after: datetime) -> Optional[datetime]:
        """Next occurrence strictly after `after`."""
        if s["type"] == "interval":
            return after + timedelta(minutes=float(s["every_minutes"]))
        hh, mm = map(int, s["time"].split(":"))
        days = s.get("days") or list(range(7))
        cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        for i in range(8):
            c = cand + timedelta(days=i)
            if c > after and c.weekday() in days:
                return c
        return None

    def next_run(self, s: Dict[str, Any]) -> Optional[datetime]:
        if not s.get("enabled", True):
            return None
        anchor = _parse(s.get("last_run_at")) or _parse(s.get("created_at")) or datetime.now()
        return self._next_after(s, anchor)

    # ------------------------------------------------------------------- api
    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for s in self._schedules:
                d = dict(s)
                d["when"] = self.describe(s)
                nxt = self.next_run(s)
                d["next_run_at"] = nxt.isoformat(timespec="seconds") if nxt else None
                out.append(d)
            return out

    def add(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        s = self._validate(spec)
        s.update({
            "id": uuid.uuid4().hex[:10],
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "last_run_at": None,
            "last_run_id": None,
            "last_error": None,
        })
        with self._lock:
            self._schedules.append(s)
            self._save()
        return s

    def update(self, sid: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            for s in self._schedules:
                if s["id"] == sid:
                    if any(k in patch for k in
                           ("flow", "type", "every_minutes", "time", "days", "inputs", "env")):
                        merged = {**s, **{k: v for k, v in patch.items() if v is not None}}
                        validated = self._validate(merged)
                        s.update(validated)
                    if "enabled" in patch:
                        s["enabled"] = bool(patch["enabled"])
                    self._save()
                    return dict(s)
        return None

    def delete(self, sid: str) -> bool:
        with self._lock:
            before = len(self._schedules)
            self._schedules = [s for s in self._schedules if s["id"] != sid]
            if len(self._schedules) != before:
                self._save()
                return True
        return False

    def fire(self, sid: str) -> Dict[str, Any]:
        """Run a schedule immediately (also used by the tick loop)."""
        with self._lock:
            s = next((x for x in self._schedules if x["id"] == sid), None)
        if s is None:
            raise KeyError(sid)
        try:
            run_id = self.launcher(s["flow"], dict(s.get("inputs") or {}), s.get("env"))
            with self._lock:
                s["last_run_at"] = datetime.now().isoformat(timespec="seconds")
                s["last_run_id"] = run_id
                s["last_error"] = None
                self._save()
            return {"run_id": run_id}
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                s["last_run_at"] = datetime.now().isoformat(timespec="seconds")
                s["last_run_id"] = None
                s["last_error"] = f"{type(exc).__name__}: {exc}"
                self._save()
            raise

    # ------------------------------------------------------------------ loop
    def check_due(self, now: Optional[datetime] = None) -> List[str]:
        """Fire every enabled schedule whose next occurrence has passed.
        Returns the ids fired (exposed for testing)."""
        now = now or datetime.now()
        due: List[str] = []
        with self._lock:
            for s in self._schedules:
                if not s.get("enabled", True):
                    continue
                anchor = _parse(s.get("last_run_at")) or _parse(s.get("created_at"))
                nxt = self._next_after(s, anchor) if anchor else None
                if nxt and nxt <= now:
                    due.append(s["id"])
        for sid in due:
            try:
                self.fire(sid)
            except Exception:  # noqa: BLE001 - recorded on the schedule
                pass
        return due

    def _loop(self) -> None:  # pragma: no cover - timing loop
        while True:
            time.sleep(TICK_SECONDS)
            try:
                self.check_due()
            except Exception:  # noqa: BLE001
                pass
