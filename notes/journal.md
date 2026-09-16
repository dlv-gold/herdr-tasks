# Project Journal

## 2026-09-15 — Foundation and approved design

### Observations

- Installed Herdr client and server are 0.9.0, protocol 22.
- Plugins support normal split panes, startup hooks, CLI/socket access, and plugin-owned state directories.
- The installed popup API has no positioning option. `layout.apply` replaces terminals and is unsuitable for adding the task panel.
- Installed Codex 0.154.0 and Claude Code 2.1.272 support noninteractive structured output.

### Interpretations

- Implement a standalone Python/Textual plugin with SQLite and a single coordinator across local sessions.
- Ask ready agents for updates once, then run a summarizer and planner sequentially.
- Default daily cutoff is 03:00 Asia/Jerusalem; weekly cutoff is Saturday at 03:00.

### Unresolved questions

- None. Provider authentication and actual model availability are environment-dependent and will be reported by the plugin.

## 2026-09-15 — Implementation and integration verification

### Observations

- Implemented the standalone Python/Textual plugin, SQLite task store, local-session collector, coordinator, Codex/Claude reporting adapters, recoverable summary/planning pipeline, and Markdown export.
- Added daily and weekly task editors, completion/reopening, status and project filters, linked missions, archive/restore, report history, and independent provider settings.
- Added locked dependency bootstrap, plugin installer/manifest, usage documentation, and normal/compact UI previews using temporary example data.
- The isolated Herdr smoke test opened a real plugin split, generated a report and tasks with a fake provider, closed the panel, and preserved the original terminals. Its coordinator stopped after its test-owned server stopped.
- Herdr test isolation requires separate XDG config/state/cache directories. `HERDR_CONFIG_PATH` alone does not isolate session discovery. Short `/tmp` paths avoid Unix socket path limits.
- Textual's test harness requires functioning local IPC. Restricted sandbox execution blocked its asynchronous wakeups; the suite passes with local IPC access.
- Reviewed manual weekly periods, first-run catch-up, rejected-plan retries, moved panel ownership, dispatch retention, and filtering of future tasks. Added regression coverage for those cases.
- Rendered and inspected previews at 48×48 and 32×24. Compact mode leaves usable task space and keeps editor/report actions available through keyboard shortcuts.

### Interpretation

- The standalone plugin can be installed without changing Herdr core or the separate Command Center project.
- Fake-provider tests exercise both subprocess adapters and transaction/recovery behavior without provider usage or prompting the user's real agents.

### Remaining environmental checks

- Real Codex/Claude authentication, selected model availability, and a wall-clock overnight run depend on the installed user's account and future runtime. No real provider calls or live-user installation were performed during implementation.

### Final verification

- Normal suite: **42 passed, 1 opt-in test skipped** (`python -m pytest -q`, Python 3.14.4).
- Isolated Herdr integration: **1 passed** with `HERDR_TASKS_LIVE_TEST=1`; includes reopening and closing with the TUI's `q` key.
- Ruff lint and formatting: passed for all 22 Python files.
- Dependency consistency: `pip check` passed.
- Package build: source distribution and wheel built successfully; source archive includes the Herdr manifest, bootstrap/installer, dependency lock, and documentation, without `.venv`.
- CLI help, initialization, status JSON, and configuration JSON: passed with temporary plugin paths.
- UI previews: inspected normal and compact color renders using example data only.

## 2026-09-15 — Stale Herdr executable during local installation

### Observations and fix

- Python dependency installation succeeded, but the link step failed because `HERDR_BIN_PATH` pointed to a removed executable under `~/.local/bin/herdr` with a ` (deleted)` suffix. The executable on PATH was valid and the running server remained compatible at 0.9.0.
- Added a shared standard-library executable resolver. Valid inherited executable paths keep priority; unavailable/nonexecutable paths fall back to Herdr on PATH. Explicit programmatic overrides fail clearly if invalid.
- The installer checks Herdr availability before dependency setup and reports failures without a traceback. The coordinator resolves the executable at each CLI invocation to tolerate replacement while it remains running.
- Added six regression tests for stale paths, precedence, invalid executables, and installer link/start commands. Relevant tests: 30 passed; lint and formatting passed.
- Completed the user's local plugin link and start steps. The installed coordinator reported `watching`, an empty error field, six discovered projects, and no report runs. No real reporting providers were invoked for verification.

## 2026-09-15 — Corrected panel-opening instructions

- Earlier user instructions and installer output incorrectly referred to a Plugins menu. Herdr 0.9.0 exposes plugin actions through the CLI and configured keybindings.
- Corrected the installer and README to show `herdr plugin action invoke herdr-tasks.toggle`. No keybindings or user pane state were changed.

## 2026-09-15 — Projects grouped by Herdr spaces

### Change

