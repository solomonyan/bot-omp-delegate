#!/usr/bin/env bash
# smoke-skills.sh — after a delegate upgrade, check that omp, Codex, and Kimi
# can see poteto-mode, interrogate, and setup-pstack.
#
# For each backend this script:
#   (a) resolves SKILL.md in that backend's skills directories
#   (b) starts a throwaway lane and asks the model to READ those paths and
#       reply with each file's YAML frontmatter name
#
# Discovery (do not assume a list-skills reply: Kimi hides user-invoked skills
# such as poteto-mode, which has disable-model-invocation: true):
#   omp   — ~/.agents/skills (skills.enableAgentsUser)
#   codex — ~/.codex/skills
#   kimi  — ~/.kimi-code/skills
#
# Usage: scripts/smoke-skills.sh
# Env:   OMP_DELEGATE_BIN, SMOKE_SKILLS_LABEL (default omp-caller:friday),
#        SMOKE_SKILLS_MAX_WAIT_SEC (default 900), SMOKE_SKILLS_POLL_SEC (default 10),
#        OMP_COORDINATOR_SUBSCRIBER_ID (optional; omitted by default so the
#        coordinator is not notified).
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${OMP_DELEGATE_BIN:-$ROOT/scripts/omp-delegate}"
LABEL="${SMOKE_SKILLS_LABEL:-omp-caller:friday}"
MAX_WAIT_SEC="${SMOKE_SKILLS_MAX_WAIT_SEC:-900}"
POLL_SEC="${SMOKE_SKILLS_POLL_SEC:-10}"
COORD="${OMP_COORDINATOR_SUBSCRIBER_ID:-}"

SKILL_NAMES="poteto-mode interrogate setup-pstack"
BACKENDS="omp codex kimi"

json_get() {
  # usage: json_get FILE_OR_STDIN key
  python3 -c '
import json, sys
raw = sys.stdin.read() if sys.argv[1] == "-" else open(sys.argv[1], encoding="utf-8").read()
key = sys.argv[2]
obj = None
for line in raw.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        cand = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(cand, dict):
        obj = cand
        if key in cand and cand.get(key):
            break
if obj is None:
    # maybe a single non-JSONL document
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(1)
val = obj.get(key, "")
if val is None:
    val = ""
elif not isinstance(val, str):
    val = json.dumps(val, ensure_ascii=False)
sys.stdout.write(val)
' "$1" "$2"
}

skill_dir_for() {
  case "$1" in
    omp) printf '%s\n' "$HOME/.agents/skills" ;;
    codex) printf '%s\n' "$HOME/.codex/skills" ;;
    kimi) printf '%s\n' "$HOME/.kimi-code/skills" ;;
    *) return 1 ;;
  esac
}

# Collect resolved SKILL.md paths for one backend. Prints "ok\tpath" or "missing\tname".
resolve_skills() {
  local backend="$1" dir name path
  dir="$(skill_dir_for "$backend")"
  for name in $SKILL_NAMES; do
    path="$dir/$name/SKILL.md"
    if [ -f "$path" ] || [ -L "$path" ]; then
      # Resolve the symlink target when possible, but keep a usable path either way.
      if [ -e "$path" ]; then
        python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$path"
      else
        printf 'MISSING:%s\n' "$name"
      fi
    else
      printf 'MISSING:%s\n' "$name"
    fi
  done
}

LANES_TO_DELETE=""
SCRATCH_TO_DELETE=""
cleanup() {
  local id dir
  for id in $LANES_TO_DELETE; do
    if [ -n "$id" ]; then
      # Idle workers must be closed before the registration can be removed (HTTP 409 otherwise).
      "$BIN" lane close "$id" >/dev/null 2>&1 || true
      "$BIN" lane delete "$id" >/dev/null 2>&1 || true
    fi
  done
  for dir in $SCRATCH_TO_DELETE; do
    if [ -n "$dir" ] && [ -d "$dir" ]; then
      rm -rf "$dir"
    fi
  done
}
trap cleanup EXIT

FAILED=0

if [ ! -x "$BIN" ]; then
  echo "FAIL: delegate binary not executable: $BIN" >&2
  exit 1
fi

for backend in $BACKENDS; do
  echo "=== $backend ==="
  dir="$(skill_dir_for "$backend")"
  echo "skills dir: $dir"

  resolved=""
  missing=""
  while IFS= read -r line; do
    case "$line" in
      MISSING:*)
        missing="$missing ${line#MISSING:}"
        ;;
      *)
        if [ -n "$line" ]; then
          if [ -n "$resolved" ]; then
            resolved="$resolved
$line"
          else
            resolved="$line"
          fi
        fi
        ;;
    esac
  done <<EOF
