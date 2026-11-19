"""Reject incompatible scopes or incomplete subject checkpoints."""
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from filelock import FileLock

from cuhksz_sis_scraper import Course, parse_requirements, Profile, ScheduleError, export_catalog
from cuhksz_sis_scraper.checkpoint import CatalogProgress


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / "catalog.json"
        self.progress = CatalogProgress(self.output)
        self.scope = {"term": "2620", "subjects": ["BIO", "ECE"], "plan": {"sha256": "version-1"}}

    def test_scope_and_empty_subject(self):
        self.progress.restore(self.scope)
        self.progress.commit("BIO", [])
        resumed = CatalogProgress(self.output)
        resumed.restore(self.scope)
        self.assertEqual(resumed.batches, {"BIO": []})
        for change in [{"term": "2610"}, {"subjects": ["BIO"]}, {"plan": {"sha256": "version-2"}}]:
            with self.subTest(change=change), self.assertRaisesRegex(ScheduleError, "--restart"):
                resumed.restore({**self.scope, **change})
        self.assertFalse(self.output.exists())

    def test_previous_format_requires_restart(self):
        self.progress.restore(self.scope)
        self.progress.commit("BIO", [])
        data = json.loads(self.progress.path.read_text())
        data["version"] = 3
        self.progress.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ScheduleError, "--restart"):
            self.progress.restore(self.scope)

    def test_damaged_checkpoint_cannot_publish(self):
        self.progress.restore(self.scope)
        self.progress.commit("BIO", [])
        data = json.loads(self.progress.path.read_text())
        data["batches"]["ECE"] = []
        self.progress.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ScheduleError, "损坏"):
            self.progress.restore(self.scope)
        self.assertFalse(self.output.exists())

    def test_concurrent_export_cannot_restart_active_job(self):
        self.progress.restore(self.scope)
        self.progress.commit("BIO", [])
        with FileLock(str(self.progress.path) + ".lock"):
            with self.assertRaisesRegex(ScheduleError, "另一个进程"):
                export_catalog(self.output, term="2620", profile=Profile(), restart=True)
        self.progress.restore(self.scope)
        self.assertEqual(self.progress.batches, {"BIO": []})

    def test_interrupted_export_and_reauthentication(self):
        from unittest.mock import MagicMock, patch
        from contextlib import ExitStack
        from cuhksz_sis_scraper import catalog, LoginRequired

        page = MagicMock()
        frame = MagicMock()
        frame.locator(catalog._NEW).is_visible.return_value = False
        term = frame.locator.return_value
        term.locator.return_value.evaluate_all.return_value = ["2620"]
        term.locator.return_value.inner_text.return_value = "2026-27 第二学期"
        term.evaluate_all.return_value = ["BIO", "ECE"]
        kwargs = dict(term="2620", profile=Profile(username="test", password="test"))
        def batch(frame, label, subject, plan):
            return [dict(course=asdict(Course(subject + " 1001", "示例", parse_requirements(""))),
                         sections=[])]

        self.output.write_text('{"previous":true}')
        with ExitStack() as stack:
            for method in ["sync_playwright", "_update", "_select", "_login_screen"]:
                mock = stack.enter_context(patch.object(catalog, method))
                if method == "_login_screen":
                    mock.return_value = False
            stack.enter_context(patch.object(catalog, "_open_component", return_value=page))
            stack.enter_context(patch.object(catalog, "_frame", return_value=frame))
            page.main_frame = frame
            stack.enter_context(patch.object(catalog, "_read_course_entries"))
            search = stack.enter_context(patch.object(catalog, "_search"))
            search.side_effect = [batch(None, None, "BIO", None), KeyboardInterrupt()]
            with self.assertRaises(KeyboardInterrupt):
                export_catalog(self.output, **kwargs)
            self.assertEqual(json.loads(self.output.read_text()), {"previous": True})
            self.assertEqual(list(json.loads(self.progress.path.read_text())["batches"]), ["BIO"])
            search.reset_mock()
            search.side_effect = [LoginRequired("expired"), batch(None, None, "ECE", None)]
            resumed = export_catalog(self.output, **kwargs)
            self.assertEqual([c.args[2] for c in search.call_args_list], ["ECE", "ECE"])
            self.assertFalse(self.progress.path.exists())
            search.side_effect = batch
            fresh = export_catalog(self.output.with_name("fresh.json"), **kwargs)
            self.assertEqual(fresh, resumed)
            self.assertEqual(fresh, json.loads(self.output.read_text()))
