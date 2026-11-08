"""Turn OCR lines into paragraph/message blocks, and find where a claim's span sits on screen.

Local context: geometric grouping (lines close vertically and aligned horizontally form one block) and
span -> line boxes mapping with proportional horizontal trimming.
Global context: without grouping, chat UIs break claims across usernames, timestamps and wrapped lines;
`locate_span` is how the overlay knows which rectangles to draw for a verdict.
"""

from __future__ import annotations

from factcheck.capture.ocr import OcrLine
from factcheck.models import Box, TextBlock
from factcheck.pipeline.extract import align_span


def merge_rows(lines: list[OcrLine]) -> list[OcrLine]:
    """Merge OCR lines that sit on the same row and nearly touch (e.g. 'mike_92' + 'Today at 14:02'):
    Windows OCR splits a visual line where there is a wide gap, which would orphan a username from its message."""
    rows: list[OcrLine] = []
    for ln in sorted(lines, key=lambda ln: (ln.box.y, ln.box.x)):
        for i, row in enumerate(rows):
            b, r = ln.box, row.box
            same_row = abs((b.y + b.h / 2) - (r.y + r.h / 2)) < 0.5 * max(b.h, r.h)
            near = 0 <= b.x - (r.x + r.w) < 3 * max(b.h, r.h)
            if same_row and near:
                text = f"{row.text} {ln.text}"
                x0, y0 = r.x, min(r.y, b.y)
                x1, y1 = b.x + b.w, max(r.y + r.h, b.y + b.h)
                rows[i] = OcrLine(text=text, box=Box(x=x0, y=y0, w=x1 - x0, h=y1 - y0, text=text))
                break
        else:
            rows.append(ln)
    return rows


def group_lines(lines: list[OcrLine], prefix: str = "d") -> list[TextBlock]:
    """Group lines into blocks: next line joins the block if it starts within ~1.2 line heights below and
    its left edge is roughly aligned (or it overlaps horizontally). Reading order: top-to-bottom, left-to-right."""
    ordered = sorted(merge_rows(lines), key=lambda ln: (round(ln.box.y / 4), ln.box.x))
    blocks: list[list[OcrLine]] = []
    for line in ordered:
        placed = False
        for group in reversed(blocks[-6:]):                   # only recent groups can continue
            last = group[-1].box
            height = max(last.h, line.box.h, 1)
            gap = line.box.y - (last.y + last.h)
            aligned = abs(line.box.x - group[0].box.x) < 2.5 * height
            overlaps = line.box.x < last.x + last.w and line.box.x + line.box.w > last.x
            if -0.5 * height <= gap <= 1.2 * height and (aligned or overlaps):
                group.append(line)
                placed = True
                break
        if not placed:
            blocks.append([line])
    return [TextBlock(id=f"{prefix}{i}", text=" ".join(ln.text for ln in group), boxes=[ln.box for ln in group])
            for i, group in enumerate(blocks)]


def locate_span(block: TextBlock, span: str) -> list[Box]:
    """Screen boxes covering `span` inside `block` (empty if not found). Block text = box texts joined by ' '."""
    exact = align_span(span, block.text)
    if exact is None:
        return []
    start = block.text.find(exact)
    end = start + len(exact)
    out, offset = [], 0
    for box in block.boxes:
        line_start, line_end = offset, offset + len(box.text)
        lo, hi = max(start, line_start), min(end, line_end)
        if lo < hi and box.text:                              # this line holds part of the span
            frac0 = (lo - line_start) / len(box.text)
            frac1 = (hi - line_start) / len(box.text)
            out.append(Box(x=box.x + box.w * frac0, y=box.y, w=max(box.w * (frac1 - frac0), 4), h=box.h, text=box.text[lo - line_start:hi - line_start]))
        offset = line_end + 1                                 # +1 for the joining space
    return out


def locate_in_blocks(blocks: list[TextBlock], span: str, preferred_id: str | None = None) -> list[Box]:
    """Search the preferred block first, then all blocks (the layout may have changed since extraction)."""
    order = sorted(blocks, key=lambda b: b.id != preferred_id)
    for block in order:
        boxes = locate_span(block, span)
        if boxes:
            return boxes
    return []
