"""Browser Harness MCP — one MCP server for your real browser.

Thin wrapper over the `browser_harness` daemon (CDP). Exposes core
browser tools, page snapshots, cross-session memory, supervised
multi-step plans, vision Q&A, and a Gmail send flow.

Run:
    python server.py            # stdio MCP server named "browser-harness"
"""
from __future__ import annotations

import base64
import functools
import json
import math
import os
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# Windows consoles default to cp1252, which cannot encode the emoji markers
# the backend prepends to tab titles — force UTF-8 so printing tool results
# never raises UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try: _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass

from mcp.server import MCPServer
from PIL import Image

# We own the tab marker (🥸, applied by _mark_disguise on tab-attaching tools).
# The backend's horse marker stays off so the two never stack. The daemon
# inherits this on fresh spawns; reload it once if an old daemon still marks.
os.environ.setdefault("BH_TAB_MARKER", "0")

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
        start_recording,
        stop_recording,
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
    if isinstance(value, str):
        # Marker swap: show 🥸 everywhere, even against an upstream backend
        # that still prepends 🐴.
        return value.replace("\U0001F434", "\U0001F978")
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


def _poll_js_text(expression: str, needle: str, timeout: float = 12.0):
    """Poll a JS text expression until `needle` appears. Returns (found, last_text).

    Replaces fixed sleeps: returns the moment the condition holds instead of
    always waiting out the full timeout. Transient JS failures are ignored so
    one bad poll doesn't fail the whole wait.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            last = js(expression) or ""
        except Exception:
            pass
        if needle in last:
            return True, last
        wait(0.5)
    return False, last


def _local_endpoint_alive(timeout: float = 1.5) -> bool:
    """True if a local CDP endpoint answers. Fails fast so a dead browser
    reports in ~1s instead of burning ensure_daemon's 30s timeout."""
    if os.environ.get("BU_CDP_URL") or os.environ.get("BU_CDP_WS"):
        return True  # remote endpoint: let ensure_daemon judge it
    for port in (9222, 9223):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=timeout
            ) as response:
                version = json.loads(response.read())
            if isinstance(version, dict) and version.get("webSocketDebuggerUrl"):
                return True
        except Exception:
            pass
    return False


def _mark_disguise():
    """Prepend 🥸 to the current tab title (backend marking stays off).

    Also strips a legacy horse prefix once, so tabs marked by older daemons
    migrate cleanly instead of stacking markers.
    """
    try:
        cdp("Runtime.evaluate", expression=(
            "if(document.title.startsWith('\U0001F434 '))document.title=document.title.slice(3);"
            "if(!document.title.startsWith('\U0001F978'))document.title='\U0001F978 '+document.title"
        ))
    except Exception:
        pass


@contextmanager
def _stderr_stdout():
    """Keep MCP stdio clean: helpers may print, redirect stdout to stderr."""
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = saved


# Read-only tools: safe to retry once after a transient IPC blip.
# Anything that clicks, types, fills, uploads, or sends is NOT here —
# retrying those could double-apply a side effect.
_READ_ONLY_TOOLS = frozenset({
    "browser_page_info",
    "browser_js",
    "browser_cdp",
    "browser_list_tabs",
    "browser_current_tab",
    "browser_wait_for_load",
    "browser_wait_for_element",
    "browser_wait_for_text",
})

_NO_BROWSER = (
    "no browser with CDP on 127.0.0.1:9222/9223 — launch Brave/Chrome with "
    "--remote-debugging-port=9222 and retry"
)


# One global lock: the backend holds a single current session, so two
# interleaved tool calls could act on the wrong tab. Every tool runs
# atomically under this lock — explicit multi-tab workflows stay deterministic.
_LOCK = threading.Lock()


