"""Inline SVG charts, rendered on the server from the same queries as the numbers. No client
library, no build step, and replay renders them exactly as live does."""

from __future__ import annotations

from html import escape
from typing import Any

MONO = "font-family:'JetBrains Mono',monospace"
GREY, RULE, INK, FAINT = "#e9e7e1", "#e2dfd8", "#14181f", "#8f97a3"
GREEN, RED, AMBER, PURPLE, TEAL, BLUE = (
    "#2e7d4f",
    "#b4452e",
    "#b8862a",
    "#7a4fb5",
    "#1f8a80",
    "#2c5ba6",
)


def _svg(w: int, h: int, body: str, title: str = "") -> str:
    t = f"<title>{escape(title)}</title>" if title else ""
    return f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" style="display:block;overflow:visible">{t}{body}</svg>'


def sparkline(values: list[float], color: str, w: int = 220, h: int = 36, title: str = "") -> str:
    """A line over equal bins with the last point marked. Flat at zero is drawn as a baseline."""
    n = len(values)
    if n == 0:
        return _svg(
            w, h, f'<line x1="0" y1="{h - 2}" x2="{w}" y2="{h - 2}" stroke="{RULE}"/>', title
        )
    top = max(values) or 1.0
    step = w / max(n - 1, 1)
    pts = [(i * step, h - 4 - (v / top) * (h - 10)) for i, v in enumerate(values)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
    area = path + f" L{pts[-1][0]:.1f},{h - 2} L0,{h - 2} Z"
    lx, ly = pts[-1]
    return _svg(
        w,
        h,
        f'<line x1="0" y1="{h - 2}" x2="{w}" y2="{h - 2}" stroke="{RULE}"/>'
        f'<path d="{area}" fill="{color}" fill-opacity=".12"/>'
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.5" fill="{color}"/>',
        title,
    )


def funnel(rows: list[tuple[str, int, int | None]], w: int = 520) -> str:
    """Horizontal bars, one per stage; a red bar beside the stage a drop leaves.
    rows: (label, count, drop_count_or_None)."""
    top = max((n for _, n, _ in rows), default=0) or 1
    rh, gap, lab = 22, 8, 210
    h = len(rows) * (rh + gap) + 4
    out = []
    for i, (label, n, drop) in enumerate(rows):
        y = 2 + i * (rh + gap)
        bw = (w - lab - 60) * n / top
        out.append(
            f'<text x="0" y="{y + 15}" font-size="11" fill="{INK}" style="{MONO}">{escape(label)}</text>'
        )
        out.append(
            f'<rect x="{lab}" y="{y}" width="{max(bw, 1):.1f}" height="{rh}" rx="3" fill="{BLUE}" fill-opacity=".85"/>'
        )
        out.append(
            f'<text x="{lab + max(bw, 1) + 6:.1f}" y="{y + 15}" font-size="12" font-weight="600" fill="{INK}" style="{MONO}">{n}</text>'
        )
        if drop:
            dw = (w - lab - 60) * drop / top
            out.append(
                f'<rect x="{lab}" y="{y + rh - 6}" width="{max(dw, 2):.1f}" height="6" rx="2" fill="{RED}"/>'
            )
            out.append(
                f'<text x="{lab + max(bw, 1) + 6 + 24:.1f}" y="{y + 15}" font-size="11" fill="{RED}" style="{MONO}">-{drop}</text>'
            )
    return _svg(w, h, "".join(out), "funnel")


def dot_strip(
    points: list[tuple[str, float, str]],
    cap: float | None,
    median: float | None,
    w: int = 520,
    unit: str = "ACU",
) -> str:
    """ACU per session as dots on one axis; the cap as a red line; the median as a tick.
    points: (label, value, color)."""
    label = {
        "ACU": "ACU per session",
        "$": "dollars per session",
        "min": "minutes per session",
    }.get(unit, f"{unit} per session")
    h, pad = 74, 16
    if points and all(v == 0 for _, v, _ in points):
        note = (
            f"the org has not returned usage for these sessions yet; the cap is {cap:g} per session"
            if cap
            else "the org has not returned usage yet"
        )
        return _svg(
            w,
            h,
            f'<text x="0" y="30" font-size="12" fill="{INK}" style="{MONO}">every session reports 0.0 {unit}</text>'
            f'<text x="0" y="50" font-size="11" fill="{FAINT}" style="{MONO}">{escape(note)}</text>',
            label,
        )
    top = max([cap or 0] + [v for _, v, _ in points]) or 1.0
    sx = lambda v: pad + (w - 2 * pad) * v / top
    out = [f'<line x1="{pad}" y1="40" x2="{w - pad}" y2="40" stroke="{RULE}"/>']
    for t in range(int(top) + 1):
        out.append(
            f'<text x="{sx(t):.1f}" y="66" font-size="10" fill="{FAINT}" text-anchor="middle" style="{MONO}">{t}</text>'
        )
    if cap:
        out.append(
            f'<line x1="{sx(cap):.1f}" y1="14" x2="{sx(cap):.1f}" y2="52" stroke="{RED}" stroke-dasharray="3 3"/>'
        )
        out.append(
            f'<text x="{sx(cap):.1f}" y="10" font-size="10" fill="{RED}" text-anchor="middle" style="{MONO}">cap {cap:g}</text>'
        )
    if median is not None:
        out.append(
            f'<line x1="{sx(median):.1f}" y1="30" x2="{sx(median):.1f}" y2="50" stroke="{INK}" stroke-width="2"/>'
        )
        out.append(
            f'<text x="{sx(median):.1f}" y="26" font-size="10" fill="{INK}" text-anchor="middle" style="{MONO}">median {median:g}</text>'
        )
    seen: dict[float, int] = {}
    for label, v, color in points:
        k = round(v, 1)
        seen[k] = seen.get(k, 0) + 1
        dy = (seen[k] - 1) * 13
        out.append(
            f'<circle cx="{sx(v):.1f}" cy="{40 - dy}" r="6" fill="{color}" fill-opacity=".9"><title>{escape(label)}: {v:.1f} {unit}</title></circle>'
        )
        out.append(
            f'<text x="{sx(v):.1f}" y="{44 - dy}" font-size="8" font-weight="700" fill="#fff" text-anchor="middle" style="{MONO}">{escape(label[:1])}</text>'
        )
    return _svg(w, h, "".join(out), label)


def squares(states: list[tuple[str, str, str]], w: int = 520) -> str:
    """Time-ordered squares: (label, state, color). One square per verdict or session."""
    size, gap = 22, 6
    per_row = max(1, (w) // (size + gap))
    rows = (len(states) + per_row - 1) // per_row or 1
    h = rows * (size + gap) + 2
    out = []
    for i, (label, state, color) in enumerate(states):
        x = (i % per_row) * (size + gap)
        y = (i // per_row) * (size + gap) + 1
        out.append(
            f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="4" fill="{color}"><title>{escape(label)}: {escape(state)}</title></rect>'
        )
        out.append(
            f'<text x="{x + size / 2}" y="{y + 15}" font-size="10" font-weight="700" fill="#fff" text-anchor="middle" style="{MONO}">{escape(label[:2])}</text>'
        )
    if not states:
        out.append(
            f'<text x="0" y="16" font-size="11" fill="{FAINT}" style="{MONO}">no gate run yet</text>'
        )
    return _svg(w, h, "".join(out), "gate outcomes in time order")


def stacked_bar(parts: list[tuple[str, int, str]], w: int = 520) -> str:
    """One bar, segments left to right, with a two-column legend beneath. parts: (label, n, color)."""
    total = sum(n for _, n, _ in parts) or 1
    h = 72
    out = [f'<rect x="0" y="4" width="{w}" height="18" rx="4" fill="{GREY}"/>']
    x = 0.0
    for label, n, color in parts:
        bw = w * n / total
        if n:
            out.append(
                f'<rect x="{x:.1f}" y="4" width="{bw:.1f}" height="18" fill="{color}"><title>{escape(label)}: {n}</title></rect>'
            )
            if bw > 18:
                out.append(
                    f'<text x="{x + bw / 2:.1f}" y="17" font-size="10" font-weight="700" fill="#fff" text-anchor="middle" style="{MONO}">{n}</text>'
                )
        x += bw
    for i, (label, n, color) in enumerate(parts):
        lx = (i % 2) * (w / 2)
        ly = 36 + (i // 2) * 16
        out.append(f'<rect x="{lx:.1f}" y="{ly}" width="10" height="10" rx="2" fill="{color}"/>')
        out.append(
            f'<text x="{lx + 14:.1f}" y="{ly + 9}" font-size="10.5" fill="{INK}" style="{MONO}">{escape(label)} {n}</text>'
        )
    out.append(
        f'<text x="{w}" y="61" font-size="10.5" fill="{FAINT}" text-anchor="end" style="{MONO}">total {total}</text>'
    )
    return _svg(w, h, "".join(out), "inventory burn-down")


def histogram(bins: list[tuple[str, int, bool]], w: int = 260) -> str:
    """Small vertical bars; unhealthy bins in red. bins: (label, n, bad)."""
    h, base = 96, 74
    top = max((n for _, n, _ in bins), default=0) or 1
    bw = (w - 8 * (len(bins) - 1)) / max(len(bins), 1)
    out = [f'<line x1="0" y1="{base}" x2="{w}" y2="{base}" stroke="{RULE}"/>']
    for i, (label, n, bad) in enumerate(bins):
        x = i * (bw + 8)
        bh = (base - 16) * n / top
        out.append(
            f'<rect x="{x:.1f}" y="{base - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="3" fill="{RED if bad else PURPLE}" fill-opacity="{".9" if n else ".15"}"/>'
        )
        out.append(
            f'<text x="{x + bw / 2:.1f}" y="{base - bh - 4:.1f}" font-size="11" font-weight="600" fill="{RED if bad and n else INK}" text-anchor="middle" style="{MONO}">{n}</text>'
        )
        out.append(
            f'<text x="{x + bw / 2:.1f}" y="{base + 14}" font-size="10.5" fill="{RED if bad else FAINT}" text-anchor="middle" style="{MONO}">{escape(label)}</text>'
        )
    return _svg(w, h, "".join(out), "session size")


def stage_durations(
    rows: list[dict[str, Any]],
) -> str:  # pragma: no cover - reserved for the Tracker pass
    return ""


def bars(
    values: list[float], labels: list[str], color: str, w: int = 220, h: int = 44, unit: str = ""
) -> str:
    """Bars with a hover title per bin, the way a usage page draws them. Zero bins are drawn as a
    faint dash so the axis stays legible."""
    n = len(values)
    if n == 0:
        return _svg(w, h, f'<line x1="0" y1="{h - 2}" x2="{w}" y2="{h - 2}" stroke="{RULE}"/>')
    top = max(values) or 1.0
    gap = 2.0
    bw = max((w - gap * (n - 1)) / n, 1.0)
    out = []
    for i, v in enumerate(values):
        x = i * (bw + gap)
        bh = (h - 6) * v / top
        label = labels[i] if i < len(labels) else ""
        shown = (
            f"{v:.2f}"
            if unit == "$"
            else (f"{v:.1f}" if isinstance(v, float) and v != int(v) else f"{int(v)}")
        )
        if v > 0:
            out.append(
                f'<rect class="bar" x="{x:.1f}" y="{h - 2 - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="1.5" fill="{color}">'
                f"<title>{escape(label)}: {shown}{' ' + unit if unit and unit != '$' else ''}{'$' if unit == '$' else ''}</title></rect>"
            )
        else:
            out.append(
                f'<rect class="bar" x="{x:.1f}" y="{h - 4}" width="{bw:.1f}" height="2" rx="1" fill="{GREY}"><title>{escape(label)}: 0</title></rect>'
            )
    return _svg(w, h, "".join(out))
