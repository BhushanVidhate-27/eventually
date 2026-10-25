from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eventually


class ParseDecisionTests(unittest.TestCase):
    def test_valid_decision(self):
        decision = eventually.parse_decision(
            '{"folder": "Receipts", "confidence": 0.9, "why": "invoice"}'
        )
        self.assertEqual(decision.folder, "Receipts")
        self.assertEqual(decision.why, "invoice")
        self.assertAlmostEqual(decision.confidence, 0.9)

    def test_fenced_json(self):
        decision = eventually.parse_decision(
            '```json\n{"folder": "Code", "confidence": 0.8, "why": "script"}\n```'
        )
        self.assertEqual(decision.folder, "Code")

    def test_unknown_folder_becomes_to_sort(self):
        decision = eventually.parse_decision(
            '{"folder": "Invoices", "confidence": 0.9, "why": "invoice"}'
        )
        self.assertEqual(decision.folder, "To Sort")

    def test_confidence_is_clamped(self):
        decision = eventually.parse_decision(
            '{"folder": "Images", "confidence": 9, "why": "photo"}'
        )
        self.assertEqual(decision.confidence, 1.0)
        decision = eventually.parse_decision(
            '{"folder": "Images", "confidence": -2, "why": "photo"}'
        )
        self.assertEqual(decision.confidence, 0.0)

    def test_filename_reason_is_cleaned(self):
        decision = eventually.parse_decision(
            '{"folder": "Images", "confidence": 0.5, "why": "other-file.png"}'
        )
        self.assertEqual(decision.why, "filed by name")

    def test_bad_json_raises(self):
        with self.assertRaises(eventually.ModelError):
            eventually.parse_decision("not json")


class GuardrailTests(unittest.TestCase):
    def test_python_file_is_code(self):
        decision = eventually.apply_guardrail(
            "main.py", eventually.Decision("Spreadsheets", "wrong")
        )
        self.assertEqual(decision.folder, "Code")
        self.assertEqual(decision.source, "guardrail")

    def test_receipt_photo_is_a_receipt(self):
        decision = eventually.apply_guardrail(
            "receipt-cafe-nero.jpg", eventually.Decision("Images", "photo")
        )
        self.assertEqual(decision.folder, "Receipts")

    def test_text_note_is_a_document(self):
        decision = eventually.apply_guardrail(
            "meeting-transcript.txt", eventually.Decision("To Sort", "unsure")
        )
        self.assertEqual(decision.folder, "Documents")

    def test_pdf_can_stay_a_document(self):
        decision = eventually.apply_guardrail(
            "lease-agreement-signed.pdf", eventually.Decision("Documents", "lease")
        )
        self.assertEqual(decision.folder, "Documents")

    def test_unknown_binary_stays_to_sort(self):
        decision = eventually.apply_guardrail(
            "unknown-file-7f3a.bin", eventually.Decision("Installers", "binary")
        )
        self.assertEqual(decision.folder, "To Sort")

    def test_screenshot_is_allowed(self):
        decision = eventually.apply_guardrail(
            "Screenshot 2026-10-24.png", eventually.Decision("Screenshots", "screen")
        )
        self.assertEqual(decision.folder, "Screenshots")


class FileDiscoveryTests(unittest.TestCase):
    def test_only_loose_real_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "keep.pdf").write_text("x")
            (root / "keep.txt").write_text("x")
            (root / ".hidden").write_text("x")
            (root / "Thumbs.db").write_text("x")
            (root / "movie.mp4.crdownload").write_text("x")
            (root / "folder").mkdir()
            (root / "link.pdf").symlink_to(root / "keep.pdf")

            names = [item.name for item in eventually.list_target_files(root)]
            self.assertEqual(names, ["keep.pdf", "keep.txt"])


class NamingTests(unittest.TestCase):
    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "report.pdf"
            first.write_text("x")
            second = eventually.unique_path(first)
            self.assertEqual(second.name, "report (2).pdf")
            second.write_text("x")
            third = eventually.unique_path(first)
            self.assertEqual(third.name, "report (3).pdf")

    def test_compound_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "backup.tar.gz"
            first.write_text("x")
            second = eventually.unique_path(first)
            self.assertEqual(second.name, "backup (2).tar.gz")


