import sqlite3
import unittest

from workbench.spindump_trace import compare_traces, import_trace


SAMPLE = """Analysis of sampling Fixture (pid 42) every 15 milliseconds
Analysis Tool:   /usr/bin/sample

Call graph:
    4 Thread_301: Main Thread
    + 4 launch  (in Fixture) + 44 [0x100]
    +   3 calculate  (in Fixture) + 16,20 [0x144,0x148]  /fixture path/fixture.c:23
    +   ! 1 emit  (in Fixture) + 8 [0x160]  fixture.c:40
    +   1 ???  (in Fixture)  load address 0x100 + 0xc0 [0x1c0]
    2 Thread_301   DispatchQueue_1: com.apple.main-thread  (serial)
      2 calculate  (in Fixture) + 12 [0x140]  /fixture path/fixture.c:23
    6 Thread_302
      6 __select  (in libsystem_kernel.dylib) + 8 [0x240]

Total number in stack (recursive counted multiple, when >=5):
Binary images description not available
"""

SPINDUMP = """Date/Time:        2026-01-01 05:00:00.000 +0000
Report Version:   72
Data Source:      Stackshots
Steps:            5 (10ms sampling interval)
Timeline format: stacks are sorted chronologically
Heaviest stack for the main thread of the target process:
  5  duplicated summary (Fixture + 42) [0x200]

Process:          Fixture [42] [unique pid 1234]
  Thread 0x301   Thread name "worker"   5 samples (1-5)    priority 31 (base 31)
  <thread QoS user initiated, process unclamped>
  5  dispatch + 5 (Fixture + 33) [0x133] 1-5
    3  go + 5 (fixture.cpp:17,9 in Fixture + 42) [0x200] (running) 1-3
    2  wrapped_call + 3 (Fixture + 52) [0x301] 4-5
     *2  finish + 3 (kernel + 52) [0x401] (blocked by latch owned by worker) 4-5

Process:          Helper [87]
  Thread 0x301   5 samples (1-5)
  5  <truncated backtrace> 1-5
    5  maybe_wait(pid_t) + 1 (Helper + 66) [0x502] 1-5

Binary Images:
"""


class MacTraceImportTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

    def test_sample_collapses_duplicate_thread_ids_without_inferred_waits(self):
        report = import_trace(self.db, SAMPLE)
        self.assertEqual(report["format"], "macos-sample")
        self.assertEqual((report["threads"], report["thread_trees"], report["thread_samples"]), (2, 3, 12))
        self.assertEqual(report["sampling_interval_ms"], 15)
        self.assertEqual(report["coverage"]["unparsed_frame_rows"], 0)
        top = report["top_frames"][0]
        self.assertEqual((top["symbol"], top["samples"], top["observed_state"]), ("__select", 6, None))
        row = self.db.execute("""
            SELECT f.path, f.line, f.leaf_samples, p.symbol
            FROM spin_frames f JOIN spin_frames p ON f.parent_id = p.id WHERE f.symbol='emit'
        """).fetchone()
        self.assertEqual(row, ("fixture.c", 40, 1, "calculate"))
        self.assertEqual(report["source_mapped_frames"], 3)
        self.assertEqual(self.db.execute("SELECT SUM(leaf_samples) FROM spin_frames").fetchone()[0], 12)

    def test_spindump_only_uses_explicit_annotations_and_actual_thread_trees(self):
        report = import_trace(self.db, SPINDUMP)
        self.assertEqual(report["format"], "macos-spindump")
        self.assertEqual((report["processes"], report["threads"], report["thread_samples"]), (2, 2, 10))
        self.assertEqual(report["sampling_interval_ms"], 10)
        self.assertEqual(report["coverage"]["truncated_backtraces"], 1)
        self.assertEqual(report["coverage"]["unparsed_frame_rows"], 0)
        by_symbol = {entry["symbol"]: entry for entry in report["top_frames"]}
        self.assertNotIn("duplicated summary", by_symbol)
        self.assertEqual(by_symbol["maybe_wait(pid_t)"]["observed_state"], None)
        self.assertEqual(by_symbol["finish"]["observed_state"], "blocked by latch owned by worker")
        self.assertEqual(by_symbol["go"]["observed_state"], "running")
        self.assertEqual((by_symbol["go"]["path"], by_symbol["go"]["line"]), ("fixture.cpp", 17))
        self.assertEqual(self.db.execute("""
            SELECT kernel, sample_range, image FROM spin_frames WHERE symbol='finish'
        """).fetchone(), (1, "4-5", "kernel"))

    def test_unknown_rows_prevent_exclusive_weight_claim_for_affected_tree(self):
        sample = SAMPLE.replace("    +   ! 1 emit", "    +   unrecognized record\n    +   ! 1 emit")
        report = import_trace(self.db, sample)
        self.assertEqual(report["coverage"]["unparsed_frame_rows"], 1)
        self.assertEqual(report["coverage"]["uncertain_weight_threads"], 1)
        self.assertIsNone(self.db.execute(
            "SELECT leaf_samples FROM spin_frames WHERE symbol='emit'"
        ).fetchone()[0])
        self.assertEqual(self.db.execute(
            "SELECT samples FROM spin_frames WHERE symbol='emit'"
        ).fetchone()[0], 1)

    def test_prefix_only_sample_and_spindump_without_supported_threads_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "closing section"):
            import_trace(self.db, SAMPLE.split("Total number")[0])
        with self.assertRaisesRegex(ValueError, "no supported thread"):
            import_trace(self.db, SPINDUMP.split("Process:")[0])

    def test_pair_compares_indexed_rows_using_symbol_identity_across_thread_ids(self):
        baseline = import_trace(self.db, SAMPLE)
        after = sqlite3.connect(":memory:")
        self.addCleanup(after.close)
        comparison_text = SAMPLE.replace("pid 42", "pid 87").replace("Thread_301", "Thread_400")
        comparison = import_trace(after, comparison_text.replace("! 1 emit", "! 2 emit"))
        baseline["top_frames"], comparison["top_frames"] = [], []
        result = {entry["symbol"]: entry for entry in compare_traces(self.db, after, baseline, comparison)}
        self.assertEqual(result["emit"]["delta_samples"], 1)
        self.assertGreater(result["emit"]["delta_share_points"], 8)
        self.assertEqual(result["calculate"]["delta_samples"], -1)
        self.assertNotIn("__select", result)
        with self.assertRaisesRegex(ValueError, "sampling interval"):
            compare_traces(self.db, after, baseline, {**comparison, "sampling_interval_ms": 9})
