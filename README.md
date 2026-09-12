# browser-harness MCP

One MCP server named `browser-harness` for your real browser — plus a Gmail send flow.

- Core browser control over CDP (`new_tab`, `page_info`, `cdp`, `js`, …) via the `browser-harness` daemon — no second CDP layer.
- Gmail flow: open login → compose → attach PDF → send → verify in Sent.

Single file: `server.py`. Stdio transport.

## Layout

```text
browser-harness-mcp/
  server.py        # the MCP server (37 tools)
  SKILL.md         # agent workflow (single-tab default, memory, plans)
  pyproject.toml   # package + deps (backend included)
  README.md        # this file
  LICENSE        # MIT
```

## Install — ONE command

```powershell
pip install git+https://github.com/vaibhxvvy/browser-harness-mcp
```

That's it. The browser backend (`browser-harness`, `mcp`, `pillow`) installs automatically in the same command — there is nothing else to install, no second MCP, no separate doctor CLI. Then check health from inside any MCP client with the `browser_doctor` tool.

From a local clone (contributors):

```powershell
pip install -e .
```

Needs Chrome/Brave/Edge with remote debugging (port 9222). The daemon auto-starts on first tool call.

## MCP config — ONE entry

After the install command (no python path needed):

```json
{
  "mcpServers": {
    "browser-harness": { "command": "browser-harness-mcp" }
  }
}
```

Local-dev alternative (run from source without installing):

```json
{
  "mcpServers": {
    "browser-harness": {
      "command": "python",
      "args": ["<clone-path>/browser-harness-mcp/server.py"]
    }
  }
}
```

Only ONE `mcpServers` entry — this server is the whole thing.

## Tools

Core browser (32):

| Tool | What it does |
|---|---|
| `browser_new_tab` | Open tab, returns targetId |
| `browser_goto` | Navigate current tab |
| `browser_page_info` | url, title, viewport |
| `browser_snapshot` | Labeled interactives + coords, one call |
| `browser_click` | Click at x, y |
| `browser_click_text` | Click button/link by visible label (no coords) |
| `browser_type` | Type into focused element |
| `browser_fill` | Fill input by CSS selector |
| `browser_press` | Press key (+ modifiers) |
| `browser_scroll` | Wheel scroll at x, y |
| `browser_screenshot` | PNG path + size AND the image itself (no key) |
| `browser_list_tabs` | List tabs |
| `browser_current_tab` | Active tab info |
| `browser_switch_tab` | Switch by id / URL substring |
| `browser_close_tab` | Close tab |
| `browser_task_tabs` | EXPLICIT multi-tab entry (parallel tasks only) |
| `browser_ensure_real_tab` | Escape chrome:// pages |
| `browser_wait` | Sleep seconds |
| `browser_wait_for_load` | Wait for readyState complete |
| `browser_wait_for_element` | Wait for CSS selector |
| `browser_wait_for_text` | Wait for text in page body |
| `browser_js` | Run JS, return value (arrows auto-invoked) |
| `browser_cdp` | Raw CDP call |
| `browser_upload_file` | Set file input |
| `browser_http_get` | Browser-less GET |
| `browser_start_recording` | Record actions to a directory |
| `browser_stop_recording` | Stop recording, return directory |
| `browser_doctor` | Health check (daemon + browser, replaces any doctor CLI) |
| `browser_note` / `browser_recall` | Cross-session memory (`~/.browser-harness-mcp/memory.jsonl`) |
| `browser_achieve` | Supervised multi-step plans (verify + retry, write gate) |
| `browser_see` | Screenshot + question for the driving model (key only for text-only drivers) |

Gmail flow (5, all in the current tab):

| Tool | What it does |
|---|---|
| `gmail_open_login` | Open Gmail login (user signs in once) |
| `gmail_compose` | Open a prefilled compose URL |
| `gmail_attach` | Attach a PDF, verify it stuck |
| `gmail_click_send` | Preview draft, or send with `confirm=True` |
| `gmail_check_sent` | Verify recipient/subject in Sent Mail |

Every tool returns JSON text. Failures return `{"error": "..."}` — never raises.

## Parallel tabs (explicit only)

Default is ONE tab: single-tab tasks must never open extra tabs — navigate the current tab.

Only when the request is genuinely multi-task ("do these things at the same time"):

```text
browser_task_tabs(urls=[...]) → [{index, targetId, url}]
browser_switch_tab(target_A) → act on A → browser_switch_tab(target_B) → act on B
```

Every tool call runs atomically under a server-side lock, so switched work can't interleave mid-flight. Execution across tabs is sequential, not simultaneous — the backend holds a single session, and true concurrency would land actions in wrong tabs.

## Usage (Gmail end-to-end)

```text
gmail_open_login → sign in once in the debug Chrome window
gmail_compose(compose_url="https://mail.google.com/mail/?view=cm&to=someone@example.com")
gmail_attach(pdf_path="/path/to/resume.pdf")
gmail_click_send() → {"sent": false, "needs_confirm": true, "preview": {...}}
gmail_click_send(confirm=True) → {"sent": true}
gmail_check_sent(recipient="someone@example.com", subject="Application for Prompt Engineer")
```

## Usage (low-level)

```text
browser_new_tab("https://mail.google.com/")
browser_page_info()
browser_js("() => document.title")
browser_screenshot()
```

## Notes

- Backend (`browser_harness` daemon) is a declared dependency — the ONE install command pulls it, no separate install.
- Dead browser fails fast (~1s) instead of hanging 30s.
- Vision is native: screenshot/see return image blocks the driving model
  sees directly (~1–2k tokens a shot, capped at 1280px). `BH_VISION_API_KEY`
  is only a fallback for text-only drivers.
- Tool calls are lock-atomic: explicit multi-tab work can't interleave.
- Read-only tools retry once on transient IPC blips; clicking/typing/sending never retry (no double side effects).
- `gmail_click_send` sends nothing without `confirm=True` — first call returns a draft preview.
- This package owns the tab marker (🥸, applied on tab-attach tools, legacy horse stripped once). Backend marking is disabled via `BH_TAB_MARKER=0`; reload an old daemon once to pick that up.
- If the backend is ever missing, tools return `{"error": "...run ONE command..."}` instead of crashing.
- stdout is redirected to stderr inside tools so MCP stdio never corrupts.
- NaN/Infinity/bytes/dates are normalized before JSON serialization.
- Gmail Send clicks the AX `Send` button, falls back to `div[role=button]` JS click.

## License

MIT — see [LICENSE](LICENSE).
