"""Focused tests for standalone OCR parsing and ordered chat history."""
import unittest
from unittest.mock import patch

from tesseract_chat_reader import RollingChatHistory, RollingHashes, parse_tsv, read_chat


class HistoryTests(unittest.TestCase):
    def test_first_capture_and_unchanged_capture(self):
        history = RollingChatHistory()
        self.assertEqual(history.update(["A", "B"]).lines, ("A", "B"))
        self.assertEqual(history.update(["A", "B"]).lines, ())

    def test_scroll_and_multiple_new_lines(self):
        history = RollingChatHistory()
        history.update(["A", "B", "C", "D"])
        result = history.update(["C", "D", "E", "F"])
        self.assertEqual(result.lines, ("E", "F"))
        self.assertEqual(result.overlap, 2)

    def test_bottom_up_keeps_equal_new_announcements(self):
        history = RollingChatHistory()
        history.update(["Header", "Same catch"])
        self.assertEqual(history.update(["Header", "Same catch", "Same catch"]).lines, ("Same catch",))
        self.assertEqual(history.update(["Same catch", "Same catch", "Same catch"]).lines, ("Same catch",))

    def test_repeated_old_view_is_suppressed_without_losing_current_anchor(self):
        history = RollingChatHistory()
        history.update(["A", "B"])
        history.update(["B", "C"])
        self.assertEqual(history.update(["A", "B"]).lines, ())
        self.assertEqual(history.update(["B", "D"]).lines, ("D",))

    def test_immediate_overlap_wins_over_historical_view(self):
        history = RollingChatHistory()
        history.update(["Same", "Other"])
        history.update(["Other", "Same"])
        self.assertEqual(history.update(["Same", "Other"]).lines, ("Other",))

    def test_blank_read_retains_anchor(self):
        history = RollingChatHistory()
        history.update(["A", "B"])
        self.assertEqual(history.update([]).lines, ())
        self.assertEqual(history.update(["B", "C"]).lines, ("C",))

    def test_disjoint_view_is_emitted_with_explicit_status(self):
        history = RollingChatHistory()
        history.update(["A", "B"])
        result = history.update(["C", "D"])
        self.assertEqual(result.lines, ("C", "D"))
        self.assertIn("No overlap", result.status)

    def test_hash_history_is_bounded_and_resettable(self):
        history = RollingChatHistory(capacity=2)
        for line in ("A", "B", "C"):
            history.update([line])
        self.assertEqual(len(history.views), 2)
        self.assertEqual(history.update(["A"]).lines, ("A",))
        history.clear()
        self.assertEqual(len(history.views), 0)
        self.assertEqual(history.update(["A"]).lines, ("A",))

    def test_whitespace_is_normalized_but_digits_are_not(self):
        history = RollingChatHistory()
        history.update(["  Player  caught 5.1kg!  "])
        self.assertEqual(history.update(["Player caught 5.1kg!"]).lines, ())
        self.assertEqual(history.update(["Player caught 51.1kg!"]).lines, ("Player caught 51.1kg!",))

    def test_hash_collision_cannot_remove_different_text(self):
        history = RollingChatHistory()
        with patch.object(RollingHashes, "span", return_value=0):
            history.update(["A", "B"])
            self.assertEqual(history.update(["X", "Y"]).lines, ("X", "Y"))

    def test_wrapped_rows_survive_overlap(self):
        history = RollingChatHistory()
        history.update(["[System] PlayerA has caught", "a Common 5.1kg Big Lumpy Trout!"])
        result = history.update(["a Common 5.1kg Big Lumpy Trout!", "[System] PlayerB has caught", "an Unusual 5.3kg Pike!"])
        self.assertEqual(result.lines, ("[System] PlayerB has caught", "an Unusual 5.3kg Pike!"))


class OCRTests(unittest.TestCase):
    TSV = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t2\t1\t0\t30\t30\t20\t90\tTrout!\n"
           "5\t1\t1\t1\t1\t2\t80\t0\t30\t20\t80\tPlayerA\n"
           "5\t1\t1\t1\t1\t1\t0\t0\t70\t20\t100\t[System]\n"
           "4\t1\t1\t1\t1\t0\t0\t0\t100\t20\t-1\t\n")

    def test_tsv_geometry_preserves_reading_order_and_confidence(self):
        reading = parse_tsv(self.TSV)
        self.assertEqual(reading.lines, ("[System] PlayerA", "Trout!"))
        self.assertEqual(reading.confidence, 90)
        self.assertEqual(reading.word_count, 3)

    def test_empty_tsv(self):
        self.assertEqual(parse_tsv("").lines, ())

    def test_cli_uses_png_stdin_and_tsv_without_a_shell(self):
        import numpy as np
        import subprocess
        result = subprocess.CompletedProcess([], 0, self.TSV.encode(), b"")
        with patch("tesseract_chat_reader.subprocess.run", return_value=result) as run:
            reading = read_chat(np.zeros((30,100,3),dtype=np.uint8), "tesseract")
        self.assertEqual(reading.lines[0], "[System] PlayerA")
        args, kwargs = run.call_args
        self.assertEqual(args[0][1:3], ["stdin", "stdout"])
        self.assertEqual(args[0][-1], "tsv")
        self.assertTrue(kwargs["input"].startswith(b"\x89PNG"))
        self.assertNotIn("shell", kwargs)

    def test_missing_tesseract_and_timeouts_are_actionable(self):
        import numpy as np
        import subprocess
        image = np.zeros((30,100,3),dtype=np.uint8)
        with patch("tesseract_chat_reader.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                read_chat(image,"missing")
        with patch("tesseract_chat_reader.subprocess.run", side_effect=subprocess.TimeoutExpired("tesseract",15)):
            with self.assertRaisesRegex(RuntimeError,"timed out"):
                read_chat(image,"tesseract")


if __name__ == "__main__":
    unittest.main()
