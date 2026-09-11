"""Browser Harness MCP — one MCP server for your real browser.

Thin wrapper over the `browser_harness` daemon (CDP). Exposes core
browser tools plus a Gmail send flow (open login → compose → attach → send).

Run:
    python server.py            # stdio MCP server named "browser-harness"
"""
from __future__ import annotations

import functools
import json
import math
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from PIL import Image

try:
    from browser_harness.admin import ensure_daemon
    from browser_harness.helpers import (
        capture_screenshot,
        cdp,
        click_at_xy,
        close_tab,
        current_tab,
        ensure_real_tab,
        fill_input,
        goto_url,
        http_get,
        js,
        list_tabs,
        new_tab,
        page_info,
        press_key,
        scroll,
        switch_tab,
        type_text,
        upload_file,
        wait,
        wait_for_element,
        wait_for_load,
    )

    _BACKEND_ERROR = None
except ImportError:
    # Backend comes with this package in ONE install (`pip install -e .`
    # pulls `browser-harness` via pyproject deps). If it is missing the
    # install was skipped — every tool reports that instead of crashing.
    _BACKEND_ERROR = (
        "browser backend not installed. Run ONE command: "
        "pip install git+https://github.com/vaibhxvvy/browser-harness-mcp"
    )

SERVER = MCPServer("browser-harness")


def _normalize(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_normalize(v) for v in value]
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _dump(value: Any) -> str:
    return json.dumps(
        _normalize(value), ensure_ascii=False, allow_nan=False, default=_json_default
    )


@contextmanager
def _stderr_stdout():
    """Keep MCP stdio clean: helpers may print, redirect stdout to stderr."""
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = saved


