"""Linux X11 backend for computer_use.

Uses xdotool for mouse/keyboard/window control, mss for screenshots,
and Pillow for SOM (Set of Marks) overlay drawing.

Tested on Linux Mint 22.3 (X11 + Cinnamon). Should work on any X11
session with xdotool and a compatible window manager.

Install dependencies:
    sudo apt-get install xdotool wmctrl scrot
    pip3 install mss pillow pyautogui python-xlib
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY"))


def _check_xdotool() -> bool:
    return bool(shutil.which("xdotool"))


def _check_wmctrl() -> bool:
    return bool(shutil.which("wmctrl"))


def linux_backend_available() -> bool:
    """True if all Linux backend requirements are met."""
    if not _is_linux():
        return False
    if not _has_display():
        return False
    if not _check_xdotool():
        return False
    return True


def linux_backend_install_hint() -> str:
    return (
        "Linux computer_use backend is not available. Install with:\n"
        "  sudo apt-get install xdotool wmctrl scrot\n"
        "  pip3 install mss pillow pyautogui python-xlib\n"
        "Ensure $DISPLAY is set (X11 session required; Wayland not supported)."
    )


# ---------------------------------------------------------------------------
# X11 / xdotool helpers
# ---------------------------------------------------------------------------

def _run_xdotool(*args: str, timeout: float = 10.0) -> Tuple[bool, str, str]:
    """Run xdotool with args. Returns (ok, stdout, stderr)."""
    cmd = ["xdotool"] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
        )
        ok = result.returncode == 0
        return ok, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "xdotool timed out"
    except Exception as e:
        return False, "", str(e)


def _run_wmctrl(*args: str, timeout: float = 10.0) -> Tuple[bool, str, str]:
    """Run wmctrl with args. Returns (ok, stdout, stderr)."""
    cmd = ["wmctrl"] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
        )
        ok = result.returncode == 0
        return ok, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "wmctrl timed out"
    except Exception as e:
        return False, "", str(e)


def _get_screen_size() -> Tuple[int, int]:
    """Get screen width and height."""
    ok, out, _ = _run_xdotool("getdisplaygeometry")
    if ok:
        parts = out.strip().split()
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    # Fallback
    return 1920, 1080


def _list_windows() -> List[Dict[str, Any]]:
    """List all windows using wmctrl -l -p -G."""
    windows = []
    ok, out, err = _run_wmctrl("-l", "-p", "-G")
    if not ok:
        logger.warning("wmctrl -l -p -G failed: %s", err)
        return windows

    # Format: id desktop pid x y width height class title
    for line in out.strip().splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            window_id = int(parts[0], 16)
            desktop = int(parts[1])
            pid = int(parts[2])
            x = int(parts[3])
            y = int(parts[4])
            width = int(parts[5])
            height = int(parts[6])
            title = parts[7]
            # Get class name via xdotool
            cls_ok, cls_out, _ = _run_xdotool(
                "getwindowclassname", str(window_id)
            )
            class_name = cls_out.strip() if cls_ok else ""
            # Get app name from PID
            app_name = ""
            try:
                with open(f"/proc/{pid}/comm", "r") as f:
                    app_name = f.read().strip()
            except Exception:
                app_name = class_name or title.split()[0] if title else ""
            windows.append({
                "window_id": window_id,
                "desktop": desktop,
                "pid": pid,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "title": title,
                "class_name": class_name,
                "app_name": app_name,
            })
        except (ValueError, IndexError):
            continue
    return windows


def _get_window_geometry(window_id: int) -> Tuple[int, int, int, int]:
    """Get window geometry (x, y, width, height)."""
    ok, out, _ = _run_xdotool("getwindowgeometry", "--shell", str(window_id))
    if ok:
        x = y = width = height = 0
        for line in out.strip().splitlines():
            if line.startswith("X="):
                x = int(line.split("=", 1)[1])
            elif line.startswith("Y="):
                y = int(line.split("=", 1)[1])
            elif line.startswith("WIDTH="):
                width = int(line.split("=", 1)[1])
            elif line.startswith("HEIGHT="):
                height = int(line.split("=", 1)[1])
        return x, y, width, height
    return 0, 0, 800, 600


def _get_active_window() -> Optional[int]:
    """Get the currently active window ID."""
    ok, out, _ = _run_xdotool("getactivewindow")
    if ok:
        try:
            return int(out.strip())
        except ValueError:
            pass
    return None


def _activate_window(window_id: int) -> bool:
    """Activate a window without necessarily raising it."""
    ok, _, _ = _run_xdotool("windowactivate", "--sync", str(window_id))
    return ok


def _raise_window(window_id: int) -> bool:
    """Raise a window to the front."""
    ok, _, _ = _run_xdotool("windowraise", str(window_id))
    return ok


def _focus_window(window_id: int) -> bool:
    """Focus a window."""
    ok, _, _ = _run_xdotool("windowfocus", "--sync", str(window_id))
    return ok


# ---------------------------------------------------------------------------
# Screenshot helpers
# ---------------------------------------------------------------------------

def _take_screenshot(window_id: Optional[int] = None) -> Tuple[Optional[bytes], int, int]:
    """Take a screenshot. Returns (png_bytes, width, height).
    
    Uses mss if available, falls back to scrot or gnome-screenshot.
    """
    try:
        import mss
        with mss.mss() as sct:
            if window_id:
                # Get window geometry and capture that region
                x, y, w, h = _get_window_geometry(window_id)
                monitor = {"left": x, "top": y, "width": w, "height": h}
            else:
                monitor = sct.monitors[0]  # Primary monitor
            screenshot = sct.grab(monitor)
            from PIL import Image
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue(), screenshot.width, screenshot.height
    except ImportError:
        pass

    # Fallback to scrot
    if shutil.which("scrot"):
        tmp_path = "/tmp/linux_backend_screenshot.png"
        if window_id:
            ok, _, _ = _run_xdotool(
                "windowraise", str(window_id)
            )
            ok, _, _ = subprocess.run(
                ["scrot", "-u", tmp_path],
                capture_output=True,
                timeout=10,
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
            )
        else:
            subprocess.run(
                ["scrot", tmp_path],
                capture_output=True,
                timeout=10,
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
            )
        try:
            with open(tmp_path, "rb") as f:
                data = f.read()
            from PIL import Image
            with Image.open(io.BytesIO(data)) as img:
                return data, img.width, img.height
        except Exception:
            pass

    # Fallback to gnome-screenshot
    if shutil.which("gnome-screenshot"):
        tmp_path = "/tmp/linux_backend_screenshot.png"
        subprocess.run(
            ["gnome-screenshot", "-f", tmp_path],
            capture_output=True,
            timeout=10,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
        )
        try:
            with open(tmp_path, "rb") as f:
                data = f.read()
            from PIL import Image
            with Image.open(io.BytesIO(data)) as img:
                return data, img.width, img.height
        except Exception:
            pass

    return None, 0, 0


def _draw_som_overlay(
    img_bytes: bytes,
    elements: List[UIElement],
) -> bytes:
    """Draw numbered overlays on interactable elements.
    
    Returns PNG bytes with SOM (Set of Marks) overlays.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(io.BytesIO(img_bytes))
    draw = ImageDraw.Draw(img)

    # Try to get a font, fall back to default
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 14)
        except Exception:
            font = ImageFont.load_default()

    for elem in elements:
        x, y, w, h = elem.bounds
        if w <= 0 or h <= 0:
            continue
        cx, cy = elem.center()

        # Draw circle with number
        radius = 12
        bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
        draw.ellipse(bbox, fill="red", outline="white", width=2)

        # Draw number
        text = str(elem.index)
        bbox_text = draw.textbbox((0, 0), text, font=font)
        tw = bbox_text[2] - bbox_text[0]
        th = bbox_text[3] - bbox_text[1]
        draw.text(
            (cx - tw // 2, cy - th // 2 - 1),
            text,
            fill="white",
            font=font,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Element detection
# ---------------------------------------------------------------------------

def _detect_interactable_elements(window_id: int) -> List[UIElement]:
    """Detect interactable elements in a window.
    
    Uses a heuristic approach since Linux doesn't have a universal AX API
    like macOS. We use xdotool to find clickable regions and combine with
    window geometry.
    
    Returns a list of UIElement with index, role, label, bounds.
    """
    elements = []
    x, y, w, h = _get_window_geometry(window_id)

    # Get window title for context
    ok, title, _ = _run_xdotool("getwindowname", str(window_id))
    window_title = title.strip() if ok else ""

    # Try to get more info via xprop
    try:
        result = subprocess.run(
            ["xprop", "-id", str(window_id)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
        )
        xprop_out = result.stdout
    except Exception:
        xprop_out = ""

    # For now, create a single element representing the window itself
    # In a more advanced implementation, we could use AT-SPI or other
    # accessibility frameworks to enumerate child elements
    elements.append(UIElement(
        index=1,
        role="AXWindow",
        label=window_title,
        bounds=(x, y, w, h),
        app="",
        pid=0,
        window_id=window_id,
    ))

    # Try to get child windows / sub-windows
    ok, out, _ = _run_xdotool("search", "--onlyvisible", "--pid", str(_get_window_pid(window_id)))
    if ok:
        child_ids = []
        for line in out.strip().splitlines():
            try:
                cid = int(line.strip())
                if cid != window_id:
                    child_ids.append(cid)
            except ValueError:
                continue

        idx = 2
        for cid in child_ids[:50]:  # Limit to avoid overwhelming
            cx, cy, cw, ch = _get_window_geometry(cid)
            ok, ctitle, _ = _run_xdotool("getwindowname", str(cid))
            child_title = ctitle.strip() if ok else ""
            if cw > 10 and ch > 10:
                elements.append(UIElement(
                    index=idx,
                    role="AXGroup",
                    label=child_title,
                    bounds=(cx, cy, cw, ch),
                    app="",
                    pid=0,
                    window_id=cid,
                ))
                idx += 1

    return elements


def _get_window_pid(window_id: int) -> int:
    """Get the PID of a window."""
    ok, out, _ = _run_xdotool("getwindowpid", str(window_id))
    if ok:
        try:
            return int(out.strip())
        except ValueError:
            pass
    return 0


# ---------------------------------------------------------------------------
# Key combo parsing
# ---------------------------------------------------------------------------

def _parse_key_combo(keys: str) -> Tuple[Optional[str], List[str]]:
    """Parse a key string like 'ctrl+s' into (key, modifiers).
    
    Maps macOS-style modifiers to Linux equivalents.
    """
    MODIFIER_NAMES = {"cmd", "command", "shift", "option", "alt", "ctrl", "control", "fn", "super", "win"}
    KEY_ALIASES = {
        "command": "super",
        "cmd": "super",
        "alt": "alt",
        "option": "alt",
        "control": "ctrl",
        "return": "Return",
        "enter": "Return",
        "esc": "Escape",
        "tab": "Tab",
        "space": "space",
        "backspace": "BackSpace",
        "delete": "Delete",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "home": "Home",
        "end": "End",
        "pageup": "Page_Up",
        "pagedown": "Page_Down",
    }

    parts = [p.strip().lower() for p in re.split(r'[+\-]', keys) if p.strip()]
    modifiers = []
    key = None
    for part in parts:
        normalized = KEY_ALIASES.get(part, part)
        if normalized in MODIFIER_NAMES or part in MODIFIER_NAMES:
            # Map to xdotool modifier names
            mod_map = {
                "cmd": "super",
                "command": "super",
                "alt": "alt",
                "option": "alt",
                "ctrl": "ctrl",
                "control": "ctrl",
                "shift": "shift",
                "fn": "",
                "super": "super",
                "win": "super",
            }
            mapped = mod_map.get(part, part)
            if mapped:
                modifiers.append(mapped)
        else:
            key = normalized
    return key, modifiers


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------

class LinuxX11Backend(ComputerUseBackend):
    """Linux computer-use backend via xdotool + mss/scrot.
    
    Supports X11 sessions. Wayland is NOT supported.
    """

    def __init__(self) -> None:
        self._active_pid: Optional[int] = None
        self._active_window_id: Optional[int] = None
        self._last_app: Optional[str] = None
        self._screen_width: int = 1920
        self._screen_height: int = 1080

    # ── Lifecycle ──────────────────────────────────────────────────
    def start(self) -> None:
        self._screen_width, self._screen_height = _get_screen_size()
        logger.info("LinuxX11Backend started. Screen: %dx%d", self._screen_width, self._screen_height)

    def stop(self) -> None:
        logger.info("LinuxX11Backend stopped.")

    def is_available(self) -> bool:
        return linux_backend_available()

    # ── Capture ────────────────────────────────────────────────────
    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        """Capture the screen or a specific window.
        
        mode: "som" (screenshot + element overlays), "vision" (screenshot only), "ax" (elements only)
        app: Optional app name to filter by
        """
        # Step 1: Find target window
        windows = _list_windows()
        if not windows:
            return CaptureResult(
                mode=mode, width=0, height=0, png_b64=None,
                elements=[], app="", window_title="",
                png_bytes_len=0,
            )

        # Filter by app name if requested
        target_window = None
        if app:
            app_lower = app.lower()
            matched = [
                w for w in windows
                if app_lower in w["app_name"].lower()
                or app_lower in w["class_name"].lower()
                or app_lower in w["title"].lower()
            ]
            if matched:
                target_window = matched[0]
            else:
                return CaptureResult(
                    mode=mode, width=0, height=0, png_b64=None,
                    elements=[], app="",
                    window_title=f"<no window matched app={app!r}; call list_apps to see available apps>",
                    png_bytes_len=0,
                )
        else:
            # Use active window or frontmost
            active_id = _get_active_window()
            if active_id:
                for w in windows:
                    if w["window_id"] == active_id:
                        target_window = w
                        break
            if not target_window:
                target_window = windows[0]

        window_id = target_window["window_id"]
        self._active_window_id = window_id
        self._active_pid = target_window["pid"]
        app_name = target_window["app_name"]
        window_title = target_window["title"]

        if app or not self._last_app:
            self._last_app = app_name

        # Step 2: Capture
        png_b64: Optional[str] = None
        elements: List[UIElement] = []
        width = height = 0
        png_bytes_len = 0

        if mode == "ax":
            # Elements only, no screenshot
            elements = _detect_interactable_elements(window_id)
            x, y, w, h = _get_window_geometry(window_id)
            width, height = w, h
        else:
            # Screenshot (vision or som)
            img_bytes, width, height = _take_screenshot(window_id)
            if img_bytes:
                # Detect elements for SOM mode
                if mode == "som":
                    elements = _detect_interactable_elements(window_id)
                    if elements:
                        img_bytes = _draw_som_overlay(img_bytes, elements)
                png_b64 = base64.b64encode(img_bytes).decode("utf-8")
                png_bytes_len = len(img_bytes)

        return CaptureResult(
            mode=mode,
            width=width,
            height=height,
            png_b64=png_b64,
            elements=elements,
            app=app_name,
            window_title=window_title,
            png_bytes_len=png_bytes_len,
        )

    # ── Pointer ────────────────────────────────────────────────────
    def click(
        self,
        *,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        window_id = self._active_window_id
        if window_id is None:
            return ActionResult(ok=False, action="click",
                                message="No active window — call capture() first.")

        # Focus the window first
        _focus_window(window_id)
        time.sleep(0.1)

        # Determine coordinates
        if element is not None:
            # Find element by index
            elements = _detect_interactable_elements(window_id)
            target_elem = None
            for e in elements:
                if e.index == element:
                    target_elem = e
                    break
            if target_elem is None:
                return ActionResult(ok=False, action="click",
                                    message=f"Element #{element} not found.")
            cx, cy = target_elem.center()
        elif x is not None and y is not None:
            cx, cy = x, y
        else:
            return ActionResult(ok=False, action="click",
                                message="click requires element= or x/y.")

        # Build xdotool command
        args = ["mousemove", "--sync", str(cx), str(cy)]
        
        # Handle modifiers
        if modifiers:
            for mod in modifiers:
                args.extend(["keydown", mod])
        
        # Click
        click_arg = "click"
        if button == "right":
            click_arg = "click"
            args.extend([click_arg, "--repeat", str(click_count), "3"])
        elif button == "middle":
            args.extend([click_arg, "--repeat", str(click_count), "2"])
        else:
            args.extend([click_arg, "--repeat", str(click_count), "1"])
        
        # Release modifiers
        if modifiers:
            for mod in reversed(modifiers):
                args.extend(["keyup", mod])
        
        ok, out, err = _run_xdotool(*args)
        if ok:
            return ActionResult(ok=True, action="click",
                                message=f"Clicked at ({cx}, {cy}) with {button} button x{click_count}")
        return ActionResult(ok=False, action="click", message=f"Click failed: {err}")

    def drag(
        self,
        *,
        from_element: Optional[int] = None,
        to_element: Optional[int] = None,
        from_xy: Optional[Tuple[int, int]] = None,
        to_xy: Optional[Tuple[int, int]] = None,
        button: str = "left",
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        window_id = self._active_window_id
        if window_id is None:
            return ActionResult(ok=False, action="drag",
                                message="No active window — call capture() first.")

        _focus_window(window_id)
        time.sleep(0.1)

        # Resolve coordinates
        if from_element is not None and to_element is not None:
            elements = _detect_interactable_elements(window_id)
            from_elem = to_elem = None
            for e in elements:
                if e.index == from_element:
                    from_elem = e
                if e.index == to_element:
                    to_elem = e
            if not from_elem or not to_elem:
                return ActionResult(ok=False, action="drag",
                                    message="from_element or to_element not found.")
            fx, fy = from_elem.center()
            tx, ty = to_elem.center()
        elif from_xy is not None and to_xy is not None:
            fx, fy = from_xy
            tx, ty = to_xy
        else:
            return ActionResult(ok=False, action="drag",
                                message="drag requires from_element/to_element or from_coordinate/to_coordinate.")

        # Build xdotool command
        btn_num = "1" if button == "left" else "2" if button == "middle" else "3"
        args = [
            "mousemove", "--sync", str(fx), str(fy),
            "mousedown", btn_num,
            "mousemove", "--sync", str(tx), str(ty),
            "mouseup", btn_num,
        ]
        
        if modifiers:
            # xdotool doesn't support modifiers during drag easily
            # We do the drag without modifiers for now
            pass
        
        ok, out, err = _run_xdotool(*args)
        if ok:
            return ActionResult(ok=True, action="drag",
                                message=f"Dragged from ({fx},{fy}) to ({tx},{ty})")
        return ActionResult(ok=False, action="drag", message=f"Drag failed: {err}")

    def scroll(
        self,
        *,
        direction: str,
        amount: int = 3,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        window_id = self._active_window_id
        if window_id is None:
            return ActionResult(ok=False, action="scroll",
                                message="No active window — call capture() first.")

        _focus_window(window_id)
        time.sleep(0.1)

        # Determine scroll position
        if element is not None:
            elements = _detect_interactable_elements(window_id)
            target_elem = None
            for e in elements:
                if e.index == element:
                    target_elem = e
                    break
            if target_elem:
                cx, cy = target_elem.center()
            else:
                return ActionResult(ok=False, action="scroll",
                                    message=f"Element #{element} not found.")
        elif x is not None and y is not None:
            cx, cy = x, y
        else:
            # Scroll at current mouse position
            cx = cy = None

        # Map direction to xdotool button
        # Button 4 = scroll up, 5 = down, 6 = left, 7 = right
        dir_map = {
            "up": "4",
            "down": "5",
            "left": "6",
            "right": "7",
        }
        btn = dir_map.get(direction, "5")
        
        args = []
        if cx is not None and cy is not None:
            args.extend(["mousemove", "--sync", str(cx), str(cy)])
        
        # xdotool click with repeat for scroll amount
        args.extend(["click", "--repeat", str(amount), btn])
        
        ok, out, err = _run_xdotool(*args)
        if ok:
            return ActionResult(ok=True, action="scroll",
                                message=f"Scrolled {direction} x{amount}")
        return ActionResult(ok=False, action="scroll", message=f"Scroll failed: {err}")

    # ── Keyboard ───────────────────────────────────────────────────
    def type_text(self, text: str) -> ActionResult:
        window_id = self._active_window_id
        if window_id is None:
            return ActionResult(ok=False, action="type_text",
                                message="No active window — call capture() first.")

        _focus_window(window_id)
        time.sleep(0.1)

        # Use xdotool type
        ok, out, err = _run_xdotool("type", "--delay", "10", text)
        if ok:
            return ActionResult(ok=True, action="type_text",
                                message=f"Typed {len(text)} characters")
        return ActionResult(ok=False, action="type_text", message=f"Type failed: {err}")

    def key(self, keys: str) -> ActionResult:
        window_id = self._active_window_id
        if window_id is None:
            return ActionResult(ok=False, action="key",
                                message="No active window — call capture() first.")

        _focus_window(window_id)
        time.sleep(0.1)

        key_name, modifiers = _parse_key_combo(keys)
        if not key_name:
            return ActionResult(ok=False, action="key",
                                message=f"Could not parse key from '{keys}'.")

        # Build xdotool key command
        if modifiers:
            combo = "+".join(modifiers + [key_name])
        else:
            combo = key_name
        
        ok, out, err = _run_xdotool("key", combo)
        if ok:
            return ActionResult(ok=True, action="key",
                                message=f"Pressed {combo}")
        return ActionResult(ok=False, action="key", message=f"Key failed: {err}")

    # ── Value setter ────────────────────────────────────────────────
    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        """Set a value on an element.
        
        On Linux, this is limited. We try to click the element and type the value.
        """
        window_id = self._active_window_id
        if window_id is None:
            return ActionResult(ok=False, action="set_value",
                                message="No active window — call capture() first.")
        if element is None:
            return ActionResult(ok=False, action="set_value",
                                message="set_value requires element= (element index).")

        # Click the element first
        click_res = self.click(element=element)
        if not click_res.ok:
            return ActionResult(ok=False, action="set_value",
                                message=f"Failed to click element #{element}: {click_res.message}")
        
        time.sleep(0.2)
        
        # Type the value
        type_res = self.type_text(value)
        if type_res.ok:
            return ActionResult(ok=True, action="set_value",
                                message=f"Set value '{value}' on element #{element}")
        return ActionResult(ok=False, action="set_value",
                            message=f"Failed to set value: {type_res.message}")

    # ── Introspection ──────────────────────────────────────────────
    def list_apps(self) -> List[Dict[str, Any]]:
        """List running applications with their PIDs and window counts."""
        windows = _list_windows()
        
        # Group by PID
        apps: Dict[int, Dict[str, Any]] = {}
        for w in windows:
            pid = w["pid"]
            if pid not in apps:
                apps[pid] = {
                    "name": w["app_name"] or w["class_name"] or w["title"],
                    "pid": pid,
                    "windows": 0,
                    "class_name": w["class_name"],
                }
            apps[pid]["windows"] += 1
        
        return list(apps.values())

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        """Target an app for subsequent actions.
        
        Finds the best matching window and sets it as active.
        """
        windows = _list_windows()
        app_lower = app.lower()
        
        matched = [
            w for w in windows
            if app_lower in w["app_name"].lower()
            or app_lower in w["class_name"].lower()
            or app_lower in w["title"].lower()
        ]
        
        target = matched[0] if matched else None
        if target:
            self._active_pid = target["pid"]
            self._active_window_id = target["window_id"]
            self._last_app = target["app_name"]
            
            if raise_window:
                _raise_window(target["window_id"])
            else:
                _focus_window(target["window_id"])
            
            return ActionResult(
                ok=True, action="focus_app",
                message=f"Targeted {target['app_name']} (pid {self._active_pid}, "
                        f"window {self._active_window_id})",
            )
        return ActionResult(ok=False, action="focus_app",
                            message=f"No window found for app '{app}'.")
