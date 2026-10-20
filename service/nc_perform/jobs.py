"""SQLite job queue and the daily budget. One database file. The queue is the
set of rows in state ``queued``. Docker compose uses this. Tests use this.
The Cloudflare adapter uses a Queue plus Durable Objects instead, and calls
the same runner over HTTP.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY,
          state TEXT NOT NULL,
          stage TEXT,
          percent INTEGER DEFAULT 0,
          error TEXT,
          error_code TEXT,
          request_json TEXT,
          result_json TEXT,
          ip_hash TEXT,
          cost_usd REAL DEFAULT 0,
          created_at TEXT,
          updated_at TEXT,
          claim_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS budget (
          day TEXT PRIMARY KEY,
          usd REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ip_hits (
          ip_hash TEXT,
          ts REAL
        );
        CREATE TABLE IF NOT EXISTS flags (
          k TEXT PRIMARY KEY,
          v TEXT
        );
        """)
        self.db.commit()

    def create(self, job: dict) -> None:
        with self._lock:
            self.db.execute(
                """INSERT INTO jobs (id, state, stage, percent, request_json, ip_hash, created_at, updated_at, claim_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job["id"], job.get("state", "queued"), job.get("stage", "queued"), job.get("percent", 0),
                 json.dumps(job.get("request") or {}), job.get("ip_hash"), job.get("created_at") or _now(),
                 _now(), job.get("claim_hash")),
            )
            self.db.commit()

    def update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        if "request" in fields:
            fields["request_json"] = json.dumps(fields.pop("request"))
        if "result" in fields:
            fields["result_json"] = json.dumps(fields.pop("result"))
        with self._lock:
            row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            # Stop wins. A render thread still writing its stage must not put the row back.
            if row and row["state"] == "cancelled":
                fields = {"updated_at": fields["updated_at"], **({"cost_usd": fields["cost_usd"]} if "cost_usd" in fields else {})}
            cols = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
            self.db.commit()

    def get(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["request"] = json.loads(d.pop("request_json") or "{}")
        d["result"] = json.loads(d.pop("result_json") or "null")
        return d

    def queued_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]

    def position(self, job_id: str) -> int | None:
        rows = self.db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created_at, id").fetchall()
        for i, r in enumerate(rows, start=1):
            if r["id"] == job_id:
                return i
        return None

    def claim_next(self) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created_at, id LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE jobs SET state='planning', stage='planning', percent=5, updated_at=? WHERE id=? AND state='queued'",
                            (_now(), row["id"]))
            if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                self.db.commit()
                return None
            self.db.commit()
        return self.get(row["id"])

    def running_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state NOT IN ('queued','done','failed','rejected','cancelled','gathered')"
        ).fetchone()[0]

    def mark_cancelled(self, job_id: str, message: str) -> str | None:
        """Set cancelled unless the job already finished. Returns the state left in the row."""
        with self._lock:
            row = self.db.execute("SELECT state, result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            state = row["state"]
            if state in ("done", "failed", "rejected", "cancelled"):
                return state
            result = json.loads(row["result_json"] or "null")
            if not isinstance(result, dict):
                result = {}
            result["stage_line"] = message
            self.db.execute(
                """UPDATE jobs SET state=?, stage=?, percent=?, error=?, error_code=?, result_json=?, updated_at=?
                   WHERE id=?""",
                ("cancelled", "cancelled", 100, message, "cancelled", json.dumps(result), _now(), job_id),
            )
            self.db.commit()
            return "cancelled"

    def mark_gathered(self, job_id: str, result: dict) -> bool:
        """A Gather finished. It is not a Performance, so it does not become done."""
        with self._lock:
            row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] == "cancelled":
                return False
            self.db.execute(
                """UPDATE jobs SET state=?, stage=?, percent=?, result_json=?, updated_at=? WHERE id=?""",
                ("gathered", "gathered", 100, json.dumps(result), _now(), job_id),
            )
            self.db.commit()
            return True

    def mark_done(self, job_id: str, **fields) -> bool:
        """Finish a job. A cancel that won the race keeps the row cancelled and returns False."""
        with self._lock:
            row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] == "cancelled":
                return False
            fields["state"] = "done"
            fields["stage"] = fields.get("stage") or "done"
            fields["percent"] = 100
            fields["updated_at"] = _now()
            if "request" in fields:
                fields["request_json"] = json.dumps(fields.pop("request"))
            if "result" in fields:
                fields["result_json"] = json.dumps(fields.pop("result"))
            cols = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
            self.db.commit()
            return True

    def spend_today(self) -> float:
        row = self.db.execute("SELECT usd FROM budget WHERE day=?", (_day(),)).fetchone()
        return float(row["usd"]) if row else 0.0

    def add_spend(self, usd: float) -> float:
        if usd <= 0:
            return self.spend_today()
        with self._lock:
            self.db.execute(
                "INSERT INTO budget (day, usd) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET usd=usd+excluded.usd",
                (_day(), usd),
            )
            self.db.commit()
        return self.spend_today()

    def hit(self, ip_hash: str, per_10min: int, per_day: int) -> str | None:
        """Record a submit. Return an error string if the IP is over a limit."""
        now = time.time()
        with self._lock:
            self.db.execute("DELETE FROM ip_hits WHERE ts < ?", (now - 86400,))
            day_n = self.db.execute("SELECT COUNT(*) c FROM ip_hits WHERE ip_hash=? AND ts >= ?", (ip_hash, now - 86400)).fetchone()["c"]
            win_n = self.db.execute("SELECT COUNT(*) c FROM ip_hits WHERE ip_hash=? AND ts >= ?", (ip_hash, now - 600)).fetchone()["c"]
            if win_n >= per_10min:
                return f"Too many Performs from this network ({per_10min} every 10 minutes). Try again in a few minutes."
            if day_n >= per_day:
                return f"Daily limit reached ({per_day} Performs from this network). It resets tomorrow."
            self.db.execute("INSERT INTO ip_hits (ip_hash, ts) VALUES (?, ?)", (ip_hash, now))
            self.db.commit()
        return None

    def flag(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT v FROM flags WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_flag(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute("INSERT INTO flags (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
            self.db.commit()

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT id, state, stage, cost_usd, ip_hash, error_code, created_at FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def summary_today(self) -> dict:
        day = _day()
        rows = self.db.execute("SELECT state, COUNT(*) c FROM jobs WHERE created_at LIKE ? GROUP BY state", (day + "%",)).fetchall()
        by = {r["state"]: r["c"] for r in rows}
        fails = self.db.execute(
            "SELECT error_code, COUNT(*) c FROM jobs WHERE created_at LIKE ? AND state IN ('failed','rejected') GROUP BY error_code",
            (day + "%",),
        ).fetchall()
        tops = self.db.execute(
            "SELECT ip_hash, COUNT(*) c FROM jobs WHERE created_at LIKE ? GROUP BY ip_hash ORDER BY c DESC LIMIT 8",
            (day + "%",),
        ).fetchall()
        return {
            "day": day,
            "usd": round(self.spend_today(), 4),
            "by_state": by,
            "failures": [dict(r) for r in fails],
            "top_ips": [dict(r) for r in tops],
            "queued": self.queued_count(),
        }
