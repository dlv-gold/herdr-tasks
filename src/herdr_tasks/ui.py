"""Mouse and keyboard task board, designed for a narrow Herdr split."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Input,
    Label,
    Markdown,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)

from .collector import Collector
from .config import Config, Paths, Role
from .daemon import ensure_running, launch_background
from .herdr import Herdr
from .pipeline import report_markdown
from .schedule import latest, next_boundary, parse, period_keys, utcnow
from .store import STATUS_LABELS, Store


def project_names(projects: list[dict]) -> dict[str, str]:
    names = {}
    grouped = any(p["workspace_id"] for p in projects)
    for project in projects:
        name = project["name"]
        if grouped and not project["workspace_id"]:
            name = "Unassigned: " + name
        elif sum(p["name"] == name for p in projects) > 1:
            name += f" ({project['session_name']}/{project['workspace_id']})"
        names[project["id"]] = name
    return names


class Editor(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    Editor { align: center middle; }
    Editor > VerticalScroll { width: 74; max-width: 100%; height: 95%; border: round $accent; padding: 0 1; background: $surface; }
    Editor Input, Editor Select { margin-bottom: 1; }
    Editor TextArea { height: 7; margin-bottom: 1; }
    Editor .buttons { height: auto; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; }
    Editor Button { min-width: 10; width: 100%; }
    Editor #error { color: $error; height: auto; }
    """

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    def error(self, exc: Exception) -> None:
        self.query_one("#error", Static).update(Text(str(exc)))


class TaskEditor(Editor):
    def __init__(
        self,
        store: Store,
        config: Config,
        kind: str,
        project_id: str = "",
        task: dict | None = None,
    ):
        super().__init__()
        self.store, self.config, self.kind, self.task_data = store, config, kind, task
        self.project_id = task["project_id"] if task else project_id

    def compose(self) -> ComposeResult:
        projects = self.store.projects()
        if self.store.get_meta("grouping") == "spaces":
            projects = [
                p
                for p in projects
                if p["workspace_id"] or (self.task_data and p["id"] == self.task_data["project_id"])
            ]
        names = project_names(projects)
        if self.project_id not in {p["id"] for p in projects}:
            self.project_id = ""
        first = self.project_id or (projects[0]["id"] if projects else Select.NULL)
        day, week = period_keys(utcnow(), self.config)
        values = self.task_data or {
            "title": "",
            "notes": "",
            "status": "todo",
            "target": day if self.kind == "daily" else week,
        }
        with VerticalScroll():
            yield Label(
                "Edit task"
                if self.task_data
                else ("New weekly mission" if self.kind == "weekly" else "New daily task")
            )
            yield Label("Space")
            yield Select(
                [(names[p["id"]], p["id"]) for p in projects],
                value=first,
                allow_blank=not bool(projects),
                id="task-project",
            )
            yield Label("Title")
            yield Input(
                values["title"],
                placeholder="What needs to be done?",
                id="task-title",
                max_length=300,
            )
            yield Label("Notes")
            yield TextArea(values["notes"], id="task-notes")
            yield Label("Planned date / week starting (YYYY-MM-DD)")
            yield Input(values["target"], id="task-target")
            yield Label("Status")
            yield Select(
                [(label, value) for value, label in STATUS_LABELS.items()],
                value=values["status"],
                allow_blank=False,
                id="task-status",
            )
            yield Label("Weekly mission (optional)")
            missions = [
                (t["title"], t["id"])
                for t in self.store.tasks(str(first), "weekly")
                if t["id"] != values.get("id")
            ]
            yield Select(
                missions,
                prompt="No linked mission",
                value=values.get("parent_id") or Select.NULL,
                disabled=self.kind == "weekly",
                id="task-parent",
            )
            yield Static("", id="error")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")
                if self.task_data:
                    yield Button(
                        "Restore" if self.task_data["archived"] else "Archive", id="archive"
                    )

    @on(Select.Changed, "#task-project")
    def project_changed(self, event: Select.Changed) -> None:
        if not self.is_mounted:
            return
        parent = self.query_one("#task-parent", Select)
        prior = parent.value
        options = [(t["title"], t["id"]) for t in self.store.tasks(str(event.value), "weekly")]
        parent.set_options(options)
        if prior in {value for _, value in options}:
            parent.value = prior

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        try:
            project = self.query_one("#task-project", Select).value
            if project is Select.NULL:
                raise ValueError("Refresh Herdr spaces before creating a task")
            parent = self.query_one("#task-parent", Select).value
            changes = {
                "project_id": str(project),
                "title": self.query_one("#task-title", Input).value,
                "notes": self.query_one("#task-notes", TextArea).text,
                "target": self.query_one("#task-target", Input).value,
                "status": str(self.query_one("#task-status", Select).value),
                "parent_id": None if parent is Select.NULL else str(parent),
            }
            if self.task_data:
                self.store.edit_task(self.task_data["id"], self.task_data["revision"], **changes)
            else:
                status = changes.pop("status")
                task_id = self.store.add_task(kind=self.kind, **changes)
                if status != "todo":
                    self.store.edit_task(task_id, 1, status=status)
            self.dismiss(True)
        except (ValueError, OSError) as exc:
            self.error(exc)

    @on(Button.Pressed, "#archive")
    def archive(self) -> None:
        try:
            self.store.edit_task(
                self.task_data["id"],
                self.task_data["revision"],
                archived=not self.task_data["archived"],
            )
            self.dismiss(True)
        except ValueError as exc:
            self.error(exc)


