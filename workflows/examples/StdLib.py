"""The standard step library: self.http / self.fs / self.sh / self.db.

Every call below is a real journaled step — open the run's report and you'll
see `fs.write_csv`, `sh.run`, `db.query` listed with their arguments and
results, exactly like the steps you write yourself. Which also means a resume
skips the ones that already finished.

This flow only touches a temp folder and a throwaway SQLite file, so it is
safe to run anywhere. It does not make network calls; the http lines are in
the docstring below rather than the body so the demo works offline.

    orders = self.http.get(f"{ctx['api']}/orders", params={"day": day})["json"]
    self.http.post(f"{ctx['api']}/ack", json={"ids": [o["id"] for o in orders]})
    self.http.download(url, "/tmp/report.pdf")
"""
import os
import tempfile

from engine import Workflow, flow, step

WORK = os.path.join(tempfile.gettempdir(), "codeflow-stdlib-demo")
DB = os.path.join(WORK, "orders.db")


class StdLibFlow(Workflow):
    description = "Standard step library demo — fs + sh + db, all journaled"
    tags = ["demo", "stdlib"]
    inputs = {"rows": 5}

    @flow
    def main(self, ctx):
        # --- fs: write a CSV, read it back -----------------------------
        self.fs.ensure_dir(WORK)
        rows = [{"id": i, "customer": f"cust-{i}", "total": i * 25.5}
                for i in range(1, int(ctx["rows"]) + 1)]
        csv_path = self.fs.write_csv(f"{WORK}/orders.csv", rows)
        loaded = self.fs.read_csv(csv_path)
        self.log(f"round-tripped {len(loaded)} rows through CSV")

        # --- db: load them into SQLite and aggregate -------------------
        self.db.script(DB, """
            DROP TABLE IF EXISTS orders;
            CREATE TABLE orders (id INTEGER PRIMARY KEY,
                                 customer TEXT, total REAL);
        """)
        self.db.executemany(
            DB, "INSERT INTO orders (id, customer, total) VALUES (?, ?, ?)",
            [(r["id"], r["customer"], float(r["total"])) for r in loaded])

        summary = self.db.query_one(
            DB, "SELECT COUNT(*) AS n, ROUND(SUM(total), 2) AS revenue FROM orders")
        big = self.db.query(
            DB, "SELECT * FROM orders WHERE total > ? ORDER BY total DESC", [50])
        self.tabulate(big)          # your own steps sit alongside the library ones

        # --- sh: shell out, capturing the output -----------------------
        listing = self.sh.run(f"ls -1 {WORK}")
        files = [f for f in listing["stdout"].splitlines() if f]

        # A non-zero exit raises ShellError; check=False returns it instead,
        # which is how you branch on an exit code.
        probe = self.sh.run("test -f /definitely/not/here", check=False)
        self.log(f"probe exited {probe['returncode']} (expected 1)")

        # --- fs: archive the folder, then tidy up ----------------------
        zipped = self.fs.archive(WORK, f"{WORK}/orders", format="zip")
        self.fs.write_json(f"{WORK}/summary.json", summary)

        return {"orders": summary["n"], "revenue": summary["revenue"],
                "files": files, "archive_bytes": zipped["bytes"]}

    @step()
    def tabulate(self, rows):
        """log_table/log_json/log_image attach to the *step* that emits them,
        so call them from inside a step — from the flow body there is nothing
        to attach to and the report says so."""
        self.log_table(rows, title="Orders over 50")
        return len(rows)

    @step()
    def cleanup(self):
        """Not called by default — here to show your own steps and the
        library ones sit side by side in the same report."""
        self.fs.remove(WORK)
        return True
