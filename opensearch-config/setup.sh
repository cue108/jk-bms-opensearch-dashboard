#!/usr/bin/env sh
# Install the jkbms index template (idempotent — safe to re-run).
# Normally run automatically by the opensearch-init service in docker compose.
set -e
OS_URL="${OPENSEARCH_URL:-http://localhost:9200}"
DIR="$(cd "$(dirname "$0")" && pwd)"

curl -fsS -X PUT "$OS_URL/_index_template/jkbms" \
  -H 'Content-Type: application/json' \
  --data-binary "@$DIR/index-template.json"
echo "index template 'jkbms' installed at $OS_URL"
