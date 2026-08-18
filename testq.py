#!/usr/bin/env python3
"""testq -- one queue for every Godot run on this machine.

The problem this exists for: there is one box, one GPU and about four engines'
worth of real capacity (measured -- see the header of mfrs's run_test_par.sh),
but several projects and a couple of dozen worktrees between them, each of which
thinks it is alone on it. Two sessions starting `run_test_par.sh 4` at the same
moment put eight engines on four engines' worth of machine, and everything that
asserts against the wall clock starts flipping: generation budgets, warm-up
budgets, and above all a multiplayer check whose whole verdict is a clock-skew
measurement between two live processes. Those failures are indistinguishable
from real ones by reading the log, which is how the same load artifact has cost
several sessions a day.

It belongs to no project for the same reason: the contention crosses them. A
capture in the DJ app and a physics suite in the game are two engines on one
box, and neither repo can see the other's. Anything project-specific lives in
`projects.json` in the runtime directory, and a project that registers nothing
still queues correctly -- it just waits for its own history before it can
estimate how long its jobs take.

So: a daemon holds the slots, and the run scripts ask before they launch. It is
deliberately small and deliberately boring.

  * Stdlib only. No pip, no venv, nothing to install.
  * Leader election is the port bind. If the bind fails somebody else is the
    daemon and this process exits happily. There is no lock file to go stale.
  * It never launches a Godot process and never keeps one warm. A pooled engine
    would show up in run_mp's stray count and condemn every multiplayer run to
    INCONCLUSIVE forever.
  * A client that cannot reach it runs unqueued with a warning. Test
    infrastructure that can break your tests is worse than no test
    infrastructure.

The unit of queueing is the SCRIPT INVOCATION, not the engine process. run_mp
needs two engines alive at once and run_test_par wants N; a per-process
semaphore of size one would deadlock both of them on the first call.

Subcommands:
  serve     run the HTTP server (internal -- `start` calls this)
  start     snapshot this file to the runtime dir and spawn `serve` detached
  status    one line per running and queued job
  stop      ask the daemon to exit
  tray      pin the notification-area icon up (the daemon raises it by itself
            while the box is busy, so this is only for keeping it there)
  reap      delete scratch belonging to worktrees that no longer exist
"""

import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Bumped when the /acquire body or the grant semantics change. A worktree
# carrying a newer client than the resident daemon gets a 409 and a printed
# restart command rather than a subtly wrong grant.
#
# A field ADDED to a response is deliberately not a bump: an old client ignores
# it and a new one has to treat its absence as "unknown" anyway, since the two
# copies of this file on the box mean either can win the port bind. Bumping for
# that would 409 every worktree over a field none of them need.
PROTO = 1

# Outside 27015-27022, which run_shots.sh ui reserves for the port number it
# photographs.
DEFAULT_PORT = 43117

# Four. Not a guess: run_test_par.sh's header records 6 and 8 shards each
# running 1.5-1.7x slower than 4 and finishing later overall, on this 24-core
# box, because a single headless engine already drives several cores (runtime
# mesh building and the physics step are both threaded).
CAPACITY = {"cpu": 4, "gpu": 1}

# One GPU, and the windowed harnesses (run_shots, run_clip) are the only things
# that want it.
TICK_SECONDS = 5.0
TASKLIST_CACHE_SECONDS = 5.0
WAIT_POLL_SECONDS = 25.0          # long-poll ceiling, under any proxy's patience
TICKET_STALE_SECONDS = 60.0       # a queued client that stopped polling is gone
HISTORY_KEEP = 200

# The tray icon follows the work: the daemon raises it when the box goes busy
# and the icon takes itself away once the queue has been idle this long. The
# linger is the point of the number -- a suite that ends badly has to leave a
# red dot and its balloon on screen long enough to be read, and vanishing the
# instant the last slot came back would hide exactly the run worth noticing.
TRAY_IDLE_LINGER = 90.0

# A job with no estimate sorts as if it were long, so an unknown never jumps a
# known-short one.
UNKNOWN_ETA = 10 ** 6
# Short-jobs-first is only fair if a long job cannot be starved forever. Once a
# ticket has waited longer than this it ages to the front regardless of size.
AGE_FLOOR_SECONDS = 600.0

# An exclusive job wants the box to itself, and engines we did not grant are
# not ours to wait out: an editor left open would block run_mp until it closed.
# So we hold out for a genuinely quiet box for this long and then run anyway --
# run_mp's own INCONCLUSIVE verdict is exactly the right thing to report if the
# timing then trips, and it is better than never running the check at all.
EXCLUSIVE_STRAY_PATIENCE = 300.0

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

IS_WINDOWS = os.name == "nt"


LEGACY_RUNTIME_NAME = "mfrs-testq"


def runtime_dir():
    """Where the daemon actually lives, which is never inside a worktree.

    A worktree gets deleted, or its copy of this file gets edited while the
    daemon is mid-flight. Both are routine here and both would be fatal to a
    daemon executing from that path, so `start` copies the file out and the
    server refuses to run from anywhere else.

    The legacy name is honoured when it is already there, and this matters more
    than tidiness: while a project still carries its own vendored copy of this
    file, either copy may win the port bind, and they have to agree on where the
    state and the history database live or the queue's memory depends on which
    one started first.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    override = os.environ.get("TESTQ_HOME")
    if override:
        return override
    legacy = os.path.join(base, LEGACY_RUNTIME_NAME)
    if os.path.isdir(legacy):
        return legacy
    return os.path.join(base, "testq")


def state_path():
    return os.path.join(runtime_dir(), "state.json")


def history_path():
    return os.path.join(runtime_dir(), "history.jsonl")


def log_path():
    return os.path.join(runtime_dir(), "daemon.log")


def db_path():
    return os.path.join(runtime_dir(), "testq.db")


def projects_path():
    return os.path.join(runtime_dir(), "projects.json")


def load_projects():
    """Optional per-project detail: where a project keeps its scratch, and where
    its test harness writes the per-tag timings the queue schedules with.

    The queue does not need any of this. A job carries its own `tree_path`, and
    an unregistered project simply gets its estimate from the median of what
    that job has actually taken before, which is where every non-suite job has
    always got it. The registry exists so a project that DOES publish timings
    gets a real number on a worktree's first ever run, and so `reap` knows which
    directories are safe to look at.

    {"mfrs": {"root": "C:/.../mfrs",
              "userdata": "%APPDATA%/Godot/app_userdata/Middle Fork River Slop",
              "scratch": "%TEMP%/mfrs",
              "weights": "test"}}
    """
    try:
        with open(projects_path(), "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception:
        return {}
    return blob if isinstance(blob, dict) else {}


def expand(path):
    return os.path.expandvars(os.path.expanduser(path or ""))


# ---------------------------------------------------------------------------
# The run history, in SQLite
# ---------------------------------------------------------------------------
#
# This started as an append-only JSONL file, which was fine for "show me the
# last fifty" and useless for every other question worth asking across
# sessions: how long does this tag usually take IN THIS WORKTREE, what is
# failing most often, is something getting slower than it was last week. Those
# are queries, so they get a query engine. sqlite3 is in the standard library,
# so this is still a zero-install tool.
#
# One writer (the daemon) and occasional readers (the CLI), so WAL plus a
# timeout is all the concurrency control needed.

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             TEXT,
    tree               TEXT,
    tree_path          TEXT,
    script             TEXT,
    arg                TEXT,
    slots              INTEGER,
    gpu                INTEGER,
    exclusive          INTEGER,
    engines            INTEGER,
    exit               INTEGER,
    verdict            TEXT,
    dur_s              REAL,
    queued_s           REAL,
    eta_s              REAL,
    observed_max_procs INTEGER,
    finished           REAL,
    daemon_sha         TEXT
);
CREATE INDEX IF NOT EXISTS runs_lookup   ON runs (script, arg, verdict);
CREATE INDEX IF NOT EXISTS runs_tree     ON runs (tree, finished);
CREATE INDEX IF NOT EXISTS runs_finished ON runs (finished);
"""


