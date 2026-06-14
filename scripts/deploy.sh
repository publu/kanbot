#!/usr/bin/env bash
# Deploy the static UI to Vercel (production) and keep getkanbot.vercel.app
# pointed at the latest deployment. *.vercel.app aliases don't auto-track prod
# deploys, so we re-alias every time here.
set -euo pipefail

SCOPE="ps-projects-0c7bba7e"
ALIAS="getkanbot.vercel.app"

cd "$(dirname "$0")/.."

DEP=$(vercel deploy --prod --yes --scope "$SCOPE" 2>&1 \
  | grep -oE 'https://kanbot-[a-z0-9]+-ps-projects-0c7bba7e\.vercel\.app' | head -1)

if [ -z "$DEP" ]; then
  echo "deploy failed (no deployment URL)" >&2
  exit 1
fi
echo "deployed: $DEP"

vercel alias set "$DEP" "$ALIAS" --scope "$SCOPE"
echo "aliased:  https://$ALIAS -> $DEP"
