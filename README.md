# Securus Scheduler

This intentionally public repository contains only the minimal, non-secret transport used to keep Securus's public data feeds fresh and request one paper-only evaluation after a successful collection cycle.

It does **not** contain Securus application source, model logic, bankroll rules, databases, backups, API keys, passwords, or trading credentials. The scheduler cannot place real-money bets.

Authentication uses a short-lived GitHub Actions OIDC identity. Securus independently verifies the repository ID, public visibility, default branch, workflow filename, event type, token audience, signature, and expiration before accepting a request. No repository secret is required.

The workflow uses standard GitHub-hosted runners on an off-peak 30-minute cadence, prevents overlapping runs, checks decision-critical source freshness per paper market (mirroring the per-sport requirements Securus itself enforces, so a single supplemental feed outage withholds only the affected sport), and submits at most one journaled paper scan per completed cycle. Provider requests retry transient errors, including malformed responses from overloaded keyless APIs, before a batch is reported as failed. A restricted twice-monthly heartbeat keeps GitHub's public-repository schedules from being disabled for inactivity; only that isolated job receives repository write permission.
