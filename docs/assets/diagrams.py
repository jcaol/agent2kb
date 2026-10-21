"""Generate the README illustrations in docs/assets/ (logo, hero, diagrams, vault).

Run from the repository root after editing the text below:

    python3 docs/assets/diagrams.py

Each diagram that sits on the page background is written twice, ``*-light.svg`` and
``*-dark.svg``; the README picks one with ``<picture>`` and ``prefers-color-scheme``, which
follows the reader's GitHub theme. Text sizes are chosen for a 1200-unit-wide canvas shown at
GitHub's ~880 px content width, so the smallest label renders at roughly 14 px.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent
FONT = (
    "font-family=\"-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, "
    "sans-serif\""
)
MONO = "font-family=\"ui-monospace, SFMono-Regular, Menlo, Consolas, monospace\""

THEMES = {
    "light": {
        "text": "#1f2328", "muted": "#59636e", "card": "#ffffff", "line": "#d1d9e0",
        "soft": "#f6f8fa", "accent": "#4f46e5", "accent2": "#0d9488", "accent3": "#c2410c",
        "chip": "#eef2ff", "chip_text": "#3730a3",
    },
    "dark": {
        "text": "#f0f6fc", "muted": "#9198a1", "card": "#151b23", "line": "#3d444d",
        "soft": "#0d1117", "accent": "#818cf8", "accent2": "#2dd4bf", "accent3": "#fb923c",
        "chip": "#1e1b4b", "chip_text": "#c7d2fe",
    },
}


def t(x, y, s, size, fill, weight=400, anchor="start", mono=False):
    family = MONO if mono else FONT
    return (
        f'<text x="{x}" y="{y}" {family} font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>'
    )


def svg(w, h, body, label):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" '
        f'height="{h}" role="img" aria-label="{escape(label)}">\n'
        f"<title>{escape(label)}</title>\n{body}\n</svg>\n"
    )


# The agent2kb mark (concept B, "compiler flow"): two source notes flow into a compiled page
# and out to a knowledge link. Drawn on a 128-unit tile; written to logo.svg and reused below.
LOGO_DEFS = (
    '<linearGradient id="logo-tile" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#312e81"/><stop offset=".55" stop-color="#4f46e5"/>'
    '<stop offset="1" stop-color="#0d9488"/></linearGradient>'
)
LOGO_BODY = """<rect x="4" y="4" width="120" height="120" rx="31" fill="url(#logo-tile)"/>
<path d="M24 45h16m-16 25h16m40-13h20" fill="none" stroke="#e0e7ff" stroke-width="4"
 stroke-linecap="round"/>
<circle cx="20" cy="45" r="6" fill="#c7d2fe"/>
<circle cx="20" cy="70" r="6" fill="#c7d2fe"/>
<path d="M49 29h26l12 12v44H49a7 7 0 0 1-7-7V36a7 7 0 0 1 7-7Z" fill="#fff"/>
<path d="M75 29v12h12" fill="#c7d2fe"/>
<rect x="51" y="50" width="24" height="4.5" rx="2.25" fill="#818cf8"/>
<rect x="51" y="61" width="28" height="4.5" rx="2.25" fill="#c7d2fe"/>
<rect x="51" y="72" width="19" height="4.5" rx="2.25" fill="#c7d2fe"/>
<path d="m96 50 10 7-10 7" fill="none" stroke="#5eead4" stroke-width="4" stroke-linecap="round"
 stroke-linejoin="round"/>
