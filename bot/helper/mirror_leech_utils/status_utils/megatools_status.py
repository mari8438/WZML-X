from ...ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)


class MegaToolsStatus:
    def __init__(self, listener, obj, gid):
        self._gid = gid
        self._obj = obj
        self.listener = listener
        self.engine = "Megatools"

    def gid(self):
        return self._gid

    def progress_raw(self):
        try:
            return float(self._obj.percentage.rstrip("%"))
        except Exception:
            return 0.0

    def progress(self):
        return self._obj.percentage

    def speed(self):
        return self._obj.speed

    def name(self):
        return self._obj.name or self.listener.name or "Mega Download"

    def size(self):
        if self._obj.total_size_str:
            return self._obj.total_size_str
        if self.listener.size:
            return get_readable_file_size(self.listener.size)
        return "N/A"

    def eta(self):
        return self._obj.eta

    def status(self):
        return MirrorStatus.STATUS_DOWNLOAD

    def processed_bytes(self):
        if self._obj.processed_size_str:
            return self._obj.processed_size_str
        if self._obj.processed_bytes:
            return get_readable_file_size(self._obj.processed_bytes)
        return "0B"

    def task(self):
        return self._obj