- The user requested project grouping by spaces. Replaced folder-based project identity with local session endpoint plus Herdr workspace ID. All agents/tabs within a space contribute to its project, and spaces sharing a folder remain separate.
- The selector uses Herdr space names and opens on the panel's own space by default. Added a space refresh button and removed manual folder-project creation; manual task/mission entry remains available.
- Added schema v2 with a pre-upgrade SQLite backup, task reassignment for unambiguous folders, preserved task IDs/content/completion/mission links, and aliases for historical reports. Ambiguous/conflicting tasks remain available for explicit assignment.
- Space renames and directory changes retain task identity. Empty spaces are discovered, and the task panel's own directory is excluded from project evidence. Existing report content remains unchanged.

### Verification and rollout

- **59 tests passed**, including UI selection and a real isolated Herdr server with two spaces using the same directory, fake reporting providers, and preserved terminal identities. Migration-specific tests also passed after the final status-preservation adjustment.
- Verified the installed plugin's coordinator identity and that no report was active, then stopped that owned coordinator, applied grouping, and restarted it. The Herdr server and user panes remained running.
- Live migration preserved all **4 tasks** and **14 per-project reports across 2 report runs**, verified field by field. Seven spaces were discovered with no unassigned tasks, collection gaps, foreign-key errors, or coordinator errors.
- Backup: `~/.local/state/herdr/plugins/herdr-tasks/tasks.sqlite3.before-spaces`.
- Existing open panels must be closed/reopened to load the new UI. Documentation and example previews were updated.

## 2026-09-16 — Local release privacy preparation

- Inspected provider invocation, authentication boundaries, source files, and existing package member lists without reading live credentials or running reporting agents. No embedded credentials were found by the targeted checks; the journal contained one personal home-directory path, which was generalized.
- Added Git/source-package exclusions for common credential files and local state, and disabled implicit wheel package-data inclusion. Documented installer-owned CLI authentication, inherited environment variables, and the limits of redaction.
- Added a real packaging regression test using synthetic private files in an isolated checkout. It verifies both source and wheel contents while preserving required plugin files.
- The user selected the Herdr plugin registry as the eventual destination, then explicitly deferred publication pending polish. Preparation remains local.

## 2026-09-16 — Expandable task descriptions

- Added a Description toggle to daily tasks and weekly missions. Descriptions start collapsed and display the existing task notes as literal text, with an empty-description hint when needed.
- Expanded rows stay expanded through board refreshes and space/filter changes for the lifetime of the panel. Opening or closing a description does not write to the task or change completion status.
- Existing open panels need to be closed and reopened to load the UI change. Registry publication remains deferred.
- Verification: all six existing UI tests passed. Offline checks for both daily tasks and weekly missions passed for expand/collapse, refresh persistence, literal description text, a 32-column layout, and unchanged task records. Ruff lint and formatting checks passed. No real agents were started.
- Follow-up: moved the description toggle beside Edit, using a compact arrow button with Show/Hide description tooltips to leave room for titles in narrow panels.

## 2026-09-16 — Review planner suggestions in Reports

- Planner runs now validate and save suggestions without creating board tasks. Reports displays each suggestion's title, notes, space, kind, and planned date, with Add to tasks / Add mission controls.
- Acceptance uses a database transaction and a durable receipt. Repeated or concurrent acceptance creates at most one task; existing manual, completed, archived, and previously auto-added tasks are preserved. Accepted suggestions retain their report's planning date and start unchecked.
- Suggestions follow space filters and legacy project aliases. Markdown exports include suggested next steps and their acceptance state at export time; Retry / Export refreshes the snapshot.
- Verification: normal suite **66 passed, 1 opt-in test skipped**; isolated real-Herdr integration **1 passed** using fake reporting providers. Tests cover persisted suggestions, explicit acceptance in daily/weekly UI, concurrent acceptance, duplicate/manual completion protection, invalid acceptance, and legacy auto-added tasks. Ruff lint and formatting passed.
- Backed up the installed database to `tasks.sqlite3.before-suggestions`, confirmed no report was active, gracefully stopped only the verified plugin coordinator under the report lock, and restarted it with the new code. Watching status confirmed; all four existing task records preserved unchanged. Herdr server and user panes were left running. Existing panels need reopening for the new Reports controls.
- Publication remains deferred pending polish.

## 2026-09-16 — Git and publishing preparation

- Initialized a local Git repository on `main`. Added unreleased 0.1.0 release notes and a publishing guide covering licensing, commit identity, verification, GitHub installation, and marketplace discovery.
- Verified the official marketplace documentation: listing is based on a public GitHub repository, a valid manifest on its default branch, and the `herdr-plugin` topic. There is no separate registry package upload in this flow.
- Refreshed documentation previews with temporary example data, including the description toggles. Added whitespace normalization to the preview generator.
- The packaging regression test, source/wheel build, Ruff checks, and manifest/version checks passed. Git preparation excludes local credentials, virtual environments, databases, and generated build artifacts.
- License selection and the eventual GitHub destination remain pending. No remote repository, push, public listing, or release was created; the existing configured Git identity is used only for the local initial commit.

## 2026-09-16 — MIT license selected

- Added the user-selected MIT license with the maintainer's copyright notice, declared MIT and the license file in Python package metadata, and linked the license from the README.
- Updated publication instructions and package verification to require the full license in both source and wheel artifacts. GitHub destination and publication remain pending.
