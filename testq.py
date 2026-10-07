#!/usr/bin/env python3
"""testq -- one queue for every Godot run on this machine.

The problem this exists for: there is one box, one GPU and eight slots' worth
of admission (a policy, not a measured ceiling -- see CAPACITY below),
but several projects and a couple of dozen worktrees between them, each of which
thinks it is alone on it. Two sessions starting `run_test_par.sh 4` at the same
moment put eight engines on a box neither of them measured, and everything that
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

Tests: `python -m unittest discover tests`. They start nothing.
"""

import base64
import ctypes
import hashlib
import http.client
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

# Eight, and an admission policy rather than a measured ceiling. This was four
# on the strength of run_test_par.sh's header, which had 6 and 8 shards each
# running 1.5-1.7x slower than 4. Retaken under the queue on a quiet box that
# did not survive: eight shards ran the mfrs suite 1.6x faster than four
# (152 s -> 94 s), and the old slowdown was other worktrees' engines. The box
# is 16 cores and 24 threads.
CAPACITY = {"cpu": 8, "gpu": 2}

# One card, two windows on it at once. It was one, and that was the queue's
# worst line: in a week, GPU jobs ran for 17 hours and queued for 44, 34 of
# them behind another window -- 14 behind their own worktree's. The card was
# never the constraint. Three windowed engines leave an RTX 3080 at a quarter
# busy; what they cost is memory, about 1.6 GB each of 12, on a desktop that
# already holds several. So two, not three. A job whose numbers need the card
# to itself books both (run_perf does); an ask for more than there is means
# all of it.

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

# What the tray raises a balloon for. It was every exit that was not 0, and on
# 6 October that was 482 of them: an agent tuning a camera test ran it red
# nine times in a row on the way to green, and each one came up on the
# desktop as "failed". A test that says no is the test working, and its owner
# has the output in front of it. So a balloon is for the run that never got to
# give a verdict -- killed here, or an engine that died under it -- and an
# ordinary failure is the red dot and a line on the page.
#
# A crash is read from the exit code, which is all there is: an NTSTATUS error
# from a Windows process, or 128 plus the signal from one that bash waited on.
# Ctrl+C is in the NTSTATUS error range and is somebody stopping a run.
NTSTATUS_ERROR = 0xC0000000
NTSTATUS_CONTROL_C = 0xC000013A
NTSTATUS_NAMES = {
    0xC0000005: "access violation",
    0xC000001D: "illegal instruction",
    0xC0000094: "divide by zero",
    0xC00000FD: "stack overflow",
    0xC0000135: "a DLL is missing",
    0xC0000142: "a DLL failed to load",
    0xC0000374: "heap corruption",
    0xC0000409: "abort",
}
# Not 137 or 143: those are somebody's kill, and 124 to 127 are timeout(1) and
# the shell failing to start the thing at all.
CRASH_SIGNALS = {132: "SIGILL", 134: "SIGABRT", 135: "SIGBUS", 136: "SIGFPE",
                 139: "SIGSEGV"}


def alert_for(exit_code, verdict):
    """What to interrupt somebody with about a finished run, or ""."""
    if verdict in ("stalled", "overran"):
        return "killed: " + verdict
    if verdict != "released" or isinstance(exit_code, bool) \
            or not isinstance(exit_code, int):
        return ""
    # A client that read the code as signed sends the same crash negative.
    code = exit_code & 0xFFFFFFFF
    if code >= NTSTATUS_ERROR and code != NTSTATUS_CONTROL_C:
        return "crashed: %s" % NTSTATUS_NAMES.get(code, "0x%08X" % code)
    if exit_code in CRASH_SIGNALS:
        return "crashed: %s" % CRASH_SIGNALS[exit_code]
    return ""

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

# An engine outside the queue docks a slot because it is load, and one that
# finished its script and never exited is not. Twenty-three headless probes
# left behind by one session, at no cpu at all for half an hour, docked every
# slot on the box and held that same session's four queued jobs for sixteen
# minutes. So an outside engine that has burned under this much of a core for
# this long is listed and not counted. It counts again on the first reading
# that shows it working.
IDLE_STRAY_SECONDS = 120.0
IDLE_STRAY_CORES = 0.1

# A working one used to dock a slot each, off the eight, and that was the
# queue's biggest cost by a distance: in a day and a half, three fifths of all
# queueing was jobs waiting with slots free on paper, and twice the box sat
# empty for most of an hour granting nothing because eight engines somebody
# had started by hand were on it. Jobs that time out in the queue get run
# outside it, which is more of the same.
#
# The eight is an admission policy on a box with sixteen cores, so the first
# few outside engines are paid for out of the cores the policy leaves over,
# and only the ones past that come off the slots. Not all eight of the spare
# cores: the agents, the recorders and the desktop live there too. And however
# many there are, the queue keeps a floor it can always grant -- a slow queue
# is a nuisance, a stopped one sends its jobs round the outside.
#
# This is about throughput, not about what a run may assert. box_engines
# still counts every outside engine, and an exclusive job still wants none.
STRAY_FREE = 4
STRAY_FLOOR = 2

# A run whose engine finished its work and then never exited keeps its lease
# for as long as its shell lives, and reap() only ever looks at the shell. A
# 48-second capture held the GPU for two hours that way, with three jobs
# polling behind it and five more giving up.
#
# Being late is not the test. The history has honest runs at fifty times their
# estimate -- the estimate is keyed on script and arg, and a capture's arg does
# not say how many seconds it was asked to record. So lateness only makes a
# lease a suspect: past max(floor, factor x its estimate), or the flat ceiling
# when there is no estimate. What convicts it is its processes doing nothing --
# the same set of pids burning under half a core between them for this long. A
# hung windowed engine idles at about a fifth of a core; a working one does not
# get under one.
STALL_FLOOR_SECONDS = 600.0
STALL_ETA_FACTOR = 3.0
STALL_UNKNOWN_SECONDS = 1800.0
STALL_SAMPLE_SECONDS = 60.0       # a process table costs a second of PowerShell
STALL_QUIET_SECONDS = 300.0
STALL_IDLE_CORES = 0.5

# Idle is not the only way to hang. A run spinning in a loop burns a core and
# looks like work for ever; two leases have held their slots for 3.5 and 1.9
# hours that way before their shells died. So there is also a flat ceiling,
# and it is deliberately far out: the longest honest run in three thousand is
# twenty minutes. A client that knows better says so with `max_s`.
CEILING_SECONDS = 3600.0
CEILING_ETA_FACTOR = 4.0

# A queued client that dies loses its ticket within a minute, and its retry
# used to start again at the back -- which is how a session whose command timed
# out after ten minutes in the queue came to wait twenty, or gave up and ran
# outside it. The same job from the same worktree inside this window gets its
# waiting time back.
PARK_SECONDS = 900.0

# What a job that was killed here is told the next time its worktree asks for
# anything, for this long. The kill itself cannot say: its reader is dead.
NOTE_SECONDS = 1800.0

# Replacing the daemon without anybody noticing: see Handover, below. The old daemon
# gives requests already in its hands this long to finish before it lets go --
# all but a cancel are over in milliseconds, and a cancel is a kill of several
# passes -- and the new one this long to say it has everything. Past either,
# the old daemon carries on as if nobody had asked.
HANDOVER_DRAIN_SECONDS = 20.0
HANDOVER_CONFIRM_SECONDS = 15.0

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

IS_WINDOWS = os.name == "nt"


RUNTIME_NAME = "testq"
LEGACY_RUNTIME_NAME = "mfrs-testq"


def runtime_base():
    return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")


def runtime_dir():
    """Where the daemon actually lives, which is never inside a worktree.

    A worktree gets deleted, or its copy of this file gets edited while the
    daemon is mid-flight. Both are routine here and both would be fatal to a
    daemon executing from that path, so `start` copies the file out and the
    server refuses to run from anywhere else.

    The directory was `mfrs-testq` while this lived in one project, and
    stayed that for as long as a project might still carry its own copy of
    this file: either copy could win the port bind, and they had to agree on
    where the history was. None does now, so `start` moves it (see
    migrate_runtime_dir). The old name is still read when it is all there
    is -- a move that could not be made yet -- and once the new one exists
    it wins, so that nothing recreating the old directory can split the
    queue's memory in two.
    """
    override = os.environ.get("TESTQ_HOME")
    if override:
        return override
    home = os.path.join(runtime_base(), RUNTIME_NAME)
    legacy = os.path.join(runtime_base(), LEGACY_RUNTIME_NAME)
    if not os.path.isdir(home) and os.path.isdir(legacy):
        return legacy
    return home


def migrate_runtime_dir():
    """Move the runtime directory from its first name to its own, once. The
    new path if it moved, "" if there was nothing to do or it could not.

    Only ever called by `start` with no daemon running, which is the one
    moment nothing has the database open.

    A rename when the directory will let go, and it often will not: the tray
    icon is a PowerShell that was started in there and holds it for as long
    as the icon is up. So failing that it is copied -- to one side first and
    renamed into place, so that a copy interrupted half way is never taken
    for the directory -- and the old one is left behind with a note in it.
    Nothing reads it again once the new one exists.
    """
    if os.environ.get("TESTQ_HOME"):
        return ""
    home = os.path.join(runtime_base(), RUNTIME_NAME)
    legacy = os.path.join(runtime_base(), LEGACY_RUNTIME_NAME)
    if os.path.exists(home) or not os.path.isdir(legacy):
        return ""
    try:
        os.rename(legacy, home)
        return home
    except OSError:
        pass
    moving = home + ".moving"
    try:
        shutil.rmtree(moving, ignore_errors=True)
        shutil.copytree(legacy, moving)
        os.rename(moving, home)
    except (OSError, shutil.Error) as exc:
        sys.stderr.write("could not move %s to %s: %r\n" % (legacy, home, exc))
        shutil.rmtree(moving, ignore_errors=True)
        return ""
    try:
        with open(os.path.join(legacy, "MOVED.txt"), "w", encoding="utf-8") as fh:
            fh.write("testq's runtime directory moved to %s on %s.\n"
                     "This copy is no longer read and can be deleted.\n"
                     % (home, time.strftime("%Y-%m-%d")))
    except OSError:
        pass
    return home


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


MIGRATED = []


