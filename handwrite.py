"""
Handwriting Page Generator
==========================

Turns a plain text file into realistic handwritten pages (PDF, PNG, SVG),
with full control over the page: paper size, margins, alignment
(including full justification), line spacing, ruled paper, ink colour,
pen thickness, handwriting style and more.

Quick start:
    python handwrite.py examples/letter.txt

See README.md for every option.
"""
import argparse
import copy
import json
import os
import sys
import textwrap

import numpy as np

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

PAGE_SIZES = {  # width, height in inches (portrait)
    "letter": (8.5, 11.0),
    "legal": (8.5, 14.0),
    "a3": (11.69, 16.54),
    "a4": (8.27, 11.69),
    "a5": (5.83, 8.27),
    "b5": (6.93, 9.84),
    "executive": (7.25, 10.5),
}

DEFAULTS = {
    "page": {
        "size": "letter",          # letter, legal, a3, a4, a5, b5, executive, custom
        "width": 8.5,              # used only when size is "custom"
        "height": 11.0,            # used only when size is "custom"
        "unit": "in",              # "in" or "mm" for width, height, margins, indent
        "orientation": "portrait", # portrait or landscape
        "margin_top": 1.0,
        "margin_bottom": 1.0,
        "margin_left": 1.0,
        "margin_right": 1.0,
        "paper_color": "#ffffff",
        "ruled_lines": False,      # draw notebook lines under the writing
        "rule_color": "#b8cfe8",
        "rule_thickness": 0.6,
        "margin_line": False,      # vertical line on the left like school paper
        "margin_line_color": "#e88080",
        "page_numbers": False,
        "page_number_format": "{n} / {total}",
        "page_number_color": "#666666",
    },
    "text": {
        "align": "justify",        # justify, left, right, center
        "chars_per_line": 50,      # fewer characters = bigger handwriting (max ~60)
        "line_spacing": 1.0,       # 1.0 = normal, 1.5 = one and a half, 2.0 = double
        "paragraph_spacing": 0.5,  # extra space for each blank line, in lines
        "indent": 0.0,             # first-line indent of every paragraph (in page units)
        "justify_last_line": False # also stretch the last line of each paragraph
    },
    "pen": {
        "style": 0,                # handwriting style 0-12 (see --preview-styles)
        "neatness": 2.0,           # 0.2 = messy/varied ... 3.0 = very neat
        "color": "#1a2a6c",        # any colour name or hex code
        "thickness": 1.0,          # 1.0 = ballpoint, 2.0 = marker
        "slant_jitter": 0.0,       # random up/down drift per line (0 = perfectly level)
    },
    "output": {
        "formats": ["pdf", "png"], # any of pdf, png, svg
        "dpi": 150,                # PNG resolution
        "seed": None,              # set a number to get the same result every run
    },
}

UNIT_TO_INCH = {"in": 1.0, "mm": 1 / 25.4, "cm": 1 / 2.54}


