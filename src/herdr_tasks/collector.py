"""Collect bounded live evidence without treating reporting turns as work."""

from __future__ import annotations

import hashlib
import json
import re

from .config import Config
from .herdr import Herdr, HerdrError, agent_key, project_root, ready, same_agent
from .schedule import stamp, utcnow
from .store import Store


def clean_text(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32)
    # Reduce accidental persistence of common credentials appearing in a terminal.
    text = re.sub(
        r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b", "[redacted credential]", text
    )
    text = re.sub(
        r"(?im)\b((?:api[_-]?key|access[_-]?token|password|authorization)\s*[:=]\s*)[^\s,]+",
        r"\1[redacted]",
        text,
    )
    return text[-12000:]


def report_pattern(marker: str) -> str:
    # Codex and Claude may prefix an assistant's first line with a terminal bullet.
    prefix = r"^[ \t]*(?:[•●>*][ \t]*)?"
    return rf"(?m){prefix}{marker}_BEGIN[ \t]*\n(.*?){prefix}{marker}_END[ \t]*$"


def passive_text(text: str) -> str:
    text = re.sub(report_pattern(r"HERDR_TASKS_[a-f0-9]{32}"), "", text, flags=re.DOTALL)
    return "\n".join(
        line for line in text.splitlines() if "Scheduled progress check (HERDR_TASKS_" not in line
    ).strip()


