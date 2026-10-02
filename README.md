# Noaerth Portfolio OS

Autonomous venture-studio operating system for the Noaerth portfolio. It chooses a bounded improvement, builds it, reviews it, and keeps going. The Noaerth website reads sanitized output. It does not own this database.

## Architecture

```text
discover / import ledgers
        |
        v
   SQLite (source of truth)
        |
        +--> work queue, reviews, locks, events
        |
        +--> public.json   (PUBLIC events only)
        |
        +--> team.json     (TEAM + PUBLIC, never the private directory)
        |
        v
   Noaerth /labs  reads public.json or PORTFOLIO_OS_PUBLIC_URL
   Noaerth /team  reads team.json only after a server-side session
```

A startup is not finished because a page returns HTTP 200. `VISUAL_QA_PENDING` stays until a visual reviewer records desktop and mobile passes.

## Safety

`openlegal-data` is refused at directory discovery. The only stored record is `OWNER-PRIVATE`, with no path and no file contents. `PRIVATE_SYSTEM` events are excluded from public snapshots by an allowlist, not a cleanup pass.

Autonomous `portfolio run` refreshes priority, writes a report, and publishes snapshots. It does not delete projects or retry Vercel while a daily cap is recorded.

A rendered page is not a design pass. Functional visual QA and design-quality review are separate. Release-ready requires both, plus a clean build when one was run. A richer earlier homepage fails the design review and opens recovery work.

Dirty product repos are not edited in place. Isolated work uses `portfolio/<startup>/<work-item>` worktrees. If the main tree is dirty, the fix stays on that branch and the startup is marked for merge review.

The role that implements a change cannot close that change by reviewing it. A failed review creates follow-up work.

## Execution

`portfolio run --startup <slug>` locks that startup, reads its repo, renders the homepage, and records founder, engineering, functional visual, design-quality, product, and QA results. It does not roam into other startups. A failed visual review creates follow-up work. The implementing role cannot approve its own change.

`portfolio daemon` repeats one startup per interval (default 120 seconds) and stops when `data/daemon.stop` exists.

Deployment attempts stay suppressed while a Vercel daily-cap blocker is on record. Design and review work still runs.

## Team secret

Set `NOAERTH_TEAM_SECRET` to a long random value in the server environment. Do not commit it. The login route compares a hash of the submitted password with a hash of that secret and sets an HTTP-only cookie. If the variable is missing, `/team` shows no internal data.

## Two browsers

Team is the studio. Portfolio OS UI is the engine. They share one database and one evidence directory.

```sh
cd ~/startups/noaerth-portfolio-os
PORTFOLIO_OS_API_TOKEN=... portfolio up
```

That starts the daemon only when the heartbeat is stale, then serves the engine UI at `http://127.0.0.1:8787`. `portfolio down` stops that server and asks the daemon to exit. `portfolio down --all-previews` also stops previews this process owns.

Team runs separately:

```sh
cd ~/startups/noaerth-portfolio-os/worktrees/noaerth-team
pnpm dev
```

The checkout at `~/startups/noaerth-team` is still the historical `main` tree and is dirty. It is not switched. The operating Team branch is `portfolio/noaerth-team/public-room`. Local login uses `NOAERTH_TEAM_SECRET` in an ignored `.env.local`. The preferred hostname remains `noaerth-team.noaerth.com`. Do not deploy it until that secret is set in production and a human approves the commit.

Daily loop: open Team, review screenshots, approve an exact commit, queue deployment. Open Portfolio OS when you need workers, locks, the shard cycle, or a preview process. Neither button deploys to Vercel by itself.

## CLI

From this directory:

```sh
python3 -m portfolio_os discover
python3 -m portfolio_os status
python3 -m portfolio_os startups
python3 -m portfolio_os startup gh0st
python3 -m portfolio_os queue
python3 -m portfolio_os review --startup gh0st --role VISUAL_REVIEWER --dimension mobile --finding "..." --resolution fail --work-item 12
python3 -m portfolio_os block-release --startup gh0st --reason "Vercel hobby daily deployment cap"
python3 -m portfolio_os publish
python3 -m portfolio_os report --kind daily
python3 -m portfolio_os run --startup gh0st
python3 -m portfolio_os doctor
```

## Noaerth

`portfolio publish` copies the public snapshot to `noaerth/data/portfolio-public.json`. Production can instead set `PORTFOLIO_OS_PUBLIC_URL` and revalidate every 60 seconds, without a deploy per event.

Team:

- `NOAERTH_TEAM_SECRET` (16+ characters, server only)
- `PORTFOLIO_OS_TEAM_FILE` or `PORTFOLIO_OS_TEAM_URL` plus `PORTFOLIO_OS_READ_TOKEN`
- Writes from the website proxy to `PORTFOLIO_OS_WRITE_URL` only when `PORTFOLIO_OS_API_TOKEN` is set. Otherwise the action route returns 501 and the CLI remains the write path.

## External intelligence

The fourth lane: what already exists elsewhere, what can be reused, who competes.

```sh
portfolio landscape evalforge            # research one startup
portfolio landscape --all --batch 10     # batch by coverage shard
portfolio landscape --coverage           # portfolio research coverage
portfolio landscape --opportunities      # reuse / competition / name collisions
portfolio landscape evalforge --write    # EXTERNAL_LANDSCAPE.md
```

Each startup receives a structured external landscape with an `EXTERNAL_LANDSCAPE.md`
artifact. `REUSE_FIT` (0–100) scores ten recorded factors; `REUSE_LEVERAGE`
(0–100) summarises how much of the intended implementation already exists.

Nothing self-authorises. The strongest recommendation is `OWNER_REVIEW`, which
creates an `EXTERNAL_REUSE_REVIEW` work item for a human. Integration work is
only created after that review. `INTEGRATION_ENGINEER` never writes safety,
payment, auth or spending controls.

See `docs/EXTERNAL_INTELLIGENCE.md`.

## Tests

```sh
PYTHONPATH=. python3 -m unittest discover -s tests
```