def _tool(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if _BACKEND_ERROR:
            return _dump({"error": _BACKEND_ERROR})
        if not _local_endpoint_alive():
            return _dump({"error": _NO_BROWSER})
        attempts = 2 if fn.__name__ in _READ_ONLY_TOOLS else 1
        with _LOCK:
            for attempt in range(attempts):
                try:
                    ensure_daemon()
                    with _stderr_stdout():
                        result = fn(*args, **kwargs)
                    return _dump(result)
                except Exception as exc:  # MCP tools must serialize browser failures
                    if attempt + 1 >= attempts:
                        return _dump({"error": str(exc)})
                    wait(1.0)
        return _dump({"error": "unreachable"})  # pragma: no cover

    return SERVER.tool(name=fn.__name__, description=fn.__doc__ or "")(wrapper)


# --- core browser tools ---


@_tool
def browser_new_tab(url: str = "about:blank"):
    """Open a new browser tab. Returns the new tab's targetId."""
    target = new_tab(url)
    _mark_disguise()
    return {"targetId": target}


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


def _click_text_impl(text: str) -> dict:
    """Click the first button/link whose label contains `text`. Raw helper —
    shared by the tool and the plan runner (which must not re-enter the lock)."""
    for n in _ax_nodes():
        name = (n.get("name") or {}).get("value", "") or ""
        role = (n.get("role") or {}).get("value", "")
        if role in ("button", "link") and text in name:
            bid = n.get("backendDOMNodeId")
            if not bid:
                continue
            try:
                model = cdp("DOM.getBoxModel", backendNodeId=bid)
            except Exception:
                continue
            q = model["model"]["content"]
            x, y = int(sum(q[0::2]) / 4), int(sum(q[1::2]) / 4)
            click_at_xy(x, y)
            return {"ok": True, "x": x, "y": y, "matched": name[:100]}
    res = js("(function(){var els=document.querySelectorAll("
             "'button,a,[role=button]');for(var i=0;i<els.length;i++){"
             "if((els[i].innerText||'').indexOf(" + json.dumps(text) + ")!==-1)"
             "{els[i].click();return 'clicked';}}return 'not found';})()")
    return {"ok": res == "clicked", "fallback": res}


@_tool
def browser_click_text(text: str):
    """Click the first button or link whose visible label contains `text`.

    No coordinates needed — resolves via the accessibility tree, with a
    DOM-text fallback. Exact-action tools (browser_click) still exist for
    pixel-precise cases.
    """
    return _click_text_impl(text)


def _snapshot_impl(limit: int):
    """Page interactives with viewport coords. Exactly one CDP roundtrip."""
    found = js("(function(){var out=[];var els=document.querySelectorAll("
        "'button,a,input,select,textarea,[role=button],[role=link],[role=textbox]');"
        "for(var i=0;i<els.length;i++){var e=els[i];var r=e.getBoundingClientRect();"
        "if(r.width<2||r.height<2)continue;"
        "var l=(e.getAttribute('aria-label')||e.innerText||e.value||e.placeholder||e.name||'')"
        ".replace(/\\s+/g,' ').trim().slice(0,80);"
        "out.push({tag:e.tagName.toLowerCase(),role:e.getAttribute('role')||'',label:l,"
        "x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)});}"
        "return out;})()")
    return (found or [])[:max(1, limit)]


@_tool
def browser_snapshot(limit: int = 100):
    """One-call page context: interactive elements with viewport coords.

    Returns [{tag, role, label, x, y}]. Recon with this first instead of
    N probes; feed x/y straight into browser_click.
    """
    return _snapshot_impl(limit)


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
def browser_screenshot(path: str | None = None, full: bool = False,
                       max_dim: int | None = None):
    """Capture a PNG screenshot. If `path` is omitted, a temp file is used.
    Set `max_dim` to downscale results larger than that dimension."""
    path = capture_screenshot(path=path, full=full, max_dim=max_dim)
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
    session = switch_tab(target)
    _mark_disguise()
    return {"sessionId": session}


@_tool
def browser_close_tab(target: str | None = None):
    """Close a tab. Without `target`, closes the active tab."""
    close_tab(target)
    return {"ok": True}


@_tool
def browser_task_tabs(urls: list[str]):
    """EXPLICIT multi-tab entry point. Open one tab per URL for genuinely
    parallel tasks ("do these N things at the same time").

    Rule: single-tab tasks must NOT use this — stay on the current tab.
    Multi-tab drivers must switch explicitly (browser_switch_tab) before
    every act; calls are lock-atomic so switched work can't interleave.
    Returns [{index, targetId, url}] in input order.
    """
    tabs = []
    for i, url in enumerate(urls):
        target = new_tab(url)
        tabs.append({"index": i, "targetId": target, "url": url})
    _mark_disguise()
    return tabs


@_tool
def browser_ensure_real_tab():
    """Switch to a real (non-internal) tab if current one is chrome:// or stale."""
    result = ensure_real_tab()
    _mark_disguise()
    return result


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
def browser_wait_for_text(text: str, timeout: float = 15.0):
    """Wait until `text` appears in the current tab body. Returns match + snippet."""
    found, body = _poll_js_text("document.body.innerText.slice(0, 8000)", text, timeout=timeout)
    i = body.find(text)
    return {"found": found, "snippet": body[max(0, i - 200):i + 500] if found else body[:500]}


@_tool
def browser_js(expression: str):
    """Evaluate a JavaScript expression in the current tab.

    Bare arrow functions are auto-invoked, so both `document.title` and
    `() => document.title` work.
    """
    expr = expression.strip()
    if expr.startswith("() =>") or expr.startswith("async () =>"):
        expr = "(" + expr + ")()"
    return js(expr)


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


@_tool
def browser_start_recording(name: str | None = None, title: str | None = None):
    """Start recording actions to a local directory."""
    return {"recording_dir": start_recording(name=name, title=title)}


@_tool
def browser_stop_recording():
    """Stop the active recording and return its directory."""
    return {"recording_dir": stop_recording()}


_MEMORY_FILE = Path.home() / ".browser-harness-mcp" / "memory.jsonl"


def _read_memory(limit: int = 200):
    try:
        lines = _MEMORY_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _save_note(site: str, fact: str):
    _MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _MEMORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.time(), "site": site, "fact": fact},
                                ensure_ascii=False) + "\n")


