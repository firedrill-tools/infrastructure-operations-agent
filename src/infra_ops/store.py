"""Transactional state for a local, synthetic deployment CLI."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class DeploymentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError(f"deployment database missing: {self.path}; run infra-demo seed")

    @classmethod
    def seed(cls, path: str | Path, fixture: str | Path) -> "DeploymentStore":
        db = Path(path).resolve()
        if db.exists():
            raise ValueError("seed destination already exists")
        db.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(Path(fixture).read_text())
        if not data.get("services") or not data.get("allowed_versions"):
            raise ValueError("invalid deployment fixture")
        with sqlite3.connect(db) as conn:
            conn.executescript("""
                CREATE TABLE services (name TEXT PRIMARY KEY, version TEXT NOT NULL,
                  previous_version TEXT, health TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE versions (service TEXT NOT NULL, version TEXT NOT NULL, fails_health INTEGER NOT NULL,
                  PRIMARY KEY (service, version));
                CREATE TABLE status_cache (name TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
                CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL,
                  event TEXT NOT NULL, version TEXT NOT NULL, revision INTEGER NOT NULL);
            """)
            for item in data["services"]:
                conn.execute("INSERT INTO services VALUES (?,?,?,?,?)", (item["name"], item["version"], None,
                             "healthy" if item["healthy"] else "degraded", item["revision"]))
                snapshot = {"name": item["name"], "version": item["version"], "previous_version": None,
                            "health": "healthy" if item["healthy"] else "degraded", "revision": item["revision"]}
                conn.execute("INSERT INTO status_cache VALUES (?,?)", (item["name"], json.dumps(snapshot)))
            for service, versions in data["allowed_versions"].items():
                for version in versions:
                    conn.execute("INSERT INTO versions VALUES (?,?,?)", (service, version,
                                 int(version in data.get("failing_versions", {}).get(service, []))))
        return cls(db)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(conn, name: str) -> dict:
        row = conn.execute("SELECT name,version,previous_version,health,revision FROM services WHERE name=?", (name,)).fetchone()
        if not row:
            raise ValueError("unknown service")
        return dict(row)

    def status(self, name: str, cached: bool = False) -> dict:
        with self._connect() as conn:
            if cached:
                row = conn.execute("SELECT snapshot FROM status_cache WHERE name=?", (name,)).fetchone()
                if not row:
                    raise ValueError("unknown service")
                return {**json.loads(row["snapshot"]), "source": "cached"}
            current = self._row(conn, name)
            conn.execute("UPDATE status_cache SET snapshot=? WHERE name=?", (json.dumps(current), name))
            return {**current, "source": "fresh"}

    def deploy(self, name: str, version: str, expected_revision: int) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._row(conn, name)
            allowed = conn.execute("SELECT 1 FROM versions WHERE service=? AND version=?", (name, version)).fetchone()
            if not allowed:
                raise ValueError("version not allowed in synthetic environment")
            if current["revision"] != expected_revision:
                raise ValueError("stale revision")
            if current["health"] != "healthy" or current["version"] == version:
                raise ValueError("service not ready for this rollout")
            revision = current["revision"] + 1
            conn.execute("UPDATE services SET version=?,previous_version=?,health='rolling',revision=? WHERE name=?",
                         (version, current["version"], revision, name))
            conn.execute("INSERT INTO history (service,event,version,revision) VALUES (?,?,?,?)",
                         (name, "deploy_started", version, revision))
            conn.commit()
            return self._row(conn, name)

    def health_check(self, name: str, expected_revision: int) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._row(conn, name)
            if current["revision"] != expected_revision:
                raise ValueError("stale revision")
            if current["health"] != "rolling":
                raise ValueError("no rollout awaiting health check")
            fails = conn.execute("SELECT fails_health FROM versions WHERE service=? AND version=?",
                                 (name, current["version"])).fetchone()["fails_health"]
            health = "degraded" if fails else "healthy"
            revision = current["revision"] + 1
            conn.execute("UPDATE services SET health=?,revision=? WHERE name=?", (health, revision, name))
            conn.execute("INSERT INTO history (service,event,version,revision) VALUES (?,?,?,?)",
                         (name, "health_failed" if fails else "health_passed", current["version"], revision))
            conn.commit()
            return self._row(conn, name)

    def rollback(self, name: str, expected_revision: int) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._row(conn, name)
            if current["revision"] != expected_revision:
                raise ValueError("stale revision")
            if current["health"] != "degraded" or not current["previous_version"]:
                raise ValueError("rollback requires a failed rollout and previous version")
            revision = current["revision"] + 1
            conn.execute("UPDATE services SET version=?,previous_version=NULL,health='healthy',revision=? WHERE name=?",
                         (current["previous_version"], revision, name))
            conn.execute("INSERT INTO history (service,event,version,revision) VALUES (?,?,?,?)",
                         (name, "rolled_back", current["previous_version"], revision))
            conn.commit()
            return self._row(conn, name)

    def history(self, name: str) -> list[dict]:
        with self._connect() as conn:
            self._row(conn, name)
            return [dict(row) for row in conn.execute(
                "SELECT event,version,revision FROM history WHERE service=? ORDER BY id", (name,))]
