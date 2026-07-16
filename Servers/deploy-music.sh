#!/usr/bin/env bash
# deploy-music.sh -- THE way to deploy the music app on pearl (issue #131).
#
# deploy-mcc.sh's contract, one directory over -- see that file's banner for
# why pull + restart is not a deploy (`next start` serves the compiled
# .next/, not source). This script enforces the one valid order --
# pull -> install -> build -> restart -- and set -e fails loudly at the first
# broken step, so a failed build never restarts the service.
#
# Run it on pearl, from anywhere:  ~/project-squirrel/Servers/deploy-music.sh
#
# This is the MANUAL path -- merle-autodeploy (autodeploy.sh) watches
# origin/main and runs the same order on its own when a merge touches music/
# (MERLE_DEPLOY_MUSIC=1 in its unit). Keep the two in step.

set -euo pipefail

REPO="${MERLE_REPO:-$HOME/project-squirrel}"

echo "==> git pull ($REPO)"
git -C "$REPO" pull --ff-only

cd "$REPO/music"

echo "==> pnpm install --frozen-lockfile"
pnpm install --frozen-lockfile

echo "==> pnpm build"
pnpm build

echo "==> sudo systemctl restart music-app"
sudo systemctl restart music-app

echo "==> music-app is $(systemctl is-active music-app)"
