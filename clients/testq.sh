# testq client for bash harnesses. Ask before you launch an engine.
#
# The canonical copy of this file lives in the testq repository
# (`clients/testq.sh`). Projects VENDOR it rather than sourcing it across an
# absolute path, so a checkout stays self-contained and still runs on a machine
# that has never heard of testq -- where every call here turns into a warning
# and the work goes ahead unqueued. Copy it in; if you change it, change it
# here first.
#
# Usage, from a run script that launches Godot:
#
#     . "$(dirname "${BASH_SOURCE[0]}")/testq.sh"
#     trap 'testq_release $?' EXIT
#     testq_acquire --arg "$WHICH" --cpu 1 --engines 1
#     timeout 1500 "$GODOT" --headless --path "$PROJ" -- --test="$WHICH"
#
# The one rule: acquire BEFORE any engine starts, including an import pass,
# and release from an EXIT trap. Wrap each ENGINE in its own `timeout`, never
# the script: time spent queued is then charged to nothing, and a job that
# waits an hour still gets its full budget.
#
#     testq_acquire --arg X [--cpu N] [--min-cpu M] [--gpu N] [--engines N]
#                   [--exclusive] [--mutex KEY]... [--size N] [--max SECONDS]
#                   [--idle-ok] [--project NAME] [--script NAME]
#
#   --cpu       slots to book (default 1). More than the box means all of it.
#   --min-cpu   a FLEXIBLE job: --cpu is what it wants, --min-cpu what it can
#               run on, and the daemon may grant anything between when starting
#               narrow answers sooner. Only for a job whose engine count
#               follows the width it is granted: read $TESTQ_SLOTS back and
#               launch that many.
#   --gpu       1 if it opens a window (two fit at once); "$(testq_box_gpus)"
#               for the card to itself -- a frame time you mean to quote.
#   --engines   engines alive at once, which is not always the slots booked:
#               the daemon needs it to tell its own engines from strangers'.
#   --exclusive the box to itself, for a measurement between live processes.
#   --mutex     a named exclusion, e.g. "clip:$TREE_ID:$SCENARIO". Repeatable.
#   --size      how much work this is, in whatever the job counts in, so the
#               estimate scales with it instead of with the arg's name.
#   --max       the longest this may run once it starts. The daemon kills it
#               past that, if anybody is waiting. (It is a backstop: your own
#               `timeout` round the engine is what should fire first.)
#   --idle-ok   a window that is meant to sit there; not to be taken for a hang.
#   --project   key into the daemon's projects.json, for estimates and `reap`.
#   --script    what to call this on the page (default: the script's own name).
#
# Afterwards:
#   $TESTQ_SLOTS        the width granted. Empty when nobody said (unqueued, or
#                       TESTQ=off): use what you asked for.
#   $TESTQ_BOX_ENGINES  engines this box is committed to carrying while you
#                       run, yours included. Exported, so the engine can read
#                       it. A test timing anything against the wall clock
#                       should assert only at 1, and must treat 0 -- nobody
#                       knows -- as "assert anyway".
#   testq_box_slots, testq_box_gpus   the size of the box, asked of the daemon.
#
# Environment:
#   TESTQ=on|off|require   off bypasses the queue; require fails rather than
#                          run unqueued, for a number you intend to quote
#   TESTQ_PORT             default 43117
#   TESTQ_AUTOSTART=0      never start a daemon that is not running
#   TESTQ_PY               path to testq.py, if it is not in the usual place
#   TESTQ_TREE             the checkout this runs in (default: $PROJ if the
#                          sourcing script set one, else the directory of the
#                          script that was run)
#
# Needs curl, sed and md5sum, which Git-Bash has.

TESTQ="${TESTQ:-on}"
TESTQ_PORT="${TESTQ_PORT:-43117}"
TESTQ_AUTOSTART="${TESTQ_AUTOSTART:-1}"
TESTQ_URL="http://127.0.0.1:$TESTQ_PORT"
TESTQ_PROTO=1
_TESTQ_LEASE=""
export TESTQ_BOX_ENGINES="${TESTQ_BOX_ENGINES:-0}"

