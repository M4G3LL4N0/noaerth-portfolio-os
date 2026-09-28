# Noaerth Portfolio OS

Internal control plane for the startup portfolio. The Noaerth website reads sanitized output. It does not own this database.

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

Autonomous `portfolio run` refreshes priority, writes a report, and publishes snapshots. It does not edit startup repositories, delete projects, or retry Vercel while a daily cap is recorded.

The role that implements a change cannot close that change by reviewing it. A failed review creates follow-up work.

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

## Tests

```sh
PYTHONPATH=. python3 -m unittest tests.test_os
```
