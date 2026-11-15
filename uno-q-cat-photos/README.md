# Collect cat photos

UNO Q App Lab app. A USB camera and the stock YOLOX-nano model watch the room. When the model reports a `cat`, the app saves the preview JPEG, a crop of that box, and a debug JPEG with the box drawn. Those files start in `uncertain/` and are not training data until you label them Grey, Black, or Not a cat.

This folder follows the App Lab layout used by `uno-q-blink/`: `app.yaml`, `python/main.py`, and a README. There is no sketch. The live page is adapted from the board's Detect Objects on Camera example (`inspirational/common/video-generic-object-detection`). Saving the preview frame follows `bricks/arduino/video_object_detection/03_get_image`.

`app.yaml` pins `yolox-object-detection` on `arduino:video_object_detection` and also uses `arduino:web_ui`. Python opens the camera with `Camera()` and passes that handle to `VideoObjectDetection(camera=camera, confidence=0.5, camera_preview=True)`. It does not open `/dev/videoN`.

No extra Python packages. Pillow and NumPy are already in the App Bricks runtime. Do not install system packages from the unprivileged dev account.

Starting this app replaces whatever app is running, including Blink. Do that only when you mean to stop the current app.

## Import and start

On a machine with this repo, zip the app so `app.yaml` sits one folder down (the folder name becomes the app id):

```bash
cd cat-game
zip -r /tmp/uno-q-cat-photos.zip uno-q-cat-photos \
  -x '*__pycache__*' '*.pyc' 'uno-q-cat-photos/tests/*' 'uno-q-cat-photos/photos/*'
```

Copy that zip to the board (for example `/tmp`, which the `arduino` user can read). The `bot` account cannot read `/home/arduino/ArduinoApps`, so import and start as `arduino`:

```bash
sudo -n -u arduino /usr/bin/arduino-app-cli app import /tmp/uno-q-cat-photos.zip
sudo -n -u arduino /usr/bin/arduino-app-cli app start user:uno-q-cat-photos --verbose
```

Logs:

```bash
sudo -n -u arduino /usr/bin/arduino-app-cli app logs user:uno-q-cat-photos --follow
```

The first start can spend a while preparing the model container. YOLOX-nano is the preloaded UNO Q model (`yolo-x-nano.eim`). If an app named `uno-q-cat-photos` is already installed, import adds a timestamp suffix and the id changes; `app list` shows it.

You can also copy the folder to `/home/arduino/ArduinoApps/uno-q-cat-photos` as a user who can write there, then `app start` that path. `bot` cannot do that copy.

## Web UI

`http://<board-host>:7000/`

The live picture is the model runner's embed on port 4912, same as Detect Objects on Camera. The page on port 7000 is the capture and label controls. Examples from this board: `http://mimaunoq.tailbee7a5.ts.net:7000/` and `http://MiMaUNOQ.local:7000/`.

Set lighting (day, lamp, low, other) before a capture. That value is stored with the photo, and a later Grey / Black / Not a cat click stores the selector's value again with the class. Skip leaves the files in `uncertain/` and does not set a class. Capture negative saves the current frame straight into `negative/` with class `negative`.

Auto-saves and the negative button share one throttle of about one capture per second. If several cats are in the frame, the highest-confidence box is the one saved that second.

Counts on the page are class by lighting. The `uncertain` row is the unlabelled queue. It is not training data.

`http://<board-host>:7000/summary` returns those counts as JSON.

## Export

Labelled photos only (grey, black, negative), one manifest line per capture:

`http://<board-host>:7000/export.zip`

Same set plus the unlabelled `uncertain/` captures:

`http://<board-host>:7000/export.zip?include_uncertain=1`

The zip contains `manifest.jsonl` and the image files. Download it from a browser on port 7000. That is the copy path, because `bot` cannot read `/home/arduino/ArduinoApps/<app>/photos/`.

## File layout

On the board the app directory is bind-mounted at `/app`, so photos written to `/app/photos` stay in the app folder across a restart.

```
uno-q-cat-photos/
  app.yaml
  README.md
  python/main.py          # bricks, callbacks, HTTP routes
  python/collector.py     # crop, files, manifest
  assets/                 # page on port 7000
  tests/test_store.py     # offline; not needed on the board
  photos/                 # created at runtime, not in git
    manifest.jsonl
    uncertain/            # auto-saved cats, class null
    grey/
    black/
    negative/
```

Each auto-saved cat writes three JPEGs next to each other: `*-full.jpg` (the preview frame), `*-crop.jpg` (the box, clamped to the frame), and `*-debug.jpg` (the preview with boxes drawn). A manual negative writes `*-full.jpg` only.

`manifest.jsonl` is an append-only log. One JSON line per capture, then another line when it is labelled. Fields: `timestamp` (ISO 8601 with the board's zone offset), `label` (`cat` or `negative`), `confidence`, `bbox` (`[x1, y1, x2, y2]` in preview pixels, or null), `lighting`, `class` (null until Grey / Black / Not a cat), and `files` (paths relative to `photos/`). A label is refused unless `class` is `grey`, `black`, or `negative`. The latest line for an id is the one that counts.

## Boxes and the preview

`VideoObjectDetection` builds `bounding_box_xyxy` as `(x, y, x + width, y + height)` from the runner message and does not scale it. `draw_bounding_boxes` uses those numbers as pixels of the JPEG you pass in. This app crops that same preview JPEG. Step 1 saw the live boxes line up with the 640×480 preview, so no 416→640 scale is applied. The debug JPEG is the check.

The debug label is drawn with confidence × 100 because `draw_bounding_boxes` expects a percent. The manifest stores the brick's original 0–1 confidence.

## Board test for task 2.1

1. Import and start the app, after you are ready for Blink (or whichever app is running) to stop.
2. Open `http://<board-host>:7000/`. The live view should show the room.
3. Put a cat in frame. Within about a second the page should show a crop and a debug image, and the log should print `saved ... class=None uncertain/...-full.jpg`.
4. As the `arduino` user, the same files are under `/home/arduino/ArduinoApps/uno-q-cat-photos/photos/uncertain/`: `*-full.jpg`, `*-crop.jpg`, `*-debug.jpg`. `photos/manifest.jsonl` has a new line with `"class": null`.
5. Open the debug JPEG (on the page, or the file). The rectangle should sit on the cat, in the same place as the live view. If the box is shifted or clearly scaled wrong, stop. Do not collect a full set until that lines up. `http://<board-host>:7000/photo/<id>/debug` serves that JPEG.

`frame is None` is ignored. A missing preview does not write a file.

## Offline test

On a PC, no board:

```bash
pip install pillow
python -m py_compile uno-q-cat-photos/python/main.py uno-q-cat-photos/python/collector.py
python -m unittest discover -s uno-q-cat-photos/tests -v
```

The test checks clamp and crop in preview pixels, the one-per-second throttle, that a label without a class is refused, and that the default zip leaves unlabelled crops out.