class ApplyUndoTests(unittest.TestCase):
    def test_apply_and_undo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "Downloads"
            target.mkdir()
            (target / "invoice.pdf").write_text("invoice")
            (target / "photo.jpg").write_text("photo")
            state = root / "state"

            groups = {
                "Receipts": [
                    eventually.PlannedFile(
                        "invoice.pdf", 7, "Receipts", "invoice", "model"
                    )
                ],
                "Images": [
                    eventually.PlannedFile("photo.jpg", 5, "Images", "photo", "model")
                ],
            }

            with contextlib.redirect_stdout(io.StringIO()):
                code = eventually.apply_groups(target, groups, state)
            self.assertEqual(code, 0)
            self.assertTrue((target / "Receipts" / "invoice.pdf").exists())
            self.assertTrue((target / "Images" / "photo.jpg").exists())
            self.assertTrue((state / "last-run.json").exists())

            with contextlib.redirect_stdout(io.StringIO()):
                code = eventually.undo_last_run(state)
            self.assertEqual(code, 0)
            self.assertTrue((target / "invoice.pdf").exists())
            self.assertTrue((target / "photo.jpg").exists())
            self.assertFalse((target / "Receipts").exists())
            self.assertFalse((target / "Images").exists())
            self.assertFalse((state / "last-run.json").exists())

    def test_collision_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "Downloads"
            target.mkdir()
            (target / "a.pdf").write_text("new")
            (target / "Documents").mkdir()
            (target / "Documents" / "a.pdf").write_text("old")
            state = root / "state"
            groups = {
                "Documents": [
                    eventually.PlannedFile("a.pdf", 3, "Documents", "doc", "model")
                ]
            }

            with contextlib.redirect_stdout(io.StringIO()):
                code = eventually.apply_groups(target, groups, state)
            self.assertEqual(code, 0)
            self.assertEqual((target / "Documents" / "a.pdf").read_text(), "old")
            self.assertEqual(
                (target / "Documents" / "a (2).pdf").read_text(), "new"
            )

            with contextlib.redirect_stdout(io.StringIO()):
                code = eventually.undo_last_run(state)
            self.assertEqual(code, 0)
            self.assertTrue((target / "a.pdf").exists())
            self.assertEqual((target / "Documents" / "a.pdf").read_text(), "old")
            self.assertFalse((target / "Documents" / "a (2).pdf").exists())

    def test_model_error_fails_closed(self):
        def broken_ask(name, model, endpoint):
            raise eventually.ModelError("nope")

        files = [eventually.FileInfo("thing.pdf", 10)]
        with contextlib.redirect_stdout(io.StringIO()):
            planned = eventually.classify_files(
                files,
                model="test",
                endpoint="http://example.invalid",
                workers=1,
                ask=broken_ask,
            )
        self.assertEqual(planned[0].folder, "To Sort")
        self.assertEqual(planned[0].source, "error")

    def test_fake_model_goes_through_guardrail(self):
        def fake_ask(name, model, endpoint):
            return eventually.Decision("Spreadsheets", "wrong")

        files = [
            eventually.FileInfo("main.py", 10),
            eventually.FileInfo("meeting-notes.txt", 10),
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            planned = eventually.classify_files(
                files,
                model="test",
                endpoint="http://example.invalid",
                workers=2,
                ask=fake_ask,
            )
        by_name = {item.name: item for item in planned}
        self.assertEqual(by_name["main.py"].folder, "Code")
        self.assertEqual(by_name["meeting-notes.txt"].folder, "Documents")


class DemoAndManifestTests(unittest.TestCase):
    def test_demo_has_twenty_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = eventually.create_demo(Path(temporary) / "demo")
            names = [path.name for path in root.iterdir()]
            self.assertEqual(len(names), 20)
            self.assertIn("main.py", names)
            self.assertIn("unknown-file-7f3a.bin", names)

    def test_manifest_is_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "last-run.json"
            eventually.write_manifest(path, {"moves": [], "version": 1})
            data = json.loads(path.read_text())
            self.assertEqual(data["version"], 1)


if __name__ == "__main__":
    unittest.main()
