# noaerth-team

## What is this?
Noaerth Team is the private operating room. It reads the team snapshot and sends allowlisted actions to Portfolio OS.

## Who is it for?
The person operating the portfolio.

## Problem
The studio cannot be run from 130 terminals, and production cannot pretend a localhost action succeeded.

## What exists
Command center, startup pages, queue, and a system page that shows the control-plane commit against the daemon commit. A startup page can show the stored dossier.

## Distinctive
It is internal. Writes go through the Portfolio OS action API. There is no arbitrary shell.

## Maturity
Early Access

## Primary workflow
Sign in, read a startup dossier, and send an allowlisted action when the write URL is configured.

## Incomplete
Production Team cannot call a laptop localhost. Until a remote control plane exists, production stays read-only or unavailable.

## Opportunity
Show UPDATE REQUIRED when the daemon commit drifts from the control plane.

Served homepage file: `src/app/page.tsx`.
Heading: none.
