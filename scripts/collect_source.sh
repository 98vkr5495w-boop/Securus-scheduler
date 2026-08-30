#!/usr/bin/env bash
set -euo pipefail

query=${1:?"usage: collect_source.sh 'mode=fast&source=source-id'"}
: "${SECURUS_URL:?SECURUS_URL is required}"
: "${SECURUS_OIDC_TOKEN:?SECURUS_OIDC_TOKEN is required}"

source_id=${query#*source=}
source_id=${source_id%%&*}

response=$(curl --fail-with-body --silent --show-error \
  --retry 2 --retry-all-errors \
  -X POST \
  -H "Authorization: Bearer ${SECURUS_OIDC_TOKEN}" \
  -H "Accept: application/json" \
  "${SECURUS_URL%/}/api/collect?${query}")

jq -e \
  '.accepted == true and (.results | length == 1) and .results[0].status == "SUCCEEDED"' \
  <<<"${response}" >/dev/null

records=$(jq -r '.recordsWritten // 0' <<<"${response}")
printf 'Collector %s succeeded (%s records).\n' "${source_id}" "${records}"
