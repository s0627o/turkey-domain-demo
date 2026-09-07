from __future__ import annotations

import threading
from collections.abc import Callable


class LeaseHeartbeat:
    def __init__(self, renew: Callable[[], None], *, interval_seconds: float) -> None:
        self.renew = renew
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._started = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> LeaseHeartbeat:
        self._thread.start()
        self._started.wait()
        self._raise_if_failed()
        return self

    def __exit__(self, error_type, _error, _traceback) -> None:
        self._stop.set()
        self._thread.join()
        if error_type is None:
            self._raise_if_failed()

    def _run(self) -> None:
        try:
            self.renew()
            self._started.set()
            while not self._stop.wait(self.interval_seconds):
                self.renew()
        except BaseException as error:
            self._error = error
            self._started.set()

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error
