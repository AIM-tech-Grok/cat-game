"""Photo files, crops, and the manifest for the cat collector.

The brick's bounding_box_xyxy is already in preview-JPEG pixels. This module
crops that JPEG. It does not scale from the 416x416 model input.
"""

from __future__ import annotations

import io
import json
import shutil
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image


class PhotoStore:
    """Files under /app/photos and the JSONL manifest. Static methods share one lock."""

    ROOT = Path("/app/photos")
    CLASSES = ("grey", "black", "negative")
    UNCERTAIN = "uncertain"
    LIGHTING = ("day", "lamp", "low", "other")
    INTERVAL_S = 1.0
    KINDS = ("full", "crop", "debug")

    root = ROOT
    _last_save_at = 0.0
    _lighting = "day"
    _last_frame: bytes | None = None
    _lock = threading.Lock()

    @staticmethod
    def configure(root: Path | None = None) -> None:
        """Point the store at a directory and clear in-memory throttle state. Tests use this."""
        PhotoStore.root = Path(root) if root is not None else PhotoStore.ROOT
        PhotoStore._last_save_at = 0.0
        PhotoStore._lighting = "day"
        PhotoStore._last_frame = None
        PhotoStore.ensure_dirs()

    @staticmethod
    def ensure_dirs() -> None:
        for name in (PhotoStore.UNCERTAIN, *PhotoStore.CLASSES):
            (PhotoStore.root / name).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def lighting() -> str:
        return PhotoStore._lighting

    @staticmethod
    def set_lighting(tag: str | None) -> str:
        if tag in PhotoStore.LIGHTING:
            PhotoStore._lighting = tag
        return PhotoStore._lighting

    @staticmethod
    def remember_frame(frame: bytes | None) -> None:
        if frame:
            with PhotoStore._lock:
                PhotoStore._last_frame = frame

    @staticmethod
    def last_frame() -> bytes | None:
        with PhotoStore._lock:
            return PhotoStore._last_frame

    @staticmethod
    def due() -> bool:
        return time.monotonic() - PhotoStore._last_save_at >= PhotoStore.INTERVAL_S

    @staticmethod
    def clamp_box(box, width: int, height: int) -> tuple[int, int, int, int] | None:
        """Clamp an xyxy box to the frame. No scaling: values are preview pixels.

        VideoObjectDetection stores (x, y, x + width, y + height) from the runner
        and does not resize them. draw_bounding_boxes then uses those integers as
        pixels of the preview JPEG. int() matches that helper.
        """
        try:
            x1, y1, x2, y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        except (TypeError, ValueError, IndexError):
            return None
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        x1 = max(0, min(int(x1), width))
        y1 = max(0, min(int(y1), height))
        x2 = max(0, min(int(x2), width))
        y2 = max(0, min(int(y2), height))
        if x2 <= x1 or y2 <= y1:
            return None
        return (x1, y1, x2, y2)

    @staticmethod
    def crop_jpeg(frame: bytes, box) -> bytes | None:
        """Crop the preview JPEG with xyxy, clamped to the image. None if the box is empty."""
        if not frame:
            return None
        try:
            image = Image.open(io.BytesIO(frame))
            image.load()
        except Exception:
            return None
        clamped = PhotoStore.clamp_box(box, image.width, image.height)
        if clamped is None:
            return None
        crop = image.crop(clamped)
        if crop.mode != "RGB":
            crop = crop.convert("RGB")
        out = io.BytesIO()
        crop.save(out, format="JPEG", quality=90)
        return out.getvalue()

    @staticmethod
    def save_cat(frame: bytes | None, details: dict | None, debug_jpeg: bytes | None) -> dict | None:
        """Save one cat box. Returns None when frame is missing, the box is empty, or the 1s throttle is closed."""
        if frame is None or not isinstance(details, dict):
            return None
        box = details.get("bounding_box_xyxy")
        crop = PhotoStore.crop_jpeg(frame, box)
        if crop is None:
            return None
        try:
            confidence = float(details.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        bbox = [float(box[0]), float(box[1]), float(box[2]), float(box[3])]
        with PhotoStore._lock:
            now = time.monotonic()
            if now - PhotoStore._last_save_at < PhotoStore.INTERVAL_S:
                return None
            record = PhotoStore._write_capture(
                directory=PhotoStore.UNCERTAIN,
                label="cat",
                confidence=confidence,
                bbox=bbox,
                class_name=None,
                full=frame,
                crop=crop,
                debug=debug_jpeg,
            )
            PhotoStore._last_save_at = now
            PhotoStore._last_frame = frame
            return record

    @staticmethod
    def save_negative(frame: bytes | None) -> dict | None:
        """Save a manual negative. class is set immediately, so it counts as labelled."""
        if not frame:
            return None
        with PhotoStore._lock:
            now = time.monotonic()
            if now - PhotoStore._last_save_at < PhotoStore.INTERVAL_S:
                return None
            record = PhotoStore._write_capture(
                directory="negative",
                label="negative",
                confidence=None,
                bbox=None,
                class_name="negative",
                full=frame,
                crop=None,
                debug=None,
            )
            PhotoStore._last_save_at = now
            PhotoStore._last_frame = frame
            return record

    @staticmethod
    def label_capture(capture_id: str, class_name: str | None, lighting: str | None = None) -> dict:
        """Move a capture into grey, black, or negative and append a manifest line with that class.

        A class outside those three is refused and nothing is written. Skip is not a class.
        """
        if class_name not in PhotoStore.CLASSES:
            return {"error": "class required"}
        with PhotoStore._lock:
            latest = PhotoStore._latest_map_unlocked().get(capture_id)
            if latest is None:
                return {"error": "unknown capture"}
            if lighting in PhotoStore.LIGHTING:
                chosen = lighting
            else:
                chosen = latest.get("lighting") if latest.get("lighting") in PhotoStore.LIGHTING else PhotoStore._lighting
            files = dict(latest.get("files") or {})
            pending = []
            for kind in PhotoStore.KINDS:
                rel = files.get(kind)
                if not rel:
                    pending.append((kind, None, None))
                    continue
                src = PhotoStore._safe_unlocked(rel)
                if src is None or not src.is_file():
                    return {"error": f"missing {kind}"}
                pending.append((kind, src, f"{class_name}/{src.name}"))
            moved = {}
            for kind, src, dest_rel in pending:
                if src is None or dest_rel is None:
                    moved[kind] = None
                    continue
                dest = PhotoStore.root / dest_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.resolve() != dest.resolve():
                    shutil.move(src, dest)
                moved[kind] = dest_rel
            if latest.get("class") == class_name and latest.get("lighting") == chosen and latest.get("files") == moved:
                return latest
            record = {
                "id": capture_id,
                "timestamp": latest.get("timestamp"),
                "label": latest.get("label"),
                "confidence": latest.get("confidence"),
                "bbox": latest.get("bbox"),
                "lighting": chosen,
                "class": class_name,
                "files": moved,
            }
            PhotoStore._append_unlocked(record)
            return record

    @staticmethod
    def summary() -> dict:
        counts = {name: {light: 0 for light in PhotoStore.LIGHTING} for name in (*PhotoStore.CLASSES, PhotoStore.UNCERTAIN)}
        with PhotoStore._lock:
            rows = list(PhotoStore._latest_map_unlocked().values())
        for row in rows:
            key = row.get("class") if row.get("class") in PhotoStore.CLASSES else PhotoStore.UNCERTAIN
            light = row.get("lighting") if row.get("lighting") in PhotoStore.LIGHTING else "other"
            counts[key][light] += 1
        labelled = sum(counts[name][light] for name in PhotoStore.CLASSES for light in PhotoStore.LIGHTING)
        return {
            "counts": counts,
            "labelled": labelled,
            "unlabelled": sum(counts[PhotoStore.UNCERTAIN].values()),
        }

    @staticmethod
    def latest_unlabelled() -> dict | None:
        with PhotoStore._lock:
            rows = [
                row
                for row in PhotoStore._latest_map_unlocked().values()
                if row.get("class") not in PhotoStore.CLASSES
            ]
        if not rows:
            return None
        return max(rows, key=lambda row: row.get("timestamp") or "")

    @staticmethod
    def record(capture_id: str) -> dict | None:
        with PhotoStore._lock:
            return PhotoStore._latest_map_unlocked().get(capture_id)

    @staticmethod
    def read_image(capture_id: str, kind: str) -> bytes | None:
        if kind not in PhotoStore.KINDS:
            return None
        with PhotoStore._lock:
            row = PhotoStore._latest_map_unlocked().get(capture_id)
            if row is None:
                return None
            rel = (row.get("files") or {}).get(kind)
            path = PhotoStore._safe_unlocked(rel)
        if path is None or not path.is_file():
            return None
        return path.read_bytes()

    @staticmethod
    def export_zip(include_uncertain: bool = False) -> bytes:
        """Zip the current labelled set. Unlabelled rows stay out unless include_uncertain is set.

        The zip manifest is one line per capture (the latest line), not the raw log.
        Every line in the default zip has a class.
        """
        with PhotoStore._lock:
            rows = list(PhotoStore._latest_map_unlocked().values())
            root = PhotoStore.root
        chosen = []
        for row in rows:
            labelled = row.get("class") in PhotoStore.CLASSES
            if not labelled and not include_uncertain:
                continue
            if not labelled and row.get("class") not in (None,):
                continue
            chosen.append(row)
        chosen.sort(key=lambda row: row.get("timestamp") or "")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for row in chosen:
                for rel in (row.get("files") or {}).values():
                    if not rel:
                        continue
                    path = PhotoStore._safe_path(root, rel)
                    if path is not None and path.is_file():
                        archive.write(path, rel)
            payload = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in chosen)
            archive.writestr("manifest.jsonl", payload)
        return buf.getvalue()

    @staticmethod
    def _write_capture(
        directory: str,
        label: str,
        confidence: float | None,
        bbox: list | None,
        class_name: str | None,
        full: bytes,
        crop: bytes | None,
        debug: bytes | None,
    ) -> dict:
        """Caller holds the lock. class_name is None for an unlabelled cat capture."""
        if class_name is not None and class_name not in PhotoStore.CLASSES:
            raise ValueError("class required")
        PhotoStore.ensure_dirs()
        now = datetime.now().astimezone()
        capture_id = PhotoStore._new_id(now)
        files = {
            "full": PhotoStore._dump(directory, capture_id, "full", full),
            "crop": PhotoStore._dump(directory, capture_id, "crop", crop),
            "debug": PhotoStore._dump(directory, capture_id, "debug", debug),
        }
        record = {
            "id": capture_id,
            "timestamp": now.isoformat(),
            "label": label,
            "confidence": confidence,
            "bbox": bbox,
            "lighting": PhotoStore._lighting,
            "class": class_name,
            "files": files,
        }
        PhotoStore._append_unlocked(record)
        return record

    @staticmethod
    def _dump(directory: str, capture_id: str, kind: str, payload: bytes | None) -> str | None:
        if not payload:
            return None
        rel = f"{directory}/{capture_id}-{kind}.jpg"
        path = PhotoStore.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return rel

    @staticmethod
    def _new_id(now: datetime) -> str:
        offset = now.strftime("%z") or "+0000"
        sign = "p" if offset.startswith("+") else "m"
        base = now.strftime("%Y%m%dT%H%M%S%f") + sign + offset[1:]
        capture_id = base
        suffix = 1
        while (PhotoStore.root / PhotoStore.UNCERTAIN / f"{capture_id}-full.jpg").exists() or (
            PhotoStore.root / "negative" / f"{capture_id}-full.jpg"
        ).exists():
            capture_id = f"{base}-{suffix}"
            suffix += 1
        return capture_id

    @staticmethod
    def _append_unlocked(record: dict) -> None:
        if record.get("class") is not None and record.get("class") not in PhotoStore.CLASSES:
            raise ValueError("class required")
        path = PhotoStore.root / "manifest.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    @staticmethod
    def _latest_map_unlocked() -> dict[str, dict]:
        latest: dict[str, dict] = {}
        path = PhotoStore.root / "manifest.jsonl"
        if not path.exists():
            return latest
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            capture_id = row.get("id")
            if capture_id:
                latest[capture_id] = row
        return latest

    @staticmethod
    def _safe_unlocked(rel: str | None) -> Path | None:
        return PhotoStore._safe_path(PhotoStore.root, rel)

    @staticmethod
    def _safe_path(root: Path, rel: str | None) -> Path | None:
        if not rel or not isinstance(rel, str):
            return None
        parts = Path(rel).parts
        if not parts or rel.startswith(("/", "\\")) or ".." in parts:
            return None
        path = (root / rel).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return None
        return path
