from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time
from pathlib import Path
from typing import Callable, Generic, TypeVar

from .archive_layout import (
    AttemptDirectoryInfo,
    FrameArtifactRef,
    choose_latest_candidate,
    discover_attempt_info,
    iter_complete_candidates,
    iter_recovered_candidates,
    select_camera_directory,
)
from .image_loader import LoadedArchiveFrame, load_archive_frame


T = TypeVar("T")


@dataclass(frozen=True)
class ViewerSnapshot:
    generation: int
    attempt_name: str
    attempt_exists: bool
    camera_names: tuple[str, ...]
    selected_camera_name: str | None
    status_message: str
    complete_frame: LoadedArchiveFrame | None
    recovered_frame: LoadedArchiveFrame | None
    discovered_file_count: int
    read_error_count: int
    archive_updating: bool
    polling_status: str


class LatestMailbox(Generic[T]):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: T | None = None
        self._has_value = False

    def put_latest(self, value: T) -> None:
        with self._lock:
            self._value = value
            self._has_value = True

    def take_latest(self) -> T | None:
        with self._lock:
            if not self._has_value:
                return None
            self._has_value = False
            return self._value

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._has_value = False


class FrameLoaderWorker:
    def __init__(
        self,
        stream: str,
        *,
        on_loaded: Callable[[LoadedArchiveFrame], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.stream = stream
        self._input = LatestMailbox[FrameArtifactRef]()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._on_loaded = on_loaded
        self._on_error = on_error

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2.0)

    def submit(self, item: FrameArtifactRef) -> None:
        self._input.put_latest(item)
        self._wake.set()

    def clear(self) -> None:
        self._input.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.05)
            self._wake.clear()
            while not self._stop.is_set():
                item = self._input.take_latest()
                if item is None:
                    break
                try:
                    loaded = load_archive_frame(item)
                except Exception as exc:  # noqa: BLE001 - report and continue
                    self._on_error(f"{self.stream}: {exc}")
                    continue
                self._on_loaded(loaded)


