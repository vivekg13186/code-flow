"""Execution history store + per-run HTML report generation.

Each run is persisted as:
  history/<run_id>.json   — raw run record
  history/<run_id>.html   — human-readable execution report
and summarised in history/index.json.
"""
from __future__ import annotations

import html
import json
import threading
from pathlib import Path
from typing import Any, Dict, List

from .runner import RunRecord

_LOCK = threading.Lock()

STATUS_COLORS = {
    "SUCCESS": "#16a34a",
    "FAILED": "#dc2626",
    "RUNNING": "#2563eb",
    "SKIPPED": "#9ca3af",
    "PENDING": "#9ca3af",
    "CANCELLED": "#d97706",
    "INTERRUPTED": "#7c3aed",
}


class HistoryStore:
    def __init__(self, folder: str | Path, limit: int = 500):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.index_file = self.folder / "index.json"
        #: retention: keep at most this many runs (oldest finished are pruned)
        self.limit = max(0, int(limit))

    # ---------------------------------------------------------------- store
    def save_run(self, record: RunRecord) -> Path:
        """Persist the run (json + html) and update the index. Thread-safe."""
        data = record.to_dict()
        report_path = self.folder / f"{record.run_id}.html"
        with _LOCK:
            (self.folder / f"{record.run_id}.json").write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
            report_path.write_text(render_report(data), encoding="utf-8")
            # resume sidecar: faithful ctx + step statuses, used by
            # POST /api/runs/{id}/resume (values must be JSON-serializable
            # to round-trip exactly; others degrade via str())
            try:
                (self.folder / f"{record.run_id}.resume.json").write_text(
                    json.dumps({
                        "workflow": record.workflow,
                        "inputs": record.inputs,
                        "environment": record.environment,
                        "ctx": record.raw_ctx,
                        "steps": [{"name": s.name, "status": s.status}
                                  for s in record.steps],
                    }, default=str), encoding="utf-8")
            except Exception:  # noqa: BLE001 - resume is best-effort
                pass
            index = self._read_index()
            entry = {
                "run_id": record.run_id,
                "workflow": record.workflow,
                "status": record.status,
                "started_at": record.started_at,
                "ended_at": record.ended_at,
                "duration_ms": record.duration_ms,
                "error": record.error,
                "steps": len(record.steps),
                "parent_run_id": record.parent_run_id,
                "environment": record.environment,
                "tags": record.tags,
            }
            index = [e for e in index if e["run_id"] != record.run_id]
            index.insert(0, entry)
            # retention: prune the oldest finished runs beyond the limit
            # (index is newest-first; RUNNING entries are never pruned)
            if self.limit and len(index) > self.limit:
                keep: list = []
                over = len(index) - self.limit
                for e in reversed(index):  # oldest first
                    if over > 0 and e.get("status") != "RUNNING":
                        over -= 1
                        for suffix in (".json", ".html", ".resume.json"):
                            p = self.folder / f"{e['run_id']}{suffix}"
                            if p.exists():
                                p.unlink()
                    else:
                        keep.append(e)
                index = list(reversed(keep))
            self.index_file.write_text(
                json.dumps(index, indent=2, default=str), encoding="utf-8"
            )
        return report_path

    def history(self) -> List[Dict[str, Any]]:
        with _LOCK:
            return self._read_index()

    def mark_interrupted(self) -> int:
        """Called at server startup: any run persisted as RUNNING did not
        survive the previous process — flag it INTERRUPTED so crashes are
        visible in the history instead of silently disappearing."""
        import json as _json

        count = 0
        with _LOCK:
            index = self._read_index()
            for entry in index:
                if entry.get("status") != "RUNNING":
                    continue
                entry["status"] = "INTERRUPTED"
                entry["error"] = "server stopped while this run was in progress"
                count += 1
                json_path = self.folder / f"{entry['run_id']}.json"
                try:
                    data = _json.loads(json_path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    data = {"run_id": entry["run_id"], "workflow": entry["workflow"],
                            "steps": [], "inputs": {}, "outputs": {}}
                data["status"] = "INTERRUPTED"
                data["error"] = "server stopped while this run was in progress"
                for s in data.get("steps", []):
                    if s.get("status") in ("RUNNING", "PENDING"):
                        s["status"] = "INTERRUPTED"
                json_path.write_text(_json.dumps(data, indent=2, default=str), encoding="utf-8")
                (self.folder / f"{entry['run_id']}.html").write_text(
                    render_report(data), encoding="utf-8")
            if count:
                self.index_file.write_text(
                    _json.dumps(index, indent=2, default=str), encoding="utf-8")
        return count

    def report_path(self, run_id: str) -> Path:
        return self.folder / f"{run_id}.html"

    def delete_run(self, run_id: str) -> bool:
        """Remove one run's report files + index entry. Returns True if found."""
        import json as _json

        with _LOCK:
            index = self._read_index()
            new_index = [e for e in index if e["run_id"] != run_id]
            found = len(new_index) != len(index)
            for suffix in (".json", ".html", ".resume.json"):
                p = self.folder / f"{run_id}{suffix}"
                if p.exists():
                    p.unlink()
                    found = True
            if found:
                self.index_file.write_text(
                    _json.dumps(new_index, indent=2, default=str), encoding="utf-8")
        return found

    def clear(self, keep_statuses=("RUNNING",)) -> int:
        """Delete all runs except those whose status is in keep_statuses.
        Returns the number of deleted runs."""
        import json as _json

        with _LOCK:
            index = self._read_index()
            keep, drop = [], []
            for e in index:
                (keep if e.get("status") in keep_statuses else drop).append(e)
            for e in drop:
                for suffix in (".json", ".html", ".resume.json"):
                    p = self.folder / f"{e['run_id']}{suffix}"
                    if p.exists():
                        p.unlink()
            self.index_file.write_text(
                _json.dumps(keep, indent=2, default=str), encoding="utf-8")
        return len(drop)

    def _read_index(self) -> List[Dict[str, Any]]:
        if self.index_file.exists():
            try:
                return json.loads(self.index_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
        return []


# -------------------------------------------------------------------- report
def _badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "#6b7280")
    return (
        f'<span style="background:{color};color:#fff;padding:2px 10px;'
        f'border-radius:999px;font-size:12px;font-weight:600">{html.escape(status)}</span>'
    )


def _kv_table(d: Dict[str, Any]) -> str:
    if not d:
        return '<p class="muted">—</p>'
    rows = "".join(
        f"<tr><td class='k'>{html.escape(str(k))}</td>"
        f"<td><code>{html.escape(repr(v))}</code></td></tr>"
        for k, v in d.items()
    )
    return f"<table class='kv'>{rows}</table>"


def render_report(run: Dict[str, Any]) -> str:
    steps_html = []
    for i, s in enumerate(run.get("steps", []), 1):
        logs = "\n".join(html.escape(line) for line in s.get("logs", []))
        detail_rows = []
        if s.get("condition"):
            res = s.get("condition_result")
            detail_rows.append(
                f"<tr><td class='k'>condition</td><td><code>{html.escape(s['condition'])}</code>"
                f" → <b>{res}</b></td></tr>"
            )
        if s.get("waited_s") is not None:
            detail_rows.append(
                f"<tr><td class='k'>wait</td><td>paused {s['waited_s']}s before continuing</td></tr>"
            )
        if s.get("loop"):
            detail_rows.append(
                f"<tr><td class='k'>loop</td><td><code>{html.escape(s['loop'])}</code>"
                f" — {s.get('iterations', 0)} iteration(s)"
                + (f", parallel={s['parallel']}" if s.get("parallel", 1) > 1 else "")
                + "</td></tr>"
            )
        if s.get("max_attempts", 1) > 1 or s.get("attempts", 0) > 1:
            detail_rows.append(
                f"<tr><td class='k'>attempts</td><td>{s.get('attempts', 0)}"
                f" / {s.get('max_attempts', 1)}</td></tr>"
            )
        if s.get("result"):
            detail_rows.append(
                f"<tr><td class='k'>result</td><td><code>{html.escape(str(s['result']))}</code></td></tr>"
            )
        if s.get("error"):
            detail_rows.append(
                f"<tr><td class='k'>error</td><td class='err'>{html.escape(str(s['error']))}"
                + (" <b>— continue_on_error: flow continued</b>" if s.get("continued") else "")
                + "</td></tr>"
            )
        detail = f"<table class='kv'>{''.join(detail_rows)}</table>" if detail_rows else ""
        tb = (
            f"<details><summary>traceback</summary><pre>{html.escape(s['traceback'])}</pre></details>"
            if s.get("traceback")
            else ""
        )
        logs_html = f"<pre class='logs'>{logs}</pre>" if logs else ""
        blocks_html = ""
        for bi, b in enumerate(s.get("blocks", []) or [], 1):
            btitle = (f"{b['type']} #{bi}"
                      + (f" — {html.escape(b['title'])}" if b.get("title") else ""))
            if b.get("type") == "json":
                blocks_html += (f"<div class='log-block'><div class='bt'>{btitle}</div>"
                                f"<pre class='jsonlog'>{html.escape(b.get('text') or '')}</pre></div>")
            elif b.get("type") == "table":
                rows = b.get("rows") or []
                cols: list = []
                for r in rows[:50]:
                    for k in r:
                        if k not in cols:
                            cols.append(k)
                head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
                body = "".join(
                    "<tr>" + "".join(
                        f"<td>{html.escape('' if r.get(c) is None else (str(r[c]) if not isinstance(r[c], (dict, list)) else __import__('json').dumps(r[c])))}</td>"
                        for c in cols) + "</tr>"
                    for r in rows)
                note = "<div class='muted' style='font-size:11px'>… truncated</div>" if b.get("truncated") else ""
                blocks_html += (f"<div class='log-block'><div class='bt'>{btitle}</div>"
                                f"<div class='logtbl-wrap'><table class='logtbl'>"
                                f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{note}</div>")
        imgs_html = ""
        if s.get("images"):
            figs = "".join(
                f"<figure><img src='{img['data']}' alt='{html.escape(img.get('title') or 'image')}'/>"
                + (f"<figcaption>{html.escape(img['title'])}</figcaption>" if img.get("title") else "")
                + "</figure>"
                for img in s["images"]
            )
            imgs_html = f"<div class='step-imgs'>{figs}</div>"
        inherited = s.get("inherited")
        steps_html.append(
            f"""
      <div class="step{' inherited' if inherited else ''}">
        <div class="step-head">
          <span class="idx">{i}</span>
          <b>{html.escape(s['name'])}</b>
          <span class="muted">({html.escape(s.get('func_name', ''))})</span>
          {_badge(s.get('status', '?'))}
          {'<span class="carried">carried over — not re-executed</span>' if inherited else ''}
          <span class="muted right">{s.get('duration_ms', '—')} ms</span>
        </div>
        {detail}{logs_html}{blocks_html}{imgs_html}{tb}
      </div>"""
        )

    err_html = (
        f"<div class='run-error'>⚠ {html.escape(str(run['error']))}</div>"
        if run.get("error")
        else ""
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Run {html.escape(run['run_id'])} — {html.escape(run['workflow'])}</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<style>
  body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; margin: 0;
         background:#f5f6f8; color:#111827; }}
  .wrap {{ max-width: 900px; margin: 0 auto; padding: 32px 20px 60px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .muted {{ color:#6b7280; font-size: 13px; }}
  .right {{ margin-left:auto; }}
  .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px;
          padding:18px 20px; margin-top:16px; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  .meta {{ display:flex; gap:24px; flex-wrap:wrap; margin-top:10px; font-size:14px; }}
  .meta div span {{ display:block; color:#6b7280; font-size:12px; }}
  .step {{ border:1px solid #e5e7eb; border-radius:10px; padding:12px 14px; margin-top:12px; background:#fff; }}
  .step.inherited {{ opacity:.75; background:#fafafa; border-style:dashed; }}
  .carried {{ font-size:11px; color:#6b7280; background:#f3f4f6; border-radius:999px;
             padding:2px 9px; }}
  .step-head {{ display:flex; align-items:center; gap:10px; }}
  .idx {{ background:#eef2ff; color:#4338ca; border-radius:6px; padding:2px 8px; font-size:12px; font-weight:700; }}
  table.kv {{ border-collapse: collapse; margin-top:10px; font-size:13px; width:100%; }}
  table.kv td {{ border-top:1px solid #f3f4f6; padding:5px 8px; vertical-align:top; }}
  table.kv td.k {{ color:#6b7280; width:120px; }}
  td.err {{ color:#b91c1c; }}
  pre {{ background:#0f172a; color:#e2e8f0; padding:12px; border-radius:8px;
        font-size:12px; overflow:auto; }}
  pre.logs {{ background:#f8fafc; color:#334155; border:1px solid #e2e8f0; }}
  .log-block {{ margin-top:10px; }}
  .log-block .bt {{ font-size:10.5px; font-weight:700; color:#6b7280;
                   text-transform:uppercase; letter-spacing:.05em; margin-bottom:4px; }}
  pre.jsonlog {{ background:#0f172a; color:#a5f3fc; padding:12px; border-radius:8px;
                font-size:12px; overflow:auto; max-height:340px; margin:0; }}
  .logtbl-wrap {{ overflow:auto; max-height:300px; border:1px solid #e5e7eb;
                 border-radius:8px; }}
  table.logtbl {{ border-collapse:collapse; font-size:12px; width:100%; }}
  table.logtbl th {{ background:#f9fafb; text-align:left; padding:6px 10px;
                    position:sticky; top:0; border-bottom:1px solid #e5e7eb;
                    font-size:11px; color:#6b7280; }}
  table.logtbl td {{ padding:5px 10px; border-bottom:1px solid #f3f4f6; }}
  .step-imgs {{ display:flex; flex-wrap:wrap; gap:12px; margin-top:10px; }}
  .step-imgs figure {{ margin:0; }}
  .step-imgs img {{ max-width:440px; max-height:340px; border:1px solid #e5e7eb;
                   border-radius:8px; background:#fff; display:block; }}
  .step-imgs figcaption {{ font-size:11px; color:#6b7280; margin-top:4px; }}
  .run-error {{ background:#fef2f2; border:1px solid #fecaca; color:#b91c1c;
              border-radius:10px; padding:12px 16px; margin-top:16px; }}
  h2 {{ font-size:15px; margin:26px 0 4px; }}
  code {{ background:#f3f4f6; padding:1px 5px; border-radius:4px; font-size:12.5px; }}
  a {{ color:#2563eb; }}
</style></head>
<body><div class="wrap">
  <a href="/">← back to code-flow</a>
  <div class="card">
    <h1>{html.escape(run['workflow'])} {_badge(run.get('status', '?'))}</h1>
    <div class="muted">run <code>{html.escape(run['run_id'])}</code>{
        f' — sub-workflow of run <a href="/reports/{html.escape(run["parent_run_id"])}">'
        f'<code>{html.escape(run["parent_run_id"])}</code></a>'
        if run.get("parent_run_id") else ""}{
        f' — resumed from <a href="/reports/{html.escape(run["resumed_from"])}">'
        f'<code>{html.escape(run["resumed_from"])}</code></a> at step '
        f'<b>{html.escape(run.get("resumed_at_step") or "?")}</b>'
        if run.get("resumed_from") else ""}</div>
    <div class="meta">
      <div><span>tags</span>{html.escape(', '.join(run.get('tags') or []) or '—')}</div>
      <div><span>environment</span>{html.escape(run.get('environment') or '—')}</div>
      <div><span>started</span>{html.escape(str(run.get('started_at', '—')))}</div>
      <div><span>ended</span>{html.escape(str(run.get('ended_at', '—')))}</div>
      <div><span>duration</span>{run.get('duration_ms', '—')} ms</div>
      <div><span>steps</span>{len(run.get('steps', []))}</div>
    </div>
    {err_html}
  </div>
  <h2>Inputs</h2><div class="card">{_kv_table(run.get('inputs', {}))}</div>
  {f"<h2>Environment — {html.escape(run['environment'])}</h2><div class='card'>{_kv_table(run.get('env_values', {}))}</div>" if run.get('environment') else ""}
  <h2>Steps</h2>{''.join(steps_html) or "<p class='muted'>no steps executed</p>"}
  <h2>Outputs</h2><div class="card">{_kv_table(run.get('outputs', {}))}</div>
  {f"<h2>Context</h2><div class='card'>{_kv_table(run.get('context', {}))}</div>" if run.get('context') else ""}
</div></body></html>"""
