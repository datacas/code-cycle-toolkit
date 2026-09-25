#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd "$(dirname "$0")" && pwd -P)"
PACKAGE_ROOT="$(CDPATH='' cd "$SCRIPT_DIR/.." && pwd -P)"
USER_HOME="${HOME:-}"

if [ -z "$USER_HOME" ]; then
  printf '%s\n' 'Cannot determine the user home directory.' >&2
  exit 1
fi

AGENT='all'
SCOPE='global'
PROJECT_DIR="$(pwd -P)"
FORCE=0
RUNTIME=1

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options]

Install the Code Cycle skills by copying them to the selected agent paths.

Options:
  --agent <all|claude|codex|opencode>  Target host (default: all)
  --scope <global|project>            Install for the user or one project
  --project-dir <path>                Project root for project scope
  --force                             Replace existing installed skill folders
  --no-runtime                        Install the skills only, without the runtime
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
    --no-runtime)
      RUNTIME=0
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
    PROJECT_DIR="$(CDPATH='' cd "$PROJECT_DIR" && pwd -P)"
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
  # .agents/skills is the cross-agent path that Codex and OpenCode read. Codex
  # also reads ~/.codex/skills, so a second copy there lists every skill twice.
  copy_skills "$BASE_DIR/.agents/skills"
  if [ "$SCOPE" = 'global' ]; then
    remove_legacy_codex_skills
  fi
}

# Earlier versions also copied every skill to ~/.codex/skills. Only entries
# named after this toolkit's own skills are considered, and nothing is removed
# without --force. A link is refused rather than followed: ~/.codex/skills
# linked to ~/.agents/skills would otherwise delete the copy just installed.
# The refusal is a warning, not a failure, because the installation itself is
# already complete.
remove_legacy_codex_skills() {
  local legacy skill_dir target
  local found=()
  legacy="$BASE_DIR/.codex/skills"

  if [ -L "$BASE_DIR/.codex" ] || [ -L "$legacy" ] || [ "$legacy" -ef "$BASE_DIR/.agents/skills" ]; then
    printf 'Left %s alone: it is a link, so duplicates there are not removed.\n' "$legacy" >&2
    return 0
  fi
  [ -d "$legacy" ] || return 0

  for skill_dir in "$SKILLS_ROOT"/*; do
    [ -d "$skill_dir" ] || continue
    target="$legacy/${skill_dir##*/}"
    if [ -L "$target" ]; then
      printf 'Left %s alone: it is a link.\n' "$target" >&2
    elif [ -e "$target" ]; then
      found+=("$target")
    fi
  done
  [ "${#found[@]}" -gt 0 ] || return 0

  if [ "$FORCE" -ne 1 ]; then
    printf 'Warning: an earlier version installed these skills to %s too, so Codex lists them twice:\n' "$legacy" >&2
    printf '  %s\n' "${found[@]}" >&2
    printf 'Remove them with: rm -rf' >&2
    printf ' %q' "${found[@]}" >&2
    printf '\nor rerun this installer with --force.\n' >&2
    return 0
  fi

  for target in "${found[@]}"; do
    rm -rf "$target"
    printf 'Removed duplicate %s\n' "$target"
  done
}

install_opencode() {
  if [ "$SCOPE" = 'project' ]; then
    copy_skills "$BASE_DIR/.opencode/skills"
  else
    copy_skills "$BASE_DIR/.config/opencode/skills"
  fi
}

# The runtime is host-independent: one copy per scope, not one per agent. A
# skill is instructions and can be duplicated harmlessly; the runtime is code
# that records a cycle, and three copies of it would be three answers to the
# question of which one a run used.
RUNTIME_MANIFEST="$SCRIPT_DIR/runtime.manifest"

# The installer writes into a directory somebody else chose, which may be a
# repository it did not create. A symlink on any path it writes to is a request
# to modify a file outside that directory, so every one of them is refused
# rather than followed: an installation is not a reason to truncate a file
# nobody named.
refuse_symlink() {
  local path="$1" what="$2"
  if [ -L "$path" ]; then
    printf 'Refusing to write through a symlink (%s): %s\n' "$what" "$path" >&2
    return 1
  fi
}

install_runtime() {
  local root destination module source
  [ -f "$RUNTIME_MANIFEST" ] || {
    printf 'Runtime manifest not found: %s\n' "$RUNTIME_MANIFEST" >&2
    return 1
  }

  root="$BASE_DIR/.code-cycle"
  destination="$root/runtime"
  refuse_symlink "$root" 'installation directory' || return 1
  refuse_symlink "$destination" 'runtime directory' || return 1
  mkdir -p "$destination"

  install_gitignore "$root" || return 1

  while IFS= read -r module || [ -n "$module" ]; do
    case "$module" in
      ''|'#'*) continue ;;
    esac
    source="$SCRIPT_DIR/$module"
    [ -f "$source" ] || {
      printf 'Runtime module listed but missing: %s\n' "$source" >&2
      return 1
    }
    if [ -e "$destination/$module" ] || [ -L "$destination/$module" ]; then
      if [ "$FORCE" -ne 1 ]; then
        printf 'Already exists: %s (use --force to replace it)\n' "$destination/$module" >&2
        return 1
      fi
      # Remove rather than copy over: copying onto a symlink writes to whatever
      # it points at, which --force never authorised.
      rm -f "$destination/$module"
    fi
    cp "$source" "$destination/$module"
  done < "$RUNTIME_MANIFEST"

  printf 'Installed runtime -> %s\n' "$destination"
  printf 'Runtime installed at %s. run_cycle.py and cc-stats use it as-is; add it to PYTHONPATH only to import its modules from other code.\n' "$destination"
}

# The directory holds installed code, never anything a project should carry, so
# a fresh installation ignores it. An existing file is somebody's own rules and
# is left exactly as it is — not merged, not replaced by --force, which asks to
# replace this toolkit's files and not the target project's.
install_gitignore() {
  local root="$1" ignore
  ignore="$root/.gitignore"

  refuse_symlink "$ignore" 'ignore file' || return 1
  if [ -e "$ignore" ]; then
    printf 'Kept the existing %s; add "runtime/" to it to leave installed code uncommitted.\n' \
      "$ignore"
    return 0
  fi

  printf '%s\n' '*' > "$ignore"
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

if [ "$RUNTIME" -eq 1 ]; then
  install_runtime
fi

printf '%s\n' 'Code Cycle Toolkit installation completed.'
