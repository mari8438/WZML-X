from os import path as ospath
from time import time

from .... import LOGGER
from ...ext_utils.status_utils import (
    EngineStatus,
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)


class AutoProcessStatus:
    """Live status for AutoProcess / Video Tools extraction and muxing.

    ``_processed_bytes`` counts fully finished inputs.  While a tool is
    writing the current output, ``_current_dest_file`` points at it and its
    on-disk size is added, so speed/progress/ETA move in real time instead
    of jumping once per finished episode.
    """

    def __init__(self, listener, gid=None, action="Auto Process", engine=None):
        self.listener = listener
        self._gid = gid or getattr(listener, "_process_gid", None) or str(listener.mid)
        self._action = action
        self._start_time = time()
        self._processed_bytes = 0
        self._total_bytes = (
            getattr(listener, "size", 0) or getattr(listener, "subsize", 0) or 1
        )
        self._current_file = getattr(listener, "name", "Media")
        self._current_dest_file = None
        self._current_dest_cap = 0
        self.engine = engine or EngineStatus().STATUS_MKVMERGE

    def set_progress(
        self,
        current_file=None,
        processed_bytes=None,
        total_bytes=None,
        engine=None,
        current_dest_file=None,
        current_input_size=None,
    ):
        if current_file:
            self._current_file = current_file
        if processed_bytes is not None:
            self._processed_bytes = processed_bytes
        if total_bytes:
            self._total_bytes = total_bytes
        if engine:
            self.engine = engine
        if current_dest_file is not None:
            # Empty string clears tracking (file finished).
            self._current_dest_file = current_dest_file or None
        if current_input_size is not None:
            self._current_dest_cap = current_input_size

    def _live_dest_bytes(self):
        dest = self._current_dest_file
        if not dest:
            return 0
        try:
            size = ospath.getsize(dest) if ospath.isfile(dest) else 0
        except OSError:
            return 0
        # Never let one in-flight file count for more than its input.
        if self._current_dest_cap:
            size = min(size, self._current_dest_cap)
        return size

    def processed_raw_bytes(self):
        total = self._processed_bytes + self._live_dest_bytes()
        return min(total, self._total_bytes) if self._total_bytes else total

    def _rate(self):
        elapsed = time() - self._start_time
        raw = self.processed_raw_bytes()
        if elapsed > 0.5 and raw > 0:
            return raw / elapsed
        return 0

    def speed(self):
        try:
            return f"{get_readable_file_size(self._rate())}/s"
        except Exception:
            return "0B/s"

    def processed_bytes(self):
        return get_readable_file_size(self.processed_raw_bytes())

    def progress(self):
        try:
            pct = min(100.0, self.processed_raw_bytes() / max(1, self._total_bytes) * 100.0)
        except Exception:
            pct = 0.0
        return f"{round(pct, 2)}%"

    def gid(self):
        return self._gid

    def name(self):
        return self._current_file or getattr(self.listener, "name", "Processing...")

    def size(self):
        return get_readable_file_size(self._total_bytes or self.listener.size)

    def eta(self):
        try:
            rate = self._rate()
            remaining = self._total_bytes - self.processed_raw_bytes()
            if rate > 0 and remaining > 0:
                return get_readable_time(remaining / rate)
            return "-"
        except Exception:
            return "-"

    def status(self):
        return MirrorStatus.STATUS_AUTOPROCESS

    def task(self):
        return self

    async def cancel_task(self):
        LOGGER.info(f"Cancelling {self._action}: {self.listener.name}")
        self.listener.is_cancelled = True
        if self.listener.subproc is not None and self.listener.subproc.returncode is None:
            try:
                self.listener.subproc.kill()
            except Exception:
                pass
        await self.listener.on_upload_error(f"{self._action} stopped by user!")
