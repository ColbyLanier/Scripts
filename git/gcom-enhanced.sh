#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME=${0##*/}
MODE="conventional"
MESSAGE=""
SKIP_STAGE=0
NO_AI=0
AUTO_ACCEPT=${GCOM_AUTO_ACCEPT:-0}
AI_CLIENT=${GCOM_AI_CLIENT:-codex}
MAX_DIFF_LINES=${GCOM_MAX_DIFF_LINES:-800}
CODEX_BIN=${GCOM_CODEX_BIN:-codex}
STAGE_FLAGS=()
STAGE_PATHS=()

usage() {
  cat <<'USAGE'
Enhanced git commit helper with AI support.

Usage: gcom-enhanced.sh [options] [message]

Options:
  -t, --twitter           Generate a <50 char Twitter-style subject.
  -d, --detailed          Generate a subject + wrapped body.
  -c, --conventional      Force Conventional Commits mode (default).
  -m, --message TEXT      Commit with the provided message (skip AI).
  --no-stage              Do not run git add automatically.
  --no-ai                 Skip AI generation and use heuristics.
  --provider {codex|cursor|claude}
                          Force a specific AI client.
  --max-lines N            Limit diff lines sent to AI (default: 800).
  -y, --yes               Auto-accept the first generated message.
  -h, --help              Show this help message.

Compatibility flags:
  --docs, --api, --<folder>
     Mirror the legacy gcom behavior: stage docs/ or app/<folder>/ before committing.

Examples:
  gcom-enhanced.sh                    # Stage all, propose Conventional commit
  gcom-enhanced.sh -t                 # Stage all, propose Twitter-style commit
  gcom-enhanced.sh --docs -d          # Stage docs/, propose detailed commit
  gcom-enhanced.sh -m "fix: handle nil"  # Stage all, commit with manual message
USAGE
}

log() {
  printf '%s\n' "$*"
}

warn() {
  printf '⚠️  %s\n' "$*" >&2
}

info() {
  printf '👉 %s\n' "$*"
}

abort() {
  warn "$1"
  exit "${2:-1}"
}

ensure_git_repo() {
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    abort "Not inside a git repository."
  fi
}

resolve_stage_path() {
  local token="$1"
  if [[ "$token" == "." || -z "$token" ]]; then
    echo "."
    return
  fi
  if [[ "$token" == "docs" ]]; then
    echo "docs/"
    return
  fi
  if [[ -d "app/$token" ]]; then
    echo "app/$token/"
    return
  fi
  echo "$token"
}

stage_changes() {
  local targets=()
  if (( SKIP_STAGE )); then
    return
  fi
  if ((${#STAGE_FLAGS[@]} == 0)); then
    targets=(".")
  else
    for flag in "${STAGE_FLAGS[@]}"; do
      targets+=("$(resolve_stage_path "$flag")")
    done
  fi
  STAGE_PATHS=("${targets[@]}")
  for path in "${targets[@]}"; do
    info "Staging ${path}"
    git add -- "$path"
  done
}

print_summary() {
  info "Staged status"
  git status -sb
  echo
  info "Diff summary"
  git diff --cached --stat --color=never || true
}

collect_diff() {
  local diff_file truncated_file
  diff_file=$(mktemp)
  truncated_file=$(mktemp)
  git diff --cached --color=never >"$diff_file"
  if [[ ! -s "$diff_file" ]]; then
    rm -f "$diff_file" "$truncated_file"
    abort "No staged changes found. Stage files before committing."
  fi
  if (( MAX_DIFF_LINES > 0 )); then
    head -n "$MAX_DIFF_LINES" "$diff_file" >"$truncated_file"
  else
    cp "$diff_file" "$truncated_file"
  fi
  echo "$diff_file|$truncated_file"
}

list_staged_files() {
  git diff --cached --name-only
}

build_prompt() {
  local diff_snippet="$1"
  local files="$2"
  local truncated_flag="$3"
  local repo_name
  repo_name=$(basename "$(git rev-parse --show-toplevel)")
  local mode_instructions
  case "$MODE" in
    twitter)
      mode_instructions="Produce exactly one Conventional Commit style subject under 50 characters. Keep the imperative tone and skip a body."
      ;;
    detailed)
      mode_instructions="Produce a Conventional Commits subject followed by a blank line and a wrapped body (bullets or short paragraphs) explaining what changed and why."
      ;;
    *)
      mode_instructions="Produce exactly one Conventional Commits subject line. Add a short optional body only if it adds clear value."
      ;;
  esac

  cat <<PROMPT
You are an elite software engineer who writes precise git commit messages.
Repository: ${repo_name}

Rules:
- Follow Conventional Commits types: feat, fix, docs, style, refactor, test, chore.
- Include a scope when obvious from filenames.
- Subject: imperative mood, <= 72 chars.
- ${mode_instructions}
- Never wrap in quotes or code fences.
${truncated_flag}

Staged files:
${files}

Staged diff:
${diff_snippet}
PROMPT
}

