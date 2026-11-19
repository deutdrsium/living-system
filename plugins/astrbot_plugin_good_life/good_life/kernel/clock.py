"""Online intervals use monotonic time; persisted anchors remain aware UTC."""
import time


class MonotonicClock:
    def __init__(self, clock=time.monotonic):
        self._clock = clock

    def stamp(self, now):
        # `now` is the caller's business UTC boundary, not an interval source.
        return self._clock()
