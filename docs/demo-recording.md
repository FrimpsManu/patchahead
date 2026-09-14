# Recording the demo

A 45-second screen capture that a stranger can follow without reading anything
first. The still images in the README were captured from this same UI and can
stay; this is for the moving version.

Nothing here is staged. The commands below run the real engine against the
bundled repository, and what appears on screen is what the engine returned.

## Before you start

```bash
pip install -e '.[demo]'          # PatchAhead is not on PyPI yet; install from the clone
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

## The sequence (45 seconds)

| Time | On screen | Why it is there |
|---|---|---|
| 0:00–0:05 | **Terminal.** Type `pip install -e '.[demo]'` — already satisfied, so it completes instantly — then `patchahead demo`. | The whole setup cost, shown rather than claimed. |
| 0:05–0:09 | The banner prints: the URL, the repository path, "the bundled repository is never modified", "localhost only". Pause on it. | Establishes the trust boundary before anything runs. |
| 0:09–0:14 | Switch to the browser at `http://127.0.0.1:8000`. Let the page settle. Do not scroll yet. | The header states the problem in one sentence; give it a beat to be read. |
| 0:14–0:18 | Move the cursor across the six scenario cards, slowly enough to read the chips: three **VERIFIED**, one **REFUSED**, one **VALIDATION FAILED**, one **PATCHED UNVERIFIED**. | Four outcomes, visible before a single click. This is the honesty of the tool in one frame. |
| 0:18–0:20 | Click **A renamed field**, then **Run this scenario**. | — |
| 0:20–0:24 | It runs — a real `pytest` subprocess in a temporary copy. Stay on the status line (`copying the repository, patching the copy, running its tests…`). | The pause is the product working, not a spinner. |
| 0:24–0:29 | The green **VERIFIED MIGRATION** banner. Hold on it, including the line *"A test failed before this patch and passes after it."* | The claim, and the evidence for it, in the same frame. |
| 0:29–0:36 | Scroll steadily to **④ Patch**. Stop on the diff: two changed lines, and `TOTAL_LABEL = "total"` nowhere in it. Then continue to **⑤ Verification** and hold on the five green gates. | Minimal diff, then the gates that earned the banner. |
| 0:36–0:41 | Scroll back up and click **Same field name, different object** → **Run this scenario**. | The pivot. |
| 0:41–0:45 | The amber **REFUSED** banner, then the impact table: both sites found, graded `low`, decision **leave alone**, reason *"receiver is order, not the declared owner invoice"*. End here. | The differentiator: it found the code and chose not to touch it. |

## What to say, if there is narration

Four sentences, one per beat:

1. "An upstream API changed and some of this service no longer works."
2. "PatchAhead reads the release note, finds the call sites, and patches a copy
   of the repository — never the repository itself."
3. "It runs the tests there, and it only says *migrated* when a test that failed
   before the patch passes after it."
4. "And when the change is about a different object that happens to share a
   field name, it finds the code, explains it, and refuses."

## Things that ruin the take

- **Scrolling fast.** The page is dense; a viewer needs about a second per
  section. Slow, steady, no overshoot.
- **Starting from a cold environment.** See "warm the cache" above.
- **Filming a command that does not work.** Once PatchAhead is published, the
  first line becomes `pip install 'patchahead[demo]'` and the recording gets
  better. Until then it is a source install, and pretending otherwise puts a
  command on screen that fails for anyone who tries it.
- **Recording the whole screen.** Capture the browser window only, plus the
  terminal for the first five seconds.
- **A port collision.** If 8000 is busy the demo moves up and the URL changes
  mid-take. Check with `patchahead demo --port 8000` first, or pass a port you
  know is free.
- **Editing the outcome.** If a scenario reports something other than what the
  card promised, that is a bug to file, not a take to cut around.

## Saving it

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
