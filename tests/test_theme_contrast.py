"""Theme colours must stay legible.

An accessibility audit found 245 contrast failures from a single token: the light
theme's muted text was too pale against every surface it was used on. Browser audits
catch that but need a server, a browser and axe; this reads the tokens straight out of
the stylesheet, so a colour that drops below WCAG AA fails the normal test run.

Only text colours are checked, and only against the backgrounds they are actually
used on — the point is to stop a regression, not to police the palette.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TOKENS = Path(__file__).resolve().parent.parent / "web" / "src" / "styles" / "tokens.css"

# WCAG 2 AA: 4.5:1 for normal text. The UI uses these muted tones at 11.5–14px, which
# is below the 18.66px "large text" threshold, so the stricter ratio applies.
AA_NORMAL = 4.5


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _hex(value: str) -> tuple[int, int, int]:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _tokens(block: str) -> dict[str, str]:
    """The --name: value pairs inside one CSS block."""
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


def _theme(name: str) -> dict[str, str]:
    css = TOKENS.read_text(encoding="utf-8")
    if name == "dark":
        start = css.index(":root")
    else:
        start = css.index(':root[data-theme="light"]')
    body = css[css.index("{", start) + 1: css.index("}", start)]
    return _tokens(body)


# (text token, background token) pairs that appear together in the UI.
PAIRS = [
    ("--text", "--bg"),
    ("--text", "--bg-elev"),
    ("--text", "--surface"),
    ("--text-2", "--bg"),
    ("--text-2", "--bg-elev"),
    ("--text-2", "--surface"),
    ("--text-2", "--surface-2"),
    ("--text-3", "--bg"),
    ("--text-3", "--bg-elev"),
    ("--text-3", "--surface"),
    ("--text-3", "--surface-2"),
    ("--text-3", "--surface-3"),
]


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_text_meets_wcag_aa(theme, fg, bg):
    t = _theme(theme)
    if fg not in t or bg not in t:
        pytest.skip(f"{fg} or {bg} not defined for the {theme} theme")
    ratio = contrast(_hex(t[fg]), _hex(t[bg]))
    assert ratio >= AA_NORMAL, (
        f"{theme} theme: {fg} ({t[fg]}) on {bg} ({t[bg]}) is {ratio:.2f}:1, "
        f"below the {AA_NORMAL}:1 needed for normal text")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_status_colours_are_legible_on_their_own_tint(theme):
    """--danger and --ok are used as text on a tinted chip of the same hue, which is
    where a shade picked for one theme tends to fail in the other."""
    t = _theme(theme)
    bg = _hex(t["--bg"])
    for name in ("--danger", "--ok", "--info"):
        if name not in t:
            continue
        ratio = contrast(_hex(t[name]), bg)
        assert ratio >= 4.0, (
            f"{theme} theme: {name} ({t[name]}) on --bg is {ratio:.2f}:1 — too faint to read")