class db(object):
    """`with db() as conn:` -- an open, initialised, committing connection."""

    def __enter__(self):
        os.makedirs(runtime_dir(), exist_ok=True)
        self.conn = sqlite3.connect(db_path(), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        return self.conn

    def __exit__(self, *exc):
        try:
            if exc[0] is None:
                self.conn.commit()
        finally:
            self.conn.close()
        return False


def import_legacy_history():
    """Carry the old JSONL into the table once, then leave it alone.

    Worth doing rather than starting clean: the medians this thing schedules
    with only get good with history behind them, and throwing away what was
    already measured would make the queue temporarily worse at its job.
    """
    legacy = history_path()
    if not os.path.exists(legacy):
        return 0
    try:
        with db() as conn:
            have = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            if have:
                return 0
            rows = []
            with open(legacy, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    rows.append((
                        r.get("id"), r.get("tree"), "", r.get("script"),
                        r.get("arg"), 0, 0, 0, 0, r.get("exit"),
                        r.get("verdict"), r.get("dur_s"), r.get("queued_s"),
                        r.get("eta_s"), r.get("observed_max_procs", 0),
                        r.get("finished"), "legacy",
                    ))
            conn.executemany(
                "INSERT INTO runs (job_id, tree, tree_path, script, arg, slots,"
                " gpu, exclusive, engines, exit, verdict, dur_s, queued_s,"
                " eta_s, observed_max_procs, finished, daemon_sha)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        os.replace(legacy, legacy + ".imported")
        return len(rows)
    except Exception as exc:
        sys.stderr.write("legacy history import failed: %r\n" % (exc,))
        return 0


def port_from_env(explicit=None):
    if explicit:
        return int(explicit)
    return int(os.environ.get("TESTQ_PORT", DEFAULT_PORT))


def now():
    return time.time()


# ---------------------------------------------------------------------------
# Windows process liveness
# ---------------------------------------------------------------------------
#
# A client hands us the pid it wants to be judged by. It has to be the WINDOWS
# pid -- bash's own $$ is an MSYS pid that no Windows API and no tasklist has
# ever heard of -- which is why run_lib.sh reads /proc/$$/winpid.
#
# Liveness is (pid exists) AND (its creation time is the one we recorded at
# grant). Windows reuses pids briskly; without the creation time a long suite
# whose shell was killed can be kept "alive" indefinitely by an unrelated
# process that happened to inherit the number.

def process_ctime(pid):
    """Creation time of a live process as an int, or None if it isn't running."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return 0
        except OSError:
            return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            # A handle can still resolve after the process has exited. Only
            # STILL_ACTIVE means it is really there.
            if code.value != STILL_ACTIVE:
                return None
        created = ctypes.c_ulonglong()
        exited = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        ok = k32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        return int(created.value)
    finally:
        k32.CloseHandle(handle)


def process_alive(pid, ctime):
    current = process_ctime(pid)
    if current is None:
        return False
    if ctime is None:
        return True
    return current == ctime


def process_table():
    """pid -> (parent pid, image name, command line) for everything on the box."""
    if not IS_WINDOWS:
        return {}
    script = ("Get-CimInstance Win32_Process | "
              "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
              "ConvertTo-Json -Compress")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        rows = json.loads(out)
    except Exception:
        return {}
    if isinstance(rows, dict):
        rows = [rows]
    table = {}
    for row in rows:
        try:
            table[int(row["ProcessId"])] = (
                int(row.get("ParentProcessId") or 0),
                row.get("Name") or "",
                row.get("CommandLine") or "",
            )
        except (TypeError, ValueError, KeyError):
            continue
    return table


def taskkill(pid):
    try:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(int(pid))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
        return True
    except Exception:
        return False


def kill_job(pid, tree_path=""):
    """Stop a run: its shell, everything under it, and its engines.

    `taskkill /F /T` is not enough on its own, and this was worth finding out
    the hard way -- killing a run's bash left `timeout.exe` and both Godot
    engines alive and still grinding through `--test=all` twenty minutes later.
    Once the shell dies the parent chain to the engines is broken, so a
    descendant walk from the shell finds nothing either.

    So there are two passes. The first walks the real parent links and kills
    the subtree, which is precise and catches everything while the chain is
    intact. The second sweeps up engines that are already orphaned, matched on
    the worktree directory in their `--path` argument -- the leaf name, because
    the shell writes it /c/Users/... and the engine reports it C:/Users/... and
    only the leaf is spelled the same in both.

    The sweep tests the IMAGE NAME for Godot and the COMMAND LINE for the
    worktree, and needs both. Testing the command line for Godot instead looks
    equivalent and is not: it also matches every shell whose command line
    happens to mention the engine, which on this box includes the editor's own
    terminals. An early version of this would have killed them.

    Never a blanket kill by image name, either -- that is how one session's
    cleanup has taken down every other worktree's tests on this box before.
    """
    if not IS_WINDOWS:
        try:
            os.kill(int(pid), 9)
            return 1
        except OSError:
            return 0

    try:
        root = int(pid)
    except (TypeError, ValueError):
        root = 0
    leaf = os.path.basename((tree_path or "").rstrip("/\\")).lower()

    # Several passes, because one is provably not enough. Git-Bash's `timeout`
    # nests a second copy of itself between the shell and the engine, the
    # console build of Godot launches the real one as a child of its own, and
    # the whole chain re-parents as its links die. A single sweep works from a
    # snapshot taken before any of that shifts, so it reliably leaves a pair of
    # engines behind -- which is exactly the bug this loop was written for,
    # found by watching a "cancelled" job keep running.
    killed = []
    for attempt in range(4):
        table = process_table()
        victims = []
        if root and root in table:
            victims.append(root)
            frontier = [root]
            while frontier:
                parent = frontier.pop()
                for child, (ppid, _, _) in table.items():
                    if ppid == parent and child not in victims:
                        victims.append(child)
                        frontier.append(child)
        elif root and attempt == 0:
            victims.append(root)

        if leaf:
            for candidate, (_, name, cmdline) in table.items():
                if candidate in victims:
                    continue
                if GODOT_MATCH in name.lower() and leaf in cmdline.lower():
                    victims.append(candidate)

        if not victims:
            break
        # Children before parents, so a shell cannot notice its child died and
        # start another one.
        for victim in reversed(victims):
            taskkill(victim)
            killed.append("%d:%s" % (victim, table.get(victim, (0, "?", ""))[1]))
        time.sleep(1.0)

    # Logged because a cancel that silently fails to stop the engines is the
    # worst outcome here: the slots come back, the page says the job is gone,
    # and the box is still busy. daemon.log is where that shows up.
    sys.stderr.write("kill_job(pid=%s, tree=%s): killed %s\n"
                     % (pid, leaf or "?", ", ".join(killed) or "nothing"))
    sys.stderr.flush()
    return len(killed)


# ---------------------------------------------------------------------------
# Counting the engines actually on the box
# ---------------------------------------------------------------------------
#
# Worktrees that predate this queue still run their own unshimmed scripts, and
# the user can always start the editor. Anything we do not know about still
# eats the machine, so we count what is really there and treat the excess as
# capacity we do not have. That single rule is also what stops a restarted
# daemon from overgranting on top of the previous daemon's orphans.

GODOT_MATCH = os.environ.get("TESTQ_GODOT_MATCH", "godot_v4").lower()

# One engine is TWO processes in tasklist. The harnesses run the _console build,
# which is a launcher: it starts the real Godot_v4.7-stable_win64.exe as its own
# child and both sit in the process list for the life of the run. Counting both
# would double every number here -- a four-shard suite reads as eight engines,
# the daemon decides four of them belong to somebody else, and it docks four
# slots of capacity that nothing is actually using. Counting only the
# non-console binary gives exactly one per running engine, whichever build was
# launched, because the console one always has a plain child.
#
# The kill path deliberately does NOT use this exclusion: there both halves of
# the pair have to die.
GODOT_EXCLUDE = "_console"


def count_godot():
    if not IS_WINDOWS:
        return 0
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=20,
        ).stdout
    except Exception:
        return 0
    n = 0
    for line in out.splitlines():
        head = line.split(",", 1)[0].strip().strip('"').lower()
        if GODOT_MATCH in head and GODOT_EXCLUDE not in head:
            n += 1
    return n


# ---------------------------------------------------------------------------
# ETA
# ---------------------------------------------------------------------------
#
# A project whose suite publishes per-tag timings gets a real estimate on a
# worktree's first run instead of waiting for its own history to build up. In
# mfrs that file is user://test/weights-<TREE_ID>.json, a tag-string ->
# milliseconds map refreshed by every `run_test.sh all`. A project that
# publishes nothing is not disadvantaged for long: the history medians take
# over after two runs of the same job.

def weights_dir(project):
    entry = load_projects().get(project or "") or {}
    userdata = expand(entry.get("userdata"))
    if not userdata:
        return ""
    return os.path.join(userdata, entry.get("weights") or "test")


def suite_eta(tree_id, which, shards=1, project=""):
    """Seconds a self-test run of `which` should take in this worktree."""
    root = weights_dir(project)
    if not root:
        return None
    path = os.path.join(root, "weights-%s.json" % tree_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            weights = json.load(fh)
    except Exception:
        return None
    if not isinstance(weights, dict) or not weights:
        return None
    total_ms = 0.0
    which = (which or "all").strip()
    if which == "all" or "/" in which:
        total_ms = sum(float(v) for v in weights.values())
    else:
        for tags, ms in weights.items():
            if which in str(tags).split():
                total_ms += float(ms)
        if total_ms <= 0:
            return None
    seconds = total_ms / 1000.0
    if shards > 1:
        # Bin packing is good but not perfect, and there is fixed startup per
        # shard; the measured spread is well inside this fudge.
        seconds = seconds / shards * 1.15
    return round(seconds, 1)


# ---------------------------------------------------------------------------
# The queue itself
# ---------------------------------------------------------------------------

class Queue(object):
    def __init__(self, capacity=None):
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.capacity = dict(capacity or CAPACITY)
        self.leases = {}
        self.queue = []
        self.held_mutex = {}
        self.history = []
        # Ids cancelled from the UI, so a client long-polling for one is told
        # to give up rather than quietly re-queueing itself.
        self.cancelled = set()
        self.seq = 0
        self.started = now()
        self.observed = 0
        self.observed_at = 0.0
        self.stray = 0
        self.stray_prev = 0
        self.sha = ""
        self.port = DEFAULT_PORT
        # Whether the box was busy at the last tick, which is all the tray
        # needs: the icon is raised on the edge into busy, not held up by us.
        self.was_busy = False
        self.load_state()
        self.load_history()

    # -- persistence ------------------------------------------------------

    def load_state(self):
        try:
            with open(state_path(), "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except Exception:
            return
        # Re-verify every lease before adopting it. The daemon may have been
        # down for hours; most of what it remembers has finished.
        for lease in blob.get("leases", []):
            if process_alive(lease.get("winpid"), lease.get("pid_ctime")):
                self.leases[lease["id"]] = lease
                for key in lease.get("mutexes", []):
                    self.held_mutex[key] = lease["id"]
        self.seq = int(blob.get("seq", 0))
        # Queued tickets are deliberately NOT restored: their clients notice
        # the dead daemon on their next poll and re-acquire from scratch,
        # which is simpler than trying to keep two ideas of the queue in step.

    def save_state(self):
        blob = {
            "seq": self.seq,
            "saved_at": now(),
            "leases": list(self.leases.values()),
        }
        tmp = state_path() + ".tmp"
        try:
            os.makedirs(runtime_dir(), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(blob, fh)
            os.replace(tmp, state_path())
        except Exception:
            pass

    def load_history(self):
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY finished DESC LIMIT ?",
                    (HISTORY_KEEP,),
                ).fetchall()
            self.history = [dict(r) for r in reversed(rows)]
        except Exception:
            self.history = []

    def append_history(self, row):
        self.history.append(row)
        self.history = self.history[-HISTORY_KEEP:]
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO runs (job_id, tree, tree_path, script, arg,"
                    " slots, gpu, exclusive, engines, exit, verdict, dur_s,"
                    " queued_s, eta_s, observed_max_procs, finished, daemon_sha)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row.get("id"), row.get("tree"), row.get("tree_path", ""),
                     row.get("script"), row.get("arg"), row.get("slots", 0),
                     row.get("gpu", 0), int(bool(row.get("exclusive"))),
                     row.get("engines", 0), row.get("exit"), row.get("verdict"),
                     row.get("dur_s"), row.get("queued_s"), row.get("eta_s"),
                     row.get("observed_max_procs", 0), row.get("finished"),
                     self.sha),
                )
        except Exception as exc:
            sys.stderr.write("history write failed: %r\n" % (exc,))

    # -- helpers ----------------------------------------------------------

    def next_id(self, prefix):
        self.seq += 1
        return "%s%d" % (prefix, self.seq)

    def need_of(self, job):
        if job.get("exclusive"):
            return self.capacity["cpu"], self.capacity["gpu"]
        return int(job.get("slots", 0)), int(job.get("gpu", 0))

    def refresh_observed(self, force=False):
        if not force and now() - self.observed_at < TASKLIST_CACHE_SECONDS:
            return
        self.observed = count_godot()
        self.observed_at = now()
        expected = sum(int(l.get("engines", 0)) for l in self.leases.values())
        stray = max(0, self.observed - expected)
        # Two consecutive samples before we believe it. A single sample catches
        # the transient --import engine of a job that has a lease but has not
        # launched its real engines yet, and flapping capacity on that would
        # make grants unpredictable for no gain.
        self.stray = min(stray, self.stray_prev)
        self.stray_prev = stray
        for lease in self.leases.values():
            lease["observed_max_procs"] = max(
                int(lease.get("observed_max_procs", 0)), self.observed
            )

    # -- the scheduler ----------------------------------------------------

    def tick(self):
        """Reap the dead, recount the box, grant what fits. Holds the lock."""
        with self.lock:
            self.refresh_observed()
            self.reap()
            granted = self.grant_pass()
            if granted:
                self.save_state()
                self.changed.notify_all()
            busy = bool(self.leases or self.queue)
        # Outside the lock: spawning a process is not something to hold the
        # scheduler for, and nothing below touches queue state.
        self.follow_tray(busy)
        return granted

    def follow_tray(self, busy):
        """Raise the notification-area icon when the box goes from idle to busy.

        The icon is worth having exactly while something is using the machine,
        so nobody should have to remember to start it -- any project's first
        acquire starts the daemon, and the daemon puts the icon up. It takes
        itself away again after TRAY_IDLE_LINGER of quiet.

        Only on the edge into busy. If you dismiss the icon by hand mid-run,
        respawning it on the next tick would be arguing with you; it stays
        dismissed until the queue has gone quiet and come back.

        Strays deliberately do not count as busy. An editor left open all
        afternoon is an unmanaged engine, and raising a permanent icon for it
        would make the icon furniture again.
        """
        if busy == self.was_busy:
            return
        self.was_busy = busy
        if busy and spawn_tray(self.port):
            sys.stderr.write("raised the tray icon\n")
            sys.stderr.flush()

    def reap(self):
        dead = []
        for lease_id, lease in self.leases.items():
            if not process_alive(lease.get("winpid"), lease.get("pid_ctime")):
                dead.append(lease_id)
        for lease_id in dead:
            self.finish(lease_id, exit_code=None, verdict="reclaimed")
        stale = [t for t in self.queue
                 if now() - t.get("last_poll", t["enqueued_at"]) > TICKET_STALE_SECONDS]
        for ticket in stale:
            self.queue.remove(ticket)
            self.append_history(self.history_row(ticket, None, "abandoned"))
        if dead or stale:
            self.save_state()

    def order(self):
        """Queue order: short jobs first, with aging so nothing starves.

        The user's call, and the right one for how this box is used: a
        twenty-second tagged slice run while iterating should not sit behind
        two eleven-minute suites. The aging term is what keeps that honest --
        once a ticket has waited past max(600s, twice its own estimate) it
        goes to the front in arrival order and short jobs stop overtaking it.
        """
        def key(ticket):
            eta = ticket.get("eta_s")
            eta = UNKNOWN_ETA if eta is None else float(eta)
            waited = now() - ticket["enqueued_at"]
            aged = waited > max(AGE_FLOOR_SECONDS, 2.0 * min(eta, UNKNOWN_ETA))
            if aged:
                return (0, ticket["enqueued_at"], 0.0)
            return (1, eta, ticket["enqueued_at"])
        return sorted(self.queue, key=key)

    def grant_pass(self):
        used_cpu = sum(self.need_of(l)[0] for l in self.leases.values())
        used_gpu = sum(self.need_of(l)[1] for l in self.leases.values())
        # Capacity we cannot use because somebody outside the queue is using
        # it. Never let this drive availability negative.
        penalty = min(self.stray, self.capacity["cpu"])
        free_cpu = max(0, self.capacity["cpu"] - used_cpu - penalty)
        free_gpu = max(0, self.capacity["gpu"] - used_gpu)

        granted = []
        reserved_mutexes = set()
        for ticket in self.order():
            need_cpu, need_gpu = self.need_of(ticket)
            mutexes = list(ticket.get("mutexes", []))
            blocked_mutex = any(
                key in self.held_mutex or key in reserved_mutexes for key in mutexes
            )
            fits = (not blocked_mutex) and need_cpu <= free_cpu and need_gpu <= free_gpu
            # An exclusive job additionally needs the box to itself: no other
            # lease at all, and no stray engine. run_mp measures inter-process
            # clock skew, so one windowed clip alongside it is enough to make
            # its verdict meaningless. Strays are not ours to wait out forever
            # though -- see EXCLUSIVE_STRAY_PATIENCE.
            if fits and ticket.get("exclusive"):
                waited = now() - ticket["enqueued_at"]
                quiet = self.stray == 0 or waited > EXCLUSIVE_STRAY_PATIENCE
                fits = not self.leases and quiet
            if not fits and self.try_shrink(ticket, blocked_mutex, free_cpu,
                                            free_gpu):
                need_cpu, need_gpu = self.need_of(ticket)
                fits = True
            if fits:
                self.activate(ticket)
                granted.append(ticket["id"])
                free_cpu -= need_cpu
                free_gpu -= need_gpu
            else:
                # Head-of-line reservation. What this ticket cannot get yet is
                # held back from everyone behind it, so a four-slot job is not
                # starved by a stream of one-slot jobs -- but a job blocked
                # purely on the GPU still lets CPU-only work past.
                free_cpu = max(0, free_cpu - need_cpu)
                free_gpu = max(0, free_gpu - need_gpu)
                reserved_mutexes.update(mutexes)
                ticket["blocked_on"] = self.explain(ticket, blocked_mutex)
        return granted

    def eta_until_free(self, want_more):
        """A rough time until `want_more` further cpu slots come free, off the
        running leases' own estimates, assuming nothing new is granted first.
        Floored per lease at 15 s: a lease already past its estimate could end
        any second, but assuming zero would make every shrink decision read
        "the wait is free" exactly when the estimate has already been wrong.
        """
        rel = []
        for l in self.leases.values():
            eta = l.get("eta_s")
            eta = UNKNOWN_ETA if eta is None else float(eta)
            remaining = max(15.0, eta - (now() - float(l.get("granted_at") or now())))
            rel.append((remaining, self.need_of(l)[0]))
        rel.sort()
        freed = 0
        for remaining, slots in rel:
            freed += slots
            if freed >= want_more:
                return remaining
        return float("inf")

    def try_shrink(self, ticket, blocked_mutex, free_cpu, free_gpu):
        """Grant a flexible job narrower than it asked, when that answers
        sooner than waiting for the full width.

        The lumpiness problem this solves: a four-slot job cannot start until
        four slots are free AT ONCE, so behind two long one-slot jobs it sits
        for minutes while two slots idle. A job that declared `slots_min` is
        promising its total work is fixed and divides across whatever width it
        gets (run_test_par: the suite is the suite, shards just split it), so
        the comparison is closed-form: total work W at width G finishes at
        W/G; waiting T for the full width N finishes at T + W/N. Shrink only
        when the first is sooner -- two slots freeing in thirty seconds still
        beat starting narrow, and this arithmetic is why a shrink never fires
        in that case.

        The contract for flexible jobs, and why `engines` is rewritten too:
        a job that sends slots_min is stating its engine count FOLLOWS the
        granted width (the client reads the granted `slots` off the lease and
        launches that many). A job whose engine count is fixed must not send
        slots_min at all.
        """
        if blocked_mutex or ticket.get("exclusive"):
            return False
        want, want_gpu = self.need_of(ticket)
        smin = int(ticket.get("slots_min", want))
        if smin >= want or free_cpu < smin or want_gpu > free_gpu:
            return False
        eta = ticket.get("eta_s")
        if eta is None:
            return False
        grant = min(want, free_cpu)
        work = float(eta) * want
        wait = self.eta_until_free(want - free_cpu)
        if work / grant >= wait + work / want:
            return False
        ticket["slots"] = grant
        ticket["engines"] = grant
        ticket["eta_s"] = round(work / grant, 1)
        return True

    def explain(self, ticket, blocked_mutex):
        if blocked_mutex:
            return "waiting for " + ", ".join(ticket.get("mutexes", []))
        if ticket.get("exclusive"):
            if self.stray:
                return "waiting for a quiet box -- %d unmanaged engine(s)" % self.stray
            return "waiting for a quiet box"
        if int(ticket.get("gpu", 0)) > 0:
            used_gpu = sum(self.need_of(l)[1] for l in self.leases.values())
            if used_gpu >= self.capacity["gpu"]:
                return "waiting for the GPU"
        if self.stray:
            return "waiting for slots -- %d unmanaged engine(s) on the box" % self.stray
        return "waiting for slots"

    def box_engines(self, ticket):
        """How many engines this box is committed to carrying while `ticket`
        runs, its own included.

        This is what a wall-clock assertion inside the run actually needs, and
        it is not the same question as "did the queue overload the box". A
        `run_test_par.sh 4` gets the whole box to itself and is still measuring
        against three sibling shards; mfrs's sound-bank load budget failed at
        577 ms that way with this number at 4 and nothing else on the machine.
        The queue was right and the measurement was still worthless. Only the
        run can decide what to do about that, and it cannot decide without
        being told.

        Called from activate() before the ticket joins self.leases, so summing
        the leases counts everybody else exactly once -- including anything
        granted earlier in the same grant_pass.
        """
        others = sum(int(l.get("engines", 1)) for l in self.leases.values())
        return int(ticket.get("engines", 1)) + others + self.stray

    def activate(self, ticket):
        self.queue.remove(ticket)
        ticket["granted_at"] = now()
        ticket["pid_ctime"] = process_ctime(ticket.get("winpid"))
        ticket["observed_max_procs"] = self.observed
        # Fixed at grant, because that is the only moment the client is
        # listening -- it exports this into the engine's environment and then
        # launches. A one-slot job can still be joined afterwards by work the
        # three free slots allow, so this is a floor and not a promise; the
        # cases that matter most (a par run booking every slot, an exclusive
        # one) cannot be joined at all and so are exact.
        ticket["box_engines"] = self.box_engines(ticket)
        self.leases[ticket["id"]] = ticket
        for key in ticket.get("mutexes", []):
            self.held_mutex[key] = ticket["id"]

    def history_row(self, job, exit_code, verdict):
        granted = job.get("granted_at")
        return {
            "id": job["id"],
            "tree": job.get("tree_id", ""),
            # Everything the ticket booked has to ride along here, or the
            # database quietly records a 4-engine par run as costing the same
            # as a 1-engine serial one. It did, for the first 400 rows: these
            # five keys were missing, append_history()'s .get() defaults
            # filled the columns with 0/"" and nobody could compute true
            # engine-hours from `stats` until it was noticed in a profile.
            "tree_path": job.get("tree_path", ""),
            "script": job.get("script", ""),
            "arg": job.get("arg", ""),
            "slots": job.get("slots", 0),
            "gpu": job.get("gpu", 0),
            "exclusive": job.get("exclusive", False),
            "engines": job.get("engines", 1),
            "exit": exit_code,
            "verdict": verdict,
            "dur_s": round(now() - granted, 1) if granted else 0.0,
            "queued_s": round((granted or now()) - job["enqueued_at"], 1),
            "eta_s": job.get("eta_s"),
            "observed_max_procs": job.get("observed_max_procs", 0),
            "finished": now(),
        }

    def finish(self, lease_id, exit_code=None, verdict="released"):
        lease = self.leases.pop(lease_id, None)
        if lease is None:
            return False
        for key in list(self.held_mutex):
            if self.held_mutex[key] == lease_id:
                del self.held_mutex[key]
        self.append_history(self.history_row(lease, exit_code, verdict))
        return True

    # -- public operations -------------------------------------------------

    def enqueue(self, body):
        with self.lock:
            self.refresh_observed()
            script = str(body.get("script", "?"))
            arg = str(body.get("arg", ""))
            tree_id = str(body.get("tree_id", "?"))
            eta = body.get("eta_s")
            if eta is None:
                eta = self.estimate(script, arg, tree_id, body)
            slots_val = max(0, int(body.get("slots", 1)))
            # A flexible job: "slots is what I want, slots_min is what I can
            # run on". 0 or absent means rigid, which is every job that runs
            # a fixed number of engines. Only a job whose engine count follows
            # the width it is granted (run_test_par picks its shard count off
            # the grant) should send this -- see try_shrink for the contract.
            smin = int(body.get("slots_min", 0) or 0)
            smin = slots_val if smin <= 0 else min(smin, slots_val)
            ticket = {
                "id": self.next_id("J"),
                "project": str(body.get("project", "")),
                "tree_id": tree_id,
                "tree_path": str(body.get("tree_path", "")),
                "script": script,
                "arg": arg,
                "cmdline": str(body.get("cmdline", "")),
                "slots": slots_val,
                "slots_min": smin,
                "gpu": max(0, int(body.get("gpu", 0))),
                "exclusive": bool(body.get("exclusive", False)),
                "engines": max(1, int(body.get("engines", 1))),
                "mutexes": [str(m) for m in body.get("mutexes", []) if str(m)],
                "winpid": int(body.get("winpid", 0) or 0),
                "pid_ctime": None,
                "enqueued_at": now(),
                "last_poll": now(),
                "granted_at": None,
                "eta_s": eta,
                "blocked_on": "",
                "observed_max_procs": 0,
                "box_engines": 0,
            }
            self.queue.append(ticket)
            self.grant_pass()
            self.save_state()
            self.changed.notify_all()
            return ticket

    def estimate(self, script, arg, tree_id, body):
        project = str(body.get("project", ""))
        if script.startswith("run_test_par"):
            shards = 1
            try:
                shards = max(1, int(arg))
            except (TypeError, ValueError):
                shards = 4
            eta = suite_eta(tree_id, "all", shards, project)
            if eta is not None:
                return eta
        elif script.startswith("run_test"):
            eta = suite_eta(tree_id, arg or "all", 1, project)
            if eta is not None:
                return eta
        # Anything else -- run_mp, shots, clips -- has no weights file, and a
        # worktree that has never run the full suite has no weights either. The
        # median of what this exact job actually took is better than a constant
        # and it is already in the table.
        #
        # Prefer this worktree's own history, because a branch mid-feature can
        # be genuinely slower than its neighbours; fall back to every worktree
        # so a fresh checkout still gets a number on its first run.
        for where, args in (
            ("script=? AND arg=? AND tree=?", (script, arg, tree_id)),
            ("script=? AND arg=?", (script, arg)),
        ):
            try:
                with db() as conn:
                    row = conn.execute(
                        "SELECT dur_s FROM runs WHERE " + where +
                        " AND verdict='released' AND dur_s > 0"
                        " ORDER BY finished DESC LIMIT 9", args
                    ).fetchall()
            except Exception:
                return None
            if len(row) >= 2:
                durations = sorted(float(r["dur_s"]) for r in row)
                return round(durations[len(durations) // 2], 1)
        return None

    def find(self, job_id):
        with self.lock:
            if job_id in self.leases:
                return self.leases[job_id], "running"
            for ticket in self.queue:
                if ticket["id"] == job_id:
                    return ticket, "queued"
            return None, None

    def poll(self, job_id):
        """Long-poll: block until this ticket is granted or the ceiling hits."""
        deadline = now() + WAIT_POLL_SECONDS
        with self.lock:
            while True:
                if job_id in self.leases:
                    return {"granted": True, "lease": self.leases[job_id]}
                if job_id in self.cancelled:
                    return {"granted": False, "cancelled": True}
                ticket = next((t for t in self.queue if t["id"] == job_id), None)
                if ticket is None:
                    return {"granted": False, "unknown": True}
                ticket["last_poll"] = now()
                remaining = deadline - now()
                if remaining <= 0:
                    order = self.order()
                    position = order.index(ticket) + 1 if ticket in order else 0
                    return {
                        "granted": False,
                        "position": position,
                        "waiting_s": round(now() - ticket["enqueued_at"], 1),
                        "blocked_on": ticket.get("blocked_on", ""),
                        "ahead": [self.brief(t) for t in order[:position - 1][:4]],
                        "running": [self.brief(l) for l in self.leases.values()],
                    }
                self.changed.wait(min(remaining, 1.0))
                # Re-run the scheduler ourselves rather than trusting the timer
                # thread: a release that frees exactly our slots should hand
                # them over now, not up to five seconds from now.
                self.refresh_observed()
                if self.grant_pass():
                    self.save_state()
                    self.changed.notify_all()

    def brief(self, job):
        started = job.get("granted_at")
        return {
            "tree": job.get("tree_id", ""),
            "script": job.get("script", ""),
            "arg": job.get("arg", ""),
            "elapsed_s": round(now() - started, 1) if started else None,
            "eta_s": job.get("eta_s"),
        }

    def release(self, lease_id, exit_code):
        with self.lock:
            ok = self.finish(lease_id, exit_code, "released")
            if ok:
                self.grant_pass()
                self.save_state()
                self.changed.notify_all()
            return ok

    def cancel(self, job_id):
        with self.lock:
            self.cancelled.add(job_id)
            if job_id in self.leases:
                lease = self.leases[job_id]
                kill_job(lease.get("winpid"), lease.get("tree_path", ""))
                self.finish(job_id, None, "cancelled")
                self.grant_pass()
                self.save_state()
                self.changed.notify_all()
                return "cancelled a running job"
            ticket = next((t for t in self.queue if t["id"] == job_id), None)
            if ticket is not None:
                self.queue.remove(ticket)
                self.append_history(self.history_row(ticket, None, "cancelled"))
                # Its client is long-polling; tell it the ticket is gone so it
                # stops waiting instead of sitting there for a minute.
                self.changed.notify_all()
                return "removed a queued job"
            return None

    def snapshot(self):
        with self.lock:
            self.refresh_observed()
            used_cpu = sum(self.need_of(l)[0] for l in self.leases.values())
            used_gpu = sum(self.need_of(l)[1] for l in self.leases.values())
            order = self.order()
            running = []
            for lease in sorted(self.leases.values(), key=lambda l: l.get("granted_at") or 0):
                row = dict(lease)
                row["elapsed_s"] = round(now() - lease["granted_at"], 1) if lease.get("granted_at") else 0
                running.append(row)
            queued = []
            for i, ticket in enumerate(order):
                row = dict(ticket)
                row["position"] = i + 1
                row["waiting_s"] = round(now() - ticket["enqueued_at"], 1)
                queued.append(row)
            return {
                "now": now(),
                "daemon": {
                    "proto": PROTO,
                    "sha": self.sha,
                    "started": self.started,
                    "runtime": os.path.abspath(sys.argv[0]),
                    "pid": os.getpid(),
                },
                "capacity": dict(self.capacity),
                "used": {"cpu": used_cpu, "gpu": used_gpu, "stray_penalty": self.stray},
                "godot": {
                    "observed": self.observed,
                    "expected": sum(int(l.get("engines", 0)) for l in self.leases.values()),
                    "unmanaged": self.stray,
                },
                "running": running,
                "queued": queued,
                "history": list(reversed(self.history[-50:])),
            }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "testq/1"
    queue = None
    stopping = None

    def log_message(self, fmt, *args):
        pass  # daemon.log is for crashes, not for a line per poll

    def _send(self, code, payload, content_type="application/json"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode("utf-8")
        else:
            body = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/version":
            return self._send(200, {"proto": PROTO, "sha": self.queue.sha,
                                    "started": self.queue.started})
        if path == "/state":
            return self._send(200, self.queue.snapshot())
        if path in ("/", "/index.html"):
            return self._send(200, UI_HTML, "text/html; charset=utf-8")
        return self._send(404, {"error": "no such path"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path == "/acquire":
            if int(body.get("proto", 0)) != PROTO:
                return self._send(409, {
                    "error": "proto",
                    "daemon_proto": PROTO,
                    "hint": origin_hint(),
                })
            ticket = self.queue.enqueue(body)
            # Say straight away whether this went through. A client that has to
            # wait can then print one line about it now, instead of staying
            # silent until its first long poll happens to come back -- which,
            # if the wait is under 25 s, is never.
            with self.queue.lock:
                granted = ticket["id"] in self.queue.leases
                position, blocked = 0, ""
                if not granted:
                    order = self.queue.order()
                    position = next((i + 1 for i, t in enumerate(order)
                                     if t["id"] == ticket["id"]), 0)
                    blocked = ticket.get("blocked_on", "")
            return self._send(200, {
                "ticket": ticket["id"],
                "eta_s": ticket.get("eta_s"),
                "granted": granted,
                "position": position,
                "blocked_on": blocked,
                # Only meaningful when granted is true; a queued ticket has no
                # grant to describe yet and the client reads it again off the
                # /wait response that finally grants it.
                "box_engines": ticket.get("box_engines", 0),
                # The width actually granted, which for a flexible job can be
                # less than it asked (see try_shrink). Same caveat as above.
                "slots": ticket.get("slots", 0),
            })
        if path == "/wait":
            job_id = str(body.get("ticket", ""))
            return self._send(200, self.queue.poll(job_id))
        if path == "/release":
            ok = self.queue.release(str(body.get("lease", "")), body.get("exit_code"))
            return self._send(200, {"released": ok})
        if path == "/cancel":
            what = self.queue.cancel(str(body.get("job", "")))
            if what is None:
                return self._send(404, {"error": "no such job"})
            return self._send(200, {"cancelled": what})
        if path == "/quit":
            with self.queue.lock:
                if self.queue.leases and not body.get("force"):
                    return self._send(409, {
                        "error": "busy",
                        "running": len(self.queue.leases),
                    })
            self._send(200, {"stopping": True})
            self.stopping.set()
            return None
        return self._send(404, {"error": "no such path"})


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>testq</title>
<style>
  :root {
    --bg: #0f1216; --panel: #171c22; --line: #262e37; --ink: #e6edf3;
    --dim: #8b97a5; --ok: #3fb950; --warn: #d29922; --bad: #f85149;
    --cool: #58a6ff; --grey: #6e7781;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.5 "Segoe UI", system-ui, sans-serif; }
  header { padding: 14px 20px; border-bottom: 1px solid var(--line);
           display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 16px; margin: 0; font-weight: 600; letter-spacing: .02em; }
  h2 { font-size: 13px; margin: 0 0 8px; color: var(--dim); font-weight: 600;
       text-transform: uppercase; letter-spacing: .06em; }
  main { padding: 20px; display: grid; gap: 20px; max-width: 1400px; }
  .panel { background: var(--panel); border: 1px solid var(--line);
           border-radius: 8px; padding: 14px 16px; }
  .pips { display: flex; gap: 6px; align-items: center; }
  .pip { width: 26px; height: 12px; border-radius: 3px; background: #222a33;
         border: 1px solid var(--line); }
  .pip.on { background: var(--cool); border-color: var(--cool); }
  .pip.gpu.on { background: #a371f7; border-color: #a371f7; }
  .pip.stray { background: repeating-linear-gradient(45deg, var(--warn),
               var(--warn) 3px, #3a2f14 3px, #3a2f14 6px); border-color: var(--warn); }
  .tag { color: var(--dim); font-size: 12px; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 11px; text-transform: uppercase;
       letter-spacing: .06em; color: var(--dim); font-weight: 600;
       padding: 4px 8px; border-bottom: 1px solid var(--line); }
  td { padding: 7px 8px; border-bottom: 1px solid #1d242c; vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  .mono { font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; }
  .bar { position: relative; height: 8px; border-radius: 4px; background: #222a33;
         min-width: 90px; overflow: hidden; }
  .bar span { position: absolute; inset: 0 auto 0 0; background: var(--cool); }
  .bar span.over { background: var(--warn); }
  .banner { background: #2b2410; border: 1px solid var(--warn); color: #f0d48a;
            border-radius: 8px; padding: 10px 14px; }
  button { background: #21262d; color: var(--ink); border: 1px solid var(--line);
           border-radius: 5px; padding: 3px 10px; cursor: pointer; font-size: 12px; }
  button:hover { border-color: var(--bad); color: var(--bad); }
  .exit0 { color: var(--ok); } .exit1 { color: var(--bad); }
  .exit2 { color: var(--warn); } .exitx { color: var(--grey); }
  .empty { color: var(--dim); font-style: italic; padding: 6px 8px; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
         background: var(--ok); margin-right: 7px; }
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span>testq</h1>
  <div class="pips" id="pips"></div>
  <div class="tag" id="capline"></div>
  <div class="tag" id="stamp" style="margin-left:auto"></div>
</header>
<main>
  <div id="banner"></div>
  <div class="panel">
    <h2>Running</h2>
    <div id="running"></div>
  </div>
  <div class="panel">
    <h2>Queued</h2>
    <div id="queued"></div>
  </div>
  <div class="panel">
    <h2>Recent</h2>
    <div id="history"></div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let last = null, lastAt = 0;

function dur(s) {
  if (s === null || s === undefined) return "--";
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return m + "m" + String(r).padStart(2, "0") + "s";
  return Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") + "m";
}
function esc(t) {
  return String(t === null || t === undefined ? "" : t).replace(/[&<>"]/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
function job(j) { return esc(j.script) + (j.arg ? " " + esc(j.arg) : ""); }
// Worktree names alone stopped being enough once more than one project shared
// the box: two repos can both have a branch worktree called `godot-fixes`.
function who(j) { return esc(j.project ? j.project + "/" + j.tree_id : j.tree_id); }

async function cancel(id, label) {
  if (!confirm("Cancel " + label + "?\n\nA running job is killed outright; a queued one just leaves the queue.")) return;
  await fetch("/cancel", {method: "POST", headers: {"Content-Type": "application/json"},
                          body: JSON.stringify({job: id})});
  refresh();
}

function render(s) {
  const drift = (Date.now() - lastAt) / 1000;
  const cap = s.capacity, used = s.used;
  let pips = "";
  for (let i = 0; i < cap.cpu; i++) {
    const stray = i >= cap.cpu - used.stray_penalty;
    const on = i < used.cpu;
    pips += '<div class="pip ' + (stray ? "stray" : on ? "on" : "") + '"></div>';
  }
  pips += '<div style="width:10px"></div>';
  for (let i = 0; i < cap.gpu; i++)
    pips += '<div class="pip gpu ' + (i < used.gpu ? "on" : "") + '"></div>';
  $("pips").innerHTML = pips;
  $("capline").textContent = used.cpu + "/" + cap.cpu + " cpu, " + used.gpu + "/" +
    cap.gpu + " gpu, " + s.godot.observed + " engine(s) on the box";
  $("stamp").textContent = "updated " + new Date().toLocaleTimeString();

  $("banner").innerHTML = s.godot.unmanaged > 0
    ? '<div class="banner">' + s.godot.unmanaged + ' Godot engine(s) running outside the queue' +
      ' &mdash; capacity reduced to match. A worktree without the queue shim, or the editor.</div>'
    : "";

  if (!s.running.length) $("running").innerHTML = '<div class="empty">nothing running</div>';
  else $("running").innerHTML = '<table><tr><th>Worktree</th><th>Job</th><th>Slots</th>' +
    '<th>Elapsed</th><th style="width:180px">Progress</th><th></th></tr>' +
    s.running.map(r => {
      const el = (r.elapsed_s || 0) + drift;
      const pct = r.eta_s ? Math.min(100, 100 * el / r.eta_s) : 0;
      const over = r.eta_s && el > r.eta_s;
      return "<tr><td class='mono'>" + who(r) + "</td><td>" + job(r) + "</td>" +
        "<td class='tag'>" + (r.exclusive ? "exclusive" : r.slots + " cpu" +
          (r.gpu ? " + gpu" : "")) + "</td>" +
        "<td class='mono'>" + dur(el) + "</td>" +
        "<td>" + (r.eta_s ? '<div class="bar"><span class="' + (over ? "over" : "") +
          '" style="width:' + pct + '%"></span></div><span class="tag">~' + dur(r.eta_s) +
          "</span>" : '<span class="tag">no estimate</span>') + "</td>" +
        "<td><button onclick=\"cancel('" + r.id + "','" + job(r) + "')\">cancel</button></td></tr>";
    }).join("") + "</table>";

  if (!s.queued.length) $("queued").innerHTML = '<div class="empty">queue is empty</div>';
  else $("queued").innerHTML = '<table><tr><th>#</th><th>Worktree</th><th>Job</th>' +
    '<th>Waiting</th><th>Estimate</th><th>Blocked on</th><th></th></tr>' +
    s.queued.map(q => "<tr><td class='mono'>" + q.position + "</td><td class='mono'>" +
      who(q) + "</td><td>" + job(q) + "</td><td class='mono'>" +
      dur((q.waiting_s || 0) + drift) + "</td><td class='tag'>" +
      (q.eta_s ? "~" + dur(q.eta_s) : "--") + "</td><td class='tag'>" +
      esc(q.blocked_on) + "</td><td><button onclick=\"cancel('" + q.id + "','" +
      job(q) + "')\">cancel</button></td></tr>").join("") + "</table>";

  if (!s.history.length) $("history").innerHTML = '<div class="empty">no history yet</div>';
  else $("history").innerHTML = '<table><tr><th>Worktree</th><th>Job</th><th>Result</th>' +
    '<th>Took</th><th>Queued</th><th>Peak engines</th><th>When</th></tr>' +
    s.history.map(h => {
      let cls = "exitx", txt = h.verdict;
      if (h.verdict === "released") {
        cls = h.exit === 0 ? "exit0" : h.exit === 2 ? "exit2" : "exit1";
        txt = h.exit === 0 ? "pass" : "exit " + h.exit;
      }
      return "<tr><td class='mono'>" + esc(h.tree) + "</td><td>" + job(h) +
        "</td><td class='" + cls + "'>" + esc(txt) + "</td><td class='mono'>" +
        dur(h.dur_s) + "</td><td class='mono'>" + dur(h.queued_s) + "</td>" +
        "<td class='mono'>" + esc(h.observed_max_procs) + "</td>" +
        "<td class='tag'>" + new Date(h.finished * 1000).toLocaleTimeString() + "</td></tr>";
    }).join("") + "</table>";
}

async function refresh() {
  try {
    const r = await fetch("/state");
    last = await r.json();
    lastAt = Date.now();
    render(last);
  } catch (e) {
    $("stamp").textContent = "daemon not responding";
  }
}
refresh();
setInterval(refresh, 2000);
// Between polls the elapsed columns keep counting, so the page never looks frozen.
setInterval(() => { if (last) render(last); }, 1000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    """The bind IS the leader election, so it has to be an exclusive one.

    Windows will happily let a second process bind an address another process
    already holds unless SO_EXCLUSIVEADDRUSE is set, and it has to be set on
    the socket BEFORE bind() -- afterwards it does nothing. Two daemons sharing
    a port would each hand out the same four slots, which is worse than having
    no queue at all because everybody would believe it.
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if IS_WINDOWS:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)


def origin_path():
    """The copy of this file `start` was run from.

    Worth recording, now that more than one project can carry a client and more
    than one copy of this file can exist: "restart the daemon" is useless advice
    if you cannot tell which file the running daemon came from. `serve` runs
    from a snapshot in the runtime dir, so its own __file__ never answers this.
    """
    return os.path.join(runtime_dir(), "origin.txt")


def origin_hint():
    try:
        with open(origin_path(), "r", encoding="utf-8") as fh:
            src = fh.read().strip()
    except Exception:
        src = ""
    return "python %s start --restart" % (src or "<your testq install>/testq.py")


def tray_script():
    """Where tray.ps1 is.

    Next to this file when the CLI asks, but the daemon serves from a snapshot
    in the runtime directory and `start` copies out testq.py alone -- so the
    daemon has to go back through origin.txt to the install it came from.
    """
    beside_self = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tray.ps1")
    if os.path.exists(beside_self):
        return beside_self
    # The daemon's copy, put there by `start` and the one that survives its
    # install being a worktree that no longer exists.
    in_runtime = os.path.join(runtime_dir(), "tray.ps1")
    if os.path.exists(in_runtime):
        return in_runtime
    try:
        with open(origin_path(), "r", encoding="utf-8") as fh:
            src = fh.read().strip()
    except Exception:
        src = ""
    if src:
        beside_origin = os.path.join(os.path.dirname(src), "tray.ps1")
        if os.path.exists(beside_origin):
            return beside_origin
    return beside_self


def spawn_tray(port, auto=True):
    """Put the icon in the notification area, detached from whoever asked.

    `auto` is the daemon's: an auto-raised icon leaves again once the queue has
    been idle for a while. One asked for by hand stays until it is dismissed.

    Two icons on one port cannot happen -- tray.ps1 holds a named mutex and the
    loser exits immediately -- so a spawn against a tray that is already up
    costs one short-lived PowerShell and changes nothing.
    """
    if not IS_WINDOWS:
        return False
    script = tray_script()
    if not os.path.exists(script):
        return False
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-WindowStyle", "Hidden", "-File", script, "-Port", str(port)]
    if auto:
        cmd += ["-Auto", "-IdleExitSeconds", str(int(TRAY_IDLE_LINGER))]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            # CREATE_NO_WINDOW alone, and the "alone" is the whole point:
            # DETACHED_PROCESS and CREATE_NO_WINDOW are documented as mutually
            # exclusive, and when the pair is passed and DETACHED wins,
            # powershell.exe comes up with no console for its host to sit in and
            # exits 0 without executing a line of the script. Silently: no
            # window, no error, no icon, and a spawn that looks like it worked
            # from here. CREATE_NO_WINDOW gives it a console nobody can see,
            # which is what was wanted, and the child outlives us either way.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True)
    except Exception:
        return False
    return True


def source_sha():
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:8]
    except Exception:
        return "unknown"


def cmd_serve(args):
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.normcase(here) != os.path.normcase(runtime_dir()) and not args.allow_worktree:
        sys.stderr.write(
            "refusing to serve from %s\n"
            "The daemon must run from %s so that editing or deleting a worktree\n"
            "cannot tear a running daemon out from under itself. Use:\n"
            "    python %s start\n" % (here, runtime_dir(), os.path.abspath(__file__))
        )
        return 2

    port = port_from_env(args.port)
    server = None
    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError:
        # Somebody else got there first. That IS the leader election; there is
        # nothing to clean up and nothing to complain about.
        sys.stderr.write("testq: port %d already held -- another daemon is live\n" % port)
        return 0

    os.makedirs(runtime_dir(), exist_ok=True)
    moved = import_legacy_history()
    if moved:
        sys.stderr.write("imported %d row(s) from the old JSONL history\n" % moved)
    queue = Queue()
    queue.sha = source_sha()
    queue.port = port
    stopping = threading.Event()
    Handler.queue = queue
    Handler.stopping = stopping

    def ticker():
        while not stopping.is_set():
            try:
                queue.tick()
            except Exception as exc:
                sys.stderr.write("tick failed: %r\n" % (exc,))
            stopping.wait(TICK_SECONDS)

    threading.Thread(target=ticker, daemon=True).start()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5},
                     daemon=True).start()
    sys.stderr.write("testq %s listening on 127.0.0.1:%d\n" % (queue.sha, port))
    sys.stderr.flush()
    try:
        stopping.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    queue.save_state()
    return 0


