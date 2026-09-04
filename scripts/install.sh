#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd -P)"
PACKAGE_ROOT="$(CDPATH= cd "$SCRIPT_DIR/.." && pwd -P)"
USER_HOME="${HOME:-}"

if [ -z "$USER_HOME" ]; then
  printf '%s\n' 'Cannot determine the user home directory.' >&2
  exit 1
fi

AGENT='all'
SCOPE='global'
PROJECT_DIR="$(pwd -P)"
FORCE=0

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options]

Install the Code Cycle skills by copying them to the selected agent paths.

Options:
  --agent <all|claude|codex|opencode>  Target host (default: all)
  --scope <global|project>            Install for the user or one project
  --project-dir <path>                Project root for project scope
  --force                             Replace existing installed skill folders
  -h, --help                          Show this help

Examples:
  bash scripts/install.sh --agent all --scope global
  bash scripts/install.sh --agent opencode --scope project --project-dir .
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --agent)
      [ "$#" -ge 2 ] || { printf '%s\n' '--agent requires a value.' >&2; exit 2; }
      AGENT="$2"
      shift 2
      ;;
    --scope)
      [ "$#" -ge 2 ] || { printf '%s\n' '--scope requires a value.' >&2; exit 2; }
      SCOPE="$2"
      shift 2
      ;;
    --project-dir)
      [ "$#" -ge 2 ] || { printf '%s\n' '--project-dir requires a value.' >&2; exit 2; }
      PROJECT_DIR="$2"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$AGENT" in
  all|claude|codex|opencode) ;;
  *) printf 'Invalid agent: %s\n' "$AGENT" >&2; exit 2 ;;
esac

case "$SCOPE" in
  global) BASE_DIR="$USER_HOME" ;;
  project)
    [ -d "$PROJECT_DIR" ] || { printf 'Project directory does not exist: %s\n' "$PROJECT_DIR" >&2; exit 2; }
    PROJECT_DIR="$(CDPATH= cd "$PROJECT_DIR" && pwd -P)"
    [ "$PROJECT_DIR" != '/' ] || { printf '%s\n' 'Refusing to use / as a project directory.' >&2; exit 2; }
    BASE_DIR="$PROJECT_DIR"
    ;;
  *) printf 'Invalid scope: %s\n' "$SCOPE" >&2; exit 2 ;;
esac

SKILLS_ROOT="$PACKAGE_ROOT/skills"
[ -d "$SKILLS_ROOT" ] || { printf 'Skills directory not found: %s\n' "$SKILLS_ROOT" >&2; exit 1; }

copy_skills() {
  local destination skill_dir skill_name target
  destination="$1"
  mkdir -p "$destination"

  for skill_dir in "$SKILLS_ROOT"/*; do
    [ -d "$skill_dir" ] || continue
    skill_name="${skill_dir##*/}"
    target="$destination/$skill_name"

    if [ -e "$target" ] || [ -L "$target" ]; then
      if [ "$FORCE" -ne 1 ]; then
        printf 'Already exists: %s (use --force to replace it)\n' "$target" >&2
        return 1
      fi
      rm -rf "$target"
    fi

    cp -R "$skill_dir" "$target"
    printf 'Installed %s -> %s\n' "$skill_name" "$target"
  done
}

install_claude() {
  copy_skills "$BASE_DIR/.claude/skills"
}

install_codex() {
  # .agents/skills is the cross-agent compatibility path. .codex/skills is
  # retained for Codex installations that use the traditional home path.
  copy_skills "$BASE_DIR/.agents/skills"
  if [ "$SCOPE" = 'global' ]; then
    copy_skills "$BASE_DIR/.codex/skills"
  fi
}

install_opencode() {
  if [ "$SCOPE" = 'project' ]; then
    copy_skills "$BASE_DIR/.opencode/skills"
  else
    copy_skills "$BASE_DIR/.config/opencode/skills"
  fi
}

case "$AGENT" in
  claude) install_claude ;;
  codex) install_codex ;;
  opencode) install_opencode ;;
  all)
    install_claude
    install_codex
    install_opencode
    ;;
esac

printf '%s\n' 'Code Cycle Toolkit installation completed.'