codex_available() {
  command -v "$CODEX_BIN" >/dev/null 2>&1
}

call_codex() {
  local prompt="$1"
  local tmp_json
  tmp_json=$(mktemp)
  local cmd=("$CODEX_BIN" exec --skip-git-repo-check --json)
  if [[ -n "${GCOM_CODEX_MODEL:-}" ]]; then
    cmd+=(--model "${GCOM_CODEX_MODEL}")
  fi
  if [[ -n "${GCOM_CODEX_PROFILE:-}" ]]; then
    cmd+=(--profile "${GCOM_CODEX_PROFILE}")
  fi
  if [[ -n "${GCOM_CODEX_EXTRA_ARGS:-}" ]]; then
    # shellcheck disable=SC2206
    local extra_args=(${GCOM_CODEX_EXTRA_ARGS})
    cmd+=("${extra_args[@]}")
  fi
  if ! "${cmd[@]}" "$prompt" >"$tmp_json"; then
    rm -f "$tmp_json"
    return 1
  fi
  if ! python3 - "$tmp_json" <<'PY'
import json, sys, pathlib
path = pathlib.Path(sys.argv[1])
text = None
for raw in path.read_text(encoding="utf-8").splitlines():
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        continue
    if data.get("type") == "item.completed":
        item = data.get("item") or {}
        if item.get("type") == "agent_message":
            candidate = (item.get("text") or "").strip()
            if candidate:
                text = candidate
if not text:
    sys.exit(1)
print(text)
PY
  then
    rm -f "$tmp_json"
    return 1
  fi
  rm -f "$tmp_json"
}

select_ai_client() {
  local preferred="$1"
  case "$preferred" in
    codex)
      if codex_available; then
        echo "codex"
        return 0
      fi
      return 1
      ;;
    cursor|cursor-agent)
      if command -v cursor-agent >/dev/null 2>&1; then
        echo "cursor-agent"
        return 0
      fi
      return 1
      ;;
    claude)
      if command -v claude >/dev/null 2>&1; then
        echo "claude"
        return 0
      fi
      return 1
      ;;
  esac
  if codex_available; then
    echo "codex"
    return 0
  fi
  if command -v cursor-agent >/dev/null 2>&1; then
    echo "cursor-agent"
    return 0
  fi
  if command -v claude >/dev/null 2>&1; then
    echo "claude"
    return 0
  fi
  return 1
}

call_ai() {
  local client="$1"
  local prompt="$2"
  case "$client" in
    codex)
      call_codex "$prompt"
      ;;
    cursor-agent)
      local args=(cursor-agent --print --output-format text)
      if [[ -n "${GCOM_CURSOR_MODEL:-}" ]]; then
        args+=(--model "${GCOM_CURSOR_MODEL}")
      fi
      "${args[@]}" "$prompt"
      ;;
    claude)
      local args=(claude --print --output-format text)
      if [[ "${GCOM_CLAUDE_SKIP_PERMISSIONS:-1}" -eq 1 ]]; then
        args+=(--dangerously-skip-permissions)
      fi
      if [[ -n "${GCOM_CLAUDE_MODEL:-}" ]]; then
        args+=(--model "${GCOM_CLAUDE_MODEL}")
      fi
      "${args[@]}" "$prompt"
      ;;
    *)
      return 1
      ;;
  esac
}

sanitize_message() {
  python3 - "$1" <<'PY'
import re, sys
if len(sys.argv) < 2:
    sys.exit(1)
raw = sys.argv[1]
raw = raw.replace('\r', '').strip()
raw = re.sub(r'```.*?```', '', raw, flags=re.S)
lines = []
for line in raw.splitlines():
    cleaned = line.strip()
    if cleaned.lower().startswith('commit message'):
        cleaned = cleaned.split(':', 1)[-1].strip() or cleaned
    if cleaned == '```':
        continue
    lines.append(cleaned if cleaned else '')
text = '\n'.join(lines).strip()
if not text:
    sys.exit(1)
print(text)
PY
}

