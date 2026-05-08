"""Brief on-screen visual confirmation that a screenshot was taken.

Renders a glowing orange-red halo around the captured area for ~2.5 s and
then fades out. Implementation uses one ``tk.Toplevel`` per glow layer per
side (top/bottom/left/right) so per-window ``-alpha`` works reliably — the
``-alpha`` + ``-transparentcolor`` combination is unreliable on Windows.

The flash is started *after* capture and any active overlay is torn down
before the next capture, so it never appears in a captured image.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_FLASH_COLOR = "#FF4500"
_DURATION_MS = 2500
_FRAME_INTERVAL_MS = 20
_GLOW_LAYERS = 8
_FULLSCREEN_INSET = 4

_lock = threading.Lock()
_active_overlay: "_Overlay | None" = None


def _flash_disabled() -> bool:
    value = os.getenv("WINDOWS_MCP_DISABLE_FLASH", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


class _Overlay:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.closed_event = threading.Event()
        self.thread: threading.Thread | None = None


def cancel_active_flash(timeout: float = 0.25) -> None:
    """Tear down any flash overlay currently on screen.

    Call this immediately before taking a screenshot so the previous flash
    can never bleed into the new capture.
    """
    global _active_overlay
    with _lock:
        ov = _active_overlay
        _active_overlay = None
    if ov is None:
        return
    ov.stop_event.set()
    ov.closed_event.wait(timeout=timeout)


def show_capture_flash(
    rects: list[tuple[int, int, int, int]],
    *,
    full_screen: bool,
) -> None:
    """Show a fade-in/out orange-red glow over each rect.

    ``rects`` are ``(left, top, right, bottom)`` tuples in virtual-screen
    coordinates. ``full_screen=True`` draws an inner glow that radiates
    inward from each monitor edge. ``full_screen=False`` draws an outer
    halo around the captured region.

    Returns immediately; rendering happens on a daemon thread.
    """
    if _flash_disabled() or not rects:
        return
    rects = [tuple(r) for r in rects]
    overlay = _Overlay()
    overlay.thread = threading.Thread(
        target=_run_overlay,
        args=(rects, full_screen, overlay),
        name="windows-mcp-flash",
        daemon=True,
    )
    with _lock:
        global _active_overlay
        _active_overlay = overlay
    overlay.thread.start()


def _build_strip_defs(
    rects: list[tuple[int, int, int, int]],
    full_screen: bool,
) -> list[tuple[int, int, int, int, float]]:
    """Return ``(l, t, r, b, base_alpha)`` strip rects for every glow layer.

    For region mode the strips spread *outward* from the rect edge; for
    full-screen mode they spread *inward* from each monitor edge. Layer 0
    sits exactly on the rect/edge boundary at full opacity; subsequent
    layers step further away with a quadratic falloff.
    """
    strips: list[tuple[int, int, int, int, float]] = []
    base_inset = _FULLSCREEN_INSET if full_screen else 0
    for r_left, r_top, r_right, r_bottom in rects:
        for layer in range(_GLOW_LAYERS):
            falloff = (1.0 - layer / _GLOW_LAYERS) ** 2
            if full_screen:
                offset = base_inset + layer
            else:
                offset = -layer
            left = r_left + offset
            top = r_top + offset
            right = r_right - offset
            bottom = r_bottom - offset
            if right - left < 2 or bottom - top < 2:
                continue
            strips.append((left, top, right, top + 1, falloff))
            strips.append((left, bottom - 1, right, bottom, falloff))
            strips.append((left, top, left + 1, bottom, falloff))
            strips.append((right - 1, top, right, bottom, falloff))
    return strips


def _run_overlay(
    rects: list[tuple[int, int, int, int]],
    full_screen: bool,
    overlay: _Overlay,
) -> None:
    try:
        import tkinter as tk
    except Exception:
        logger.debug("tkinter unavailable; skipping screenshot flash")
        overlay.closed_event.set()
        return

    root: "tk.Tk | None" = None
    try:
        strip_defs = _build_strip_defs(rects, full_screen)
        if not strip_defs:
            return

        root = tk.Tk()
        root.withdraw()

        strip_windows: list[tuple["tk.Toplevel", float]] = []
        for left, top, right, bottom, base_alpha in strip_defs:
            w = tk.Toplevel(root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            try:
                w.attributes("-toolwindow", True)
            except tk.TclError:
                pass
            w.configure(bg=_FLASH_COLOR)
            w.geometry(f"{right - left}x{bottom - top}+{left}+{top}")
            try:
                w.attributes("-alpha", base_alpha)
            except tk.TclError:
                pass
            strip_windows.append((w, base_alpha))

        start = time.perf_counter()

        def tick() -> None:
            if overlay.stop_event.is_set():
                root.destroy()
                return
            elapsed_ms = (time.perf_counter() - start) * 1000
            if elapsed_ms >= _DURATION_MS:
                root.destroy()
                return
            t_norm = elapsed_ms / _DURATION_MS
            if full_screen:
                time_alpha = 1.0 - abs(2 * t_norm - 1)
            elif t_norm < 0.15:
                time_alpha = t_norm / 0.15
            elif t_norm < 0.65:
                time_alpha = 1.0
            else:
                time_alpha = max(0.0, 1.0 - (t_norm - 0.65) / 0.35)
            for w, base_alpha in strip_windows:
                try:
                    w.attributes("-alpha", max(0.0, min(1.0, base_alpha * time_alpha)))
                except tk.TclError:
                    pass
            root.after(_FRAME_INTERVAL_MS, tick)

        root.after(0, tick)
        root.mainloop()
    except Exception:
        logger.debug("screenshot flash overlay failed", exc_info=True)
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    finally:
        with _lock:
            global _active_overlay
            if _active_overlay is overlay:
                _active_overlay = None
        overlay.closed_event.set()
