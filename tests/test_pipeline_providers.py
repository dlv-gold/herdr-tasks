import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from herdr_tasks.config import Config, Paths, Role
from herdr_tasks.pipeline import Pipeline, make_spec, scheduled_spec
from herdr_tasks.process import ProcessError, execute, lock
from herdr_tasks.providers import Provider
from herdr_tasks.store import Store


class FakeCollector:
    def __init__(self):
        self.requests = 0

    def scan(self):
        return [], []

    def request_updates(self, run_id, agents):
        self.requests += 1
        return []


class FakeProvider:
    def __init__(self, fail_planner=False):
        self.stages = []
        self.fail_planner = fail_planner

    def generate(self, role, stage, evidence, timeout):
        self.stages.append((stage, role.provider))
        if stage == "summary":
            return {
                "coverage": "Recorded evidence only",
                "projects": [
                    {
                        "project_id": p["project_id"],
                        "status": "unknown",
                        "summary": "No new recorded activity",
                        "accomplishments": [],
                        "unfinished": ["Verify plugin"],
                        "blockers": [],
                        "weekly_summary": "Weekly progress reviewed"
                        if evidence["spec"]["weekly"]
                        else None,
                    }
                    for p in evidence["projects"]
                ],
            }
        if self.fail_planner:
            raise ProcessError("Fake provider unavailable")
        return {
            "tasks": [
                {
                    "project_id": p["project_id"],
                    "existing_task_id": None,
                    "parent_id": None,
                    "title": "Verify plugin",
                    "notes": "Run the acceptance checks",
                    "kind": "daily",
                }
                for p in evidence["projects"]
            ]
        }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "config", tmp_path / "state")
    paths.create()
    store = Store(paths.database)
    store.project(str(tmp_path / "project"), "Project")
    monkeypatch.setattr(
        "herdr_tasks.pipeline.git_evidence", lambda *_: {"status": "", "commits": ""}
    )
    return paths, store


def test_pipeline_runs_two_roles_and_exports_weekly_summary(setup):
    paths, store = setup
    provider, collector = FakeProvider(), FakeCollector()
    config = Config(planner=Role(provider="claude"))
    pipeline = Pipeline(paths, config, store, None, provider, collector)
    result = pipeline.run(weekly=True, now=datetime(2026, 9, 19, 1, tzinfo=UTC))
    assert result["status"] == "partial"  # Missing activity is explicit.
    assert provider.stages == [("summary", "codex"), ("planner", "claude")]
    assert collector.requests == 1
    assert not store.tasks()
    assert store.suggestions(result["id"])[0]["state"] == "pending"
    store.accept_suggestion(result["id"], 0)
    assert len(store.tasks()) == 1
    assert store.tasks()[0]["target"] == "2026-09-19"
    reports = list((paths.state / "reports").rglob("*.md"))
    assert len(reports) == 2
    assert any("Weekly progress reviewed" in p.read_text() for p in reports)
    pipeline.run(retry=result["id"])
    assert len(store.tasks()) == 1 and len(provider.stages) == 2


def test_failed_planner_preserves_summary_then_retry_only_plans(setup):
    paths, store = setup
    provider, collector = FakeProvider(fail_planner=True), FakeCollector()
    pipeline = Pipeline(paths, Config(), store, None, provider, collector)
    first = pipeline.run()
    assert first["status"] == "failed" and first["summary"]
    assert store.reports()
    assert list((paths.state / "reports").rglob("README.md"))
    assert not store.tasks()
    provider.fail_planner = False
    second = pipeline.run(retry=first["id"])
    assert second["status"] == "partial"
    assert collector.requests == 1
    assert [stage for stage, _ in provider.stages] == ["summary", "planner", "planner"]


def test_schedule_arms_at_install_and_combines_due_cutoffs(setup):
    _, store = setup
    config = Config()
    friday = datetime(2026, 9, 18, 10, tzinfo=UTC)
    assert scheduled_spec(friday, config, store) is None
    saturday = datetime(2026, 9, 19, 0, tzinfo=UTC)
    key, spec = scheduled_spec(saturday, config, store)
    assert spec["weekly"] and spec["daily_due"]
    store.set_meta("last_daily_attempt", spec["end"])
    store.set_meta("last_weekly_attempt", spec["weekly_end"])
    assert scheduled_spec(saturday + timedelta(minutes=5), config, store) is None
    assert key.startswith("scheduled:")


def test_catch_up_covers_gap_and_plans_current_day(setup):
    _, store = setup
    store.set_meta("armed_at", "2026-09-10T00:00:00+00:00")
    store.set_meta("last_daily_success", "2026-09-11T00:00:00+00:00")
    _, spec = scheduled_spec(datetime(2026, 9, 21, 8, tzinfo=UTC), Config(), store)
    assert spec["start"] == "2026-09-11T00:00:00+00:00"
    assert spec["target_day"] == "2026-09-21"
    assert spec["target_week"] == "2026-09-19"