# The path as GODOT writes it -- C:/Users/..., forward slashes -- because that
# is what the daemon matches engines' `--path` against, and what the tree id
# below is a hash of. The Node client and the daemon's `reap` compute the same
# id from the same spelling; all three have to agree.
_testq_native() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s' "$1"
  fi
}

_testq_tree() {
  local dir="${TESTQ_TREE:-${PROJ:-}}"
  [ -n "$dir" ] || dir="$(cd "$(dirname "$0")" && pwd)"
  _testq_native "$dir" | sed 's:/*$::'
}

_testq_tree_id() {
  local path="$1"
  printf '%s-%s' "$(basename "$path")" "$(printf '%s' "$path" | md5sum | cut -c1-6)"
}

# Where the daemon's own source lives, which is only ever needed to START one.
# Walked for, because a worktree sits several directories below the project
# and both should find the same <projects>/_tools/testq.
_testq_py() {
  if [ -n "${TESTQ_PY:-}" ]; then
    [ -f "$TESTQ_PY" ] && printf '%s' "$TESTQ_PY"
    return
  fi
  local dir i=0
  dir="$(_testq_tree)"
  while [ "$i" -lt 8 ]; do
    if [ -f "$dir/_tools/testq/testq.py" ]; then
      printf '%s' "$dir/_tools/testq/testq.py"
      return
    fi
    case "$dir" in ""|"/"|"."|[A-Za-z]:|[A-Za-z]:/) break ;; esac
    dir="$(dirname "$dir")"
    i=$((i + 1))
  done
}

_testq_up() { curl -fsS --max-time 2 "$TESTQ_URL/healthz" >/dev/null 2>&1; }

_testq_post() { # _testq_post <path> <json> [timeout]
  curl -fsS --max-time "${3:-30}" -X POST "$TESTQ_URL/$1" \
    -H 'Content-Type: application/json' -d "$2" 2>/dev/null
}

# Bash's own $$ is an MSYS pid that no Windows API has heard of, so a daemon
# told $$ could never tell whether this shell was alive and would reclaim its
# slots mid-run. /proc/<pid>/winpid is Git-for-Windows' translation.
_testq_winpid() { cat "/proc/$$/winpid" 2>/dev/null || echo 0; }

_testq_num() { # _testq_num <json> <field>: a number, or nothing
  printf '%s' "$1" | sed -n 's/.*"'"$2"'"[: ]*\([0-9][0-9.]*\).*/\1/p'
}

_testq_str() { # _testq_str <json> <field>: a string with no quote in it
  printf '%s' "$1" | sed -n 's/.*"'"$2"'"[: ]*"\([^"]*\)".*/\1/p'
}

# Whether a reply says yes to <field>: `"field": true`, or 1. Asked of the
# field itself. The first of these clients asked whether "granted" was followed
# ANYWHERE by "true", and the day the queued reply grew a second yes-or-no
# field, queued jobs with `"granted": false, ... "start_in_floor": true` took
# themselves for granted and ran. The daemon now keeps `true` out of a queued
# reply for the sake of the copies of that client still out there; this one
# does not need it to.
_testq_is() { # _testq_is <json> <field>
  case "$1" in
    *"\"$2\": true"*|*"\"$2\":true"*|*"\"$2\": 1"[,}]*|*"\"$2\":1"[,}]*) return 0 ;;
  esac
  return 1
}

_testq_json() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

_testq_span() {
  local s="${1%.*}"
  if [ "${s:-0}" -lt 90 ]; then printf '%ss' "${s:-0}"; else printf '%sm' $(( (s + 30) / 60 )); fi
}

# "starts in ~12m", or nothing when the daemon cannot say: the job is waiting
# on a mutex or on engines the queue did not start, and nobody knows when
# those end.
_testq_starts() {
  local s
  s="$(_testq_num "$1" start_in_s)"
  [ -n "$s" ] || return 0
  if _testq_is "$1" start_in_floor; then
    printf 'starts in at least %s' "$(_testq_span "$s")"
  else
    printf 'starts in ~%s' "$(_testq_span "$s")"
  fi
}