fallback_message() {
  local mode="$MODE"
  mapfile -t files < <(git diff --cached --name-only | head -n 5)
  local primary="${files[0]:-changes}"
  local scope=""
  local type="chore"
  local lower
  lower=$(printf '%s\n' "${files[@]}" | tr '[:upper:]' '[:lower:]')
  if grep -q '^docs/' <<<"$lower"; then
    type="docs"
  elif grep -q 'test' <<<"$lower"; then
    type="test"
  elif grep -q 'fix' <<<"$lower"; then
    type="fix"
  elif grep -q 'feat' <<<"$lower"; then
    type="feat"
  fi
  if [[ "$primary" == app/*/* ]]; then
    scope="$(cut -d'/' -f2 <<<"$primary")"
  elif [[ "$primary" == docs/* ]]; then
    scope="docs"
  fi
  local subject="${type}${scope:+(${scope})}: update ${primary##*/}"
  if [[ "$mode" == "twitter" ]]; then
    python3 - "$subject" <<'PY'
import sys
if len(sys.argv) < 2:
    sys.exit(0)
text = sys.argv[1].strip()
limit = 48
if len(text) <= limit:
    print(text)
else:
    print(text[:limit-1] + '…')
PY
    return
  fi
  if [[ "$mode" == "detailed" ]]; then
    printf '%s\n\n' "$subject"
    for file in "${files[@]}"; do
      printf ' - update %s\n' "$file"
    done
    return
  fi
  printf '%s\n' "$subject"
}

resolve_editor_cmd() {
  if [[ -n "${GCOM_EDITOR_CMD:-}" ]]; then
    echo "${GCOM_EDITOR_CMD}"
    return
  fi
  if [[ -n "${VISUAL:-}" ]]; then
    echo "${VISUAL}"
    return
  fi
  if [[ -n "${EDITOR:-}" ]]; then
    echo "${EDITOR}"
    return
  fi
  local git_editor
  git_editor=$(git config core.editor 2>/dev/null || git var GIT_EDITOR 2>/dev/null || true)
  if [[ -n "$git_editor" && "$git_editor" != "editor" ]]; then
    echo "$git_editor"
    return
  fi
  if command -v nano >/dev/null 2>&1; then
    echo "vim"
    return
  fi
  if command -v vim >/dev/null 2>&1; then
    echo "vim"
    return
  fi
  if command -v vi >/dev/null 2>&1; then
    echo "vi"
    return
  fi
  echo "ed"
}

run_editor_command() {
  local editor_cmd="$1"
  local file="$2"
  local escaped_file
  printf -v escaped_file "%q" "$file"
  bash -lc "$editor_cmd $escaped_file"
}

inline_edit_message() {
  local initial="$1"
  >&2 echo "Current message: $initial"
  read -r -p "New one-line subject (blank keeps current): " edited || return 1
  if [[ -z "$edited" ]]; then
    printf '%s' "$initial"
  else
    printf '%s' "$edited"
  fi
}

editor_edit_message() {
  local initial="$1"
  local file editor_cmd
  editor_cmd=$(resolve_editor_cmd)
  file=$(mktemp)
  printf '%s\n' "$initial" >"$file"
  {
    printf 'Opening editor command: %s\n' "$editor_cmd"
    if [[ "$editor_cmd" == nano* ]]; then
      echo "nano shortcuts: Ctrl+O to save, Ctrl+X to exit."
    fi
  } >&2
  if ! run_editor_command "$editor_cmd" "$file"; then
    rm -f "$file"
    return 1
  fi
  cat "$file"
  rm -f "$file"
}

prompt_loop() {
  local message="$1" prompt
  while true; do
    >&2 echo "---------------- Commit Message ----------------"
    >&2 echo "$message"
    >&2 echo "------------------------------------------------"
    if (( AUTO_ACCEPT )); then
      >&2 echo "Auto-accept enabled; proceeding with the above message."
      printf '%s' "$message"
      return
    fi
    read -rp "Accept, inline edit, open editor, regenerate, or abort? [a/e/v/r/q] " prompt
    case "${prompt,,}" in
      a|"")
        printf '%s' "$message"
        return
        ;;
      e)
        local edited
        if ! edited=$(inline_edit_message "$message"); then
          continue
        fi
        message="$edited"
        ;;
      v)
        local edited
        if ! edited=$(editor_edit_message "$message"); then
          continue
        fi
        message="$edited"
        ;;
      r)
        message=""
        printf '%s' "__REGENERATE__"
        return
        ;;
      q)
        abort "Commit aborted by user." 0
        ;;
      *)
        echo "Please answer a, e, r, or q."
        ;;
    esac
  done
}

