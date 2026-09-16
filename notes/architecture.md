# Architecture

The task board is a normal Herdr split at the right edge of the invoking tab. One board is allowed per tab. The plugin never calls `layout.apply` and closes only panes whose plugin ownership matches.

The shared SQLite database contains durable projects, task changes, sampled activity, report stages, and agent update dispatch records. Each project is identified by its local session endpoint and Herdr workspace ID; directory changes and renames do not change that identity. Configuration is stored separately in Herdr's plugin config directory. A user process lock allows one coordinator; job leases also protect manual runs. Provider output is validated before transactions insert missions. Existing task text and completion are never AI-editable.

The coordinator enumerates local sessions, samples bounded output when agent state or output changes, and periodically reconciles sessions. A startup hook launches it and exits. It runs while at least one local server is alive. Disabled plugins pause scheduling. No systemd unit is installed.

Each batch collects live updates once, synthesizes per-project daily/weekly reports, and saves daily/weekly task suggestions. Reports offers per-suggestion Add to tasks / Add mission buttons. Only explicit acceptance creates a task. Acceptance is serialized in a transaction, recorded durably, and deduplicated against existing tasks without modifying them. Prior auto-added tasks are recognized by their report origin; project aliases resolve old report suggestions to their current space. Busy or blocked agents are not prompted. An ambiguous submission is never retried automatically. Run markers identify replies. All collected terminal text is untrusted evidence. Automated update activity is kept out of ordinary progress snapshots.

Codex and Claude adapters run independently selectable model/effort settings through their existing CLI authentication. They receive evidence over stdin, run from a private scratch directory, and cannot edit project files. They have no task execution role. Defaults use Codex and its configured model/effort when available. Provider failures do not trigger another provider or model.

Daily periods run from local 03:00 to the next local 03:00; UTC timestamps are persisted. Weekly periods end Saturday 03:00. A missed run is consolidated into one catch-up batch, reports identify missing coverage, and plans target the current day/week. Saturday batches share agent updates and produce both daily and weekly outputs.

## Implementation map

- `config.py`, `schedule.py`: plugin paths, atomic settings, timezone-aware cutoffs and planning periods.
- `store.py`: SQLite state, revision-checked edits, durable dispatch claims, idempotent planning transactions.
- `herdr.py`, `collector.py`: explicit socket endpoints, workspace grouping and directory discovery, panel ownership, bounded evidence and progress replies.
- `providers.py`, `process.py`: validated JSON contracts, independent CLI roles, bounded child processes and Linux locks.
- `pipeline.py`: persisted report stages, consolidated catch-up, independent summary saving, task planning and Markdown export.
- `daemon.py`: one local coordinator, events plus periodic reconciliation, scheduler and restart recovery.
- `ui.py`, `cli.py`: Textual board, editors, reports, settings, diagnostics and plugin actions.

Manual daily/weekly reports cover the current period up to the requested time. Weekly reports also include bounded raw activity and Git evidence, so they do not depend exclusively on successful daily reports. First catch-up runs can start at installation; later ones start from the previous successful cutoff when needed.

Saved summaries survive planner failures. An explicit retry regenerates a rejected plan using the saved evidence and summary, while an interrupted application resumes its persisted plan. Provider/model fallback is never automatic. Durable agent dispatch claims survive evidence retention.

Panel records follow terminal identity across tab moves. The host's plugin-only pane endpoint provides the final ownership check. The task UI hides future work by default, carries unfinished work forward, and offers historical/archived tasks through its history toggle. Small terminals use a compact layout.

Schema v2 separates a project's unique identity from its directories. It backs up v1 databases before upgrading. Unambiguous previous folder projects migrate to spaces without changing task IDs, content, completion, or mission links. Report aliases preserve access to original reports from the new space. Ambiguous/conflicting tasks remain available for manual assignment. Migration waits for any active reporting run to finish.
