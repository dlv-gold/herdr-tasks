# Project Rules

## Persistent Context

- Read `notes/README.md` before substantial repository work.
- Put durable technical findings in focused Markdown files under `notes/` and link them from the notes index.
- Append a dated entry to `notes/journal.md` after material investigations and changes.
- Distinguish observations, interpretations, and unresolved questions.

## Implementation and verification

- Preserve user-owned Herdr panes and projects. Operate on explicit session and pane IDs.
- Use fake AI providers in automated tests; real provider calls are never required by the test suite.
- Run Herdr integration tests with an isolated config and an owned named session.
- Task completion is user-controlled. Generated plans save suggestions in Reports; only explicit user acceptance adds a task. Plans may reference existing tasks, but cannot complete or rewrite them.
- Group projects, tasks, and agent activity by Herdr workspace ID scoped to its local session. Directory and label changes must not change the space's task identity. Preserve existing tasks and reports during migrations.
- Use subprocess argument arrays, bounded output, and explicit timeouts.
- Keep implementation and review in the current agent unless the user authorizes delegation.
