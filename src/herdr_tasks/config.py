"""Plugin-owned paths and validated user settings."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Paths:
    config: Path
    state: Path

    @classmethod
    def discover(cls) -> Paths:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        return cls(
            Path(
                os.environ.get(
                    "HERDR_PLUGIN_CONFIG_DIR", config_root / "herdr/plugins/config/herdr-tasks"
                )
            ),
            Path(
                os.environ.get("HERDR_PLUGIN_STATE_DIR", state_root / "herdr/plugins/herdr-tasks")
            ),
        )

    def create(self) -> None:
        for path in (self.config, self.state):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def database(self) -> Path:
        return self.state / "tasks.sqlite3"


@dataclass
class Role:
    provider: str = "codex"
    model: str = ""
    effort: str = ""
    binary: str = ""

    def validate(self) -> None:
        if not isinstance(self.provider, str) or self.provider not in {"codex", "claude"}:
            raise ValueError("Provider must be codex or claude")
        if not isinstance(self.effort, str) or self.effort not in {
            "",
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("Invalid reasoning effort")
        if self.provider == "claude" and self.effort in {"none", "minimal"}:
            raise ValueError("Claude effort must be low, medium, high, xhigh, max, or CLI default")
        for value in (self.model, self.effort, self.binary):
            if not isinstance(value, str) or "\x00" in value or "\n" in value:
                raise ValueError("Provider settings must be single-line strings")


@dataclass
class Config:
    version: int = 1
    enabled: bool = True
    timezone: str = "Asia/Jerusalem"
    daily_time: str = "03:00"
    weekly_day: int = 5  # Monday = 0, Saturday = 5
    weekly_time: str = "03:00"
    sample_seconds: int = 30
    agent_timeout: int = 120
    provider_timeout: int = 600
    max_agents: int = 64
    max_projects: int = 64
    evidence_bytes: int = 512_000
    history_days: int = 35
    panel_fraction: float = 0.35
    summary: Role = field(default_factory=Role)
    planner: Role = field(default_factory=Role)

    def validate(self) -> None:
        if self.version != 1 or not isinstance(self.enabled, bool):
            raise ValueError("Unsupported configuration version or enabled value")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Use a valid IANA timezone, such as Asia/Jerusalem") from exc
        for value in (self.daily_time, self.weekly_time):
            if not isinstance(value, str) or len(value) != 5:
                raise ValueError("Schedule times must be HH:MM")
            time.fromisoformat(value)
        if not isinstance(self.weekly_day, int) or not 0 <= self.weekly_day <= 6:
            raise ValueError("Weekly day must be 0 (Monday) through 6 (Sunday)")
        bounds = {
            "sample_seconds": (5, 3600),
            "agent_timeout": (5, 900),
            "provider_timeout": (10, 3600),
            "max_agents": (1, 512),
            "max_projects": (1, 256),
            "evidence_bytes": (16_000, 2_000_000),
            "history_days": (7, 365),
        }
        for key, (minimum, maximum) in bounds.items():
            value = getattr(self, key)
            if not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{key} must be between {minimum} and {maximum}")
        if not 0.15 <= self.panel_fraction <= 0.65:
            raise ValueError("Panel fraction must be between 0.15 and 0.65")
        self.summary.validate()
        self.planner.validate()

    @classmethod
    def load(cls, paths: Paths) -> Config:
        file = paths.config / "config.json"
        if not file.exists():
            return cls()
        if file.stat().st_size > 64_000:
            raise ValueError("Configuration is too large")
        try:
            data = json.loads(file.read_text())
            if not isinstance(data, dict):
                raise ValueError("Configuration must be a JSON object")
            for key in ("summary", "planner"):
                data[key] = Role(**data.get(key, {}))
            config = cls(**data)
            config.validate()
            return config
        except (TypeError, KeyError) as exc:
            raise ValueError(f"Invalid configuration: {exc}") from exc

    def save(self, paths: Paths) -> None:
        self.validate()
        paths.create()
        atomic_write(paths.config / "config.json", json.dumps(asdict(self), indent=2) + "\n")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
