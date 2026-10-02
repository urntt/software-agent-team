# OpenClaw schema-1 fixtures

These schema-only fixtures are copied from the generated SQLite schema strings
in the published OpenClaw 2026.7.1-2 package (MIT). Tests create synthetic auth
records and an old absolute registry locator; no user database or credential
is included. They exercise the production configuration transaction with the
current pinned SDK's own Doctor migration API and real local sandbox.

The source module and fixture hashes are recorded in `provenance.json`.
