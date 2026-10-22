"""Tests for testq. Stdlib only, like the thing they test:

    python -m unittest discover tests

Nothing here starts a daemon, an engine or a process. The queue is driven
through Queue.tick() on a fake clock, and the box is a dictionary shaped like
process_table()'s answer, so a test can describe a hung Godot in one line and
watch what the daemon would do about it over twenty simulated minutes.
"""

import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# Before the import: the module reads this for its database and state file.
os.environ["TESTQ_HOME"] = tempfile.mkdtemp(prefix="testq-tests-")
atexit.register(shutil.rmtree, os.environ["TESTQ_HOME"], ignore_errors=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import testq  # noqa: E402

G = "Godot_v4.7-stable_win64.exe"
GC = "Godot_v4.7-stable_win64_console.exe"
MAIN = "C:/Users/dev/Documents/projects/mfrs"
WT_A = MAIN + "/.claude/worktrees/weekend-features"
WT_B = MAIN + "/.claude/worktrees/hats"
SNAP = "C:/Users/dev/AppData/Local/Temp/claude/scratch-wh/snap2"
PROG = "C:/Users/dev/Documents/projects/prognosticator/.claude/worktrees/mk64"


class Box(unittest.TestCase):
    """A fake machine: a clock, a process table and a fresh queue."""

    def setUp(self):
        self.clock = 1000000.0
        self.table = {}
        self.cwds = {}
        self.killed = []
        self.table_works = True
        self.dead = set()

        def taskkill(pid):
            self.killed.append(pid)
            self.table.pop(pid, None)
            return True

        for name, value in (
            ("now", lambda: self.clock),
            ("process_table", lambda: dict(self.table) if self.table_works else {}),
            ("process_cwd", lambda pid: self.cwds.get(pid, "")),
            ("process_alive", lambda pid, ctime: pid not in self.dead),
            ("process_ctime", lambda pid: 1),
            ("taskkill", taskkill),
            ("spawn_tray", lambda *a, **k: False),
            ("IS_WINDOWS", True),
        ):
            patch = mock.patch.object(testq, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # daemon.log's lines: every kill writes two, and they are not the
        # test's output.
        for target, name, value in ((testq.time, "sleep", lambda s: None),
                                    (sys, "stderr", io.StringIO())):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        with testq.db() as conn:
            conn.execute("DELETE FROM runs")
        try:
            os.remove(testq.state_path())
        except OSError:
            pass
        # One GPU, whatever the real box has: nearly every test here makes a
        # line by putting a second window behind a first. TwoGpus has the rest.
        self.q = testq.Queue(dict(testq.CAPACITY, gpu=self.GPUS))

    GPUS = 1

    # -- building the box ---------------------------------------------------

    def proc(self, pid, ppid, name, cmdline="", cpu=0.0):
        self.table[pid] = (ppid, name, cmdline, cpu)

    def engine(self, pid, ppid, path, cpu=0.0, name=G):
        self.proc(pid, ppid, name, "%s --path %s --x" % (name, path), cpu)

    def job(self, script="cap.mjs", arg="x", tree=SNAP, winpid=100, **more):
        body = {"script": script, "arg": arg, "tree_id": os.path.basename(tree) or "t",
                "tree_path": tree, "winpid": winpid, "gpu": 1}
        body.update(more)
        if winpid and winpid not in self.table:
            self.proc(winpid, 1, "node.exe")
        return self.q.enqueue(body)

    def run_for(self, seconds, rates=None, churn=None):
        """Advance the clock in ticks. `rates` is cores burned per pid; `churn`
        is a pid to replace with a new one every forty seconds."""
        end = self.clock + seconds
        while self.clock < end:
            self.clock += 5
            for pid, rate in (rates or {}).items():
                if pid in self.table:
                    row = self.table[pid]
                    self.table[pid] = row[:3] + (row[3] + rate * 5,)
            if churn and int(self.clock) % 40 == 0 and churn[0] in self.table:
                row = self.table.pop(churn[0])
                churn[0] += 1000
                self.table[churn[0]] = row[:3] + (0.0,)
            for ticket in self.q.queue:
                ticket["last_poll"] = self.clock
            self.q.tick()

    def running(self, ticket):
        return ticket["id"] in self.q.leases

    def verdict(self, ticket):
        rows = [h for h in self.q.history if h["id"] == ticket["id"]]
        return rows[-1]["verdict"] if rows else None


# ---------------------------------------------------------------------------
# Whose engine is it
# ---------------------------------------------------------------------------

class EngineInTree(unittest.TestCase):
    def place(self, path, tree, cwd=""):
        return testq.engine_in_tree(G + " --path " + path + " --x", tree, cwd)

    def test_an_engine_is_in_its_own_tree(self):
        self.assertIs(self.place(MAIN, MAIN), True)
        self.assertIs(self.place(WT_A, WT_A), True)

    def test_worktrees_nest_and_do_not_belong_to_the_checkout_around_them(self):
        self.assertIs(self.place(WT_A, MAIN), False)

    def test_siblings_and_parents_are_not_inside(self):
        self.assertIs(self.place(WT_B, WT_A), False)
        self.assertIs(self.place(MAIN, WT_A), False)
        self.assertIs(self.place(MAIN + "-old", MAIN), False)

    def test_the_project_may_be_a_subdirectory(self):
        self.assertIs(self.place(SNAP.replace("/", "\\") + "\\godot", SNAP), True)

    def test_every_spelling_of_a_path_is_one_path(self):
        msys = "/c/Users/dev/Documents/projects/mfrs/.claude/worktrees/weekend-features/"
        self.assertIs(self.place(WT_A, msys), True)
        quoted = testq.engine_in_tree(G + ' --path "C:/My Stuff/p/godot" -x', "C:/My Stuff/p")
        self.assertIs(quoted, True)

    def test_a_relative_path_cannot_be_placed_without_a_directory(self):
        self.assertIsNone(self.place("godot", SNAP))
        self.assertIsNone(testq.engine_in_tree(G + " -e", SNAP))

    def test_a_job_with_no_tree_owns_nothing_for_certain(self):
        self.assertIsNone(self.place(MAIN, ""))

    def test_a_relative_path_is_placed_by_the_working_directory(self):
        inside = PROG.replace("/", "\\") + "\\godot\\"
        self.assertIs(self.place("godot", PROG, inside), True)
        self.assertIs(self.place("godot", WT_A, inside), False)
        whole_project = "C:/Users/dev/Documents/projects/prognosticator"
        self.assertIs(self.place("godot", whole_project, inside), False)

    def test_labels(self):
        self.assertEqual(testq.tree_label(WT_A), "mfrs/weekend-features")
        self.assertEqual(testq.tree_label(PROG + "/godot"), "prognosticator/mk64")
        self.assertEqual(testq.tree_label(MAIN), "projects/mfrs")


class ArgSize(unittest.TestCase):
    def test_a_declared_size_wins(self):
        self.assertEqual(testq.arg_size("warehouse", 30), ("warehouse", 30.0))

    def test_a_count_written_into_the_arg_is_a_size(self):
        self.assertEqual(testq.arg_size("148 scenarios"), ("scenarios", 148.0))

    def test_most_args_have_no_size(self):
        self.assertEqual(testq.arg_size("b-dbg.json"), ("b-dbg.json", None))
        self.assertEqual(testq.arg_size("jrb,bob"), ("jrb,bob", None))
        self.assertEqual(testq.arg_size(""), ("", None))


class AlertFor(unittest.TestCase):
    """What the tray interrupts somebody with."""

    def test_a_test_that_failed_is_not_news(self):
        for code in (0, 1, 2):
            self.assertEqual(testq.alert_for(code, "released"), "")

    def test_a_run_the_queue_killed_is(self):
        self.assertEqual(testq.alert_for(None, "stalled"), "killed: stalled")
        self.assertEqual(testq.alert_for(None, "overran"), "killed: overran")

    def test_an_engine_that_died_is(self):
        self.assertEqual(testq.alert_for(3221225477, "released"),
                         "crashed: access violation")
        self.assertEqual(testq.alert_for(3221226525, "released"),
                         "crashed: 0xC000041D")
        self.assertEqual(testq.alert_for(139, "released"), "crashed: SIGSEGV")

    def test_the_same_crash_read_as_a_signed_number(self):
        self.assertEqual(testq.alert_for(-1073741819, "released"),
                         "crashed: access violation")

    def test_somebody_stopping_a_run_is_not_a_crash(self):
        for code in (0xC000013A, 124, 130, 137, 143):
            self.assertEqual(testq.alert_for(code, "released"), "")

    def test_a_run_that_just_went_away_says_nothing(self):
        for verdict in ("abandoned", "reclaimed", "cancelled"):
            self.assertEqual(testq.alert_for(None, verdict), "")

    def test_an_exit_code_that_is_not_a_number(self):
        for code in (None, "1", True, 1.5):
            self.assertEqual(testq.alert_for(code, "released"), "")


class AlertInState(Box):
    def test_the_history_the_tray_reads_carries_it(self):
        crashed = self.job(gpu=0)
        self.q.release(crashed["id"], 3221225477)
        failed = self.job(script="t.mjs", winpid=200, gpu=0)
        self.q.release(failed["id"], 1)
        rows = self.q.snapshot()["history"]
        self.assertEqual([(r["exit"], r["alert"]) for r in rows],
                         [(1, ""), (3221225477, "crashed: access violation")])


# ---------------------------------------------------------------------------
# Handover
# ---------------------------------------------------------------------------

class Handover(Box):
    """One daemon's queue, exported and adopted by the next. The sockets are
    not here: this is what has to be true of the state that crosses."""

    def successor(self):
        # Through JSON, as it travels.
        blob = json.loads(json.dumps(self.q.export_state()))
        return testq.Queue(dict(testq.CAPACITY, gpu=self.GPUS), inherited=blob)

    def test_tickets_cross_under_their_own_ids_and_in_order(self):
        running = self.job()
        first = self.job(script="b.mjs", tree=WT_A, winpid=200)
        self.clock += 30
        second = self.job(script="c.mjs", tree=WT_B, winpid=300)
        new = self.successor()
        self.assertEqual(list(new.leases), [running["id"]])
        self.assertEqual([t["id"] for t in new.order()], [first["id"], second["id"]])
        self.assertEqual(new.queue[0]["enqueued_at"], first["enqueued_at"])

    def test_the_next_poll_is_answered_as_the_last_one_was(self):
        self.job()
        waiting = self.job(script="b.mjs", tree=WT_A, winpid=200)
        new = self.successor()
        def wait(seconds):
            self.clock += seconds

        with mock.patch.object(new.changed, "wait", wait):
            reply = new.poll(waiting["id"])
        self.assertEqual((reply["granted"], reply["position"]), (False, 1))
        self.assertNotIn("unknown", reply)

    def test_a_release_sent_to_the_new_daemon_grants_the_next_in_line(self):
        running = self.job()
        waiting = self.job(script="b.mjs", tree=WT_A, winpid=200)
        new = self.successor()
        self.assertTrue(new.release(running["id"], 0))
        self.assertIn(waiting["id"], new.leases)

    def test_new_ids_carry_on_from_the_old_ones(self):
        old = self.job()
        new = self.successor()
        self.proc(200, 1, "node.exe")
        fresh = new.enqueue({"script": "n.mjs", "tree_id": "t", "winpid": 200})
        self.assertNotEqual(fresh["id"], old["id"])
        self.assertEqual(new.seq, self.q.seq + 1)

    def test_what_the_old_daemon_was_remembering_crosses_too(self):
        job = self.job(eta_s=48)
        self.engine(101, 100, SNAP + "/godot")
        self.job(script="other.mjs", tree=WT_B, winpid=200, eta_s=30)
        # Late enough to be watched, not yet idle long enough to be killed.
        self.run_for(780, {101: 0.2})
        self.q.parked[("t", "s", "a")] = (self.clock - 50, self.clock - 5, {"cpu": 45.0})
        self.q.notes["t"] = ("killed", self.clock)
        self.q.cancelled.add("J99")
        new = self.successor()
        # How long the hung lease has sat idle: a restart throws that away,
        # and the job behind it then waits the whole quiet period again.
        self.assertEqual(new.stall_samples[job["id"]]["cpu"],
                         self.q.stall_samples[job["id"]]["cpu"])
        self.assertEqual(new.leases[job["id"]].get("quiet_since"),
                         self.q.leases[job["id"]].get("quiet_since"))
        self.assertEqual(new.parked, self.q.parked)
        self.assertEqual(new.notes, self.q.notes)
        self.assertEqual(new.cancelled, {"J99"})

    def test_a_long_poll_comes_back_still_queued_when_the_daemon_lets_go(self):
        self.job()
        waiting = self.job(script="b.mjs", tree=WT_A, winpid=200)
        self.q.handing_over = True
        reply = self.q.poll(waiting["id"])
        self.assertEqual((reply["granted"], reply["position"]), (False, 1))
        # Old bash clients take any `true` in a queued reply for a grant.
        self.assertNotIn("true", json.dumps(reply))

    def test_nothing_is_granted_or_killed_while_it_lets_go(self):
        running = self.job()
        waiting = self.job(script="b.mjs", tree=WT_A, winpid=200)
        self.q.handing_over = True
        self.dead.add(100)
        self.assertFalse(self.q.tick())
        self.assertIn(running["id"], self.q.leases)
        self.assertNotIn(waiting["id"], self.q.leases)

    def test_a_daemon_that_has_handed_over_writes_nothing_more(self):
        self.job()
        self.q.save_state()
        with open(testq.state_path(), encoding="utf-8") as fh:
            before = fh.read()
        self.q.handed_over = True
        self.job(script="b.mjs", tree=WT_A, winpid=200)
        with open(testq.state_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

class KillJob(Box):
    """A main-checkout bash job, a bash job and a Node job in worktree A,
    worktree B running outside the queue, and an engine with a relative path."""

    def setUp(self):
        super(KillJob, self).setUp()
        self.proc(10, 1, "bash.exe", "bash run_test.sh all")
        self.proc(11, 999, "timeout.exe")                 # no living parent
        self.engine(12, 11, MAIN, name=GC)
        self.engine(13, 12, MAIN)
        self.proc(20, 1, "bash.exe", "bash run_clip.sh hats")
        self.proc(21, 998, "timeout.exe")
        self.engine(22, 21, WT_A, name=GC)
        self.engine(23, 22, WT_A)
        self.engine(30, 997, WT_B)
        self.proc(40, 1, "node.exe", "node capture.mjs")
        self.engine(41, 40, WT_A + "/godot")
        self.proc(50, 996, G, G + " --headless --path godot --script t.gd")
        # A shell that only mentions an engine must never be mistaken for one.
        self.proc(60, 1, "bash.exe", "bash -c 'echo %s --path %s'" % (G, MAIN))

    def test_main_checkout_takes_only_its_own(self):
        testq.kill_job(10, MAIN, [20, 40])
        self.assertEqual(sorted(self.killed), [10, 12, 13])

    def test_a_worktree_spares_the_other_job_in_it(self):
        testq.kill_job(20, WT_A, [10, 40])
        self.assertEqual(sorted(self.killed), [20, 22, 23])

    def test_no_tree_path_means_the_subtree_and_nothing_else(self):
        testq.kill_job(10, "", [])
        self.assertEqual(sorted(self.killed), [10])

    def test_a_relative_path_engine_is_reached_through_its_directory(self):
        self.cwds[50] = WT_A.replace("/", "\\") + "\\godot\\"
        testq.kill_job(20, WT_A, [10, 40])
        self.assertEqual(sorted(self.killed), [20, 22, 23, 50])

    def test_two_bash_jobs_in_one_worktree_are_one_pool(self):
        # Documented limit, pinned so that changing it is a decision.
        testq.kill_job(40, WT_A, [10, 20])
        self.assertEqual(sorted(self.killed), [22, 23, 40, 41])


# ---------------------------------------------------------------------------
# Self-rescue
# ---------------------------------------------------------------------------

class Stalled(Box):
    def hung_capture(self, **more):
        """The 5 October hang: a Node capture whose engine finished and never
        exited, idling at a fifth of a core."""
        job = self.job(eta_s=48, **more)
        self.engine(101, 100, SNAP + "/godot")
        return job

    def waiter(self):
        return self.job(script="other.mjs", tree=WT_B, winpid=200, eta_s=30)

    def test_a_hung_job_with_somebody_waiting_is_killed(self):
        job, waiter = self.hung_capture(), self.waiter()
        self.run_for(1200, {101: 0.2})
        self.assertEqual(self.verdict(job), "stalled")
        self.assertEqual(sorted(self.killed), [100, 101])
        self.assertTrue(self.running(waiter))

    def test_it_is_given_fifteen_minutes_first(self):
        job = self.hung_capture()
        self.waiter()
        self.run_for(840, {101: 0.2})
        self.assertTrue(self.running(job))

    def test_nobody_waiting_nobody_killed(self):
        job = self.hung_capture()
        self.run_for(3000, {101: 0.2})
        self.assertTrue(self.running(job))
        self.assertEqual(self.killed, [])

    def test_but_it_was_being_watched(self):
        job = self.hung_capture()
        self.run_for(3000, {101: 0.2})
        self.waiter()
        self.run_for(70, {101: 0.2})
        self.assertEqual(self.verdict(job), "stalled")

    def test_late_is_not_hung(self):
        job = self.hung_capture()
        self.waiter()
        self.run_for(3000, {101: 1.5})
        self.assertTrue(self.running(job))

    def test_short_lived_engines_are_work_even_at_no_cpu(self):
        job = self.hung_capture()
        self.waiter()
        self.run_for(3000, {}, churn=[101])
        self.assertTrue(self.running(job))

    def test_no_estimate_waits_longer(self):
        job = self.job()
        self.engine(101, 100, SNAP + "/godot")
        self.waiter()
        self.run_for(1700, {101: 0.2})
        self.assertTrue(self.running(job))
        self.run_for(600, {101: 0.2})
        self.assertEqual(self.verdict(job), "stalled")

    def test_idle_ok_is_left_to_sit(self):
        job = self.hung_capture(idle_ok=True)
        self.waiter()
        self.run_for(3000, {101: 0.2})
        self.assertTrue(self.running(job))

    def test_a_box_that_will_not_say_is_not_an_idle_box(self):
        job = self.hung_capture()
        self.waiter()
        self.table_works = False
        self.run_for(3000, {101: 0.2})
        self.assertTrue(self.running(job))

    def test_the_owner_is_told_on_its_next_acquire(self):
        job = self.hung_capture()
        self.waiter()
        self.run_for(1200, {101: 0.2})
        note = self.q.note_for(job["tree_id"])
        self.assertIn("stalled", note)
        self.assertIn("idle", note)
        self.assertEqual(self.q.note_for(job["tree_id"]), "")


class StalledInWorktrees(Box):
    """Bash clients, whose engines are never under their shell."""

    def setUp(self):
        super(StalledInWorktrees, self).setUp()
        self.proc(10, 1, "bash.exe")
        self.engine(13, 999, MAIN)
        self.proc(20, 1, "bash.exe")
        self.engine(23, 998, WT_A)
        self.engine(30, 997, WT_B)                         # outside the queue
        self.proc(50, 996, G, G + " --headless --path godot --script t.gd")

    def hold(self, tree, shell):
        job = self.job(script="run_clip.sh", tree=tree, winpid=shell, eta_s=48)
        self.job(script="other.sh", tree=SNAP, winpid=60, eta_s=30)
        return job

    def test_a_working_engine_in_the_worktree_keeps_its_job(self):
        job = self.hold(WT_A, 20)
        self.run_for(3000, {23: 1.2})
        self.assertTrue(self.running(job))

    def test_a_hung_one_goes_engine_and_all(self):
        job = self.hold(WT_A, 20)
        self.run_for(1200, {23: 0.19, 13: 1.2, 30: 1.5})
        self.assertEqual(self.verdict(job), "stalled")
        self.assertEqual(sorted(self.killed), [20, 23])

    def test_the_main_checkout_is_not_kept_alive_by_its_worktrees(self):
        job = self.hold(MAIN, 10)
        self.run_for(1200, {23: 1.2, 30: 1.5})
        self.assertEqual(self.verdict(job), "stalled")
        self.assertEqual(sorted(self.killed), [10, 13])

    def test_nor_killed_while_it_is_the_one_working(self):
        job = self.hold(MAIN, 10)
        self.run_for(3000, {13: 1.2})
        self.assertTrue(self.running(job))

    def test_an_engine_nobody_can_place_counts_for_everybody(self):
        job = self.hold(WT_A, 20)
        self.run_for(3000, {50: 1.2})
        self.assertTrue(self.running(job))

    def test_until_its_directory_places_it_somewhere_else(self):
        self.cwds[50] = WT_B.replace("/", "\\") + "\\godot\\"
        job = self.hold(WT_A, 20)
        self.run_for(1200, {50: 1.2})
        self.assertEqual(self.verdict(job), "stalled")

    def test_a_job_that_never_said_where_it_runs_is_given_every_doubt(self):
        job = self.hold("", 20)
        self.run_for(3000, {30: 1.2})
        self.assertTrue(self.running(job))


class Ceiling(Box):
    def spinning(self, **more):
        job = self.job(eta_s=48, **more)
        self.engine(101, 100, SNAP + "/godot")
        return job

    def test_a_job_spinning_in_a_loop_is_killed_at_the_ceiling(self):
        job = self.spinning()
        self.job(script="other.mjs", tree=WT_B, winpid=200, eta_s=30)
        self.run_for(3500, {101: 1.0})
        self.assertTrue(self.running(job))
        self.run_for(200, {101: 1.0})
        self.assertEqual(self.verdict(job), "overran")
        self.assertEqual(sorted(self.killed), [100, 101])

    def test_only_with_somebody_waiting(self):
        job = self.spinning()
        self.run_for(8000, {101: 1.0})
        self.assertTrue(self.running(job))

    def test_a_client_can_set_its_own(self):
        job = self.spinning(max_s=120)
        self.job(script="other.mjs", tree=WT_B, winpid=200, eta_s=30)
        self.run_for(110, {101: 1.0})
        self.assertTrue(self.running(job))
        self.run_for(30, {101: 1.0})
        self.assertEqual(self.verdict(job), "overran")

    def test_or_raise_it(self):
        job = self.spinning(max_s=7200)
        self.job(script="other.mjs", tree=WT_B, winpid=200, eta_s=30)
        self.run_for(5000, {101: 1.0})
        self.assertTrue(self.running(job))

    def test_a_long_estimate_stretches_it(self):
        job = self.job(eta_s=1500)
        self.engine(101, 100, SNAP + "/godot")
        self.job(script="other.mjs", tree=WT_B, winpid=200, eta_s=30)
        self.run_for(5500, {101: 1.0})
        self.assertTrue(self.running(job))
        self.run_for(700, {101: 1.0})
        self.assertEqual(self.verdict(job), "overran")


class Progress(Box):
    """A job that says how far it has got is judged by that, not its cpu."""

    def suite(self, **more):
        job = self.job(script="run_test.sh", arg="all", eta_s=48, **more)
        self.engine(101, 100, SNAP + "/godot")
        return job

    def waiter(self):
        return self.job(script="other.mjs", tree=WT_B, winpid=200, eta_s=30)

    def run_reporting(self, job, seconds, rate, cpu, every=30):
        """Run for `seconds`, reporting `rate` more rows every `every`."""
        done = job.get("progress_done") or 0
        end = self.clock + seconds
        while self.clock < end:
            self.run_for(every, {101: cpu})
            done += rate
            self.q.progress(job["id"], done, 3104, "rows")

    def test_a_count_that_moves_is_never_stalled_however_idle_it_looks(self):
        # Under the cpu rule this is the 5 October hang, killed at 15 minutes.
        job, _ = self.suite(), self.waiter()
        self.run_reporting(job, 3000, 20, 0.2)
        self.assertTrue(self.running(job))
        self.assertEqual(self.killed, [])

    def test_a_count_that_stops_is_stalled_however_busy_it_looks(self):
        # A row spinning in a loop: a whole core, and no row ever finishes.
        job, waiter = self.suite(), self.waiter()
        self.q.progress(job["id"], 212, 3104, "rows")
        self.run_for(890, {101: 1.5})
        self.assertTrue(self.running(job))
        self.run_for(20, {101: 1.5})
        self.assertEqual(self.verdict(job), "stalled")
        self.assertEqual(sorted(self.killed), [100, 101])
        self.assertTrue(self.running(waiter))
        note = self.q.note_for(job["tree_id"])
        self.assertIn("no progress for", note)
        self.assertIn("at 212 of 3104 rows", note)
        self.assertIn("stall_s", note)

    def test_reports_that_repeat_the_count_are_not_movement(self):
        job, _ = self.suite(), self.waiter()
        self.run_reporting(job, 1000, 0, 1.5)
        self.assertEqual(self.verdict(job), "stalled")

    def test_nobody_waiting_nobody_killed(self):
        job = self.suite()
        self.q.progress(job["id"], 212, 3104)
        self.run_for(3000, {101: 0.2})
        self.assertTrue(self.running(job))

    def test_a_client_can_give_its_rows_longer(self):
        job, _ = self.suite(stall_s=1800), self.waiter()
        self.q.progress(job["id"], 212, 3104)
        self.run_for(1700, {101: 1.5})
        self.assertTrue(self.running(job))
        self.run_for(200, {101: 1.5})
        self.assertEqual(self.verdict(job), "stalled")

    def test_idle_ok_is_still_left_to_sit(self):
        job, _ = self.suite(idle_ok=True), self.waiter()
        self.q.progress(job["id"], 212, 3104)
        self.run_for(3000, {101: 0.2})
        self.assertTrue(self.running(job))

    def test_a_count_that_moves_is_still_stopped_at_its_ceiling(self):
        job, _ = self.suite(max_s=1200), self.waiter()
        self.run_reporting(job, 1300, 20, 1.0)
        self.assertEqual(self.verdict(job), "overran")

    def test_until_it_reports_it_is_judged_by_its_cpu(self):
        job, _ = self.suite(), self.waiter()
        self.run_for(1200, {101: 0.2})
        self.assertEqual(self.verdict(job), "stalled")

    def test_a_count_going_down_has_started_something_new(self):
        job, _ = self.suite(), self.waiter()
        self.q.progress(job["id"], 3104, 3104)
        self.run_for(800, {101: 1.0})
        self.q.progress(job["id"], 3, 40)
        self.run_for(800, {101: 1.0})
        self.assertTrue(self.running(job))

    def test_a_lease_nobody_has_is_refused(self):
        self.assertFalse(self.q.progress("J999", 1, 2))
        self.assertEqual(testq.progress_reply(self.q, {"lease": "J999", "done": 1})[0], 404)
        self.assertEqual(testq.progress_reply(self.q, {"lease": "J1", "done": "x"})[0], 400)

    def test_the_reply_says_no_true_to_an_old_bash_client(self):
        job = self.suite()
        code, reply = testq.progress_reply(
            self.q, {"lease": job["id"], "done": 5, "total": 9, "unit": "rows"})
        self.assertEqual((code, reply), (200, {"progress": 1}))
        self.assertNotIn("true", json.dumps(reply))

    def test_it_rides_on_the_snapshot_and_crosses_a_handover(self):
        job = self.suite()
        self.q.progress(job["id"], 2310, 3104, "rows")
        row = self.q.snapshot()["running"][0]
        self.assertEqual((row["progress_done"], row["progress_total"],
                          row["progress_unit"]), (2310, 3104, "rows"))
        self.assertEqual(row["stall_window_s"], testq.PROGRESS_STALL_SECONDS)
        heir = testq.Queue(dict(testq.CAPACITY, gpu=1),
                           inherited=json.loads(json.dumps(self.q.export_state())))
        self.assertEqual(heir.leases[job["id"]]["progress_done"], 2310)
        self.assertEqual(heir.progress_stalls(), [])

    def test_status_says_how_far_and_whether_it_moves(self):
        job = self.suite()
        self.q.progress(job["id"], 2310, 3104, "rows")
        state = self.q.snapshot()
        aside = testq.run_aside(state["running"][0], state["now"])
        self.assertIn("2,310 / 3,104 rows", aside)
        self.assertIn("working", aside)
        self.clock += 400
        state = self.q.snapshot()
        self.assertIn("no progress 6m40s", testq.run_aside(state["running"][0], state["now"]))


class BusyBox(Box):
    """An estimate is stretched by how much busier the box is than it was."""

    def ran(self, seconds, load_avg=None, max_procs=1):
        self.q.append_history({
            "id": "J0", "tree": "t", "script": "run_test.sh", "arg": "all",
            "exit": 0, "verdict": "released", "dur_s": seconds, "queued_s": 0,
            "observed_max_procs": max_procs, "load_avg": load_avg,
            "finished": self.clock})
        self.clock += 1

    def suite(self):
        return self.job(script="run_test.sh", arg="all", tree_id="t", gpu=0)

    def crowd(self, n):
        """n engines on the box that are not this job's."""
        for i in range(n):
            self.engine(500 + i, 1, WT_B + str(i), cpu=0.0)

    def test_a_quiet_box_leaves_the_estimate_alone(self):
        for _ in range(3):
            self.ran(2460, load_avg=1.0)
        job = self.suite()
        self.run_for(30)
        self.assertEqual(self.q.eta_now(job), 2460)
        self.assertEqual(self.q.snapshot()["running"][0]["busy"], 0)

    def test_five_engines_on_a_box_measured_at_one_is_longer(self):
        for _ in range(3):
            self.ran(2460, load_avg=1.0)
        job = self.suite()
        self.engine(101, 100, SNAP + "/godot")
        self.crowd(4)
        self.run_for(60)
        self.assertAlmostEqual(self.q.contention(job), 1.4)
        row = self.q.snapshot()["running"][0]
        self.assertEqual(row["eta_now_s"], 3444)
        self.assertEqual(row["busy"], 1)
        state = self.q.snapshot()
        self.assertIn("busy box, was ~41m00s", testq.run_aside(row, state["now"]))

    def test_measured_on_a_busy_box_it_is_not_stretched_again(self):
        for _ in range(3):
            self.ran(3444, load_avg=5.0)
        job = self.suite()
        self.engine(101, 100, SNAP + "/godot")
        self.crowd(4)
        self.run_for(60)
        self.assertAlmostEqual(self.q.contention(job), 1.0)

    def test_nor_shrunk_on_a_quieter_one(self):
        for _ in range(3):
            self.ran(3444, load_avg=5.0)
        job = self.suite()
        self.run_for(60)
        self.assertEqual(self.q.contention(job), 1.0)

    def test_a_lease_from_an_older_daemon_looks_its_history_up(self):
        for _ in range(3):
            self.ran(3444, load_avg=5.0)
        job = self.suite()
        del job["eta_load"]
        self.engine(101, 100, SNAP + "/godot")
        self.crowd(4)
        self.run_for(60)
        self.assertAlmostEqual(self.q.contention(job), 1.0)
        self.assertEqual(job["eta_load"], 5.0)

    def test_old_rows_are_read_at_the_most_they_saw(self):
        for _ in range(3):
            self.ran(2460, max_procs=3)
        self.assertEqual(self.q.history_load("run_test.sh", "all", "t"), 3)

    def test_a_run_records_the_load_it_ran_under(self):
        job = self.suite()
        self.engine(101, 100, SNAP + "/godot")
        self.crowd(2)
        self.run_for(60)
        self.q.release(job["id"], 0)
        with testq.db() as conn:
            row = conn.execute("SELECT load_avg FROM runs WHERE job_id=?",
                               (job["id"],)).fetchone()
        self.assertAlmostEqual(row["load_avg"], 3.0, delta=0.3)

    def test_a_slow_box_does_not_make_a_slow_run_a_suspect(self):
        # 400 s measured alone, on a box now carrying twenty: 1160 s there,
        # so suspected at three times that and not at three times 400.
        for _ in range(3):
            self.ran(400, load_avg=1.0)
        job = self.suite()
        self.engine(101, 100, SNAP + "/godot")
        self.crowd(19)
        self.run_for(30)
        self.clock += 1300
        self.assertEqual(self.q.stall_suspects(), [])
        self.clock += 2300
        self.assertEqual(self.q.stall_suspects(), [job["id"]])


class CpuOnThePage(Box):
    def test_a_running_job_says_what_it_burns(self):
        job = self.job(eta_s=48)
        self.engine(101, 100, SNAP + "/godot")
        self.run_for(60, {101: 1.0})
        row = self.q.snapshot()["running"][0]
        self.assertAlmostEqual(row["cpu_cores"], 1.0, delta=0.1)
        self.run_for(60, {101: 0.0})
        self.assertEqual(self.q.snapshot()["running"][0]["cpu_cores"], 0.0)
        self.assertIn("0.0 cpu", testq.run_aside(self.q.snapshot()["running"][0], self.clock))
        self.assertTrue(self.running(job))


# ---------------------------------------------------------------------------
# Counting the box
# ---------------------------------------------------------------------------

class Observed(Box):
    def test_engines_are_counted_once_each(self):
        self.engine(12, 11, MAIN, name=GC)
        self.engine(13, 12, MAIN)
        self.engine(30, 997, WT_B)
        self.q.refresh_observed(force=True)
        self.assertEqual(self.q.observed, 2)

    def test_a_failed_look_keeps_the_last_count(self):
        self.engine(13, 12, MAIN)
        self.engine(30, 997, WT_B)
        self.q.refresh_observed(force=True)
        self.table_works = False
        self.q.refresh_observed(force=True)
        self.assertEqual(self.q.observed, 2)

    def test_engines_outside_the_queue_are_named_by_worktree(self):
        job = self.job(tree=WT_A, winpid=40)
        self.engine(41, 40, WT_A + "/godot")               # the job's own
        self.engine(23, 998, WT_A)                         # same tree, by hand
        self.engine(30, 997, WT_B)
        self.engine(31, 997, WT_B)
        self.proc(50, 996, G, G + " --headless --path godot")
        self.cwds[50] = PROG.replace("/", "\\") + "\\godot\\"
        self.q.refresh_observed(force=True)
        self.assertTrue(self.running(job))
        self.assertEqual(
            [(g["tree"], g["engines"]) for g in self.q.snapshot()["godot"]["outside"]],
            [("mfrs/hats", 2), ("prognosticator/mk64", 1)])

    def test_an_idle_engine_outside_the_queue_stops_docking_slots(self):
        for pid in range(30, 30 + testq.CAPACITY["cpu"]):
            self.engine(pid, 997, WT_B)                    # finished, never exited
        self.engine(60, 996, WT_A)                         # working
        self.run_for(20, rates={60: 1.0})
        suite = self.job(script="run_test_par.sh", tree=SNAP, winpid=100, gpu=0,
                         slots=testq.CAPACITY["cpu"], engines=1)
        self.assertFalse(self.running(suite))
        self.run_for(testq.IDLE_STRAY_SECONDS, rates={60: 1.0})
        self.assertTrue(self.running(suite))
        self.engine(101, 100, SNAP)                        # the suite's own
        self.run_for(10, rates={60: 1.0, 101: 1.0})
        godot = self.q.snapshot()["godot"]
        self.assertEqual((godot["unmanaged"], godot["idle"]),
                         (1, testq.CAPACITY["cpu"]))
        self.assertEqual(
            [(g["tree"], g["engines"], g["idle"]) for g in godot["outside"]],
            [("mfrs/hats", testq.CAPACITY["cpu"], testq.CAPACITY["cpu"]),
             ("mfrs/weekend-features", 1, 0)])

    def test_an_idle_engine_counts_again_once_it_works(self):
        self.engine(30, 997, WT_B)
        self.run_for(testq.IDLE_STRAY_SECONDS + 20)
        self.assertEqual((self.q.stray, self.q.stray_idle), (0, 1))
        self.run_for(10, rates={30: 1.0})
        self.assertEqual((self.q.stray, self.q.stray_idle), (1, 0))

    def test_a_recycled_pid_is_not_idle_on_the_old_ones_record(self):
        self.engine(30, 997, WT_B, cpu=50.0)
        self.run_for(testq.IDLE_STRAY_SECONDS + 20)
        self.engine(30, 997, WT_B, cpu=0.0)
        self.run_for(10)
        self.assertEqual((self.q.stray, self.q.stray_idle), (1, 0))


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

class Scheduling(Box):
    def test_one_gpu_one_job(self):
        first = self.job(winpid=100)
        second = self.job(script="b.mjs", winpid=200)
        self.assertTrue(self.running(first))
        self.assertFalse(self.running(second))
        self.assertEqual(second["blocked_on"], "waiting for the GPU")
        self.q.release(first["id"], 0)
        self.assertTrue(self.running(second))

    def test_shortest_first(self):
        self.job(winpid=100, eta_s=100)
        long = self.job(script="long.mjs", winpid=200, eta_s=500)
        short = self.job(script="short.mjs", winpid=300, eta_s=20)
        self.assertEqual([t["id"] for t in self.q.order()], [short["id"], long["id"]])

    def test_but_nothing_starves(self):
        self.job(winpid=100, eta_s=100)
        long = self.job(script="long.mjs", winpid=200, eta_s=200)
        self.run_for(700)
        short = self.job(script="short.mjs", winpid=300, eta_s=20)
        self.assertEqual([t["id"] for t in self.q.order()], [long["id"], short["id"]])

    def test_a_cpu_job_goes_past_the_gpu_line(self):
        self.job(winpid=100)
        self.job(script="b.mjs", winpid=200)
        suite = self.job(script="run_test.sh", winpid=300, gpu=0)
        self.assertTrue(self.running(suite))

    def test_a_booking_wider_than_the_box_is_the_whole_box(self):
        suite = self.job(script="run_test_par.sh", winpid=100, gpu=0,
                         slots=testq.CAPACITY["cpu"] + 4)
        self.assertTrue(self.running(suite))
        self.assertEqual(suite["slots"], testq.CAPACITY["cpu"])

    def test_a_dead_shell_gives_its_slots_back(self):
        first = self.job(winpid=100)
        second = self.job(script="b.mjs", winpid=200)
        self.dead.add(100)
        self.run_for(5)
        self.assertEqual(self.verdict(first), "reclaimed")
        self.assertTrue(self.running(second))


class TwoGpus(Box):
    GPUS = 2

    def test_two_windows_at_once_and_a_third_waits(self):
        first = self.job(winpid=100)
        second = self.job(script="b.mjs", winpid=200)
        third = self.job(script="c.mjs", winpid=300)
        self.assertTrue(self.running(first) and self.running(second))
        self.assertFalse(self.running(third))
        self.assertEqual(third["blocked_on"], "waiting for the GPU")
        self.q.release(first["id"], 0)
        self.assertTrue(self.running(third))

    def test_a_job_that_books_both_waits_for_both(self):
        clip = self.job(winpid=100)
        perf = self.job(script="run_perf.sh", winpid=200, gpu=2)
        late = self.job(script="c.mjs", winpid=300)
        self.assertFalse(self.running(perf))
        self.assertEqual(perf["blocked_on"], "waiting for the GPU")
        # The free half is held for the job at the head of the line.
        self.assertFalse(self.running(late))
        self.q.release(clip["id"], 0)
        self.assertTrue(self.running(perf))
        self.assertFalse(self.running(late))

    def test_asking_for_more_gpu_than_there_is_means_all_of_it(self):
        perf = self.job(script="run_perf.sh", winpid=100, gpu=99)
        self.assertTrue(self.running(perf))
        self.assertEqual(perf["gpu"], 2)


class Parked(Box):
    def abandon(self, ticket):
        """Its client stops polling, the way a killed one does."""
        for _ in range(16):
            self.clock += 5
            for other in self.q.queue:
                if other is not ticket:
                    other["last_poll"] = self.clock
            self.q.tick()
        self.assertEqual(self.verdict(ticket), "abandoned")

    def test_a_retry_gets_its_wait_back(self):
        self.job(winpid=100, eta_s=1000)
        first = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=200, eta_s=100)
        self.run_for(400)
        self.abandon(first)
        rival = self.job(script="clip.sh", arg="gates", tree=WT_B, winpid=300, eta_s=100)
        self.run_for(60)
        again = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=400, eta_s=100)
        self.assertEqual(again["enqueued_at"], first["enqueued_at"])
        self.assertGreater(again["resumed_s"], 500)
        self.assertEqual([t["id"] for t in self.q.order()], [again["id"], rival["id"]])

    def test_a_retry_that_beats_the_tick_still_gets_it(self):
        self.job(winpid=100, eta_s=1000)
        first = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=200, eta_s=100)
        self.run_for(400)
        self.dead.add(200)                                 # killed this instant
        again = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=400, eta_s=100)
        self.assertEqual(self.verdict(first), "abandoned")
        self.assertEqual(again["enqueued_at"], first["enqueued_at"])
        self.assertEqual(len(self.q.queue), 1)

    def test_two_live_copies_of_one_job_are_two_jobs(self):
        self.job(winpid=100, eta_s=1000)
        first = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=200, eta_s=100)
        self.run_for(100)
        second = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=400, eta_s=100)
        self.assertNotIn("resumed_s", second)
        self.assertEqual(len(self.q.queue), 2)
        self.assertIsNone(self.verdict(first))

    def test_only_the_same_job(self):
        self.job(winpid=100, eta_s=1000)
        first = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=200)
        self.abandon(first)
        other = self.job(script="clip.sh", arg="gates", tree=WT_A, winpid=400)
        self.assertNotIn("resumed_s", other)

    def test_and_not_for_ever(self):
        self.job(winpid=100, eta_s=1000)
        first = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=200)
        self.abandon(first)
        self.run_for(testq.PARK_SECONDS + 10)
        again = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=400)
        self.assertNotIn("resumed_s", again)

    def test_a_restarted_daemon_keeps_the_places(self):
        holder = self.job(winpid=100, eta_s=1000)
        self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=200, eta_s=100)
        self.run_for(400)
        self.q.save_state()
        self.clock += 10
        self.q = testq.Queue(dict(testq.CAPACITY, gpu=self.GPUS))
        self.assertTrue(self.running(holder))
        rival = self.job(script="clip.sh", arg="gates", tree=WT_B, winpid=300, eta_s=100)
        again = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=400, eta_s=100)
        self.assertEqual(again["resumed_s"], 410.0)
        self.assertEqual(again["waits"], {"gpu": 400.0, "away": 10.0})
        self.assertEqual([t["id"] for t in self.q.order()], [again["id"], rival["id"]])

    def test_a_cancelled_ticket_is_not_parked(self):
        self.job(winpid=100, eta_s=1000)
        first = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=200)
        self.q.cancel(first["id"])
        again = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=400)
        self.assertNotIn("resumed_s", again)