@_tool
def browser_note(site: str, fact: str):
    """Save a fact about a site/task for future sessions.

    The harness never learns across restarts on its own — write down what
    worked ("gmail Send button AX name contains Ctrl-Enter") and recall it
    next time via browser_recall. This file is the self-improvement loop.
    """
    _save_note(site, fact)
    return {"ok": True}


@_tool
def browser_recall(site: str = ""):
    """Recall saved facts. Substring-matches `site`; empty returns recent."""
    query = site.strip().lower()
    mem = _read_memory()
    if query:
        mem = [m for m in mem if query in str(m.get("site", "")).lower()]
    return mem[-50:]


_BODY_SLICE = "document.body.innerText.slice(0, 8000)"

_PLAN_ACTIONS = frozenset({
    "goto", "js", "click_text", "fill", "press",
    "wait_for_text", "wait_for_element", "wait", "snapshot", "note",
})
_PLAN_WRITES = frozenset({"click_text", "fill", "press"})


def _validate_plan(steps, confirm_writes, max_steps):
    """Pure plan check. Returns (ok, error, plan). Never touches the browser."""
    if not isinstance(steps, list) or not steps:
        return False, "steps must be a non-empty list", []
    plan = steps[:max(1, int(max_steps))]
    warning = "truncated to max_steps" if len(steps) > len(plan) else ""
    for i, step in enumerate(plan):
        if not isinstance(step, dict):
            return False, f"step {i} must be an object", []
        action = step.get("action", "")
        if action not in _PLAN_ACTIONS:
            return False, f"step {i}: unknown action {action!r}", []
        if action in _PLAN_WRITES and not confirm_writes:
            return False, f"step {i} ({action}) writes: pass confirm_writes=True", []
        if not isinstance(step.get("args", {}), dict):
            return False, f"step {i}: args must be an object", []
    return True, warning, plan


