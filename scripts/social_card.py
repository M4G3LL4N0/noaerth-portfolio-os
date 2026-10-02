#!/usr/bin/env python3
"""Render 1280x640 Open Graph / social-preview cards from one template.

Design system: research laboratory x advanced engineering company x
underground creative technology studio. Dark, geometric, hairline grid, one
restrained spectral accent, monospace labels, grotesque wordmark. Renders to
PNG with rsvg-convert because GitHub's social-preview upload accepts raster
images reliably.

Every fact printed on a card comes from the CARDS table below, which is filled
from verified test/build output. Nothing is inferred.

Usage:
    python3 social_card.py <output-dir> [name ...]
"""
from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

BG = "#06070A"
GRID = "#ffffff"
INK = "#ECEEF1"
BODY = "#B9BEC6"
MUTED = "#8A9099"
FAINT = "#4A5058"
MINT = "#5EE7D0"
PERI = "#7C8CFF"
VIOLET = "#C084FC"

# name -> (title, subtitle, [facts])
CARDS: dict[str, tuple[str, str, list[str]]] = {
    "noaerth-portfolio-os": (
        "Noaerth Portfolio OS",
        "Control plane for a portfolio of autonomous agents.",
        ["PYTHON 3.11-3.13", "0 DEPENDENCIES", "125 TESTS", "MIT"],
    ),
    "agentos": (
        "AgentOS",
        "Provider-neutral agent execution engine.",
        ["PYTHON", "710 TESTS", "MCP", "A2A", "MIT"],
    ),
    "grokinstall": (
        "GrokInstall",
        "Install the capability, not the complexity.",
        ["GO 1.24", "28 PACKAGES TESTED", "MIT"],
    ),
    "grokmax": (
        "GrokMax",
        "Deterministic-first routing that refuses to overstate savings.",
        ["TYPESCRIPT", "287 TESTS", "NODE 24", "PRE-RELEASE"],
    ),
    "gh0st": (
        "gh0st",
        "Local-first encrypted AI client. Nothing leaves by default.",
        ["TAURI", "RUST", "TYPESCRIPT", "MIT"],
    ),
    "opencode-watchdog": (
        "OpenCode Watchdog",
        "A local circuit breaker for runaway agent sessions.",
        ["DETERMINISTIC", "NO MODEL", "70 TESTS", "MIT"],
    ),
    "M4G3LL4N0": (
        "M4G3LL4N0",
        "Build systems. Prove them. Compound what works.",
        ["AUTONOMOUS SYSTEMS", "DEV INFRASTRUCTURE", "RESEARCH TOOLING"],
    ),
}


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def wrap(text: str, per_line: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= per_line:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def svg(name: str, title: str, subtitle: str, facts: list[str]) -> str:
    title_lines = wrap(title, 20)
    y = 300 - 46 * (len(title_lines) - 1)

    title_markup = "".join(
        f'<text x="96" y="{y}" class="sans t">{esc(line)}</text>' for line in title_lines
    )
    y += 78

    subtitle_lines = wrap(subtitle, 52)
    subtitle_markup = "".join(
        f'<text x="96" y="{y + 46 * i}" class="sans s">{esc(line)}</text>'
        for i, line in enumerate(subtitle_lines)
    )

    # Fact strip along the bottom, evenly spaced, never overflowing.
    slot = 1088 / max(1, len(facts))
    fact_markup = "".join(
        f'<rect x="{96 + slot * i:.0f}" y="520" width="3" height="26" fill="{MINT}" opacity="0.7"/>'
        f'<text x="{96 + slot * i + 16:.0f}" y="539" class="f">{esc(fact)}</text>'
        for i, fact in enumerate(facts)
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 640" width="1280" height="640" role="img" aria-label="{esc(title)} — {esc(subtitle)}">
  <defs>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{MINT}"/><stop offset="55%" stop-color="{PERI}"/><stop offset="100%" stop-color="{VIOLET}"/>
    </linearGradient>
    <linearGradient id="rule" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{MINT}" stop-opacity="0.8"/><stop offset="100%" stop-color="{VIOLET}" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0%" stop-color="{PERI}" stop-opacity="0.15"/><stop offset="100%" stop-color="{PERI}" stop-opacity="0"/>
    </radialGradient>
    <pattern id="grid" width="64" height="64" patternUnits="userSpaceOnUse">
      <path d="M64 0 L0 0 0 64" fill="none" stroke="{GRID}" stroke-opacity="0.045" stroke-width="1"/>
    </pattern>
    <style>
      .sans {{ font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; }}
      .mono {{ font-family: Menlo, Consolas, monospace; }}
      .t {{ font-size: 76px; font-weight: 700; letter-spacing: -2.2px; fill: {INK}; }}
      .s {{ font-size: 29px; font-weight: 400; fill: {BODY}; }}
      .f {{ font-size: 15px; letter-spacing: 2px; fill: {MUTED}; }}
    </style>
  </defs>

  <rect width="1280" height="640" fill="{BG}"/>
  <rect width="1280" height="640" fill="url(#grid)"/>
  <ellipse cx="1090" cy="140" rx="420" ry="300" fill="url(#glow)"/>

  <g stroke="{MINT}" stroke-opacity="0.22" stroke-width="1">
    <line x1="980" y1="212" x2="1058" y2="140"/>
    <line x1="1058" y1="140" x2="1146" y2="188"/>
    <line x1="1146" y1="188" x2="1224" y2="120"/>
    <line x1="980" y1="212" x2="1146" y2="188"/>
  </g>
  <g fill="{BG}" stroke="{PERI}" stroke-width="1.6">
    <circle cx="980" cy="212" r="6"/><circle cx="1058" cy="140" r="6"/>
    <circle cx="1146" cy="188" r="7" fill="{MINT}" stroke="{MINT}"/><circle cx="1224" cy="120" r="6"/>
  </g>

  <rect x="96" y="112" width="3" height="44" fill="url(#accent)"/>
  <text x="120" y="142" class="mono" font-size="19" letter-spacing="5.5" fill="{MINT}">NOAERTH / M4G3LL4N0</text>
  <text x="120" y="196" class="mono" font-size="16" letter-spacing="2" fill="{FAINT}">github.com/M4G3LL4N0/{esc(name)}</text>

  {title_markup}
  {subtitle_markup}

  <line x1="96" y1="480" x2="1184" y2="480" stroke="url(#rule)" stroke-width="1"/>
  {fact_markup}

  <text x="96" y="600" class="mono" font-size="15" letter-spacing="2.5" fill="{FAINT}">noaerth.com</text>
</svg>
"""


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    wanted = sys.argv[2:] or list(CARDS)
    out_dir.mkdir(parents=True, exist_ok=True)

    unknown = [n for n in wanted if n not in CARDS]
    if unknown:
        print(f"unknown card(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    failures = 0
    for name in wanted:
        title, subtitle, facts = CARDS[name]
        svg_path = out_dir / f"{name}.svg"
        png_path = out_dir / f"{name}.png"
        svg_path.write_text(svg(name, title, subtitle, facts), encoding="utf-8")

        result = subprocess.run(
            ["rsvg-convert", "-w", "1280", "-h", "640", "-o", str(png_path), str(svg_path)],
            capture_output=True,
        )
        if result.returncode != 0 or not png_path.exists():
            print(f"FAIL {name}: {result.stderr.decode()[:120]}", file=sys.stderr)
            failures += 1
            continue
        size = png_path.stat().st_size
        dims = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(png_path)],
            capture_output=True, text=True,
        ).stdout
        print(f"ok   {name:24} {png_path} {size // 1024}KB  {dims.split()[-3]}x{dims.split()[-1]}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
