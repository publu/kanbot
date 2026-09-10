#!/usr/bin/env bash
# Deploy the landing page (site/) to Vercel (production) and keep the alias
# pointed at the latest deployment. *.vercel.app aliases don't auto-track prod
# deploys, so we re-alias every time here.
set -euo pipefail

SCOPE="pablo-9865s-projects"
ALIAS="kanbot-gamma.vercel.app"   # getkanbot.vercel.app is held by an older team account

cd "$(dirname "$0")/.."

DEP=$(vercel deploy --prod --yes --scope "$SCOPE" 2>&1 \
  | grep -oE 'https://kanbot-[a-z0-9]+-pablo-9865s-projects\.vercel\.app' | head -1)

if [ -z "$DEP" ]; then
  echo "deploy failed (no deployment URL)" >&2
  exit 1
fi
echo "deployed: $DEP"

vercel alias set "$DEP" "$ALIAS" --scope "$SCOPE"
echo "aliased:  https://$ALIAS -> $DEP"
