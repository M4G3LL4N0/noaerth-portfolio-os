# External Intelligence

Portfolio OS now does four kinds of intelligence:

| Lane | Question |
|---|---|
| **Internal product** | What exists? What is broken? What should we build? |
| **Portfolio** | What is hot, cold, important, viable, commercial, neglected? |
| **External** | What already exists elsewhere? What can we reuse? Who competes? |
| **Daily executive** | What actually changed? Where should attention go? |

This document covers the third lane. Everything else is deterministic; the
reasoning model is only reached where reasoning materially improves the result.

---

## The reuse-first question

Before substantial new implementation:

> Does strong existing work already solve this problem, or a large piece of it?

The answer is recorded as one of `ADOPT`, `INTEGRATE`, `FORK`, `WRAP`,
`REFERENCE`, `REJECT`, `OWNER_REVIEW` — or, when nothing usable exists,
`CONTINUE CUSTOM`.

**No decision is ever auto-authorised.** The strongest recommendation a system
can produce is `OWNER_REVIEW`. High fit produces a work item for a human, never
a self-granted licence to rewrite a build plan.

---

## Pipeline

```
startup facts ──► query plan (3–6 queries)
                      │
                      ▼
              GitHub retrieval ──► cache (TTL 30d)
                      │
                      ▼
              deterministic filter ──► shortlist + discarded reasons
                      │
                      ▼
              reuse fit + classification + license + security
                      │
                      ▼
              ECOSYSTEM_SCOUT (optional, shortlist only)
                      │
                      ▼
              EXTERNAL_LANDSCAPE.md + database + reuse_leverage
```

Retrieval is deterministic and runs first. The Scout never crawls; it reasons
over candidates retrieval already found. It is gated on an explicit endpoint
(`TRILLIONX_SCOUT_CMD`) — an ambient agent binary on `PATH` is not consent to
spend model calls.

---

## Query generation

A startup name is a bad query: it finds forks of itself and unrelated same-name
products. Queries are derived from the *shape of the problem* instead.

Identity is weighted far above prose. `fastprocure-ai` is a procurement product
even though its README mentions "evaluating AI startups" once; a strict word
boundary would also miss `fastprocure` entirely, because `procure` sits inside a
compound. Categories additionally require specificity, so one incidental prose
mention of "data" cannot select the `data` family.

Stack vocabulary is excluded from queries — "next", "vitest" and "router"
produce searches like `router evaluation`, which find nothing.

---

## Deterministic filter

Dropped before any reasoning, each with a recorded reason:

- our own repositories (`first_party`)
- empty descriptions
- generated mirrors
- tiny forks
- stale **and** small (stale but substantial is kept — §8 explicitly wants
  interesting abandoned work)
- forks of ourselves

---

## Scoring

`REUSE_FIT` is 0–100 across ten named factors: functional overlap, code quality,
activity, license, architecture compatibility, integration effort, documentation,
community, security, maintenance. Every factor is stored, so any number on screen
can be explained.

Two rules keep it honest:

1. **Functional overlap gates the blend.** Stars, license and activity are
   necessary but not sufficient. A permissive, popular, well-maintained project
   doing a different job scores well on blend and is still not a reusable
   foundation. A published project scoring 48/100 on quality cannot reach high-fit
   on popularity alone.
2. **Classification keys on overlap, not blend.** The same reasoning applied at
   the label level.

When the plan itself carries little vocabulary the gate relaxes, because a thin
plan cannot measure relevance. That thinness is reported separately as **survey
confidence** rather than hidden.

---

## Survey confidence

A low score only means something if the survey actually worked. Four states:

| State | Meaning |
|---|---|
| `GOOD` | Retrieval worked and produced a usable shortlist |
| `THIN` | Too little retrieved to conclude |
| `ECOSYSTEM_CLOSED` | Many results, all discarded — the category is genuinely not open source |
| `NOT_RETRIEVED` | Retrieval did not run |

`THIN` and `NOT_RETRIEVED` produce `INSUFFICIENT EVIDENCE`, never a
low-reuse conclusion. `ECOSYSTEM_CLOSED` produces a genuinely useful reading: if
procurement tooling is closed-source everywhere, that gap is an unbuilt product.

---

## License

