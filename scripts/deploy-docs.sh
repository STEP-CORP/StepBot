#!/usr/bin/env bash
# Deploy the docs site to your own docs host.
# Usage: ./scripts/deploy-docs.sh <user@host> <remote-dir> [https://домен]
set -euo pipefail

HOST="${1:?Usage: $0 <user@host> <remote-dir> [https://домен]}"
DIR="${2:?Usage: $0 <user@host> <remote-dir> [https://домен]}"
URL="${3:-}"

DOCS_URL="$URL" npm run build --prefix docs-site
rsync -az --delete --timeout=30 docs-site/.vitepress/dist/ "$HOST:$DIR/"
[ -n "$URL" ] && curl -s -o /dev/null -w 'docs: %{http_code}\n' "$URL/"
exit 0