class Docked(Box):
    """What the engines outside the queue cost it in slots."""

    def outside(self, count):
        self.rates = dict((pid, 1.0) for pid in range(900, 900 + count))
        for pid in self.rates:
            self.engine(pid, 997, WT_B)
        self.run_for(20, rates=self.rates)

    def wide(self, slots, winpid):
        return self.job(script="run_test_par.sh", arg=str(slots), tree=SNAP,
                        winpid=winpid, gpu=0, slots=slots)

    def too_wide(self, slots):
        """A job this wide waits. It is taken out again so that it is not
        at the head, holding slots back from the next one asked about."""
        ticket = self.wide(slots, 100)
        self.assertFalse(self.running(ticket))
        self.assertEqual(ticket["waiting_on"], "outside")
        self.q.cancel(ticket["id"])

    def test_the_first_few_cost_nothing(self):
        self.outside(testq.STRAY_FREE)
        self.assertTrue(self.running(self.wide(testq.CAPACITY["cpu"], 100)))

    def test_the_next_one_costs_a_slot(self):
        self.outside(testq.STRAY_FREE + 1)
        self.too_wide(testq.CAPACITY["cpu"])
        self.assertTrue(self.running(self.wide(testq.CAPACITY["cpu"] - 1, 200)))
        self.assertEqual(self.q.snapshot()["used"]["stray_penalty"], 1)

    def test_a_crowd_of_them_cannot_stop_the_queue(self):
        self.outside(3 * testq.CAPACITY["cpu"])
        self.too_wide(testq.STRAY_FLOOR + 1)
        self.assertTrue(self.running(self.wide(testq.STRAY_FLOOR, 200)))

    def test_the_run_is_still_told_about_every_one(self):
        self.outside(testq.STRAY_FREE)
        suite = self.job(script="run_test.sh", tree=SNAP, winpid=100, gpu=0)
        self.assertEqual(suite["box_engines"], testq.STRAY_FREE + 1)


