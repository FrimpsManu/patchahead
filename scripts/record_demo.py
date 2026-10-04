"""Record docs/media/demo.gif by driving the real `patchahead demo` page.

    pip install playwright pillow && python -m playwright install chromium
    python scripts/record_demo.py docs/media/demo.gif "$(which patchahead)"


Nothing is staged: the server is the real one, each scenario runs the real
engine against the bundled repository, and every frame is a screenshot of what
the page showed at that moment. Frames are kept with their own durations, so
holds cost one frame, not many.
"""

import io
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

PORT = 8731
URL = f"http://127.0.0.1:{PORT}/"
WIDTH, HEIGHT = 1280, 900
SCALE = 0.75  # GitHub renders the README at ~880px; 960px keeps text crisp
OUT = Path(sys.argv[1])
PATCHAHEAD = sys.argv[2]

frames: list[tuple[Image.Image, int]] = []
#: Rows hidden at the top of every frame: the header bar, which shows where the
#: demo is installed -- a path from this machine. Measured once, then the
#: "camera" is the window below it, so the page itself is never altered.
CROP = 0


def shot(page, ms: int) -> None:
    # The header bar shows where the demo is installed -- a path from this
    # machine. The take is framed below it, and a frame that shows it fails.
    bar_bottom = page.evaluate(
        "document.querySelector('header .bar').getBoundingClientRect().bottom"
    )
    assert bar_bottom <= CROP, f"the header bar is in frame ({bar_bottom}px > {CROP}px)"
    png = page.screenshot(clip={"x": 0, "y": CROP, "width": WIDTH, "height": HEIGHT})
    image = Image.open(io.BytesIO(png)).convert("RGB")
    image = image.resize((int(WIDTH * SCALE), int(HEIGHT * SCALE)), Image.LANCZOS)
    if frames and frames[-1][0].tobytes() == image.tobytes():
        frames[-1] = (frames[-1][0], frames[-1][1] + ms)
    else:
        frames.append((image, ms))


def scroll_to(page, selector: str, steps: int = 10, offset: int = 70, nth: int = 0) -> None:
    """Scroll smoothly so the ``nth`` match of ``selector`` sits near the top."""
    target = page.evaluate(
        "([s, n, o]) => document.querySelectorAll(s)[n].getBoundingClientRect().top"
        " + window.scrollY - o",
        [selector, nth, offset + CROP],
    )
    start = page.evaluate("window.scrollY")
    for i in range(1, steps + 1):
        page.evaluate(f"window.scrollTo(0, {start + (target - start) * i / steps})")
        shot(page, 70)


def to_top(page) -> None:
    page.evaluate("window.scrollTo(0, 0)")


def run_scenario(page, scenario: str) -> None:
    page.click(f'.scenario[data-id="{scenario}"]')
    shot(page, 900)
    page.click("#run")
    # The status line while the real engine copies, patches, and runs pytest.
    deadline = time.time() + 60
    while time.time() < deadline and not page.query_selector(".verdict"):
        shot(page, 250)
        time.sleep(0.25)
    page.wait_for_selector(".verdict", timeout=60_000)