def _tool(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            if _BACKEND_ERROR:
                return _dump({"error": _BACKEND_ERROR})
            ensure_daemon()
            with _stderr_stdout():
                result = fn(*args, **kwargs)
            return _dump(result)
        except Exception as exc:  # MCP tools must serialize browser failures
            return _dump({"error": str(exc)})

    return SERVER.tool(name=fn.__name__, description=fn.__doc__ or "")(wrapper)


# --- core browser tools ---


@_tool
def browser_new_tab(url: str = "about:blank"):
    """Open a new browser tab. Returns the new tab's targetId."""
    return {"targetId": new_tab(url)}


@_tool
def browser_goto(url: str):
    """Navigate the current tab to `url`."""
    return goto_url(url)


@_tool
def browser_page_info():
    """Return current tab metadata: url, title, viewport and scroll sizes."""
    return page_info()


@_tool
def browser_click(x: int, y: int, button: str = "left", clicks: int = 1):
    """Click at screen coordinates (x, y). `button` is left/right/middle."""
    click_at_xy(x, y, button=button, clicks=clicks)
    return {"ok": True}


@_tool
def browser_type(text: str):
    """Insert text into the focused element."""
    type_text(text)
    return {"ok": True}


@_tool
def browser_fill(selector: str, text: str, clear_first: bool = True):
    """Fill an input matched by `selector` with `text`."""
    fill_input(selector, text, clear_first=clear_first)
    return {"ok": True}


@_tool
def browser_press(key: str, modifiers: int = 0):
    """Press a key. modifiers bitfield: 1=Alt, 2=Ctrl, 4=Meta, 8=Shift."""
    press_key(key, modifiers=modifiers)
    return {"ok": True}


@_tool
def browser_scroll(x: int, y: int, dy: int = -300, dx: int = 0):
    """Scroll the wheel at (x, y) by dy vertical / dx horizontal pixels."""
    scroll(x, y, dy=dy, dx=dx)
    return {"ok": True}


@_tool
def browser_screenshot(path: str | None = None, full: bool = False):
    """Capture a PNG screenshot. If `path` is omitted, a temp file is used."""
    path = capture_screenshot(path=path, full=full)
    width, height = Image.open(path).size
    return {"path": path, "width": width, "height": height,
            "size_bytes": os.path.getsize(path)}


@_tool
def browser_list_tabs():
    """List open page tabs."""
    return list_tabs()


@_tool
def browser_current_tab():
    """Return the active tab's targetId, url and title."""
    return current_tab()


@_tool
def browser_switch_tab(target: str):
    """Switch to tab by targetId or URL substring. Returns the sessionId."""
    return {"sessionId": switch_tab(target)}


@_tool
def browser_close_tab(target: str | None = None):
    """Close a tab. Without `target`, closes the active tab."""
    close_tab(target)
    return {"ok": True}


@_tool
def browser_ensure_real_tab():
    """Switch to a real (non-internal) tab if current one is chrome:// or stale."""
    return ensure_real_tab()


@_tool
def browser_doctor():
    """Check daemon + browser connection. Run this first; no other CLI needed."""
    info = page_info()
    return {"daemon": "up", "url": info.get("url"), "title": info.get("title")}


@_tool
def browser_wait(seconds: float = 1.0):
    """Wait for `seconds`."""
    wait(seconds)
    return {"ok": True}


@_tool
def browser_wait_for_load(timeout: float = 15.0):
    """Wait until the current tab's readyState is complete."""
    return {"ok": wait_for_load(timeout=timeout)}


@_tool
def browser_wait_for_element(selector: str, timeout: float = 10.0):
    """Wait for an element matching `selector` to appear."""
    return {"ok": wait_for_element(selector, timeout=timeout)}


@_tool
def browser_js(expression: str):
    """Evaluate a JavaScript expression in the current tab."""
    return js(expression)


@_tool
def browser_cdp(method: str, params: dict | None = None):
    """Call a raw CDP method. `params` are passed as kwargs."""
    return cdp(method, **(params or {}))


@_tool
def browser_upload_file(selector: str, path: str):
    """Set files on a file input matched by `selector`."""
    upload_file(selector, path)
    return {"ok": True}


@_tool
def browser_http_get(url: str, timeout: float = 20.0):
    """HTTP GET `url` without the browser. Returns the response body."""
    return {"text": http_get(url, timeout=timeout)}


# --- Gmail flow (ported from job/harness_*.py) ---


def _ax_nodes():
    return cdp("Accessibility.getFullAXTree").get("nodes", [])


def _click_node_by_name(name: str) -> dict:
    for n in _ax_nodes():
        n_name = n.get("name", {}).get("value", "")
        role = n.get("role", {}).get("value", "")
        if n_name == name and role == "button":
            bid = n.get("backendDOMNodeId")
            model = cdp("DOM.getBoxModel", backendNodeId=bid)
            q = model["model"]["content"]
            x, y = sum(q[0::2]) / 4, sum(q[1::2]) / 4
            click_at_xy(int(x), int(y))
            return {"ok": True, "x": int(x), "y": int(y)}
    # JS fallback for Gmail's div[role=button] Send
    res = js("() => { const b = Array.from(document.querySelectorAll"
            "('div[role=button]')).find(e => e.textContent.trim() === '"
            + name + "'); if (b) { b.click(); return 'clicked'; }"
            " return 'not found'; }")
    return {"ok": res == "clicked", "fallback": res}


@_tool
def gmail_open_login():
    """Open Gmail login in a new tab. User completes sign-in manually."""
    new_tab("https://mail.google.com/")
    wait(4)
    try:
        wait_for_load()
    except Exception:
        pass
    return page_info()


@_tool
def gmail_compose(compose_url: str):
    """Open a Gmail compose URL (prefilled to/subject/body). Returns page info."""
    new_tab(compose_url)
    wait(6)
    try:
        wait_for_load()
    except Exception:
        pass
    return page_info()


@_tool
def gmail_attach(pdf_path: str):
    """Attach a PDF via Gmail's file input. Returns whether DOM shows it."""
    for selector in ("input[type=file]", "input"):
        try:
            upload_file(selector, pdf_path)
            break
        except Exception:
            continue
    wait(3)
    name = Path(pdf_path).name
    found = js("() => document.documentElement.innerHTML.includes("
               + json.dumps(name) + " )")
    return {"attached": bool(found), "file": name}


@_tool
def gmail_click_send():
    """Click Gmail's Send button. Returns click coords + sent confirmation."""
    clicked = _click_node_by_name("Send")
    wait(5)
    try:
        body = js("() => document.body.innerText.slice(0, 5000)") or ""
    except Exception:
        body = ""
    clicked["sent"] = "Message sent" in body
    return clicked


@_tool
def gmail_check_sent(recipient: str, subject: str = ""):
    """Open Sent Mail and check recipient (and optional subject) appears."""
    new_tab("https://mail.google.com/mail/u/0/#sent")
    wait(6)
    try:
        wait_for_load()
    except Exception:
        pass
    wait(4)
    try:
        text = js("() => document.body.innerText.slice(0, 8000)") or ""
    except Exception:
        text = ""
    return {
        "recipient_found": recipient in text,
        "subject_found": bool(subject) and subject in text,
        "snippet": text[:1000],
    }


def main() -> None:
    """Run the browser-harness MCP server over stdio."""
    SERVER.run()


if __name__ == "__main__":
    main()
