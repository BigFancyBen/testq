"""Tests for testq. Stdlib only, like the thing they test:

    python -m unittest discover tests

Nothing here starts a daemon, an engine or a process. The queue is driven
through Queue.tick() on a fake clock, and the box is a dictionary shaped like
process_table()'s answer, so a test can describe a hung Godot in one line and
watch what the daemon would do about it over twenty simulated minutes.
"""

import atexit
import io
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
        suite = self.job(script="run_test.sh", tree=SNAP, winpid=100, gpu=0, slots=1)
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

    def test_a_cancelled_ticket_is_not_parked(self):
        self.job(winpid=100, eta_s=1000)
        first = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=200)
        self.q.cancel(first["id"])
        again = self.job(script="clip.sh", arg="hats", tree=WT_A, winpid=400)
        self.assertNotIn("resumed_s", again)


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
    def ran(self, script, arg, seconds, size=None, tree="t"):
        self.q.append_history({
            "id": "J0", "tree": tree, "script": script, "arg": arg, "size": size,
            "exit": 0, "verdict": "released", "dur_s": seconds, "queued_s": 0,
            "finished": self.clock})
        self.clock += 1

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