def _run_plan_step(action, args):
    """Run one plan step with raw helpers (never wrapped tools: no lock nesting)."""
    if action == "goto":
        goto_url(args.get("url", "about:blank"))
        return True, "navigated"
    if action == "js":
        return True, str(js(args.get("expression", "document.title")))[:300]
    if action == "click_text":
        result = _click_text_impl(args.get("text", ""))
        return bool(result.get("ok")), str(result)[:300]
    if action == "fill":
        fill_input(args.get("selector", ""), args.get("text", ""),
                   clear_first=bool(args.get("clear_first", True)))
        return True, "filled"
    if action == "press":
        press_key(args.get("key", "Enter"), modifiers=int(args.get("modifiers", 0)))
        return True, "pressed"
    if action == "wait_for_text":
        found, body = _poll_js_text(_BODY_SLICE, args.get("text", ""),
                                    timeout=float(args.get("timeout", 10.0)))
        return found, ("matched" if found else "timeout: " + body[:200])
    if action == "wait_for_element":
        ok = wait_for_element(args.get("selector", ""),
                              timeout=float(args.get("timeout", 10.0)))
        return ok, "present" if ok else "timeout"
    if action == "wait":
        wait(float(args.get("seconds", 1.0)))
        return True, "waited"
    if action == "snapshot":
        return True, f"{len(_snapshot_impl(int(args.get('limit', 50))))} elements"
    if action == "note":
        _save_note(args.get("site", ""), args.get("fact", ""))
        return True, "noted"
    return False, "unreachable"


@_tool
def browser_achieve(goal: str, steps: list, confirm_writes: bool = False,
                    max_steps: int = 20):
    """Run a declared multi-step plan server-side, with verify + retry per step.

    Planning stays with the calling agent — `steps` is explicit:
    [{action, args?, expect?, expect_timeout?}]. Actions: goto, js,
    click_text, fill, press, wait_for_text, wait_for_element, wait,
    snapshot, note. Each step runs; `expect` text is verified and failed
    steps retry once before the run aborts. Write actions
    (click_text/fill/press) need confirm_writes=True.
    Returns {goal, ok, transcript:[{step, action, ok, detail}]}.
    """
    ok, error, plan = _validate_plan(steps, confirm_writes, max_steps)
    if not ok:
        return {"goal": goal, "ok": False, "error": error, "transcript": []}
    transcript = []
    run_ok = True
    for i, step in enumerate(plan):
        action, args = step.get("action", ""), step.get("args", {}) or {}
        expect = step.get("expect", "")
        done, detail = False, ""
        for _ in range(2):
            done, detail = _run_plan_step(action, args)
            if done and expect:
                found, _ = _poll_js_text(_BODY_SLICE, expect,
                                         timeout=float(step.get("expect_timeout", 8.0)))
                done = found
                detail = "verified" if found else f"expect missing: {expect[:100]}"
            if done:
                break
            wait(1.0)
        transcript.append({"step": i, "action": action, "ok": done,
                           "detail": detail[:300]})
        if not done:
            run_ok = False
            break
    out = {"goal": goal, "ok": run_ok, "transcript": transcript}
    if error:
        out["warning"] = error
    return out


def _vision_ask(question: str, image_b64: str) -> str:
    """Ask an OpenAI-compatible vision API about a PNG. Raises without a key."""
    key = os.environ.get("BH_VISION_API_KEY", "")
    if not key:
        raise RuntimeError("no vision key: set BH_VISION_API_KEY "
                           "(optional: BH_VISION_MODEL, BH_VISION_BASE_URL)")
    model = os.environ.get("BH_VISION_MODEL", "gpt-4o-mini")
    base = os.environ.get("BH_VISION_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = json.dumps({
        "model": model,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_b64}},
        ]}],
    }).encode()
    request = urllib.request.Request(
        base + "/chat/completions", data=payload,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read())
    return data["choices"][0]["message"]["content"]


@_tool
def browser_see(question: str):
    """Ask a vision model about the current tab ("what does the blue button say?").

    Screenshots the tab and sends it to an OpenAI-compatible chat API.
    Needs BH_VISION_API_KEY in the environment (BH_VISION_MODEL defaults to
    gpt-4o-mini, BH_VISION_BASE_URL to https://api.openai.com/v1).
    """
    shot = capture_screenshot(path=None, full=False, max_dim=1280)
    answer = _vision_ask(question, base64.b64encode(Path(shot).read_bytes()).decode())
    return {"answer": answer}