def deep_update(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v
    return base


def parse_value(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def load_settings(path, overrides):
    settings = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, encoding="utf-8") as f:
            deep_update(settings, json.load(f))
    for item in overrides:
        if "=" not in item:
            sys.exit(f"--set expects section.key=value, got: {item}")
        key, raw = item.split("=", 1)
        parts = key.strip().split(".")
        if len(parts) != 2 or parts[0] not in settings or parts[1] not in settings[parts[0]]:
            sys.exit(f"Unknown setting '{key}'. Run with --show-settings to list them.")
        settings[parts[0]][parts[1]] = parse_value(raw.strip())
    return settings


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

# characters the handwriting model cannot draw -> closest drawable replacement
REPLACE = {
    "—": " - ", "–": "-", "‒": "-", "−": "-", "‐": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"', "`": "'",
    "…": "...", " ": " ", "\t": " ", "•": "-",
    "[": "(", "]": ")", "{": "(", "}": ")", "<": "(", ">": ")",
    "/": "-", "\\": "-", "*": "-", "_": " ", "|": " ", "~": "-",
    "&": "and", "%": " percent", "+": " plus ", "=": " equals ",
    "@": " at ", "$": "USD ", "^": "",
    "Q": "q", "X": "x", "Z": "z",
}

ALIGN_TAGS = {"[justify]": "justify", "[left]": "left", "[right]": "right", "[center]": "center"}


def sanitize(text, valid):
    out = []
    for ch in text:
        ch = REPLACE.get(ch, ch)
        out.append("".join(c for c in ch if c in valid))
    return " ".join("".join(out).split())


def layout(text, chars, default_align, valid):
    """Turn the source text into rows.

    Row kinds: ("text", line, align, is_first, is_last), ("blank",), ("page",)
    """
    rows = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.lower() == "[page]":
            rows.append(("page",))
            continue
        align = default_align
        for tag, value in ALIGN_TAGS.items():
            if stripped.lower().startswith(tag):
                align, stripped = value, stripped[len(tag):].strip()
                break
        para = sanitize(stripped, valid)
        if not para:
            if rows and rows[-1][0] == "text":
                rows.append(("blank",))
            elif rows and rows[-1][0] == "blank":
                rows.append(("blank",))
            continue
        wrapped = textwrap.wrap(para, width=chars, break_long_words=True)
        for i, ln in enumerate(wrapped):
            rows.append(("text", ln, align, i == 0, i == len(wrapped) - 1))
    while rows and rows[-1][0] != "text":
        rows.pop()
    return rows


# ---------------------------------------------------------------------------
# Stroke handling
# ---------------------------------------------------------------------------

def to_coords(offsets, drawing):
    offsets = offsets.copy()
    offsets[:, :2] *= 1.5
    c = drawing.offsets_to_coords(offsets)
    c = drawing.denoise(c)
    c[:, :2] = drawing.align(c[:, :2])
    c[:, 1] *= -1
    c[:, 0] -= c[:, 0].min()
    return c


def split_strokes(coords):
    """Split into pen-down segments using the end-of-stroke flag."""
    segs, start = [], 0
    for i, eos in enumerate(coords[:, 2]):
        if eos == 1.0:
            segs.append(coords[start:i + 1, :2].copy())
            start = i + 1
    if start < len(coords):
        segs.append(coords[start:, :2].copy())
    return [s for s in segs if len(s)]


def trim_tail(segs, text):
    """Remove stray marks the pen sometimes makes after a line's final period."""
    if not text.endswith(".") or len(segs) < 2:
        return segs
    ys = np.concatenate([s[:, 1] for s in segs])
    h = np.percentile(ys, 95) - np.percentile(ys, 5)
    mid = np.median(ys)
    width = line_width(segs)
    dots = [s for s in segs
            if np.ptp(s[:, 0]) < 0.15 * h and np.ptp(s[:, 1]) < 0.15 * h
            and s[:, 1].mean() > mid and s[:, 0].min() > 0.6 * width]
    if not dots:
        return segs
    dot_end = max(dots, key=lambda s: s[:, 0].max())[:, 0].max()
    kept = [s for s in segs if s[:, 0].min() <= dot_end + 0.05 * h]
    return kept or segs


def line_width(segs):
    return max(s[:, 0].max() for s in segs)


def scale_x(segs, factor):
    return [np.column_stack([s[:, 0] * factor, s[:, 1]]) for s in segs]


def justify(segs, n_spaces, target):
    """Stretch the gaps between words so the line spans exactly `target`."""
    width = line_width(segs)
    if n_spaces == 0 or width >= target:
        return segs
    spans = sorted((s[:, 0].min(), s[:, 0].max()) for s in segs)
    blocks = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= blocks[-1][1] + 1.0:
            blocks[-1][1] = max(blocks[-1][1], b)
        else:
            blocks.append([a, b])
    gaps = [(blocks[i + 1][0] - blocks[i][1], blocks[i][1]) for i in range(len(blocks) - 1)]
    if not gaps:
        return segs
    cut_points = sorted(x for _, x in sorted(gaps, reverse=True)[:n_spaces])
    extra = (target - width) / len(cut_points)
    out = []
    for s in segs:
        shift = extra * sum(1 for cp in cut_points if s[:, 0].min() > cp)
        t = s.copy()
        t[:, 0] += shift
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate(hand, lines, idx, style, bias, batch=12):
    results = []
    for i in range(0, len(idx), batch):
        chunk = idx[i:i + batch]
        print(f"  writing line(s) {', '.join(str(j + 1) for j in chunk)}")
        out = hand._sample([lines[j] for j in chunk], biases=[bias] * len(chunk),
                           styles=[style] * len(chunk))
        results += list(zip(chunk, out))
    return results


def preview_styles(hand, drawing, path, text, bias):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    styles = list(range(13))
    out = hand._sample([text] * len(styles), biases=[bias] * len(styles), styles=styles)
    fig, axs = plt.subplots(len(styles), 1, figsize=(10, 1.2 * len(styles)))
    for ax, s, st in zip(axs, out, styles):
        for seg in trim_tail(split_strokes(to_coords(s, drawing)), text):
            ax.plot(seg[:, 0], -seg[:, 1], color="#1a2a6c", lw=1)
        ax.set_title(f"style {st}", loc="left", fontsize=9)
        ax.axis("off")
        ax.set_aspect("equal")
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    print(f"saved {path}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(placed_rows, settings, out, total_target):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    import svgwrite

    pg, tx, pen, op = settings["page"], settings["text"], settings["pen"], settings["output"]
    unit = UNIT_TO_INCH.get(pg["unit"], 1.0)

    if pg["size"] == "custom":
        w_in, h_in = pg["width"] * unit, pg["height"] * unit
    else:
        if pg["size"] not in PAGE_SIZES:
            sys.exit(f"Unknown page size '{pg['size']}'. Use one of: {', '.join(PAGE_SIZES)}, custom")
        w_in, h_in = PAGE_SIZES[pg["size"]]
    if pg["orientation"] == "landscape":
        w_in, h_in = h_in, w_in

    ml, mr = pg["margin_left"] * unit, pg["margin_right"] * unit
    mt, mb = pg["margin_top"] * unit, pg["margin_bottom"] * unit
    usable_in = w_in - ml - mr
    if usable_in <= 0:
        sys.exit("Margins are wider than the page.")

    upi = total_target / usable_in  # drawing units per inch
    page_w, page_h = w_in * upi, h_in * upi
    ml_u, mt_u, mb_u = ml * upi, mt * upi, mb * upi

    heights = []
    for row in placed_rows:
        if row[0] == "text":
            ys = np.concatenate([s[:, 1] for s in row[1]])
            heights.append(np.percentile(ys, 95) - np.percentile(ys, 5))
    base_line_h = max(1.75 * float(np.median(heights)), total_target / 26)
    line_h = base_line_h * float(tx["line_spacing"])

    # flow rows onto pages
    rng = np.random.default_rng(op["seed"])
    pages, cur, y = [], [], mt_u
    for row in placed_rows:
        if row[0] == "page":
            if cur:
                pages.append(cur)
            cur, y = [], mt_u
            continue
        if row[0] == "blank":
            if cur:
                y += line_h * float(tx["paragraph_spacing"])
            continue
        if y + line_h > page_h - mb_u and cur:
            pages.append(cur)
            cur, y = [], mt_u
        jitter = rng.normal(0, 1) * float(pen["slant_jitter"]) * line_h * 0.08
        cur.append((row[1], y + 0.6 * line_h + jitter, row[2]))
        y += line_h
    if cur:
        pages.append(cur)

    stroke_pt = 0.75 * float(pen["thickness"])     # line width in points
    stroke_u = stroke_pt / 72.0 * upi
    rule_u = float(pg["rule_thickness"]) / 72.0 * upi
    formats = [f.lower() for f in op["formats"]]
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    pdf = PdfPages(out + ".pdf") if "pdf" in formats else None
    total = len(pages)
    for pn, page in enumerate(pages, 1):
        fig = plt.figure(figsize=(w_in, h_in))
        fig.patch.set_facecolor(pg["paper_color"])
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, page_w)
        ax.set_ylim(page_h, 0)
        ax.axis("off")
        ax.set_facecolor(pg["paper_color"])
        dwg = svgwrite.Drawing(filename=f"{out}-page-{pn}.svg", size=(f"{w_in}in", f"{h_in}in"))
        dwg.viewbox(width=page_w, height=page_h)
        dwg.add(dwg.rect(insert=(0, 0), size=(page_w, page_h), fill=pg["paper_color"]))

        if pg["ruled_lines"]:
            ry = mt_u + 0.6 * line_h + 0.3 * base_line_h
            while ry < page_h - mb_u * 0.5:
                ax.plot([0, page_w], [ry, ry], color=pg["rule_color"], lw=pg["rule_thickness"], zorder=0)
                dwg.add(dwg.line((0, ry), (page_w, ry), stroke=pg["rule_color"], stroke_width=rule_u))
                ry += line_h
        if pg["margin_line"]:
            mx = ml_u * 0.85
            ax.plot([mx, mx], [0, page_h], color=pg["margin_line_color"], lw=1.0, zorder=0)
            dwg.add(dwg.line((mx, 0), (mx, page_h), stroke=pg["margin_line_color"], stroke_width=1.0 / 72 * upi))

        for segs, base_y, x_off in page:
            ymid = float(np.median(np.concatenate([s[:, 1] for s in segs])))
            for s in segs:
                x = s[:, 0] + ml_u + x_off
                yy = s[:, 1] - ymid + base_y
                if len(s) == 1:
                    x, yy = np.r_[x, x + 0.5], np.r_[yy, yy]
                ax.plot(x, yy, color=pen["color"], linewidth=stroke_pt,
                        solid_capstyle="round", solid_joinstyle="round")
                d = "M{:.2f},{:.2f} ".format(x[0], yy[0]) + " ".join(
                    "L{:.2f},{:.2f}".format(a, b) for a, b in zip(x[1:], yy[1:]))
                dwg.add(dwg.path(d).stroke(color=pen["color"], width=stroke_u,
                                           linecap="round", linejoin="round").fill("none"))

        if pg["page_numbers"]:
            label = pg["page_number_format"].format(n=pn, total=total)
            ax.text(page_w / 2, page_h - mb_u / 2, label, ha="center", va="center",
                    fontsize=9, color=pg["page_number_color"])
            dwg.add(dwg.text(label, insert=(page_w / 2, page_h - mb_u / 2), text_anchor="middle",
                             font_size=9 / 72 * upi, fill=pg["page_number_color"],
                             font_family="sans-serif"))

        if "svg" in formats:
            dwg.save()
        if "png" in formats:
            fig.savefig(f"{out}-page-{pn}.png", dpi=op["dpi"], facecolor=pg["paper_color"])
        if pdf:
            pdf.savefig(fig, facecolor=pg["paper_color"])
        plt.close(fig)
        print(f"  page {pn} of {total} done")
    if pdf:
        pdf.close()
    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Write a text file as handwritten pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              python handwrite.py examples/letter.txt
              python handwrite.py my_text.txt --out output/my_page --settings examples/settings_notebook.json
              python handwrite.py my_text.txt --set page.size=a4 --set text.align=left --set pen.color=black
              python handwrite.py my_text.txt --redo 4,8 --candidates 5
              python handwrite.py my_text.txt --pick 4:2,8:3
              python handwrite.py my_text.txt --keep-writing --set page.ruled_lines=true
              python handwrite.py --preview-styles
        """))
    ap.add_argument("textfile", nargs="?", help="plain text file to write")
    ap.add_argument("--out", default=None, help="output name without extension (default: output/<textfile name>)")
    ap.add_argument("--settings", default=None, help="JSON settings file (see examples/)")
    ap.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                    help="change one setting, e.g. --set page.size=a4 (can be repeated)")
    ap.add_argument("--redo", default="", help="line numbers to write again, e.g. 4,8 (see <out>.lines.txt)")
    ap.add_argument("--candidates", type=int, default=1, help="with --redo: write N versions of each line to choose from")
    ap.add_argument("--pick", default="", help="use chosen versions, e.g. 4:2,8:3 (line:version)")
    ap.add_argument("--keep-writing", action="store_true",
                    help="reuse the handwriting from the last run and only redraw the page (for changing page settings)")
    ap.add_argument("--preview-styles", action="store_true", help="save a sheet showing all 13 handwriting styles")
    ap.add_argument("--show-settings", action="store_true", help="print all settings with their current values")
    args = ap.parse_args()

    settings = load_settings(args.settings, args.set)
    if args.show_settings:
        print(json.dumps(settings, indent=2))
        return

    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
    if settings["output"]["seed"] is not None:
        os.environ["HANDWRITING_SEED"] = str(int(settings["output"]["seed"]))
        np.random.seed(int(settings["output"]["seed"]))

    import warnings
    warnings.filterwarnings("ignore")
    import logging
    logging.getLogger("tensorflow").setLevel(logging.ERROR)
    from handwriting_synthesis import drawing
    from handwriting_synthesis.hand import Hand

    pen, tx = settings["pen"], settings["text"]
    if not 0 <= int(pen["style"]) <= 12:
        sys.exit("pen.style must be between 0 and 12")

    if args.preview_styles:
        print("Loading handwriting model...")
        hand = Hand()
        os.makedirs("output", exist_ok=True)
        preview_styles(hand, drawing, "output/styles.png",
                       "Hello, this is how my handwriting looks today.", float(pen["neatness"]))
        return

    if not args.textfile:
        ap.error("please give a text file, e.g. python handwrite.py examples/letter.txt")

    valid = set(drawing.alphabet)
    with open(args.textfile, encoding="utf-8") as f:
        source = f.read()
    chars = int(tx["chars_per_line"])
    if chars > 60:
        print("note: chars_per_line above 60 makes the handwriting less reliable; using 60")
        chars = 60
    rows = layout(source, chars, tx["align"], valid)
    text_rows = [r for r in rows if r[0] == "text"]
    if not text_rows:
        sys.exit("The text file has nothing to write.")
    lines = [r[1] for r in text_rows]

    out = args.out or os.path.join("output", os.path.splitext(os.path.basename(args.textfile))[0])
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    cache = out + ".strokes.npz"
    cand_file = out + ".candidates.npz"

    print("Loading handwriting model...")
    hand = Hand()

    strokes = [None] * len(lines)
    todo = list(range(len(lines)))
    if (args.redo or args.pick or args.keep_writing) and os.path.exists(cache):
        old = np.load(cache, allow_pickle=True)
        if list(old["lines"]) == lines:
            strokes = list(old["strokes"])
            todo = []
        else:
            print("note: the text changed since the last run, so every line is written again")
    if args.redo and not todo:
        todo = [int(n) - 1 for n in args.redo.split(",") if n.strip()]
        if any(not 0 <= j < len(lines) for j in todo):
            sys.exit(f"--redo line numbers must be between 1 and {len(lines)}")

    if args.pick:
        if not os.path.exists(cand_file):
            sys.exit("No saved versions found. Run with --redo ... --candidates N first.")
        cands = np.load(cand_file, allow_pickle=True)["cands"].item()
        for item in args.pick.split(","):
            ln, opt = (int(v) for v in item.split(":"))
            if (ln, opt) not in cands:
                sys.exit(f"Version {ln}:{opt} does not exist in the last candidates sheet.")
            strokes[ln - 1] = cands[(ln, opt)]

    if args.redo and args.candidates > 1:
        jobs = [j for j in todo for _ in range(args.candidates)]
        print(f"Writing {args.candidates} versions of line(s) {args.redo}...")
        results = generate(hand, lines, jobs, int(pen["style"]), float(pen["neatness"]))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        cands, counter = {}, {}
        fig, axs = plt.subplots(len(results), 1, figsize=(10, 1.1 * len(results)))
        for ax, (j, s) in zip(np.atleast_1d(axs), results):
            counter[j] = counter.get(j, 0) + 1
            cands[(j + 1, counter[j])] = s
            for seg in split_strokes(to_coords(s, drawing)):
                ax.plot(seg[:, 0], -seg[:, 1], color="#1a2a6c", lw=1)
            ax.set_title(f"{j + 1}:{counter[j]}", loc="left", fontsize=10, fontweight="bold")
            ax.axis("off")
            ax.set_aspect("equal")
        fig.tight_layout()
        fig.savefig(out + "-candidates.png", dpi=70)
        np.savez(cand_file, cands=np.array(cands, dtype=object))
        print(f"\nSaved {out}-candidates.png")
        print(f"Look at it, then keep the best ones with:  --pick LINE:VERSION,LINE:VERSION")
        return

    if todo:
        print(f"Writing {len(todo)} line(s)...")
        for j, s in generate(hand, lines, todo, int(pen["style"]), float(pen["neatness"])):
            strokes[j] = s

    arr = np.empty(len(strokes), dtype=object)
    arr[:] = strokes
    np.savez(cache, lines=np.array(lines, dtype=object), strokes=arr)
    with open(out + ".lines.txt", "w", encoding="utf-8") as f:
        f.writelines(f"{n + 1:3d}  {ln}\n" for n, ln in enumerate(lines))

    # measure and arrange every line
    segs_per_line = [trim_tail(split_strokes(to_coords(s, drawing)), ln) for s, ln in zip(strokes, lines)]
    widths = [line_width(sg) for sg in segs_per_line]
    full = [w for w, r in zip(widths, text_rows) if not r[4]] or widths
    target = float(np.percentile(full, 90))

    unit = UNIT_TO_INCH.get(settings["page"]["unit"], 1.0)
    pg = settings["page"]
    size = PAGE_SIZES.get(pg["size"], (pg["width"] * unit, pg["height"] * unit))
    w_in = size[1] if pg["orientation"] == "landscape" else size[0]
    usable_in = w_in - (pg["margin_left"] + pg["margin_right"]) * unit
    indent_u = float(tx["indent"]) * unit / usable_in * target if usable_in > 0 else 0.0

    placed, k = [], 0
    for row in rows:
        if row[0] != "text":
            placed.append(row)
            continue
        _, ln, align, first, last = row
        sg = segs_per_line[k]
        k += 1
        x_off = indent_u if first else 0.0
        avail = target - x_off
        w = line_width(sg)
        if w > avail:
            sg = scale_x(sg, avail / w)
            w = avail
        if align == "justify" and (not last or tx["justify_last_line"]):
            sg = justify(sg, ln.count(" "), avail)
        elif align == "right":
            x_off += avail - w
        elif align == "center":
            x_off += (avail - w) / 2
        placed.append(("text", sg, x_off))

    print("Drawing pages...")
    total = render(placed, settings, out, target)
    made = ", ".join(f for f in settings["output"]["formats"])
    print(f"\nDone: {total} page(s) saved as {out} ({made})")
    print(f"Line numbers for --redo are listed in {out}.lines.txt")


if __name__ == "__main__":
    main()
