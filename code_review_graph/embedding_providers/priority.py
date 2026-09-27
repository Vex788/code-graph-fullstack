"""Lower the priority of the calling thread only (never the whole process)."""

from __future__ import annotations

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

_QOS_CLASS_UTILITY = 0x11


def lower_current_thread_priority(step: int = 10) -> str:
    """Best effort; returns what was applied: ``nice+N``, ``qos-utility`` or ``unchanged``.

    Linux nice values are per thread, so ``setpriority`` on the thread id
    leaves the caller's other threads alone. On macOS ``os.nice`` would
    renice the whole process, so the thread's QoS class is lowered instead.
    """
    if sys.platform == "darwin":
        try:
            import ctypes
            import ctypes.util

            lib = ctypes.CDLL(ctypes.util.find_library("System") or "libSystem.dylib")
            if lib.pthread_set_qos_class_self_np(_QOS_CLASS_UTILITY, 0) == 0:
                return "qos-utility"
        except (OSError, AttributeError) as exc:
            logger.debug("Cannot lower thread QoS: %s", exc)
        return "unchanged"
    if sys.platform.startswith("linux") and hasattr(os, "setpriority"):
        try:
            tid = threading.get_native_id()
            current = os.getpriority(os.PRIO_PROCESS, tid)
            target = min(19, current + step)
            if target > current:
                os.setpriority(os.PRIO_PROCESS, tid, target)
            return f"nice+{target - current}"
        except OSError as exc:
            logger.debug("Cannot renice embedding thread: %s", exc)
    return "unchanged"