commit_with_message() {
  local message="$1"
  local tmp
  tmp=$(mktemp)
  printf '%s\n' "$message" >"$tmp"
  git commit -F "$tmp"
  rm -f "$tmp"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -t|--twitter)
        MODE="twitter"
        shift
        ;;
      -d|--detailed)
        MODE="detailed"
        shift
        ;;
      -c|--conventional)
        MODE="conventional"
        shift
        ;;
      -m|--message)
        [[ $# -ge 2 ]] || abort "--message requires text"
        MESSAGE="$2"
        shift 2
        ;;
      --no-stage)
        SKIP_STAGE=1
        shift
        ;;
      --no-ai)
        NO_AI=1
        shift
        ;;
      --provider)
        [[ $# -ge 2 ]] || abort "--provider needs a value"
        AI_CLIENT="$2"
        shift 2
        ;;
      --max-lines)
        [[ $# -ge 2 ]] || abort "--max-lines needs a value"
        MAX_DIFF_LINES="$2"
        shift 2
        ;;
      -y|--yes)
        AUTO_ACCEPT=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        if [[ $# -gt 0 ]]; then
          if [[ -z "$MESSAGE" ]]; then
            MESSAGE="$*"
            break
          fi
        fi
        ;;
      --*)
        STAGE_FLAGS+=("${1#--}")
        shift
        ;;
      *)
        if [[ -z "$MESSAGE" ]]; then
          MESSAGE="$1"
        else
          MESSAGE+=" $1"
        fi
        shift
        ;;
    esac
  done
}

main() {
  parse_args "$@"
  ensure_git_repo
  stage_changes
  if git diff --cached --quiet; then
    abort "No staged changes after staging step."
  fi
  print_summary
  if [[ -n "$MESSAGE" ]]; then
    info "Using provided message."
    commit_with_message "$MESSAGE"
    exit 0
  fi
  local diff_paths diff_file truncated_file truncated_note diff_snippet files prompt ai_output sanitized message loop_result client
  diff_paths=$(collect_diff)
  diff_file=${diff_paths%%|*}
  truncated_file=${diff_paths##*|}
  trap '[[ -n "${diff_file:-}" ]] && rm -f "$diff_file"; [[ -n "${truncated_file:-}" ]] && rm -f "$truncated_file"' EXIT
  if [[ $(wc -l <"$diff_file") -gt $(wc -l <"$truncated_file") ]]; then
    truncated_note="(Diff truncated to ${MAX_DIFF_LINES} lines to keep prompts fast.)"
  else
    truncated_note=""
  fi
  diff_snippet=$(cat "$truncated_file")
  files=$(list_staged_files)
  prompt=$(build_prompt "$diff_snippet" "$files" "$truncated_note")

  while true; do
    if (( NO_AI )); then
      warn "AI disabled; using fallback heuristics."
      message=$(fallback_message)
    else
      if ! client=$(select_ai_client "$AI_CLIENT"); then
        warn "No AI client available; using fallback."
        message=$(fallback_message)
      else
        info "Calling AI (${client}) for commit suggestion..."
        if ! ai_output=$(call_ai "$client" "$prompt"); then
          warn "AI client unavailable or failed; using fallback."
          message=$(fallback_message)
        else
          if ! sanitized=$(sanitize_message "$ai_output" 2>/dev/null); then
            warn "Unable to sanitize AI response; using fallback."
            message=$(fallback_message)
          else
            message="$sanitized"
          fi
        fi
      fi
    fi
    loop_result=$(prompt_loop "$message") || true
    if [[ "$loop_result" == "__REGENERATE__" ]]; then
      continue
    fi
    message="$loop_result"
    break
  done
  info "Committing with the approved message."
  commit_with_message "$message"
}

main "$@"
