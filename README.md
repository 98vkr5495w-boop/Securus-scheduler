# Securus Scheduler

This intentionally public repository contains only the minimal, non-secret transport used to keep Securus's public data feeds fresh and request one paper-only evaluation after a successful collection cycle.

It does **not** contain Securus application source, model logic, bankroll rules, databases, backups, API keys, passwords, or trading credentials. The scheduler cannot place real-money bets.

Authentication uses a short-lived GitHub Actions OIDC identity. Securus independently verifies the repository ID, public visibility, default branch, workflow filename, event type, token audience, signature, and expiration before accepting a request. No repository secret is required.

The workflow uses standard GitHub-hosted runners on an off-peak 30-minute cadence, prevents overlapping runs, checks decision-critical source freshness, and submits at most one journaled paper scan per completed cycle.
