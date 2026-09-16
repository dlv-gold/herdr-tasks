# Changelog

## 0.1.0 — Unreleased

- Right-side task board with daily tasks, weekly missions, completion checkboxes, status, notes, and expandable descriptions.
- Projects grouped by local Herdr spaces, with preserved identity across space renames and directory changes.
- Scheduled daily and weekly reports from recorded activity and updates from available agents.
- Independent Codex or Claude summarizer and planner settings, including model and reasoning effort.
- Planner suggestions in Reports, with explicit Add to tasks / Add mission controls and duplicate protection.
- Manual editing, archives, Markdown report exports, and recovery of interrupted reporting runs.
- Local CLI authentication belonging to each installer; release exclusions for common credential files and runtime data.

### Compatibility

- Linux, Python 3.11+, and Herdr 0.9.0+.
- Reporting adapters checked against Codex CLI 0.154.0 and Claude Code 2.1.272. Actual model access depends on the installer's account.
- No hosted backend, system service, or SSH-agent collection.
