# Release and Channel Lifecycle

This maintainer guide owns SAT version changes, release-candidate preparation,
Git tagging, GitHub Release publication, and stable/dev channel verification.
User-facing install and update commands belong in
[`installation.md`](installation.md); durable product choices belong in
[`VISION.md`](../VISION.md); current publication and validation facts belong in
[`STATUS.md`](../STATUS.md).

## Identity Model

`pyproject.toml` is the authoritative numeric SAT release version. `uv.lock`
must contain the same project version. A published stable identity also binds:

- The exact `vMAJOR.MINOR.PATCH` tag;
- The full Git source revision;
- A SHA-256 digest of the deterministic uncompressed Git archive;
- The repository URL;
- Every persisted-schema readable range; and
- The uploaded `sat-release.json` manifest and its GitHub-reported digest.

The user-visible diagnostic form is `MAJOR.MINOR.PATCH+g<revision-prefix>`, but
the revision suffix is provenance rather than another release. Ordinary stable
update notifications compare only `MAJOR.MINOR.PATCH`.

The bare `sat` product entry performs this comparison once, synchronously, in
the current task-admission process. It does not install a daemon, schedule a
job, continue after process exit, or silently activate the result. Stable
SemVer growth is rendered as a non-blocking warning with `sat update`; dev
commit movement remains detailed provenance only. A remote resolution failure
is recorded as unavailable and cannot make an otherwise safe task unavailable.
`sat --version`, help, and other local-only commands do not use this path.

The task-admission self-check and every newly created terminal `FinalReport`
carry the same `SoftwareVersionReport` object. The machine-readable final report
therefore binds release, full source revision, install mode, channel, artifact
digest, identity status, and schema support into the rollback-capable terminal
bundle; the Markdown report renders its exact release and revision. Legacy final
reports remain readable and retain their original canonical hashes when this
optional field is absent.

## When to Change the Version

Do not change the release number for every commit. Normal development advances
the exact Git revision on the dev channel while the numeric candidate version
remains unchanged. Change the number when a release scope is frozen and the
candidate is being prepared for stable publication.

Classify every user-visible change since the last stable release in
`release/change-impact.json`:

- `patch`: compatible fixes, hardening, documentation, or packaging corrections;
- `minor`: a new user-visible capability or a compatibility break while SAT is
  still on major version `0`;
- `major`: a compatibility break after `1.0.0`.

The release gate calculates the minimum permitted increment from the highest
prior release tag. It rejects a false baseline, an insufficient increment,
version drift between the tag, package, lock, and change ledger, a dirty tree,
tag reuse, incomplete schema metadata, or a tag bound to another commit.

## Prepare a Candidate

1. Freeze the intended release scope. Resolve or explicitly defer every issue
   selected for that release.
2. Set `baseline_version` to the previous stable release, or `null` only for the
   first release. Set `target_version` and list the classified changes in
   `release/change-impact.json`.
3. Set the same target version in `pyproject.toml`, then refresh the derived lock:

   ```bash
   uv lock
   ```

4. Update user documentation, schema compatibility, migrations, and current
   status in the same candidate commit.
5. Run the complete repository gate and commit all candidate files:

   ```bash
   make check
   git status --short
   ```

6. From that exact clean commit, generate a pre-tag manifest outside the
   worktree:

   ```bash
   candidate="$(git rev-parse HEAD)"
   uv run --frozen python scripts/release.py \
     --tag v0.1.0 \
     --allow-untagged-candidate \
     --output /tmp/sat-release.json
   ```

   Replace `v0.1.0` with the declared target. Inspect the manifest and preserve
   the candidate revision in release evidence.

7. Push the candidate commit and rehearse a fixed dev install using that full
   revision. Do not use moving `main` as candidate evidence:

   ```bash
   curl -fsSL \
     https://raw.githubusercontent.com/urntt/software-agent-team/main/scripts/bootstrap.sh \
     | env SAT_INSTALL_CHANNEL=dev SAT_INSTALL_REF="$candidate" bash
   ```

   Exercise first launch, a representative task, update check, channel status,
   failed-activation rollback, state preservation, and uninstall. If an earlier
   fixed candidate is already installed, retarget it without repeating bootstrap:

   ```bash
   sat channel switch dev --ref "$candidate"
   ```

   Verify that SAT resolves and displays the new exact revision before
   confirmation, stages it through the normal managed transaction, and keeps an
   unchanged revision as a no-op. A rehearsal failure returns the issue to
   development; it is not authorization to publish.

## Publish Stable

Pushing the exact annotated version tag is the explicit human publication
authorization:

```bash
git tag -a v0.1.0 -m "release: SAT 0.1.0"
git push origin v0.1.0
```

The pinned GitHub Actions workflow checks out that exact tag, installs the
locked toolchain, reruns formatting, lint, and all tests, rebuilds the manifest,
refuses an existing release, and creates the GitHub Release with exactly one
`sat-release.json` identity asset. It never publishes from a dirty checkout or
moving branch.

Never force-move or reuse a version tag. Because all local candidate gates run
before tag authorization, a post-push gate failure should be an attributable
infrastructure or publication failure and may be rerun against the same exact
tag. If the source itself must change, use the next valid SemVer; do not repair
the old identity in place.

## Verify the Published Journey

Publication is not complete evidence until a clean user-local environment
resolves the new stable release and exercises the lifecycle:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/urntt/software-agent-team/main/scripts/bootstrap.sh \
  | bash
sat --version
sat channel status
sat update --check
```

Verify the reported release, full revision, tag, artifact digest, schema ranges,
and installed channel against the GitHub Release manifest. Then verify a stable
update from the prior release, stable-to-dev and dev-to-stable switches,
automatic rollback after an injected activation failure, active-run refusal,
and preservation-aware uninstall. Record immutable evidence before announcing
the release as validated.

## Channel Semantics

- `stable` is the normal install channel. It resolves only the latest published,
  non-prerelease GitHub Release and verifies its manifest before cloning the
  tagged source.
- `dev` is an explicit developer choice. It may follow `main`, another ref, or a
  full candidate revision and always records the resolved commit. An explicit
  dev ref can retarget an installation already on dev; omitting the ref on a
  same-channel switch remains a local no-op.
- Install, update, and switch never change channels silently.
- SAT has no updater daemon or scheduler. Update discovery runs only in a SAT
  foreground process. The ordinary product entry checks once per new task;
  local version/help/status commands remain offline, and `sat update --check`
  is the explicit on-demand check.
- A stable release-number change may produce the normal update prompt. A
  commit-only change remains visible in detailed provenance without producing
  that prompt.
