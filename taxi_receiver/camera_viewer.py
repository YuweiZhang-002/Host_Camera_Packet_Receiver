from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import time
import tkinter as tk
from tkinter import filedialog, ttk

from .archive_monitor import ArchiveViewerBackend, ViewerSnapshot
from .image_loader import LoadedArchiveFrame


DEFAULT_ARCHIVE_ROOT = Path(__file__).resolve().parents[1] / "images" / "temp" / "archive"


@dataclass
class _PanelRenderState:
    frame_key: tuple[int, str, int] | None = None
    fit_to_window: bool = False
    photo: tk.PhotoImage | None = None


class _ImagePanel:
    def __init__(self, parent: ttk.Frame, title: str, empty_message: str) -> None:
        self.frame = ttk.Frame(parent, padding=(12, 12))
        self._title = ttk.Label(self.frame, text=title, style="ViewerTitle.TLabel")
        self._title.pack(anchor="w")
        self._canvas = tk.Canvas(self.frame, width=640, height=480, highlightthickness=0)
        self._canvas.pack(fill="both", expand=True, pady=(8, 8))
        self._info = tk.StringVar(value=empty_message)
        ttk.Label(self.frame, textvariable=self._info, style="ViewerInfo.TLabel").pack(anchor="w")
        self._empty_message = empty_message
        self._state = _PanelRenderState()
        self._placeholder = None
        self._render_placeholder(empty_message)

    @property
    def canvas(self) -> tk.Canvas:
        return self._canvas

    def clear(self, message: str | None = None) -> None:
        self._state = _PanelRenderState()
        self._info.set(message or self._empty_message)
        self._render_placeholder(message or self._empty_message)

    def render(self, frame: LoadedArchiveFrame, *, fit_to_window: bool) -> None:
        frame_key = (frame.generation, frame.stream, frame.frame_id)
        if self._state.frame_key == frame_key and self._state.fit_to_window == fit_to_window:
            return

        try:
            photo = tk.PhotoImage(file=str(frame.pgm_path))
        except tk.TclError as exc:
            self._info.set(f"{self._empty_message} ({exc})")
            return

        if fit_to_window:
            photo = self._fit_photo(photo)

        self._state = _PanelRenderState(frame_key=frame_key, fit_to_window=fit_to_window, photo=photo)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=photo)
        self._canvas.configure(scrollregion=(0, 0, photo.width(), photo.height()))
        self._info.set(self._format_info(frame))

    def _fit_photo(self, photo: tk.PhotoImage) -> tk.PhotoImage:
        canvas_width = max(1, self._canvas.winfo_width())
        canvas_height = max(1, self._canvas.winfo_height())
        if canvas_width <= 10 or canvas_height <= 10:
            return photo
        width_scale = max(1, (photo.width() + canvas_width - 1) // canvas_width)
        height_scale = max(1, (photo.height() + canvas_height - 1) // canvas_height)
        scale = max(width_scale, height_scale)
        if scale <= 1:
            return photo
        return photo.subsample(scale, scale)

    def _render_placeholder(self, message: str) -> None:
        self._canvas.delete("all")
        self._canvas.create_rectangle(0, 0, 640, 480, fill="#10161f", outline="")
        self._canvas.create_text(
            320,
            240,
            text=message,
            fill="#c8d0dc",
            font=("Segoe UI", 16),
        )
        self._canvas.configure(scrollregion=(0, 0, 640, 480))

    @staticmethod
    def _format_info(frame: LoadedArchiveFrame) -> str:
        timestamp = datetime.fromtimestamp(frame.timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        parts = [
            f"frame_id={frame.frame_id}",
            f"file={frame.file_name}",
            f"timestamp={timestamp}",
            f"size={frame.width}x{frame.height}",
            f"status={frame.status}",
        ]
        if frame.status == "RECOVERED":
            parts.extend(
                [
                    f"missing_count={frame.missing_count}",
                    f"missing_rows={','.join(str(value) for value in frame.missing_rows) or '-'}",
                    f"fill_policy={frame.fill_policy or '-'}",
                    f"expected_rows={frame.expected_rows if frame.expected_rows is not None else '-'}",
                ]
            )
        return " | ".join(parts)


class CameraViewerApp:
    def __init__(
        self,
        *,
        archive_root: str | Path = DEFAULT_ARCHIVE_ROOT,
        attempt: str = "attempt1",
        camera: str | None = None,
        poll_interval_ms: int = 50,
        refresh_interval_ms: int = 50,
    ) -> None:
        self.root = tk.Tk()
        self.root.title("taxi_receiver Camera Viewer")
        self.root.minsize(1100, 760)
        self.root.configure(bg="#0f131a")
        self.backend = ArchiveViewerBackend(archive_root, poll_interval_ms=poll_interval_ms)
        self._refresh_interval_ms = refresh_interval_ms
        self._pause_state = False
        self._latest_snapshot: ViewerSnapshot | None = None
        self._rendered_generation = -1
        self._ui_poll_count = 0
        self._ui_fps = 0.0
        self._fps_window_start = time.monotonic()

        self.archive_root_var = tk.StringVar(value=str(Path(archive_root).resolve()))
        self.attempt_var = tk.StringVar(value=attempt)
        self.camera_var = tk.StringVar(value=camera or "")
        self.status_var = tk.StringVar(value="Initializing")
        self.pause_text_var = tk.StringVar(value="Pause")
        self.fit_to_window_var = tk.BooleanVar(value=True)
        self.camera_state_var = tk.StringVar(value="No camera archive found")

        self._build_styles()
        self._build_layout()

        self.backend.apply_configuration(self.archive_root_var.get(), self.attempt_var.get(), camera)
        self.backend.start()

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(self._refresh_interval_ms, self._poll_backend)

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        try:
            self.backend.stop()
        finally:
            self.root.destroy()

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("ViewerTitle.TLabel", font=("Segoe UI", 12, "bold"), foreground="#e8edf5", background="#0f131a")
        style.configure("ViewerInfo.TLabel", font=("Segoe UI", 9), foreground="#c7d0dc", background="#0f131a")
        style.configure("ViewerFrame.TFrame", background="#0f131a")
        style.configure("Control.TLabel", foreground="#e8edf5", background="#0f131a", font=("Segoe UI", 9))
        style.configure("Status.TLabel", foreground="#b7c1cf", background="#111826", font=("Segoe UI", 9))

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="ViewerFrame.TFrame", padding=12)
        outer.pack(fill="both", expand=True)

        controls = ttk.Frame(outer, style="ViewerFrame.TFrame")
        controls.pack(fill="x")
        self._build_controls(controls)

        body = ttk.PanedWindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(12, 8))

        self.complete_panel = _ImagePanel(body, "COMPLETE / Main", "No COMPLETE image")
        self.recovered_panel = _ImagePanel(body, "RECOVERED", "No RECOVERED image")
        body.add(self.complete_panel.frame, weight=1)
        body.add(self.recovered_panel.frame, weight=1)

        status_frame = ttk.Frame(outer, style="ViewerFrame.TFrame")
        status_frame.pack(fill="x")
        ttk.Label(status_frame, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        self.status_detail_var = tk.StringVar(value="")
        ttk.Label(status_frame, textvariable=self.status_detail_var, style="Status.TLabel").pack(side="right")

    def _build_controls(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Archive Root", style="Control.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.archive_root_var, width=64).grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(parent, text="Browse", command=self._browse_root).grid(row=0, column=2, sticky="ew")

        ttk.Label(parent, text="Attempt", style="Control.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(parent, textvariable=self.attempt_var, width=18).grid(row=1, column=1, sticky="w", padx=(8, 8), pady=(8, 0))
        ttk.Button(parent, text="Load / Apply", command=self._apply_configuration).grid(row=1, column=2, sticky="ew", pady=(8, 0))

        ttk.Label(parent, text="Camera", style="Control.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.camera_combo = ttk.Combobox(parent, textvariable=self.camera_var, state="disabled", width=18, values=())
        self.camera_combo.grid(row=2, column=1, sticky="w", padx=(8, 8), pady=(8, 0))
        self.camera_combo.bind("<<ComboboxSelected>>", lambda _event: self._apply_configuration())

        button_row = ttk.Frame(parent, style="ViewerFrame.TFrame")
        button_row.grid(row=2, column=2, sticky="ew", pady=(8, 0))
        ttk.Button(button_row, textvariable=self.pause_text_var, command=self._toggle_pause).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Refresh", command=self.backend.refresh_now).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Open Folder", command=self._open_current_folder).pack(side="left")

        ttk.Checkbutton(parent, text="Fit to Window", variable=self.fit_to_window_var).grid(row=3, column=1, sticky="w", padx=(8, 8), pady=(8, 0))
        parent.columnconfigure(1, weight=1)

    def _browse_root(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.archive_root_var.get() or str(DEFAULT_ARCHIVE_ROOT))
        if chosen:
            self.archive_root_var.set(chosen)

    def _apply_configuration(self) -> None:
        self.backend.apply_configuration(
            self.archive_root_var.get(),
            self.attempt_var.get(),
            self.camera_var.get() or None,
        )
        self.complete_panel.clear("No COMPLETE image")
        self.recovered_panel.clear("No RECOVERED image")
        self._rendered_generation = self.backend.generation

    def _toggle_pause(self) -> None:
        self._pause_state = not self._pause_state
        if self._pause_state:
            self.backend.pause()
            self.pause_text_var.set("Resume")
            self.status_var.set("Paused")
        else:
            self.backend.resume()
            self.pause_text_var.set("Pause")

    def _open_current_folder(self) -> None:
        snapshot = self._latest_snapshot or self.backend.snapshot()
        target = self._current_folder_for_snapshot(snapshot)
        if target is None:
            return
        if hasattr(os, "startfile"):
            os.startfile(str(target))

    def _poll_backend(self) -> None:
        self._ui_poll_count += 1
        now = time.monotonic()
        elapsed = now - self._fps_window_start
        if elapsed >= 1.0:
            self._ui_fps = self._ui_poll_count / elapsed
            self._fps_window_start = now
            self._ui_poll_count = 0

        snapshot = self.backend.snapshot()
        self._latest_snapshot = snapshot
        self._sync_controls(snapshot)
        if snapshot.generation != self._rendered_generation:
            self.complete_panel.clear("No COMPLETE image")
            self.recovered_panel.clear("No RECOVERED image")
            self._rendered_generation = snapshot.generation
        self._render_snapshot(snapshot)
        self._update_status_bar(snapshot)
        self.root.after(self._refresh_interval_ms, self._poll_backend)

    def _sync_controls(self, snapshot: ViewerSnapshot) -> None:
        if snapshot.camera_names:
            self.camera_combo.configure(state="readonly")
            self.camera_combo["values"] = snapshot.camera_names
            if snapshot.selected_camera_name and self.camera_var.get() != snapshot.selected_camera_name:
                self.camera_var.set(snapshot.selected_camera_name)
        else:
            self.camera_combo.configure(state="disabled")
            self.camera_combo["values"] = ()
            self.camera_var.set("")

        if snapshot.attempt_exists:
            normalized_attempt = snapshot.attempt_name
            if self.attempt_var.get() != normalized_attempt:
                self.attempt_var.set(normalized_attempt)

        if not snapshot.attempt_exists or not snapshot.camera_names:
            self.complete_panel.clear("No COMPLETE image")
            self.recovered_panel.clear("No RECOVERED image")

    def _render_snapshot(self, snapshot: ViewerSnapshot) -> None:
        if snapshot.complete_frame is not None:
            self.complete_panel.render(snapshot.complete_frame, fit_to_window=self.fit_to_window_var.get())
        if snapshot.recovered_frame is not None:
            self.recovered_panel.render(snapshot.recovered_frame, fit_to_window=self.fit_to_window_var.get())

    def _update_status_bar(self, snapshot: ViewerSnapshot) -> None:
        complete_id = snapshot.complete_frame.frame_id if snapshot.complete_frame else "-"
        recovered_id = snapshot.recovered_frame.frame_id if snapshot.recovered_frame else "-"
        self.status_var.set(snapshot.status_message)
        self.status_detail_var.set(
            f"attempt={snapshot.attempt_name} | camera={snapshot.selected_camera_name or '-'} | "
            f"COMPLETE={complete_id} | RECOVERED={recovered_id} | fps={self._ui_fps:0.1f} | "
            f"files={snapshot.discovered_file_count} | read_errors={snapshot.read_error_count} | "
            f"{snapshot.polling_status} | updating={'yes' if snapshot.archive_updating else 'no'}"
        )

    def _current_folder_for_snapshot(self, snapshot: ViewerSnapshot) -> Path | None:
        if snapshot.complete_frame is not None:
            return snapshot.complete_frame.pgm_path.parent
        if snapshot.recovered_frame is not None:
            return snapshot.recovered_frame.pgm_path.parent
        if snapshot.attempt_exists:
            attempt_dir = self.backend.archive_root / snapshot.attempt_name
            if snapshot.selected_camera_name:
                return attempt_dir / snapshot.selected_camera_name
            return attempt_dir
        return None


def main(argv: list[str] | None = None) -> int:
    from .viewer_cli import build_argument_parser

    args = build_argument_parser().parse_args(argv)
    app = CameraViewerApp(
        archive_root=args.archive_root,
        attempt=args.attempt,
        camera=args.camera,
        poll_interval_ms=args.poll_interval_ms,
        refresh_interval_ms=args.refresh_interval_ms,
    )
    app.run()
    return 0
