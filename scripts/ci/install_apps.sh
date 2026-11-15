#!/usr/bin/env bash
set -euo pipefail
: "${GITHUB_ENV:?Run this script from GitHub Actions}"

# the bundled Injector is Intel; arm64 runners need Rosetta
if [ "$(uname -m)" = "arm64" ]; then
  sudo softwareupdate --install-rosetta --agree-to-license 2>/dev/null || true
fi
if arch -x86_64 /usr/bin/true 2>/dev/null; then
  echo "rosetta: usable"
else
  echo "::warning::Rosetta is not usable; the Intel Injector will fail"
fi

# the installer opens with `clear` and runs under set -euo pipefail;
# with TERM unset that single failure aborts the whole install
export TERM=xterm-256color
curl -fsSL --max-time 120 "https://gitlab.com/norbyv1/opiumware/-/raw/main/inst" -o /tmp/opium-inst.sh
set +e
bash /tmp/opium-inst.sh 2>&1 | tee /tmp/opium-install.log
install_status=${PIPESTATUS[0]}
set -e
echo "installer exit: $install_status"
tail -40 /tmp/opium-install.log || true
if [ "$install_status" -ne 0 ]; then
  echo "::warning::Opiumware installer exited $install_status; continuing without a Player"
fi

# the installer uses /Applications when writable, otherwise ~/Applications
if [ -d /Applications/Opiumware.app ]; then
  app_directory=/Applications
else
  app_directory="$HOME/Applications"
fi
{
  echo "APP_DIR=$app_directory"
  echo "OPOM_ROOT=$HOME/Opiumware"
  echo "OPOM_ROBLOX_APP=$app_directory/Roblox.app"
  echo "OPOM_EDITOR_APP=$app_directory/Opiumware.app"
  echo "OPOM_WORKSPACE=$HOME/Opiumware/workspace"
} >> "$GITHUB_ENV"
for app in "$app_directory/Roblox.app" "$app_directory/Opiumware.app"; do
  if [ -d "$app" ]; then echo "installed: $app"; else echo "::warning::missing $app"; fi
done
