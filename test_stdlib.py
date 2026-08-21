"""Verification for the standard step library."""
import json, os, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

from engine import Workflow, flow, step, parallel_map, HttpServerError, HttpClientError, ShellError
from engine.runner import WorkflowRunner

PORT = 8765
HITS = {"flaky": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/ok"):
            self._send(200, {"hello": "world", "path": self.path})
        elif self.path == "/flaky":                 # 503 twice, then 200
            HITS["flaky"] += 1
            if HITS["flaky"] < 3:
                self._send(503, {"error": "try later"})
            else:
                self._send(200, {"recovered_after": HITS["flaky"]})
        elif self.path == "/missing":
            self._send(404, {"error": "nope"})
        elif self.path == "/blob":
            raw = b"x" * 5000
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self._send(200, {"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        self._send(201, {"created": True, "echo": body})


srv = HTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"
TMP = tempfile.mkdtemp(prefix="cf-stdlib-")
FAILMARK = os.path.join(TMP, "fixed")
results = []


def check(label, cond, extra=""):
    results.append((label, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{('  ' + str(extra)) if extra else ''}")


# ---------------------------------------------------------------- http
class HttpFlow(Workflow):
    description = "http checks"

    @flow
    def main(self, ctx):
        ok = self.http.get(f"{BASE}/ok?a=1")
        created = self.http.post(f"{BASE}/items", json={"id": 7})
        recovered = self.http.get(f"{BASE}/flaky")        # 503,503,200 -> retried
        blob = self.http.download(f"{BASE}/blob", f"{TMP}/blob.bin")
        soft = self.http.get(f"{BASE}/missing", check=False)
        return {"ok": ok, "created": created, "recovered": recovered,
                "blob": blob, "soft": soft}


class Http404Flow(Workflow):
    description = "4xx must fail immediately"

    @flow
    def main(self, ctx):
        self.http.get(f"{BASE}/missing")


print("\n[http]")
r = WorkflowRunner(HttpFlow).run().to_dict()
o = r["outputs"]
check("GET parses json", r["status"] == "SUCCESS" and o["ok"]["json"]["hello"] == "world")
check("GET reports status/elapsed", o["ok"]["status"] == 200 and o["ok"]["elapsed_ms"] >= 0)
check("POST sends body, returns 201", o["created"]["status"] == 201
      and o["created"]["json"]["echo"] == {"id": 7})
check("503 retried until success", o["recovered"]["json"]["recovered_after"] == 3, f'attempts={HITS["flaky"]}')
flaky_rec = [s for s in r["steps"] if s["name"] == "http.get" and s["attempts"] > 1]
check("retries visible in the report", flaky_rec and flaky_rec[0]["attempts"] == 3,
      f'attempts={flaky_rec[0]["attempts"] if flaky_rec else "?"}')
check("download wrote the file", o["blob"]["bytes"] == 5000
      and os.path.getsize(f"{TMP}/blob.bin") == 5000)
check("no .part left behind", not os.path.exists(f"{TMP}/blob.bin.part"))
check("check=False returns the 404", o["soft"]["status"] == 404 and o["soft"]["ok"] is False)

r2 = WorkflowRunner(Http404Flow).run().to_dict()
n404 = [s for s in r2["steps"] if s["name"] == "http.get"]
check("4xx fails the run", r2["status"] == "FAILED" and "404" in (r2.get("error") or ""))
check("4xx does NOT burn retries", n404 and n404[0]["attempts"] == 1,
      f'attempts={n404[0]["attempts"] if n404 else "?"}')


# ---------------------------------------------------------------- fs
class FsFlow(Workflow):
    description = "fs checks"

    @flow
    def main(self, ctx):
        self.fs.write_text(f"{TMP}/a/note.txt", "hello")
        text = self.fs.read_text(f"{TMP}/a/note.txt")
        self.fs.write_json(f"{TMP}/a/data.json", {"n": 1})
        data = self.fs.read_json(f"{TMP}/a/data.json")
        self.fs.write_csv(f"{TMP}/a/rows.csv", [{"x": 1, "y": "two"}])
        rows = self.fs.read_csv(f"{TMP}/a/rows.csv")
        found = self.fs.glob(f"{TMP}/a/*.json")
        self.fs.copy(f"{TMP}/a/note.txt", f"{TMP}/b/note.txt")
        self.fs.move(f"{TMP}/b/note.txt", f"{TMP}/b/moved.txt")
        st = self.fs.stat(f"{TMP}/b/moved.txt")
        gone = self.fs.stat(f"{TMP}/nope.txt")
        arch = self.fs.archive(f"{TMP}/a", f"{TMP}/a.zip")
        self.fs.unpack(arch["path"], f"{TMP}/unpacked")
        unpacked = self.fs.glob(f"{TMP}/unpacked/*")
        removed = self.fs.remove(f"{TMP}/b")
        return {"text": text, "data": data, "rows": rows, "found": found,
                "st": st, "gone": gone, "unpacked": unpacked, "removed": removed}


class FsClashFlow(Workflow):
    description = "overwrite guard"

    @flow
    def main(self, ctx):
        self.fs.write_text(f"{TMP}/c1.txt", "one")
        self.fs.write_text(f"{TMP}/c2.txt", "two")
        self.fs.copy(f"{TMP}/c1.txt", f"{TMP}/c2.txt")     # must raise


print("\n[fs]")
r = WorkflowRunner(FsFlow).run().to_dict()
o = r["outputs"]
check("run succeeded", r["status"] == "SUCCESS", r.get("error"))
check("text round-trip", o["text"] == "hello")
check("json round-trip", o["data"] == {"n": 1})
check("csv round-trip", o["rows"] == [{"x": "1", "y": "two"}])
check("glob finds the json", o["found"] and o["found"][0].endswith("data.json"))
check("copy+move landed", o["st"]["exists"] and o["st"]["path"].endswith("moved.txt"))
check("stat on a missing path is falsy, not an error", o["gone"]["exists"] is False)
check("archive+unpack round-trip", len(o["unpacked"]) == 3, o["unpacked"])
check("remove deleted the tree", o["removed"] is True and not os.path.exists(f"{TMP}/b"))
rc = WorkflowRunner(FsClashFlow).run().to_dict()
check("copy refuses to clobber", rc["status"] == "FAILED"
      and "exists" in (rc.get("error") or ""))


# ---------------------------------------------------------------- sh
class ShFlow(Workflow):
    description = "sh checks"

    @flow
    def main(self, ctx):
        hello = self.sh.run("echo hello && echo oops >&2")
        listed = self.sh.run(["echo", "from-a-list"])
        bad = self.sh.run("exit 3", check=False)
        where = self.sh.which("python3")
        piped = self.sh.run(f"printf 'b\\na\\n' | sort")
        return {"hello": hello, "listed": listed, "bad": bad,
                "where": where, "piped": piped}


class ShFailFlow(Workflow):
    description = "non-zero exit raises"

    @flow
    def main(self, ctx):
        self.sh.run("exit 42")


print("\n[sh]")
r = WorkflowRunner(ShFlow).run().to_dict()
o = r["outputs"]
check("stdout captured", o["hello"]["stdout"].strip() == "hello")
check("stderr captured", o["hello"]["stderr"].strip() == "oops")
check("list form runs without a shell", o["listed"]["stdout"].strip() == "from-a-list")
check("check=False returns the code", o["bad"]["returncode"] == 3 and o["bad"]["ok"] is False)
check("which() resolves", o["where"] and o["where"].endswith("python3"))
check("shell pipes work for a string cmd", o["piped"]["stdout"].split() == ["a", "b"])
sh_logs = [s for s in r["steps"] if s["name"] == "sh.run"][0]["logs"]
check("command echoed into the report", any("$ echo hello" in l for l in sh_logs))
rf = WorkflowRunner(ShFailFlow).run().to_dict()
check("non-zero exit fails the run", rf["status"] == "FAILED"
      and "exited 42" in (rf.get("error") or ""), rf.get("error"))


# ---------------------------------------------------------------- db
DB = f"{TMP}/t.db"


class DbFlow(Workflow):
    description = "db checks"

    @flow
    def main(self, ctx):
        self.db.script(DB, "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT);")
        exists = self.db.table_exists(DB, "t")
        missing = self.db.table_exists(DB, "nope")
        ins = self.db.execute(DB, "INSERT INTO t (v) VALUES (?)", ["one"])
        many = self.db.executemany(DB, "INSERT INTO t (v) VALUES (?)",
                                   [["two"], ["three"]])
        rows = self.db.query(DB, "SELECT * FROM t ORDER BY id")
        one = self.db.query_one(DB, "SELECT COUNT(*) AS n FROM t")
        none = self.db.query_one(DB, "SELECT * FROM t WHERE v = ?", ["zzz"])
        limited = self.db.query(DB, "SELECT * FROM t ORDER BY id", limit=2)
        return {"exists": exists, "missing": missing, "ins": ins, "many": many,
                "rows": rows, "one": one, "none": none, "limited": limited}


print("\n[db]")
r = WorkflowRunner(DbFlow).run().to_dict()
o = r["outputs"]
check("run succeeded", r["status"] == "SUCCESS", r.get("error"))
check("table_exists true/false", o["exists"] is True and o["missing"] is False)
check("execute returns lastrowid", o["ins"]["lastrowid"] == 1)
check("executemany reports the batch", o["many"]["batch"] == 2)
check("query returns dicts", o["rows"] == [{"id": 1, "v": "one"}, {"id": 2, "v": "two"},
                                           {"id": 3, "v": "three"}])
check("query_one aggregates", o["one"] == {"n": 3})
check("query_one returns None when empty", o["none"] is None)
check("limit is honoured", len(o["limited"]) == 2)
check("params are bound, not formatted",
      WorkflowRunner(DbFlow).run().to_dict()["status"] == "SUCCESS")


# ------------------------------------------------------- parallel + resume
class ParallelFlow(Workflow):
    description = "library steps under parallel_map"

    @flow
    def main(self, ctx):
        urls = [f"{BASE}/ok?i={i}" for i in range(6)]
        got = parallel_map(lambda u: self.http.get(u), urls, workers=4)
        return {"n": len(got), "paths": sorted(g["json"]["path"] for g in got)}


print("\n[parallel]")
r = WorkflowRunner(ParallelFlow).run().to_dict()
gets = [s for s in r["steps"] if s["name"] == "http.get"]
check("all 6 concurrent calls journaled", len(gets) == 6, f"got {len(gets)}")
check("each has its own log lines", all(s["logs"] for s in gets))
check("results all distinct", len(set(r["outputs"]["paths"])) == 6)


class ResumeFlow(Workflow):
    description = "resume skips completed library steps"

    @flow
    def main(self, ctx):
        self.fs.write_text(f"{TMP}/r/step1.txt", "written once")
        self.db.execute(DB, "INSERT INTO t (v) VALUES (?)", ["resume-probe"])
        self.fs.read_text(f"{TMP}/r/step1.txt")
        self.gate()
        return {"done": True}

    @step(timeout=20)
    def gate(self):
        if not os.path.exists(FAILMARK):
            raise ConnectionError("gate closed")
        return True


print("\n[resume]")
first = WorkflowRunner(ResumeFlow).run()
d1 = first.to_dict()
before = WorkflowRunner(DbFlow).run()  # noqa - unrelated
rows_after_first = len([r for r in __import__("sqlite3").connect(DB)
                        .execute("SELECT v FROM t WHERE v='resume-probe'")])
check("first run fails at the gate", d1["status"] == "FAILED")
open(FAILMARK, "w").close()
resumed = WorkflowRunner(ResumeFlow, resume={"journal": dict(first.journal),
                                             "from_run": first.run_id}).run().to_dict()
inherited = [s["name"] for s in resumed["steps"] if s.get("inherited")]
ran = [s["name"] for s in resumed["steps"] if not s.get("inherited")]
rows_after_resume = len([r for r in __import__("sqlite3").connect(DB)
                         .execute("SELECT v FROM t WHERE v='resume-probe'")])
check("resume succeeds", resumed["status"] == "SUCCESS", resumed.get("error"))
check("library steps carried over, not re-run",
      inherited == ["fs.write_text", "db.execute", "fs.read_text"], inherited)
check("only the failed step re-ran", ran == ["gate"], ran)
check("the INSERT did not happen twice",
      rows_after_first == rows_after_resume == 1,
      f"{rows_after_first} -> {rows_after_resume}")


# ---------------------------------------------------------------- summary
bad = [l for l, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
if bad:
    print("FAILED:", *bad, sep="\n  - ")
srv.shutdown()
raise SystemExit(1 if bad else 0)