class Exclusive(Box):
    """run_mp wants the box to itself, and there is nearly always an engine
    on it that the queue did not start."""

    def stray(self):
        self.engine(900, 1, PROG + "/godot")
        self.run_for(20, rates={900: 1.0})

    def mp(self, **more):
        return self.job(script="run_mp.sh", arg="30", tree=WT_A, winpid=200,
                        gpu=0, exclusive=True, engines=2, **more)

    def test_an_engine_outside_the_queue_is_waited_out_and_no_longer(self):
        self.stray()
        mp = self.mp()
        self.run_for(testq.EXCLUSIVE_STRAY_PATIENCE - 20, rates={900: 1.0})
        self.assertFalse(self.running(mp))
        self.assertEqual(mp["blocked_on"],
                         "waiting for a quiet box -- 1 unmanaged engine(s)")
        self.run_for(30, rates={900: 1.0})
        self.assertTrue(self.running(mp))

    def test_and_the_box_is_not_emptied_for_it_meanwhile(self):
        self.stray()
        mp = self.mp()
        check = self.job(script="run_check.sh", tree=WT_B, winpid=300, gpu=0)
        self.assertTrue(self.running(check))
        self.assertFalse(self.running(mp))

    def test_until_its_patience_runs_out(self):
        self.stray()
        mp = self.mp()
        check = self.job(script="run_check.sh", tree=WT_B, winpid=300, gpu=0)
        self.engine(301, 300, WT_B)
        self.run_for(testq.EXCLUSIVE_STRAY_PATIENCE + 10, rates={900: 1.0, 301: 1.0})
        later = self.job(script="run_test.sh", tree=SNAP, winpid=400, gpu=0)
        self.assertFalse(self.running(later))
        del self.table[301]
        self.q.release(check["id"], 0)
        self.assertTrue(self.running(mp))
        self.assertFalse(self.running(later))

    def test_a_quiet_box_is_drained_for_it_at_once(self):
        first = self.job(script="run_test.sh", winpid=100, gpu=0, eta_s=100)
        mp = self.mp(eta_s=30)
        later = self.job(script="run_check.sh", tree=WT_B, winpid=300, gpu=0, eta_s=60)
        self.assertFalse(self.running(later))
        self.q.release(first["id"], 0)
        self.assertTrue(self.running(mp))
        self.assertFalse(self.running(later))


