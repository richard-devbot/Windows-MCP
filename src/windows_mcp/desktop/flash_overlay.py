"""Brief on-screen visual confirmation that a screenshot was taken.

Renders an orange-red glow halo around the captured area for ~2.5 s using
small **opaque** Tk Toplevel windows arranged as concentric border strips
(top/bottom/left/right per glow layer). Per-window ``-alpha`` gives a
real fade and a stack of decreasing alphas creates the soft halo. Tk's
``-transparentcolor`` is deliberately *not* used because some Windows
configurations refuse to render a window when it is set, so the original
canvas-based approach was invisible on those systems.

The flash is started *after* capture and any active overlay is torn down
before the next capture so it never appears in a captured image.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_FLASH_COLOR = "#FF4500"
_DURATION_MS = 2500
_FRAME_INTERVAL_MS = 25
# Tk on Windows hangs its mainloop when ~8+ Toplevels are created back-to-back
# on a non-main thread (overrideredirect + alpha), so the halo is rendered with
# a single ring of 4 thick strips rather than a multi-layer gradient.
_GLOW_LAYERS = 1
_LAYER_THICKNESS = 8
_FULLSCREEN_INSET = 6

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
    """Tear down any flash overlay currently on screen."""
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
    """Show a fade-in/out orange-red glow around each rect.

    ``rects`` are ``(left, top, right, bottom)`` tuples in virtual-screen
    coordinates. ``full_screen=True`` draws an inner halo radiating inward
    from each monitor edge; ``full_screen=False`` draws an outer halo.
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
    """Return ``(l, t, r, b, base_alpha)`` for every strip window.

    The first layer sits on the rect edge at full alpha (1.0); each
    subsequent layer steps further away (outward for region, inward for
    full-screen) at quadratically falling base alpha.
    """
    strips: list[tuple[int, int, int, int, float]] = []
    base_inset = _FULLSCREEN_INSET if full_screen else 0
    for r_left, r_top, r_right, r_bottom in rects:
        for layer in range(_GLOW_LAYERS):
            base_alpha = (1.0 - layer / _GLOW_LAYERS) ** 2
            if full_screen:
                offset = base_inset + layer * _LAYER_THICKNESS
            else:
                offset = -(layer + 1) * _LAYER_THICKNESS
            left = r_left + offset
            top = r_top + offset
            right = r_right - offset
            bottom = r_bottom - offset
            if right - left < _LAYER_THICKNESS * 2 or bottom - top < _LAYER_THICKNESS * 2:
                continue
            strips.append((left, top, right, top + _LAYER_THICKNESS, base_alpha))
            strips.append((left, bottom - _LAYER_THICKNESS, right, bottom, base_alpha))
            strips.append((left, top, left + _LAYER_THICKNESS, bottom, base_alpha))
            strips.append((right - _LAYER_THICKNESS, top, right, bottom, base_alpha))
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

        windows: list[tuple["tk.Toplevel", float]] = []
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
            windows.append((w, base_alpha))

        logger.info(
            "screenshot flash overlay started: %d strip windows for %d rect(s) (full_screen=%s)",
            len(windows),
            len(rects),
            full_screen,
        )
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
            for w, base_alpha in windows:
                try:
                    w.attributes("-alpha", max(0.05, min(1.0, base_alpha * time_alpha)))
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
