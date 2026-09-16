"""Render documentation previews using temporary example data and no agents."""

import asyncio
import tempfile
from pathlib import Path

from herdr_tasks.config import Config, Paths
from herdr_tasks.schedule import period_keys, stamp, utcnow
from herdr_tasks.store import Store
from herdr_tasks.ui import TaskApp


async def main():
    output = Path(__file__).resolve().parents[1] / "docs"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="herdr-tasks-preview-") as directory:
        paths = Paths(Path(directory) / "config", Path(directory) / "state")
        store = Store(paths.database)
        store.sync_spaces(
            [
                {
                    "endpoint": "/preview",
                    "session": "example",
                    "workspace_id": "w1",
                    "name": "Observatory",
                    "roots": ["/example/observatory"],
                }
            ]
        )
        project = store.space_id("/preview", "w1")
        day, week = period_keys(utcnow(), Config())
        for title, status in (
            ("Verify overnight data", "in_progress"),
            ("Review error handling", "todo"),
            ("Add export button", "completed"),
        ):
            task = store.add_task(project, title, "daily", day)
            if status != "todo":
                store.edit_task(task, 1, status=status)
        store.add_task(project, "Ship the project dashboard", "weekly", week)
        store.set_meta("coordinator_status", "watching")
        store.set_meta("coordinator_heartbeat", stamp(utcnow()))
        for name, size in (("board", (48, 48)), ("compact", (32, 24))):
            app = TaskApp(paths, start_coordinator=False, workspace_id="w1", endpoint="/preview")
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                app.save_screenshot(f"{name}.svg", str(output))
                screenshot = output / f"{name}.svg"
                screenshot.write_text(
                    "\n".join(line.rstrip() for line in screenshot.read_text().splitlines()) + "\n"
                )


if __name__ == "__main__":
    asyncio.run(main())