class SettingsEditor(Editor):
    def __init__(self, paths: Paths, config: Config):
        super().__init__()
        self.paths, self.config = paths, config

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Schedules and report agents")
            yield Label("Automatic reports")
            yield Switch(self.config.enabled, id="enabled")
            yield Label("Timezone")
            yield Input(self.config.timezone, id="timezone")
            yield Label("Daily cutoff (HH:MM)")
            yield Input(self.config.daily_time, id="daily-time")
            yield Label("Weekly cutoff")
            yield Select(
                [
                    (day, i)
                    for i, day in enumerate(
                        (
                            "Monday",
                            "Tuesday",
                            "Wednesday",
                            "Thursday",
                            "Friday",
                            "Saturday",
                            "Sunday",
                        )
                    )
                ],
                value=self.config.weekly_day,
                allow_blank=False,
                id="weekly-day",
            )
            yield Input(self.config.weekly_time, id="weekly-time")
            for name in ("summary", "planner"):
                role = getattr(self.config, name)
                yield Label("Summarizer" if name == "summary" else "Planner")
                yield Select(
                    [("Codex", "codex"), ("Claude", "claude")],
                    value=role.provider,
                    allow_blank=False,
                    id=name + "-provider",
                )
                yield Input(role.model, placeholder="Model: CLI default", id=name + "-model")
                yield Select(
                    [("Effort: CLI default", "")]
                    + [
                        (v.title(), v)
                        for v in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
                    ],
                    value=role.effort,
                    allow_blank=False,
                    id=name + "-effort",
                )
            yield Label("Agent reply timeout (seconds)")
            yield Input(str(self.config.agent_timeout), type="integer", id="agent-timeout")
            yield Label("Summary / planner timeout (seconds each)")
            yield Input(str(self.config.provider_timeout), type="integer", id="provider-timeout")
            yield Static("", id="error")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        try:
            config = replace(
                self.config,
                enabled=self.query_one("#enabled", Switch).value,
                timezone=self.query_one("#timezone", Input).value.strip(),
                daily_time=self.query_one("#daily-time", Input).value.strip(),
                weekly_day=int(self.query_one("#weekly-day", Select).value),
                weekly_time=self.query_one("#weekly-time", Input).value.strip(),
                agent_timeout=int(self.query_one("#agent-timeout", Input).value),
                provider_timeout=int(self.query_one("#provider-timeout", Input).value),
            )
            for name in ("summary", "planner"):
                setattr(
                    config,
                    name,
                    Role(
                        provider=str(self.query_one("#" + name + "-provider", Select).value),
                        model=self.query_one("#" + name + "-model", Input).value.strip(),
                        effort=str(self.query_one("#" + name + "-effort", Select).value),
                        binary=getattr(self.config, name).binary,
                    ),
                )
            config.save(self.paths)
            self.dismiss(True)
        except (ValueError, OSError) as exc:
            self.error(exc)


