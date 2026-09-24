#!/bin/sh
# One-command installer: download a Code Cycle Toolkit release and install
# its skills and runtime, without cloning the repository first.
#
#   curl -fsSL https://raw.githubusercontent.com/datacas/code-cycle-toolkit/main/scripts/get.sh | sh
#
# Options go after `sh -s --`:
#
#   curl -fsSL .../get.sh | sh -s -- --version v0.3.0
#   curl -fsSL .../get.sh | sh -s -- --agent claude --scope project --project-dir .
#
# --version latest|main|vX.Y.Z picks what to download (default: latest
# release, or $CODE_CYCLE_VERSION). Every other option is passed to
# scripts/install.sh. Without any, this installs every host globally.
# --force is always added, so running this again updates an installation.
# POSIX sh on purpose, so `| sh` works where sh is dash; install.sh itself
# still runs under bash, which must be installed.

set -eu

REPO='datacas/code-cycle-toolkit'
VERSION="${CODE_CYCLE_VERSION:-latest}"
# Collect the installer's options in the positional parameters, dropping
# --version and its value: POSIX sh has no arrays.
count=$#
passthrough=0
while [ "$count" -gt 0 ]; do
  arg=$1
  shift
  count=$((count - 1))
  if [ "$arg" = '--version' ]; then
    [ "$count" -gt 0 ] || { printf '%s\n' '--version requires a value.' >&2; exit 2; }
    VERSION=$1
    shift
    count=$((count - 1))
  else
    set -- "$@" "$arg"
    passthrough=$((passthrough + 1))
  fi
done
if [ "$passthrough" -eq 0 ]; then
  set -- --agent all --scope global
fi
set -- "$@" --force

for tool in curl tar bash; do
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

bash "$src/scripts/install.sh" "$@"
printf 'Code Cycle Toolkit %s is installed.\n' "$ref"
