# Changelog

All notable changes to Portfolio OS are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-10-02

First public release. The control plane that runs the Noaerth portfolio,
published rather than demonstrated.

### Added

- **SQLite source of truth.** Work items, agent runs, reviews, locks and events
  in one store, so a model that forgets is a recoverable incident rather than a
  data loss.
- **Work queue with locks.** Two agents cannot claim the same work item.
- **Reviewer separation.** The role that implements a change cannot close that
  change by reviewing it. A failed review creates follow-up work instead of
  silently closing.
- **Visual QA gate.** A page returning HTTP 200 is not a pass.
  `VISUAL_QA_PENDING` stays until a reviewer records desktop *and* mobile
  passes. Functional QA and design-quality review are separate gates.
- **Allowlist public boundary.** `publish` emits only events explicitly marked
  public. `PRIVATE_SYSTEM` events are excluded by construction, not by a
  cleanup pass afterwards. `publish/public.json` is committed so the boundary
  can be inspected rather than trusted.
- **Directory exclusion.** `openlegal-data` is refused at discovery. Only an
  `OWNER-PRIVATE` marker is stored — no path, no file contents.
- **Release blocking.** Deployment stays suppressed while a Vercel daily-cap
  blocker is on record; design and review work continues regardless.
- **Isolated worktrees.** Dirty product repos are never edited in place. Work
  goes to `portfolio/<startup>/<work-item>`.
- **Local engine UI.** `portfolio up` serves on `127.0.0.1:8787`, loopback only,
  using a random local credential that is never printed.
- **External intelligence lane.** `portfolio landscape` scores reuse fit and
  reuse leverage, then recommends `OWNER_REVIEW`. Nothing self-authorises.
- **Ecosystem grouping.** Group ventures so shared resources and duplicated
  effort become visible.

### Changed

- Replaced two hardcoded `/Users/matador` absolute paths in `execute.py` and
  `preview.py` with `PORTFOLIO_OS_SHOT`. This was both a portability bug and a
  path leak; the visual capture path previously resolved on one machine only.
- `dossiers/` is no longer tracked. It is generated per-venture analysis,
  rebuilt from the database, and covers ventures that are not all public.
- `.github/` is no longer gitignored, so the CI workflow is actually tracked.

### Verified

- 125 tests pass
- `portfolio --help` and `portfolio doctor` exit 0
- CI runs the suite on Python 3.11, 3.12 and 3.13
- gitleaks over the working tree and full reachable history: 0 findings
- No third-party runtime dependencies

[Unreleased]: https://github.com/M4G3LL4N0/noaerth-portfolio-os/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/M4G3LL4N0/noaerth-portfolio-os/releases/tag/v0.1.0
