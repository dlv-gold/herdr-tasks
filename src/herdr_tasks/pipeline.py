"""Recoverable collection → summary → planning → saved suggestions for user review."""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .collector import Collector
from .config import Config, Paths, atomic_write
from .herdr import Herdr, git_evidence
from .process import lock
from .providers import Provider
from .schedule import boundary, latest, parse, period_keys, stamp, utcnow
from .store import Store


def make_spec(
    now: datetime,
    config: Config,
    store: Store,
    *,
    weekly: bool = False,
    scheduled: bool = False,
    daily_due: bool = True,
) -> dict:
    end = latest(now, config) if scheduled else now
    weekly_end = latest(now, config, True)
    local_day = end.astimezone(ZoneInfo(config.timezone)).date()
    start = boundary(local_day - timedelta(days=1), config.daily_time, config.timezone)
    if not scheduled:
        start = latest(now, config)
    previous = store.get_meta("last_daily_success") or store.get_meta("armed_at")
    if scheduled and previous and parse(previous) < start:
        start = parse(previous)
    week_start = boundary(
        weekly_end.astimezone(ZoneInfo(config.timezone)).date() - timedelta(days=7),
        config.weekly_time,
        config.timezone,
    )
    if not scheduled:
        week_start, weekly_end = weekly_end, now
    previous_week = store.get_meta("last_weekly_success") or store.get_meta("armed_at")
    if scheduled and previous_week and parse(previous_week) < week_start:
        week_start = parse(previous_week)
    target_day, target_week = period_keys(now, config)
    return {
        "start": stamp(start),
        "end": stamp(end),
        "weekly_start": stamp(week_start),
        "weekly_end": stamp(weekly_end),
        "weekly": weekly,
        "daily_due": daily_due,
        "target_day": target_day,
        "target_week": target_week,
        "scheduled": scheduled,
        "timezone": config.timezone,
        "cutoff_time": config.daily_time,
        "coverage_note": "Only recorded activity is available. Live updates are collected after the cutoff and may include later work.",
    }


def scheduled_spec(now: datetime, config: Config, store: Store) -> tuple[str, dict] | None:
    armed = store.get_meta("armed_at")
    if not armed:
        store.set_meta("armed_at", stamp(now))
        return None
    if not config.enabled:
        return None
    daily_end, weekly_end = latest(now, config), latest(now, config, True)
    daily = daily_end > parse(store.get_meta("last_daily_attempt", armed))
    weekly = weekly_end > parse(store.get_meta("last_weekly_attempt", armed))
    if not daily and not weekly:
        return None
    spec = make_spec(now, config, store, weekly=weekly, scheduled=True, daily_due=daily)
    key = f"scheduled:{stamp(daily_end)}:{stamp(weekly_end) if weekly else '-'}"
    return key, spec


