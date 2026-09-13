# Securus Scheduler

This intentionally public repository contains only the minimal, non-secret transport used to keep Securus's public data feeds fresh and request one paper-only evaluation after a successful collection cycle.

It does **not** contain Securus application source, model logic, bankroll rules, databases, backups, API keys, passwords, or trading credentials. The scheduler cannot place real-money bets.

Authentication uses a short-lived GitHub Actions OIDC identity. Securus independently verifies the repository ID, public visibility, default branch, workflow filename, event type, token audience, signature, and expiration before accepting a request. No repository secret is required.

The primary workflow uses standard GitHub-hosted runners on an off-peak
30-minute cadence, prevents overlapping runs, checks decision-critical source
freshness, and submits at most one journaled paper scan per completed cycle.
Because GitHub schedules are best-effort, an isolated watchdog performs a
lightweight check every ten minutes. It reads only Securus's public source
timestamps and dispatches the trusted scheduler when any frequent feed is due
for its 30-minute refresh. Successful verification-only paper-job reruns cannot
reset that clock. Failed or unavailable source checks have a ten-minute retry
cooldown measured from actual `collect-inputs` attempts, preventing rapid retries.
The watchdog has Actions write permission but no OIDC permission. It sends no
GitHub credential to Securus and never logs source response bodies.
Deep-stat freshness is checked separately against the existing six-hour cadence,
so a skipped :07 slot cannot starve nflverse or Baseball Savant updates.
It also checks after trusted main-branch scheduler completions, including
validation deployments. Pull-request and foreign-repository completions cannot
enter this privileged recovery path. A completed refresh immediately suppresses
another dispatch; unavailable feeds retain the bounded retry cooldown.
If GitHub collection history is unavailable too, recovery defers to the next
check rather than repeatedly dispatching with an unknown cooldown. That state
does not claim the feeds are healthy; Securus keeps stale decisions fail-closed.

GitHub's cron delivery for this repository is best-effort and has been observed
to degrade sharply: 48 scheduled scheduler runs per day (a perfect two per hour)
on 2026-09-06 through 2026-09-08 fell to 10 to 15 per day from 2026-09-10, and
the watchdog fell from 48 per day to 7 per day even after its cron was raised
to six per hour. That degradation began around 13:00 UTC on 2026-09-09 with no
repository change, so it is a platform-side throttle that no workflow edit can
repair. The chained completion checks below recover single missed slots; only
the prepared external recovery timer removes the dependence on GitHub cron.

Every cadence-gate result is classified (`gate_state`): `DUE`, `FRESH`
(feeds under 30 minutes old), `PENDING` (another trusted runtime already owns
the cycle), or `DEFERRED`. A deferred cycle leaves feeds stale, so the gate
fails the run visibly instead of reporting a green skip. A RUNNING collector
receipt counts as "in progress" only while it is younger than the 20-minute
lease horizon and only for the decision feeds the gate depends on; an older or
unverifiable receipt is an abandoned worker and recovery proceeds.

Scheduled and recovery scans derive a UUIDv4 request ID from the repository ID
and the canonical UTC `:07`/`:37` cycle key. Delayed triggers are promoted to the
current cycle rather than replaying an obsolete scan. The paper scan now executes inside Securus's bounded request and its status
poll outlasts Securus's abandonment horizon, so a dead worker is reported as
`RUN_TIMED_OUT` rather than an ambiguous verification failure; public logs carry
only that coarse category, never markets or decision reasons. If status checks race or a recovery is duplicated, Securus's
durable run journal returns the existing result instead of executing the same
scan twice. The cadence gate and shared scheduler concurrency limit redundant
collection writes. Both workflows still share GitHub as a provider, so the
watchdog reduces missed-trigger risk but is not an independent uptime guarantee.

The prepared [external recovery Worker](external/README.md) supplies a separate
ten-minute timer after account deployment and secure credential setup. Its
presence in this repository is not proof that independent delivery is active.

Storage cleanup is automatic before and after ordinary collection. In addition,
the watchdog dispatches the existing storage-only workflow when storage is in
WARNING and feeds are still fresh. This dedicated cleanup targets 74% utilization,
below the unchanged 75% warning boundary, instead of stopping at the ordinary
83.5% collection-admission target. Due collection retains priority below the
unchanged 85% critical boundary; critical storage permits only maintenance.
In addition, every completed collection cycle runs one bounded dedicated drain
toward the same 74% target while storage is in WARNING and feeds are fresh, so
cleanup no longer waits for a watchdog cron that GitHub may not deliver; an
incomplete drain is annotated as a warning, never reported as healthy.
The storage-only mode cannot collect or scan paper bets. Both paths retain the
known-idle check, ten-minute attempt cooldown, four-minute/12-pass cleanup budget,
stagnation stop and verified archival/deletion protections. An incomplete drain
is reported as incomplete, never healthy; later eligible checks can continue it.
This remains dependent on GitHub delivery until the separate timer is activated.

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