class WaitedOn(Box):
    """What each queued second was for, which is what goes in the history."""

    def test_the_gpu(self):
        first = self.job(winpid=100)
        second = self.job(script="b.mjs", winpid=200)
        self.run_for(60)
        self.q.release(first["id"], 0)
        self.assertEqual(second["waits"], {"gpu": 60.0})
        self.q.release(second["id"], 0)
        self.assertEqual(json.loads(self.q.history[-1]["waited_on"]), {"gpu": 60.0})

    def test_a_job_that_did_not_wait_has_nothing_to_say(self):
        first = self.job(winpid=100)
        self.q.release(first["id"], 0)
        self.assertIsNone(self.q.history[-1]["waited_on"])

    def test_engines_outside_the_queue(self):
        rates = {}
        for pid in range(900, 900 + testq.CAPACITY["cpu"]):
            self.engine(pid, 997, WT_B)
            rates[pid] = 1.0
        self.run_for(20, rates=rates)
        suite = self.job(script="run_test_par.sh", tree=SNAP, winpid=100, gpu=0,
                         slots=testq.CAPACITY["cpu"])
        self.assertFalse(self.running(suite))
        self.assertEqual(suite["blocked_on"],
                         "waiting for slots -- 8 unmanaged engine(s) on the box")
        self.run_for(30, rates=rates)
        self.assertEqual(suite["waits"], {"outside": 30.0})
        self.assertIn("1 job(s) waiting on engines outside the queue: mfrs/hats x8",
                      sys.stderr.getvalue())

    def test_a_slot_kept_for_a_job_in_front(self):
        self.job(script="run_test_par.sh", winpid=100, gpu=0, slots=6)
        big = self.job(script="big.sh", winpid=200, gpu=0, slots=4, eta_s=10)
        small = self.job(script="small.sh", winpid=300, gpu=0, slots=1, eta_s=100)
        self.assertEqual(big["waiting_on"], "slots")
        self.assertFalse(self.running(small))
        self.assertEqual(small["blocked_on"],
                         "waiting behind a job ahead of it in the queue")

    def test_a_retry_brings_its_reasons_with_it(self):
        self.job(winpid=100, eta_s=1000)
        self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=200, eta_s=100)
        self.run_for(400)
        self.dead.add(200)
        self.run_for(60)
        again = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=400, eta_s=100)
        self.assertEqual(again["waits"], {"gpu": 400.0, "away": 55.0})