$(resolve_skills "$backend")
EOF

  if [ -n "$missing" ]; then
    echo "FAIL $backend: SKILL.md not found for:${missing} (looked in $dir)"
    FAILED=1
    continue
  fi
  echo "resolved:"
  printf '%s\n' "$resolved"

  scratch="$(mktemp -d "${TMPDIR:-/tmp}/smoke-skills.${backend}.XXXXXX")"
  SCRATCH_TO_DELETE="$SCRATCH_TO_DELETE $scratch"
  printf 'smoke-skills scratch for %s\n' "$backend" > "$scratch/README.txt"

  lane_name="smoke-skills-${backend}-$$-$(date +%H%M%S)"
  create_out=""
  if ! create_out="$("$BIN" lane create --name "$lane_name" --cwd "$scratch" --backend "$backend" 2>/tmp/smoke-skills-create-err.$$)"; then
    echo "FAIL $backend: lane create failed: $(tr '\n' ' ' </tmp/smoke-skills-create-err.$$)"
    rm -f /tmp/smoke-skills-create-err.$$
    FAILED=1
    continue
  fi
  rm -f /tmp/smoke-skills-create-err.$$
  lane_id="$(printf '%s' "$create_out" | json_get - id || true)"
  if [ -z "$lane_id" ]; then
    lane_id="$(printf '%s' "$create_out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("lane") or {}).get("id",""))' || true)"
  fi
  if [ -z "$lane_id" ]; then
    echo "FAIL $backend: lane create returned no id: $create_out"
    FAILED=1
    continue
  fi
  LANES_TO_DELETE="$LANES_TO_DELETE $lane_id"
  echo "lane: $lane_name ($lane_id)"

  prompt="Read each of these SKILL.md files and reply with the YAML frontmatter name from each file, one name per line (expected: poteto-mode, interrogate, setup-pstack). Do not start any other work."
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    prompt="$prompt
$path"
  done <<EOF
$resolved
EOF

  run_args=(lane run --detach --label "$LABEL")
  if [ -n "$COORD" ]; then
    run_args+=(--coordinator-subscriber-id "$COORD")
  fi
  run_out=""
  if ! run_out="$("$BIN" "${run_args[@]}" "$lane_id" "$prompt" 2>/tmp/smoke-skills-run-err.$$)"; then
    echo "FAIL $backend: lane run failed: $(tr '\n' ' ' </tmp/smoke-skills-run-err.$$)"
    rm -f /tmp/smoke-skills-run-err.$$
    FAILED=1
    continue
  fi
  rm -f /tmp/smoke-skills-run-err.$$
  job_id="$(printf '%s' "$run_out" | json_get - id || true)"
  if [ -z "$job_id" ]; then
    echo "FAIL $backend: lane run returned no job id: $run_out"
    FAILED=1
    continue
  fi
  echo "job: $job_id"

  elapsed=0
  status=""
  snap_file="$(mktemp "${TMPDIR:-/tmp}/smoke-skills-snap.XXXXXX")"
  while :; do
    if ! "$BIN" read "$job_id" >"$snap_file" 2>/tmp/smoke-skills-read-err.$$; then
      echo "FAIL $backend: read failed: $(tr '\n' ' ' </tmp/smoke-skills-read-err.$$)"
      rm -f /tmp/smoke-skills-read-err.$$ "$snap_file"
      FAILED=1
      status="read-failed"
      break
    fi
    rm -f /tmp/smoke-skills-read-err.$$
    status="$(json_get "$snap_file" status || true)"
    case "$status" in
      completed|aborted|error|interrupted)
        break
        ;;
      waiting_input)
        echo "FAIL $backend: job $job_id waiting_input (not answering)"
        FAILED=1
        break
        ;;
    esac
    if [ "$elapsed" -ge "$MAX_WAIT_SEC" ]; then
      echo "FAIL $backend: job $job_id still '$status' after ${MAX_WAIT_SEC}s"
      FAILED=1
      status="timeout"
      break
    fi
    sleep "$POLL_SEC"
    elapsed=$((elapsed + POLL_SEC))
  done

  if [ "$status" != "completed" ]; then
    if [ "$status" != "waiting_input" ] && [ "$status" != "timeout" ] && [ "$status" != "read-failed" ]; then
      err="$(json_get "$snap_file" error || true)"
      echo "FAIL $backend: job $job_id status=$status ${err:+error=$err}"
      FAILED=1
    fi
    rm -f "$snap_file"
    continue
  fi

  message="$(json_get "$snap_file" finalMessage || true)"
  rm -f "$snap_file"
  excerpt="$(printf '%s' "$message" | python3 -c 'import sys; t=sys.stdin.read().strip().replace("\n"," | "); print(t[:400])')"
  echo "reply: $excerpt"

  lc="$(printf '%s' "$message" | tr '[:upper:]' '[:lower:]')"
  missing_names=""
  for name in $SKILL_NAMES; do
    case "$lc" in
      *"$name"*) ;;
      *) missing_names="$missing_names $name" ;;
    esac
  done
  if [ -n "$missing_names" ]; then
    echo "FAIL $backend: completed but frontmatter names missing:${missing_names}"
    FAILED=1
    continue
  fi
  echo "PASS $backend (${elapsed}s)"
done

if [ "$FAILED" -ne 0 ]; then
  echo "smoke-skills: FAIL"
  exit 1
fi
echo "smoke-skills: PASS"
exit 0
