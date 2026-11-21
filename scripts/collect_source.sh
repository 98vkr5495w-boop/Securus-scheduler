#!/usr/bin/env bash
set -euo pipefail

query=${1:?"usage: collect_source.sh 'mode=fast&source=source-id'"}
: "${SECURUS_URL:?SECURUS_URL is required}"
: "${SECURUS_OIDC_TOKEN:?SECURUS_OIDC_TOKEN is required}"

source_id=${query#*source=}
source_id=${source_id%%&*}

retry_args=(--retry 2 --retry-all-errors)
# A transport failure may hide a partial commit. Do not replay this POST.
if [[ "$source_id" == "propline" ]]; then
  retry_args=(--retry 0 --max-time 130)
fi
response=$(curl --fail-with-body --silent --show-error \
  "${retry_args[@]}" \
  -X POST \
  -H "Authorization: Bearer ${SECURUS_OIDC_TOKEN}" \
  -H "Accept: application/json" \
  "${SECURUS_URL%/}/api/collect?${query}")

jq -e \
  '.accepted == true and (.results | length == 1) and .results[0].status == "SUCCEEDED"' \
  <<<"${response}" >/dev/null

records=$(jq -r '.recordsWritten // 0' <<<"${response}")

# Only this caller opts in. Never export an unvalidated body or credential.
if [[ "${SECURUS_RECEIPT_OUTPUT:-}" == "propline" ]]; then
  [[ "$source_id" == "propline" && -n "${GITHUB_OUTPUT:-}" ]]
  for field in startedAt completedAt; do
    stamp=$(jq -er --arg field "$field" '
      .results[0] | select(.sourceId == "propline" and
        (.recordsWritten | type == "number") and .recordsWritten > 0)
      | .[$field] | select(type == "string" and
        test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]{3}Z$"))
    ' <<<"$response")
    printf '%s=%s\n' "$field" "$stamp" >> "$GITHUB_OUTPUT"
  done
fi
printf 'Collector %s succeeded (%s records).\n' "${source_id}" "${records}"
