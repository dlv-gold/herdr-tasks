# Publishing Herdr Tasks

Publication is deferred until the maintainer is ready. These are preparation and release instructions, not commands executed automatically by the plugin.

## Where it is published

Herdr installs plugins from public GitHub repositories. The [Herdr marketplace](https://herdr.dev/docs/marketplace/) discovers repositories with the `herdr-plugin` GitHub topic and a parseable `herdr-plugin.toml` on the default branch. This plugin's manifest is at the repository root. Marketplace discovery is automatic, not a separate registry upload or review.

## Before the first public push

1. The project uses the MIT license, declared in `pyproject.toml`. Keep the full `LICENSE` text and copyright notice in source and release artifacts; the packaging test verifies inclusion.
2. Choose the GitHub owner and repository name. Replace `OWNER/herdr-tasks` below with that actual destination when adding installation instructions to the README.
3. Review the commit author name and email; commits make them public when pushed. Use the maintainer's chosen Git identity, including a GitHub no-reply address if preferred.
4. Confirm the manifest and Python package versions match. Leave the changelog entry unreleased until the actual release date.
5. Review the Git file list and diff. Keep virtual environments, auth files, environment files, task databases, report exports, logs, and local CLI settings out of Git. Exclusion patterns and targeted scans do not detect every possible secret pasted into source.

## Verify the checkout

```sh
python3 scripts/bootstrap.py
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m build --no-isolation
git diff --check
git status --short
```

The tests use synthetic providers and temporary state. `test_release.py` builds both package formats with synthetic private files planted in its temporary checkout and verifies their exclusion. The optional real-Herdr test uses a separate test-owned session and fake providers:

```sh
HERDR_TASKS_LIVE_TEST=1 .venv/bin/python -m pytest tests/test_live_herdr.py -q
```

The source distribution contains the Herdr manifest and installation scripts. The wheel alone is not a complete Herdr plugin. Build artifacts stay out of Git; the marketplace installs the GitHub source checkout.

## When publication is explicitly authorized

1. Create the chosen GitHub repository, configure `origin`, and push the reviewed `main` branch. Do not change visibility or push while publication is deferred.
2. Test a clean install under an isolated user configuration, using the published repository:

   ```sh
   herdr plugin install OWNER/herdr-tasks
   ```

   This is an installation command: it runs the manifest build and enables scheduling. Use a test environment rather than overwriting the maintainer's existing linked installation. Each installer signs into their own Codex or Claude CLI.

3. Set the repository description and add the GitHub topic `herdr-plugin` when ready for automatic marketplace discovery. Herdr documents a roughly 30-minute index refresh.
4. Date the changelog entry, tag the verified release commit as `v0.1.0`, and publish release notes. Installers can select that version with `herdr plugin install OWNER/herdr-tasks --ref v0.1.0`.

Keep publication, topic changes, and release creation explicit; no script in this repository performs them automatically.