class TaskRow(Vertical):
    def __init__(self, task: dict, name: str, *, expanded: bool = False):
        super().__init__(id="row-" + task["id"], classes="task-row")
        self.task_data, self.project_name = task, name
        self.expanded = expanded

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Checkbox(
                Text(self.task_data["title"]),
                value=self.task_data["status"] == "completed",
                id="check-" + self.task_data["id"],
                disabled=bool(self.task_data["archived"]),
            )
            yield Button("Edit", id="edit-" + self.task_data["id"], classes="edit-task")
            yield Button(
                "▾" if self.expanded else "▸",
                id="description-toggle-" + self.task_data["id"],
                classes="description-toggle",
                tooltip="Hide description" if self.expanded else "Show description",
            )
        status = (
            "Archived" if self.task_data["archived"] else STATUS_LABELS[self.task_data["status"]]
        )
        yield Static(
            Text(
                f"{self.project_name} · {status}\n{self.task_data['target']} · {'AI' if self.task_data['source'] == 'ai' else 'Manual'}"
            ),
            classes="task-detail",
        )
        description = Static(
            Text(self.task_data["notes"] or "No description yet. Use Edit to add one."),
            id="description-" + self.task_data["id"],
            classes="task-description",
        )
        description.display = self.expanded
        yield description

    def toggle_description(self) -> bool:
        self.expanded = not self.expanded
        self.query_one(".task-description", Static).display = self.expanded
        button = self.query_one(".description-toggle", Button)
        button.label = "▾" if self.expanded else "▸"
        button.tooltip = "Hide description" if self.expanded else "Show description"
        return self.expanded


class SuggestionRow(Vertical):
    def __init__(self, suggestion: dict, space: str):
        super().__init__(classes="suggestion-row")
        self.suggestion, self.space = suggestion, space

    def compose(self) -> ComposeResult:
        item = self.suggestion
        yield Static(Text(item["title"], style="bold"))
        yield Static(Text(f"{self.space} · {item['kind'].title()} · {item['target']}"))
        yield Static(Text(item.get("notes") or "No description provided."))
        label = {"added": "Added", "existing": "Already tracked"}.get(
            item["state"], "Add mission" if item["kind"] == "weekly" else "Add to tasks"
        )
        yield Button(
            label,
            id=f"add-suggestion-{item['run_id']}-{item['index']}",
            disabled=item["state"] != "pending",
            variant="primary",
        )


