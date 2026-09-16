"""Transactional task state and restart-safe reporting stages."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .schedule import stamp, utcnow

STATUSES = ("todo", "in_progress", "blocked", "completed")
STATUS_LABELS = {
    "todo": "To do",
    "in_progress": "In progress",
    "blocked": "Blocked",
    "completed": "Completed",
}


class Conflict(ValueError):
    pass


def normalize(title: str) -> str:
    return " ".join(title.casefold().split())


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise ValueError("Database was created by a newer Herdr Tasks version")
            if version == 1:
                backup = path.with_name(path.name + ".before-spaces")
                if not backup.exists():
                    with sqlite3.connect(backup) as destination:
                        db.backup(destination)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY, root TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unknown');
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL, normalized_title TEXT NOT NULL, notes TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('daily','weekly')),
                    target TEXT NOT NULL, status TEXT NOT NULL, parent_id TEXT REFERENCES tasks(id),
                    source TEXT NOT NULL, origin_run TEXT, origin_index INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
                    revision INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(origin_run, origin_index));
                CREATE UNIQUE INDEX IF NOT EXISTS open_task_title ON tasks(project_id,kind,normalized_title)
                    WHERE status != 'completed' AND archived = 0;
                CREATE TABLE IF NOT EXISTS task_events(
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS activity(
                    id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    agent_key TEXT NOT NULL, at TEXT NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS activity_project_time ON activity(project_id,at);
                CREATE INDEX IF NOT EXISTS activity_agent ON activity(agent_key,id);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, run_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, status TEXT NOT NULL, spec TEXT NOT NULL,
                    evidence TEXT, summary TEXT, plan TEXT, error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS updates(
                    run_id TEXT NOT NULL REFERENCES runs(id), agent_key TEXT NOT NULL,
                    at TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(run_id,agent_key));
                CREATE TABLE IF NOT EXISTS reports(
                    run_id TEXT NOT NULL REFERENCES runs(id), project_id TEXT NOT NULL REFERENCES projects(id),
                    created_at TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_id,project_id));
                CREATE TABLE IF NOT EXISTS suggestion_acceptances(
                    run_id TEXT NOT NULL REFERENCES runs(id), item_index INTEGER NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    PRIMARY KEY(run_id,item_index));
            """)
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] < 2:
                db.execute("ALTER TABLE projects RENAME COLUMN root TO identity")
                for column in (
                    "root TEXT NOT NULL DEFAULT ''",
                    "workspace_id TEXT NOT NULL DEFAULT ''",
                    "endpoint TEXT NOT NULL DEFAULT ''",
                    "session_name TEXT NOT NULL DEFAULT ''",
                    "roots TEXT NOT NULL DEFAULT '[]'",
                    "hidden INTEGER NOT NULL DEFAULT 0",
                ):
                    db.execute(f"ALTER TABLE projects ADD COLUMN {column}")
                db.execute("UPDATE projects SET root=identity")
                db.execute("PRAGMA user_version=2")
            db.execute("""CREATE TABLE IF NOT EXISTS project_aliases(
                old_id TEXT PRIMARY KEY REFERENCES projects(id),
                project_id TEXT NOT NULL REFERENCES projects(id))""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get_meta(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def panel_records(self) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT value FROM meta WHERE key LIKE 'panel:%'")
            ]

    def project(self, root: str, name: str, at: str | None = None) -> str:
        project_id = hashlib.sha256(root.encode()).hexdigest()[:20]
        with self.connect() as db:
            db.execute(
                """INSERT INTO projects(id,identity,root,name,last_seen) VALUES(?,?,?,?,?)
                ON CONFLICT(identity) DO UPDATE SET name=excluded.name,last_seen=excluded.last_seen""",
                (project_id, root, root, name, at or stamp(utcnow())),
            )
        return project_id

    @staticmethod
    def space_id(endpoint: str, workspace_id: str) -> str:
        return hashlib.sha256(f"space:{endpoint}:{workspace_id}".encode()).hexdigest()[:20]

    def sync_spaces(self, spaces: list[dict], at: str | None = None) -> None:
        """Keep workspace identity separate from its current directories and label."""
        now = at or stamp(utcnow())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for space in spaces:
                roots = list(dict.fromkeys(space["roots"]))
                project_id = self.space_id(space["endpoint"], space["workspace_id"])
                db.execute(
                    """INSERT INTO projects(
                    id,identity,root,name,last_seen,workspace_id,endpoint,session_name,roots)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
                    root=excluded.root,name=excluded.name,last_seen=excluded.last_seen,
                    session_name=excluded.session_name,roots=excluded.roots,hidden=0""",
                    (
                        project_id,
                        "space:" + project_id,
                        roots[0] if roots else "",
                        space["name"],
                        now,
                        space["workspace_id"],
                        space["endpoint"],
                        space["session"],
                        json.dumps(roots),
                    ),
                )
            if not spaces:
                return
            db.execute(
                "INSERT INTO meta VALUES('grouping','spaces') ON CONFLICT(key) DO UPDATE SET value='spaces'"
            )
            # An in-flight plan still refers to the old project IDs. Migrate after it finishes.
            if db.execute(
                "SELECT 1 FROM runs WHERE status NOT IN ('complete','partial','failed') LIMIT 1"
            ).fetchone():
                return
            candidates: dict[str, set[str]] = {}
            for row in db.execute("SELECT id,roots FROM projects WHERE workspace_id!=''"):
                for root in json.loads(row["roots"]):
                    candidates.setdefault(root, set()).add(row["id"])
            for old in db.execute(
                """SELECT * FROM projects WHERE workspace_id='' AND
                (hidden=0 OR EXISTS(SELECT 1 FROM tasks WHERE tasks.project_id=projects.id))"""
            ).fetchall():
                matches = candidates.get(old["root"], set())
                destination = next(iter(matches)) if len(matches) == 1 else None
                prior = db.execute(
                    "SELECT project_id FROM project_aliases WHERE old_id=?", (old["id"],)
                ).fetchone()
                if prior:
                    destination = prior[0]
                if destination:
                    conflict = db.execute(
                        """SELECT 1 FROM tasks a JOIN tasks b
                        ON a.kind=b.kind AND a.normalized_title=b.normalized_title
                        WHERE a.project_id=? AND b.project_id=?
                        AND a.status!='completed' AND b.status!='completed'
                        AND a.archived=0 AND b.archived=0 LIMIT 1""",
                        (old["id"], destination),
                    ).fetchone()
                    if conflict:
                        destination = None
                if destination:
                    if not prior:
                        priorities = {
                            "unknown": 0,
                            "on_track": 1,
                            "needs_attention": 2,
                            "blocked": 3,
                        }
                        current_status = db.execute(
                            "SELECT status FROM projects WHERE id=?", (destination,)
                        ).fetchone()[0]
                        if priorities.get(old["status"], 0) > priorities.get(current_status, 0):
                            db.execute(
                                "UPDATE projects SET status=? WHERE id=?",
                                (old["status"], destination),
                            )
                    db.execute(
                        """INSERT INTO task_events(task_id,at,data)
                        SELECT id,?,? FROM tasks WHERE project_id=?""",
                        (
                            now,
                            json.dumps({"action": "assigned_to_space", "project_id": destination}),
                            old["id"],
                        ),
                    )
                    db.execute(
                        "UPDATE tasks SET project_id=?,revision=revision+1,updated_at=? WHERE project_id=?",
                        (destination, now, old["id"]),
                    )
                    db.execute(
                        "UPDATE activity SET project_id=? WHERE project_id=?",
                        (destination, old["id"]),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO project_aliases VALUES(?,?)",
                        (old["id"], destination),
                    )
                has_tasks = db.execute(
                    "SELECT 1 FROM tasks WHERE project_id=? LIMIT 1", (old["id"],)
                ).fetchone()
                # Ambiguous tasks stay accessible for manual assignment; old reports remain intact.
                db.execute(
                    "UPDATE projects SET hidden=? WHERE id=?", (not bool(has_tasks), old["id"])
                )

    def projects(self, include_hidden: bool = False) -> list[dict]:
        with self.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM projects "
                    + ("" if include_hidden else "WHERE hidden=0 ")
                    + "ORDER BY name,root"
                )
            ]
            for row in rows:
                row["roots"] = json.loads(row["roots"]) or ([row["root"]] if row["root"] else [])
            return rows

    def tasks(
        self, project_id: str = "", kind: str = "", include_archived: bool = False
    ) -> list[dict]:
        terms, args = ["1=1"], []
        if project_id:
            terms.append("project_id=?")
            args.append(project_id)
        if kind:
            terms.append("kind=?")
            args.append(kind)
        if not include_archived:
            terms.append("archived=0")
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM tasks WHERE "
                    + " AND ".join(terms)
                    + " ORDER BY status='completed',target,created_at",
                    args,
                )
            ]

    def task(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Task no longer exists")
            return dict(row)

    @staticmethod
    def _validate_task(db, values: dict) -> None:
        if values["kind"] not in {"daily", "weekly"} or values["status"] not in STATUSES:
            raise ValueError("Invalid task kind or status")
        if not 1 <= len(values["title"].strip()) <= 300 or len(values["notes"]) > 8000:
            raise ValueError("Use a title of 1–300 characters and notes of at most 8000 characters")
        from datetime import date

        if date.fromisoformat(values["target"]).isoformat() != values["target"]:
            raise ValueError("Use a planned date in YYYY-MM-DD format")
        if values.get("parent_id"):
            parent = db.execute(
                "SELECT project_id,kind FROM tasks WHERE id=?", (values["parent_id"],)
            ).fetchone()
            if (
                not parent
                or parent["kind"] != "weekly"
                or parent["project_id"] != values["project_id"]
                or values["kind"] != "daily"
            ):
                raise ValueError("A daily task may link to a weekly mission in the same project")

    def add_task(
        self,
        project_id: str,
        title: str,
        kind: str,
        target: str,
        notes: str = "",
        parent_id: str | None = None,
    ) -> str:
        values = dict(
            project_id=project_id,
            title=title.strip(),
            notes=notes,
            kind=kind,
            target=target,
            parent_id=parent_id,
            status="todo",
        )
        with self.connect() as db:
            self._validate_task(db, values)
            try:
                return self._insert_task(db, values, "manual")
            except sqlite3.IntegrityError as exc:
                raise Conflict(
                    "An open task with this title already exists, or its project is unavailable"
                ) from exc

    @staticmethod
    def _insert_task(db, values: dict, source: str, run_id=None, index=None) -> str:
        task_id, now = uuid.uuid4().hex, stamp(utcnow())
        db.execute(
            """INSERT INTO tasks(id,project_id,title,normalized_title,notes,kind,target,status,parent_id,
            source,origin_run,origin_index,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                values["project_id"],
                values["title"],
                normalize(values["title"]),
                values.get("notes", ""),
                values["kind"],
                values["target"],
                "todo",
                values.get("parent_id"),
                source,
                run_id,
                index,
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO task_events(task_id,at,data) VALUES(?,?,?)",
            (task_id, now, json.dumps({"action": "created", "source": source})),
        )
        return task_id

    def edit_task(self, task_id: str, revision: int, **changes) -> None:
        allowed = {
            "title",
            "notes",
            "kind",
            "target",
            "status",
            "parent_id",
            "archived",
            "project_id",
        }
        if set(changes) - allowed:
            raise ValueError("Unknown task fields")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["revision"] != revision:
                raise Conflict("This task changed in another panel. Reopen it and try again.")
            values = dict(row) | changes
            values["title"] = values["title"].strip()
            self._validate_task(db, values)
            if db.execute(
                "SELECT 1 FROM tasks WHERE parent_id=? AND project_id!=? LIMIT 1",
                (task_id, values["project_id"]),
            ).fetchone():
                raise ValueError(
                    "Move or unlink the mission's daily tasks before changing its project"
                )
            if (
                values["kind"] != "weekly"
                and db.execute(
                    "SELECT 1 FROM tasks WHERE parent_id=? LIMIT 1", (task_id,)
                ).fetchone()
            ):
                raise ValueError("Unlink the mission's daily tasks before changing its kind")
            now = stamp(utcnow())
            completed = (row["completed_at"] or now) if values["status"] == "completed" else None
            try:
                db.execute(
                    """UPDATE tasks SET project_id=?,title=?,normalized_title=?,notes=?,kind=?,target=?,
                    status=?,parent_id=?,archived=?,completed_at=?,updated_at=?,revision=revision+1 WHERE id=?""",
                    (
                        values["project_id"],
                        values["title"],
                        normalize(values["title"]),
                        values["notes"],
                        values["kind"],
                        values["target"],
                        values["status"],
                        values["parent_id"],
                        int(values["archived"]),
                        completed,
                        now,
                        task_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("An open task with this title already exists") from exc
            db.execute(
                "INSERT INTO task_events(task_id,at,data) VALUES(?,?,?)",
                (task_id, now, json.dumps(changes)),
            )

    def activity(self, project_id: str, agent_key: str, data: dict, at: str | None = None) -> bool:
        payload = json.dumps(data, sort_keys=True)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as db:
            previous = db.execute(
                "SELECT fingerprint FROM activity WHERE agent_key=? ORDER BY id DESC LIMIT 1",
                (agent_key,),
            ).fetchone()
            if previous and previous[0] == fingerprint:
                return False
            db.execute(
                "INSERT INTO activity(project_id,agent_key,at,fingerprint,data) VALUES(?,?,?,?,?)",
                (project_id, agent_key, at or stamp(utcnow()), fingerprint, payload),
            )
        return True

    def history(self, project_id: str, since: str, until: str, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row) | {"data": json.loads(row["data"])}
                for row in db.execute(
                    "SELECT at,data FROM activity WHERE project_id=? AND at>=? AND at<=? ORDER BY id DESC LIMIT ?",
                    (project_id, since, until, limit),
                )
            ]

    def prune(self, days: int) -> None:
        with self.connect() as db:
            cutoff = stamp(utcnow() - timedelta(days=days))
            db.execute("DELETE FROM activity WHERE at<?", (cutoff,))
            db.execute(
                "UPDATE runs SET evidence=NULL WHERE created_at<? AND status IN ('complete','partial')",
                (cutoff,),
            )
            db.execute(
                "UPDATE updates SET data='{}' WHERE at<? AND run_id IN (SELECT id FROM runs WHERE status IN ('complete','partial'))",
                (cutoff,),
            )

    def start_run(self, key: str, spec: dict) -> dict:
        now, run_id = stamp(utcnow()), uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO runs(id,run_key,created_at,updated_at,status,spec) VALUES(?,?,?,?,?,?)",
                (run_id, key, now, now, "collecting", json.dumps(spec)),
            )
            result = dict(db.execute("SELECT * FROM runs WHERE run_key=?", (key,)).fetchone())
        return self._decode_run(result)

    @staticmethod
    def _decode_run(row: dict) -> dict:
        for key in ("spec", "evidence", "summary", "plan"):
            if row[key]:
                row[key] = json.loads(row[key])
        return row

    def run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Unknown report run")
            return self._decode_run(dict(row))

    def runs(self, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            return [
                self._decode_run(dict(row))
                for row in db.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            ]

    def stage(
        self, run_id: str, status: str, *, field: str = "", value=None, error: str = ""
    ) -> None:
        if field not in {"", "evidence", "summary", "plan"}:
            raise ValueError("Invalid stage")
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?,updated_at=?,error=? WHERE id=?",
                (status, stamp(utcnow()), error[:2000], run_id),
            )
            if field:
                db.execute(f"UPDATE runs SET {field}=? WHERE id=?", (json.dumps(value), run_id))

    def save_summary(self, run_id: str, summary: dict) -> None:
        """A valid report survives independently of the subsequent planning stage."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE runs SET status='planning',summary=?,updated_at=?,error='' WHERE id=?",
                (json.dumps(summary), stamp(utcnow()), run_id),
            )
            for report in summary["projects"]:
                db.execute(
                    "INSERT OR REPLACE INTO reports VALUES(?,?,?,?)",
                    (run_id, report["project_id"], stamp(utcnow()), json.dumps(report)),
                )
                db.execute(
                    "UPDATE projects SET status=? WHERE id=?",
                    (report["status"], report["project_id"]),
                )

    def claim_update(self, run_id: str, agent_key: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "INSERT OR IGNORE INTO updates VALUES(?,?,?,?,?)",
                    (run_id, agent_key, stamp(utcnow()), "dispatching", "{}"),
                ).rowcount
                == 1
            )

    def save_update(self, run_id: str, agent_key: str, status: str, data: dict) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE updates SET status=?,data=? WHERE run_id=? AND agent_key=?",
                (status, json.dumps(data), run_id, agent_key),
            )

    def updates(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row) | {"data": json.loads(row["data"])}
                for row in db.execute("SELECT * FROM updates WHERE run_id=?", (run_id,))
            ]

    def update_pending(self, agent_key: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    """SELECT 1 FROM updates JOIN runs ON runs.id=updates.run_id
                WHERE agent_key=? AND updates.status IN ('dispatching','waiting','ambiguous')
                AND runs.status NOT IN ('complete','partial','failed') LIMIT 1""",
                    (agent_key,),
                ).fetchone()
                is not None
            )

    def finish(self, run_id: str, summary: dict, plan: dict, partial: bool = False) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row["status"] in {"complete", "partial"}:
                return
            spec = json.loads(row["spec"])
            known_projects = {r[0] for r in db.execute("SELECT id FROM projects")}
            for report in summary["projects"]:
                if report["project_id"] not in known_projects:
                    raise ValueError("Report references an unknown project")
                db.execute(
                    "INSERT OR REPLACE INTO reports VALUES(?,?,?,?)",
                    (run_id, report["project_id"], stamp(utcnow()), json.dumps(report)),
                )
                db.execute(
                    "UPDATE projects SET status=? WHERE id=?",
                    (report["status"], report["project_id"]),
                )
            for item in plan["tasks"]:
                if item["project_id"] not in known_projects:
                    raise ValueError("Plan references an unknown project")
                if item.get("existing_task_id"):
                    existing = db.execute(
                        "SELECT project_id FROM tasks WHERE id=?", (item["existing_task_id"],)
                    ).fetchone()
                    if not existing or existing[0] != item["project_id"]:
                        raise ValueError("Plan references an unknown task")
                    continue  # Existing tasks, including manual edits/completion, are immutable to AI.
                if item["kind"] == "weekly" and not spec["weekly"]:
                    raise ValueError("Daily run cannot create weekly missions")
                values = (
                    {"notes": ""}
                    | item
                    | {
                        "target": spec["target_day"]
                        if item["kind"] == "daily"
                        else spec["target_week"],
                        "status": "todo",
                    }
                )
                self._validate_task(db, values)
            db.execute(
                "UPDATE runs SET status=?,summary=?,plan=?,error='',updated_at=? WHERE id=?",
                (
                    "partial" if partial else "complete",
                    json.dumps(summary),
                    json.dumps(plan),
                    stamp(utcnow()),
                    run_id,
                ),
            )

    @staticmethod
    def _suggestion(db, run: dict, index: int) -> dict:
        item = dict(run["plan"]["tasks"][index])
        alias = db.execute(
            "SELECT project_id FROM project_aliases WHERE old_id=?", (item["project_id"],)
        ).fetchone()
        if alias:
            item["project_id"] = alias[0]
        accepted = db.execute(
            "SELECT task_id FROM suggestion_acceptances WHERE run_id=? AND item_index=?",
            (run["id"], index),
        ).fetchone()
        legacy = db.execute(
            "SELECT id FROM tasks WHERE origin_run=? AND origin_index=?", (run["id"], index)
        ).fetchone()
        task_id = (accepted or legacy or [None])[0]
        state = "added" if task_id else "pending"
        if not task_id:
            task_id = item.get("existing_task_id")
            duplicate = db.execute(
                "SELECT id FROM tasks WHERE project_id=? AND kind=? AND normalized_title=?",
                (item["project_id"], item["kind"], normalize(item["title"])),
            ).fetchone()
            task_id = task_id or (duplicate[0] if duplicate else None)
            if task_id:
                state = "existing"
        return item | {
            "run_id": run["id"],
            "index": index,
            "task_id": task_id,
            "state": state,
            "target": run["spec"]["target_day"]
            if item["kind"] == "daily"
            else run["spec"]["target_week"],
        }

    def suggestions(self, run_id: str, project_id: str = "") -> list[dict]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Unknown report run")
            run = self._decode_run(dict(row))
            if run["status"] not in {"complete", "partial"} or not run["plan"]:
                return []
            items = [self._suggestion(db, run, i) for i in range(len(run["plan"]["tasks"]))]
            return [item for item in items if not project_id or item["project_id"] == project_id]

    def accept_suggestion(self, run_id: str, index: int) -> str:
        """Explicit user acceptance; serialize clicks and preserve existing task content."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Unknown report run")
            run = self._decode_run(dict(row))
            if (
                run["status"] not in {"complete", "partial"}
                or not run["plan"]
                or not isinstance(index, int)
                or not 0 <= index < len(run["plan"]["tasks"])
            ):
                raise ValueError("Suggestion is not available")
            item = self._suggestion(db, run, index)
            task_id = item["task_id"]
            if task_id is None:
                values = {"notes": "", "status": "todo"} | item
                self._validate_task(db, values)
                task_id = self._insert_task(db, values, "ai", run_id, index)
            db.execute(
                "INSERT OR IGNORE INTO suggestion_acceptances VALUES(?,?,?)",
                (run_id, index, task_id),
            )
            return task_id

    def reports(self, project_id: str = "", limit: int = 100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT reports.*,projects.name,runs.spec FROM reports
                JOIN projects ON projects.id=reports.project_id JOIN runs ON runs.id=reports.run_id
                WHERE (?='' OR reports.project_id=? OR reports.project_id IN
                    (SELECT old_id FROM project_aliases WHERE project_id=?))
                ORDER BY reports.created_at DESC LIMIT ?""",
                (project_id, project_id, project_id, limit),
            )
            return [
                dict(row) | {"data": json.loads(row["data"]), "spec": json.loads(row["spec"])}
                for row in rows
            ]
