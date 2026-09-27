"""Scripted ``time.sleep`` for the ``incremental.watch`` loop only.

Patching ``time.sleep`` globally hands the script to every thread that sleeps
while the test runs: a leftover daemon thread from an earlier test can steal a
tick (so an action runs before the observer is scheduled), and a lock or retry
sleep in watch's startup, outside the loop's ``except KeyboardInterrupt``,
receives the terminal interrupt, which aborts the whole pytest session.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from code_review_graph import incremental

_real_sleep = time.sleep


@contextmanager
def watch_loop_sleep(side_effect: Any) -> Iterator[None]:
    """Run *side_effect* for watch-loop ticks on this thread; all else really sleeps.

    *side_effect* is an exception (class or instance) to raise, or a callable
    taking the requested seconds, like ``Mock.side_effect``.
    """
    owner = threading.current_thread()
    watch_code = incremental.watch.__code__

    def sleep(seconds: float) -> Any:
        if threading.current_thread() is owner and sys._getframe(1).f_code is watch_code:
            if isinstance(side_effect, BaseException) or (
                isinstance(side_effect, type) and issubclass(side_effect, BaseException)
            ):
                raise side_effect
            call: Callable[[float], Any] = side_effect
            return call(seconds)
        return _real_sleep(seconds)

    with patch("time.sleep", new=sleep):
        yield