class TaskApp(App):
    TITLE = "Herdr Tasks"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("n", "new_task", "New task"),
        ("m", "new_mission", "Mission"),
        ("s", "settings", "Settings"),
        ("r", "daily", "Run report"),
        ("h", "history", "History"),
        ("q", "quit", "Close"),
        ("escape", "quit", "Close"),
    ]
    CSS = """
    Screen { background: $background; }
    #heading { height: 2; color: $accent; text-style: bold; padding: 0 1; }
    #top { height: 3; }
    #project { width: 1fr; }
    #refresh-spaces { min-width: 5; width: 5; }
    #toolbar { height: auto; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; }
    #toolbar Button { width: 100%; min-width: 10; }
    #status { height: auto; max-height: 5; padding: 0 1; color: $text-muted; }
    #overview { height: auto; max-height: 5; padding: 0 1; }
    #history-toggle { height: 3; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    .task-list { height: 1fr; }
    .task-row { height: auto; border-bottom: solid $surface; padding: 0 1 1 0; }
    .task-row Horizontal { height: auto; }
    .task-row Checkbox { width: 1fr; height: auto; min-height: 3; }
    .edit-task { min-width: 6; width: 6; }
    .task-detail { color: $text-muted; height: auto; padding-left: 1; }
    .description-toggle { width: 5; min-width: 5; }
    .task-description { height: auto; padding: 1; }
    .empty { padding: 1; color: $text-muted; height: auto; }
    #report-body { padding: 0 1; }
    #report-scroll { height: 1fr; }
    #report-actions { height: auto; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; }
    #report-actions Button { min-width: 10; width: 100%; }
    #suggestions { height: auto; }
    .suggestion-row { height: auto; border-top: solid $surface; padding: 1; }
    .suggestion-row Static { height: auto; margin-bottom: 1; }
    .suggestion-row Button { width: 100%; min-width: 0; }
    Screen.compact #heading, Screen.compact #history-toggle { display: none; }
    Screen.compact #run-daily, Screen.compact #settings { display: none; }
    Screen.compact #toolbar { height: 3; }
    Screen.compact #status { max-height: 1; }
    Screen.compact #overview { max-height: 2; }
    """

    def __init__(
        self,
        paths: Paths,
        *,
        start_coordinator: bool = True,
        view: str = "today",
        workspace_id: str | None = None,
        endpoint: str | None = None,
    ):
        super().__init__()
        self.paths = paths
        self.store = Store(paths.database)
        self.config = Config.load(paths)
        self.start_coordinator = start_coordinator
        self.initial_view = view
        self.project_options = []
        self.board_signature = ""
        self.report_options = []
        self.report_signature = ""
        self.rendering = False
        self.expanded_tasks: set[str] = set()
        self.terminal_id = ""
        workspace_id = (
            workspace_id
            or os.environ.get("HERDR_TASKS_WORKSPACE_ID")
            or os.environ.get("HERDR_WORKSPACE_ID")
        )
        endpoint = endpoint or os.environ.get("HERDR_SOCKET_PATH")
        self.preferred_project = (
            self.store.space_id(endpoint, workspace_id) if endpoint and workspace_id else ""
        )

    def compose(self) -> ComposeResult:
        yield Static("HERDR  /  TASKS", id="heading")
        with Horizontal(id="top"):
            yield Select([("All spaces", "")], value="", allow_blank=False, id="project")
            yield Button("↻", id="refresh-spaces", tooltip="Refresh Herdr spaces")
        with Horizontal(id="toolbar"):
            yield Button("+ Daily task", id="new-task", variant="primary")
            yield Button("+ Mission", id="new-mission")
            yield Button("Run daily", id="run-daily")
            yield Button("Settings", id="settings")
        yield Static("", id="status", markup=False)
        yield Static("", id="overview", markup=False)
        yield Checkbox("All dates / archived", id="history-toggle")
        with TabbedContent(initial="today", id="tabs"):
            with TabPane("Today", id="today"):
                yield VerticalScroll(id="daily-list", classes="task-list")
            with TabPane("Week", id="week"):
                yield VerticalScroll(id="weekly-list", classes="task-list")
            with TabPane("Reports", id="reports"):
                yield Select([], prompt="No reports yet", id="report-select")
                with Horizontal(id="report-actions"):
                    yield Button("Run weekly", id="run-weekly")
                    yield Button("Retry / Export", id="retry-report")
                with VerticalScroll(id="report-scroll"):
                    yield Markdown(
                        "No reports yet. Run a daily report to get started.", id="report-body"
                    )
                    yield Vertical(id="suggestions")
        yield Footer()

    async def on_mount(self) -> None:
        self.theme = "textual-dark"
        self.screen.set_class(self.size.height < 36, "compact")
        if self.initial_view in {"today", "week", "reports"}:
            self.query_one("#tabs", TabbedContent).active = self.initial_view
        await self.refresh_board()
        self.set_interval(2, self.refresh_board)
        if self.start_coordinator:
            await asyncio.to_thread(ensure_running, self.paths)
            try:
                result = await asyncio.to_thread(
                    Herdr().call,
                    os.environ["HERDR_SOCKET_PATH"],
                    "pane.get",
                    {"pane_id": os.environ["HERDR_PANE_ID"]},
                )
                self.terminal_id = result["pane"]["terminal_id"]
            except (KeyError, RuntimeError):
                pass
        self.open_view(self.initial_view)

    def on_resize(self, event) -> None:
        if self.screen_stack:
            self.screen_stack[0].set_class(event.size.height < 36, "compact")

    def action_history(self) -> None:
        checkbox = self.query_one("#history-toggle", Checkbox)
        checkbox.value = not checkbox.value

    def open_view(self, view: str) -> None:
        if view in {"today", "week", "reports"}:
            self.query_one("#tabs", TabbedContent).active = view
        elif view == "settings":
            self.action_settings()
        elif view == "add-task":
            self.action_new_task()
        elif view == "add-mission":
            self.action_new_mission()

    def selected_project(self) -> str:
        value = self.query_one("#project", Select).value
        return "" if value is Select.NULL else str(value)

    async def refresh_board(self) -> None:
        if len(self.screen_stack) > 1 or self.rendering:
            return
        self.rendering = True
        try:
            if self.terminal_id:
                requested = self.store.get_meta("view:" + self.terminal_id)
                if requested:
                    self.store.set_meta("view:" + self.terminal_id, "")
                    self.open_view(requested)
                    if len(self.screen_stack) > 1:
                        return
            self.config = Config.load(self.paths)
            projects = self.store.projects()
            names = project_names(projects)
            options = [(names[p["id"]], p["id"]) for p in projects]
            select = self.query_one("#project", Select)
            if options != self.project_options:
                value = select.value
                select.set_options([("All spaces", "")] + options)
                if self.preferred_project in names:
                    select.value = self.preferred_project
                    self.preferred_project = ""
                else:
                    select.value = value if value in names else ""
                self.project_options = options
            project = self.selected_project()
            all_dates = self.query_one("#history-toggle", Checkbox).value
            tasks = self.store.tasks(project, include_archived=all_dates)
            now = utcnow()
            day, week = period_keys(now, self.config)
            if not all_dates:
                tasks = [
                    t
                    for t in tasks
                    if t["target"] <= (day if t["kind"] == "daily" else week)
                    and (
                        t["status"] != "completed"
                        or (
                            t["completed_at"]
                            and parse(t["completed_at"])
                            >= latest(now, self.config, t["kind"] == "weekly")
                        )
                    )
                ]
            signature = json.dumps([tasks, names, day, week], sort_keys=True)
            if signature != self.board_signature:
                for kind in ("daily", "weekly"):
                    container = self.query_one(f"#{kind}-list", VerticalScroll)
                    await container.remove_children()
                    entries = [t for t in tasks if t["kind"] == kind]
                    if entries:
                        await container.mount(
                            *(
                                TaskRow(
                                    task,
                                    names.get(task["project_id"], "Project"),
                                    expanded=task["id"] in self.expanded_tasks,
                                )
                                for task in entries
                            )
                        )
                    else:
                        await container.mount(
                            Static(
                                "No tasks here yet. Add one above, or choose a suggestion in Reports.",
                                classes="empty",
                            )
                        )
                self.board_signature = signature
            counts = {
                key: sum(t["status"] == key and not t["archived"] for t in tasks)
                for key in STATUS_LABELS
            }
            agents = json.loads(self.store.get_meta("agents", "[]"))
            statuses = [
                a["agent"]["agent_status"]
                for a in agents
                if not project or a["project_id"] == project
            ]
            overview = f"{counts['todo']} open · {counts['in_progress']} active · {counts['blocked']} blocked · {counts['completed']} done"
            if statuses:
                overview += f"\nAgents: {statuses.count('working')} working · {statuses.count('blocked')} blocked · {len(statuses)} total"
            if project:
                state = next((p["status"] for p in projects if p["id"] == project), "unknown")
                overview += "\nSpace: " + state.replace("_", " ")
            self.query_one("#overview", Static).update(overview)
            next_run = (
                next_boundary(now, self.config)
                .astimezone(ZoneInfo(self.config.timezone))
                .strftime("%a %H:%M")
            )
            next_weekly = (
                next_boundary(now, self.config, True)
                .astimezone(ZoneInfo(self.config.timezone))
                .strftime("%a %H:%M")
            )
            heartbeat = self.store.get_meta("coordinator_heartbeat")
            state = self.store.get_meta("coordinator_status", "Not started")
            if heartbeat and now - parse(heartbeat) > timedelta(minutes=2):
                state = "Coordinator offline — reopen or restart scheduling"
            status = (
                f"{state}\nDaily: {next_run}\nWeekly: {next_weekly}"
                if self.config.enabled
                else "Automatic reports paused"
            )
            error = self.store.get_meta("coordinator_error")
            if error:
                status += "\n" + error
            self.query_one("#status", Static).update(status)
            runs = self.store.runs()
            report_options = [
                (f"{r['created_at'][:16].replace('T', ' ')} · {r['status']}", r["id"]) for r in runs
            ]
            if report_options != self.report_options:
                report_select = self.query_one("#report-select", Select)
                selected = report_select.value
                report_select.set_options(report_options)
                report_select.value = (
                    selected
                    if selected in {r["id"] for r in runs}
                    else (runs[0]["id"] if runs else Select.NULL)
                )
                self.report_options = report_options
            await self.refresh_report()
        except (ValueError, OSError) as exc:
            self.query_one("#status", Static).update(str(exc))
        finally:
            self.rendering = False

    async def refresh_report(self) -> None:
        selected = self.query_one("#report-select", Select).value
        if selected is Select.NULL:
            return
        run = self.store.run(str(selected))
        project = self.selected_project()
        suggestions = self.store.suggestions(run["id"], project)
        signature = json.dumps([run["updated_at"], project, run["status"], suggestions])
        if signature == self.report_signature:
            return
        text = f"# {'Daily + weekly' if run['spec']['weekly'] else 'Daily'} report\n\nStatus: **{run['status']}**\n\n"
        text += f"Period: {run['spec']['start']} → {run['spec']['end']}\n\n"
        if run["spec"]["weekly"]:
            text += (
                f"Weekly period: {run['spec']['weekly_start']} → {run['spec']['weekly_end']}\n\n"
            )
        if run["error"]:
            text += "## Run issue\n\n" + run["error"] + "\n\n"
        if run["summary"]:
            text += run["summary"]["coverage"] + "\n\n"
            names = {p["id"]: p["name"] for p in self.store.projects(include_hidden=True)}
            included = {r["project_id"] for r in self.store.reports(project)} if project else set()
            for report in run["summary"]["projects"]:
                if (
                    not project
                    or project == report["project_id"]
                    or report["project_id"] in included
                ):
                    text += (
                        report_markdown(names.get(report["project_id"], "Project"), report) + "\n\n"
                    )
        for gap in (run["evidence"] or {}).get("gaps", []):
            text += f"\n- Coverage: {gap}\n"
        await self.query_one("#report-body", Markdown).update(text)
        container = self.query_one("#suggestions", Vertical)
        await container.remove_children()
        if run["plan"] and run["status"] in {"complete", "partial"}:
            names = {p["id"]: p["name"] for p in self.store.projects(include_hidden=True)}
            await container.mount(
                Static(
                    "Suggested next steps — choose what to add."
                    if suggestions
                    else "No additional tasks suggested.",
                    classes="empty",
                )
            )
            if suggestions:
                await container.mount(
                    *(
                        SuggestionRow(item, names.get(item["project_id"], "Space"))
                        for item in suggestions
                    )
                )
        self.report_signature = signature

    @on(Select.Changed, "#project")
    @on(Checkbox.Changed, "#history-toggle")
    async def filters_changed(self) -> None:
        await self.refresh_board()

    @on(Select.Changed, "#report-select")
    async def report_changed(self) -> None:
        await self.refresh_report()

    @on(Checkbox.Changed)
    async def checked(self, event: Checkbox.Changed) -> None:
        if (
            not event.checkbox.id
            or not event.checkbox.id.startswith("check-")
            or self.rendering
            or not event.checkbox.is_mounted
        ):
            return
        task = event.checkbox.parent.parent.task_data
        desired = "completed" if event.value else "todo"
        if event.value == (task["status"] == "completed"):
            return
        try:
            self.store.edit_task(task["id"], task["revision"], status=desired)
            await self.refresh_board()
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            self.board_signature = ""
            await self.refresh_board()

    @on(Button.Pressed)
    async def buttons(self, event: Button.Pressed) -> None:
        button = event.button.id or ""
        actions = {
            "new-task": self.action_new_task,
            "new-mission": self.action_new_mission,
            "settings": self.action_settings,
            "run-daily": self.action_daily,
            "run-weekly": lambda: self.start_report(True),
            "retry-report": self.retry_report,
        }
        if button in actions:
            actions[button]()
        elif button == "refresh-spaces":
            self.refresh_spaces()
        elif button.startswith("add-suggestion-"):
            run_id, index = button.removeprefix("add-suggestion-").rsplit("-", 1)
            try:
                self.store.accept_suggestion(run_id, int(index))
                self.notify("Task is on the board")
                await self.refresh_board()
            except ValueError as exc:
                self.notify(str(exc), severity="error")
        elif button.startswith("description-toggle-"):
            task_id = button.removeprefix("description-toggle-")
            row = self.query_one("#row-" + task_id, TaskRow)
            if row.toggle_description():
                self.expanded_tasks.add(task_id)
            else:
                self.expanded_tasks.discard(task_id)
        elif button.startswith("edit-"):
            task = self.store.task(button[5:])
            self.push_screen(
                TaskEditor(self.store, self.config, task["kind"], task=task), self.editor_closed
            )

    async def editor_closed(self, saved: bool) -> None:
        if saved:
            self.board_signature = ""
            await self.refresh_board()

    def action_new_task(self) -> None:
        self.push_screen(
            TaskEditor(self.store, self.config, "daily", self.selected_project()),
            self.editor_closed,
        )

    def action_new_mission(self) -> None:
        self.push_screen(
            TaskEditor(self.store, self.config, "weekly", self.selected_project()),
            self.editor_closed,
        )

    def action_settings(self) -> None:
        self.push_screen(SettingsEditor(self.paths, self.config), self.editor_closed)

    def action_daily(self) -> None:
        self.start_report(False)

    @work(thread=True, exclusive=True, group="spaces")
    def refresh_spaces(self) -> None:
        try:
            _, gaps = Collector(Herdr(), self.store, self.config).scan()
            self.call_from_thread(self.notify, "Spaces refreshed" if not gaps else "; ".join(gaps))
            self.call_from_thread(self.refresh_board)
        except Exception as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")

    def retry_report(self) -> None:
        value = self.query_one("#report-select", Select).value
        if value is not Select.NULL:
            self.start_report(retry=str(value))

    @work(thread=True, group="reports")
    def start_report(self, weekly: bool = False, retry: str | None = None) -> None:
        try:
            args = ["retry", retry] if retry else (["run", "--weekly"] if weekly else ["run"])
            launch_background(self.paths, args)
            self.call_from_thread(self.notify, "Report requested — see Reports for progress")
        except Exception as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
