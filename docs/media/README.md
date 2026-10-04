# Media

Images used by the top of `README.md`.

| File | What it is |
|---|---|
| `demo-verified.png` | The scenario picker and the *Verified migration* outcome, light theme. |
| `demo-gates.png` | The five validation gates for that run, light theme. |
| `demo-refusal.png` | The *Refused* outcome and the impact table behind it, dark theme. |
| `demo.gif` | The demo run end to end: the six scenarios, a verified migration and its gates, then a refusal. |

All of them are real screenshots of `patchahead demo`, captured from a
headless browser driving the running server — not mockups, and not composites.
Whatever the engine returned that run is what is in the frame. The GIF is made
by [`scripts/record_demo.py`](../../scripts/record_demo.py); its frames start
just below the page's header bar, which shows the local install path.

They are documentation of the current UI, so they go stale when it changes.
`../demo-recording.md` has the procedure for retaking them.
