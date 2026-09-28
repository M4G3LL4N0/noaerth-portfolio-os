# Ecosystem contract

NOAERTH OWNS: portfolio presentation.

LABS OWNS: public studio activity.

TEAM OWNS: internal studio UI.

PORTFOLIO OS OWNS: state and execution.

Labs is read-only. Team actions are an allowlist (`create_task`, `pause`, `resume`, `priority`, `reject_review`) sent to `portfolio serve` on localhost. The web apps do not open SQLite.

See `NOAERTH_ECOSYSTEM_ARCHITECTURE.md` in the startups root for the longer map. Domains `labs.noaerth.com` and `team.noaerth.com` are `DOMAIN_CONFIGURATION_REQUIRED`.
