"""Collect labelled cat photos on an Arduino UNO Q.

Live view and boxes come from the Detect Objects example. Saving the preview
frame comes from the video_object_detection 03_get_image sample.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arduino.app_utils import App
from arduino.app_bricks.web_ui import WebUI
from arduino.app_bricks.video_objectdetection import VideoObjectDetection
from arduino.app_peripherals.camera import Camera
from arduino.app_utils.image import draw_bounding_boxes
from fastapi.responses import Response

from collector import PhotoStore

# One default Camera(). The brick owns start/stop. The negative button reads the
# same handle, so nothing opens /dev/videoN itself.
camera = Camera()
ui = WebUI()
detection = VideoObjectDetection(camera=camera, confidence=0.5, camera_preview=True)


class CatApp:
    """Brick callbacks and the Web UI routes. Static methods keep the wiring in one place."""

    @staticmethod
    def on_detections(detections, frame):
        """One callback for the whole frame. The parameter name `frame` is what the brick looks for.

        on_detect("cat") holds a per-label lock until it returns, and a second cat box in the
        same frame is dropped. on_detect_all hands over every box at once. Saves are still
        capped at about one per second, keeping the highest-confidence cat box.
        """
        if frame is None or not isinstance(detections, dict):
            return
        PhotoStore.remember_frame(frame)
        CatApp._publish_detections(detections)
        cats = [item for item in (detections.get("cat") or []) if isinstance(item, dict)]
        if not cats or not PhotoStore.due():
            return
        cats.sort(key=CatApp._confidence, reverse=True)
        record = PhotoStore.save_cat(frame, cats[0], CatApp.debug_jpeg(frame, detections))
        if record is None:
            return
        print(f"saved {record['id']} class={record['class']} {record['files'].get('full')}")
        CatApp.publish(record)

    @staticmethod
    def debug_jpeg(frame: bytes, detections: dict) -> bytes | None:
        """Draw the brick's boxes on the preview JPEG.

        draw_bounding_boxes uses xyxy as pixels and expects confidence on a 0–100 scale
        (the still-image brick multiplies by 100; this video brick leaves it at 0–1).
        get_image_bytes PNG-encodes a PIL image, so the debug file is JPEG-encoded here.
        """
        drawn = {}
        for label, boxes in detections.items():
            if not isinstance(boxes, list):
                continue
            scaled = []
            for box in boxes:
                if not isinstance(box, dict):
                    continue
                scaled.append({
                    "confidence": CatApp._confidence(box) * 100.0,
                    "bounding_box_xyxy": box.get("bounding_box_xyxy"),
                })
            if scaled:
                drawn[label] = scaled
        try:
            image = draw_bounding_boxes(frame, drawn)
        except Exception as exc:
            print(f"debug image skipped: {exc}")
            return None
        if image is None:
            return None
        if image.mode != "RGB":
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=85)
        return out.getvalue()

    @staticmethod
    def grab_frame() -> bytes | None:
        """Current camera JPEG for a manual negative. Falls back to the last preview frame."""
        array = None
        try:
            array = camera.capture(latest=True)
        except Exception as exc:
            print(f"camera capture failed: {exc}")
        if array is not None:
            from arduino.app_utils.image import compress_to_jpeg

            encoded = compress_to_jpeg(array)
            if encoded is not None:
                # The helper's docstring says bytes. The implementation returns a NumPy array.
                data = encoded if isinstance(encoded, bytes) else encoded.tobytes()
                if data:
                    return data
        return PhotoStore.last_frame()

    @staticmethod
    def on_connect(sid):
        CatApp.push_state(sid)

    @staticmethod
    def on_hello(sid, _data):
        CatApp.push_state(sid)

    @staticmethod
    def on_set_lighting(sid, data):
        tag = data.get("lighting") if isinstance(data, dict) else data
        if isinstance(tag, str):
            PhotoStore.set_lighting(tag.strip().lower())
        ui.send_message("lighting", PhotoStore.lighting(), room=sid)

    @staticmethod
    def on_label(sid, data):
        if not isinstance(data, dict):
            ui.send_message("status", "Label needs a class.", room=sid)
            return
        if data.get("class") == "skip":
            ui.send_message("status", "Skipped. Left in uncertain/.", room=sid)
            return
        lighting = data.get("lighting")
        if isinstance(lighting, str):
            PhotoStore.set_lighting(lighting.strip().lower())
        result = PhotoStore.label_capture(str(data.get("id") or ""), data.get("class"), PhotoStore.lighting())
        if result.get("error"):
            ui.send_message("status", result["error"], room=sid)
            return
        ui.send_message("status", f"Stored as {result['class']} / {result['lighting']}.", room=sid)
        CatApp.publish(PhotoStore.latest_unlabelled() or result)

    @staticmethod
    def on_capture_negative(sid, data):
        if isinstance(data, dict) and isinstance(data.get("lighting"), str):
            PhotoStore.set_lighting(data["lighting"].strip().lower())
        frame = CatApp.grab_frame()
        if not frame:
            ui.send_message("status", "No frame yet.", room=sid)
            return
        record = PhotoStore.save_negative(frame)
        if record is None:
            ui.send_message("status", "Wait a second before the next capture.", room=sid)
            return
        print(f"saved {record['id']} class=negative {record['files'].get('full')}")
        ui.send_message("status", f"Negative stored ({record['lighting']}).", room=sid)
        CatApp.publish(record)

    @staticmethod
    def export_zip(include_uncertain: str = "0"):
        include = str(include_uncertain).lower() in {"1", "true", "yes"}
        name = "cat-photos-with-uncertain.zip" if include else "cat-photos.zip"
        return Response(
            content=PhotoStore.export_zip(include),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @staticmethod
    def photo(capture_id: str, kind: str):
        data = PhotoStore.read_image(capture_id, kind)
        if data is None:
            return Response(status_code=404)
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @staticmethod
    def summary_route():
        return PhotoStore.summary()

    @staticmethod
    def push_state(sid):
        ui.send_message("lighting", PhotoStore.lighting(), room=sid)
        ui.send_message("summary", PhotoStore.summary(), room=sid)
        latest = PhotoStore.latest_unlabelled()
        if latest:
            ui.send_message("capture", CatApp.public_record(latest), room=sid)

    @staticmethod
    def publish(record):
        if not record or record.get("error"):
            ui.send_message("summary", PhotoStore.summary())
            return
        ui.send_message("capture", CatApp.public_record(record))
        ui.send_message("summary", PhotoStore.summary())

    @staticmethod
    def public_record(record: dict) -> dict:
        files = record.get("files") or {}
        return {
            "id": record.get("id"),
            "timestamp": record.get("timestamp"),
            "label": record.get("label"),
            "confidence": record.get("confidence"),
            "bbox": record.get("bbox"),
            "lighting": record.get("lighting"),
            "class": record.get("class"),
            "has_crop": bool(files.get("crop")),
            "has_debug": bool(files.get("debug")),
            "has_full": bool(files.get("full")),
        }

    @staticmethod
    def _publish_detections(detections: dict) -> None:
        entries = []
        for label, boxes in detections.items():
            if not isinstance(boxes, list):
                continue
            for box in boxes:
                if isinstance(box, dict):
                    entries.append({"label": label, "confidence": box.get("confidence")})
        ui.send_message("detection", entries[:8])

    @staticmethod
    def _confidence(details: dict) -> float:
        try:
            return float(details.get("confidence") or 0)
        except (TypeError, ValueError):
            return 0.0


PhotoStore.ensure_dirs()
detection.on_detect_all(CatApp.on_detections)
ui.on_connect(CatApp.on_connect)
ui.on_message("hello", CatApp.on_hello)
ui.on_message("set_lighting", CatApp.on_set_lighting)
ui.on_message("label_capture", CatApp.on_label)
ui.on_message("capture_negative", CatApp.on_capture_negative)
ui.expose_api("GET", "/export.zip", CatApp.export_zip)
ui.expose_api("GET", "/photo/{capture_id}/{kind}", CatApp.photo)
ui.expose_api("GET", "/summary", CatApp.summary_route)

App.run()