<circle cx="104" cy="81" r="5" fill="#5eead4"/>
<path d="M100 73v3" stroke="#5eead4" stroke-width="4" stroke-linecap="round"/>"""


def logo():
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" '
        'height="128" role="img" aria-label="agent2kb logo">\n<title>agent2kb</title>\n'
        f"<defs>{LOGO_DEFS}</defs>\n{LOGO_BODY}\n</svg>\n"
    )


def hero():
    # One version for both themes: the banner carries its own background.
    b = [
        "<defs>",
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#312e81"/><stop offset="0.55" stop-color="#4f46e5"/>'
        '<stop offset="1" stop-color="#0d9488"/></linearGradient>',
        "</defs>",
        '<rect width="1200" height="320" rx="24" fill="url(#bg)"/>',
    ]
    # The mark, with a faint outline so the tile stands off the banner's own gradient.
    b.insert(1, LOGO_DEFS)
    b.append('<g transform="translate(70 62) scale(1.5)">'
             '<rect x="4" y="4" width="120" height="120" rx="31" fill="none" stroke="#ffffff" '
             f'stroke-opacity="0.35" stroke-width="3"/>{LOGO_BODY}</g>')
    b += [
        t(330, 150, "agent2kb", 92, "#ffffff", 800),
        t(334, 205, "The knowledge compiler for agent teams", 36, "#e0e7ff", 500),
        t(334, 258, "Agents collect · an LLM compiles · agents and people read one wiki",
          24, "#c7d2fe"),
        '<rect x="930" y="40" width="226" height="48" rx="24" fill="#ffffff" '
        'fill-opacity="0.16"/>',
        t(1043, 72, "agent2kb.com", 24, "#ffffff", 700, "middle"),
    ]
    return svg(1200, 320, "\n".join(b), "agent2kb: the knowledge compiler for agent teams")


def box(x, y, w, h, c, stroke, rx=18, dash=False):
    d = ' stroke-dasharray="8 7"' if dash else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{c["card"]}" '
            f'stroke="{stroke}" stroke-width="2.5"{d}/>')


def store(x, y, w, label, sub, c):
    # A storage cylinder: body plus top ellipse.
    h, ry = 112, 16
    return "\n".join([
        f'<path d="M{x} {y + ry} v{h - 2 * ry} a{w / 2} {ry} 0 0 0 {w} 0 v-{h - 2 * ry}" '
        f'fill="{c["soft"]}" stroke="{c["accent2"]}" stroke-width="2.5"/>',
        f'<ellipse cx="{x + w / 2}" cy="{y + ry}" rx="{w / 2}" ry="{ry}" fill="{c["card"]}" '
        f'stroke="{c["accent2"]}" stroke-width="2.5"/>',
        t(x + w / 2, y + 66, label, 26, c["text"], 700, "middle", mono=True),
        t(x + w / 2, y + 96, sub, 18, c["muted"], 400, "middle"),
    ])


def arrow(x1, y1, x2, y2, c, dash=False, color=None):
    d = ' stroke-dasharray="9 8"' if dash else ""
    col = color or c["muted"]
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{col}" stroke-width="3"'
            f'{d} marker-end="url(#ah-{"d" if dash else "s"})"/>')


def pipeline(name, c):
    b = [
        "<defs>",
        f'<marker id="ah-s" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{c["muted"]}"/>'
        "</marker>",
        f'<marker id="ah-d" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10 z" '
        f'fill="{c["accent3"]}"/></marker>',
        "</defs>",
    ]
    # Stage headings.
    for x, n, title in ((40, "1", "COLLECT"), (440, "2", "COMPILE"), (840, "3", "USE")):
        b.append(f'<circle cx="{x + 22}" cy="34" r="20" fill="{c["accent"]}"/>')
        b.append(t(x + 22, 42, n, 22, "#ffffff", 700, "middle"))
        b.append(t(x + 54, 43, title, 24, c["accent"], 800))
    # 1. Collect.
    b.append(box(40, 74, 320, 252, c, c["line"]))
    b.append(t(64, 116, "Agents · bots · you", 26, c["text"], 700))
    chips = [("URL", 64, 140), ("PDF / DOCX", 158, 140), ("video", 64, 188),
             ("audio", 160, 188), ("image", 256, 188), ("Web Clipper", 64, 236)]
    for label, x, y in chips:
        w = 18 + len(label) * 12
        b.append(f'<rect x="{x}" y="{y}" width="{w}" height="36" rx="18" fill="{c["chip"]}"/>')
        b.append(t(x + w / 2, y + 25, label, 18, c["chip_text"], 600, "middle"))
    b.append(t(200, 306, "a2kb ingest", 22, c["accent2"], 700, "middle", mono=True))
    # raw/ store.
    b.append(store(150, 352, 210, "raw/", "your sources", c))
    b.append(arrow(200, 328, 200, 350, c))
    # 2. Compile.
    b.append(f'<polygon points="470,120 790,120 815,192 790,264 470,264 445,192" '
             f'fill="{c["card"]}" stroke="{c["accent"]}" stroke-width="3"/>')
    b.append(t(630, 172, "a2kb compile", 30, c["text"], 800, "middle", mono=True))
    b.append(t(630, 208, "LLM · incremental · sandboxed", 20, c["muted"], 400, "middle"))
    b.append(t(630, 236, "titles · summaries · tags · links", 20, c["muted"], 400, "middle"))
    b.append(t(630, 98, "cron: compile · lint · backup · digest", 18, c["muted"], 500,
               "middle"))
    b.append(arrow(362, 408, 450, 220, c))
    # wiki/ store.
    b.append(store(525, 352, 210, "wiki/", "pages + [[links]]", c))
    b.append(arrow(630, 266, 630, 348, c))
    # 3. Use.
    b.append(box(840, 74, 320, 140, c, c["line"]))
    b.append(t(866, 116, "Agents", 26, c["text"], 700))
    b.append(t(866, 150, "a2kb query · MCP", 20, c["accent2"], 600, mono=True))
    b.append(t(866, 184, "Claude Code, Codex, bots", 19, c["muted"]))
    b.append(box(840, 236, 320, 140, c, c["line"]))
    b.append(t(866, 278, "People", 26, c["text"], 700))
    b.append(t(866, 312, "Obsidian", 20, c["accent2"], 600))
    b.append(t(866, 346, "read · edit · Web Clipper", 19, c["muted"]))
    b.append(arrow(737, 392, 836, 170, c))
    b.append(arrow(737, 410, 836, 310, c))
    # Compounding loop: saved answers feed the next compile.
    b.append(box(840, 398, 320, 92, c, c["accent3"], dash=True))
    b.append(t(866, 436, "outputs/  saved answers", 20, c["text"], 700, mono=True))
    b.append(t(866, 468, "fed back as derived context", 19, c["muted"]))
    b.append(f'<path d="M838 470 C 760 514, 520 514, 505 470 S 492 330, 498 270" fill="none" '
             f'stroke="{c["accent3"]}" stroke-width="3" stroke-dasharray="9 8" '
             f'marker-end="url(#ah-d)"/>')
    # Footer band.
    b.append(f'<rect x="40" y="520" width="1120" height="56" rx="14" fill="{c["soft"]}" '
             f'stroke="{c["line"]}" stroke-width="2"/>')
    b.append(t(600, 556, "One isolated S3 prefix per KB  ·  your own bucket  ·  plain Markdown "
               "you can read, diff and back up", 20, c["text"], 500, "middle"))
    (OUT / f"{name}-{'light' if c is THEMES['light'] else 'dark'}.svg").write_text(
        svg(1200, 600, "\n".join(b),
            "How agent2kb works: collect sources with a2kb ingest into raw/, compile them "
            "with an LLM into wiki/, and read the wiki from agents (CLI, MCP) and people "
            "(Obsidian); saved answers feed the next compile")
    )


PILLARS = [
    ("Compiles, not just retrieves",
     ["An LLM writes real pages with titles,", "summaries, tags and [[links]]."]),
    ("One isolated KB per project",
     ["Own prefix and, where the provider", "allows, its own scoped key."]),
    ("Agents and people, one wiki",
     ["MCP and CLI for agents; plain", "Markdown in Obsidian for people."]),
    ("Knowledge that compounds",
     ["Saved answers feed the next compile,", "labelled as derived, never primary."]),
    ("Your bucket, no lock-in",
     ["Any S3-compatible storage; files you", "can read, diff, back up and edit."]),
    ("Safe with untrusted content",
     ["SSRF guard, sandboxed CLIs, privacy-first", "defaults and a fully local option."]),
]


def pillars(name, c):
    b = []
    accents = [c["accent"], c["accent2"], c["accent3"]]
    for i, (title, lines) in enumerate(PILLARS):
        col, row = i % 2, i // 2
        x, y = 20 + col * 590, 20 + row * 186
        a = accents[i % 3]
        b.append(box(x, y, 570, 166, c, c["line"], rx=20))
        b.append(f'<rect x="{x}" y="{y + 24}" width="6" height="56" rx="3" fill="{a}"/>')
        b.append(f'<circle cx="{x + 56}" cy="{y + 52}" r="24" fill="{a}" fill-opacity="0.15"/>')
        b.append(t(x + 56, y + 61, str(i + 1), 26, a, 800, "middle"))
        b.append(t(x + 96, y + 62, title, 28, c["text"], 700))
        b.append(t(x + 32, y + 110, " ".join(lines[:1]), 21, c["muted"]))
        b.append(t(x + 32, y + 142, " ".join(lines[1:]), 21, c["muted"]))
    (OUT / f"{name}-{'light' if c is THEMES['light'] else 'dark'}.svg").write_text(
        svg(1200, 578, "\n".join(b),
            "What makes agent2kb different: " + "; ".join(p[0] for p in PILLARS))
    )


def vault():
    """An illustration of a compiled page in an Obsidian-style vault (not a screenshot)."""
    bg, panel, side, line = "#1e1e1e", "#262626", "#202020", "#363636"
    text, muted, link, chip = "#dcddde", "#999999", "#a78bfa", "#3b2f63"
    tag_text, accent, ghost = "#d8ccff", "#7c3aed", "#6b6b6b"
    b = [f'<rect width="1200" height="720" rx="18" fill="{bg}"/>',
         f'<rect width="1200" height="44" rx="18" fill="{side}"/>',
         f'<rect y="26" width="1200" height="18" fill="{side}"/>']
    for i, col in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        b.append(f'<circle cx="{28 + i * 22}" cy="22" r="7" fill="{col}"/>')
    b.append(t(600, 28, "notes  ·  wiki/20261019-Alpha-Project-Facts-0ef060b8.md", 16, muted,
               500, "middle"))
    # File tree: the KB prefix is the vault root.
    b.append(f'<rect y="44" width="250" height="676" fill="{side}"/>')
    b.append(f'<line x1="250" y1="44" x2="250" y2="720" stroke="{line}"/>')
    tree = [("▾ raw/", 0, muted), ("20261019-Alpha-Proj…", 1, muted),
            ("▾ wiki/", 0, text), ("20261019-Alpha-Proj…", 1, "#ffffff"),
            ("index", 1, text), ("log", 1, text), ("▾ concepts/", 1, text),
            ("lantern", 2, text), ("markdown", 2, text), ("release-cadence", 2, text),
            ("team-rituals", 2, text), ("▸ outputs/", 0, muted)]
    for i, (name, depth, col) in enumerate(tree):
        y = 86 + i * 34
        if col == "#ffffff":
            b.append(f'<rect x="10" y="{y - 22}" width="230" height="32" rx="6" '
                     f'fill="{accent}" fill-opacity="0.35"/>')
        b.append(t(24 + depth * 18, y, name, 17, col, 600 if depth == 0 else 400))
    # Page: properties (frontmatter), then the compiled article.
    x0 = 290
    b.append(f'<rect x="{x0 - 10}" y="64" width="560" height="168" rx="10" fill="{panel}" '
             f'stroke="{line}"/>')
    props = [("source", "examples/first-note.md"), ("date", "2026-10-19"),
             ("source_kind", "primary"), ("tags", "")]
    for i, (k, v) in enumerate(props):
        y = 96 + i * 36
        b.append(t(x0 + 8, y, k, 16, muted, 500, mono=True))
        b.append(t(x0 + 150, y, v, 16, text, 400, mono=True))
    cx = x0 + 150
    for tag in ("alpha", "release-cadence", "docs"):
        w = 26 + len(tag) * 10.5
        b.append(f'<rect x="{cx}" y="190" width="{w}" height="28" rx="14" fill="{chip}"/>')
        b.append(t(cx + w / 2, 209, "#" + tag, 15, tag_text, 500, "middle"))
        cx += w + 8
    b.append(t(x0, 284, "Alpha Project Facts", 34, "#ffffff", 800))
    body = [
        (318, [("The alpha project, codenamed ", text), ("Lantern", link),
               (", ships a release", text)]),
        (346, [("every six weeks; its docs are written in ", text), ("Markdown", link),
               (".", text)]),
    ]
    for y, parts in body:
        b.append(f'<text x="{x0}" y="{y}" {FONT} font-size="19" fill="{text}">'
                 + "".join(f'<tspan fill="{c}">{escape(s)}</tspan>' for s, c in parts)
                 + "</text>")
    b.append(t(x0, 398, "Key facts", 25, "#ffffff", 700))
    facts = [
        [("Started 15 January 2026 by a team of four", text)],
        [("Releases every six weeks: ", text), ("[[Release Cadence]]", link)],
        [("Weekly review on Tuesdays: ", text), ("[[Team Rituals]]", link)],
        [("Named after a ", text), ("[[Lantern]]", link),
         (": it helps you find things", text)],
    ]
    for i, parts in enumerate(facts):
        y = 434 + i * 32
        b.append(f'<circle cx="{x0 + 8}" cy="{y - 6}" r="3.5" fill="{muted}"/>')
        b.append(f'<text x="{x0 + 24}" y="{y}" {FONT} font-size="18" fill="{text}">'
                 + "".join(f'<tspan fill="{c}">{escape(s)}</tspan>' for s, c in parts)
                 + "</text>")
    b.append(t(x0, 590, "Related concepts", 25, "#ffffff", 700))
    related = [("[[Lantern]]", link), ("   [[Markdown]]", link),
               ("   [[Bucket Storage]]", ghost), (" (ghost link)", muted)]
    b.append(f'<text x="{x0}" y="626" {FONT} font-size="18" font-weight="500">'
             + "".join(f'<tspan fill="{c}" xml:space="preserve">{escape(s)}</tspan>'
                       for s, c in related)
             + "</text>")
    b.append(t(x0, 668, "Compiled by a2kb from 1 raw source  ·  answers cite this page",
               15, muted, 400))
    # Graph view.
    gx = 880
    b.append(f'<rect x="{gx}" y="64" width="300" height="372" rx="10" fill="{panel}" '
             f'stroke="{line}"/>')
    b.append(t(gx + 18, 94, "Graph view", 18, text, 700))
    nodes = {"Alpha Project Facts": (1030, 250, 16, accent),
             "Lantern": (950, 170, 10, link), "Markdown": (1105, 165, 10, link),
             "Release Cadence": (1100, 330, 10, link), "Team Rituals": (945, 340, 10, link),
             "Bucket Storage": (1050, 395, 7, ghost), "index": (1035, 128, 7, muted)}
    edges = [("Alpha Project Facts", n) for n in nodes if n != "Alpha Project Facts"]
    edges += [("Lantern", "Markdown"), ("Release Cadence", "Team Rituals")]
    for a, c2 in edges:
        (x1, y1, *_), (x2, y2, *_) = nodes[a], nodes[c2]
        b.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#555555" '
                 f'stroke-width="1.6"/>')
    for name, (x, y, r, col) in nodes.items():
        b.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{col}"/>')
        if name != "index":
            b.append(t(x, y + r + 18, name, 13, muted, 400, "middle"))
    # Backlinks.
    b.append(f'<rect x="{gx}" y="452" width="300" height="236" rx="10" fill="{panel}" '
             f'stroke="{line}"/>')
    b.append(t(gx + 18, 482, "Backlinks  3", 18, text, 700))
    for i, (name, note) in enumerate((("concepts/release-cadence", "concept page"),
                                      ("concepts/lantern", "concept page"),
                                      ("index", "KB index"))):
        y = 524 + i * 54
        b.append(t(gx + 18, y, name, 17, link, 500))
        b.append(t(gx + 18, y + 22, note, 14, muted))
    return svg(1200, 720, "\n".join(b),
               "Illustration: a compiled agent2kb page in an Obsidian vault, with frontmatter "
               "tags, the article, wikilinks, a graph view and backlinks")


def devices(name, c):
    """Every device and agent reads the same KB, because the KB lives in one S3 bucket."""
    b = [
        "<defs>",
        f'<marker id="ah" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" '
        f'markerHeight="6" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" '
        f'fill="{c["muted"]}"/></marker>',
        "</defs>",
    ]
    # Central bucket.
    cx, cy = 600, 250
    b.append(f'<path d="M{cx - 130} {cy - 70} v140 a130 26 0 0 0 260 0 v-140" '
             f'fill="{c["soft"]}" stroke="{c["accent2"]}" stroke-width="3"/>')
    b.append(f'<ellipse cx="{cx}" cy="{cy - 70}" rx="130" ry="26" fill="{c["card"]}" '
             f'stroke="{c["accent2"]}" stroke-width="3"/>')
    b.append(t(cx, cy - 4, "One KB", 30, c["text"], 800, "middle"))
    b.append(t(cx, cy + 30, "in your S3 bucket", 20, c["muted"], 500, "middle"))
    b.append(t(cx, cy + 62, "raw/ · wiki/ · outputs/", 15, c["accent2"], 600, "middle",
               mono=True))
    # People's devices on the left.
    b.append(t(165, 46, "PEOPLE", 20, c["accent"], 800, "middle"))
    b.append(t(165, 72, "Obsidian + Remotely Save", 17, c["muted"], 500, "middle"))
    people = [(110, "Desktop", "macOS · Windows · Linux", "monitor"),
              (250, "iPhone / iPad", "Obsidian for iOS", "phone"),
              (390, "Android", "Obsidian for Android", "phone")]
    for y, title, sub, kind in people:
        b.append(box(30, y, 290, 112, c, c["line"]))
        if kind == "monitor":
            b.append(f'<rect x="50" y="{y + 26}" width="56" height="38" rx="5" fill="none" '
                     f'stroke="{c["accent"]}" stroke-width="3"/>')
            b.append(f'<path d="M78 {y + 64} v10 M66 {y + 76} h24" stroke="{c["accent"]}" '
                     f'stroke-width="3"/>')
        else:
            b.append(f'<rect x="62" y="{y + 20}" width="32" height="56" rx="7" fill="none" '
                     f'stroke="{c["accent"]}" stroke-width="3"/>')
            b.append(f'<circle cx="78" cy="{y + 68}" r="2.5" fill="{c["accent"]}"/>')
        b.append(t(122, y + 52, title, 21, c["text"], 700))
        b.append(t(122, y + 80, sub, 15, c["muted"]))
        b.append(f'<line x1="322" y1="{y + 56}" x2="{cx - 134}" y2="{cy + (y - 250) * 0.35}" '
                 f'stroke="{c["muted"]}" stroke-width="2.5" marker-start="url(#ah)" '
                 f'marker-end="url(#ah)"/>')
    # Agents on the right.
    b.append(t(1035, 46, "AGENTS", 20, c["accent3"], 800, "middle"))
    b.append(t(1035, 72, "CLI · MCP over stdio or HTTPS", 17, c["muted"], 500, "middle"))
    agents = [(110, "Claude Code", "on any laptop"), (250, "Codex & bots", "servers, CI, cron"),
              (390, "Remote MCP", "bearer-token HTTPS")]
    for y, title, sub in agents:
        b.append(box(880, y, 290, 112, c, c["line"]))
        b.append(f'<rect x="900" y="{y + 26}" width="52" height="40" rx="6" fill="none" '
                 f'stroke="{c["accent3"]}" stroke-width="3"/>')
        b.append(t(926, y + 52, ">_", 18, c["accent3"], 700, "middle", mono=True))
        b.append(t(968, y + 52, title, 21, c["text"], 700))
        b.append(t(968, y + 80, sub, 15, c["muted"]))
        b.append(f'<line x1="878" y1="{y + 56}" x2="{cx + 134}" y2="{cy + (y - 250) * 0.35}" '
                 f'stroke="{c["muted"]}" stroke-width="2.5" marker-start="url(#ah)" '
                 f'marker-end="url(#ah)"/>')
    # Compile runs beside the bucket, not on any one device.
    b.append(f'<rect x="{cx - 150}" y="{cy + 112}" width="300" height="58" rx="14" '
             f'fill="{c["card"]}" stroke="{c["accent"]}" stroke-width="2.5"/>')
    b.append(t(cx, cy + 148, "cron: a2kb compile", 19, c["text"], 700, "middle", mono=True))
    b.append(f'<line x1="{cx}" y1="{cy + 110}" x2="{cx}" y2="{cy + 98}" '
             f'stroke="{c["muted"]}" stroke-width="2.5" marker-end="url(#ah)"/>')
    b.append(f'<rect x="40" y="530" width="1120" height="52" rx="14" fill="{c["soft"]}" '
             f'stroke="{c["line"]}" stroke-width="2"/>')
    b.append(t(600, 563, "No single machine owns the knowledge: every device and every agent "
               "reads the same, always-compiled KB", 19, c["text"], 500, "middle"))
    (OUT / f"{name}-{'light' if c is THEMES['light'] else 'dark'}.svg").write_text(
        svg(1200, 600, "\n".join(b),
            "One KB in your S3 bucket, shared by people on desktop, iPhone and Android "
            "through Obsidian, and by agents through the CLI and MCP")
    )


if __name__ == "__main__":
    (OUT / "logo.svg").write_text(logo())
    (OUT / "hero.svg").write_text(hero())
    (OUT / "vault.svg").write_text(vault())
    for theme in THEMES.values():
        pipeline("pipeline", theme)
        pillars("pillars", theme)
        devices("devices", theme)
