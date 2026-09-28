# portfolio-control

## What is this?
Portfolio OS is the private control plane: discovery, dossiers, queue, reviews, snapshots, and the release budget.

## Who is it for?
The daemon and the Team app. Not the public.

## Problem
Website review was being treated as the whole startup, and the running daemon could lag the control-plane commit.

## What exists
SQLite state, a dossier generator with a canonical entrypoint and a build plan, a public sanitizer, and a daemon heartbeat that records its commit.

## Distinctive
It decides and records work. Noaerth, Labs, and Team are products that read what it publishes.

## Maturity
Product

## Primary workflow
Classify the entrypoint, write a build plan, make the bounded change, then publish a sanitized snapshot.

## Incomplete
The daemon must be restarted onto the commit that contains this behavior or Team will show UPDATE REQUIRED.

## Opportunity
Restart the daemon onto the current commit after the current lock is free.

Served homepage file: `none`.
Heading: none.
