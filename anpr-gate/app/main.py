from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager

import cv2
import uvicorn
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .anpr.normalize import normalize_plate
from .anpr.recognizer import Detection, PlateRecognizer
from .authorization.database import DuplicatePlateError, PlateDatabase
from .authorization.service import AuthorizationService
from .camera.base import Camera
from .camera.rtsp import RtspCamera
from .camera.webcam import WebcamCamera
from .config import Settings
from .pipeline import AnprPipeline
from .trigger.base import GateTrigger
from .trigger.console import ConsoleGateTrigger
from .trigger.http import HttpGateTrigger

settings = Settings.from_env()
database = PlateDatabase(settings.database_path)
database.initialize()
stop_event = threading.Event()
camera_thread: threading.Thread | None = None
server: uvicorn.Server | None = None
preview_condition = threading.Condition()
latest_preview: bytes | None = None
preview_sequence = 0
overlay_lock = threading.Lock()
latest_detections: list[Detection] = []
latest_detection_size: tuple[int, int] | None = None


class PlateCreate(BaseModel):
    plate: str = Field(min_length=1)


class PlateResponse(BaseModel):
    id: int
    plate: str


def build_camera() -> Camera:
    if settings.camera_type == "rtsp":
        return RtspCamera(settings.rtsp_url, name="recognition")
    return WebcamCamera(settings.webcam_index)


def build_trigger() -> GateTrigger:
    if settings.trigger_type == "http":
        return HttpGateTrigger(settings.gate_trigger_url)
    return ConsoleGateTrigger()


