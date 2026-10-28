"""Regression coverage for the full-chat live OCR integration."""
from pathlib import Path
from types import SimpleNamespace
import queue
import tempfile
import unittest

import numpy as np

import spacegoat_fisch_server as live


OLD = "[System] OldPlayer has caught a Common 1.0kg Old Trout!"
FIRST = "[System] PlayerA has caught a Common 5.1kg Big Lumpy Trout!"
SECOND = "[System] PlayerB has caught a Common 3.2kg Big Sovereign Bass!"


class LiveTesseractTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = live.Config(self.folder.name)
        self.store = live.Store(Path(self.folder.name)/"live.sqlite3")
        self.store.transition("start")
        self.runtime = live.Runtime(self.config,self.store)
        self.runtime.auto_monitor = False
        self.runtime.frames = queue.Queue(64)
        self.runtime.candidates = queue.Queue(64)
        self.runtime.run_id = "tesseract-test"

    def tearDown(self):
        self.runtime.close()
        self.folder.cleanup()

    def run_views(self, views, scores=None, epochs=None, pixel_ids=None, during_read=None):
        outputs = iter(enumerate(views))
        self.read_count = 0
        def read_rows(image,settings):
            index,rows = next(outputs)
            self.read_count += 1
            if during_read:
                during_read(index)
            return [(text, scores[index] if scores else .99) for text in rows]
        self.runtime.engine = SimpleNamespace(read_rows=read_rows)
        for index in range(len(views)):
            pixels = index if pixel_ids is None else pixel_ids[index]
            image = np.full((40,160,3),pixels,dtype=np.uint8)
            epoch = epochs[index] if epochs else 0
            self.runtime.frames.put((image,self.store.active()["id"],"running",live.now(),0,epoch))
        self.runtime.monitor_stop.set()
        self.runtime.track_loop()
        self.runtime.ocr_loop()

    def test_existing_config_upgrades_to_tesseract_without_losing_settings(self):
        self.assertEqual(self.config.snapshot()["ocr_backend"],"tesseract")
        values=self.config.snapshot()
        for key in ("ocr_backend","capture_interval","tesseract_path","tesseract_mode","tesseract_scale","tesseract_psm"):
            values.pop(key)
        values["region"]=[10,20,400,300]
        import json
        self.config.path.write_text(json.dumps(values),encoding="utf-8")
        upgraded=live.Config(self.folder.name).snapshot()
        self.assertEqual(upgraded["ocr_backend"],"tesseract")
        self.assertEqual(upgraded["capture_interval"],2.0)
        self.assertEqual(upgraded["region"],[10,20,400,300])

    def test_baseline_excluded_and_new_scrolling_messages_score_once(self):
        self.run_views([[OLD],[OLD,FIRST],[FIRST,SECOND],[FIRST,SECOND]])
        self.assertEqual(self.store.state()["counts"],{"confirmed":2})
        self.assertEqual({e["username"] for e in self.store.events()["items"]},{"PlayerA","PlayerB"})
        self.assertIn(SECOND,self.runtime.health["preview"])

    def test_distinct_identical_announcements_are_preserved(self):
        self.run_views([[OLD],[OLD,FIRST],[OLD,FIRST,FIRST],[FIRST,FIRST,FIRST]])
        self.assertEqual(self.store.state()["leaderboard"][0]["catches"],3)
        self.assertEqual(self.store.state()["leaderboard"][0]["total_kg"],15.3)

    def test_nonoverlapping_view_cannot_rescore_history(self):
        self.run_views([[OLD],[FIRST],[OLD],[OLD,FIRST]])
        # The disconnected FIRST is review-only; OLD is a known historical view.
        self.assertEqual(self.store.state()["counts"],{"review":1,"confirmed":1})
        review=next(e for e in self.store.events()["items"] if e["status"]=="review")
        self.assertEqual(review["reason"],"Uncertain visual insertion")
        self.assertTrue((Path(self.folder.name)/"evidence"/review["evidence"]).exists())

    def test_wrapped_message_joins_new_rows_across_captures(self):
        header="Existing chat"
        prefix="[System] PlayerA has perfectly"
        middle="caught a Common 5.1kg Big Sovereign"
        tail="Largemouth Bass!"
        self.run_views([[header],[header,prefix],[header,prefix,middle],[prefix,middle,tail]])
        rows=self.store.events()["items"]
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]["status"],rows[0]["fish"],rows[0]["weight_units"]),
                         ("confirmed","Big Sovereign Largemouth Bass",5100000))

    def test_low_confidence_is_not_hidden_by_good_neighboring_rows(self):
        self.run_views([[OLD],[OLD,FIRST],[FIRST,SECOND]],scores=[.99,.6,.99])
        self.assertEqual(self.store.state()["counts"],{"review":1,"confirmed":1})
        self.assertEqual(self.store.state()["leaderboard"][0]["username"],"PlayerB")

    def test_blank_read_does_not_establish_baseline_and_identical_pixels_skip_ocr(self):
        # First two frames have identical pixels, but blank OCR must be retried.
        self.run_views([[],[OLD],[OLD,FIRST]],pixel_ids=[1,1,1])
        self.assertEqual(self.read_count,2)
        self.assertEqual(self.store.events()["items"],[])

    def test_tournament_transition_discards_inflight_result_and_rebaselines(self):
        def transition(index):
            if index==1:
                self.runtime.tournament_action("pause")
                self.runtime.tournament_action("resume")
        self.run_views([[OLD],[OLD,FIRST],[OLD,FIRST],[FIRST,SECOND]],
                       epochs=[0,0,2,2],during_read=transition)
        self.assertEqual(self.store.state()["counts"],{"confirmed":1})
        self.assertEqual(self.store.events()["items"][0]["username"],"PlayerB")

    def test_reader_settings_validation(self):
        for settings in ({"capture_interval":0},{"capture_interval":float("nan")},
                         {"tesseract_psm":1},{"tesseract_scale":4},{"tesseract_mode":"unknown"},
                         {"ocr_backend":"unknown"}):
            with self.subTest(settings=settings),self.assertRaises(ValueError):
                live.validate_config(settings,self.config.snapshot())
        live.validate_config({"capture_interval":.5,"tesseract_mode":"Color","tesseract_psm":11},self.config.snapshot())

    def test_threshold_migration_preserves_custom_settings_and_legacy_choice(self):
        import json
        for threshold,backend,expected in ((.92,None,.85),(.97,None,.97),(.92,"rapidocr",.92)):
            with self.subTest(threshold=threshold,backend=backend):
                values=self.config.snapshot()
                values["confidence"]=threshold
                if backend is None:
                    values.pop("ocr_backend")
                else:
                    values["ocr_backend"]=backend
                self.config.path.write_text(json.dumps(values),encoding="utf-8")
                self.assertEqual(live.Config(self.folder.name).snapshot()["confidence"],expected)
        self.assertTrue((Path(self.folder.name)/"config.pre-tesseract.json").is_file())

    def test_tesseract_adapter_preserves_per_row_confidence(self):
        from unittest.mock import patch
        from tesseract_chat_reader import OCRRead
        reading=OCRRead((FIRST,SECOND),95,20,(99,30))
        with patch.object(live,"read_chat",return_value=reading):
            rows=live.TesseractLiveEngine().read_rows(np.zeros((30,60,3),dtype=np.uint8),self.config.snapshot())
        self.assertEqual(rows,[(FIRST,.99),(SECOND,.3)])

    def test_stop_during_ocr_discards_inflight_catch(self):
        def stop(index):
            if index==1:
                self.runtime.cancel_work.set()
        self.run_views([[OLD],[OLD,FIRST]],during_read=stop)
        self.assertEqual(self.store.events()["items"],[])

    def test_missing_tesseract_explains_how_to_install_it(self):
        from unittest.mock import patch
        with patch.object(live,"tesseract_available",return_value=False):
            with self.assertRaises(RuntimeError) as failure:
                self.runtime.initialize_ocr()
        message=str(failure.exception)
        self.assertIn("https://github.com/UB-Mannheim/tesseract/wiki",message)
        self.assertIn("64-bit",message)
        self.assertIn("English",message)
        self.assertIn("C:\\Program Files\\Tesseract-OCR",message)
        self.assertIn("Restart",message)
        self.assertIn("Reader settings",message)


if __name__ == "__main__":
    unittest.main()
