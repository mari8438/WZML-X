from time import time
from .... import LOGGER
from ...ext_utils.status_utils import (
    get_readable_file_size,
    EngineStatus,
    MirrorStatus,
    get_readable_time,
)


class AutoProcessStatus:
    def __init__(self, listener, gid, action="Auto Process", engine=None):
        self.listener = listener
        self._gid = gid
        self._action = action
        self._start_time = time()
        self._processed_bytes = 0
        self._total_bytes = getattr(listener, "size", 0) or getattr(listener, "subsize", 0) or 1
        self._progress = 0.0
        self._current_file = getattr(listener, "name", "Media")
        eng = EngineStatus()
        self.engine = engine or eng.STATUS_MKVMERGE

    def set_progress(self, current_file=None, processed_bytes=None, total_bytes=None, engine=None):
        if current_file:
            self._current_file = current_file
        if processed_bytes is not None:
            self._processed_bytes = processed_bytes
        if total_bytes:
            self._total_bytes = total_bytes
        if self._total_bytes > 0:
            self._progress = min(100.0, (self._processed_bytes / self._total_bytes) * 100.0)
        if engine:
            self.engine = engine

    def speed(self):
        try:
            elapsed = time() - self._start_time
            if elapsed > 0.5 and self._processed_bytes > 0:
                return f"{get_readable_file_size(self._processed_bytes / elapsed)}/s"
            return "0 B/s"
        except Exception:
            return "0 B/s"

    def processed_bytes(self):
        return get_readable_file_size(self._processed_bytes)

    def progress(self):
        return f"{round(self._progress, 2)}%"

    def gid(self):
        return self._gid

    def name(self):
        return self._current_file or getattr(self.listener, "name", "Processing...")

    def size(self):
        return get_readable_file_size(self._total_bytes) if self._total_bytes else get_readable_file_size(self.listener.size)

    def eta(self):
        try:
            elapsed = time() - self._start_time
            if elapsed > 1 and self._processed_bytes > 0 and self._total_bytes > self._processed_bytes:
                rate = self._processed_bytes / elapsed
                rem_bytes = self._total_bytes - self._processed_bytes
                return get_readable_time(rem_bytes / rate)
            return "-"
        except Exception:
            return "-"

    def status(self):
        return MirrorStatus.STATUS_AUTOPROCESS

    def task(self):
        return self

    async def cancel_task(self):
        LOGGER.info(f"Cancelling AutoProcess: {self.listener.name}")
        self.listener.is_cancelled = True
        if self.listener.subproc is not None and self.listener.subproc.returncode is None:
            try:
                self.listener.subproc.kill()
            except Exception:
                pass
        await self.listener.on_upload_error(f"{self._action} stopped by user!")