server = subprocess.Popen(
    [PATCHAHEAD, "demo", "--no-browser", "--port", str(PORT)],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(URL, timeout=1)
            break
        except OSError:
            time.sleep(0.5)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        probe = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        probe.goto(URL)
        probe.wait_for_selector(".scenario")
        CROP = (
            int(
                probe.evaluate(
                    "document.querySelector('header .bar').getBoundingClientRect().bottom"
                )
            )
            + 6
        )
        probe.close()
        page = browser.new_page(
            viewport={"width": WIDTH, "height": HEIGHT + CROP}, color_scheme="light"
        )

        # Warm the cache once and discard it, as the recording script says.
        page.goto(URL)
        page.wait_for_selector(".scenario")
        page.click('.scenario[data-id="field-rename"]')
        page.click("#run")
        page.wait_for_selector(".verdict", timeout=60_000)

        # The take.
        page.goto(URL)
        page.wait_for_selector(".scenario")
        page.mouse.move(5, 5)
        to_top(page)
        shot(page, 3000)  # the page settles; the header states the problem

        for card in page.query_selector_all(".scenario"):
            card.hover()
            shot(page, 700)  # four outcomes, visible before a click

        run_scenario(page, "field-rename")
        to_top(page)
        scroll_to(page, ".verdict", steps=6, offset=150)
        shot(page, 3500)  # "Verified migration", and why

        scroll_to(page, "section.step h2", nth=3)  # 4 Patch
        shot(page, 3500)  # two changed lines; TOTAL_LABEL untouched
        scroll_to(page, "section.step h2", nth=4)  # 5 Verification
        shot(page, 3500)  # five gates

        to_top(page)
        shot(page, 800)
        run_scenario(page, "receiver-mismatch")
        to_top(page)
        scroll_to(page, ".verdict", steps=6, offset=150)
        shot(page, 3000)  # "Refused"
        scroll_to(page, "section.step h2", nth=1)  # 2 Impact
        shot(page, 4500)  # found, graded low, left alone -- end here

        browser.close()
finally:
    server.terminate()
    server.wait(timeout=10)

images = [frame for frame, _ in frames]
durations = [ms for _, ms in frames]
raw = OUT.with_suffix(".frames")
raw.mkdir(exist_ok=True)
for i, image in enumerate(images):
    image.save(raw / f"{i:03d}.png")
# One palette for every frame, built from a sample of all of them. Per-frame
# palettes make the encoder's frame-to-frame deltas meaningless, and regions
# of an earlier frame bleed into later ones.
step = max(1, len(images) // 16)
sample = images[::step]
mosaic = Image.new("RGB", (images[0].width, images[0].height * len(sample)))
for i, image in enumerate(sample):
    mosaic.paste(image, (0, i * image.height))
palette = mosaic.quantize(colors=192, method=Image.Quantize.MEDIANCUT)
# The page is almost all greys, so a median cut spends the palette on them and
# drops the few small coloured marks -- the outcome dots, the accent -- along
# with pure white. Those are reserved explicitly.
RESERVED = [
    *[(255, 255, 255), (0, 0, 0), (29, 29, 31)],  # page, ink
    *[(0, 113, 227), (41, 151, 255)],  # accent, light and dark
    *[(52, 199, 89), (255, 159, 10), (255, 59, 48)],  # outcome dots
    *[(36, 138, 61), (215, 0, 21)],  # check marks
]
colors = palette.getpalette()[: 192 * 3]
for rgb in RESERVED:
    colors.extend(rgb)
entries = [tuple(colors[i : i + 3]) for i in range(0, len(colors), 3)]
nearest: dict[tuple[int, int, int], int] = {}


def index_of(rgb: tuple[int, int, int]) -> int:
    # Exact nearest colour. Pillow's own palette mapping rounds through a coarse
    # cache, which turns pure white into (252, 252, 252).
    if rgb not in nearest:
        nearest[rgb] = min(
            range(len(entries)),
            key=lambda i: sum((a - b) ** 2 for a, b in zip(rgb, entries[i], strict=True)),
        )
    return nearest[rgb]


quantized = []
for image in images:
    frame = Image.new("P", image.size)
    frame.putpalette(colors)
    pixels = image.tobytes()
    frame.putdata([index_of(tuple(pixels[i : i + 3])) for i in range(0, len(pixels), 3)])
    quantized.append(frame)
quantized[0].save(
    OUT,
    save_all=True,
    append_images=quantized[1:],
    duration=durations,
    loop=0,
    optimize=False,
    disposal=1,
)
total = sum(durations) / 1000
print(f"{len(images)} frames, {total:.1f}s, {OUT.stat().st_size / 1e6:.2f} MB -> {OUT}")