class Collector:
    def __init__(self, api: Herdr, store: Store, config: Config):
        self.api, self.store, self.config = api, store, config
        self.roots: dict[str, tuple[str, str]] = {}

    def scan(self, sessions: list[dict] | None = None) -> tuple[list[dict], list[str]]:
        sessions = sessions if sessions is not None else self.api.sessions()
        agents, gaps = [], []
        now = stamp(utcnow())
        snapshots, spaces = [], []
        panels = self.store.panel_records()
        for session in sessions:
            endpoint = session["socket_path"]
            try:
                snapshot = self.api.snapshot(endpoint)
            except HerdrError:
                gaps.append(f"Session {session['name']} unavailable")
                continue
            snapshots.append((session, snapshot))
            panel_terminals = {
                p.get("terminal_id") for p in panels if p.get("endpoint") == endpoint
            }
            for workspace in snapshot["workspaces"]:
                roots = []
                cwd = workspace.get("cwd") or (workspace.get("worktree") or {}).get("checkout_path")
                if cwd:
                    roots.append(self._root(cwd))
                for pane in [*snapshot.get("panes", []), *snapshot["agents"]]:
                    if pane.get("terminal_id") in panel_terminals:
                        continue
                    if pane["workspace_id"] != workspace["workspace_id"]:
                        continue
                    cwd = pane.get("foreground_cwd") or pane.get("cwd")
                    if cwd:
                        roots.append(self._root(cwd))
                spaces.append(
                    {
                        "endpoint": endpoint,
                        "session": session["name"],
                        "workspace_id": workspace["workspace_id"],
                        "name": workspace.get("label") or workspace["workspace_id"],
                        "roots": list(dict.fromkeys(roots)),
                    }
                )
        self.store.sync_spaces(spaces, now)
        for session, snapshot in snapshots:
            endpoint = session["socket_path"]
            workspaces = {w["workspace_id"]: w for w in snapshot["workspaces"]}
            for agent in snapshot["agents"]:
                if len(agents) >= self.config.max_agents:
                    gaps.append("Agent collection limit reached")
                    break
                workspace = workspaces.get(agent["workspace_id"])
                if workspace is None:
                    gaps.append(f"Agent {agent['pane_id']} has no available workspace")
                    continue
                cwd = agent.get("foreground_cwd") or agent.get("cwd") or workspace.get("cwd")
                project = self.store.space_id(endpoint, workspace["workspace_id"])
                key = agent_key(endpoint, agent)
                record = {
                    "endpoint": endpoint,
                    "session": session["name"],
                    "workspace_id": workspace["workspace_id"],
                    "cwd": cwd,
                    "key": key,
                    "project_id": project,
                    "agent": agent,
                }
                agents.append(record)
                data = {
                    "pane_id": agent["pane_id"],
                    "session": session["name"],
                    "agent": agent.get("agent"),
                    "name": agent.get("name"),
                    "status": agent["agent_status"],
                }
                if self.store.update_pending(key):
                    continue
                try:
                    text = clean_text(self.api.read(endpoint, agent["pane_id"]))
                    signature = hashlib.sha256(text.encode()).hexdigest()
                    if signature == self.store.get_meta("automation_screen:" + key):
                        continue
                    data["text"] = passive_text(text)
                except HerdrError:
                    data["coverage"] = "Terminal output unavailable"
                self.store.activity(project, key, data, now)
        self.store.set_meta("agents", json.dumps(agents))
        self.store.set_meta("scan_gaps", json.dumps(gaps))
        self.store.set_meta("last_scan", now)
        return agents, gaps

    def _root(self, cwd: str) -> str:
        if cwd not in self.roots:
            self.roots[cwd] = project_root(cwd)
        return self.roots[cwd][0]

    def request_updates(self, run_id: str, agents: list[dict]) -> list[dict]:
        for record in agents:
            key, original, endpoint = record["key"], record["agent"], record["endpoint"]
            if not self.store.claim_update(run_id, key):
                continue
            base = {
                "project_id": record["project_id"],
                "name": original.get("name") or original.get("agent"),
                "original_status": original["agent_status"],
                "pane_id": original["pane_id"],
            }
            if not ready(original):
                self.store.save_update(
                    run_id,
                    key,
                    "unavailable",
                    base | {"reason": "Agent is busy, blocked, or not ready"},
                )
                continue
            try:
                current = self.api.agent(endpoint, original["pane_id"])
                if not same_agent(original, current) or not ready(current):
                    self.store.save_update(
                        run_id,
                        key,
                        "unavailable",
                        base | {"reason": "Agent changed or became busy"},
                    )
                    continue
                marker = f"HERDR_TASKS_{run_id}"
                prompt = (
                    f"Scheduled progress check ({marker}). Give a short factual report of this project's recent work: "
                    "accomplishments, unfinished tasks, blockers, and suggested next steps. Distinguish tested results "
                    "from assumptions. Use only context you already have; do not run commands, change files, launch "
                    "agents, or start new work. This is a reporting request only. Limit the response to 250 words. "
                    f"Start your response on its own line with {marker}_BEGIN and end on its own line with {marker}_END."
                )
                self.store.save_update(run_id, key, "waiting", base)
                self.api.call(
                    endpoint,
                    "agent.prompt",
                    {
                        "target": original["pane_id"],
                        "text": prompt,
                        "wait": {"timeout_ms": self.config.agent_timeout * 1000},
                    },
                    timeout=self.config.agent_timeout + 10,
                )
                after = self.api.agent(endpoint, original["pane_id"])
                if not same_agent(original, after):
                    raise HerdrError("agent_changed", "Original agent is no longer present")
                text = clean_text(self.api.read(endpoint, original["pane_id"], lines=200))
                self.store.set_meta(
                    "automation_screen:" + key, hashlib.sha256(text.encode()).hexdigest()
                )
                pattern = report_pattern(marker)
                matches = re.findall(pattern, text, re.DOTALL)
                if matches:
                    self.store.save_update(
                        run_id, key, "received", base | {"report": matches[-1].strip()[:12000]}
                    )
                else:
                    self.store.save_update(
                        run_id,
                        key,
                        "missing",
                        base
                        | {
                            "reason": "A complete marked response was not available in terminal history"
                        },
                    )
            except HerdrError as exc:
                # The prompt may have reached the agent. Never submit it a second time.
                self.store.save_update(run_id, key, "ambiguous", base | {"reason": str(exc)[:300]})
        return self.store.updates(run_id)