class Pipeline:
    def __init__(
        self,
        paths: Paths,
        config: Config,
        store: Store,
        api: Herdr,
        provider: Provider | None = None,
        collector: Collector | None = None,
    ):
        self.paths, self.config, self.store, self.api = paths, config, store, api
        self.provider = provider or Provider()
        self.collector = collector or Collector(api, store, config)

    def run(
        self,
        *,
        weekly: bool = False,
        scheduled: tuple[str, dict] | None = None,
        retry: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        with lock(self.paths.state / "job.lock") as acquired:
            if not acquired:
                raise ValueError("A report is already running; its progress is shown in Reports")
            now = now or utcnow()
            if retry:
                run = self.store.run(retry)
                if run["status"] == "failed" and run["plan"] is not None:
                    # An explicit retry may regenerate a plan rejected during application.
                    # Interrupted (nonfailed) runs still resume their persisted plan.
                    self.store.stage(run["id"], "planning", field="plan", value=None)
                    run["plan"] = None
            else:
                key, spec = scheduled or (
                    "manual:" + uuid.uuid4().hex,
                    make_spec(now, self.config, self.store, weekly=weekly),
                )
                run = self.store.start_run(key, spec)
            if run["status"] in {"complete", "partial"}:
                self.export(run["id"])
                return run
            spec, run_id = run["spec"], run["id"]
            if spec["scheduled"]:
                if spec["daily_due"]:
                    self.store.set_meta("last_daily_attempt", spec["end"])
                if spec["weekly"]:
                    self.store.set_meta("last_weekly_attempt", spec["weekly_end"])
            try:
                evidence = run["evidence"]
                if not evidence:
                    self.store.stage(run_id, "collecting")
                    agents, gaps = self.collector.scan()
                    updates = self.collector.request_updates(run_id, agents)
                    evidence = self.build_evidence(spec, updates, gaps)
                    self.store.stage(run_id, "summarizing", field="evidence", value=evidence)
                summary = run["summary"]
                if summary is None:
                    summary = (
                        self.provider.generate(
                            self.config.summary, "summary", evidence, self.config.provider_timeout
                        )
                        if evidence["projects"]
                        else {"coverage": "No discovered projects", "projects": []}
                    )
                    self.validate_projects(summary, evidence)
                    self.store.save_summary(run_id, summary)
                    self.export(run_id)
                plan = run["plan"]
                if plan is None:
                    planner_input = {
                        "spec": spec,
                        "summary": summary,
                        "projects": [
                            {"project_id": p["project_id"], "tasks": p["tasks"]}
                            for p in evidence["projects"]
                        ],
                    }
                    plan = (
                        self.provider.generate(
                            self.config.planner,
                            "planner",
                            planner_input,
                            self.config.provider_timeout,
                        )
                        if evidence["projects"]
                        else {"tasks": []}
                    )
                    self.store.stage(run_id, "applying", field="plan", value=plan)
                included = {p["project_id"] for p in evidence["projects"]}
                if any(t["project_id"] not in included for t in plan["tasks"]):
                    raise ValueError("Planner referenced a project outside this report")
                self.store.finish(run_id, summary, plan, partial=bool(evidence["gaps"]))
                if spec["scheduled"]:
                    if spec["daily_due"]:
                        self.store.set_meta("last_daily_success", spec["end"])
                    if spec["weekly"]:
                        self.store.set_meta("last_weekly_success", spec["weekly_end"])
                self.export(run_id)
            except Exception as exc:
                # Keep completed DB results if export alone failed; they can be re-exported.
                saved = self.store.run(run_id)
                if saved["status"] in {"complete", "partial"}:
                    self.store.stage(
                        run_id, saved["status"], error=f"Report export failed: {type(exc).__name__}"
                    )
                else:
                    self.store.stage(run_id, "failed", error=str(exc)[:1000])
                    with contextlib.suppress(OSError):
                        self.export(run_id)
            return self.store.run(run_id)

    def build_evidence(self, spec: dict, updates: list[dict], gaps: list[str]) -> dict:
        projects = self.store.projects()
        evidence = {"spec": spec, "projects": [], "gaps": list(gaps)}
        if self.store.get_meta("grouping") == "spaces":
            unassigned = [p for p in projects if not p["workspace_id"]]
            if unassigned:
                evidence["gaps"].append(
                    "Some previous folder tasks need manual assignment to a Herdr space"
                )
            projects = [p for p in projects if p["workspace_id"]]
        for update in updates:
            if not update["data"].get("project_id"):
                evidence["gaps"].append(
                    "An agent update was interrupted before its context was saved"
                )
        if len(projects) > self.config.max_projects:
            evidence["gaps"].append(
                f"Only the first {self.config.max_projects} of {len(projects)} projects fit the configured limit"
            )
        for project in projects[: self.config.max_projects]:
            project_id = project["id"]
            task_items = self.store.tasks(project_id, include_archived=True)
            if len(task_items) > 150:
                evidence["gaps"].append(f"Task context capped for {project['name']}")
            tasks = [
                {
                    k: t[k]
                    for k in (
                        "id",
                        "title",
                        "notes",
                        "kind",
                        "status",
                        "target",
                        "parent_id",
                        "archived",
                    )
                }
                for t in task_items[:150]
            ]
            for task in tasks:
                task["notes"] = task["notes"][:800]
            history = self.store.history(project_id, spec["start"], spec["end"], limit=8)
            for event in history:
                if "text" in event["data"]:
                    event["data"]["text"] = event["data"]["text"][-2500:]
            replies = []
            for update in updates:
                if update["data"].get("project_id") != project_id:
                    continue
                replies.append({"status": update["status"], **update["data"]})
                if update["status"] != "received":
                    evidence["gaps"].append(
                        f"{project['name']}: {update['data'].get('name', 'agent')} — {update['data'].get('reason', 'Update uncertain after restart')}"
                    )
            if not history and not replies:
                evidence["gaps"].append(
                    f"{project['name']}: no recorded agent activity in the report period"
                )
            weekly_history = []
            weekly_activity = []
            if spec["weekly"]:
                weekly_history = [
                    r["data"]
                    for r in self.store.reports(project_id, limit=30)
                    if spec["weekly_start"] <= r["spec"]["end"] <= spec["weekly_end"]
                ]
                weekly_activity = self.store.history(
                    project_id, spec["weekly_start"], spec["weekly_end"], limit=16
                )
                for event in weekly_activity:
                    if "text" in event["data"]:
                        event["data"]["text"] = event["data"]["text"][-2500:]
            evidence["projects"].append(
                {
                    "project_id": project_id,
                    "name": project["name"],
                    "root": project["root"],
                    "space": {"id": project["workspace_id"], "session": project["session_name"]},
                    "tasks": tasks,
                    "activity": history,
                    "agent_updates": replies,
                    "git": git_evidence(project["root"], spec["start"]),
                    "weekly_history": weekly_history,
                    "weekly_activity": weekly_activity,
                    "weekly_git": git_evidence(project["root"], spec["weekly_start"])
                    if spec["weekly"]
                    else None,
                    "other_directories": [
                        {
                            "root": root,
                            "git": git_evidence(
                                root, spec["weekly_start"] if spec["weekly"] else spec["start"]
                            ),
                        }
                        for root in project["roots"][1:4]
                    ],
                }
            )
            if len(project["roots"]) > 4:
                evidence["gaps"].append(
                    f"{project['name']}: Git context limited to four directories"
                )
        # Drop oldest bulky evidence first; never truncate JSON or silently lose project identities.
        omitted = False
        while len(json.dumps(evidence).encode()) > self.config.evidence_bytes:
            candidates = [
                (p, key)
                for p in evidence["projects"]
                for key in ("activity", "weekly_history", "weekly_activity")
                if p[key]
            ]
            if not candidates:
                raise ValueError(
                    "Task/update context exceeds the evidence limit; increase evidence_bytes or reduce max_projects"
                )
            project, key = max(candidates, key=lambda item: len(json.dumps(item[0][item[1]])))
            project[key].pop()
            omitted = True
        if omitted:
            evidence["gaps"].append(
                "Older activity/report excerpts omitted to fit the evidence limit"
            )
        return evidence

    @staticmethod
    def validate_projects(summary: dict, evidence: dict) -> None:
        expected = {p["project_id"] for p in evidence["projects"]}
        actual = [p["project_id"] for p in summary["projects"]]
        if set(actual) != expected or len(actual) != len(expected):
            raise ValueError("Summary must contain exactly one report for every supplied project")
        for report in summary["projects"]:
            if evidence["spec"]["weekly"] and not report["weekly_summary"]:
                raise ValueError("A weekly run must include a weekly summary for every project")
            if not evidence["spec"]["weekly"] and report["weekly_summary"] is not None:
                raise ValueError("A daily-only run must not include a weekly summary")

    def export(self, run_id: str) -> None:
        run = self.store.run(run_id)
        root = self.paths.state / "reports" / run["created_at"][:10] / run_id
        summary = run["summary"] or {"coverage": "No summary available", "projects": []}
        index = f"# Project report\n\nStatus: {run['status']}\n\nDaily period: {run['spec']['start']} → {run['spec']['end']}\n\n"
        if run["spec"]["weekly"]:
            index += (
                f"Weekly period: {run['spec']['weekly_start']} → {run['spec']['weekly_end']}\n\n"
            )
        index += summary["coverage"] + "\n\n"
        if run["error"]:
            index += f"Run issue: {run['error']}\n\n"
        names = {p["id"]: p["name"] for p in self.store.projects(include_hidden=True)}
        for report in summary["projects"]:
            project_id = report["project_id"]
            name = names.get(project_id, project_id)
            text = report_markdown(name, report)
            if run["plan"] and run["status"] in {"complete", "partial"}:
                text += "\n## Suggested next steps\n\nAdd pending suggestions from Reports in the task panel.\n\n"
                items = [
                    t
                    for t in self.store.suggestions(run_id)
                    if run["plan"]["tasks"][t["index"]]["project_id"] == project_id
                ]
                for item in items:
                    title = item["title"]
                    if item.get("existing_task_id"):
                        title = self.store.task(item["existing_task_id"])["title"]
                    state = {
                        "pending": "Not added",
                        "added": "Added",
                        "existing": "Already tracked",
                    }[item["state"]]
                    text += f"- {title} ({item['kind']}; {state})\n"
                    if item.get("notes"):
                        text += "\n" + item["notes"] + "\n\n"
                if not items:
                    text += "No additional tasks proposed.\n"
            atomic_write(root / f"{project_id}.md", text)
            index += f"- [{name}]({project_id}.md): {report['status']}\n"
        for gap in (run["evidence"] or {}).get("gaps", []):
            index += f"\n- Coverage: {gap}\n"
        atomic_write(root / "README.md", index)


def report_markdown(name: str, report: dict) -> str:
    text = f"# {name}\n\n**Status:** {report['status'].replace('_', ' ')}\n\n{report['summary']}\n"
    for field, heading in (
        ("accomplishments", "Accomplishments"),
        ("unfinished", "Remaining work"),
        ("blockers", "Blockers"),
    ):
        text += (
            f"\n## {heading}\n\n"
            + ("\n".join(f"- {item}" for item in report[field]) or "None recorded.")
            + "\n"
        )
    if report.get("weekly_summary"):
        text += "\n## Weekly summary\n\n" + report["weekly_summary"] + "\n"
    return text
