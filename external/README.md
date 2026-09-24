# Independent Securus recovery timer

Status: prepared source, **not deployed or verified**. The existing hourly ChatGPT
health recovery remains a separate fallback. This Worker removes dependence on
GitHub's cron delivery; it still uses GitHub's API and runners for collection.

The Worker checks public feed timestamps and the completed paper-scan journal
every ten minutes and dispatches only
the existing trusted `main` scheduler when a frequent feed reaches 30 minutes or
a deep-stat feed reaches six hours. The scheduler rechecks freshness, preserves
shared concurrency, and uses its canonical cycle journal to prevent duplicate
paper scans. No betting policy, Site source, database, or private telemetry lives
in this transport repository.

The prepared recovery transport fails closed on stale/unknown D1 capacity,
unfinished source receipts, unfinished maintenance, incomplete job pages, or
unknown runtime history. It honors the ten-minute cooldown for both collection
and maintenance, including the separate Cloudflare timer, and rereads Site
evidence immediately before dispatch. The Site remains the atomic lease owner;
the public response does not expose raw lease contents. An old RUNNING receipt
is never manually treated as expired here: the Site must project abandonment
to FAILED. A missing recovery credential fails visibly even while feeds are
fresh. A completed paper scan older than 60 minutes cannot report FEEDS_CURRENT.

## Activation requires the owner's account

1. Connect the owner's Cloudflare account and inspect the existing
   `securus-maintenance` Worker before modifying anything. That Worker is a
   separate storage-only identity: preserve it and its secure pairing.
2. In GitHub's secure token settings, create an expiring fine-grained token
   restricted to `98vkr5495w-boop/Securus-scheduler`, with repository **Actions:
   read and write** and required metadata access only. No repository contents
   write, administration, private-repository access, or trading credentials.
   Store it as the Cloudflare **Secret** `GITHUB_RECOVERY_TOKEN` for this Worker.
   Never paste it into chat, Git, logs, command arguments, or source code. The
   connected ChatGPT GitHub credential must not be extracted or reused.
3. Deploy `external/recovery-worker.mjs` as `securus-recovery` using the included
   Wrangler configuration in the owner's account. No managed Site D1/R2 binding
   changes are needed. Do not overwrite `securus-maintenance` with this file.
4. Verify the actual Cloudflare Cron Trigger. Allow for propagation, then observe
   two real scheduled events and their redacted result codes. A successful HTTP
   request to `/` proves neither scheduling nor secret configuration.
5. When a feed is due, verify `RECOVERY_DISPATCHED`, a newly created GitHub
   workflow-dispatch run, a successful actual `collect-inputs` job, fresh feed
   completion times, and a verified paper scan. A dispatch receipt alone is not
   restoration evidence. Then verify subsequent checks return `FEEDS_CURRENT`.

## Maintenance and remaining acceptance checks

The separate storage-only maintenance Worker is already deployed and has
produced durable `cloudflare-cron-10m` completion receipts. Preserve its existing
identity, pairing, and Cron Trigger. Its installer is served by the Site at
`/securus-worker-install.html`. This recovery Worker refuses dispatch at
85% storage utilization or when capacity cannot be verified; independent
maintenance must restore headroom first. Do not raise the storage limit or
disable freshness checks to obtain a green status.

Activation is incomplete until real `cloudflare-cron-10m` maintenance completions
are visible, repeated recovery deliveries have been observed, storage declines
below its 75% warning boundary, and a 24-hour actual-size trend shows maintenance
keeping up. Do not claim these acceptance checks from tests or source presence.

Successful checks log redacted result codes; they are not delivered external
alerts. Configure and verify the owner's chosen alert destination separately.
The Site's `SECURUS_ALERT_WEBHOOK_URL` and maintenance Worker's
`ALERT_WEBHOOK_URL` are distinct settings. Do not put either secret in Git or
assume that configuring one configures the other. Token expiration/rotation and
an actual delivered test alert are operational acceptance checks.

The mandatory maintenance cooldown can defer recovery while the independent
maintenance timer is active. Do not remove it to obtain a green check: verify
actual timely collection and scan completion under both timers before calling
the deployment reliable. A FRESH check or accepted dispatch is not that proof.

## Failure behavior and revocation

- Public HTTP routes cannot dispatch or act as an authenticated proxy.
- Only fixed Site/GitHub URLs are contacted; redirects are rejected. The GitHub
  secret is sent only to the fixed repository's GitHub API endpoints.
- Every invocation has a 90-second/24-request budget and 512 KiB response cap.
  There is at most one POST and no automatic retry of an ambiguous dispatch.
- Pending runtimes and attempts in the last ten minutes suppress new dispatches.
  Unknown history, malformed responses, critical storage, or failed access cause
  a visible failed Cron event; they never mean healthy.
- Remove the Cron Trigger and revoke the dedicated GitHub token to stop recovery.
  The normal GitHub schedule and existing paper-only safeguards remain intact.
- If GitHub runners/API or Cloudflare scheduling are unavailable, feeds can still
  become stale. Keep fail-closed betting and the separate health monitor enabled.

Official references: [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/),
[Cloudflare Secrets](https://developers.cloudflare.com/workers/configuration/secrets/),
[GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).
