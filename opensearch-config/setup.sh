#!/usr/bin/env sh
# Install the jkbms index template (idempotent — safe to re-run).
# Normally run automatically by the opensearch-init service in docker compose.
set -e
OS_URL="${OPENSEARCH_URL:-http://localhost:9200}"
DIR="$(cd "$(dirname "$0")" && pwd)"

sed "s/%INDEX_PREFIX%/${INDEX_PREFIX:-jkbms}/g" "$DIR/index-template.json" |
  curl -fsS -X PUT "$OS_URL/_index_template/jkbms" \
  -H 'Content-Type: application/json' \
  --data-binary @-
echo "index template installed for ${INDEX_PREFIX:-jkbms}-* at $OS_URL"
