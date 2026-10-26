import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import builder_core as core


def label(**updates):
    return dict(status="labeled", name_text="Вихрь | +15", name_color="green",
                upgrade_state="VALUE", upgrade="15", quantity_state="ABSENT", quantity="",
                buyout_state="VALUE", buyout="9 999 999 999", note="", **updates)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = core.Store(self.root)
        self.source = self.root / "screenshots/pending/тест.png"
        Image.new("RGB", (1920, 1081), "#123456").save(self.source)
        self.original = self.source.read_bytes()
        self.sha = core.digest(self.original)
        self.rows = [label() for _ in range(10)]

    def commit(self):
        self.store.commit(self.source, self.sha, 0, self.rows)

    def test_commit_and_exact_crops(self):
        self.commit()
        self.assertFalse(self.source.exists())
        record = self.store.index["screenshots"][self.sha]
        self.assertEqual((self.root / record["source"]).read_bytes(), self.original)
        self.assertEqual(len(list((self.root / "crops").rglob("*.png"))), 40)
        for row in record["rows"]:
            self.assertEqual(row["buyout"], 9999999999)
            self.assertEqual(row["quantity"], 1)
            for name, item in row["crops"].items():
                with Image.open(self.root / item["path"]) as crop:
                    box = core.row_boxes(row["row"] - 1)[name]
                    self.assertEqual(crop.size, (box[2] - box[0], box[3] - box[1]))

    def test_invalid_last_row_writes_nothing(self):
        self.rows[-1]["buyout"] = "12x"
        with self.assertRaises(ValueError):
            self.commit()
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(self.store.index_path.exists())
        self.assertFalse(list((self.root / "crops").rglob("*.png")))

    def test_crop_failure_then_retry(self):
        original_writer = self.store._write_crop
        count = 0

        def fail(relative, raw):
            nonlocal count
            count += 1
            if count == 7:
                raise OSError("injected disk failure")
            original_writer(relative, raw)

        with patch.object(self.store, "_write_crop", side_effect=fail):
            with self.assertRaises(OSError):
                self.commit()
        self.assertTrue(self.source.exists())
        self.assertFalse(self.store.index_path.exists())
        self.commit()
        self.assertFalse(self.source.exists())

    def test_json_failure_preserves_existing_index(self):
        core.atomic_json(self.store.index_path, self.store.index)
        original = self.store.index_path.read_bytes()
        with patch.object(core.os, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaises(OSError):
                self.commit()
        self.assertEqual(self.store.index_path.read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.commit()

    def test_interrupted_archive_recovered_without_duplicates(self):
        with patch.object(self.store, "_archive", side_effect=OSError("injected interruption")):
            with self.assertRaises(OSError):
                self.commit()
        self.assertTrue(self.source.exists())
        restarted = core.Store(self.root)
        self.assertTrue(restarted.recover(self.source, self.sha))
        self.assertEqual(len(restarted.index["screenshots"]), 1)
        self.assertFalse(self.source.exists())

    def test_interruption_after_link_preserves_source_and_retries(self):
        unlink = Path.unlink

        def fail_source(path, *args, **kwargs):
            if path == self.source:
                raise OSError("injected unlink failure")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_source):
            with self.assertRaises(OSError):
                self.commit()
        self.assertTrue(self.source.exists())
        self.assertTrue(core.Store(self.root).recover(self.source, self.sha))

    def test_damaged_crop_blocks_recovery(self):
        with patch.object(self.store, "_archive", side_effect=OSError("stop")):
            with self.assertRaises(OSError):
                self.commit()
        crop = next((self.root / "crops").rglob("*.png"))
        crop.write_bytes(b"broken")
        with self.assertRaises(ValueError):
            core.Store(self.root).recover(self.source, self.sha)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_processed_collision_never_overwritten(self):
        destination = self.root / f"screenshots/processed/{self.sha}.png"
        destination.write_bytes(b"other image")
        with self.assertRaises(ValueError):
            self.commit()
        self.assertEqual(destination.read_bytes(), b"other image")
        self.assertTrue(self.source.exists())

    def test_source_changed_during_annotation(self):
        self.source.write_bytes(self.original + b"changed")
        with self.assertRaises(ValueError):
            self.commit()
        self.assertTrue(self.source.exists())

    def test_skipped_rows_and_menu(self):
        rows = [{"status": "skipped", "reason": "BID перекрывает BUYOUT"} for _ in range(9)]
        self.store.commit(self.source, self.sha, 1, rows)
        record = self.store.index["screenshots"][self.sha]
        self.assertEqual(record["rows"][1]["roi"]["full_lot"][1], 467)
        self.assertFalse(list((self.root / "crops").rglob("*.png")))

    def test_duplicate_content_archives_without_duplicate_labels(self):
        self.commit()
        duplicate = self.source.with_name("duplicate.png")
        duplicate.write_bytes(self.original)
        self.assertTrue(self.store.recover(duplicate, self.sha))
        self.assertEqual(len(self.store.index["screenshots"]), 1)

    def test_unknown_index_not_overwritten(self):
        self.store.index_path.write_text('{"legacy": true}', encoding="utf-8")
        with self.assertRaises(ValueError):
            core.Store(self.root)
        self.assertEqual(json.loads(self.store.index_path.read_text()), {"legacy": True})

    def test_lock_exclusive_and_released(self):
        lock = core.DatasetLock(self.root)
        try:
            with self.assertRaises(RuntimeError):
                core.DatasetLock(self.root)
        finally:
            lock.close()
        core.DatasetLock(self.root).close()


class LabelTests(unittest.TestCase):
    def test_1080_and_1081_use_identical_pixel_coordinates(self):
        # Models an extra bottom scanline, not a real capture comparison.
        image = Image.new("RGB", (1920, 1081))
        from PIL import ImageDraw
        draw = ImageDraw.Draw(image)
        for y in range(1081):
            draw.line((0, y, 1919, y), fill=(y % 256, (y * 7) % 256, (y * 13) % 256))
        with tempfile.TemporaryDirectory() as folder:
            a, b = Path(folder) / "1081.png", Path(folder) / "1080.png"
            image.save(a)
            image.crop((0, 0, 1920, 1080)).save(b)
            first, _ = core.read_image(a)
            second, _ = core.read_image(b)
            self.assertEqual(first.size, (1920, 1081))
            self.assertEqual(second.size, (1920, 1080))
            for menu in range(10):
                for row in range(9 if menu else 10):
                    for box in core.row_boxes(row, menu).values():
                        self.assertEqual(first.crop(box).tobytes(), second.crop(box).tobytes())

    def test_values(self):
        for value, expected in (("1", 1), ("9 999 999 999", 9999999999),
                                ("999999999999", 999999999999), ("1\u202f000", 1000)):
            self.assertEqual(core.positive_integer(value), expected)
        for value in ("", "0", "-1", "1.2", "1e3", "1 00", "1,000", "---"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.positive_integer(value)

    def test_ambiguity_is_not_one_or_zero(self):
        data = label()
        data.update(quantity_state="AMBIGUOUS", buyout_state="ABSENT",
                    name_text="Длинное имя..", upgrade_state="UNKNOWN")
        result = core.validate_label(data)
        self.assertIsNone(result["quantity"])
        self.assertIsNone(result["buyout"])
        self.assertIsNone(result["upgrade"])
        self.assertTrue(result["name_truncated"])

    def test_conflicting_upgrade(self):
        data = label()
        data["upgrade"] = "12"
        with self.assertRaises(ValueError):
            core.validate_label(data)

    def test_geometry_inside_each_row(self):
        for menu in range(10):
            for row in range(9 if menu else 10):
                boxes = core.row_boxes(row, menu)
                full = boxes["full_lot"]
                self.assertLessEqual(full[3], 763)
                for box in boxes.values():
                    self.assertTrue(full[0] <= box[0] < box[2] <= full[2])
                    self.assertTrue(full[1] <= box[1] < box[3] <= full[3])


class GuiTests(unittest.TestCase):
    def test_unicode_paste_shortcut_selection_and_menu(self):
        import tkinter as tk
        from tkinter import ttk
        from types import SimpleNamespace
        from builder import enable_entry_paste, entry_control_key
        root = tk.Tk()
        root.withdraw()
        try:
            entry = ttk.Entry(root)
            menu = enable_entry_paste(entry)
            clipboard = patch.object(entry, "clipboard_get", return_value="«Житница»")
            mocked_clipboard = clipboard.start()
            self.addCleanup(clipboard.stop)
            entry.insert(0, "старое имя")
            entry.selection_range(0, tk.END)
            entry.event_generate("<<Paste>>")
            self.assertEqual(entry.get(), "«Житница»")
            entry.delete(0, tk.END)
            # Same VK_V, Russian layout keysym. No OS keyboard-layout change needed.
            result = entry_control_key(SimpleNamespace(widget=entry, keycode=86, keysym="Cyrillic_em"))
            self.assertEqual(result, "break")
            self.assertEqual(entry.get(), "«Житница»")
            entry.delete(0, tk.END)
            entry_control_key(SimpleNamespace(widget=entry, keycode=86, keysym="v"))
            self.assertEqual(entry.get(), "«Житница»")
            entry.selection_range(1, 8)
            mocked_clipboard.return_value = "Янтарник"
            menu.invoke(0)
            self.assertEqual(entry.get(), "«Янтарник»")
            mocked_clipboard.side_effect = tk.TclError("No text in clipboard")
            entry.event_generate("<<Paste>>")
            self.assertEqual(entry.get(), "«Янтарник»")
            self.assertIsNone(entry_control_key(SimpleNamespace(widget=entry, keycode=65, keysym="a")))
        finally:
            root.destroy()

    def test_navigation_drafts_validation_and_next_screenshot(self):
        import tkinter as tk
        from builder import Builder
        with tempfile.TemporaryDirectory() as folder:
            store = core.Store(Path(folder))
            for number in range(2):
                Image.new("RGB", (1920, 1081), (number, 0, 0)).save(
                    store.root / f"screenshots/pending/{number}.png")
            root = tk.Tk()
            root.withdraw()
            self.addCleanup(lambda: root.destroy() if root.winfo_exists() else None)
            app = Builder(root, store)
            with patch("builder.messagebox.showerror") as errors:
                app.accept()
                errors.assert_called_once()
                errors.reset_mock()
                for key, value in label().items():
                    if key in app.variables:
                        app.variables[key].set(value)
                app.accept()
                self.assertEqual(app.index, 1)
                app.navigate(-1)
                self.assertEqual(app.variables["name_text"].get(), "Вихрь | +15")
                app.save_draft()
                saved = store.load_draft(app.sha)
                self.assertEqual(saved["rows"][0]["status"], "labeled")
                app.defer()
                self.assertEqual(app.source.name, "1.png")
                app.refresh()
                self.assertEqual(app.source.name, "0.png")
                self.assertEqual(app.variables["name_text"].get(), "Вихрь | +15")
                app.navigate(1)
                for _ in range(9):
                    app.variables["reason"].set("Пустая строка")
                    app.skip()
                app.confirmed.set(True)
                app.commit()
                self.assertEqual(app.source.name, "1.png")
                self.assertEqual(len(store.index["screenshots"]), 1)
                errors.assert_not_called()
                app.menu.set(1)
                app.change_menu()
                self.assertEqual(len(app.rows), 9)
                self.assertFalse(app.confirmed.get())
                app.save_draft()
            if app.timer is not None:
                root.after_cancel(app.timer)


if __name__ == "__main__":
    unittest.main()
