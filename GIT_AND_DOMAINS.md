# Repository and domain plan

## Binary history

Labs commit `270b164` (“autobuilder micro-cycle for noaerth-labs”) added `node_modules/@next/swc-darwin-arm64/next-swc.darwin-arm64.node`. It is about 124MB. It is not in the current `main` tree (`fa2770a`) and not in `portfolio/noaerth-labs/public-feed`. `.gitignore` already ignored `node_modules/`; the file was committed anyway. `*.node` is now ignored on the release branch.

Team has the same class of historical blobs (`next-swc` and `sharp` darwin binaries) from an autobuilder micro-cycle. They are not in `portfolio/noaerth-team/public-room`.

GitHub currently has only:

- `portfolio/noaerth-labs/public-feed`
- `portfolio/noaerth-team/public-room`

There is no `main` on either remote. Those release branches are orphan snapshots. They do not share history with local `main`. Pushing local `main` would resend the oversized blobs and be rejected.

## Canonicalization

Option A does not work: a normal push of `main` still transfers the old blob.

Option B is the current state. Treat the public-feed and public-room branches as the release candidates.

Option C, replacing remote `main` with the clean history, is `OWNER_APPROVAL_REQUIRED`. Do not force-push. Local `main` and its dirty working trees stay in place.

`scripts/preflight-size.py` fails if any file tracked at HEAD is over 20MB.

## Dirty mains

Both local `main` worktrees contain a large autobuilder index: claim registers, rebuild backups, and generated reports, plus some app files. They were not wiped and were not committed. Useful product code from those trees was already superseded by the release branches. A file-by-file port is still `OWNER_APPROVAL_REQUIRED` before anyone cleans the working tree.

## Domains

Existing Vercel projects, same team: `noaerth`, `noaerth-labs`, `noaerth-team`. Do not create new projects.

Target:

- `www.noaerth.com` → noaerth
- `labs.noaerth.com` → noaerth-labs, branch `portfolio/noaerth-labs/public-feed`
- `team.noaerth.com` → noaerth-team, branch `portfolio/noaerth-team/public-room`

DNS was not changed. Attaching the subdomains in the Vercel project settings is `OWNER_ACTION_REQUIRED`.

Do not deploy Team until `NOAERTH_TEAM_SECRET` is set in that project’s environment. Labs does not need that secret. The Vercel daily cap still blocks deploys. Status remains `RELEASE_READY — PROVIDER_BLOCKED` plus `DOMAIN_CONFIGURATION_REQUIRED`.

Main `/labs` and `/team` stay on www.noaerth.com until those hostnames answer.