class Cancel(Box):
    def test_a_running_job_is_killed_and_the_next_one_starts(self):
        first = self.job(tree=WT_A, winpid=40)
        self.engine(41, 40, WT_A)
        second = self.job(script="b.mjs", winpid=200)
        self.assertEqual(self.q.cancel(first["id"]), "cancelled a running job")
        self.assertEqual(sorted(self.killed), [40, 41])
        self.assertEqual(self.verdict(first), "cancelled")
        self.assertTrue(self.running(second))

    def test_it_is_cancelled_whoever_notices_first(self):
        first = self.job(winpid=40)
        first["cancelling"] = True                         # the kill is under way
        self.q.release(first["id"], 137)                   # its exit trap gets in
        self.assertEqual(self.verdict(first), "cancelled")


class OldBashClients(Box):
    """The vendored bash clients take any reply with "granted" and then
    "true" in it for a grant. See not_granted_flag."""

    def looks_granted(self, reply):
        text = json.dumps(reply)
        return '"granted"' in text and "true" in text.split('"granted"', 1)[1]

    def test_a_queued_reply_never_says_true(self):
        self.job(winpid=100)                               # no estimate: a floor
        second = self.job(script="b.mjs", winpid=200)
        self.assertEqual(self.q.start_in(second, self.q.order()), (None, True))
        with mock.patch.object(testq, "WAIT_POLL_SECONDS", 0.0):
            reply = self.q.poll(second["id"])
        self.assertFalse(reply["granted"])
        self.assertEqual(reply["start_in_floor"], 1)
        self.assertFalse(self.looks_granted(reply))

    def test_nor_does_the_answer_to_the_acquire(self):
        self.job(winpid=100)
        second = self.job(script="b.mjs", winpid=200, exclusive=True, idle_ok=True)
        reply = testq.acquire_reply(self.q, second)
        self.assertFalse(reply["granted"])
        self.assertFalse(self.looks_granted(reply))

    def test_a_grant_still_does(self):
        first = self.job(winpid=100)
        self.assertTrue(self.looks_granted(testq.acquire_reply(self.q, first)))
        self.assertTrue(self.looks_granted(self.q.poll(first["id"])))