| Class | Meaning | Action |
|---|---|---|
| `PERMISSIVE` | MIT, Apache-2.0, BSD, ISC, MPL-2.0, CC0 | usable with attribution |
| `COPYLEFT_REVIEW` | GPL, AGPL, LGPL, CC-BY-SA, EUPL | review distribution before integration |
| `PROPRIETARY` | `NOASSERTION`, `other`, unlicensed-but-declared | do not copy code |
| `UNKNOWN` | absent or unrecognised | do not copy until resolved |

`NOASSERTION` is GitHub's own marker for "no license grant detected" and is
treated as proprietary, not as unknown. Attribution obligations are stored per
candidate.

---

## Competition is not a reason to stop (§17)

A closed-source project that solves the same problem is classified
`DIRECT_COMPETITOR`: visible competition, zero reuse. That is different from a
name collision, which is a branding finding.

The recommendation never says "stop". It asks whether we can differentiate, serve
a narrower user, combine workflows, use the OSS underneath, improve UX, target a
different distribution channel, or specialise.

---

## Name collisions (§19)

Detected when a third-party project of significant size uses our name.
`evalforge` collides with `jsdhwfmax/EvalForge` (210 stars). The output is
`BRAND_COLLISION_REVIEW` — never an automatic rename. It asks for assessment of
industry overlap, trademark and public confusion risk, domain availability, and
search discoverability.

---

## Coverage and TTL

Default TTL is 30 days. Research is cached per query; unchanged research is
never repeated. Coverage states: `RESEARCHED`, `STALE`, `NOT_RESEARCHED`, with
`EXTERNAL_RESEARCH_CURRENT` as the portfolio-level metric.

Refresh earlier when product direction changes, a major feature is planned, the
owner asks, or the ecosystem moves fast.

---

## Commands

```bash
portfolio landscape <startup>              # research and show
portfolio landscape <startup> --refresh    # ignore cache and re-search
portfolio landscape <startup> --write      # write EXTERNAL_LANDSCAPE.md
portfolio landscape <startup> --json
portfolio landscape <startup> --no-scout   # deterministic only
portfolio landscape --all --batch 10       # batch by coverage shard
portfolio landscape --stale                # what needs refreshing
portfolio landscape --coverage             # portfolio coverage
portfolio landscape --opportunities        # reuse / competition / collisions
```

`--all` processes by coverage shard so product development keeps running
alongside research. The scout is off unless explicitly enabled.

---

## Integration points

| Where | What |
|---|---|
| `daily_scores.reuse_leverage` | per-startup leverage, persisted daily |
| Daily report `♻ REUSE OPPORTUNITY` | top 5, max — the report stays short |
| Daily report `⚔ CROWDED` | only when competition is real |
| Startup rows | compact `♻<n>` marker, still one sentence |
| `EXTERNAL_REUSE_REVIEW` work items | created at leverage ≥ 70, for a human |
| `INTEGRATION_WORK` work items | created only after a review approves |
| Team `/opportunities` | reuse, competition, collisions, foundations, integrations |
| `GET /api/v1/landscape/<slug>` | full landscape for the startup detail view |

---

## Specialists

`ECOSYSTEM_SCOUT` (grok-4.7) — repository, competitor, framework and research
reasoning over an already-shortlisted set.

`INTEGRATION_ENGINEER` (grok-4.7) — isolates the reusable portion, adapts it,
preserves differentiation and attribution, defines the test. Falls back to a
deterministic plan when the model is unavailable.

Both refuse to write safety, payment, auth, spend, budget, guard, approval,
authz, secret or credential paths. Those controls are not agent-writable.

---

## Honest limits

- **GitHub is one source.** Commercial and enterprise categories are
  systematically under-served; that shows up as `ECOSYSTEM_CLOSED`, not as a
  false low-reuse result. Package ecosystems (npm, PyPI, crates.io) and commercial
  competitor databases are not yet queried; `EXTERNAL_LANDSCAPE.md` states which
  sources were actually surveyed.
- **Reuse fit is an estimate.** Every factor is recorded so it can be argued
  with, but it is not a measurement of integration effort.
- **The scout is not running by default.** Without `TRILLIONX_SCOUT_CMD` the
  output is fully deterministic, which means component-level analysis of *why*
  something is reusable is missing.
- **Search ranking is imperfect.** GitHub relevance favours exact-title matches;
  stars favours popular-but-unrelated work. Both are fetched and interleaved,
  which reduces but does not eliminate the error.