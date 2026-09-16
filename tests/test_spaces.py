import copy
import sqlite3

from herdr_tasks.collector import Collector
from herdr_tasks.config import Config
from herdr_tasks.store import Store


def space(workspace_id="w1", name="Research", roots=None, endpoint="/session"):
    return {
        "workspace_id": workspace_id,
        "name": name,
        "roots": roots or ["/repo"],
        "endpoint": endpoint,
        "session": "local",
    }


def test_two_spaces_using_same_folder_stay_separate(tmp_path):
    store = Store(tmp_path / "tasks.db")
    store.sync_spaces([space(), space("w2", "Experiments")])
    projects = store.projects()
    assert len(projects) == 2
    assert {p["name"] for p in projects} == {"Research", "Experiments"}
    assert {p["root"] for p in projects} == {"/repo"}
    assert len({p["id"] for p in projects}) == 2


def test_space_identity_survives_rename_and_directory_change(tmp_path):
    store = Store(tmp_path / "tasks.db")
    store.sync_spaces([space()])
    project = store.projects()[0]["id"]
    task = store.add_task(project, "My task", "daily", "2026-09-15")
    store.edit_task(task, 1, status="completed", notes="Keep my notes")
    store.sync_spaces([space(name="Renamed", roots=["/different-repo"])])
    assert store.projects()[0]["id"] == project
    assert store.projects()[0]["name"] == "Renamed"
    assert store.task(task)["status"] == "completed"
    assert store.task(task)["notes"] == "Keep my notes"


def test_space_ids_are_scoped_to_local_session(tmp_path):
    store = Store(tmp_path / "tasks.db")
    store.sync_spaces([space(endpoint="/first"), space(endpoint="/second")])
    assert len(store.projects()) == 2


def test_migration_preserves_tasks_links_activity_and_old_reports(tmp_path):
    store = Store(tmp_path / "tasks.db")
    old = store.project("/repo", "Old folder name")
    mission = store.add_task(old, "Mission", "weekly", "2026-09-12")
    task = store.add_task(old, "Daily", "daily", "2026-09-15", notes="My notes", parent_id=mission)
    store.edit_task(task, 1, status="completed")
    before = store.task(task)
    store.activity(old, "agent", {"text": "Earlier progress"})
    run = store.start_run("old-report", {"weekly": False})
    summary = {"projects": [{"project_id": old, "status": "on_track", "summary": "Old report"}]}
    store.save_summary(run["id"], summary)
    store.stage(run["id"], "partial")
    store.sync_spaces([space()])
    project = store.projects()[0]["id"]
    assert store.projects()[0]["name"] == "Research"
    after = store.task(task)
    assert after["project_id"] == store.task(mission)["project_id"] == project
    for key in ("id", "title", "notes", "status", "completed_at", "parent_id", "source", "target"):
        assert after[key] == before[key]
    assert store.history(project, "2000", "9999")[0]["data"]["text"] == "Earlier progress"
    assert store.reports(project)[0]["data"]["summary"] == "Old report"
    assert store.run(run["id"])["summary"] == summary
    store.sync_spaces([space()])
    assert len(store.tasks()) == 2 and store.task(task)["revision"] == after["revision"]
    with store.connect() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_ambiguous_existing_tasks_remain_available_for_manual_assignment(tmp_path):
    store = Store(tmp_path / "tasks.db")
    old = store.project("/repo", "Old")
    task = store.add_task(old, "Keep me", "daily", "2026-09-15")
    store.sync_spaces([space(), space("w2", "Another")])
    assert len(store.projects()) == 3
    assert store.task(task)["project_id"] == old
    store.edit_task(task, 1, project_id=store.space_id("/session", "w2"))
    store.sync_spaces([space(), space("w2", "Another")])
    assert len(store.projects()) == 2


def test_conflicting_legacy_titles_are_preserved(tmp_path):
    store = Store(tmp_path / "tasks.db")
    for root in ("/one", "/two"):
        store.add_task(store.project(root, root), "Same title", "daily", "2026-09-15")
    store.sync_spaces([space(roots=["/one", "/two"])])
    assert len(store.tasks()) == 2
    assert len(store.projects()) == 2  # One space plus one unassigned previous project.


def test_task_migration_waits_for_an_in_flight_report(tmp_path):
    store = Store(tmp_path / "tasks.db")
    old = store.project("/repo", "Old")
    task = store.add_task(old, "Keep me", "daily", "2026-09-15")
    run = store.start_run("running", {})
    store.sync_spaces([space()])
    assert store.task(task)["project_id"] == old
    store.stage(run["id"], "complete")
    store.sync_spaces([space()])
    assert store.task(task)["project_id"] == store.space_id("/session", "w1")


def test_v1_schema_upgrade_keeps_backup_and_original_project(tmp_path):
    path = tmp_path / "tasks.db"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE projects(
            id TEXT PRIMARY KEY, root TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unknown');
            INSERT INTO projects VALUES('old','/repo','Old','2026-09-15','unknown');
            PRAGMA user_version=1;""")
    store = Store(path)
    assert store.projects()[0]["id"] == "old"
    assert store.projects()[0]["root"] == "/repo"
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    with sqlite3.connect(path.with_name(path.name + ".before-spaces")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT root FROM projects").fetchone()[0] == "/repo"


def test_collector_groups_all_space_agents_and_discovers_empty_spaces(tmp_path, monkeypatch):
    monkeypatch.setattr("herdr_tasks.collector.project_root", lambda cwd: (cwd, cwd))
    agents = [
        {
            "pane_id": f"w1:p{i}",
            "workspace_id": "w1",
            "terminal_id": f"t{i}",
            "agent": "codex",
            "agent_status": "working",
            "cwd": cwd,
        }
        for i, cwd in enumerate(("/one", "/two"))
    ]
    snapshot = {
        "workspaces": [
            {"workspace_id": "w1", "label": "Research"},
            {"workspace_id": "w2", "label": "Empty"},
        ],
        "agents": agents,
        "panes": agents,
    }

    class Host:
        def sessions(self):
            return [{"name": "local", "socket_path": "/session"}]

        def snapshot(self, endpoint):
            return copy.deepcopy(snapshot)

        def read(self, *args):
            return "Activity"

    store = Store(tmp_path / "tasks.db")
    records, gaps = Collector(Host(), store, Config()).scan()
    assert not gaps and len(store.projects()) == 2
    assert {r["project_id"] for r in records} == {store.space_id("/session", "w1")}
    assert next(p for p in store.projects() if p["name"] == "Research")["roots"] == ["/one", "/two"]
