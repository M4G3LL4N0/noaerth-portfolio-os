# Portfolio OS local start

From this directory:

```sh
portfolio up --open
```

That prepares the database, starts the daemon if the heartbeat is stale, and serves the engine UI at `http://127.0.0.1:8787`. On loopback it creates an ignored local credential and opens a one-time bootstrap URL. The browser session is an HTTP-only cookie. The credential is not printed and is not valid off this machine.

If the UI is already listening, `portfolio up` does not start a second server. `portfolio up --open` still opens a fresh one-time session.

Shutdown:

```sh
portfolio down
```

`portfolio down --all-previews` also stops previews this process owns.

Team, from `~/startups/noaerth-team`:

```sh
pnpm dev
```

Then open `http://127.0.0.1:4320`. Review and release approval stay in Team. Portfolio OS shows workers, queue, coverage, previews, and provider state. Neither command deploys to Vercel.
