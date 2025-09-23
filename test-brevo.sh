#!/usr/bin/env bash
set -euo pipefail
API="${API_BASE_URL:-http://localhost:5000}"
EMAIL="$1"

if [[ -z "$EMAIL" ]]; then
  echo "Usage: $0 you@example.com" >&2
  exit 1
fi

curl -sS "$API/health/brevo" | jq .
echo "---"
curl -sS "$API/email/test?email=$EMAIL" | jq .
