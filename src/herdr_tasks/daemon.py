"""One coordinator across local Herdr servers, with event-driven sampling."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid

from .collector import Collector
from .config import Config, Paths
from .herdr import Herdr
from .pipeline import Pipeline, scheduled_spec
from .process import lock
from .schedule import stamp, utcnow
from .store import Store


def ensure_running(paths: Paths) -> bool:
    paths.create()
    with lock(paths.state / "coordinator.lock") as acquired:
        if not acquired:
            return False
    # Competing launchers are harmless: the child holds the real process lock.
    launch_background(paths, ["daemon"], "coordinator")
    return True


def launch_background(paths: Paths, args: list[str], log_name: str = "reports") -> None:
    paths.create()
    env = os.environ.copy()
    env.update(HERDR_PLUGIN_CONFIG_DIR=str(paths.config), HERDR_PLUGIN_STATE_DIR=str(paths.state))
    log = paths.state / f"{log_name}.log"
    if log.exists() and log.stat().st_size > 1_000_000:
        log.replace(paths.state / f"{log_name}.previous.log")
    with log.open("ab") as stream:
        subprocess.Popen(
            [sys.executable, "-m", "herdr_tasks", *args],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=env,
            start_new_session=True,
            close_fds=True,
        )


async def watch(endpoint: str, pane_ids: tuple[str, ...], wake: asyncio.Event) -> None:
    subscriptions = [
        {"type": kind}
        for kind in (
            "workspace.created",
            "workspace.closed",
            "workspace.renamed",
            "pane.created",
            "pane.closed",
            "pane.agent_detected",
        )
    ]
    subscriptions.extend(
        {"type": "pane.agent_status_changed", "pane_id": pane} for pane in pane_ids
    )
    while True:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(endpoint, limit=1_000_000), timeout=5
            )
            writer.write(
                (
                    json.dumps(
                        {
                            "id": uuid.uuid4().hex,
                            "method": "events.subscribe",
                            "params": {"subscriptions": subscriptions},
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            while line := await reader.readline():
                message = json.loads(line)
                if "error" in message:
                    break
                wake.set()  # Also reconcile after subscription acknowledgment/reconnection.
        except (TimeoutError, OSError, ValueError):
            pass
        finally:
            if writer:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        await asyncio.sleep(5)


async def coordinate(paths: Paths, api: Herdr | None = None) -> None:
    api = api or Herdr()
    store = Store(paths.database)
    wake, stop = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    watchers: dict[str, tuple[tuple[str, ...], asyncio.Task]] = {}
    job: asyncio.Task | None = None
    last_scan = last_discovery = last_prune = 0.0
    sessions: list[dict] = []
    empty_checks = 0
    store.set_meta("coordinator_pid", str(os.getpid()))
    store.set_meta("coordinator_status", "starting")
    try:
        while not stop.is_set():
            tick = time.monotonic()
            try:
                errors = []
                config = Config.load(paths)
                if tick - last_discovery >= 10:
                    sessions = await asyncio.to_thread(api.sessions)
                    last_discovery = tick
                    empty_checks = empty_checks + 1 if not sessions else 0
                if empty_checks >= 2:
                    break
                if job and job.done():
                    try:
                        job.result()
                    except Exception as exc:
                        errors.append(str(exc)[:1000])
                    job = None
                enabled_sessions = []
                for session in sessions:
                    try:
                        if await asyncio.to_thread(api.enabled, session["socket_path"]):
                            enabled_sessions.append(session)
                    except Exception:
                        errors.append(f"Session {session['name']} unavailable")
                if not enabled_sessions:
                    store.set_meta("coordinator_status", "paused: plugin disabled or no server")
                    for _, task in watchers.values():
                        task.cancel()
                    watchers.clear()
                else:
                    if wake.is_set() or tick - last_scan >= config.sample_seconds:
                        wake.clear()
                        collector = Collector(api, store, config)
                        agents, _ = await asyncio.to_thread(collector.scan, enabled_sessions)
                        last_scan = tick
                        expected = {
                            s["socket_path"]: tuple(
                                sorted(
                                    a["agent"]["pane_id"]
                                    for a in agents
                                    if a["endpoint"] == s["socket_path"]
                                )
                            )
                            for s in enabled_sessions
                        }
                        for endpoint in list(watchers):
                            if (
                                endpoint not in expected
                                or expected[endpoint] != watchers[endpoint][0]
                            ):
                                watchers.pop(endpoint)[1].cancel()
                        for endpoint, panes in expected.items():
                            if endpoint not in watchers:
                                watchers[endpoint] = (
                                    panes,
                                    asyncio.create_task(watch(endpoint, panes, wake)),
                                )
                    now = utcnow()
                    due = scheduled_spec(now, config, store)
                    with lock(paths.state / "job.lock") as available:
                        can_start = available
                    if config.enabled and job is None and can_start:
                        pending = next(
                            (
                                r
                                for r in reversed(store.runs())
                                if r["status"] not in {"failed", "complete", "partial"}
                            ),
                            None,
                        )
                        pipeline = Pipeline(paths, config, store, api)
                        if pending:
                            job = asyncio.create_task(
                                asyncio.to_thread(pipeline.run, retry=pending["id"])
                            )
                        elif due:
                            job = asyncio.create_task(
                                asyncio.to_thread(pipeline.run, scheduled=due)
                            )
                    store.set_meta(
                        "coordinator_status",
                        "report running"
                        if job
                        else ("watching" if config.enabled else "scheduling paused"),
                    )
                store.set_meta("coordinator_heartbeat", stamp(utcnow()))
                store.set_meta("coordinator_error", "; ".join(errors)[:1000])
                if tick - last_prune > 3600:
                    store.prune(config.history_days)
                    last_prune = tick
            except Exception as exc:
                store.set_meta("coordinator_error", str(exc)[:1000])
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        for _, task in watchers.values():
            task.cancel()
        await asyncio.gather(*(task for _, task in watchers.values()), return_exceptions=True)
        if job:
            # Complete a bounded, already-started report before shutting down.
            try:
                await job
            except Exception as exc:
                store.set_meta("coordinator_error", str(exc)[:1000])
        store.set_meta("coordinator_status", "stopped")
        store.set_meta("coordinator_pid", "")


def run_daemon(paths: Paths) -> None:
    with lock(paths.state / "coordinator.lock") as acquired:
        if acquired:
            asyncio.run(coordinate(paths))
