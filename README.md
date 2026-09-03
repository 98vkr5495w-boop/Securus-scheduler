# Securus Scheduler

This intentionally public repository contains only the minimal, non-secret transport used to keep Securus's public data feeds fresh and request one paper-only evaluation after a successful collection cycle.

It does **not** contain Securus application source, model logic, bankroll rules, databases, backups, API keys, passwords, or trading credentials. The scheduler cannot place real-money bets.

Authentication uses a short-lived GitHub Actions OIDC identity. Securus independently verifies the repository ID, public visibility, default branch, workflow filename, event type, token audience, signature, and expiration before accepting a request. No repository secret is required.

The primary workflow uses standard GitHub-hosted runners on an off-peak
30-minute cadence, prevents overlapping runs, checks decision-critical source
freshness, and submits at most one journaled paper scan per completed cycle.
Because GitHub schedules are best-effort, an isolated watchdog checks 15 minutes
after each primary slot and dispatches the existing trusted scheduler only when
no successful `collect-and-scan` job completed recently. The watchdog has
Actions write permission but no OIDC permission and never contacts Securus.

Scheduled and recovery scans derive a UUIDv4 request ID from the repository ID
and the canonical UTC `:07`/`:37` cycle key. If status checks race or a recovery is duplicated, Securus's
durable run journal returns the existing result instead of executing the same
scan twice. The cadence gate and shared scheduler concurrency limit redundant
collection writes. Both workflows still share GitHub as a provider, so the
watchdog reduces missed-trigger risk but is not an independent uptime guarantee.

Official NBA injury-report availability is independently checked on a separate
runner job with no OIDC capability. That job downloads only the exact NBA PDF
named by Securus, verifies its byte count and SHA-256 digest, and uses Poppler's
`pdftotext` to count team submissions only within the requested game-date
section. It reports the canonical matchup identities it actually found and
requires both official team blocks for each one. It passes bounded
diagnostics—not report text, player data, or a client readiness decision—to a
fresh authenticated job. Securus binds those diagnostics to both its current
PDF collection and its independently collected NBA schedule; the reported and
expected matchup sets must match exactly. Securus remains authoritative for
per-sport readiness, so an incomplete or unavailable NBA report keeps NBA in
`NO_BET` without suppressing otherwise-ready sports.

A restricted twice-monthly heartbeat keeps GitHub's public-repository schedules
from being disabled for inactivity; only that isolated job receives repository
contents-write permission. The watchdog receives only Actions dispatch permission.
