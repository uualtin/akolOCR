from __future__ import annotations

import html
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Iterator
from urllib.parse import quote

import cv2
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from .anpr.normalize import normalize_plate
from .anpr.recognizer import PlateRecognizer
from .authorization.database import DuplicatePlateError, PlateDatabase
from .camera.rtsp import RtspCamera
from .config import CameraSettings, Settings
from .pipeline import AnprPipeline
from .trigger.base import GateTrigger
from .trigger.console import ConsoleGateTrigger
from .trigger.http import HttpGateTrigger


class CameraRuntime:
    def __init__(
        self,
        config: CameraSettings,
        recognizer: Any,
        recognizer_lock: threading.Lock,
        pipeline: AnprPipeline,
        process_fps: float,
    ) -> None:
        self.config = config
        self.recognizer = recognizer
        self.recognizer_lock = recognizer_lock
        self.pipeline = pipeline
        self.process_fps = max(process_fps, 0.1)
        self.camera = RtspCamera(config.rtsp_url, name=config.camera_id)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run,
            name=f"camera-{self.config.camera_id}",
            daemon=True,
        )
        self.thread.start()

    def _run(self) -> None:
        self.camera.open()
        interval = 1.0 / self.process_fps
        next_process_at = 0.0
        while not self.stop_event.is_set():
            frame = self.camera.read()
            if frame is None:
                continue
            now = time.monotonic()
            if now < next_process_at:
                continue
            next_process_at = now + interval
            try:
                with self.recognizer_lock:
                    detections = self.recognizer.recognize(frame)
                self.pipeline.process(detections)
            except Exception as exc:
                print(
                    f"[ANPR_ERROR] camera={self.config.camera_id} error={exc}",
                    flush=True,
                )

    def stream(self) -> Iterator[bytes]:
        last_sequence = -1
        while not self.stop_event.is_set():
            snapshot = self.camera.snapshot()
            if snapshot is None or snapshot[0] == last_sequence:
                time.sleep(0.1)
                continue
            last_sequence, frame = snapshot
            encoded, jpeg = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80]
            )
            if encoded:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + jpeg.tobytes()
                    + b"\r\n"
                )

    def status(self) -> dict[str, bool | int | float | None]:
        last_frame_at = self.camera.last_frame_at
        return {
            "configured": True,
            "online": bool(last_frame_at and time.time() - last_frame_at < 10),
            "last_frame_at": last_frame_at,
            "reconnect_count": self.camera.reconnect_count,
        }

    def stop(self) -> None:
        self.stop_event.set()
        self.camera.release()
        if self.thread is not None:
            self.thread.join(timeout=3)


def build_trigger(settings: Settings, camera: CameraSettings) -> GateTrigger:
    if settings.gate_trigger_type == "http":
        return HttpGateTrigger(camera.gate_open_url, gate_id=camera.camera_id)
    return ConsoleGateTrigger(camera.camera_id)


class ApplicationContext:
    def __init__(
        self,
        settings: Settings,
        *,
        recognizer: Any | None = None,
        start_cameras: bool = True,
    ) -> None:
        self.settings = settings
        self.database = PlateDatabase(settings.database_path)
        self.recognizer = recognizer
        self.start_cameras = start_cameras
        self.recognizer_lock = threading.Lock()
        self.runtimes: dict[str, CameraRuntime] = {}

    def start(self) -> None:
        self.database.initialize()
        configured = [camera for camera in self.settings.cameras if camera.rtsp_url]
        if not self.start_cameras or not configured:
            return
        if self.recognizer is None:
            self.recognizer = PlateRecognizer()
        for camera in configured:
            pipeline = AnprPipeline(
                database=self.database,
                trigger=build_trigger(self.settings, camera),
                min_confidence=self.settings.min_confidence,
                cooldown_seconds=self.settings.trigger_cooldown_seconds,
                min_plate_length=self.settings.min_plate_length,
                detection_confirm_frames=self.settings.detection_confirm_frames,
                detection_confirm_seconds=self.settings.detection_confirm_seconds,
            )
            runtime = CameraRuntime(
                camera,
                self.recognizer,
                self.recognizer_lock,
                pipeline,
                self.settings.anpr_process_fps,
            )
            self.runtimes[camera.camera_id] = runtime
            runtime.start()

    def stop(self) -> None:
        for runtime in self.runtimes.values():
            runtime.stop()
        self.runtimes.clear()