class StartIn(Box):
    def test_the_gpu_line_adds_up(self):
        self.job(winpid=100, eta_s=100)
        self.clock += 40
        self.job(script="b.mjs", winpid=200, eta_s=50)
        third = self.job(script="c.mjs", winpid=300, eta_s=80)
        seconds, floor = self.q.start_in(third, self.q.order())
        self.assertEqual((seconds, floor), (110, False))

    def test_an_unknown_ahead_makes_it_a_floor(self):
        self.job(winpid=100, eta_s=100)
        self.job(script="b.mjs", winpid=200)
        self.run_for(700)                                  # so it has aged ahead
        third = self.job(script="c.mjs", winpid=300, eta_s=80)
        seconds, floor = self.q.start_in(third, self.q.order())
        self.assertTrue(floor)
        self.assertEqual(seconds, 15)

    def test_a_job_past_its_estimate_is_not_due_this_second(self):
        self.job(winpid=100, eta_s=100)
        self.clock += 500
        second = self.job(script="b.mjs", winpid=200, eta_s=50)
        self.assertEqual(self.q.start_in(second, self.q.order()), (15, False))

    def test_it_rides_on_the_snapshot(self):
        self.job(winpid=100, eta_s=100)
        self.job(script="b.mjs", winpid=200, eta_s=50)
        row = self.q.snapshot()["queued"][0]
        self.assertEqual((row["start_in_s"], row["start_in_floor"]), (100, False))


class Estimates(Box):
    def ran(self, script, arg, seconds, size=None, tree="t", slots=1):
        self.q.append_history({
            "id": "J0", "tree": tree, "script": script, "arg": arg, "size": size,
            "exit": 0, "verdict": "released", "dur_s": seconds, "queued_s": 0,
            "finished": self.clock, "slots": slots})
        self.clock += 1

    def test_a_job_that_went_wide_is_estimated_by_its_work(self):
        # Oathkeep's `all`: three runs on one engine, then two on four.
        for seconds, slots in ((2400, 1), (2400, 1), (2400, 1), (650, 4), (610, 4)):
            self.ran("run_test.sh", "all", seconds, slots=slots)
        self.assertEqual(self.eta("run_test.sh", "all", slots=4, slots_min=1), 600)
        self.assertEqual(self.eta("run_test.sh", "all", slots=2), 1200)

    def test_a_rigid_job_is_still_the_plain_median(self):
        for seconds in (100, 120, 900):
            self.ran("run_mp.sh", "", seconds, slots=2)
        self.assertEqual(self.eta("run_mp.sh", "", slots=2), 120)

    def eta(self, script, arg, **body):
        return self.q.estimate(script, arg, "t", body)

    def test_the_median_of_the_same_job(self):
        for seconds in (40, 50, 300):
            self.ran("clip.sh", "hats", seconds)
        self.assertEqual(self.eta("clip.sh", "hats"), 50)

    def test_a_count_in_the_arg_is_fitted(self):
        # 10 s to start and 6 s a scenario.
        for n in (5, 47, 157):
            self.ran("shoot.mjs", "%d scenarios" % n, 10 + 6 * n)
        self.assertAlmostEqual(self.eta("shoot.mjs", "148 scenarios"), 898, delta=1)

    def test_a_declared_size_is_fitted_and_beats_the_arg_median(self):
        for seconds in (30, 30, 60):
            self.ran("capture.mjs", "warehouse", 18 + seconds, size=seconds)
        self.assertAlmostEqual(self.eta("capture.mjs", "warehouse", size=600), 618, delta=1)

    def test_runs_at_this_very_size_beat_the_line(self):
        for n, seconds in ((5, 40), (47, 290), (148, 500), (148, 520), (148, 2000)):
            self.ran("shoot.mjs", "%d scenarios" % n, seconds)
        self.assertEqual(self.eta("shoot.mjs", "148 scenarios"), 520)

    def test_kinds_do_not_mix(self):
        for n in (5, 47, 157):
            self.ran("shoot.mjs", "%d scenarios" % n, 10 + 6 * n)
        self.assertIsNone(self.q.sized_estimate("shoot.mjs", "levels", 3))

    def test_bigger_coming_out_faster_is_not_a_law(self):
        for n, seconds in ((5, 300), (47, 200), (157, 100)):
            self.ran("shoot.mjs", "%d scenarios" % n, seconds)
        self.assertIsNone(self.q.sized_estimate("shoot.mjs", "scenarios", 400))

    def test_any_run_of_the_script_is_better_than_nothing(self):
        for arg, seconds in (("a.json", 10), ("b.json", 30), ("c.json", 20)):
            self.ran("shoot.mjs", arg, seconds)
        self.assertEqual(self.eta("shoot.mjs", "never-seen.json"), 20)

    def test_and_nothing_is_still_nothing(self):
        self.assertIsNone(self.eta("brand-new.mjs", "x"))

    def test_the_size_is_recorded(self):
        job = self.job(script="capture.mjs", arg="warehouse", size=30)
        self.q.release(job["id"], 0)
        with testq.db() as conn:
            row = conn.execute("SELECT size FROM runs WHERE job_id=?", (job["id"],)).fetchone()
        self.assertEqual(row["size"], 30)


# ---------------------------------------------------------------------------
# The real box
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.name == "nt", "reads Windows process memory")
class RuntimeDir(unittest.TestCase):
    """The directory had one project's name on it, and moves once."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="testq-base-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        env = dict(os.environ, LOCALAPPDATA=self.base)
        env.pop("TESTQ_HOME", None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(sys, "stderr", io.StringIO())
        patch.start()
        self.addCleanup(patch.stop)
        self.legacy = os.path.join(self.base, testq.LEGACY_RUNTIME_NAME)
        self.home = os.path.join(self.base, testq.RUNTIME_NAME)

    def old_install(self):
        os.makedirs(self.legacy)
        with open(os.path.join(self.legacy, "testq.db"), "w") as fh:
            fh.write("a week of runs")

    def history(self):
        with open(os.path.join(testq.runtime_dir(), "testq.db")) as fh:
            return fh.read()

    def test_a_new_machine_gets_the_new_name(self):
        self.assertEqual(testq.runtime_dir(), self.home)
        self.assertEqual(testq.migrate_runtime_dir(), "")

    def test_the_old_one_is_read_until_it_is_moved(self):
        self.old_install()
        self.assertEqual(testq.runtime_dir(), self.legacy)

    def test_it_moves_with_everything_in_it(self):
        self.old_install()
        self.assertEqual(testq.migrate_runtime_dir(), self.home)
        self.assertFalse(os.path.exists(self.legacy))
        self.assertEqual(testq.runtime_dir(), self.home)
        self.assertEqual(self.history(), "a week of runs")

    def test_a_directory_that_will_not_let_go_is_copied(self):
        self.old_install()
        rename = os.rename

        def held(src, dst):
            if src == self.legacy:
                raise PermissionError("the tray icon is sitting in it")
            return rename(src, dst)

        with mock.patch.object(os, "rename", held):
            self.assertEqual(testq.migrate_runtime_dir(), self.home)
        self.assertEqual(testq.runtime_dir(), self.home)
        self.assertEqual(self.history(), "a week of runs")
        self.assertTrue(os.path.exists(os.path.join(self.legacy, "MOVED.txt")))
        self.assertFalse(os.path.exists(self.home + ".moving"))

    def test_once_moved_the_old_name_is_never_read_again(self):
        self.old_install()
        testq.migrate_runtime_dir()
        os.makedirs(self.legacy)                           # something put it back
        self.assertEqual(testq.runtime_dir(), self.home)
        self.assertEqual(testq.migrate_runtime_dir(), "")

    def test_a_home_somebody_chose_is_left_alone(self):
        self.old_install()
        with mock.patch.dict(os.environ, {"TESTQ_HOME": self.base}):
            self.assertEqual(testq.runtime_dir(), self.base)
            self.assertEqual(testq.migrate_runtime_dir(), "")
        self.assertTrue(os.path.isdir(self.legacy))


class Capacity(unittest.TestCase):
    """The size of the box is this machine's to say, in config.json."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="testq-config-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patch = mock.patch.dict(os.environ, {"TESTQ_HOME": self.home})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(sys, "stderr", io.StringIO())
        patch.start()
        self.addCleanup(patch.stop)

    def config(self, text):
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            fh.write(text)

    def test_no_file_is_the_defaults(self):
        self.assertEqual(testq.load_capacity(), testq.CAPACITY)

    def test_a_machine_says_its_own(self):
        self.config('{"capacity": {"cpu": 6, "gpu": 1}}')
        self.assertEqual(testq.load_capacity(), {"cpu": 6, "gpu": 1})

    def test_one_number_leaves_the_other(self):
        self.config('{"capacity": {"cpu": 6}}')
        self.assertEqual(testq.load_capacity(),
                         dict(testq.CAPACITY, cpu=6))

    def test_a_box_with_no_card(self):
        self.config('{"capacity": {"gpu": 0}}')
        self.assertEqual(testq.load_capacity()["gpu"], 0)

    def test_a_typo_cannot_stop_the_queue(self):
        for text in ('{"capacity": {"cpu": 0}}', '{"capacity": {"cpu": "6"}}',
                     '{"capacity": {"cpu": 6.5}}', '{"capacity": {"cpu": true}}',
                     '{"capacity": {"gpu": -1}}', '{"capacity": 6}',
                     '[6, 2]', '{"capacity": {"cpu": 6,}}', ''):
            self.config(text)
            self.assertEqual(testq.load_capacity(), testq.CAPACITY, text)

    def test_the_queue_grants_to_it(self):
        self.config('{"capacity": {"cpu": 3}}')
        q = testq.Queue(testq.load_capacity())
        self.assertEqual(q.capacity["cpu"], 3)
        self.assertEqual(testq.CAPACITY["cpu"], 8)


