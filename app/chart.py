"""Price history chart as a PNG image, for alert messages.

Drawn with Pillow at twice the target size and scaled down, which smooths the lines.
Only numbers and dates are drawn as text. Product titles are never rendered, so no
untrusted text reaches the font renderer.
"""

import io
import math
from collections.abc import Sequence
from datetime import datetime, tzinfo

from PIL import Image, ImageDraw, ImageFont

from app.money import format_money

WIDTH, HEIGHT, SCALE = 900, 420, 2
LEFT, RIGHT, TOP, BOTTOM = 96, 28, 58, 46
TARGET_GRID_LINES = 5
MAX_POINT_MARKERS = 60
# The dashboard's dark palette.
BACKGROUND = (15, 20, 27)
GRID = (38, 46, 58)
TEXT = (138, 151, 168)
BRIGHT = (231, 237, 244)
ACCENT = (91, 157, 255)
TARGET = (61, 220, 176)
BASELINE = (138, 151, 168)

Point = tuple[datetime, int]


def _dashed(draw: ImageDraw.ImageDraw, y: float, x0: float, x1: float, color: tuple[int, int, int]) -> None:
    dash, gap, x = 12 * SCALE, 8 * SCALE, x0
    while x < x1:
        draw.line([(x, y), (min(x + dash, x1), y)], fill=color, width=2 * SCALE)
        x += dash + gap


def _ticks(low: float, high: float) -> list[int]:
    """Round grid values (1, 2, 2.5 or 5 times a power of ten) inside the visible range."""
    raw = (high - low) / (TARGET_GRID_LINES - 1)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    value = math.ceil(low / step) * step
    ticks = []
    while value <= high:
        ticks.append(int(round(value)))
        value += step
    return sorted(set(ticks))  # sub-unit steps round to the same value


def render_price_chart(
    points: Sequence[Point],
    currency: str,
    zone: tzinfo,
    baseline_minor: int | None = None,
    target_minor: int | None = None,
) -> bytes:
    """Render observations (time, price in minor units), oldest first, with optional reference lines."""
    if len(points) < 2:
        raise ValueError("a chart needs at least two observations")
    width, height = WIDTH * SCALE, HEIGHT * SCALE
    left, right, top, bottom = LEFT * SCALE, width - RIGHT * SCALE, TOP * SCALE, height - BOTTOM * SCALE
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=13 * SCALE)
    bold = ImageFont.load_default(size=15 * SCALE)

    times = [moment.timestamp() for moment, _ in points]
    prices = [price for _, price in points]
    references = [value for value in (baseline_minor, target_minor) if value is not None]
    lowest, highest = min(prices + references), max(prices + references)
    padding = max((highest - lowest) * 0.08, highest * 0.01, 1.0)
    low, high = lowest - padding, highest + padding
    start, span = times[0], max(times[-1] - times[0], 1.0)
    ticks = _ticks(low, high)
    # Widen the left margin when the price labels need more room, so large amounts are never cut off.
    widest = max(draw.textlength(format_money(value, currency), font=font) for value in ticks) if ticks else 0
    left = max(left, int(widest) + 22 * SCALE)

    def x_of(stamp: float) -> float:
        return left + (stamp - start) / span * (right - left)

    def y_of(value: float) -> float:
        return bottom - (value - low) / (high - low) * (bottom - top)

    for value in ticks:
        y = y_of(value)
        draw.line([(left, y), (right, y)], fill=GRID, width=SCALE)
        draw.text((left - 10 * SCALE, y), format_money(value, currency), fill=TEXT, font=font, anchor="rm")

    for stamp, anchor in ((times[0], "la"), (times[len(times) // 2], "ma"), (times[-1], "ra")):
        local = datetime.fromtimestamp(stamp, zone)
        # Within two days the date alone would repeat, so the time of day is shown instead.
        label = f"{local.hour % 12 or 12}:{local:%M} {local:%p}" if span <= 2 * 86400 else f"{local:%b} {local.day}"
        draw.text((x_of(stamp), bottom + 12 * SCALE), label, fill=TEXT, font=font, anchor=anchor)

    for reference, color, name in ((baseline_minor, BASELINE, "Baseline"), (target_minor, TARGET, "Target")):
        if reference is None:
            continue
        y = y_of(reference)
        _dashed(draw, y, left, right, color)
        text = f"{name} {format_money(reference, currency)}"
        draw.text((right - 6 * SCALE, y - 6 * SCALE), text, fill=color, font=font, anchor="rs")

    path = [(x_of(stamp), y_of(price)) for stamp, price in zip(times, prices, strict=True)]
    draw.line(path, fill=ACCENT, width=3 * SCALE, joint="curve")
    if len(path) <= MAX_POINT_MARKERS:
        radius = 4 * SCALE
        for x, y in path:
            draw.ellipse([(x - radius, y - radius), (x + radius, y + radius)], fill=ACCENT)
    last_x, last_y = path[-1]
    radius = 7 * SCALE
    box = [(last_x - radius, last_y - radius), (last_x + radius, last_y + radius)]
    draw.ellipse(box, fill=BRIGHT, outline=ACCENT, width=3 * SCALE)

    # Header: the period on the left, the latest price on the right, clear of every line and label.
    first, final = datetime.fromtimestamp(times[0], zone), datetime.fromtimestamp(times[-1], zone)
    period = f"{first:%b} {first.day} to {final:%b} {final.day}, {len(points)} checks"
    draw.text((left, 20 * SCALE), period, fill=TEXT, font=font, anchor="la")
    draw.text((right, 16 * SCALE), f"Now {format_money(prices[-1], currency)}", fill=BRIGHT, font=bold, anchor="ra")

    output = io.BytesIO()
    image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS).save(output, format="PNG", optimize=True)
    return output.getvalue()