# --- Gmail flow (ported from job/harness_*.py) ---


def _ax_nodes():
    return cdp("Accessibility.getFullAXTree").get("nodes", [])


def _click_node_by_name(name: str) -> dict:
    found = None
    for n in _ax_nodes():
        n_name = n.get("name", {}).get("value", "")
        role = n.get("role", {}).get("value", "")
        if role == "button" and name in n_name:
            found = n
            if "Ctrl-Enter" in n_name:
                break
    if found is not None:
        bid = found.get("backendDOMNodeId")
        model = cdp("DOM.getBoxModel", backendNodeId=bid)
        q = model["model"]["content"]
        x, y = sum(q[0::2]) / 4, sum(q[1::2]) / 4
        click_at_xy(int(x), int(y))
        return {"ok": True, "x": int(x), "y": int(y)}
    # JS fallback for Gmail's div[role=button] Send. Must be an IIFE —
    # the backend returns {} for a bare arrow function instead of invoking it.
    res = js("(() => { const b = Array.from(document.querySelectorAll"
             "('div[role=button]')).find(e => e.textContent.trim() === '"
             + name + "'); if (b) { b.click(); return 'clicked'; }"
             " return 'not found'; })()")
    return {"ok": res == "clicked", "fallback": res}


@_tool
def gmail_open_login():
    """Open Gmail login in the current tab. User completes sign-in manually."""
    goto_url("https://mail.google.com/")
    wait(4)
    try:
        wait_for_load()
    except Exception:
        pass
    _mark_disguise()
    return page_info()


@_tool
def gmail_compose(compose_url: str):
    """Open a Gmail compose URL (prefilled to/subject/body) in the current tab."""
    goto_url(compose_url)
    # Poll for the compose box instead of a fixed sleep — usually ready in 1-3s.
    wait_for_element("input[name=subjectbox]", timeout=15.0)
    _mark_disguise()
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
    probe = "document.documentElement.innerHTML.includes(" + json.dumps(name) + ") ? 'YES' : 'NO'"
    found, _ = _poll_js_text(probe, "YES", timeout=10.0)
    return {"attached": found, "file": name}


def _compose_preview():
    """Best-effort snapshot of the open compose draft. Never raises."""
    def _get(expression):
        try:
            return js(expression) or ""
        except Exception:
            return ""
    subject = _get("document.querySelector('input[name=subjectbox]') ? "
                   "document.querySelector('input[name=subjectbox]').value : ''")
    body = _get("(function(){var el=document.querySelector('div[role=textbox]');"
                "return el?el.innerText.slice(0,300):'';})()")
    return {"subject": subject[:200], "body": body[:300]}


@_tool
def gmail_click_send(confirm: bool = False):
    """Click Gmail's Send button. Returns click coords + sent confirmation.

    Safety gate: without confirm=True this sends nothing and returns a
    preview of the pending draft instead. Pass confirm=True to send.
    """
    if not confirm:
        return {"sent": False, "needs_confirm": True, "preview": _compose_preview()}
    clicked = _click_node_by_name("Send")
    # Poll for the banner instead of one check after a fixed sleep.
    found, _ = _poll_js_text("document.body.innerText.slice(0, 5000)", "Message sent", timeout=12.0)
    clicked["sent"] = found
    return clicked


@_tool
def gmail_check_sent(recipient: str, subject: str = ""):
    """Open Sent Mail in the current tab; check recipient (and subject)."""
    goto_url("https://mail.google.com/mail/u/0/#sent")
    try:
        wait_for_load(timeout=15)
    except Exception:
        pass
    # Poll for the address instead of fixed sleeps — Sent indexes at its own pace.
    found, text = _poll_js_text("document.body.innerText.slice(0, 8000)", recipient, timeout=15.0)
    _mark_disguise()
    return {
        "recipient_found": found,
        "subject_found": bool(subject) and subject in text,
        "snippet": text[:1000],
    }


def main() -> None:
    """Run the browser-harness MCP server over stdio."""
    SERVER.run()


if __name__ == "__main__":
    main()