def test_first_missed_run_catches_up_from_installation(setup):
    _, store = setup
    store.set_meta("armed_at", "2026-09-01T10:00:00+00:00")
    _, spec = scheduled_spec(datetime(2026, 9, 21, 8, tzinfo=UTC), Config(), store)
    assert spec["start"] == spec["weekly_start"] == "2026-09-01T10:00:00+00:00"


def test_manual_weekly_run_uses_current_week_and_earlier_activity(setup):
    paths, store = setup
    now = datetime(2026, 9, 16, 10, tzinfo=UTC)
    spec = make_spec(now, Config(), store, weekly=True)
    assert spec["weekly_start"] == "2026-09-12T00:00:00+00:00"
    assert spec["weekly_end"] == "2026-09-16T10:00:00+00:00"
    project = store.projects()[0]["id"]
    store.activity(project, "agent", {"text": "Monday progress"}, "2026-09-14T10:00:00+00:00")
    evidence = Pipeline(paths, Config(), store, None).build_evidence(spec, [], [])
    assert evidence["projects"][0]["weekly_activity"][0]["data"]["text"] == "Monday progress"


def test_explicit_retry_regenerates_rejected_plan_without_reprompting(setup):
    paths, store = setup
    provider, collector = FakeProvider(), FakeCollector()
    valid_generate = provider.generate

    def generate(role, stage, evidence, timeout):
        result = valid_generate(role, stage, evidence, timeout)
        if stage == "planner":
            result["tasks"][0]["project_id"] = "not-in-report"
        return result

    provider.generate = generate
    pipeline = Pipeline(paths, Config(), store, None, provider, collector)
    first = pipeline.run()
    assert first["status"] == "failed" and not store.tasks()
    provider.generate = valid_generate
    second = pipeline.run(retry=first["id"])
    assert second["status"] == "partial" and not store.tasks()
    assert len(store.suggestions(second["id"])) == 1
    assert collector.requests == 1


def test_job_lock_prevents_second_report(setup):
    paths, store = setup
    pipeline = Pipeline(paths, Config(), store, None, FakeProvider(), FakeCollector())
    with lock(paths.state / "job.lock") as acquired:
        assert acquired
        with pytest.raises(ValueError, match="already running"):
            pipeline.run()
    assert not store.runs()


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "fake-ai"
    script.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,sys
from pathlib import Path
args=sys.argv[1:]
Path(os.environ["FAKE_ARGS"]).write_text(json.dumps(args))
Path(os.environ["FAKE_PROMPT"]).write_text(sys.stdin.read())
data=json.loads(os.environ.get("FAKE_RESULT",'{"tasks":[]}'))
if "--output-last-message" in args:
    Path(args[args.index("--output-last-message")+1]).write_text(json.dumps(data))
else:
    print(json.dumps({"subtype":"success","structured_output":data}))
"""
    )
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("FAKE_PROMPT", str(tmp_path / "prompt.txt"))
    monkeypatch.setattr(
        "herdr_tasks.providers.codex_defaults", lambda: ("configured-model", "high")
    )
    return script


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_provider_invocation_uses_structured_output_and_no_shell(fake_cli, provider):
    result = Provider().generate(
        Role(provider=provider, model="literal;model", effort="high", binary=str(fake_cli)),
        "planner",
        {"projects": []},
        10,
    )
    assert result == {"tasks": []}
    args = json.loads((fake_cli.parent / "args.json").read_text())
    assert "literal;model" in args
    if provider == "codex":
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in args and "remote_plugin" in args
    else:
        assert args[args.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in args
    assert "untrusted evidence" in (fake_cli.parent / "prompt.txt").read_text()


def test_malformed_provider_result_is_rejected(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_RESULT", '{"tasks":[{"title":"Bad"}]}')
    with pytest.raises(ValueError, match="invalid planner"):
        Provider().generate(Role(binary=str(fake_cli)), "planner", {}, 10)


def test_subprocess_output_and_time_are_bounded():
    with pytest.raises(ProcessError, match="output limit"):
        execute([sys.executable, "-c", "print('x'*20000)"], limit=1000)
    with pytest.raises(ProcessError, match="timed out"):
        execute([sys.executable, "-c", "import time;time.sleep(5)"], timeout=0.05)


def test_new_day_does_not_duplicate_carried_task(setup):
    paths, store = setup
    pipeline = Pipeline(paths, Config(), store, None, FakeProvider(), FakeCollector())
    run = pipeline.run(now=datetime(2026, 9, 15, 8, tzinfo=UTC))
    store.accept_suggestion(run["id"], 0)
    original = store.tasks()[0]
    pipeline.run(now=datetime(2026, 9, 16, 8, tzinfo=UTC))
    assert len(store.tasks()) == 1
    assert store.tasks()[0]["id"] == original["id"]


def test_ai_does_not_recreate_a_manually_completed_task(setup):
    paths, store = setup
    pipeline = Pipeline(paths, Config(), store, None, FakeProvider(), FakeCollector())
    run = pipeline.run()
    store.accept_suggestion(run["id"], 0)
    original = store.tasks()[0]
    store.edit_task(original["id"], original["revision"], status="completed")
    pipeline.run()
    assert len(store.tasks()) == 1
    assert store.task(original["id"])["status"] == "completed"
