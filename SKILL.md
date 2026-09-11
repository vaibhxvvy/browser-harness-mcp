# browser-harness skill

Drive the user's real browser through the `browser-harness` MCP server.
One tab, one flow, verify everything.

## Default workflow (single tab)

1. `browser_doctor` — if it errors, the browser isn't up; stop and say so.
2. `browser_snapshot` — recon the page first (labels + coords in one call).
3. Act with `browser_goto` / `browser_click_text` / `browser_fill` / `browser_js`.
4. Verify with `browser_wait_for_text` or `browser_page_info` — never assume.

## Rules

- ONE tab unless the request is explicitly multi-task ("do these at the same
  time"). Then: `browser_task_tabs(urls=[...])`, and `browser_switch_tab`
  explicitly before every act.
- Never open extra tabs for a single-tab task. Never use `browser_new_tab`
  when `browser_goto` on the current tab will do.
- `gmail_click_send` sends NOTHING without `confirm=True`. First call returns
  a draft preview — show it, then ask.
- Prefer `browser_wait_for_text` / `browser_wait_for_element` over `browser_wait`.
- `browser_js` accepts direct expressions or arrow functions (auto-invoked).
- Coordinates from `browser_snapshot` are viewport pixels for `browser_click`.

## Memory (the part that compounds)

- After anything reusable works, `browser_note(site, fact)` it
  (e.g. "gmail Send button AX name contains Ctrl-Enter").
- Before starting on a known site, `browser_recall(site)` first.

## Multi-step flows

Bundle declared plans into one `browser_achieve(goal, steps)` call instead of
N roundtrips. Steps: goto, js, click_text, fill, press, wait_for_text,
wait_for_element, wait, snapshot, note. Add `expect` text per step for
verification; writes need `confirm_writes=True`. Planning stays with you —
achieve supervises, it doesn't invent.

## Vision

`browser_see(question)` answers visual questions ("what does the blue button
say?"). Needs `BH_VISION_API_KEY` (plus optional `BH_VISION_MODEL` /
`BH_VISION_BASE_URL`); without a key it errors — say so and continue blind.
