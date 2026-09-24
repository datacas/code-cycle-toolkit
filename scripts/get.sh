#!/usr/bin/env bash
# One-command installer: download a Code Cycle Toolkit release and install
# its skills and runtime, without cloning the repository first.
#
#   curl -fsSL https://raw.githubusercontent.com/datacas/code-cycle-toolkit/main/scripts/get.sh | bash
#
# Options go after `bash -s --`:
#
#   curl -fsSL .../get.sh | bash -s -- --version v0.3.0
#   curl -fsSL .../get.sh | bash -s -- --agent claude --scope project --project-dir .
#
# --version latest|main|vX.Y.Z picks what to download (default: latest
# release, or $CODE_CYCLE_VERSION). Every other option is passed to
# scripts/install.sh. Without any, this installs every host globally.
# --force is always added, so running this again updates an installation.

set -euo pipefail

REPO='datacas/code-cycle-toolkit'
VERSION="${CODE_CYCLE_VERSION:-latest}"
INSTALL_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      [ "$#" -ge 2 ] || { printf '%s\n' '--version requires a value.' >&2; exit 2; }
      VERSION="$2"
      shift 2
      ;;
    *)
      INSTALL_ARGS+=("$1")
      shift
      ;;
  esac
done

if [ "${#INSTALL_ARGS[@]}" -eq 0 ]; then
  INSTALL_ARGS=(--agent all --scope global)
fi
INSTALL_ARGS+=(--force)

for tool in curl tar; do
  command -v "$tool" >/dev/null 2>&1 || { printf 'Required tool not found: %s\n' "$tool" >&2; exit 1; }
done

case "$VERSION" in
  latest)
    # The /releases/latest page redirects to /releases/tag/<tag>; reading the
    # redirect avoids the API and its rate limit.
    url=$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest")
    ref="${url##*/}"
    archive="https://github.com/$REPO/archive/refs/tags/$ref.tar.gz"
    ;;
  main)
    ref='main'
    archive="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
    ;;
  *)
    ref="$VERSION"
    archive="https://github.com/$REPO/archive/refs/tags/$ref.tar.gz"
    ;;
esac

if [ "$ref" != 'main' ] && ! printf '%s' "$ref" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
  printf 'Not a release tag: %s (expected latest, main, or vX.Y.Z)\n' "$ref" >&2
  exit 2
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

printf 'Downloading Code Cycle Toolkit %s...\n' "$ref"
if ! curl -fsSL -o "$tmp/toolkit.tar.gz" "$archive"; then
  printf 'Could not download %s. Check that the version exists: https://github.com/%s/releases\n' \
    "$ref" "$REPO" >&2
  exit 1
fi
src="$tmp/toolkit"
mkdir "$src"
tar -xzf "$tmp/toolkit.tar.gz" -C "$src" --strip-components=1

[ -f "$src/scripts/install.sh" ] || { printf '%s\n' 'The downloaded archive has no scripts/install.sh.' >&2; exit 1; }

bash "$src/scripts/install.sh" "${INSTALL_ARGS[@]}"
printf 'Code Cycle Toolkit %s is installed.\n' "$ref"