class GpuNotIdle(Box):
    """A free window is not left idle behind CPU-only work. The box was at
    8/8 CPU slots with the GPU under 10% busy: windowed jobs queue behind
    shortest-first CPU work, and each freed slot went to the CPU job."""

    GPUS = 3

    def fill(self, cpu, gpu=0, eta=1000):
        leases = []
        for i in range(cpu):
            leases.append(self.job(script="cpu%d.sh" % i, winpid=1000 + i, gpu=0,
                                   slots=1, eta_s=eta))
        for i in range(gpu):
            leases.append(self.job(script="win%d.sh" % i, winpid=2000 + i, gpu=1,
                                   slots=1, eta_s=eta))
        for lease in leases:
            self.assertTrue(self.running(lease))
        return leases

    def test_the_default_card_takes_three_windows(self):
        self.assertEqual(testq.CAPACITY["gpu"], 3)

    def test_a_freed_slot_goes_to_the_windowed_job_not_the_shorter_cpu_one(self):
        leases = self.fill(8)
        short = self.job(script="quick.sh", winpid=500, gpu=0, slots=1, eta_s=10)
        shot = self.job(script="run_shots.sh", winpid=501, gpu=1, slots=1, eta_s=100)
        self.assertEqual([t["id"] for t in self.q.order()], [short["id"], shot["id"]])
        self.q.release(leases[0]["id"], 0)
        self.assertTrue(self.running(shot))
        self.assertFalse(self.running(short))
        self.assertEqual(short["blocked_on"],
                         "waiting for a slot kept for a windowed job, so the GPU is not idle")
        # And the CPU job has the next one.
        self.q.release(leases[1]["id"], 0)
        self.assertTrue(self.running(short))

    def test_with_no_window_free_the_cpu_job_goes_first(self):
        leases = self.fill(5, gpu=3)
        short = self.job(script="quick.sh", winpid=500, gpu=0, slots=1, eta_s=10)
        shot = self.job(script="run_shots.sh", winpid=501, gpu=1, slots=1, eta_s=100)
        self.q.release(leases[0]["id"], 0)                 # a CPU slot, no window
        self.assertTrue(self.running(short))
        self.assertFalse(self.running(shot))

    def test_a_head_that_is_reserving_keeps_what_frees(self):
        leases = self.fill(8)
        wide = self.job(script="run_test_par.sh", winpid=500, gpu=0, slots=6, eta_s=5)
        short = self.job(script="quick.sh", winpid=501, gpu=0, slots=1, eta_s=10)
        shot = self.job(script="run_shots.sh", winpid=502, gpu=1, slots=1, eta_s=100)
        self.q.release(leases[0]["id"], 0)
        for ticket in (wide, short, shot):
            self.assertFalse(self.running(ticket))
        for lease in leases[1:6]:
            self.q.release(lease["id"], 0)
        self.assertTrue(self.running(wide))
        self.assertFalse(self.running(shot))
        self.q.release(leases[6]["id"], 0)
        self.assertTrue(self.running(shot))

    def test_a_job_that_has_aged_is_never_held(self):
        leases = self.fill(8, eta=5000)
        old = self.job(script="quick.sh", winpid=500, gpu=0, slots=1, eta_s=10)
        self.run_for(700)
        shot = self.job(script="run_shots.sh", winpid=501, gpu=1, slots=1, eta_s=100)
        self.q.release(leases[0]["id"], 0)
        self.assertTrue(self.running(old))
        self.assertFalse(self.running(shot))

    def test_when_both_fit_nobody_is_held(self):
        leases = self.fill(8)
        pair = self.job(script="pair.sh", winpid=500, gpu=0, slots=2, eta_s=10)
        shot = self.job(script="run_shots.sh", winpid=501, gpu=1, slots=1, eta_s=100)
        for lease in leases[:3]:
            self.dead.add(lease["winpid"])
        self.run_for(5)
        self.assertTrue(self.running(pair) and self.running(shot))

    def test_a_windowed_job_stuck_on_a_mutex_holds_nothing(self):
        leases = self.fill(7)
        clip = self.job(script="clip.sh", winpid=400, gpu=1, slots=1, eta_s=50,
                        mutexes=["clip:a"])
        self.assertTrue(self.running(clip))                # holds the mutex
        short = self.job(script="quick.sh", winpid=500, gpu=0, slots=1, eta_s=10)
        twin = self.job(script="clip2.sh", winpid=501, gpu=1, slots=1, eta_s=100,
                        mutexes=["clip:a"])
        self.assertFalse(self.running(short) or self.running(twin))
        self.q.release(leases[0]["id"], 0)
        self.assertTrue(self.running(short))               # twin cannot start anyway
        self.assertFalse(self.running(twin))

    def test_a_measurement_waiting_for_the_card_does_not_hold_the_line(self):
        # A window is in use, so run_perf's ask for all three waits and
        # reserves the GPU; the shot behind it cannot start either. Holding
        # the CPU job for that shot would idle the slot until aging.
        leases = self.fill(7, gpu=1)
        short = self.job(script="quick.sh", winpid=500, gpu=0, slots=1, eta_s=10)
        perf = self.job(script="run_perf.sh", winpid=501, gpu=3, slots=1, eta_s=20)
        shot = self.job(script="run_shots.sh", winpid=502, gpu=1, slots=1, eta_s=100)
        self.assertEqual([t["id"] for t in self.q.order()],
                         [short["id"], perf["id"], shot["id"]])
        self.q.release(leases[0]["id"], 0)
        self.assertTrue(self.running(short))
        self.assertFalse(self.running(shot))


class Duplicates(Box):
    """One job, asked for twice by one worktree."""

    def queue_two(self, **more):
        self.job(script="hog.sh", arg="y", winpid=100, tree=WT_A)             # holds the card
        first = self.job(script="run_shots.sh", arg="sdt_hf", winpid=200, tree=WT_A, **more)
        second = self.job(script="run_shots.sh", arg="sdt_hf", winpid=201, tree=WT_A, **more)
        return first, second

    def test_the_second_is_refused_and_names_the_first(self):
        first, second = self.queue_two(dedupe=True)
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.q.queue), 1)
        reply = testq.acquire_reply(self.q, second)
        self.assertEqual(reply["duplicate"], 1)
        self.assertEqual(reply["existing"], first["id"])
        self.assertEqual(reply["position"], 1)
        # No ticket to mistake for its own, and nothing that reads as a grant.
        self.assertNotIn("ticket", reply)
        self.assertNotIn("true", json.dumps(reply))

    def test_a_client_that_did_not_say_it_can_be_told_gets_a_second_ticket(self):
        first, second = self.queue_two()
        self.assertFalse(second.get("duplicate"))
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(len(self.q.queue), 2)

    def test_the_same_client_asking_again_gets_its_own_ticket_back(self):
        self.job(script="hog.sh", winpid=100, tree=WT_A)
        first = self.job(script="run_shots.sh", arg="a", winpid=200, tree=WT_A, dedupe=True)
        again = self.job(script="run_shots.sh", arg="a", winpid=200, tree=WT_A, dedupe=True)
        self.assertEqual(again["id"], first["id"])
        self.assertFalse(again.get("duplicate"))
        self.assertEqual(len(self.q.queue), 1)

    def test_a_different_arg_or_worktree_is_a_different_job(self):
        self.job(script="hog.sh", winpid=100, tree=WT_A)
        a = self.job(script="run_shots.sh", arg="a", winpid=200, tree=WT_A, dedupe=True)
        b = self.job(script="run_shots.sh", arg="b", winpid=201, tree=WT_A, dedupe=True)
        c = self.job(script="run_shots.sh", arg="a", winpid=202, tree=WT_B, dedupe=True)
        self.assertEqual(len({a["id"], b["id"], c["id"]}), 3)
        self.assertEqual(len(self.q.queue), 3)

    def test_a_copy_that_is_running_is_not_in_the_way(self):
        running = self.job(script="run_shots.sh", arg="a", winpid=100, tree=WT_A, dedupe=True)
        self.assertTrue(self.running(running))
        again = self.job(script="run_shots.sh", arg="a", winpid=200, tree=WT_A, dedupe=True)
        self.assertFalse(again.get("duplicate"))
        self.assertEqual(len(self.q.queue), 1)

    def test_a_copy_whose_client_died_is_not_in_the_way(self):
        first, _ = self.queue_two()
        self.q.cancel(self.q.queue[-1]["id"])
        self.dead.add(first["winpid"])
        retry = self.job(script="run_shots.sh", arg="sdt_hf", winpid=202, tree=WT_A,
                         dedupe=True)
        self.assertFalse(retry.get("duplicate"))
        self.assertEqual(len(self.q.queue), 1)


