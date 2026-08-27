# Generated Project Starter

This is an intentionally minimal Python 3.12 starter repository. The confirmed
TaskBrief supplied by Software Agent Team is authoritative.

Replace this file with project-specific setup, start, test, and known-limitation
guidance. In `sat-project.json`, preserve the supplied setup command
`["uv", "sync", "--dev"]` and test command `["uv", "run", "pytest"]`.
Replace only the starter's `start` placeholder with the project-specific,
non-shell argv required by the TaskBrief. The final start command must be
directly runnable from the project root without appending arguments or editing
configuration. Document the exact shell form of all three manifest commands.
If you commit `uv.lock`, regenerate it without absolute paths, `file:` sources,
parent-directory dependencies, or references to SAT's private offline
wheelhouse. Otherwise keep the starter rule that ignores the local lock.

If the implementation uses a `src` layout, keep pytest importable in the clean
quality workspace (for example through the supplied pytest `pythonpath`) as
well as after `uv sync --dev`.
