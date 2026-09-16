from concurrent.futures import ThreadPoolExecutor

import pytest
from textual.widgets import Button

from herdr_tasks.config import Config, Paths
from herdr_tasks.pipeline import make_spec
from herdr_tasks.schedule import utcnow
from herdr_tasks.store import Store
from herdr_tasks.ui import TaskApp


def proposal(store, *, kind="daily", project=None, title="Review the changes"):
    project = project or store.project("/example", "Example")
    spec = make_spec(utcnow(), Config(), store, weekly=True)
    run = store.start_run("manual:" + title + kind, spec)
    item = {
        "project_id": project,
        "title": title,
        "notes": "Run checks first.",
        "kind": kind,
        "existing_task_id": None,
        "parent_id": None,
    }
    store.finish(run["id"], {"coverage": "Test evidence", "projects": []}, {"tasks": [item]})
    return run["id"], project


def test_suggestions_persist_until_explicit_acceptance(tmp_path):
    store = Store(tmp_path / "tasks.db")
    run_id, _ = proposal(store)
    reopened = Store(store.path)
    assert not reopened.tasks()
    assert reopened.suggestions(run_id)[0]["state"] == "pending"
    task_id = reopened.accept_suggestion(run_id, 0)
    original = reopened.task(task_id)
    assert original["status"] == "todo" and original["notes"] == "Run checks first."
    reopened.edit_task(task_id, original["revision"], title="My edited title", status="completed")
    edited = reopened.task(task_id)
    assert Store(store.path).accept_suggestion(run_id, 0) == task_id
    assert reopened.task(task_id) == edited
    assert reopened.suggestions(run_id)[0]["state"] == "added"


def test_concurrent_acceptance_adds_only_one_task(tmp_path):
    store = Store(tmp_path / "tasks.db")
    run_id, _ = proposal(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.accept_suggestion(run_id, 0), range(2)))
    assert results[0] == results[1]
    assert len(store.tasks()) == 1


def test_existing_completed_task_is_not_reopened_and_acceptance_is_remembered(tmp_path):
    store = Store(tmp_path / "tasks.db")
    run_id, project = proposal(store)
    task_id = store.add_task(project, "Review the changes", "daily", "2026-09-16")
    store.edit_task(task_id, 1, status="completed", notes="My own notes")
    original = store.task(task_id)
    assert store.suggestions(run_id)[0]["state"] == "existing"
    assert store.accept_suggestion(run_id, 0) == task_id
    assert store.task(task_id) == original
    store.edit_task(task_id, original["revision"], title="Renamed later")
    assert store.accept_suggestion(run_id, 0) == task_id
    assert len(store.tasks()) == 1


def test_incomplete_runs_and_invalid_indexes_cannot_be_accepted(tmp_path):
    store = Store(tmp_path / "tasks.db")
    pending = store.start_run("incomplete", {})
    assert not store.suggestions(pending["id"])
    with pytest.raises(ValueError):
        store.accept_suggestion(pending["id"], 0)
    run_id, _ = proposal(store)
    for index in (-1, 1):
        with pytest.raises(ValueError):
            store.accept_suggestion(run_id, index)
    assert not store.tasks()


def test_legacy_automatically_added_tasks_are_recognized(tmp_path):
    store = Store(tmp_path / "tasks.db")
    run_id, project = proposal(store)
    with store.connect() as db:
        task_id = store._insert_task(
            db,
            {
                "project_id": project,
                "title": "Legacy task",
                "kind": "daily",
                "target": "2026-09-16",
            },
            "ai",
            run_id,
            0,
        )
    assert store.suggestions(run_id)[0]["state"] == "added"
    assert store.accept_suggestion(run_id, 0) == task_id
    assert len(store.tasks()) == 1


@pytest.mark.parametrize("kind", ["daily", "weekly"])
async def test_add_suggestion_from_reports(tmp_path, kind):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    store = Store(paths.database)
    run_id, project = proposal(store, kind=kind)
    other = store.project("/other", "Other")
    assert len(store.suggestions(run_id, project)) == 1
    assert not store.suggestions(run_id, other)
    app = TaskApp(paths, start_coordinator=False, view="reports")
    async with app.run_test(size=(46, 48)) as pilot:
        await pilot.pause()
        assert not store.tasks()
        selector = f"#add-suggestion-{run_id}-0"
        button = app.query_one(selector, Button)
        button.scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click(selector)
        await pilot.pause()
        assert len(store.tasks()) == 1
        task = store.tasks()[0]
        assert task["kind"] == kind and task["status"] == "todo"
        assert task["project_id"] == project
        assert app.query_one(selector, Button).disabled
        assert str(app.query_one(selector, Button).label) == "Added"