class ArchiveViewerBackend:
    def __init__(
        self,
        archive_root: str | Path,
        *,
        poll_interval_ms: int = 50,
    ) -> None:
        if poll_interval_ms <= 0:
            raise ValueError("poll_interval_ms must be positive")
        self.archive_root = Path(archive_root).resolve()
        self.poll_interval_ms = poll_interval_ms
        self._attempt_text = "attempt1"
        self._camera_name: str | None = None
        self._generation = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._pause = threading.Event()
        self._monitor_thread = threading.Thread(target=self._run, daemon=True)
        self._last_signatures: dict[str, tuple[int, int, int, str]] = {}
        self._last_change_monotonic = 0.0
        self._read_error_count = 0
        self._status_message = "Waiting"
        self._attempt_exists = False
        self._camera_names: tuple[str, ...] = ()
        self._selected_camera_name: str | None = None
        self._discovered_file_count = 0
        self._archive_updating = False
        self._complete_frame: LoadedArchiveFrame | None = None
        self._recovered_frame: LoadedArchiveFrame | None = None
        self._polling_status = f"polling {poll_interval_ms} ms"
        self._loader_workers = {
            "COMPLETE": FrameLoaderWorker(
                "COMPLETE",
                on_loaded=self._accept_loaded_frame,
                on_error=self._record_error,
            ),
            "RECOVERED": FrameLoaderWorker(
                "RECOVERED",
                on_loaded=self._accept_loaded_frame,
                on_error=self._record_error,
            ),
        }

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def start(self) -> None:
        for worker in self._loader_workers.values():
            worker.start()
        self._monitor_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._monitor_thread.join(timeout=2.0)
        for worker in self._loader_workers.values():
            worker.stop()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()
        self._wake.set()

    def refresh_now(self) -> None:
        self._wake.set()

    def set_archive_root(self, archive_root: str | Path) -> None:
        with self._lock:
            self.archive_root = Path(archive_root).resolve()
            self._generation += 1
            self._complete_frame = None
            self._recovered_frame = None
            self._last_signatures.clear()
            self._status_message = "Refreshing"
            self._last_change_monotonic = time.monotonic()
            generation = self._generation
        for worker in self._loader_workers.values():
            worker.clear()
        self._wake.set()
        self._refresh_state(generation, status_message="Refreshing")

    def apply_configuration(
        self,
        archive_root: str | Path,
        attempt_text: str,
        camera_name: str | None = None,
    ) -> None:
        with self._lock:
            self.archive_root = Path(archive_root).resolve()
            self._generation += 1
            self._attempt_text = attempt_text
            self._camera_name = camera_name
            self._selected_camera_name = camera_name
            self._complete_frame = None
            self._recovered_frame = None
            self._status_message = "Refreshing"
            self._last_signatures.clear()
            self._last_change_monotonic = time.monotonic()
            generation = self._generation
        for worker in self._loader_workers.values():
            worker.clear()
        self._wake.set()
        self._refresh_state(generation, status_message="Refreshing")

    def set_selection(self, attempt_text: str, camera_name: str | None = None) -> None:
        with self._lock:
            self._generation += 1
            self._attempt_text = attempt_text
            self._camera_name = camera_name
            self._selected_camera_name = camera_name
            self._status_message = "Refreshing"
            self._complete_frame = None
            self._recovered_frame = None
            self._last_signatures.clear()
            self._last_change_monotonic = time.monotonic()
            generation = self._generation
        for worker in self._loader_workers.values():
            worker.clear()
        self._wake.set()
        self._refresh_state(generation, status_message="Refreshing")

    def snapshot(self) -> ViewerSnapshot:
        with self._lock:
            return ViewerSnapshot(
                generation=self._generation,
                attempt_name=self._attempt_text,
                attempt_exists=self._attempt_exists,
                camera_names=self._camera_names,
                selected_camera_name=self._selected_camera_name,
                status_message=self._status_message,
                complete_frame=self._complete_frame,
                recovered_frame=self._recovered_frame,
                discovered_file_count=self._discovered_file_count,
                read_error_count=self._read_error_count,
                archive_updating=self._archive_updating,
                polling_status=self._polling_status,
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._pause.is_set():
                self._wake.wait(self.poll_interval_ms / 1000.0)
                self._wake.clear()
                continue
            self._scan_once()
            self._wake.wait(self.poll_interval_ms / 1000.0)
            self._wake.clear()

    def _scan_once(self) -> None:
        with self._lock:
            generation = self._generation
            attempt_text = self._attempt_text
            selected_camera_name = self._camera_name

        try:
            attempt_info = discover_attempt_info(self.archive_root, attempt_text)
        except ValueError as exc:
            self._refresh_state(
                generation,
                status_message=f"Invalid attempt: {exc}",
                camera_names=(),
                discovered_file_count=0,
                archive_updating=False,
            )
            with self._lock:
                self._attempt_exists = False
                self._camera_names = ()
                self._selected_camera_name = None
                self._complete_frame = None
                self._recovered_frame = None
            return
        if not attempt_info.exists:
            self._refresh_state(
                generation,
                attempt_info=attempt_info,
                status_message="Attempt not found",
                camera_names=(),
                discovered_file_count=0,
                archive_updating=False,
            )
            return

        camera_names = tuple(
            camera.name
            for camera in attempt_info.camera_dirs
            if camera.has_archive
        )
        if not camera_names:
            self._refresh_state(
                generation,
                attempt_info=attempt_info,
                status_message="No camera archive found",
                camera_names=(),
                discovered_file_count=0,
                archive_updating=False,
            )
            return

        selected_camera = select_camera_directory(attempt_info, selected_camera_name)
        if selected_camera is None:
            selected_camera = next(
                camera for camera in attempt_info.camera_dirs if camera.has_archive
            )

        complete_candidates = iter_complete_candidates(
            selected_camera.path,
            attempt_name=attempt_info.attempt_name,
            camera_name=selected_camera.name,
            camera_id=selected_camera.camera_id,
            generation=generation,
        )
        recovered_candidates = iter_recovered_candidates(
            selected_camera.path,
            attempt_name=attempt_info.attempt_name,
            camera_name=selected_camera.name,
            camera_id=selected_camera.camera_id,
            generation=generation,
        )
        complete = choose_latest_candidate(complete_candidates)
        recovered = choose_latest_candidate(recovered_candidates)

        if complete is not None:
            self._maybe_submit_candidate(complete)
        if recovered is not None:
            self._maybe_submit_candidate(recovered)

        discovered_file_count = sum(
            1 for candidate in complete_candidates + recovered_candidates
        )
        archive_updating = self._is_recent_change()
        self._refresh_state(
            generation,
            attempt_info=attempt_info,
            camera_names=camera_names,
            selected_camera_name=selected_camera.name,
            status_message="Archive ready",
            discovered_file_count=discovered_file_count,
            archive_updating=archive_updating,
        )

    def _maybe_submit_candidate(self, candidate: FrameArtifactRef) -> None:
        with self._lock:
            last_signature = self._last_signatures.get(candidate.stream)
            if last_signature == candidate.signature:
                return
            self._last_signatures[candidate.stream] = candidate.signature
            self._last_change_monotonic = time.monotonic()
            worker = self._loader_workers[candidate.stream]
        worker.submit(candidate)

    def _accept_loaded_frame(self, frame: LoadedArchiveFrame) -> None:
        with self._lock:
            if frame.generation != self._generation:
                return
            if frame.stream == "COMPLETE":
                self._complete_frame = frame
            elif frame.stream == "RECOVERED":
                self._recovered_frame = frame
            self._status_message = "Archive ready"

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._read_error_count += 1
            self._status_message = message

    def _refresh_state(
        self,
        generation: int,
        *,
        attempt_info: AttemptDirectoryInfo | None = None,
        camera_names: tuple[str, ...] | None = None,
        selected_camera_name: str | None = None,
        status_message: str,
        discovered_file_count: int | None = None,
        archive_updating: bool | None = None,
    ) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if attempt_info is not None:
                self._attempt_exists = attempt_info.exists
                if attempt_info.exists:
                    self._attempt_text = attempt_info.attempt_name
            self._status_message = status_message
            if camera_names is not None:
                self._camera_names = camera_names
            if selected_camera_name is not None:
                self._selected_camera_name = selected_camera_name
            if discovered_file_count is not None:
                self._discovered_file_count = discovered_file_count
            if archive_updating is not None:
                self._archive_updating = archive_updating

    def _is_recent_change(self) -> bool:
        with self._lock:
            return (time.monotonic() - self._last_change_monotonic) < max(
                0.5,
                self.poll_interval_ms / 1000.0 * 4,
            )