class Orphans(Box):
    def test_a_queued_job_cancelled_by_its_own_client_is_orphaned(self):
        self.job(winpid=100)
        waiting = self.job(script="b.mjs", winpid=200)
        self.assertEqual(self.q.cancel(waiting["id"], "orphan"), "removed a queued job")
        self.assertEqual(self.verdict(waiting), "orphaned")
        self.assertEqual(self.q.queue, [])

    def test_a_running_job_cancelled_by_its_own_client_is_orphaned(self):
        first = self.job(tree=WT_A, winpid=40)
        self.engine(41, 40, WT_A)
        second = self.job(script="b.mjs", winpid=200)
        self.assertEqual(self.q.cancel(first["id"], "orphan"), "cancelled a running job")
        self.assertEqual(sorted(self.killed), [40, 41])
        self.assertEqual(self.verdict(first), "orphaned")
        self.assertTrue(self.running(second))

    def test_the_page_and_the_terminal_are_still_just_cancelled(self):
        running = self.job(winpid=100)
        self.assertEqual(self.q.cancel(running["id"]), "cancelled a running job")
        self.assertEqual(self.verdict(running), "cancelled")

    def test_it_is_not_an_alert(self):
        self.assertEqual(testq.alert_for(None, "orphaned"), "")


class CancelCommand(unittest.TestCase):
    STATE = {"queued": [
        {"id": "J5", "tree_id": "hats-a1b2c3", "tree_path": WT_B},
        {"id": "J6", "tree_id": "weekend-features-d4e5f6", "tree_path": WT_A},
        {"id": "J7", "tree_id": "hats-a1b2c3", "tree_path": WT_B},
    ], "running": [{"id": "J1", "tree_id": "hats-a1b2c3", "tree_path": WT_B}]}

    def run_cancel(self, jobs=(), tree="", known=("J5", "J6", "J7", "J1")):
        sent = []

        def fake(port, path, payload=None, timeout=5.0):
            if path == "/state":
                return 200, self.STATE
            sent.append(payload["job"])
            if payload["job"] in known:
                return 200, {"cancelled": "removed a queued job"}
            return 404, {"error": "no such job"}

        out = io.StringIO()
        args = mock.Mock(port=43999, jobs=list(jobs), tree=tree)
        with mock.patch.object(testq, "http_json", fake), \
                mock.patch("sys.stdout", out):
            code = testq.cmd_cancel(args)
        return code, sent, out.getvalue()

    def test_by_id(self):
        code, sent, out = self.run_cancel(["J6"])
        self.assertEqual((code, sent), (0, ["J6"]))
        self.assertIn("J6: removed a queued job", out)

    def test_a_worktrees_queued_jobs_and_not_the_running_one(self):
        code, sent, _ = self.run_cancel(tree="hats")
        self.assertEqual((code, sent), (0, ["J5", "J7"]))

    def test_a_fragment_of_a_name_cancels_nothing(self):
        code, sent, out = self.run_cancel(tree="weekend")
        self.assertEqual(sent, [])
        self.assertIn("no queued jobs", out)

    def test_the_whole_id_works_and_what_it_cancels_is_shown(self):
        _, sent, out = self.run_cancel(tree="weekend-features-d4e5f6")
        self.assertEqual(sent, ["J6"])
        self.assertIn("will cancel J6", out)

    def test_a_name_shared_by_two_checkouts_is_refused(self):
        state = {"queued": self.STATE["queued"] + [
            {"id": "J8", "tree_id": "hats-ffffff", "tree_path": WT_A}], "running": []}
        with mock.patch.object(
                type(self), "STATE", state):
            code, sent, out = self.run_cancel(tree="hats")
        self.assertEqual((code, sent), (2, []))
        self.assertIn("hats-ffffff", out)

    def test_an_id_that_is_gone_says_so_and_fails(self):
        code, sent, out = self.run_cancel(["J99"])
        self.assertEqual(code, 1)
        self.assertIn("no such job", out)

    def test_nothing_to_cancel(self):
        self.assertEqual(self.run_cancel()[0], 2)
        code, sent, out = self.run_cancel(tree="nope")
        self.assertEqual((code, sent), (0, []))
        self.assertIn("no queued jobs", out)

    def test_it_is_in_the_help(self):
        shown = io.StringIO()
        with mock.patch("sys.stdout", shown), self.assertRaises(SystemExit):
            testq.main(["cancel", "--help"])
        self.assertIn("--tree", shown.getvalue())
        shown = io.StringIO()
        with mock.patch("sys.stdout", shown), self.assertRaises(SystemExit):
            testq.main(["--help"])
        self.assertIn("cancel", shown.getvalue())


class StatsOutcomes(Box):
    def row(self, script, arg, exit_code, verdict, ago):
        ticket = self.job(script=script, arg=arg, winpid=100)
        self.q.leases.pop(ticket["id"], None)
        row = self.q.history_row(ticket, exit_code, verdict)
        row["finished"] = self.clock - ago
        self.q.append_history(row)

    def stats(self, days=1.0, tree=""):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            testq.cmd_stats(mock.Mock(days=days, tree=tree, limit=12))
        return out.getvalue()

    def line(self, text, job):
        return next(l for l in text.splitlines() if l.strip().startswith(job))

    def test_when_a_long_job_last_passed_is_on_its_row(self):
        self.row("run_test.sh", "all", 0, "released", 3 * 86400)    # outside the window
        self.row("run_test.sh", "all", 1, "released", 3600)
        self.row("run_test.sh", "all", 0, "released", 7200)
        self.row("run_shots.sh", "x", 0, "released", 60)
        text = self.stats()
        self.assertIn("last green 2.0h ago", self.line(text, "run_test.sh all"))
        self.assertIn("last green 60s ago", self.line(text, "run_shots.sh x"))

    def test_a_job_that_has_never_passed_says_so(self):
        self.row("run_mp.sh", "", 1, "released", 600)
        self.row("run_mp.sh", "", None, "cancelled", 300)
        self.assertIn("never green on record", self.line(self.stats(), "run_mp.sh"))

    def test_the_window_does_not_hide_the_last_green(self):
        self.row("run_test.sh", "all", 0, "released", 3 * 86400)
        self.row("run_test.sh", "all", 1, "released", 600)
        line = self.line(self.stats(days=1.0), "run_test.sh all")
        self.assertIn("0 passed, 1 failed", line)
        self.assertIn("last green 3d ago", line)

    def test_the_tree_filter_applies(self):
        self.row("run_test.sh", "all", 0, "released", 600)
        self.assertIn("no runs recorded", self.stats(tree="no-such-tree"))

    def test_cancelled_and_lost_are_told_apart(self):
        for verdict in ("cancelled", "orphaned", "abandoned", "abandoned",
                        "reclaimed", "stalled", "overran"):
            self.row("run_test.sh", "all", None, verdict, 600)
        text = self.stats()
        line = self.line(text, "run_test.sh all")
        self.assertIn("1 cancelled", line)
        self.assertIn("1 orphaned", line)
        self.assertIn("2 left the queue", line)
        self.assertIn("1 lost mid-run", line)
        self.assertIn("2 killed by the daemon", line)
        self.assertNotIn("cancelled/lost", text)
        # Each is explained once, under the table.
        self.assertIn("lost mid-run: the client vanished while running", text)
        self.assertIn("orphaned: cancelled by their own client", text)


class RequestAllowed(unittest.TestCase):
    """Who may talk to the daemon: its clients and its own page, not a
    website that happens to be open in the browser on the same machine."""

    def test_clients_send_a_loopback_host_and_no_origin(self):
        for host in ("127.0.0.1:43117", "localhost:43117", "[::1]:43117",
                     "LOCALHOST:43117", "127.0.0.1", None, ""):
            self.assertTrue(testq.request_allowed(host, None, 43117), host)

    def test_the_page_may_post_to_itself(self):
        for origin in ("http://localhost:43117", "http://127.0.0.1:43117"):
            self.assertTrue(
                testq.request_allowed("localhost:43117", origin, 43117), origin)

    def test_another_site_may_not(self):
        for origin in ("https://example.com", "null", "",
                       "http://localhost:3000", "http://localhost:43117.example.com"):
            self.assertFalse(
                testq.request_allowed("127.0.0.1:43117", origin, 43117), origin)

    def test_a_rebound_name_may_not(self):
        for host in ("example.com:43117", "example.com", "127.0.0.1.example.com:43117"):
            self.assertFalse(testq.request_allowed(host, None, 43117), host)
            self.assertFalse(
                testq.request_allowed(host, "http://" + host, 43117), host)


class RealProcessTable(unittest.TestCase):
    def test_this_process_is_in_it(self):
        table = testq.process_table()
        self.assertGreater(len(table), 10)
        parent, name, _, cpu = table[os.getpid()]
        self.assertIn("python", name.lower())
        self.assertIn(parent, table)
        self.assertGreater(cpu, 0)

    def test_it_can_read_its_own_startup_strings(self):
        here = testq.process_cwd(os.getpid())
        self.assertEqual(testq.norm_path(here), testq.norm_path(os.getcwd()))
        self.assertIn("unittest", testq.peb_string(os.getpid(), testq.PEB_COMMAND_LINE)
                      + " unittest")

    def test_a_process_that_is_not_there_says_nothing(self):
        self.assertEqual(testq.process_cwd(0x7FFFFFF0), "")
        self.assertEqual(testq.process_times(0x7FFFFFF0), (0.0, 0))


if __name__ == "__main__":
    unittest.main()