def draw_detections(frame, detections: list[Detection]) -> None:
    for detection in detections:
        x1, y1, x2, y2 = detection.box
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 3)
        label = f"{detection.text}  {detection.confidence:.2f}"
        (text_width, text_height), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2
        )
        label_top = max(0, y1 - text_height - baseline - 10)
        cv2.rectangle(
            frame,
            (x1, label_top),
            (x1 + text_width + 12, y1),
            (0, 255, 0),
            -1,
        )
        cv2.putText(
            frame,
            label,
            (x1 + 6, y1 - baseline - 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )


def publish_preview(frame) -> None:
    global latest_preview, preview_sequence
    encoded, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not encoded:
        return
    with preview_condition:
        latest_preview = jpeg.tobytes()
        preview_sequence += 1
        preview_condition.notify_all()


def update_preview_overlay(frame, detections: list[Detection]) -> None:
    global latest_detections, latest_detection_size
    height, width = frame.shape[:2]
    with overlay_lock:
        latest_detections = detections
        latest_detection_size = (width, height)


def scaled_preview_detections(frame) -> list[Detection]:
    height, width = frame.shape[:2]
    with overlay_lock:
        detections = list(latest_detections)
        source_size = latest_detection_size
    if source_size is None:
        return []

    source_width, source_height = source_size
    scale_x = width / source_width
    scale_y = height / source_height
    return [
        Detection(
            text=detection.text,
            confidence=detection.confidence,
            box=(
                round(detection.box[0] * scale_x),
                round(detection.box[1] * scale_y),
                round(detection.box[2] * scale_x),
                round(detection.box[3] * scale_y),
            ),
        )
        for detection in detections
    ]


def run_rtsp_preview_loop(camera: RtspCamera, done: threading.Event) -> None:
    camera.open()
    try:
        while not done.is_set() and not stop_event.is_set():
            frame = camera.read()
            if frame is None:
                continue
            draw_detections(frame, scaled_preview_detections(frame))
            publish_preview(frame)
    finally:
        camera.release()


def preview_stream():
    last_sequence = -1
    while not stop_event.is_set():
        with preview_condition:
            preview_condition.wait_for(
                lambda: preview_sequence != last_sequence or stop_event.is_set(),
                timeout=1,
            )
            frame = latest_preview
            last_sequence = preview_sequence
        if frame is None:
            time.sleep(0.05)
            continue
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"


def run_camera_loop() -> None:
    camera: Camera | None = None
    preview_camera: RtspCamera | None = None
    preview_thread: threading.Thread | None = None
    preview_done = threading.Event()
    display_video = settings.display_video
    window_opened = False
    process_interval = 1.0 / max(settings.anpr_process_fps, 0.1)
    next_process_at = 0.0
    try:
        recognizer = PlateRecognizer()
        authorization = AuthorizationService(database)
        pipeline = AnprPipeline(
            min_confidence=settings.min_confidence,
            authorization_enabled=settings.authorization_enabled,
            authorization=authorization,
            trigger=build_trigger(),
            cooldown_seconds=settings.trigger_cooldown_seconds,
            min_plate_length=settings.min_plate_length,
            detection_confirm_frames=settings.detection_confirm_frames,
            detection_confirm_seconds=settings.detection_confirm_seconds,
        )
        camera = build_camera()
        if not camera.open() and settings.camera_type == "webcam":
            return

        if settings.camera_type == "rtsp":
            preview_camera = RtspCamera(settings.rtsp_preview_url, name="preview")
            preview_thread = threading.Thread(
                target=run_rtsp_preview_loop,
                args=(preview_camera, preview_done),
                name="rtsp-preview",
                daemon=True,
            )
            preview_thread.start()

        while not stop_event.is_set():
            frame = camera.read()
            if frame is None:
                continue

            now = time.monotonic()
            if now < next_process_at:
                continue
            next_process_at = now + process_interval

            detections = pipeline.process(recognizer.recognize(frame))
            update_preview_overlay(frame, detections)
            if settings.camera_type != "rtsp":
                draw_detections(frame, detections)
                publish_preview(frame)
            if display_video:
                draw_detections(frame, detections)
                try:
                    cv2.imshow("ANPR Gate", frame)
                    window_opened = True
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        stop_event.set()
                        if server is not None:
                            server.should_exit = True
                        break
                except Exception as exc:
                    display_video = False
                    print(
                        "[DISPLAY] video preview disabled; camera processing "
                        f"continues error={exc}",
                        flush=True,
                    )
    except Exception as exc:
        print(f"[APP] camera worker stopped error={exc}", flush=True)
    finally:
        preview_done.set()
        if preview_camera is not None:
            preview_camera.release()
        if preview_thread is not None:
            preview_thread.join(timeout=3)
        if camera is not None:
            camera.release()
        if window_opened:
            try:
                cv2.destroyAllWindows()
            except Exception as exc:
                print(f"[DISPLAY] window cleanup failed error={exc}", flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global camera_thread
    stop_event.clear()
    camera_thread = threading.Thread(target=run_camera_loop, name="anpr-camera", daemon=True)
    camera_thread.start()
    yield
    stop_event.set()
    with preview_condition:
        preview_condition.notify_all()
    if camera_thread is not None:
        camera_thread.join(timeout=5)


app = FastAPI(title="ANPR Gate MVP", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def preview_page() -> str:
    return """<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ANPR Gate - Canlı Kamera</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center;
      background: #07110b; color: #eaffef; font-family: system-ui, sans-serif; }
    main { width: min(1100px, 94vw); }
    header { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
    .live { width: 10px; height: 10px; border-radius: 50%; background: #21e66f;
      box-shadow: 0 0 14px #21e66f; }
    h1 { margin: 0; font-size: 1.15rem; }
    img { display: block; width: 100%; border: 1px solid #1d6136; border-radius: 12px;
      background: #000; box-shadow: 0 20px 60px #0008; }
    p { color: #8fac98; font-size: .9rem; }
  </style>
</head>
<body><main>
  <header><span class="live"></span><h1>ANPR Gate · Canlı Kamera</h1></header>
  <img src="/video-feed" alt="Canlı ANPR kamera görüntüsü">
  <p>Algılanan plakalar yeşil çerçeve ve güven skoruyla gösterilir.</p>
</main></body></html>"""


@app.get("/video-feed")
def video_feed() -> StreamingResponse:
    return StreamingResponse(
        preview_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/service-worker.js", include_in_schema=False)
def service_worker() -> Response:
    return Response(
        content="self.addEventListener('install', () => self.skipWaiting());",
        media_type="application/javascript",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/plates", response_model=list[PlateResponse])
def list_plates() -> list[dict[str, int | str]]:
    return database.list_plates()


@app.post("/plates", response_model=PlateResponse, status_code=status.HTTP_201_CREATED)
def add_plate(payload: PlateCreate) -> dict[str, int | str]:
    plate = normalize_plate(payload.plate)
    if not plate:
        raise HTTPException(status_code=422, detail="Plate must contain A-Z or 0-9")
    try:
        return database.add_plate(plate)
    except DuplicatePlateError as exc:
        raise HTTPException(status_code=409, detail="Plate already exists") from exc


@app.delete("/plates/{plate}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plate(plate: str) -> Response:
    normalized = normalize_plate(plate)
    if not normalized or not database.delete_plate(normalized):
        raise HTTPException(status_code=404, detail="Plate not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":
    config = uvicorn.Config(app, host=settings.api_host, port=settings.api_port)
    server = uvicorn.Server(config)
    server.run()
