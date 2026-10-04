# Recording the demo

`docs/media/demo.gif` is recorded automatically:

```bash
pip install playwright pillow && python -m playwright install chromium
python scripts/record_demo.py docs/media/demo.gif "$(which patchahead)"
```

It starts the real demo server, warms it once, and follows the browser part of
the sequence below with a headless Chromium, keeping every frame as the page
showed it. Frames start just below the header bar, which shows where the demo
is installed -- a path from the recording machine -- and the script stops if
that bar is ever in frame. The rest of this page is for recording by hand,
with a terminal and narration.

## Recording by hand

A 45-second screen capture that a stranger can follow without reading anything
first. The still images in the README were captured from this same UI and can
stay; this is for the moving version.

Nothing here is staged. The commands below run the real engine against the
bundled repository, and what appears on screen is what the engine returned.

### Before you start

```bash
pip install 'patchahead[demo]'
patchahead demo --list            # sanity check: six scenarios, no errors
```

Then:

- **Terminal:** one window, no split panes, font ~16pt, a plain prompt
  (`PS1='$ '`). Clear it. Nothing else in the scrollback.
- **Browser:** one window at **1280×900**, no extensions visible, no bookmarks
  bar, no other tabs. Zoom at 100%.
- **Warm the cache once and discard that take.** The first migration in a fresh
  environment spends a second or two importing pytest, which reads as lag.
  Run the field-rename scenario once, then reload before recording.
- Record at 1280×900 or 1440×900. Anything wider makes the text unreadable once
  GitHub scales the GIF down.

### The sequence (45 seconds)

| Time | On screen | Why it is there |
|---|---|---|
| 0:00–0:05 | **Terminal.** Type `pip install 'patchahead[demo]'` — already satisfied, so it completes instantly — then `patchahead demo`. | The whole setup cost, shown rather than claimed. |
| 0:05–0:09 | The banner prints: the URL, the repository path, "the bundled repository is never modified", "localhost only". Pause on it. | Establishes the trust boundary before anything runs. |
| 0:09–0:14 | Switch to the browser at `http://127.0.0.1:8000`. Let the page settle. Do not scroll yet. | The header states the problem in one sentence; give it a beat to be read. |
| 0:14–0:18 | Move the cursor across the six scenario cards, slowly enough to read the expected outcomes: three *verified*, one *refused*, one *rejected*, one *patched, not verified*. | Four outcomes, visible before a single click. This is the honesty of the tool in one frame. |
| 0:18–0:20 | Click **A renamed field**, then **Run this scenario**. | — |
| 0:20–0:24 | It runs — a real `pytest` subprocess in a temporary copy. Stay on the status line (*Copying the repository, patching the copy, running its tests…*). | The pause is the product working, not a spinner. |
| 0:24–0:29 | The **Verified migration** card, marked with a green dot. Hold on it, including the line *"A test that failed before the patch passes after it."* | The claim, and the evidence for it, in the same frame. |
| 0:29–0:36 | Scroll steadily to **04 Patch**. Stop on the diff: two changed lines, and `TOTAL_LABEL = "total"` nowhere in it. Then continue to **05 Verification** and hold on the five passed checks. | Minimal diff, then the checks that earned the verdict. |
| 0:36–0:41 | Scroll back up and click **Same field name, different object** → **Run this scenario**. | The pivot. |
| 0:41–0:45 | The **Refused** card, marked with an orange dot, then the impact table: both sites found, graded `low`, decision **leave alone**, reason *"receiver is order, not the declared owner invoice"*. End here. | The differentiator: it found the code and chose not to touch it. |

### What to say, if there is narration

Four sentences, one per beat:

1. "An upstream API changed and some of this service no longer works."
2. "PatchAhead reads the release note, finds the call sites, and patches a copy
   of the repository — never the repository itself."
3. "It runs the tests there, and it only says *migrated* when a test that failed
   before the patch passes after it."
4. "And when the change is about a different object that happens to share a
   field name, it finds the code, explains it, and refuses."

### Things that ruin the take

- **Scrolling fast.** The page is dense; a viewer needs about a second per
  section. Slow, steady, no overshoot.
- **Starting from a cold environment.** See "warm the cache" above.
- **Recording the whole screen.** Capture the browser window only, plus the
  terminal for the first five seconds.
- **A port collision.** If 8000 is busy the demo moves up and the URL changes
  mid-take. Check with `patchahead demo --port 8000` first, or pass a port you
  know is free.
- **Editing the outcome.** If a scenario reports something other than what the
  card promised, that is a bug to file, not a take to cut around.

### Saving it

```bash
# Suggested: ≤ 10 MB, ~12 fps, 1280px wide. GitHub will not render a huge GIF.
ffmpeg -i demo.mov -vf "fps=12,scale=1280:-1:flags=lanczos" -loop 0 docs/media/demo.gif
```

Then replace the `<!-- DEMO RECORDING GOES HERE -->` comment near the top of
`README.md` with:

```markdown
![PatchAhead demo](docs/media/demo.gif)
```

A short MP4 is smaller and sharper than a GIF; GitHub renders one if you drag it
into a release or an issue and paste the resulting URL. The GIF is the version
that works inline in the README on every client, which is why it is the default.

## Regenerating the stills

The three PNGs in `docs/media/` are screenshots of this same UI, captured with a
headless browser rather than by hand. If the UI changes enough that they are
misleading, retake them the same way: run `patchahead demo --port 8731
--no-browser`, drive the page, and clip each region. They are documentation of
the current UI, so a stale one is a small bug, not a permanent fixture.
