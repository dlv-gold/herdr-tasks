"""Plugin actions and a small local CLI for setup and diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from .collector import Collector
from .config import Config, Paths
from .daemon import ensure_running, run_daemon
from .herdr import Herdr, HerdrError, toggle_panel
from .pipeline import Pipeline
from .process import ProcessError
from .store import Store


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Daily tasks, weekly missions, and project reports inside Herdr"
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create default plugin settings and storage")
    commands.add_parser("status", help="Show coordinator and recent report status")
    commands.add_parser("config", help="Print current settings")
    commands.add_parser("ensure-running", help="Start the singleton coordinator if needed")
    commands.add_parser("daemon", help="Run the coordinator in the foreground")
    commands.add_parser("collect", help="Discover projects and record current agent activity")
    commands.add_parser("toggle", help="Toggle the right task panel in the invoking tab")
    open_command = commands.add_parser("open", help="Open or focus the task panel")
    open_command.add_argument(
        "--view",
        choices=["today", "week", "reports", "settings", "add-task", "add-mission"],
        default="today",
    )
    ui = commands.add_parser("ui", help="Run the terminal task board")
    ui.add_argument(
        "--no-coordinator", action="store_true", help="UI only, useful for offline task editing"
    )
    run = commands.add_parser("run", help="Collect updates and generate a report now")
    run.add_argument(
        "--weekly", action="store_true", help="Also summarize the week and create weekly missions"
    )
    retry = commands.add_parser("retry", help="Resume a failed run, or re-export a completed one")
    retry.add_argument("run_id")
    export = commands.add_parser("export", help="Re-export a report as Markdown")
    export.add_argument("run_id")
    return root


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    paths = Paths.discover()
    try:
        paths.create()
        config, store, api = Config.load(paths), Store(paths.database), Herdr()
        match args.command:
            case "init":
                config.save(paths)
                print(f"Settings: {paths.config / 'config.json'}\nState: {paths.state}")
            case "config":
                print(json.dumps(asdict(config), indent=2))
            case "status":
                print(
                    json.dumps(
                        {
                            "coordinator": store.get_meta("coordinator_status", "not started"),
                            "heartbeat": store.get_meta("coordinator_heartbeat"),
                            "error": store.get_meta("coordinator_error"),
                            "projects": len(store.projects()),
                            "tasks": len(store.tasks()),
                            "runs": [
                                {k: r[k] for k in ("id", "created_at", "status", "error")}
                                for r in store.runs(5)
                            ],
                        },
                        indent=2,
                    )
                )
            case "ensure-running":
                ensure_running(paths)
            case "daemon":
                run_daemon(paths)
            case "collect":
                agents, gaps = Collector(api, store, config).scan()
                print(
                    json.dumps(
                        {"agents": len(agents), "projects": len(store.projects()), "gaps": gaps}
                    )
                )
            case "toggle" | "open":
                if os.environ.get("HERDR_ENV") != "1":
                    raise ValueError("Open the task panel from a Herdr pane or plugin action")
                context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
                pane = os.environ.get("HERDR_PANE_ID") or context.get("focused_pane_id")
                endpoint = os.environ.get("HERDR_SOCKET_PATH")
                if not endpoint or not pane:
                    raise ValueError("Herdr did not provide an invoking pane and socket")
                ensure_running(paths)
                result = toggle_panel(
                    api,
                    store,
                    config,
                    endpoint,
                    pane,
                    view=getattr(args, "view", "today"),
                    open_only=args.command == "open",
                )
                print(json.dumps(result))
            case "ui":
                from .ui import TaskApp

                TaskApp(
                    paths,
                    start_coordinator=not args.no_coordinator,
                    view=os.environ.get("HERDR_TASKS_VIEW", "today"),
                ).run()
            case "run" | "retry":
                pipeline = Pipeline(paths, config, store, api)
                result = pipeline.run(
                    weekly=getattr(args, "weekly", False), retry=getattr(args, "run_id", None)
                )
                print(json.dumps({k: result[k] for k in ("id", "status", "error")}))
                return 1 if result["status"] == "failed" else 0
            case "export":
                Pipeline(paths, config, store, api).export(args.run_id)
                print(f"Report exported under {paths.state / 'reports'}")
        return 0
    except (ValueError, OSError, HerdrError, ProcessError) as exc:
        print(f"Herdr Tasks: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