class db(object):
    """`with db() as conn:` -- an open, initialised, committing connection."""

    def __enter__(self):
        os.makedirs(runtime_dir(), exist_ok=True)
        self.conn = sqlite3.connect(db_path(), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        if not MIGRATED:
            # Added after the table had three thousand rows in it. The other
            # copy of this file on the box names its columns when it inserts,
            # so it neither sees this one nor minds it.
            for column in ("size REAL", "waited_on TEXT"):
                try:
                    self.conn.execute("ALTER TABLE runs ADD COLUMN " + column)
                except sqlite3.OperationalError:
                    pass
            MIGRATED.append(True)
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


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_WINAPI = []


def winapi():
    """(kernel32, ntdll) with prototypes declared. Handles and addresses are
    pointer-sized, and ctypes' default of a C int truncates both."""
    if not _WINAPI:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        void_p, ulong = ctypes.c_void_p, ctypes.c_ulong
        k32.OpenProcess.restype = void_p
        k32.OpenProcess.argtypes = [ulong, ctypes.c_int, ulong]
        k32.CloseHandle.argtypes = [void_p]
        k32.ReadProcessMemory.argtypes = [void_p, void_p, void_p,
                                          ctypes.c_size_t, void_p]
        k32.GetProcessTimes.argtypes = [void_p] * 5
        k32.CreateToolhelp32Snapshot.restype = void_p
        k32.CreateToolhelp32Snapshot.argtypes = [ulong, ulong]
        k32.Process32FirstW.argtypes = [void_p, void_p]
        k32.Process32NextW.argtypes = [void_p, void_p]
        ntdll.NtQueryInformationProcess.argtypes = [
            void_p, ctypes.c_int, void_p, ulong, void_p]
        _WINAPI.extend([k32, ntdll])
    return _WINAPI[0], _WINAPI[1]


def process_times(pid):
    """(cpu seconds so far, creation time) of a live process; zeros for one
    that will not open, which is the system's own and nothing we weigh."""
    k32, _ = winapi()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return 0.0, 0
    try:
        created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
        if not k32.GetProcessTimes(handle, ctypes.byref(created),
                                   ctypes.byref(exited), ctypes.byref(kernel),
                                   ctypes.byref(user)):
            return 0.0, 0
        # Both in 100 ns units.
        return (kernel.value + user.value) / 1e7, int(created.value)
    finally:
        k32.CloseHandle(handle)


def process_table():
    """pid -> (parent pid, image name, command line, cpu seconds so far) for
    everything on the box. {} if the box will not say.

    The command line is only filled in for engines. It is the one field that
    has to be read out of the process's own memory, nothing here looks at any
    other process's, and six hundred of those reads a tick would be the most
    expensive thing the daemon does.

    Straight from the kernel, not through WMI. This used to be PowerShell and
    Get-CimInstance, and tasklist for the engine count -- a second apiece, and
    on a loaded box WMI answers "Call cancelled" for minutes at a time. For
    those minutes the daemon counted no engines on a box carrying eight, which
    is the wrong direction to be wrong in: it is what tells run_mp the box is
    quiet.

    A parent pid is only a number, and Windows hands numbers out again. A
    process older than its "parent" was not started by it; that link is cut
    here so that no walk down from a shell collects a stranger.
    """
    if not IS_WINDOWS:
        return {}
    try:
        k32, _ = winapi()
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return {}
        rows = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            more = k32.Process32FirstW(snap, ctypes.byref(entry))
            while more:
                rows.append((int(entry.th32ProcessID),
                             int(entry.th32ParentProcessID), entry.szExeFile))
                more = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        table, born = {}, {}
        for pid, ppid, name in rows:
            cpu, born[pid] = process_times(pid)
            cmdline = ""
            if GODOT_MATCH in name.lower():
                cmdline = peb_string(pid, PEB_COMMAND_LINE)
            table[pid] = (ppid, name, cmdline, cpu)
        for pid, (ppid, name, cmdline, cpu) in list(table.items()):
            if born.get(ppid) and born[pid] and born[ppid] > born[pid]:
                table[pid] = (0, name, cmdline, cpu)
        return table
    except Exception:
        return {}


def subtree(table, root):
    """`root` and everything under it in `table`, parents before children."""
    found = [root]
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        for child, row in table.items():
            if row[0] == parent and child not in found:
                found.append(child)
                frontier.append(child)
    return found


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


def norm_path(path):
    """One spelling for a path the shell writes /c/Users/..., the engine
    reports C:\\Users\\... and a client sends C:/Users/..."""
    path = (path or "").strip().strip('"').replace("\\", "/").lower()
    if re.match(r"^/[a-z]/", path):
        path = path[1] + ":" + path[2:]
    return path.rstrip("/")


# Offsets into RTL_USER_PROCESS_PARAMETERS of two UNICODE_STRINGs -- a byte
# length, padding, and a pointer to the text. The 64-bit layout, which has not
# moved since Vista.
PEB_CURRENT_DIRECTORY = 0x38
PEB_COMMAND_LINE = 0x70


def peb_string(pid, offset):
    """One of a live process's startup strings, or "" if it will not say.

    Read out of the process's own memory, because nothing politer exists: no
    Windows API reports another process's directory at all, and the only one
    that reports its command line is WMI (see process_table for why not). Any
    failure is "" -- an engine with no readable command line or directory is
    one nobody can place, and callers already have to cope with those.
    """
    if not IS_WINDOWS:
        return ""
    try:
        k32, ntdll = winapi()
        # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
        handle = k32.OpenProcess(0x0410, False, int(pid))
    except Exception:
        return ""
    if not handle:
        return ""
    try:
        def read(address, size):
            buf = ctypes.create_string_buffer(size)
            if not address or not k32.ReadProcessMemory(handle, address, buf,
                                                        size, None):
                raise OSError("unreadable")
            return buf.raw

        def pointer(address):
            return int.from_bytes(read(address, 8), "little")

        # PROCESS_BASIC_INFORMATION: the PEB's address is its second pointer.
        info = (ctypes.c_void_p * 6)()
        if ntdll.NtQueryInformationProcess(handle, 0, info,
                                           ctypes.sizeof(info), None):
            return ""
        params = pointer(int(info[1] or 0) + 0x20)     # PEB.ProcessParameters
        length = int.from_bytes(read(params + offset, 2), "little")
        if not length:
            return ""
        text = read(pointer(params + offset + 8), length)
        return text.decode("utf-16-le", "replace")
    except Exception:
        return ""
    finally:
        k32.CloseHandle(handle)


def process_cwd(pid):
    return peb_string(pid, PEB_CURRENT_DIRECTORY)


ENGINE_PATH = re.compile(r'--path[ =]+(?:"([^"]+)"|(\S+))')


def engine_in_tree(cmdline, tree_path, cwd=""):
    """Whether the engine with this command line is running out of the
    worktree at `tree_path`: True, False, or None when it cannot be told.

    Worktrees nest. Every mfrs worktree lives under the main checkout, at
    mfrs/.claude/worktrees/<name>, so "the engine's project is under this
    directory" is true of the main checkout for every engine in every one of
    them -- and the leaf name, which is all this used to compare, is worse
    still. An engine below a `worktrees` directory inside the tree belongs to
    that inner worktree and not to this one.

    None is an engine started with a relative `--path`, or none, when `cwd`
    is not there to resolve it against: it is in somebody's worktree and its
    command line does not say whose. So is every engine, to a job whose client
    never said which worktree it runs in. Callers have to pick a side for
    those, and they do not pick the same one.
    """
    found = ENGINE_PATH.search(cmdline or "")
    project = norm_path(found.group(1) or found.group(2)) if found else ""
    if cwd and not re.match(r"^[a-z]:/", project):
        # Not joined to the relative path: Godot changes directory into its
        # project as it starts, so `cwd` already is the project -- or, for the
        # first instant, the directory it was launched from. Both are inside
        # the worktree, which is all that is being asked.
        project = norm_path(cwd)
    tree = norm_path(tree_path)
    if not tree or not re.match(r"^[a-z]:/", project):
        return None
    if project == tree:
        return True
    if not project.startswith(tree + "/"):
        return False
    return "/worktrees/" not in project[len(tree):] + "/"


def engine_of(pid, cmdline, tree_path):
    """engine_in_tree for a live engine, asking the process where it is when
    its command line alone does not say. prognosticator's harnesses all start
    theirs with `--path godot`."""
    placed = engine_in_tree(cmdline, tree_path)
    if placed is None and tree_path:
        placed = engine_in_tree(cmdline, tree_path, process_cwd(pid))
    return placed


def kill_job(pid, tree_path="", spare=()):
    """Stop a run: its shell, everything under it, and its engines.

    `taskkill /F /T` is not enough on its own, and this was worth finding out
    the hard way -- killing a run's bash left `timeout.exe` and both Godot
    engines alive and still grinding through `--test=all` twenty minutes later.
    Git-Bash's `timeout` has no living Windows parent even while the shell is
    up, so a descendant walk from the shell never reaches a bash client's
    engines at all.

    So there are two passes. The first walks the real parent links and kills
    the subtree, which is precise and is the whole run for a Node client. The
    second sweeps up the engines the walk cannot reach, matched on the worktree
    in their `--path` argument -- see engine_in_tree for what "in" has to mean
    when worktrees nest.

    The sweep tests the IMAGE NAME for Godot and the COMMAND LINE for the
    worktree, and needs both. Testing the command line for Godot instead looks
    equivalent and is not: it also matches every shell whose command line
    happens to mention the engine, which on this box includes the editor's own
    terminals. An early version of this would have killed them.

    `spare` is the shells of the other running jobs. One worktree can hold two
    leases -- a suite and a clip -- and the sweep cannot tell their engines
    apart by path, so whatever is under another lease's shell is left alone.

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
            victims.extend(subtree(table, root))
        elif root and attempt == 0:
            victims.append(root)

        if tree_path:
            spared = set()
            for other in spare:
                spared.update(subtree(table, other))
            for candidate, (_, name, cmdline, _) in table.items():
                if candidate in victims or candidate in spared:
                    continue
                if (GODOT_MATCH in name.lower()
                        and engine_of(candidate, cmdline, tree_path)):
                    victims.append(candidate)

        if not victims:
            break
        # Children before parents, so a shell cannot notice its child died and
        # start another one.
        for victim in reversed(victims):
            taskkill(victim)
            killed.append("%d:%s" % (victim, table.get(victim, (0, "?", "", 0.0))[1]))
        time.sleep(1.0)

    # Logged because a cancel that silently fails to stop the engines is the
    # worst outcome here: the slots come back, the page says the job is gone,
    # and the box is still busy. daemon.log is where that shows up.
    sys.stderr.write("kill_job(pid=%s, tree=%s): killed %s\n"
                     % (pid, tree_path or "?", ", ".join(killed) or "nothing"))
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


def is_engine(name):
    name = name.lower()
    return GODOT_MATCH in name and GODOT_EXCLUDE not in name


def count_godot(table=None):
    """Engines on the box, or None when the box would not say -- which is not
    the same as none, and the caller must not treat it as none."""
    if table is None:
        table = process_table()
    if not table:
        return None
    return sum(1 for row in table.values() if is_engine(row[1]))


def tree_label(path):
    """A project path as short as it can be and still say whose it is:
    `mfrs/weekend-features` for a worktree, the last two directories for
    anything else."""
    path = norm_path(path)
    nested = re.search(r"/([^/]+)/(?:[^/]+/)?worktrees/([^/]+)", path)
    if nested:
        return nested.group(1) + "/" + nested.group(2)
    return "/".join(path.split("/")[-2:])


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


SIZED_ARG = re.compile(r"^(\d+(?:\.\d+)?)\s+(\S.*)$")


def arg_size(arg, size=None):
    """(kind, size) for a job: what it is doing and how much of it.

    A client that knows says so in `size` and the arg is the kind. One that
    does not may still have written it into the arg -- "148 scenarios" -- and
    then the number is the size and the rest is the kind.
    """
    try:
        if size is not None and float(size) > 0:
            return arg or "", float(size)
    except (TypeError, ValueError):
        pass
    found = SIZED_ARG.match(arg or "")
    if found:
        return found.group(2), float(found.group(1))
    return arg or "", None


# ---------------------------------------------------------------------------
# The queue itself
# ---------------------------------------------------------------------------

class Queue(object):
    def __init__(self, capacity=None, inherited=None):
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
        # lease id -> the last cpu reading of its processes, for rescue_stalled.
        # Not on the lease itself: leases are saved, and a reading from before
        # a restart says nothing about the minute just gone.
        self.stall_samples = {}
        # (tree, script, arg) -> (enqueued_at, abandoned_at, waits) of a ticket
        # whose client went away, so a retry can pick its wait back up.
        self.parked = {}
        # The outside engines last written to daemon.log for holding a job up,
        # so the log gets a line when that changes and not one a tick.
        self.outside_logged = ""
        # tree id -> (text, at): see NOTE_SECONDS.
        self.notes = {}
        # Engines no running job accounts for, by worktree, for the page.
        self.outside = []
        self.seq = 0
        self.started = now()
        self.observed = 0
        self.observed_at = 0.0
        self.stray = 0
        self.stray_prev = 0
        # Outside engines doing nothing, which dock no capacity, and the cpu
        # readings that say so: pid -> {"cpu", "at", "quiet_since"}.
        self.stray_idle = 0
        self.stray_samples = {}
        self.sha = ""
        self.port = DEFAULT_PORT
        # Whether the box was busy at the last tick, which is all the tray
        # needs: the icon is raised on the edge into busy, not held up by us.
        self.was_busy = False
        # A handover to the daemon replacing this one (see Handover, below).
        # `handing_over` while it lets go: long polls come back, no tick
        # starts. `handed_over` once the state has gone: nothing here may
        # change or be saved again, because it is no longer ours. It waits
        # for a tick to finish first (`ticking`), which kills outside the lock.
        self.handing_over = False
        self.handed_over = False
        self.ticking = False
        if inherited is None:
            self.load_state()
        else:
            self.adopt_state(inherited)
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
        # the dead daemon on their next poll and re-acquire, which is simpler
        # than trying to keep two ideas of the queue in step. Their places
        # are, though, the same way a dead client's is: parked, for the
        # ticket that comes back asking. A restart used to send everything
        # queued to the back in whatever order it happened to re-ask.
        left = float(blob.get("saved_at") or 0)
        for row in blob.get("waiting", []):
            try:
                tree, script, arg, enqueued_at, waits = row
                key = (tree, script, arg)
                if now() - left <= PARK_SECONDS and key not in self.parked:
                    self.parked[key] = (float(enqueued_at), left, dict(waits))
            except (TypeError, ValueError):
                continue

    def export_state(self):
        """Everything a daemon taking over from this one needs to carry on as
        if it were this one. Holds the lock.

        Unlike state.json this has the queue in it, tickets and all: the
        clients behind them are still polling for those ids, and the daemon
        that answers the next poll has to know them. It has the readings too
        -- how long a lease or a stray has sat idle -- which a restart rightly
        throws away and a handover a second long has no reason to.
        """
        return {
            "seq": self.seq,
            "leases": list(self.leases.values()),
            "queue": list(self.queue),
            "cancelled": sorted(self.cancelled),
            "parked": [list(key) + list(parked)
                       for key, parked in self.parked.items()],
            "notes": [[tree, text, at] for tree, (text, at) in self.notes.items()],
            "stall_samples": self.stall_samples,
            "stray_samples": self.stray_samples,
            "outside_logged": self.outside_logged,
            "was_busy": self.was_busy,
        }

    def adopt_state(self, blob):
        """Take up where the daemon that sent export_state() left off."""
        for lease in blob.get("leases", []):
            self.leases[lease["id"]] = lease
            for key in lease.get("mutexes", []):
                self.held_mutex[key] = lease["id"]
        self.queue = list(blob.get("queue", []))
        self.seq = int(blob.get("seq", 0))
        self.cancelled = set(blob.get("cancelled", []))
        for tree, script, arg, enqueued_at, left, waits in blob.get("parked", []):
            self.parked[(tree, script, arg)] = (enqueued_at, left, dict(waits))
        for tree, text, at in blob.get("notes", []):
            self.notes[tree] = (text, at)
        # JSON has turned every pid these are keyed by into a string.
        for lease_id, sample in blob.get("stall_samples", {}).items():
            self.stall_samples[lease_id] = {
                "at": sample["at"],
                "cpu": dict((int(pid), cpu) for pid, cpu in sample["cpu"].items())}
        self.stray_samples = dict(
            (int(pid), sample) for pid, sample in blob.get("stray_samples", {}).items())
        self.outside_logged = blob.get("outside_logged", "")
        # So that the tray is not raised again for a box that was busy already.
        self.was_busy = bool(blob.get("was_busy"))

    def save_state(self):
        if self.handed_over:
            return
        blob = {
            "seq": self.seq,
            "saved_at": now(),
            "leases": list(self.leases.values()),
            # Earliest first, so of two live copies of one job it is the
            # longer wait that is kept. See load_state.
            "waiting": [list(self.park_key(t)) + [t["enqueued_at"],
                                                  t.get("waits") or {}]
                        for t in sorted(self.queue, key=lambda t: t["enqueued_at"])],
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
                    " queued_s, eta_s, observed_max_procs, finished, daemon_sha,"
                    " size, waited_on)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row.get("id"), row.get("tree"), row.get("tree_path", ""),
                     row.get("script"), row.get("arg"), row.get("slots", 0),
                     row.get("gpu", 0), int(bool(row.get("exclusive"))),
                     row.get("engines", 0), row.get("exit"), row.get("verdict"),
                     row.get("dur_s"), row.get("queued_s"), row.get("eta_s"),
                     row.get("observed_max_procs", 0), row.get("finished"),
                     self.sha, row.get("size"), row.get("waited_on")),
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
        table = process_table()
        seen = count_godot(table)
        self.observed_at = now()
        if seen is None:
            # Keep the last count. Reading "could not look" as "nothing there"
            # tells an exclusive job the box is quiet when it is not.
            return
        self.observed = seen
        self.outside = self.engines_outside(table)
        expected = sum(int(l.get("engines", 0)) for l in self.leases.values())
        stray = max(0, self.observed - expected - self.stray_idle)
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

    def engines_outside(self, table):
        """The engines no running job accounts for, grouped by the worktree
        they are running out of: [{"tree", "path", "engines", "idle"}], busiest
        first. Also settles self.stray_idle.

        "N unmanaged engines" was never enough to act on. Two thirds of all
        runs share the box with engines the queue did not start, and the only
        way to find out whose was to go and read command lines. This is not
        the number the scheduler docks capacity by -- that stays the plain
        count over what was booked, less the idle ones found here -- it is who
        to go and talk to.
        """
        mine = set()
        for lease in self.leases.values():
            mine.update(subtree(table, int(lease.get("winpid") or 0)))
        trees = [l.get("tree_path") for l in self.leases.values()]
        groups = {}
        samples = {}
        self.stray_idle = 0
        for pid, (_, name, cmdline, cpu) in table.items():
            if pid in mine or not is_engine(name):
                continue
            if any(engine_of(pid, cmdline, tree) for tree in trees if tree):
                continue
            found = ENGINE_PATH.search(cmdline or "")
            project = norm_path(found.group(1) or found.group(2)) if found else ""
            if not re.match(r"^[a-z]:/", project):
                project = norm_path(process_cwd(pid))
            label = tree_label(project) if project else "unknown"
            row = groups.setdefault(label, {"tree": label, "path": project,
                                            "engines": 0, "idle": 0})
            row["engines"] += 1
            samples[pid] = self.stray_sample(pid, cpu)
            if self.stray_is_idle(samples[pid]):
                row["idle"] += 1
                self.stray_idle += 1
        # Rebuilt each look, so a pid that has left takes its readings with it.
        self.stray_samples = samples
        return sorted(groups.values(), key=lambda g: (-g["engines"], g["tree"]))

    def stray_sample(self, pid, cpu):
        """This look's reading of an outside engine, carrying forward how long
        it has been quiet. Holds the lock."""
        last = self.stray_samples.get(pid)
        sample = {"cpu": cpu, "at": now(), "quiet_since": None}
        # Cpu time only goes up; one that went down is a new process wearing
        # an old pid, and starts again as unknown.
        if last is None or cpu < last["cpu"]:
            return sample
        if now() <= last["at"]:
            return last
        if cpu - last["cpu"] < IDLE_STRAY_CORES * (now() - last["at"]):
            sample["quiet_since"] = last["quiet_since"] or last["at"]
        return sample

    def stray_is_idle(self, sample):
        since = sample.get("quiet_since")
        return since is not None and now() - since >= IDLE_STRAY_SECONDS

    # -- the scheduler ----------------------------------------------------

    def tick(self):
        """Reap the dead, recount the box, grant what fits. Holds the lock."""
        with self.lock:
            if self.handing_over:
                return False
            self.ticking = True
        try:
            return self.tick_once()
        finally:
            with self.lock:
                self.ticking = False
                self.changed.notify_all()

    def tick_once(self):
        with self.lock:
            self.refresh_observed()
            self.reap()
            granted = self.grant_pass()
            if granted:
                self.save_state()
                self.changed.notify_all()
            busy = bool(self.leases or self.queue)
            self.log_outside()
            suspects = self.stall_suspects()
            overrun = self.ceiling_breaches()
        # Outside the lock: spawning a process is not something to hold the
        # scheduler for, and nothing below touches queue state.
        self.follow_tray(busy)
        if suspects or overrun:
            self.rescue_stalled(suspects, overrun)
        return granted

    def log_outside(self):
        """Write down whose engines are holding a queued job up, when that
        changes. Holds the lock.

        The history could say that jobs waited and never why. An afternoon of
        a box that would grant nothing looked, the day after, exactly like an
        afternoon of a full one, and whose engines they had been was gone
        with the engines.
        """
        held = [t["id"] for t in self.queue if t.get("waiting_on") == "outside"]
        who = ""
        if held:
            who = ", ".join("%s x%d" % (g["tree"], g["engines"] - g["idle"])
                            for g in self.outside if g["engines"] > g["idle"])
        if who == self.outside_logged:
            return
        self.outside_logged = who
        if who:
            sys.stderr.write("%s %d job(s) waiting on engines outside the queue: %s\n"
                             % (time.strftime("%H:%M:%S"), len(held), who))
            sys.stderr.flush()

    def other_shells(self, lease_id):
        """The pids every other running job is judged by. Holds the lock."""
        return [int(l.get("winpid") or 0) for l in self.leases.values()
                if l["id"] != lease_id and l.get("winpid")]

    def stall_suspects(self):
        """Ids of the leases late enough to be worth a look at their processes,
        and due one. Holds the lock."""
        due = []
        for lease_id, lease in self.leases.items():
            if lease.get("idle_ok"):
                # A window somebody is meant to be looking at. Its client said
                # so; the ceiling is still there for it.
                continue
            eta = lease.get("eta_s")
            limit = STALL_UNKNOWN_SECONDS
            if eta is not None:
                limit = max(STALL_FLOOR_SECONDS, STALL_ETA_FACTOR * float(eta))
            if now() - float(lease.get("granted_at") or now()) <= limit:
                continue
            last = self.stall_samples.get(lease_id)
            if last is None or now() - last["at"] >= STALL_SAMPLE_SECONDS:
                due.append(lease_id)
        return due

    def ceiling_of(self, lease):
        """Seconds this lease may hold its slots with somebody waiting,
        however busy it looks. What its client declared, or else far past
        anything honest: see CEILING_SECONDS."""
        declared = lease.get("max_s")
        if declared:
            return float(declared)
        eta = lease.get("eta_s")
        if eta is None:
            return CEILING_SECONDS
        return max(CEILING_SECONDS, CEILING_ETA_FACTOR * float(eta))

    def ceiling_breaches(self):
        """Ids of the leases past their ceiling, if anybody is waiting. Holds
        the lock."""
        if not self.queue:
            return []
        return [lease_id for lease_id, lease in self.leases.items()
                if now() - float(lease.get("granted_at") or now())
                > self.ceiling_of(lease)]

    def stall_sample(self, lease, table):
        """Take a cpu reading of the lease's processes. True once they have sat
        idle for STALL_QUIET_SECONDS. Holds the lock.

        The processes are the shell's subtree plus the engines running out of
        the lease's worktree. The second half is not a tidy-up: a bash client's
        engines are never under its shell, so for run_test.sh and run_clip.sh
        the subtree is one sleeping bash and the worktree match is the whole
        measurement. It is engine_in_tree's match, so the main checkout is not
        kept looking busy by its worktrees, nor a worktree by its neighbours.

        An engine that cannot be placed -- a relative `--path` and a working
        directory that would not read -- counts for every lease it might
        belong to. That is the side to err on here: it can only make a lease
        look busy.

        A changed set of pids counts as busy whatever the cpu says. A shoot
        that runs a hundred four-second engines can put a whole one between two
        readings, and its cpu time leaves the table with it.
        """
        root = int(lease.get("winpid") or 0)
        if root not in table:
            return False
        pids = subtree(table, root)
        spared = set()
        for other in self.other_shells(lease["id"]):
            spared.update(subtree(table, other))
        for pid, (_, name, cmdline, _) in table.items():
            if pid in pids or pid in spared or GODOT_MATCH not in name.lower():
                continue
            if engine_of(pid, cmdline, lease.get("tree_path")) is not False:
                pids.append(pid)
        cpu = dict((pid, table[pid][3]) for pid in pids)
        last = self.stall_samples.get(lease["id"])
        self.stall_samples[lease["id"]] = {"at": now(), "cpu": cpu}
        if last is None:
            lease.pop("quiet_since", None)
            return False
        burned = sum(max(0.0, cpu[pid] - last["cpu"][pid])
                     for pid in cpu if pid in last["cpu"])
        if (set(cpu) != set(last["cpu"])
                or burned >= STALL_IDLE_CORES * (now() - last["at"])):
            lease.pop("quiet_since", None)
            return False
        lease.setdefault("quiet_since", last["at"])
        return now() - lease["quiet_since"] >= STALL_QUIET_SECONDS

    def rescue_stalled(self, suspects, overrun=()):
        """Kill a lease that is in somebody's way and is not coming back, and
        hand its slots on. Takes the lock itself, and not around the slow
        parts.

        Two ways to qualify. `suspects` are late and get their processes read;
        one that has sat idle long enough is `stalled`. `overrun` are past
        their ceiling and are not read at all -- a run spinning in a loop
        looks exactly like work, which is what the ceiling is for -- and those
        are `overran`.

        Only with a queue, either way. A hung run on an otherwise empty box is
        costing nobody anything, and it may be a window somebody is looking
        at. It is still watched, so the first job to queue behind it does not
        then wait out the five minutes.

        The kill is the cancel button's: the shell's subtree and the worktree's
        engines, sparing what is under another lease. For a stalled lease
        nothing it reaches was working -- every engine the sweep can match was
        in the reading that just came back idle, which is the only reason a
        kill nobody asked for is allowed to sweep at all. An engine that could
        not be placed counted towards that reading and is not killed: it kept
        nobody waiting if it was busy, and if it was idle it may still be
        somebody else's.

        The job's owner is usually an agent that will see only a dead process,
        so what happened is left as a note for its worktree's next acquire.
        """
        table = process_table() if suspects else {}
        doomed = []
        with self.lock:
            waiting = len(self.queue)
            for lease_id in suspects:
                lease = self.leases.get(lease_id)
                if lease is None or not table:
                    continue
                if self.stall_sample(lease, table) and waiting:
                    doomed.append((dict(lease), self.other_shells(lease_id),
                                   "stalled", "idle for %ds"
                                   % (now() - lease["quiet_since"])))
            for lease_id in overrun:
                lease = self.leases.get(lease_id)
                if (lease is None or not waiting
                        or any(d[0]["id"] == lease_id for d in doomed)):
                    continue
                doomed.append((dict(lease), self.other_shells(lease_id),
                               "overran", "past its %ds ceiling"
                               % self.ceiling_of(lease)))
        for lease, spare, verdict, why in doomed:
            what = ("%s %s" % (lease.get("script", ""), lease.get("arg", ""))).strip()
            text = ("%s %s after %ds (estimate %s): %s with %d job(s) waiting"
                    % (what, verdict, now() - lease["granted_at"],
                       "none" if lease.get("eta_s") is None
                       else "%ds" % lease["eta_s"], why, waiting))
            sys.stderr.write("%s %s -- killing it\n" % (lease["id"], text))
            sys.stderr.flush()
            lease["note"] = text
            kill_job(lease.get("winpid"), lease.get("tree_path", ""), spare)
        if not doomed:
            return
        with self.lock:
            for lease, _, verdict, _ in doomed:
                if self.finish(lease["id"], None, verdict):
                    self.notes[lease.get("tree_id", "")] = (
                        "testq killed your last run here -- " + lease["note"]
                        + ". If it was meant to take that long, say so with"
                        " max_s (maxSeconds), or idle_ok (idleOk) for a window"
                        " that is meant to sit there.", now())
            self.grant_pass()
            self.save_state()
            self.changed.notify_all()

    def note_for(self, tree_id):
        """The note left for this worktree, once. Holds the lock."""
        text, at = self.notes.pop(tree_id, ("", 0))
        return text if now() - at <= NOTE_SECONDS else ""

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
        # A queued client is gone when it has stopped polling -- or when its
        # process has, which is known at once. Waiting on the polls alone took
        # up to a minute and a half: the long poll a dead client left behind
        # keeps refreshing the ticket until it returns to nobody.
        stale = [t for t in self.queue
                 if now() - t.get("last_poll", t["enqueued_at"]) > TICKET_STALE_SECONDS
                 or (t.get("winpid") and not process_alive(t["winpid"], None))]
        for ticket in stale:
            self.queue.remove(ticket)
            self.append_history(self.history_row(ticket, None, "abandoned"))
            self.parked[self.park_key(ticket)] = (
                ticket["enqueued_at"], now(), dict(ticket.get("waits") or {}))
        for key in [k for k, parked in self.parked.items()
                    if now() - parked[1] > PARK_SECONDS]:
            del self.parked[key]
        if dead or stale:
            self.save_state()

    def park_key(self, ticket):
        return (ticket.get("tree_id", ""), ticket.get("script", ""),
                ticket.get("arg", ""))

    def start_in(self, ticket, order):
        """A rough (seconds until `ticket` starts, whether that is only a
        floor). Holds the lock.

        Rough on purpose: one line of arithmetic over the estimates already on
        the tickets, with no attempt to replay the scheduler. Its job is to
        let a caller decide between waiting and coming back -- a session whose
        command will be killed in ten minutes needs to know that the wait is
        twenty -- and "about twelve minutes" does that as well as 11m40s.

        A job with no estimate adds nothing and turns the answer into a floor,
        which is said out loud to the client rather than hidden in a guess.
        """
        floor = [False]

        def length(job, started=None):
            eta = job.get("eta_s")
            if eta is None:
                floor[0] = True
                return 0.0
            if started is None:
                return float(eta)
            return max(15.0, float(eta) - (now() - float(started)))

        need_cpu, need_gpu = self.need_of(ticket)
        ahead = order[:order.index(ticket)] if ticket in order else []
        if ticket.get("exclusive"):
            wait = max([length(l, l.get("granted_at") or now())
                        for l in self.leases.values()] or [0.0])
            wait += sum(length(t) for t in ahead)
        elif need_gpu > 0:
            # One line for the GPU: whoever has it, then everybody ahead who
            # wants it.
            wait = sum(length(l, l.get("granted_at") or now())
                       for l in self.leases.values() if self.need_of(l)[1] > 0)
            wait += sum(length(t) for t in ahead if self.need_of(t)[1] > 0)
            wait /= float(max(1, self.capacity["gpu"]))
        else:
            # Slot-seconds of work ahead, spread over the slots. A ticket in
            # the GPU line is not ahead of a CPU-only one: it is passed.
            work = sum(length(l, l.get("granted_at") or now()) * self.need_of(l)[0]
                       for l in self.leases.values())
            work += sum(length(t) * self.need_of(t)[0] for t in ahead
                        if self.need_of(t)[1] == 0)
            free = self.capacity["cpu"] - sum(self.need_of(l)[0]
                                              for l in self.leases.values())
            wait = 0.0 if free >= need_cpu else work / float(max(1, self.capacity["cpu"]))
        if wait <= 0:
            # Queued, and nothing with an estimate is in its way: it is waiting
            # on a mutex, or on engines the queue did not start. Nobody knows
            # when those end, and "starts in 0s" would be a lie told twice a
            # minute.
            return None, True
        return round(wait), floor[0]

    def order(self):
        """Queue order: short jobs first, with aging so nothing starves.

        The user's call, and the right one for how this box is used: a
        twenty-second tagged slice run while iterating should not sit behind
        two eleven-minute suites. The aging term is what keeps that honest --
        once a ticket has waited past max(600s, twice its own estimate) it
        goes to the front in arrival order and short jobs stop overtaking it.

        A ticket with no estimate ages at the floor. It SORTS as long, which
        is right, but it must not AGE as long: twice UNKNOWN_ETA is twenty-three
        days, and a first-ever capture script sat at the back for as long as
        estimated work kept arriving.
        """
        def key(ticket):
            eta = ticket.get("eta_s")
            waited = now() - ticket["enqueued_at"]
            limit = AGE_FLOOR_SECONDS
            if eta is not None:
                limit = max(limit, 2.0 * float(eta))
            eta = UNKNOWN_ETA if eta is None else float(eta)
            aged = waited > limit
            if aged:
                return (0, ticket["enqueued_at"], 0.0)
            return (1, eta, ticket["enqueued_at"])
        return sorted(self.queue, key=key)

    def grant_pass(self):
        used_cpu = sum(self.need_of(l)[0] for l in self.leases.values())
        used_gpu = sum(self.need_of(l)[1] for l in self.leases.values())
        # Capacity we cannot use because somebody outside the queue is using
        # it. Never let this drive availability negative.
        penalty = self.stray_penalty()
        free_cpu = max(0, self.capacity["cpu"] - used_cpu - penalty)
        free_gpu = max(0, self.capacity["gpu"] - used_gpu)
        # What is free before the penalty and before anything is held back
        # for a ticket further up, which is only for saying why a ticket is
        # waiting: see waiting_on.
        raw_cpu = self.capacity["cpu"] - used_cpu
        raw_gpu = self.capacity["gpu"] - used_gpu

        granted = []
        reserved_mutexes = set()
        reserved_cpu = 0
        reserved_gpu = 0
        for ticket in self.order():
            need_cpu, need_gpu = self.need_of(ticket)
            mutexes = list(ticket.get("mutexes", []))
            blocked_mutex = any(
                key in self.held_mutex or key in reserved_mutexes for key in mutexes
            )
            fits = (not blocked_mutex) and need_cpu <= free_cpu and need_gpu <= free_gpu
            # An exclusive job needs the box to itself: no other lease at all,
            # and no stray engine. run_mp measures inter-process clock skew,
            # so one windowed clip alongside it is enough to make its verdict
            # meaningless. Strays are not ours to wait out forever though --
            # see EXCLUSIVE_STRAY_PATIENCE.
            #
            # Asked of the leases and not of free_cpu, which is already short
            # by the strays' penalty: an exclusive job needs every slot, so
            # with one working engine outside the queue it never fitted, the
            # patience was never reached, and it sat at the head reserving
            # the whole box for as long as the stray lived. Sixteen run_mp in
            # nineteen gave up in the queue that way. Nothing held back for a
            # ticket ahead of it either, which is what "every slot" was also
            # checking.
            outwaiting = False
            if ticket.get("exclusive"):
                waited = now() - ticket["enqueued_at"]
                quiet = self.stray == 0 or waited > EXCLUSIVE_STRAY_PATIENCE
                fits = (not blocked_mutex and not self.leases and quiet
                        and not reserved_cpu and not reserved_gpu)
                outwaiting = not quiet
            if not fits and self.try_shrink(ticket, blocked_mutex, free_cpu,
                                            free_gpu, reserved_gpu > 0):
                need_cpu, need_gpu = self.need_of(ticket)
                fits = True
            if fits:
                self.activate(ticket)
                granted.append(ticket["id"])
                free_cpu -= need_cpu
                free_gpu -= need_gpu
                raw_cpu -= need_cpu
                raw_gpu -= need_gpu
            else:
                # Head-of-line reservation. What this ticket cannot get yet is
                # held back from everyone behind it, so a four-slot job is not
                # starved by a stream of one-slot jobs.
                #
                # A ticket that is short of nothing but the GPU reserves no
                # CPU at all. There is always a line for the GPU on this box,
                # so a slot held for the head of it is a slot idle all day --
                # and the head does not need it: whoever has the GPU has a
                # slot too and hands both back together. If a shorter CPU job
                # takes that slot first, the ticket is then short of CPU, and
                # reserves like anything else.
                #
                # Nor does one that is further back in the GPU line than the
                # GPU is deep, whatever else it is short of: it cannot start
                # until the ones ahead have finished and given their slots
                # back. Reserving for each of them parked a CPU-only suite
                # behind four queued clips with three cores idle.
                #
                # An exclusive job is in neither case -- it is waiting for the
                # whole box -- so it reserves all of it. Except while it is
                # still giving the strays their five minutes: it cannot start
                # before that runs out whatever the queue does, and emptying
                # the box for it meanwhile is eight slots idle for nothing.
                # It reserves from the moment the strays go or its patience
                # does, and the box drains for it then.
                reason = self.waiting_on(ticket, blocked_mutex, raw_cpu,
                                         raw_gpu, penalty)
                self.charge(ticket, reason)
                ticket["blocked_on"] = self.explain(ticket, reason)
                if outwaiting:
                    continue
                gpu_alone = (not blocked_mutex and need_cpu <= free_cpu
                             and need_gpu > free_gpu)
                behind_gpu = (need_gpu > 0
                              and reserved_gpu + need_gpu > self.capacity["gpu"])
                if ticket.get("exclusive") or not (gpu_alone or behind_gpu):
                    reserved_cpu += need_cpu
                    free_cpu = max(0, free_cpu - need_cpu)
                reserved_gpu += need_gpu
                free_gpu = max(0, free_gpu - need_gpu)
                reserved_mutexes.update(mutexes)
        return granted

    def stray_penalty(self):
        """Slots the engines outside the queue cost it. Holds the lock.

        See STRAY_FREE. Never the whole box: with STRAY_FLOOR slots always
        left, a crowd of outside engines slows the queue and cannot stop it.
        """
        docked = max(0, self.stray - STRAY_FREE)
        return min(docked, max(0, self.capacity["cpu"] - STRAY_FLOOR))

    def waiting_on(self, ticket, blocked_mutex, raw_cpu, raw_gpu, penalty):
        """One word for what is keeping this ticket in the queue. Holds the
        lock.

        `raw_cpu` and `raw_gpu` are what no running job has, with nothing
        taken off for outside engines or held back for tickets further up.
        So the order below is the order of blame: what a running job holds
        first, then what the engines outside the queue cost, and only then
        the queue's own doing -- `ahead`, which is a slot that is free and
        being kept for somebody in front.
        """
        need_cpu, need_gpu = self.need_of(ticket)
        if blocked_mutex:
            return "mutex"
        if ticket.get("exclusive"):
            return "outside" if self.stray and not self.leases else "quiet"
        if need_gpu > raw_gpu:
            return "gpu"
        if need_cpu > raw_cpu:
            return "slots"
        if need_cpu > raw_cpu - penalty:
            return "outside"
        return "ahead"

    def charge(self, ticket, reason):
        """Book the time since this ticket was last looked at to `reason`.
        Holds the lock.

        This is what the history was missing. A week of it said jobs had
        queued for a hundred and twenty hours and nothing about why, and the
        answer -- that most of it was not behind another job at all -- had to
        be reconstructed from start and finish times.
        """
        waits = ticket.setdefault("waits", {})
        mark = ticket.get("wait_mark") or now()
        waits[reason] = round(waits.get(reason, 0.0) + max(0.0, now() - mark), 1)
        ticket["wait_mark"] = now()
        ticket["waiting_on"] = reason

    def eta_until_free(self, want_more, gpu_line=False):
        """A rough time until `want_more` further cpu slots come free, off the
        running leases' own estimates, assuming nothing new is granted first.
        With `gpu_line`, somebody ahead in the queue is waiting for the GPU,
        and that assumption is false for exactly one kind of lease: the slot
        a GPU job gives back goes to the next GPU job, not to the caller. A
        suite one slot short was told "fifteen seconds" by every clip in turn.
        Floored per lease at 15 s: a lease already past its estimate could end
        any second, but assuming zero would make every shrink decision read
        "the wait is free" exactly when the estimate has already been wrong.
        """
        rel = []
        for l in self.leases.values():
            if gpu_line and self.need_of(l)[1] > 0:
                continue
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

    def try_shrink(self, ticket, blocked_mutex, free_cpu, free_gpu,
                   gpu_line=False):
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
        wait = self.eta_until_free(want - free_cpu, gpu_line)
        if work / grant >= wait + work / want:
            return False
        ticket["slots"] = grant
        ticket["engines"] = grant
        ticket["eta_s"] = round(work / grant, 1)
        return True

    def explain(self, ticket, reason):
        """waiting_on's word as the sentence a queued client prints."""
        if reason == "mutex":
            return "waiting for " + ", ".join(ticket.get("mutexes", []))
        if ticket.get("exclusive"):
            if self.stray:
                return "waiting for a quiet box -- %d unmanaged engine(s)" % self.stray
            return "waiting for a quiet box"
        if reason == "gpu":
            return "waiting for the GPU"
        if reason == "outside":
            return "waiting for slots -- %d unmanaged engine(s) on the box" % self.stray
        if reason == "ahead":
            return "waiting behind a job ahead of it in the queue"
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
        if ticket.get("waiting_on"):
            self.charge(ticket, ticket["waiting_on"])
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
            "size": job.get("size"),
            # Seconds queued, by what for: see waiting_on.
            "waited_on": json.dumps(job["waits"]) if job.get("waits") else None,
            "observed_max_procs": job.get("observed_max_procs", 0),
            "finished": now(),
        }

    def finish(self, lease_id, exit_code=None, verdict="released"):
        lease = self.leases.pop(lease_id, None)
        if lease is None:
            return False
        if lease.get("cancelling"):
            # Its shell is being killed from the page. Whichever notices
            # first -- the shell's own exit trap, the reaper, or cancel() --
            # it was cancelled.
            exit_code, verdict = None, "cancelled"
        self.stall_samples.pop(lease_id, None)
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
            max_s = body.get("max_s")
            try:
                max_s = float(max_s) if max_s else None
            except (TypeError, ValueError):
                max_s = None
            # More than the whole box means the whole box. A client that
            # remembers a bigger one would otherwise wait for ever.
            slots_val = min(max(0, int(body.get("slots", 1))), self.capacity["cpu"])
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
                "gpu": min(max(0, int(body.get("gpu", 0))), self.capacity["gpu"]),
                "exclusive": bool(body.get("exclusive", False)),
                "engines": max(1, int(body.get("engines", 1))),
                "mutexes": [str(m) for m in body.get("mutexes", []) if str(m)],
                "winpid": int(body.get("winpid", 0) or 0),
                "pid_ctime": None,
                "enqueued_at": now(),
                "last_poll": now(),
                "granted_at": None,
                "eta_s": eta,
                # How much work this is, in whatever the job counts in --
                # seconds to record, scenarios to shoot. See sized_estimate.
                "size": arg_size(arg, body.get("size"))[1],
                # See ceiling_of and stall_suspects.
                "max_s": max_s,
                "idle_ok": bool(body.get("idle_ok", False)),
                "blocked_on": "",
                "wait_mark": now(),
                "observed_max_procs": 0,
                "box_engines": 0,
            }
            # A retry can be here before the tick that would notice its
            # predecessor died -- an agent whose command timed out asks again
            # within the second. Clear the dead out first so that it is parked
            # in time to be picked up.
            self.reap()
            parked = self.parked.pop(self.park_key(ticket), None)
            if parked and now() - parked[1] <= PARK_SECONDS:
                # The same job from the same worktree, back after its client
                # died in the queue. It keeps the wait it had already done,
                # which is what puts it ahead of whatever arrived since and
                # what ages it to the front on time.
                ticket["enqueued_at"] = parked[0]
                ticket["resumed_s"] = round(now() - parked[0], 1)
                # And what it had waited for, so the row this one finally
                # writes accounts for the whole wait. `away` is the gap
                # between its client dying and asking again.
                ticket["waits"] = dict(parked[2])
                ticket["waits"]["away"] = round(
                    ticket["waits"].get("away", 0.0) + now() - parked[1], 1)
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
        kind, size = arg_size(arg, body.get("size"))
        if size:
            eta = self.sized_estimate(script, kind, size)
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
        # Last, the script with any argument at all. Wrong by a lot for a
        # script whose arguments differ by a lot, and still better than
        # nothing: a job with no estimate sorts behind everything that has
        # one, and more than a quarter of all jobs used to arrive with none.
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT dur_s FROM runs WHERE script=? AND"
                    " verdict='released' AND dur_s > 0"
                    " ORDER BY finished DESC LIMIT 15", (script,)
                ).fetchall()
        except Exception:
            return None
        if len(row) >= 3:
            durations = sorted(float(r["dur_s"]) for r in row)
            return round(durations[len(durations) // 2], 1)
        return None

    def sized_estimate(self, script, kind, size):
        """Seconds for `size` units of `kind`, off what other sizes took.

        The estimate is keyed on script and arg, and for a lot of jobs the arg
        is not what decides how long they take. `capture-warehouse.mjs
        warehouse` is 48 s recording thirty seconds and ten minutes recording
        five hundred; `sm64-shoot-game.mjs "148 scenarios"` has never been run
        before at exactly 148. Both are a fixed cost plus so much a unit, so
        that is what is fitted: a straight line through this script's recent
        runs of the same kind.

        Two or more runs at this very size win over the line -- they are the
        measurement, the line is the guess.
        """
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT arg, size, dur_s FROM runs WHERE script=? AND"
                    " verdict='released' AND dur_s > 0"
                    " ORDER BY finished DESC LIMIT 80", (script,)
                ).fetchall()
        except Exception:
            return None
        points = []
        for r in rows:
            row_kind, row_size = arg_size(r["arg"], r["size"])
            if row_kind == kind and row_size:
                points.append((row_size, float(r["dur_s"])))
        same = sorted(d for s, d in points if abs(s - size) <= 0.01 * size)
        if len(same) >= 2:
            return round(same[len(same) // 2], 1)
        if len(points) < 3 or len(set(s for s, _ in points)) < 2:
            return None
        n = float(len(points))
        mean_s = sum(s for s, _ in points) / n
        mean_d = sum(d for _, d in points) / n
        spread = sum((s - mean_s) ** 2 for s, _ in points)
        slope = sum((s - mean_s) * (d - mean_d) for s, d in points) / spread
        if slope <= 0:
            # Bigger jobs coming out faster is noise, not a law.
            return None
        return round(max(1.0, mean_d + slope * (size - mean_s)), 1)

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
                # A daemon handing over answers what it has in hand, and
                # "still queued" is true: the client asks again and the
                # daemon that has taken over knows the ticket.
                if remaining <= 0 or self.handing_over:
                    order = self.order()
                    position = order.index(ticket) + 1 if ticket in order else 0
                    start_in, floor = self.start_in(ticket, order)
                    return {
                        "granted": False,
                        "position": position,
                        "start_in_s": start_in,
                        "start_in_floor": not_granted_flag(floor),
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
            lease = self.leases.get(job_id)
            if lease is None:
                ticket = next((t for t in self.queue if t["id"] == job_id), None)
                if ticket is None:
                    return None
                self.queue.remove(ticket)
                self.append_history(self.history_row(ticket, None, "cancelled"))
                # Its client is long-polling; tell it the ticket is gone so it
                # stops waiting instead of sitting there for a minute.
                self.changed.notify_all()
                return "removed a queued job"
            lease["cancelling"] = True
            winpid, tree_path = lease.get("winpid"), lease.get("tree_path", "")
            spare = self.other_shells(job_id)
        # Not under the lock, as rescue_stalled's is not: the kill is several
        # passes with a second between them, and every acquire, poll and
        # release on the box was waiting behind it. A bash client gives its
        # acquire ten seconds before it runs unqueued.
        kill_job(winpid, tree_path, spare)
        with self.lock:
            self.finish(job_id, None, "cancelled")
            self.grant_pass()
            self.save_state()
            self.changed.notify_all()
        return "cancelled a running job"

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
                row["start_in_s"], row["start_in_floor"] = self.start_in(ticket, order)
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
                "used": {"cpu": used_cpu, "gpu": used_gpu,
                         "stray_penalty": self.stray_penalty()},
                "godot": {
                    "observed": self.observed,
                    "expected": sum(int(l.get("engines", 0)) for l in self.leases.values()),
                    "unmanaged": self.stray,
                    "idle": self.stray_idle,
                    "outside": list(self.outside),
                },
                "running": running,
                "queued": queued,
                "history": [dict(row, alert=alert_for(row.get("exit"), row.get("verdict")))
                            for row in reversed(self.history[-50:])],
            }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def not_granted_flag(value):
    """A yes or no for a reply that says a ticket is still queued, as 1 or 0
    and never as JSON's `true`.

    The bash clients read these replies without a JSON parser, and the test
    every vendored copy of them shipped with for "was I granted" is the word
    "granted" followed anywhere by the word "true". The day `start_in_floor`
    was added to the queued reply, a ticket queued behind anything with no
    estimate -- or behind a mutex, or behind engines outside the queue, which
    is always a floor -- read as granted. The run started at once, outside
    the queue, on top of whatever it had been queued behind; its ticket was
    never polled again and went into the history as abandoned a minute
    later, three hundred times a day. Those runs were most of the "engines
    outside the queue" this daemon then docked slots for.

    There are thirty checkouts carrying that client and no way to change
    them all at once, so the reply is what gives way. Nothing that is not
    granted may say `true` anywhere in it: test_a_queued_reply_never_says_true.
    """
    return 1 if value else 0


def request_allowed(host, origin, port):
    """Whether a request came from this machine's own clients or its own page.

    Binding to 127.0.0.1 keeps other machines out and does nothing about the
    browser already on this one: any page it has open can POST here, and
    /cancel and /quit kill things. A browser always says where a cross-site
    request came from, so an Origin that is not this daemon's own page is
    refused. The Host check is for DNS rebinding, where a hostile name is
    pointed at 127.0.0.1 so that the page reading /state IS same-origin -- it
    still has to send its own name as Host. curl, urllib and Node send no
    Origin and a loopback Host, which is every real client there is.
    """
    host = (host or "").strip().lower()
    if host:
        name, sep, tail = host.rpartition(":")
        if not sep or not tail.isdigit():
            name = host
        if name not in LOOPBACK_HOSTS:
            return False
    if origin is None:
        return True
    origin = origin.strip().lower()
    return origin in ["http://%s:%d" % (h, port) for h in LOOPBACK_HOSTS]


def acquire_reply(queue, ticket):
    """What /acquire says back about the ticket it just made."""
    # Say straight away whether this went through. A client that has to
    # wait can then print one line about it now, instead of staying
    # silent until its first long poll happens to come back -- which,
    # if the wait is under 25 s, is never.
    with queue.lock:
        granted = ticket["id"] in queue.leases
        position, blocked = 0, ""
        start_in, floor = 0, False
        if not granted:
            order = queue.order()
            position = next((i + 1 for i, t in enumerate(order)
                             if t["id"] == ticket["id"]), 0)
            blocked = ticket.get("blocked_on", "")
            start_in, floor = queue.start_in(ticket, order)
        note = queue.note_for(ticket.get("tree_id", ""))
    return {
        "ticket": ticket["id"],
        "eta_s": ticket.get("eta_s"),
        "granted": granted,
        "position": position,
        "blocked_on": blocked,
        # Roughly when a queued ticket starts, and whether that is only
        # a floor because something ahead of it has no estimate.
        "start_in_s": start_in,
        "start_in_floor": not_granted_flag(floor),
        # Set when this ticket picked up the wait of one whose client
        # died in the queue: how long it has now been waiting in all.
        "resumed_s": ticket.get("resumed_s", 0),
        # Something this worktree should be told -- today, only that
        # its last run was killed here and why.
        "note": note,
        # Only meaningful when granted is true; a queued ticket has no
        # grant to describe yet and the client reads it again off the
        # /wait response that finally grants it.
        "box_engines": ticket.get("box_engines", 0),
        # The width actually granted, which for a flexible job can be
        # less than it asked (see try_shrink). Same caveat as above.
        "slots": ticket.get("slots", 0),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "testq/1"
    queue = None
    stopping = None
    # A connection that never sends its request -- a browser opens them ahead
    # of need -- is dropped after this long, where it used to hold a thread
    # for ever. It would hold up a handover for as long too.
    timeout = 5

    def log_message(self, fmt, *args):
        pass  # daemon.log is for crashes, not for a line per poll

    def _refused(self):
        if request_allowed(self.headers.get("Host"), self.headers.get("Origin"),
                           self.server.server_address[1]):
            return False
        self._send(403, {"error": "not from this machine"})
        return True

    def _send(self, code, payload, content_type="application/json"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode("utf-8")
        else:
            body = payload.encode("utf-8") if isinstance(payload, str) else payload
        # A client killed while it waits has hung up by the time its long poll
        # comes back, and the headers are the first thing to find that out.
        # That is every abandoned ticket; it is not worth a traceback each.
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
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
        if self._refused():
            return None
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/version":
            return self._send(200, {"proto": PROTO, "sha": self.queue.sha,
                                    "started": self.queue.started,
                                    # For `start --restart`: whether this
                                    # daemon can be replaced without being
                                    # stopped, and how to tell it has been.
                                    "handover": can_hand_over(),
                                    "pid": os.getpid()})
        if path == "/state":
            return self._send(200, self.queue.snapshot())
        if path in ("/", "/index.html"):
            return self._send(200, UI_HTML, "text/html; charset=utf-8")
        return self._send(404, {"error": "no such path"})

    def do_POST(self):
        if self._refused():
            return None
        path = urlparse(self.path).path
        body = self._body()
        if path == "/handover":
            return self._handover(body)
        if self.queue.handed_over:
            # Cannot happen -- a handover waits for every connection this
            # daemon accepted -- and must not be answered from a queue that
            # is no longer ours if it ever does.
            return self._send(503, {"error": "handed over"})
        if path == "/acquire":
            if int(body.get("proto", 0)) != PROTO:
                return self._send(409, {
                    "error": "proto",
                    "daemon_proto": PROTO,
                    "hint": origin_hint(),
                })
            return self._send(200, acquire_reply(
                self.queue, self.queue.enqueue(body)))
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

    def _handover(self, body):
        """The old daemon's half of a handover, on the connection the new one
        opened and keeps open until it is over."""
        queue = self.queue
        try:
            pid = int(body.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if not can_hand_over() or pid <= 0:
            return self._send(400, {"error": "no handover here"})
        with queue.lock:
            if queue.handing_over:
                return self._send(409, {"error": "already handing over"})
            queue.handing_over = True
            queue.changed.notify_all()
        # Stop taking connections. They are not refused: the socket goes on
        # listening, and whoever connects from here on waits in its backlog for
        # the new daemon to accept them.
        self.server.shutdown()
        blob = None
        deadline = time.monotonic() + HANDOVER_DRAIN_SECONDS
        # Everything accepted before that is answered first, this request
        # excepted, and a tick is let finish: it kills outside the lock.
        drained = self.server.wait_in_hand(1, deadline)
        with queue.lock:
            while drained and queue.ticking and time.monotonic() < deadline:
                queue.changed.wait(0.25)
            if drained and not queue.ticking:
                queue.handed_over = True
                blob = queue.export_state()
        took = False
        if blob is not None:
            try:
                share = self.server.socket.share(pid)
                self._send(200, {"state": blob,
                                 "socket": base64.b64encode(share).decode("ascii")})
                # The new daemon says K when it holds the socket and the
                # state, and serves only once it has D back. Anything else --
                # it died, it took too long -- and this daemon was never
                # replaced. Exactly one of the two is ever accepting.
                self.connection.settimeout(HANDOVER_CONFIRM_SECONDS)
                took = self.rfile.read(1) == b"K"
            except Exception as exc:
                sys.stderr.write("handover failed: %r\n" % (exc,))
        if took:
            sys.stderr.write("handed over to pid %d: %d running, %d queued\n"
                             % (pid, len(blob["leases"]), len(blob["queue"])))
            sys.stderr.flush()
            try:
                self.wfile.write(b"D")
            except Exception:
                pass
            self.stopping.set()
            return None
        sys.stderr.write("handover to pid %d did not complete -- carrying on\n" % pid)
        sys.stderr.flush()
        with queue.lock:
            queue.handing_over = queue.handed_over = False
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.5}, daemon=True).start()
        if blob is None:
            self._send(503, {"error": "busy"})
        return None


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

  const idle = s.godot.idle || 0;
  $("banner").innerHTML = s.godot.unmanaged > 0
    ? '<div class="banner">' + s.godot.unmanaged + ' Godot engine(s) running outside the queue' +
      ' &mdash; ' + (used.stray_penalty ? used.stray_penalty + ' slot(s) docked for them.'
        : 'no slots docked yet.') + ' A worktree without the queue shim, or the editor.' +
      (idle ? ' ' + idle + ' more sitting idle, not counted.' : '') +
      outside(s) + '</div>'
    : idle > 0
    ? '<div class="banner">' + idle + ' Godot engine(s) outside the queue sitting idle' +
      ' &mdash; not counted against capacity, and probably hung.' + outside(s) + '</div>'
    : "";

  function outside(s) {
    const rows = s.godot.outside || [];
    if (!rows.length) return "";
    return "<br>" + rows.map(g => "<span class='mono'>" + esc(g.tree) + "</span> &times;" +
      g.engines + (g.idle ? " (" + g.idle + " idle)" : "")).join(", ");
  }
  function startsIn(q) {
    if (q.start_in_s == null) return "--";
    return (q.start_in_floor ? "&ge; " : "~") + dur(Math.max(0, q.start_in_s - drift));
  }

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
        "<td class='mono'>" + dur(el) + (r.quiet_since ? " <span class='tag'>idle " +
          dur(s.now + drift - r.quiet_since) + "</span>" : "") + "</td>" +
        "<td>" + (r.eta_s ? '<div class="bar"><span class="' + (over ? "over" : "") +
          '" style="width:' + pct + '%"></span></div><span class="tag">~' + dur(r.eta_s) +
          "</span>" : '<span class="tag">no estimate</span>') + "</td>" +
        "<td><button onclick=\"cancel('" + r.id + "','" + job(r) + "')\">cancel</button></td></tr>";
    }).join("") + "</table>";

  if (!s.queued.length) $("queued").innerHTML = '<div class="empty">queue is empty</div>';
  else $("queued").innerHTML = '<table><tr><th>#</th><th>Worktree</th><th>Job</th>' +
    '<th>Waiting</th><th>Starts in</th><th>Estimate</th><th>Blocked on</th><th></th></tr>' +
    s.queued.map(q => "<tr><td class='mono'>" + q.position + "</td><td class='mono'>" +
      who(q) + "</td><td>" + job(q) + "</td><td class='mono'>" +
      dur((q.waiting_s || 0) + drift) + "</td><td class='tag'>" + startsIn(q) +
      "</td><td class='tag'>" +
      (q.eta_s ? "~" + dur(q.eta_s) : "--") + "</td><td class='tag'>" +
      esc(q.blocked_on) + "</td><td><button onclick=\"cancel('" + q.id + "','" +
      job(q) + "')\">cancel</button></td></tr>").join("") + "</table>";

  if (!s.history.length) $("history").innerHTML = '<div class="empty">no history yet</div>';
  else $("history").innerHTML = '<table><tr><th>Worktree</th><th>Job</th><th>Result</th>' +
    '<th>Took</th><th>Queued</th><th>Peak engines</th><th>When</th></tr>' +
    s.history.map(h => {
      let cls = "exitx", txt = h.verdict;
      if (h.alert) {
        // Killed here, by us, or an engine that died. Not a test saying no,
        // and "exit 3221225477" does not tell anybody that.
        cls = "exit1";
        txt = h.alert;
      } else if (h.verdict === "released") {
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
    # Deep enough to hold everybody who asks during a handover, which is every
    # queued client at once: their long polls are all answered in the same
    # instant and all come straight back. Fixed when the socket first listens,
    # and that socket is then passed from daemon to daemon.
    request_queue_size = 128

    def __init__(self, address, handler, inherited=None):
        # Connections accepted and not yet finished with, which is what a
        # handover waits on. Counted at the accept and not in the handler: a
        # request taken in the last moment before the daemon stopped listening
        # has not reached its handler yet, and one in a hundred did exactly
        # that and was answered by a queue that had already gone.
        self.in_hand = 0
        self.in_hand_changed = threading.Condition()
        ThreadingHTTPServer.__init__(self, address, handler,
                                     bind_and_activate=inherited is None)
        if inherited is not None:
            # Already bound and listening: it is the last daemon's.
            self.socket.close()
            self.socket = inherited
            self.server_address = inherited.getsockname()
            self.server_name, self.server_port = address

    def server_bind(self):
        if IS_WINDOWS:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)

    def process_request(self, request, client_address):
        with self.in_hand_changed:
            self.in_hand += 1
        try:
            ThreadingHTTPServer.process_request(self, request, client_address)
        except Exception:
            self.let_go()
            raise

    def process_request_thread(self, request, client_address):
        try:
            ThreadingHTTPServer.process_request_thread(self, request, client_address)
        finally:
            self.let_go()

    def let_go(self):
        with self.in_hand_changed:
            self.in_hand -= 1
            self.in_hand_changed.notify_all()

    def wait_in_hand(self, count, deadline):
        """Wait for all but `count` accepted connections to be finished with.
        Only means anything once serve_forever has stopped."""
        with self.in_hand_changed:
            while self.in_hand > count:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.in_hand_changed.wait(left)
        return True


# ---------------------------------------------------------------------------
# Handover
# ---------------------------------------------------------------------------
#
# `start --restart` used to stop the daemon and start another. Running jobs
# came through that -- a lease is re-adopted from state.json -- but the queue
# did not: every ticket was forgotten, and each client found out on its next
# poll and asked again. And for the second or two with nothing on the port, a
# run that asked started a daemon of its own from whatever copy of this file
# it could find, or ran unqueued; a release was lost. With a dozen agents
# relying on the queue, changing this file meant picking a moment.
#
# Now the new daemon is started first and takes over from the old one:
#
#   new  POST /handover {pid}             on a connection it then keeps open
#   old  stops accepting, lets what it has in hand finish, long polls included
#   old  replies with its whole state and its listening socket, duplicated
#        into the new process (WSADuplicateSocket, which is socket.share)
#   new  builds its queue from that, says K
#   old  says D and exits; new starts accepting
#
# The port is never closed, so nothing is refused: a client that connects in
# the middle waits in the socket's backlog and is answered by the new daemon.
# Tickets keep their ids, so the poll that was answered "still queued" by the
# old daemon is asked again of the new one and means the same thing. No
# process the queue started is touched at any point.
#
# If any step fails the old daemon carries on, and says so in daemon.log.

def can_hand_over():
    return IS_WINDOWS and hasattr(socket.socket, "share")


def take_over(port):
    """The new daemon's half, up to holding everything: (connection, the
    listening socket, the state). Raises if the old daemon will not."""
    conn = socket.create_connection(("127.0.0.1", port),
                                    timeout=HANDOVER_DRAIN_SECONDS + 15)
    try:
        payload = json.dumps({"pid": os.getpid()}).encode("utf-8")
        conn.sendall(("POST /handover HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n"
                      "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                      % (port, len(payload))).encode("ascii") + payload)
        resp = http.client.HTTPResponse(conn)
        resp.begin()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError("the running daemon answered %d %s"
                               % (resp.status, raw[:200]))
        body = json.loads(raw.decode("utf-8"))
        listener = socket.fromshare(base64.b64decode(body["socket"]))
        return conn, listener, body["state"]
    except Exception:
        conn.close()
        raise


def confirm_take_over(conn):
    """Tell the old daemon to go, and hear that it has. False means it is
    still serving and this process must not."""
    try:
        conn.settimeout(HANDOVER_CONFIRM_SECONDS)
        conn.sendall(b"K")
        return conn.recv(1) == b"D"
    except Exception:
        return False
    finally:
        conn.close()


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
            # Not the daemon's own directory, which it would otherwise
            # inherit and hold open for as long as the icon is up.
            cwd=os.path.expanduser("~"),
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
    if args.takeover:
        try:
            conn, listener, inherited = take_over(port)
            queue = Queue(inherited=inherited)
            server = Server(("127.0.0.1", port), Handler, inherited=listener)
        except Exception as exc:
            sys.stderr.write("testq: could not take over on port %d: %r\n" % (port, exc))
            return 1
        if not confirm_take_over(conn):
            sys.stderr.write("testq: the old daemon did not let go -- leaving it be\n")
            return 1
        sys.stderr.write("took over: %d running, %d queued\n"
                         % (len(queue.leases), len(queue.queue)))
        queue.save_state()
    else:
        try:
            server = Server(("127.0.0.1", port), Handler)
        except OSError:
            # Somebody else got there first. That IS the leader election; there
            # is nothing to clean up and nothing to complain about.
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
    old = None
    if daemon_up(port):
        code, ver = http_json(port, "/version")
        ver = ver if isinstance(ver, dict) else {}
        if not args.restart:
            print("testq already running on %d (sha %s)" % (port, ver.get("sha", "?")))
            return 0
        if ver.get("handover") and can_hand_over():
            # Not stopped: the new daemon is started beside it and takes the
            # queue and the port off it. See Handover, below.
            old = ver
        else:
            http_json(port, "/quit", {"force": bool(args.force)})
            for _ in range(40):
                if not daemon_up(port):
                    break
                time.sleep(0.25)
            else:
                print("the running daemon is from before a restart could hand the "
                      "queue over, and it has live jobs. Add --force for this one "
                      "restart: running jobs carry on, queued ones ask again and "
                      "keep their places.")
                return 1

    if old is None:
        # Only with no daemon running: nothing then has the database open.
        moved = migrate_runtime_dir()
        if moved:
            print("moved the runtime directory to %s" % moved)
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
    child = subprocess.Popen(
        [sys.executable, snapshot, "serve", "--port", str(port)]
        + (["--takeover"] if old else []),
        stdout=logfh, stderr=logfh, stdin=subprocess.DEVNULL,
        creationflags=flags, close_fds=True, env=env,
        cwd=runtime_dir(),
    )
    if old:
        return await_handover(port, old, child, sha)
    for _ in range(40):
        if daemon_up(port):
            print("testq %s listening on http://localhost:%d/" % (sha, port))
            return 0
        time.sleep(0.25)
    print("testq did not come up within 10s -- see %s" % log_path())
    return 1


def await_handover(port, old, child, sha):
    """Watch a handover from the outside: it is done when the port answers
    from another process, and it has failed when the new daemon has exited."""
    deadline = time.monotonic() + HANDOVER_DRAIN_SECONDS + HANDOVER_CONFIRM_SECONDS + 15
    while time.monotonic() < deadline:
        # Slow to answer while the two change over, never refused.
        code, ver = http_json(port, "/version", timeout=2.0)
        if isinstance(ver, dict) and ver.get("pid") not in (None, old.get("pid")):
            code, state = http_json(port, "/state")
            state = state if isinstance(state, dict) else {}
            print("testq %s took over from %s on http://localhost:%d/ -- "
                  "%d running and %d queued carried across"
                  % (sha, old.get("sha", "?"), port,
                     len(state.get("running", [])), len(state.get("queued", []))))
            return 0
        if child.poll() is not None:
            break
        time.sleep(0.25)
    print("the handover did not happen; testq %s is still serving and nothing "
          "was interrupted -- see %s" % (old.get("sha", "?"), log_path()))
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
    aside = []
    if state["godot"]["unmanaged"]:
        aside.append("%d unmanaged" % state["godot"]["unmanaged"])
    if state["godot"].get("idle"):
        aside.append("%d idle, not counted" % state["godot"]["idle"])
    print("testq  %d/%d cpu  %d/%d gpu  %d engine(s) on the box%s"
          % (used["cpu"], cap["cpu"], used["gpu"], cap["gpu"],
             state["godot"]["observed"],
             "  (%s)" % ", ".join(aside) if aside else ""))
    for r in state["running"]:
        idle = ""
        if r.get("quiet_since"):
            idle = "  idle " + fmt_dur(state["now"] - r["quiet_since"])
        print("  RUN   %-34s %-16s %8s / %-8s %s%s"
              % (r["tree_id"][:34], (r["script"] + " " + (r["arg"] or "")).strip(),
                 fmt_dur(r.get("elapsed_s")), fmt_dur(r.get("eta_s")), r["id"],
                 idle))
    for q in state["queued"]:
        starts = ""
        if q.get("start_in_s") is not None:
            starts = "  starts in %s%s" % (">=" if q.get("start_in_floor") else "~",
                                           fmt_dur(q["start_in_s"]))
        print("  WAIT %d %-34s %-16s waiting %-8s %s%s"
              % (q["position"], q["tree_id"][:34],
                 (q["script"] + " " + (q["arg"] or "")).strip(),
                 fmt_dur(q.get("waiting_s")), q.get("blocked_on", ""), starts))
    for g in state["godot"].get("outside", []):
        print("  OUT   %-34s %d engine(s) not started by a running job%s"
              % (g["tree"][:34], g["engines"],
                 "  (%d idle)" % g["idle"] if g.get("idle") else ""))
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


# waiting_on's words, for `stats`.
WAITED_ON = {
    "gpu": "the GPU, behind another window",
    "slots": "a slot, with every one of them taken",
    "outside": "engines running outside the queue",
    "ahead": "a free slot being held for a job ahead in the queue",
    "quiet": "the box to empty, for an exclusive job",
    "mutex": "a mutex",
    "away": "its own client, which had died and came back for its place",
}


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

        # Rows from before the queue kept this have nothing in the column
        # and are left out, so early on the hours here are fewer than the
        # total above.
        waited = {}
        for r in conn.execute("SELECT waited_on FROM runs WHERE " + where +
                              " AND waited_on IS NOT NULL", params):
            try:
                for reason, seconds in json.loads(r["waited_on"]).items():
                    waited[reason] = waited.get(reason, 0.0) + float(seconds)
            except (ValueError, TypeError, AttributeError):
                continue
        if waited:
            print("\nwhat the queueing was for:")
            for reason, seconds in sorted(waited.items(), key=lambda kv: -kv[1]):
                print("  %7.1f h  %s" % (seconds / 3600.0,
                                         WAITED_ON.get(reason, reason)))

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
    p.add_argument("--takeover", action="store_true",
                   help="take the queue and the port off the running daemon")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("start", help="snapshot and launch the daemon")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--restart", action="store_true",
                   help="replace a running daemon; its queue and jobs carry across")
    p.add_argument("--force", action="store_true",
                   help="restart a daemon too old to hand over, even with live jobs")
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