def http_json(port, path, payload=None, timeout=5.0):
    """Tiny HTTP client so the CLI has no dependency on curl either."""
    import urllib.request
    import urllib.error
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except Exception:
        return None, None


def daemon_up(port):
    code, _ = http_json(port, "/healthz", timeout=2.0)
    return code == 200


def cmd_start(args):
    port = port_from_env(args.port)
    if daemon_up(port):
        if not args.restart:
            code, ver = http_json(port, "/version")
            sha = ver.get("sha") if isinstance(ver, dict) else "?"
            print("testq already running on %d (sha %s)" % (port, sha))
            return 0
        http_json(port, "/quit", {"force": bool(args.force)})
        for _ in range(40):
            if not daemon_up(port):
                break
            time.sleep(0.25)
        else:
            print("the running daemon would not stop; it still has live jobs "
                  "(add --force to take the box out from under them)")
            return 1

    os.makedirs(runtime_dir(), exist_ok=True)
    sha = source_sha()
    snapshot = os.path.join(runtime_dir(), "testq-%s.py" % sha)
    if not os.path.exists(snapshot):
        shutil.copyfile(os.path.abspath(__file__), snapshot)
    with open(os.path.join(runtime_dir(), "current.txt"), "w", encoding="utf-8") as fh:
        fh.write(os.path.basename(snapshot))
    with open(origin_path(), "w", encoding="utf-8") as fh:
        fh.write(os.path.abspath(__file__))
    # tray.ps1 goes out with it. The daemon raises the icon itself now, and it
    # is served from the runtime directory precisely because the file it was
    # started from may be a worktree that gets deleted mid-afternoon -- which
    # would otherwise cost every later run its icon. Never fatal: no icon is a
    # worse day than no queue by a wide margin.
    try:
        beside = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tray.ps1")
        if os.path.exists(beside):
            shutil.copyfile(beside, os.path.join(runtime_dir(), "tray.ps1"))
    except Exception as exc:
        sys.stderr.write("could not copy tray.ps1 to the runtime dir: %r\n" % (exc,))

    flags = 0
    if IS_WINDOWS:
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    logfh = open(log_path(), "a", encoding="utf-8")
    logfh.write("\n--- start %s sha %s port %d ---\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), sha, port))
    logfh.flush()
    env = dict(os.environ)
    env["TESTQ_PORT"] = str(port)
    subprocess.Popen(
        [sys.executable, snapshot, "serve", "--port", str(port)],
        stdout=logfh, stderr=logfh, stdin=subprocess.DEVNULL,
        creationflags=flags, close_fds=True, env=env,
        cwd=runtime_dir(),
    )
    for _ in range(40):
        if daemon_up(port):
            print("testq %s listening on http://localhost:%d/" % (sha, port))
            return 0
        time.sleep(0.25)
    print("testq did not come up within 10s -- see %s" % log_path())
    return 1


def fmt_dur(s):
    if s is None:
        return "--"
    s = int(round(s))
    if s < 60:
        return "%ds" % s
    return "%dm%02ds" % (s // 60, s % 60)


def cmd_status(args):
    port = port_from_env(args.port)
    code, state = http_json(port, "/state")
    if code != 200 or not isinstance(state, dict):
        print("testq is not running on port %d" % port)
        return 1
    used, cap = state["used"], state["capacity"]
    print("testq  %d/%d cpu  %d/%d gpu  %d engine(s) on the box%s"
          % (used["cpu"], cap["cpu"], used["gpu"], cap["gpu"],
             state["godot"]["observed"],
             "  (%d unmanaged)" % state["godot"]["unmanaged"]
             if state["godot"]["unmanaged"] else ""))
    for r in state["running"]:
        print("  RUN   %-34s %-16s %8s / %-8s %s"
              % (r["tree_id"][:34], (r["script"] + " " + (r["arg"] or "")).strip(),
                 fmt_dur(r.get("elapsed_s")), fmt_dur(r.get("eta_s")), r["id"]))
    for q in state["queued"]:
        print("  WAIT %d %-34s %-16s waiting %-8s %s"
              % (q["position"], q["tree_id"][:34],
                 (q["script"] + " " + (q["arg"] or "")).strip(),
                 fmt_dur(q.get("waiting_s")), q.get("blocked_on", "")))
    if not state["running"] and not state["queued"]:
        print("  idle")
    return 0


def cmd_tray(args):
    """Put the icon in the notification area and keep it there.

    The daemon raises the icon by itself whenever the box is busy, so this is
    now for pinning it up permanently -- an icon asked for by hand does not
    take itself away when the queue goes quiet.
    """
    port = port_from_env(args.port)
    script = tray_script()
    if not os.path.exists(script):
        print("tray.ps1 is missing next to testq.py")
        return 1
    if not IS_WINDOWS:
        print("the tray icon is Windows-only; the page works anywhere")
        return 1
    if args.stop:
        # The icon's own message loop owns its lifetime, so stopping it means
        # stopping the host process; "Hide this icon" on the menu is the
        # civilised route and this is the one for scripts.
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\""
             " | Where-Object { $_.CommandLine -like '*tray.ps1*' }"
             " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("tray icon stopped")
        return 0
    if not spawn_tray(port, auto=False):
        print("could not start the tray icon")
        return 1
    print("tray icon pinned -- look in the notification overflow area (^).")
    print("Drag it onto the taskbar to keep it visible.")
    return 0


def cmd_stats(args):
    """What the box has actually been doing, across every session.

    Reads the database directly rather than going through the daemon, so it
    answers even when nothing is running.
    """
    since = now() - args.days * 86400.0
    where = "finished > ?"
    params = [since]
    if args.tree:
        where += " AND tree LIKE ?"
        params.append("%" + args.tree + "%")

    with db() as conn:
        # Engine-hours weight each run by how many engines it booked; rows
        # from before the history writer carried `engines` (or imported from
        # the legacy JSONL) hold 0 there, so they count as one engine, which
        # is what most of them were.
        total = conn.execute(
            "SELECT COUNT(*) n, SUM(dur_s) ran, SUM(queued_s) waited,"
            "       SUM(dur_s * MAX(COALESCE(engines, 1), 1)) eng"
            " FROM runs WHERE " + where, params).fetchone()
        if not total["n"]:
            print("no runs recorded in the last %d day(s)" % args.days)
            return 0
        print("last %d day(s): %d run(s), %.1f h of box time, %.1f engine-hours,"
              " %.1f h spent queueing"
              % (args.days, total["n"], (total["ran"] or 0) / 3600.0,
                 (total["eng"] or 0) / 3600.0, (total["waited"] or 0) / 3600.0))

        print("\nby job:")
        print("  %-22s %5s %7s %7s %7s  %s"
              % ("job", "runs", "median", "worst", "queued", "outcome"))
        rows = conn.execute(
            "SELECT script, arg, COUNT(*) n,"
            "       SUM(CASE WHEN verdict='released' AND exit=0 THEN 1 ELSE 0 END) ok,"
            "       SUM(CASE WHEN verdict='released' AND exit<>0 THEN 1 ELSE 0 END) bad,"
            "       SUM(CASE WHEN verdict<>'released' THEN 1 ELSE 0 END) other,"
            "       AVG(dur_s) avg, MAX(dur_s) worst, SUM(queued_s) queued"
            " FROM runs WHERE " + where +
            " GROUP BY script, arg ORDER BY SUM(dur_s) DESC LIMIT ?",
            params + [args.limit]).fetchall()
        for r in rows:
            label = ("%s %s" % (r["script"], r["arg"] or "")).strip()
            outcome = "%d passed" % r["ok"]
            if r["bad"]:
                outcome += ", %d failed" % r["bad"]
            if r["other"]:
                outcome += ", %d cancelled/lost" % r["other"]
            print("  %-22s %5d %7s %7s %7s  %s"
                  % (label[:22], r["n"], fmt_dur(r["avg"]), fmt_dur(r["worst"]),
                     fmt_dur(r["queued"]), outcome))

        print("\nby worktree:")
        rows = conn.execute(
            "SELECT tree, COUNT(*) n,"
            "       SUM(dur_s * MAX(COALESCE(engines, 1), 1)) ran,"
            "       SUM(queued_s) waited,"
            "       MAX(observed_max_procs) peak"
            " FROM runs WHERE " + where +
            " GROUP BY tree ORDER BY ran DESC LIMIT ?",
            params + [args.limit]).fetchall()
        for r in rows:
            print("  %-38s %4d run(s)  %8s engine time  %8s queued  peak %d engine(s)"
                  % ((r["tree"] or "?")[:38], r["n"], fmt_dur(r["ran"]),
                     fmt_dur(r["waited"]), r["peak"] or 0))

        # A job that fails sometimes and passes other times is the expensive
        # kind of problem here, so it gets called out by name rather than left
        # for someone to notice in the pass/fail columns above.
        #
        # Within ONE worktree, though. Aggregated across trees this cried
        # wolf: every worktree here is a branch mid-development, so a job red
        # thirteen times in the branch that broke it and green everywhere else
        # is development doing its job, not flake. (A row this table once
        # showed "failing 42% of the time" was byte-for-byte deterministic at
        # HEAD -- every one of its failures belonged to two feature branches.)
        # A job that flips within a single checkout has no such excuse.
        rows = conn.execute(
            "SELECT script, arg, tree, COUNT(*) n,"
            "       SUM(CASE WHEN exit=0 THEN 1 ELSE 0 END) ok"
            " FROM runs WHERE " + where + " AND verdict='released'"
            " GROUP BY script, arg, tree HAVING ok > 0 AND ok < n",
            params).fetchall()
        if rows:
            print("\nsometimes passing, sometimes not, in one worktree:")
            for r in rows:
                print("  %-22s %-30s %d of %d passed"
                      % (("%s %s" % (r["script"], r["arg"] or "")).strip()[:22],
                         (r["tree"] or "?")[:30], r["ok"], r["n"]))
    return 0


def cmd_stop(args):
    port = port_from_env(args.port)
    code, body = http_json(port, "/quit", {"force": bool(args.force)})
    if code is None:
        print("testq is not running on port %d" % port)
        return 0
    if code == 409:
        print("%d job(s) still running; use --force to stop anyway"
              % body.get("running", 0))
        return 1
    print("testq stopping")
    return 0


# ---------------------------------------------------------------------------
# reap
# ---------------------------------------------------------------------------

def tree_id_for(path):
    """The same key run_lib.sh computes, in Python.

    run_lib.sh hashes `cygpath -m "$PROJ"` -- the path as GODOT would write it,
    C:/Users/... with forward slashes -- and main.gd hashes the same string
    from globalize_path(). All three have to agree or the scratch directories
    stop lining up.
    """
    native = os.path.abspath(path).replace("\\", "/").rstrip("/")
    if len(native) > 1 and native[1] == ":":
        native = native[0].upper() + native[1:]
    digest = hashlib.md5(native.encode("utf-8")).hexdigest()[:6]
    return "%s-%s" % (os.path.basename(native), digest)


def live_tree_ids(root):
    ids = set()
    if os.path.isdir(root):
        ids.add(tree_id_for(root))
    wt = os.path.join(root, ".claude", "worktrees")
    if os.path.isdir(wt):
        for name in os.listdir(wt):
            full = os.path.join(wt, name)
            if os.path.isdir(full):
                ids.add(tree_id_for(full))
    return ids


def reap_candidates(name, entry):
    """Scratch under this project's known directories whose worktree is gone.

    Registered projects only, and only the directories the registry names. A
    reaper that guesses where a project keeps its scratch is a reaper that
    deletes somebody's work, so an unconfigured project simply has nothing to
    collect rather than a default worth arguing about.
    """
    root = expand(entry.get("root"))
    if not root or not os.path.isdir(root):
        print("  %s: root %s is not there -- skipping" % (name, root or "(unset)"))
        return []
    live = live_tree_ids(root)
    if not live:
        print("  %s: no worktrees under %s -- refusing to delete anything" % (name, root))
        return []
    print("  %s: %d live worktree(s)" % (name, len(live)))

    found = []
    userdata = expand(entry.get("userdata"))
    parents = [expand(entry.get("scratch"))]
    if userdata:
        parents += [os.path.join(userdata, sub)
                    for sub in (entry.get("scratch_subdirs") or ["clips", "shots"])]
    for parent in parents:
        if not parent or not os.path.isdir(parent):
            continue
        for leaf in os.listdir(parent):
            if leaf not in live:
                found.append(os.path.join(parent, leaf))
    wdir = weights_dir(name)
    if wdir and os.path.isdir(wdir):
        for leaf in os.listdir(wdir):
            m = re.match(r"^weights-(.+)\.json$", leaf)
            if m and m.group(1) not in live:
                found.append(os.path.join(wdir, leaf))
    return found


def cmd_reap(args):
    projects = load_projects()
    if not projects:
        print("no projects registered in %s -- nothing to reap" % projects_path())
        return 1
    if args.project:
        projects = {k: v for k, v in projects.items() if k == args.project}
        if not projects:
            print("no project called %r is registered" % args.project)
            return 1

    candidates = []
    for name, entry in sorted(projects.items()):
        candidates.extend(reap_candidates(name, entry))

    if not candidates:
        print("nothing stale to clean up")
        return 0
    total = 0
    for path in candidates:
        size = 0
        if os.path.isdir(path):
            for dirpath, _, files in os.walk(path):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
        else:
            try:
                size = os.path.getsize(path)
            except OSError:
                pass
        total += size
        print("  %8.1f MB  %s" % (size / 1e6, path))
    print("%d item(s), %.1f MB" % (len(candidates), total / 1e6))
    if not args.yes:
        print("\ndry run -- nothing deleted. Re-run with --yes to remove these.")
        return 0
    for path in candidates:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError as exc:
            print("  could not remove %s: %s" % (path, exc))
    print("removed.")
    return 0


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="testq", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=None)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="run the server (use `start` instead)")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--allow-worktree", action="store_true",
                   help="permit serving from a worktree path (debugging only)")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("start", help="snapshot and launch the daemon")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--restart", action="store_true", help="replace a running daemon")
    p.add_argument("--force", action="store_true", help="restart even with live jobs")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("status", help="what the box is doing")
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("stop", help="stop the daemon")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("tray", help="show the icon in the notification area")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--stop", action="store_true")
    p.set_defaults(fn=cmd_tray)

    p = sub.add_parser("stats", help="what the box has been doing, from the database")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--tree", default="", help="only worktrees matching this")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("reap", help="delete scratch from worktrees that are gone")
    p.add_argument("--yes", action="store_true", help="actually delete")
    p.add_argument("--project", default="", help="only this registered project")
    p.set_defaults(fn=cmd_reap)

    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
