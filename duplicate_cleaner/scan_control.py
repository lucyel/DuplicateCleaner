"""Cooperative scan suspension without changing a running scan's settings."""

from threading import Event
from time import monotonic

from .models import Cancelled


class ScanControl(Event):
    def __init__(self, on_paused=None):
        super().__init__()
        self.pause_requested = Event()
        self.on_paused = on_paused
        self.paused_seconds = 0.0

    def pause(self):
        self.pause_requested.set()

    def resume(self):
        self.pause_requested.clear()

    def checkpoint(self):
        if self.pause_requested.is_set() and not self.is_set():
            started = monotonic()
            if self.on_paused:
                self.on_paused()
            while self.pause_requested.is_set() and not self.wait(0.1):
                pass
            self.paused_seconds += monotonic() - started
        if self.is_set():
            raise Cancelled()


def check_scan(cancel):
    if isinstance(cancel, ScanControl):
        cancel.checkpoint()
    elif cancel.is_set():
        raise Cancelled()


def active_scan_time(cancel):
    return monotonic() - (cancel.paused_seconds if isinstance(cancel, ScanControl) else 0.0)