def dashboard_html(plates: list[dict[str, int | str]], message: str = "") -> str:
    plate_rows = "".join(
        f"""<tr><td>{html.escape(str(item['plate']))}</td><td>
        <form method="post" action="/plates/{quote(str(item['plate']), safe='')}/delete">
          <button class="danger" type="submit">Sil</button>
        </form></td></tr>"""
        for item in plates
    )
    if not plate_rows:
        plate_rows = '<tr><td colspan="2" class="empty">Henüz plaka eklenmedi.</td></tr>'
    notice = f'<p class="notice">{html.escape(message)}</p>' if message else ""
    return f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ANPR Gate</title>
  <style>
    :root {{ color-scheme: dark; --bg:#08110b; --panel:#101d14; --line:#294b34;
      --text:#effff3; --muted:#9eb4a4; --green:#35df78; --red:#ff6b6b; }}
    * {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg); color:var(--text);
      font:15px system-ui,sans-serif }} main {{ width:min(1400px,94vw); margin:28px auto 60px }}
    h1,h2 {{ margin:0 0 14px }} h1 {{ font-size:1.45rem }} h2 {{ font-size:1rem }}
    .cameras,.bottom {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:18px }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px }}
    img {{ display:block; width:100%; aspect-ratio:16/9; object-fit:contain; background:#000;
      border-radius:8px }} .label {{ color:var(--muted); margin:0 0 10px }}
    table {{ width:100%; border-collapse:collapse }} th,td {{ padding:10px; text-align:left;
      border-bottom:1px solid var(--line) }} th {{ color:var(--muted); font-weight:600 }}
    form.add {{ display:flex; gap:8px; margin-bottom:12px }} input {{ flex:1; min-width:0;
      padding:10px; background:#07100a; color:var(--text); border:1px solid var(--line); border-radius:7px }}
    button {{ border:0; border-radius:7px; padding:9px 13px; cursor:pointer; background:var(--green);
      color:#041108; font-weight:700 }} button.danger {{ background:transparent; color:var(--red);
      border:1px solid #713838 }} .notice {{ color:var(--green) }} .empty {{ color:var(--muted) }}
    @media(max-width:850px) {{ .cameras,.bottom {{ grid-template-columns:1fr }} }}
  </style>
</head>
<body><main>
  <h1>ANPR Gate</h1>{notice}
  <section class="cameras">
    <article class="panel"><p class="label">Giriş kamerası</p>
      <img src="/video-feed/entry.mjpeg" alt="Giriş kamerası"></article>
    <article class="panel"><p class="label">Çıkış kamerası</p>
      <img src="/video-feed/exit.mjpeg" alt="Çıkış kamerası"></article>
  </section>
  <section class="bottom">
    <article class="panel"><h2>Access list</h2>
      <form class="add" method="post" action="/plates">
        <input name="plate" placeholder="34 ABC 123" required autocomplete="off">
        <button type="submit">Ekle</button>
      </form>
      <table><thead><tr><th>Plaka</th><th></th></tr></thead><tbody>{plate_rows}</tbody></table>
    </article>
    <article class="panel"><h2>Audit log</h2>
      <table><thead><tr><th>Plaka</th><th>Sysdate</th></tr></thead>
        <tbody id="audit"><tr><td colspan="2" class="empty">Yükleniyor…</td></tr></tbody></table>
    </article>
  </section>
</main>
<script>
async function refreshAudit() {{
  try {{
    const response = await fetch('/api/audit?limit=50', {{cache:'no-store'}});
    const items = await response.json();
    const body = document.getElementById('audit');
    body.replaceChildren();
    if (!items.length) {{
      const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 2;
      cell.className = 'empty'; cell.textContent = 'Henüz kayıt yok.'; return;
    }}
    for (const item of items) {{
      const row = body.insertRow(); row.insertCell().textContent = item.plate;
      row.insertCell().textContent = item.sysdate;
    }}
  }} catch (_) {{}}
}}
refreshAudit(); setInterval(refreshAudit, 2000);
</script></body></html>"""


def create_app(
    settings: Settings | None = None,
    *,
    recognizer: Any | None = None,
    start_cameras: bool = True,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    context = ApplicationContext(
        resolved_settings, recognizer=recognizer, start_cameras=start_cameras
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        context.start()
        yield
        context.stop()

    application = FastAPI(title="ANPR Gate MVP", lifespan=lifespan)
    application.state.context = context

    @application.get("/", response_class=HTMLResponse)
    def dashboard(message: str = "") -> str:
        return dashboard_html(context.database.list_plates(), message)

    @application.get("/video-feed/{camera_id}.mjpeg")
    def video_feed(camera_id: str) -> StreamingResponse:
        if camera_id not in {"entry", "exit"}:
            raise HTTPException(status_code=404, detail="Camera not found")
        runtime = context.runtimes.get(camera_id)
        if runtime is None:
            raise HTTPException(status_code=503, detail="Camera is not configured")
        return StreamingResponse(
            runtime.stream(), media_type="multipart/x-mixed-replace; boundary=frame"
        )

    @application.get("/api/audit")
    def audit(limit: Annotated[int, Query(ge=1, le=200)] = 50):
        return context.database.recent_audit(limit)

    @application.post("/plates")
    def add_plate(plate: Annotated[str, Form()]):
        normalized = normalize_plate(plate)
        if not normalized:
            return RedirectResponse(
                "/?message=" + quote("Geçerli bir plaka girin."), status_code=303
            )
        try:
            context.database.add_plate(normalized)
            message = f"{normalized} access liste eklendi."
        except DuplicatePlateError:
            message = f"{normalized} zaten access listte."
        return RedirectResponse("/?message=" + quote(message), status_code=303)

    @application.post("/plates/{plate}/delete")
    def delete_plate(plate: str):
        normalized = normalize_plate(plate)
        deleted = bool(normalized and context.database.delete_plate(normalized))
        message = (
            f"{normalized} access listten silindi."
            if deleted
            else "Plaka bulunamadı."
        )
        return RedirectResponse("/?message=" + quote(message), status_code=303)

    @application.get("/health")
    def health():
        camera_status = {}
        for camera in resolved_settings.cameras:
            runtime = context.runtimes.get(camera.camera_id)
            camera_status[camera.camera_id] = (
                runtime.status()
                if runtime is not None
                else {"configured": bool(camera.rtsp_url), "online": False}
            )
        return {"status": "ok", "cameras": camera_status}

    return application


app = create_app()


if __name__ == "__main__":
    current_settings = app.state.context.settings
    uvicorn.run(app, host=current_settings.api_host, port=current_settings.api_port)
