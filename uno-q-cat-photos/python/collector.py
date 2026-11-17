"""Photo files, crops, and the manifest for the cat collector.

The brick's bounding_box_xyxy is already in preview-JPEG pixels. This module
crops that JPEG. It does not scale from the 416x416 model input.
"""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import threading
import time
import zipfile
from collections import deque
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
    # About two saves a second while a cat stays in frame. 300 shots is a few
    # minutes of that, and 200 MB is the backstop if the JPEGs run large.
    DEFAULTS = {
        "auto_enabled": False,
        "confidence": 0.5,
        "cooldown_s": 0.5,
        "max_photos": 300,
        "max_disk_mb": 200,
        "camera_gap_s": 30,
    }
    QUEUE_SIZE = 2

    root = ROOT
    auto_enabled = False
    confidence = 0.5
    cooldown_s = 0.5
    max_photos = 300
    max_disk_mb = 200
    camera_gap_s = 30
    _last_save_at = 0.0
    _last_auto_at = -1e9
    _prev_auto_at = -1e9
    _lighting = "day"
    _last_frame: bytes | None = None
    _count = 0
    _bytes = 0
    _dropped = 0
    _last_auto: dict | None = None
    _auto: deque = deque()
    _jobs: queue.Queue = queue.Queue(maxsize=2)
    _now = time.monotonic
    _lock = threading.Lock()

    @staticmethod
    def configure(root: Path | None = None) -> None:
        """Point the store at a directory and clear in-memory throttle state. Tests use this."""
        PhotoStore.root = Path(root) if root is not None else PhotoStore.ROOT
        for key, value in PhotoStore.DEFAULTS.items():
            setattr(PhotoStore, key, value)
        PhotoStore._last_save_at = 0.0
        PhotoStore._last_auto_at = -1e9
        PhotoStore._prev_auto_at = -1e9
        PhotoStore._lighting = "day"
        PhotoStore._last_frame = None
        PhotoStore._count = 0
        PhotoStore._bytes = 0
        PhotoStore._dropped = 0
        PhotoStore._last_auto = None
        PhotoStore._auto = deque()
        PhotoStore._jobs = queue.Queue(maxsize=PhotoStore.QUEUE_SIZE)
        PhotoStore._now = time.monotonic
        CameraHealth.reset(PhotoStore.camera_gap_s)
        PhotoStore.ensure_dirs()
        PhotoStore.rebuild_index()

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
            with PhotoStore._lock:
                PhotoStore._lighting = tag
        return PhotoStore._lighting

    @staticmethod
    def lighting_from(message) -> str | None:
        """Return a lighting tag carried by a Web UI message, or None."""
        tag = message.get("lighting") if isinstance(message, dict) else message
        if isinstance(tag, str):
            tag = tag.strip().lower()
            if tag in PhotoStore.LIGHTING:
                return tag
        return None

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
    def save_negative(frame: bytes | None, lighting: str | None = None) -> dict | None:
        """Save a manual negative. class is set immediately, so it counts as labelled.

        `lighting` is the tag from the click. It is written on the manifest line even
        when the selector was still the default.
        """
        if not frame:
            return None
        with PhotoStore._lock:
            if lighting in PhotoStore.LIGHTING:
                PhotoStore._lighting = lighting
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
            PhotoStore._auto = deque(item for item in PhotoStore._auto if item["id"] != capture_id)
            record = {
                "id": capture_id,
                "timestamp": latest.get("timestamp"),
                "label": latest.get("label"),
                "confidence": latest.get("confidence"),
                "bbox": latest.get("bbox"),
                "lighting": chosen,
                "class": class_name,
                "source": latest.get("source") or "manual",
                "bytes": latest.get("bytes"),
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
        with PhotoStore._lock:
            photos = PhotoStore._count
            disk_bytes = PhotoStore._bytes
            dropped = PhotoStore._dropped
            last_auto = dict(PhotoStore._last_auto) if PhotoStore._last_auto else None
        return {
            "counts": counts,
            "labelled": labelled,
            "unlabelled": sum(counts[PhotoStore.UNCERTAIN].values()),
            "auto": {
                "enabled": PhotoStore.auto_enabled,
                "threshold": PhotoStore.confidence,
                "cooldown_s": PhotoStore.cooldown_s,
                "max_photos": PhotoStore.max_photos,
                "max_disk_mb": PhotoStore.max_disk_mb,
                "photos": photos,
                "disk_bytes": disk_bytes,
                "dropped": dropped,
                "last": last_auto,
            },
            "camera": CameraHealth.snapshot(),
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
    def load_config(path: Path | None = None) -> None:
        """Read config.yaml, then CAT_* environment variables. Missing file keeps the defaults."""
        for key, value in PhotoStore.DEFAULTS.items():
            setattr(PhotoStore, key, value)
        PhotoStore._read_config_file(path or Path("/app/config.yaml"))
        PhotoStore._read_config_env()
        CameraHealth.reset(PhotoStore.camera_gap_s)

    @staticmethod
    def pick_cat(detections, threshold: float | None = None) -> dict | None:
        """Highest-confidence cat box at or above the threshold. Every other class is ignored."""
        if not isinstance(detections, dict):
            return None
        limit = PhotoStore.confidence if threshold is None else threshold
        best = None
        best_confidence = -1.0
        for item in detections.get("cat") or []:
            if not isinstance(item, dict):
                continue
            try:
                confidence = float(item.get("confidence"))
            except (TypeError, ValueError):
                continue
            if confidence < limit or confidence <= best_confidence:
                continue
            best = item
            best_confidence = confidence
        return best

    @staticmethod
    def reserve_auto() -> bool:
        """Claim the next auto-save slot. False during the cooldown."""
        now = PhotoStore._now()
        if now - PhotoStore._last_auto_at < PhotoStore.cooldown_s:
            return False
        PhotoStore._prev_auto_at = PhotoStore._last_auto_at
        PhotoStore._last_auto_at = now
        return True

    @staticmethod
    def undo_reserve() -> None:
        PhotoStore._last_auto_at = PhotoStore._prev_auto_at

    @staticmethod
    def submit_auto(job) -> bool:
        """Queue one auto save. A full queue drops the frame instead of blocking."""
        try:
            PhotoStore._jobs.put_nowait(job)
            return True
        except queue.Full:
            PhotoStore._dropped += 1
            return False

    @staticmethod
    def save_auto(frame: bytes | None, details: dict | None, debug_jpeg: bytes | None = None) -> dict | None:
        """Save one auto cat capture, pruning oldest unlabelled auto shots if the budget is over.

        The running photo count and byte total are updated in memory. The manifest is
        appended to, including tombstones for a prune batch. It is not rewritten.
        """
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
        incoming = len(frame) + len(crop) + len(debug_jpeg or b"")
        with PhotoStore._lock:
            victims, room = PhotoStore._plan_prune(1, incoming)
            doomed = PhotoStore._commit_deletions(victims)
            if not room:
                record = None
            else:
                record = PhotoStore._write_capture(
                    directory=PhotoStore.UNCERTAIN,
                    label="cat",
                    confidence=confidence,
                    bbox=bbox,
                    class_name=None,
                    full=frame,
                    crop=crop,
                    debug=debug_jpeg,
                    source="auto",
                )
                PhotoStore._auto.append({
                    "id": record["id"],
                    "bytes": record["bytes"],
                    "files": dict(record["files"]),
                })
                PhotoStore._last_auto = {
                    "id": record["id"],
                    "timestamp": record["timestamp"],
                    "confidence": record["confidence"],
                    "has_crop": bool(record["files"].get("crop")),
                    "has_full": bool(record["files"].get("full")),
                }
        PhotoStore._unlink(doomed)
        return record

    @staticmethod
    def rebuild_index() -> None:
        """One pass over the manifest. Saves after this update the counters themselves."""
        with PhotoStore._lock:
            rows = list(PhotoStore._latest_map_unlocked().values())
            PhotoStore._count = 0
            PhotoStore._bytes = 0
            PhotoStore._auto = deque()
            rows.sort(key=lambda row: row.get("timestamp") or "")
            for row in rows:
                size = row.get("bytes")
                if not isinstance(size, int) or size < 0:
                    size = PhotoStore._file_size(row.get("files") or {})
                PhotoStore._count += 1
                PhotoStore._bytes += size
                if row.get("source") == "auto" and row.get("class") not in PhotoStore.CLASSES:
                    PhotoStore._auto.append({
                        "id": row["id"],
                        "bytes": size,
                        "files": dict(row.get("files") or {}),
                    })

    @staticmethod
    def _plan_prune(extra_count: int, extra_bytes: int) -> tuple[list, bool]:
        """Pop oldest auto shots until this save fits, then a bit further so the next saves do not prune.

        Caller holds the lock. Returns (victims, room_for_the_new_save).
        """
        if not PhotoStore._over(extra_count, extra_bytes, PhotoStore._count, PhotoStore._bytes):
            return [], True
        victims = []
        removed_count = 0
        removed_bytes = 0
        while PhotoStore._auto and PhotoStore._over(
            extra_count,
            extra_bytes,
            PhotoStore._count - removed_count,
            PhotoStore._bytes - removed_bytes,
        ):
            item = PhotoStore._auto.popleft()
            victims.append(item)
            removed_count += 1
            removed_bytes += int(item["bytes"])
        head_count = int(PhotoStore.max_photos * 0.9) if PhotoStore.max_photos > 0 else None
        head_bytes = int(PhotoStore.max_disk_mb * 0.9 * 1024 * 1024) if PhotoStore.max_disk_mb > 0 else None
        while PhotoStore._auto:
            count_now = PhotoStore._count - removed_count
            bytes_now = PhotoStore._bytes - removed_bytes
            above = (head_count is not None and count_now > head_count) or (
                head_bytes is not None and bytes_now > head_bytes
            )
            if not above:
                break
            item = PhotoStore._auto.popleft()
            victims.append(item)
            removed_count += 1
            removed_bytes += int(item["bytes"])
        room = not PhotoStore._over(
            extra_count,
            extra_bytes,
            PhotoStore._count - removed_count,
            PhotoStore._bytes - removed_bytes,
        )
        return victims, room

    @staticmethod
    def _over(extra_count: int, extra_bytes: int, count: int, nbytes: int) -> bool:
        if PhotoStore.max_photos > 0 and count + extra_count > PhotoStore.max_photos:
            return True
        if PhotoStore.max_disk_mb > 0 and nbytes + extra_bytes > PhotoStore.max_disk_mb * 1024 * 1024:
            return True
        return False

    @staticmethod
    def _commit_deletions(victims: list) -> list[str]:
        """Append tombstones and drop the victims from the running totals. Caller holds the lock."""
        if not victims:
            return []
        doomed = []
        now = datetime.now().astimezone().isoformat()
        path = PhotoStore.root / "manifest.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for item in victims:
                PhotoStore._count = max(0, PhotoStore._count - 1)
                PhotoStore._bytes = max(0, PhotoStore._bytes - int(item["bytes"]))
                handle.write(json.dumps({"id": item["id"], "deleted": True, "timestamp": now}, separators=(",", ":")) + "\n")
                for rel in (item.get("files") or {}).values():
                    if rel:
                        doomed.append(rel)
        return doomed

    @staticmethod
    def _unlink(paths: list[str]) -> None:
        for rel in paths:
            path = PhotoStore._safe_path(PhotoStore.root, rel)
            if path is None:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _file_size(files: dict) -> int:
        total = 0
        for rel in files.values():
            path = PhotoStore._safe_path(PhotoStore.root, rel)
            if path is not None and path.is_file():
                total += path.stat().st_size
        return total

    @staticmethod
    def _read_config_file(path: Path) -> None:
        if not path.is_file():
            return
        values = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        PhotoStore._apply_config_map(values)

    @staticmethod
    def _read_config_env() -> None:
        names = {
            "CAT_AUTO": "auto_enabled",
            "CAT_CONFIDENCE": "confidence",
            "CAT_COOLDOWN_S": "cooldown_s",
            "CAT_MAX_PHOTOS": "max_photos",
            "CAT_MAX_DISK_MB": "max_disk_mb",
            "CAT_CAMERA_GAP_S": "camera_gap_s",
        }
        found = {names[key]: os.environ[key] for key in names if key in os.environ and os.environ[key] != ""}
        PhotoStore._apply_config_map(found)

    @staticmethod
    def _apply_config_map(values: dict) -> None:
        flags = {"1", "true", "yes", "on"}
        if "auto_enabled" in values:
            PhotoStore.auto_enabled = str(values["auto_enabled"]).strip().lower() in flags
        for name in ("confidence", "cooldown_s", "camera_gap_s", "max_disk_mb"):
            if name in values:
                try:
                    setattr(PhotoStore, name, float(values[name]))
                except ValueError:
                    pass
        if "max_photos" in values:
            try:
                PhotoStore.max_photos = int(float(values["max_photos"]))
            except ValueError:
                pass

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
        source: str = "manual",
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
        size = len(full) + len(crop or b"") + len(debug or b"")
        record = {
            "id": capture_id,
            "timestamp": now.isoformat(),
            "label": label,
            "confidence": confidence,
            "bbox": bbox,
            "lighting": PhotoStore._lighting,
            "class": class_name,
            "source": source,
            "bytes": size,
            "files": files,
        }
        PhotoStore._append_unlocked(record)
        PhotoStore._count += 1
        PhotoStore._bytes += size
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
            if not capture_id:
                continue
            if row.get("deleted"):
                latest.pop(capture_id, None)
                continue
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


class CameraHealth:
    """Remember the last preview and the last detection, and log when either goes quiet."""

    gap_s = 30.0
    _lock = threading.Lock()
    _last_preview = 0.0
    _last_detection = 0.0
    _preview_wall = ""
    _detection_wall = ""
    _preview_down = False
    _detection_down = False
    _events: list[dict] = []

    @staticmethod
    def reset(gap_s: float = 30.0) -> None:
        with CameraHealth._lock:
            CameraHealth.gap_s = gap_s
            CameraHealth._last_preview = 0.0
            CameraHealth._last_detection = 0.0
            CameraHealth._preview_wall = ""
            CameraHealth._detection_wall = ""
            CameraHealth._preview_down = False
            CameraHealth._detection_down = False
            CameraHealth._events = []

    @staticmethod
    def start(now: float, wall: str) -> None:
        with CameraHealth._lock:
            CameraHealth._last_preview = now
            CameraHealth._last_detection = now
            CameraHealth._preview_wall = wall
            CameraHealth._detection_wall = wall
            CameraHealth._preview_down = False
            CameraHealth._detection_down = False

    @staticmethod
    def note_preview(now: float, wall: str) -> list[str]:
        return CameraHealth._note("preview", now, wall)

    @staticmethod
    def note_detection(now: float, wall: str) -> list[str]:
        return CameraHealth._note("detection", now, wall)

    @staticmethod
    def poll(now: float, wall: str, has_preview: bool) -> list[str]:
        """Call on a timer. `has_preview` is whether the brick currently has a JPEG."""
        lines: list[str] = []
        if has_preview:
            lines.extend(CameraHealth.note_preview(now, wall))
        lines.extend(CameraHealth._gap(now, wall, "preview", has_preview))
        lines.extend(CameraHealth._gap(now, wall, "detection", False))
        return lines

    @staticmethod
    def snapshot() -> dict:
        with CameraHealth._lock:
            return {
                "gap_s": CameraHealth.gap_s,
                "preview": "down" if CameraHealth._preview_down else "ok",
                "detection": "down" if CameraHealth._detection_down else "ok",
                "last_preview": CameraHealth._preview_wall or None,
                "last_detection": CameraHealth._detection_wall or None,
                "events": list(CameraHealth._events),
            }

    @staticmethod
    def _note(kind: str, now: float, wall: str) -> list[str]:
        with CameraHealth._lock:
            down = CameraHealth._preview_down if kind == "preview" else CameraHealth._detection_down
            if kind == "preview":
                CameraHealth._last_preview = now
                CameraHealth._preview_wall = wall
                CameraHealth._preview_down = False
            else:
                CameraHealth._last_detection = now
                CameraHealth._detection_wall = wall
                CameraHealth._detection_down = False
            if not down:
                return []
            message = "preview recovered" if kind == "preview" else "detections recovered"
            return [CameraHealth._record(wall, message)]

    @staticmethod
    def _gap(now: float, wall: str, kind: str, fresh: bool) -> list[str]:
        with CameraHealth._lock:
            if kind == "preview":
                if fresh or CameraHealth._preview_down:
                    return []
                last = CameraHealth._last_preview
                since = CameraHealth._preview_wall
                if now - last < CameraHealth.gap_s:
                    return []
                CameraHealth._preview_down = True
                message = f"no preview frame since {since or wall}"
            else:
                if CameraHealth._detection_down:
                    return []
                last = CameraHealth._last_detection
                since = CameraHealth._detection_wall
                if now - last < CameraHealth.gap_s:
                    return []
                CameraHealth._detection_down = True
                message = f"no detection result since {since or wall}"
            return [CameraHealth._record(wall, message)]

    @staticmethod
    def _record(wall: str, message: str) -> str:
        """Caller holds the lock."""
        line = f"{wall} camera: {message}"
        CameraHealth._events.append({"time": wall, "message": message})
        if len(CameraHealth._events) > 30:
            CameraHealth._events = CameraHealth._events[-30:]
        return line