# How many CPU slots the whole box is, asked rather than written down: it was
# written down once, as four, and the daemon went to eight. 8 when nobody
# answers, which is what a daemon about to be autostarted will say.
testq_box_slots() {
  local n=""
  [ "$TESTQ" != "off" ] && n="$(curl -fsS --max-time 2 "$TESTQ_URL/state" 2>/dev/null \
    | sed -n 's/.*"capacity"[: ]*{"cpu"[: ]*\([0-9]*\).*/\1/p')"
  [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null || n=8
  printf '%s' "$n"
}

# The same question about the GPU: how many windows fit on the card at once.
# A run whose frame times are the point books this many. 2 when nobody answers.
testq_box_gpus() {
  local n=""
  [ "$TESTQ" != "off" ] && n="$(curl -fsS --max-time 2 "$TESTQ_URL/state" 2>/dev/null \
    | sed -n 's/.*"capacity"[: ]*{[^}]*"gpu"[: ]*\([0-9]*\).*/\1/p')"
  [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null || n=2
  printf '%s' "$n"
}

_testq_granted() {
  _TESTQ_LEASE="$2"
  export TESTQ_BOX_ENGINES="$(_testq_num "$1" box_engines)"
  : "${TESTQ_BOX_ENGINES:=0}"
  # Matches `"slots":` and cannot match `"slots_min":` -- the closing quote is
  # part of the pattern.
  export TESTQ_SLOTS="$(_testq_num "$1" slots)"
}

testq_acquire() {
  # Kept whole for asking again: a daemon that has been restarted has
  # forgotten the ticket, and the answer to that is the same request.
  local -a asked=("$@")
  local arg="" cpu=1 min_cpu=0 gpu=0 engines=1 exclusive=false mutexes=""
  local size=null max_s=null idle_ok=false project="" script=""
  export TESTQ_SLOTS=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --arg)       arg="$2"; shift 2 ;;
      --cpu)       cpu="$2"; shift 2 ;;
      --min-cpu)   min_cpu="$2"; shift 2 ;;
      --gpu)       gpu="$2"; shift 2 ;;
      --engines)   engines="$2"; shift 2 ;;
      --exclusive) exclusive=true; shift ;;
      --mutex)     mutexes="$mutexes${mutexes:+,}\"$(_testq_json "$2")\""; shift 2 ;;
      --size)      size="$2"; shift 2 ;;
      --max)       max_s="$2"; shift 2 ;;
      --idle-ok)   idle_ok=true; shift ;;
      --project)   project="$2"; shift 2 ;;
      --script)    script="$2"; shift 2 ;;
      *)           shift ;;
    esac
  done
  [ -n "$script" ] || script="$(basename "$0")"

  [ "$TESTQ" = "off" ] && return 0

  if ! _testq_up; then
    local py
    py="$(_testq_py)"
    if [ "$TESTQ_AUTOSTART" = "1" ] && [ -n "$py" ] && command -v python >/dev/null 2>&1; then
      python "$py" start --port "$TESTQ_PORT" >/dev/null 2>&1
    fi
  fi
  if ! _testq_up; then
    if [ "$TESTQ" = "require" ]; then
      echo "!!! testq is not running and TESTQ=require -- refusing to run unqueued" >&2
      exit 3
    fi
    # Never hold a run hostage to the queue. Infrastructure that can stop you
    # testing is worse than the contention it was built to prevent -- but say
    # so loudly, because an unqueued run's timing numbers are not evidence.
    echo "!!! testq unavailable -- running UNQUEUED; wall-clock assertions may flake" >&2
    return 0
  fi

  local tree body resp ticket
  tree="$(_testq_tree)"
  body="{\"proto\":$TESTQ_PROTO,\"project\":\"$(_testq_json "$project")\""
  body="$body,\"tree_id\":\"$(_testq_tree_id "$tree")\",\"tree_path\":\"$(_testq_json "$tree")\""
  body="$body,\"script\":\"$(_testq_json "$script")\",\"arg\":\"$(_testq_json "$arg")\""
  body="$body,\"slots\":$cpu,\"slots_min\":$min_cpu,\"gpu\":$gpu,\"engines\":$engines"
  body="$body,\"exclusive\":$exclusive,\"mutexes\":[$mutexes],\"size\":$size"
  body="$body,\"max_s\":$max_s,\"idle_ok\":$idle_ok,\"winpid\":$(_testq_winpid)}"
  resp="$(_testq_post acquire "$body" 10)"
  if [ -z "$resp" ]; then
    echo "!!! testq refused the job -- running UNQUEUED" >&2
    return 0
  fi
  case "$resp" in
    *'"error"'*'"proto"'*)
      echo "!!! testq daemon is older than this worktree -- running UNQUEUED." >&2
      echo "    Upgrade it with: $(_testq_str "$resp" hint)" >&2
      return 0 ;;
  esac
  ticket="$(_testq_str "$resp" ticket)"
  [ -n "$ticket" ] || { echo "!!! testq gave no ticket -- running UNQUEUED" >&2; return 0; }

  # Left for this worktree by the daemon: today, that its last run here was
  # killed and why. The run it is about could not be told; it was dead.
  local note
  note="$(_testq_str "$resp" note)"
  [ -n "$note" ] && echo "!!! $note" >&2

  if _testq_is "$resp" granted; then
    _testq_granted "$resp" "$ticket"
    return 0
  fi

  # Time the wait here rather than trusting a field from the server: a run
  # that gets its slot inside the first long poll never sees a not-granted
  # response at all.
  local t0 misses=0 starts resumed why
  t0="$(date +%s)"
  why="$(_testq_str "$resp" blocked_on)"
  echo "[testq] the box is busy -- queued at position $(_testq_num "$resp" position) (${why:-waiting})." >&2
  echo "[testq] nothing has launched yet; watch http://localhost:$TESTQ_PORT/" >&2
  resumed="$(_testq_num "$resp" resumed_s)"
  if [ -n "$resumed" ] && [ "${resumed%.*}" -gt 0 ]; then
    echo "[testq] picked up the place of the same job queued $(_testq_span "$resumed") ago" >&2
  fi
  starts="$(_testq_starts "$resp")"
  if [ -n "$starts" ]; then
    echo "[testq] $starts." >&2
    if [ "$(_testq_num "$resp" start_in_s | cut -d. -f1)" -gt 240 ]; then
      # Said once, to whoever started this with a clock running on it. Being
      # killed in the queue is survivable, but only by asking again.
      echo "[testq] if your command will time out before then, run it in the background; a job killed while queued keeps its place for 15 minutes if it asks again." >&2
    fi
  fi

  # /wait blocks server-side for up to 25 s per call, so this loop is quiet
  # rather than a poll storm; each miss prints one line so a background log
  # always says why nothing is happening yet.
  while :; do
    resp="$(_testq_post wait "{\"ticket\":\"$ticket\"}" 40)"
    if [ -z "$resp" ]; then
      misses=$((misses + 1))
      if [ "$misses" -ge 3 ]; then
        echo "[testq] daemon stopped answering -- re-queueing" >&2
        testq_acquire "${asked[@]}"
        return $?
      fi
      sleep 2
      continue
    fi
    misses=0
    if _testq_is "$resp" granted; then
      _testq_granted "$resp" "$ticket"
      echo "[testq] got the box after $(( $(date +%s) - t0 ))s -- starting" >&2
      return 0
    fi
    if _testq_is "$resp" cancelled; then
      echo "!!! cancelled from the testq page before it started" >&2
      exit 4
    fi
    if _testq_is "$resp" unknown; then
      # The daemon was restarted, or the ticket went stale. Ask again with
      # everything that was asked the first time; the new ticket picks up
      # this one's place.
      echo "[testq] the daemon forgot this ticket -- re-queueing" >&2
      testq_acquire "${asked[@]}"
      return $?
    fi
    why="$(_testq_str "$resp" blocked_on)"
    starts="$(_testq_starts "$resp")"
    echo "[testq] still queued at position $(_testq_num "$resp" position) after $(( $(date +%s) - t0 ))s -- ${why:-waiting}${starts:+, $starts}" >&2
  done
}

# Safe to call more than once and safe in a trap. Pass the exit code
# explicitly when the trap does anything before this, because by then $? is
# the status of THAT command and not the script's:
#     trap 'rc=$?; rm -rf "$SCRATCH"; testq_release $rc' EXIT
testq_release() {
  [ -n "$_TESTQ_LEASE" ] || return 0
  local code="${1:-$?}"
  _testq_post release "{\"lease\":\"$_TESTQ_LEASE\",\"exit_code\":${code:-0}}" 5 >/dev/null
  _TESTQ_LEASE=""
}
