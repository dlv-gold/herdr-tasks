# Release privacy

## Observations

- Reporting invokes the locally installed Codex or Claude CLI. Plugin settings contain provider, model, effort, and executable selection, with no API-key or password field.
- The adapters inherit the local process environment, including any provider authentication variables already present. The official CLI handles authentication; the plugin does not open CLI login-token files. `codex_defaults` parses the local Codex configuration and returns only model and reasoning effort.
- Provider stderr is drained and discarded. Failure messages expose exit status rather than provider stderr content.
- A source and package audit found no credential files or strings matching the checked common provider-key/private-key patterns. A personal home-directory path in the implementation journal was replaced with a portable example.
- Git ignores common environment/credential files, local CLI configuration directories, databases, logs, and backups. Source packaging excludes those private file types. Wheel packaging disables implicit package-data inclusion.
- `tests/test_release.py` builds a source distribution and a wheel in a temporary checkout containing synthetic private files. It checks that private filenames and sentinel contents are absent and that required plugin files remain present.

## Interpretation and limits

- Publishing source or verified release artifacts does not transfer the developer's CLI login to installers. Each installer authenticates their own local CLI.
- This is a targeted inspection, not proof that arbitrary future source text cannot contain secrets. Terminal redaction is partial, and task notes, Git metadata, reports, and terminal evidence may contain private project information. Runtime state must not be published.
- There were no reads of live credential stores or authenticated provider calls during this check.
- The source distribution contains the Herdr manifest and bootstrap scripts. The wheel alone is not the complete Herdr plugin release.

## Publication status

The intended destination is the Herdr plugin registry. Publication is deferred at the user's request pending further polish. No registry upload is part of this change.
