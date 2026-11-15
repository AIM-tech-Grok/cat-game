"""Offline checks for crop, clamp, and the manifest. No board and no App Bricks."""

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from collector import PhotoStore


def jpeg(width, height, color=(20, 40, 60)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="JPEG")
    return out.getvalue()


class PhotoStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        PhotoStore.configure(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()
        PhotoStore.root = PhotoStore.ROOT
        PhotoStore._last_save_at = 0.0
        PhotoStore._lighting = "day"
        PhotoStore._last_frame = None

    def lines(self):
        path = PhotoStore.root / "manifest.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def save_cat(self, box=(10, 20, 110, 220), confidence=0.82):
        frame = jpeg(640, 480)
        return PhotoStore.save_cat(
            frame,
            {"confidence": confidence, "bounding_box_xyxy": box},
            jpeg(32, 24, (1, 2, 3)),
        )

    def test_crop_uses_preview_pixels_without_scaling(self):
        # A 416-wide box on a 640x480 preview stays 416 pixels. It is not stretched to the frame.
        cropped = PhotoStore.crop_jpeg(jpeg(640, 480), (0, 0, 416, 416))
        with Image.open(io.BytesIO(cropped)) as image:
            self.assertEqual(image.size, (416, 416))

    def test_clamp_keeps_inside_boxes_and_drops_empty_ones(self):
        self.assertEqual(PhotoStore.clamp_box((10.9, 20.2, 30.8, 40.1), 640, 480), (10, 20, 30, 40))
        self.assertEqual(PhotoStore.clamp_box((-20, -10, 50, 40), 640, 480), (0, 0, 50, 40))
        self.assertEqual(PhotoStore.clamp_box((600, 400, 800, 600), 640, 480), (600, 400, 640, 480))
        self.assertEqual(PhotoStore.clamp_box((5, 8, 1, 3), 640, 480), (1, 3, 5, 8))
        self.assertIsNone(PhotoStore.clamp_box((10, 10, 10, 40), 640, 480))
        self.assertIsNone(PhotoStore.clamp_box(None, 640, 480))

    def test_missing_frame_writes_nothing(self):
        self.assertIsNone(PhotoStore.save_cat(None, {"confidence": 0.9, "bounding_box_xyxy": (1, 1, 4, 4)}, None))
        self.assertIsNone(PhotoStore.save_negative(None))
        self.assertFalse((PhotoStore.root / "manifest.jsonl").exists())

    def test_cat_capture_is_unlabelled_and_throttled(self):
        record = self.save_cat()
        self.assertIsNotNone(record)
        self.assertIsNone(record["class"])
        self.assertEqual(record["label"], "cat")
        self.assertEqual(record["confidence"], 0.82)
        self.assertEqual(record["bbox"], [10.0, 20.0, 110.0, 220.0])
        self.assertEqual(record["lighting"], "day")
        self.assertRegex(record["timestamp"], r"(Z|[+-]\d\d:\d\d)$")
        self.assertTrue(record["files"]["full"].startswith("uncertain/"))
        self.assertTrue((PhotoStore.root / record["files"]["crop"]).is_file())
        with Image.open(PhotoStore.root / record["files"]["crop"]) as image:
            self.assertEqual(image.size, (100, 200))
        self.assertIsNone(self.save_cat())
        self.assertEqual(len(self.lines()), 1)
        self.assertIsNone(self.lines()[0]["class"])

    def test_label_requires_a_class_and_then_moves_the_crop(self):
        record = self.save_cat()
        for bad in (None, "", "skip", "cat", "unlabelled"):
            refused = PhotoStore.label_capture(record["id"], bad)
            self.assertEqual(refused.get("error"), "class required")
        self.assertEqual(len(self.lines()), 1)
        self.assertIsNone(self.lines()[0]["class"])

        PhotoStore.set_lighting("lamp")
        labelled = PhotoStore.label_capture(record["id"], "grey", "lamp")
        self.assertEqual(labelled["class"], "grey")
        self.assertEqual(labelled["lighting"], "lamp")
        self.assertFalse((PhotoStore.root / record["files"]["crop"]).exists())
        self.assertTrue((PhotoStore.root / labelled["files"]["crop"]).is_file())
        self.assertTrue(labelled["files"]["crop"].startswith("grey/"))

        summary = PhotoStore.summary()
        self.assertEqual(summary["counts"]["grey"]["lamp"], 1)
        self.assertEqual(summary["counts"]["uncertain"]["day"], 0)
        self.assertEqual(summary["labelled"], 1)
        self.assertEqual(summary["unlabelled"], 0)
        self.assertIsNone(PhotoStore.latest_unlabelled())

    def test_export_keeps_unlabelled_crops_out_of_the_training_zip(self):
        unlabelled = self.save_cat()
        PhotoStore._last_save_at = 0
        negative = PhotoStore.save_negative(jpeg(640, 480, (9, 9, 9)))
        self.assertEqual(negative["class"], "negative")
        PhotoStore._last_save_at = 0
        other = self.save_cat(box=(30, 40, 90, 140), confidence=0.7)
        PhotoStore.label_capture(other["id"], "black", "low")

        with zipfile.ZipFile(io.BytesIO(PhotoStore.export_zip(False))) as default:
            names = set(default.namelist())
            self.assertIn("manifest.jsonl", names)
            self.assertNotIn(unlabelled["files"]["crop"], names)
            self.assertNotIn(unlabelled["files"]["full"], names)
            self.assertTrue(any(name.startswith("black/") for name in names))
            self.assertTrue(any(name.startswith("negative/") for name in names))
            exported = [json.loads(line) for line in default.read("manifest.jsonl").decode().splitlines()]
        self.assertTrue(exported)
        self.assertTrue(all(row["class"] in PhotoStore.CLASSES for row in exported))
        self.assertNotIn(unlabelled["id"], {row["id"] for row in exported})

        with zipfile.ZipFile(io.BytesIO(PhotoStore.export_zip(True))) as with_uncertain:
            uncertain_names = set(with_uncertain.namelist())
            uncertain_rows = [json.loads(line) for line in with_uncertain.read("manifest.jsonl").decode().splitlines()]
        self.assertIn(unlabelled["files"]["crop"], uncertain_names)
        self.assertIn(unlabelled["id"], {row["id"] for row in uncertain_rows})

    def test_paths_cannot_escape_the_photo_root(self):
        self.assertIsNone(PhotoStore._safe_path(PhotoStore.root, "../secret.jpg"))
        self.assertIsNone(PhotoStore._safe_path(PhotoStore.root, "/etc/passwd"))
        self.assertIsNone(PhotoStore._safe_path(PhotoStore.root, "grey/../../secret.jpg"))


if __name__ == "__main__":
    unittest.main()
