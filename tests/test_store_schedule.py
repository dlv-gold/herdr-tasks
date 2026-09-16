from datetime import UTC, datetime

import pytest

from herdr_tasks.config import Config, Paths, Role
from herdr_tasks.schedule import boundary, latest, next_boundary, period_keys
from herdr_tasks.store import Conflict, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "tasks.db")


def test_manual_task_persistence_and_edit_conflict(store):
    project = store.project("/work/project", "Project")
    task = store.add_task(project, "Ship plugin", "daily", "2026-09-15")
    store.edit_task(task, 1, status="completed", notes="Verified")
    reopened = Store(store.path).task(task)
    assert reopened["completed_at"] and reopened["status"] == "completed"
    with pytest.raises(Conflict):
        store.edit_task(task, 1, title="Old edit")
    store.edit_task(task, 2, status="todo")
    assert store.task(task)["completed_at"] is None


def test_ai_cannot_change_existing_tasks_and_retry_does_not_duplicate(store):
    project = store.project("/work/p", "P")
    task = store.add_task(project, "Manual mission", "weekly", "2026-09-12")
    store.edit_task(task, 1, status="completed", notes="My words")
    run = store.start_run(
        "scheduled:one", {"weekly": True, "target_day": "2026-09-19", "target_week": "2026-09-19"}
    )
    summary = {"projects": [{"project_id": project, "status": "on_track"}]}
    plan = {
        "tasks": [
            {
                "project_id": project,
                "existing_task_id": task,
                "title": "Overwrite",
                "kind": "weekly",
                "notes": "AI",
            },
            {
                "project_id": project,
                "existing_task_id": None,
                "title": "Next step",
                "kind": "daily",
                "notes": "",
            },
        ]
    }
    store.finish(run["id"], summary, plan)
    store.finish(run["id"], summary, plan)
    assert len(store.tasks()) == 1
    added = store.accept_suggestion(run["id"], 1)
    assert store.accept_suggestion(run["id"], 1) == added
    assert len(store.tasks()) == 2
    assert store.task(task)["notes"] == "My words"
    assert store.task(task)["status"] == "completed"
    assert store.start_run("scheduled:one", {})["id"] == run["id"]


def test_invalid_plan_rolls_back_all_insertions(store):
    project = store.project("/work/p", "P")
    run = store.start_run("a", {"weekly": False, "target_day": "2026-09-15"})
    items = [
        {"project_id": project, "title": "Valid first", "kind": "daily"},
        {"project_id": "missing", "title": "Invalid second", "kind": "daily"},
    ]
    with pytest.raises(ValueError):
        store.finish(run["id"], {"projects": []}, {"tasks": items})
    assert not store.tasks()


def test_cross_project_parent_rejected_and_open_titles_deduplicated(store):
    p, q = store.project("/p", "P"), store.project("/q", "Q")
    mission = store.add_task(p, "Mission", "weekly", "2026-09-12")
    with pytest.raises(ValueError):
        store.add_task(q, "Task", "daily", "2026-09-15", parent_id=mission)
    with pytest.raises(Conflict):
        store.add_task(p, "  MISSION ", "weekly", "2026-09-19")


def test_agent_update_dispatch_is_durable_and_activity_deduplicated(store):
    p = store.project("/p", "P")
    run = store.start_run("a", {})
    assert store.claim_update(run["id"], "session:agent")
    assert not Store(store.path).claim_update(run["id"], "session:agent")
    assert store.activity(p, "a", {"status": "done", "text": "finished"})
    assert not store.activity(p, "a", {"status": "done", "text": "finished"})


def test_three_am_day_and_saturday_week():
    config = Config()
    before = datetime(2026, 9, 18, 23, 59, tzinfo=UTC)
    after = datetime(2026, 9, 19, 0, 0, tzinfo=UTC)
    assert period_keys(before, config) == ("2026-09-18", "2026-09-12")
    assert period_keys(after, config) == ("2026-09-19", "2026-09-19")
    assert latest(after, config) == latest(after, config, True)
    assert next_boundary(after, config).date().isoformat() == "2026-09-20"


def test_dst_gap_and_fold_have_single_stable_boundary():
    from datetime import date

    gap = boundary(date(2026, 3, 8), "02:30", "America/New_York")
    assert gap == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    fold = boundary(date(2026, 11, 1), "01:30", "America/New_York")
    assert fold == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_config_roundtrip_and_invalid_settings(tmp_path):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    config = Config(planner=Role(provider="claude", model="example", effort="high"))
    config.save(paths)
    assert Config.load(paths) == config
    config.weekly_day = 9
    with pytest.raises(ValueError):
        config.save(paths)


def test_retention_keeps_agent_dispatch_claims(store):
    run = store.start_run("old", {})
    assert store.claim_update(run["id"], "agent")
    store.save_update(run["id"], "agent", "ambiguous", {"reason": "timeout"})
    store.stage(run["id"], "failed")
    with store.connect() as db:
        db.execute("UPDATE updates SET at='2000-01-01T00:00:00+00:00'")
    store.prune(35)
    assert not store.claim_update(run["id"], "agent")
    assert store.updates(run["id"])[0]["status"] == "ambiguous"


@pytest.mark.parametrize(
    "data", ["[]", '{"summary":null}', '{"panel_fraction":"wide"}', '{"timezone":null}']
)
def test_malformed_config_has_a_readable_error(tmp_path, data):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    paths.create()
    (paths.config / "config.json").write_text(data)
    with pytest.raises(ValueError):
        Config.load(paths)
