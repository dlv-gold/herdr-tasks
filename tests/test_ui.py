from datetime import timedelta

from textual.widgets import Checkbox, Input, Select, TabbedContent

from herdr_tasks.config import Config, Paths
from herdr_tasks.schedule import period_keys, utcnow
from herdr_tasks.store import Store
from herdr_tasks.ui import SettingsEditor, TaskApp, TaskEditor


async def test_create_complete_and_reopen_task_in_panel(tmp_path):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    store = Store(paths.database)
    project = store.project("/work/project", "Project")
    app = TaskApp(paths, start_coordinator=False)
    async with app.run_test(size=(46, 48)) as pilot:
        await pilot.click("#new-task")
        await pilot.pause()
        assert isinstance(app.screen, TaskEditor)
        app.screen.query_one("#task-title", Input).value = "Verify plugin"
        await pilot.click("#save")
        await pilot.pause()
        task = store.tasks(project)[0]
        await pilot.click("#check-" + task["id"])
        await pilot.pause()
        assert store.task(task["id"])["status"] == "completed"
        await pilot.click("#check-" + task["id"])
        await pilot.pause()
        assert store.task(task["id"])["status"] == "todo"


async def test_refresh_does_not_reset_in_progress_and_narrow_panel(tmp_path):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    store = Store(paths.database)
    project = store.project("/work/p", "P")
    task_id = store.add_task(project, "Long title " * 9, "daily", "2026-09-15")
    store.edit_task(task_id, 1, status="in_progress")
    app = TaskApp(paths, start_coordinator=False)
    async with app.run_test(size=(32, 40)) as pilot:
        await pilot.pause()
        assert store.task(task_id)["status"] == "in_progress"
        await pilot.resize_terminal(25, 30)
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).region.width <= 25


async def test_provider_settings_saved_and_reloaded(tmp_path):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    app = TaskApp(paths, start_coordinator=False)
    async with app.run_test(size=(80, 60)) as pilot:
        await pilot.click("#settings")
        await pilot.pause()
        assert isinstance(app.screen, SettingsEditor)
        app.screen.query_one("#planner-provider", Select).value = "claude"
        app.screen.query_one("#planner-model", Input).value = "example-model"
        app.screen.query_one("#weekly-day", Select).value = 4
        app.screen.save()
        await pilot.pause()
        config = Config.load(paths)
        assert config.planner.provider == "claude"
        assert config.planner.model == "example-model"
        assert config.weekly_day == 4


async def test_editor_escape_preserves_task(tmp_path):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    store = Store(paths.database)
    project = store.project("/work/p", "P")
    store.add_task(project, "Keep this", "weekly", "2026-09-12")
    app = TaskApp(paths, start_coordinator=False, view="week")
    async with app.run_test(size=(50, 40)) as pilot:
        await pilot.press("m")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert len(store.tasks()) == 1
        assert not isinstance(app.screen, TaskEditor)


async def test_compact_panel_shows_tasks_and_future_tasks_are_in_history(tmp_path):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    store = Store(paths.database)
    project = store.project("/work/p", "P")
    now = utcnow()
    current = store.add_task(project, "Visible today", "daily", period_keys(now, Config())[0])
    future = store.add_task(
        project, "Later task", "daily", period_keys(now + timedelta(days=2), Config())[0]
    )
    app = TaskApp(paths, start_coordinator=False)
    async with app.run_test(size=(32, 24)) as pilot:
        await pilot.pause()
        assert app.screen.has_class("compact")
        check = app.query_one("#check-" + current, Checkbox)
        assert check.region.bottom < 24
        assert not app.query("#check-" + future)
        await pilot.press("h")
        await pilot.pause()
        assert app.query("#check-" + future)


async def test_panel_defaults_to_its_space_and_can_show_all_spaces(tmp_path):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    store = Store(paths.database)
    store.sync_spaces(
        [
            {
                "endpoint": "/session",
                "session": "local",
                "workspace_id": wid,
                "name": name,
                "roots": ["/same-folder"],
            }
            for wid, name in (("w1", "First"), ("w2", "Second"))
        ]
    )
    day = period_keys(utcnow(), Config())[0]
    first = store.add_task(store.space_id("/session", "w1"), "First task", "daily", day)
    second = store.add_task(store.space_id("/session", "w2"), "Second task", "daily", day)
    app = TaskApp(paths, start_coordinator=False, workspace_id="w2", endpoint="/session")
    async with app.run_test(size=(46, 48)) as pilot:
        await pilot.pause()
        assert app.selected_project() == store.space_id("/session", "w2")
        assert app.query("#check-" + second) and not app.query("#check-" + first)
        await pilot.click("#new-task")
        await pilot.pause()
        assert app.screen.query_one("#task-project", Select).value == store.space_id(
            "/session", "w2"
        )
        await pilot.press("escape")
        app.query_one("#project", Select).value = ""
        await pilot.pause()
        assert app.query("#check-" + first) and app.query("#check-" + second)
