# BLA / BoldSign — Font Size + Signature Placement Fix

## TL;DR

Both bugs are confirmed and root-caused. Neither is in BoldSign's API — both are in
`fill_bla_pdf()` in `generate_bla.py`.

1. **Font size bug**: `add_redact_annot()`'s text-replacement mode auto-shrinks text
   to fit the redaction rectangle. Our rectangles (taken from the original placeholder's
   bbox) are only ~1pt taller than the font size, so longer replacement strings
   (business name, address, website) get auto-shrunk and vertically squished toward
   the bottom of the box. Short replacements ($500,000, 10%) barely shrink, which is
   why the bug looked "inconsistent" rather than uniform.
2. **Signature/date placement**: hardcoded `Bounds` in `generate_bla.py` were
   hand-measured off a screenshot and are off by ~8pt vertically from the actual
   underscore-line position in the PDF. Not catastrophic, but not exact, and will
   drift again if the template changes.

Reproduced both locally against the actual `template.pdf` and confirmed the fix
below resolves the font issue completely. Sending the corrected `fill_bla_pdf()`
function — drop-in replacement, no other part of the script needs to change.

---

## Root cause #1: font size / sunken text

`page.add_redact_annot(rect, text, fontsize=9.5, ...)` does **not** draw text at
exactly `fontsize` the way `insert_text()` does. PyMuPDF treats the rect as a
text box and auto-fits/shrinks the text to guarantee it fits inside the box's
exact width and height. Our boxes come straight from `page.search_for()` on the
original `{{placeholder}}` string, so the box height is just the original text's
line height (~10.5pt for 9.5pt font) — almost zero padding. Any replacement
string that's visually wider than the placeholder triggers shrink-to-fit, and the
shrunk glyphs anchor toward the bottom of the box, which is what reads as
"small and sitting below the line."

Confirmed by extracting the actual spans from `template.pdf`:

```
business_name      → TimesNewRomanPS-BoldMT, 9.5pt
business_address   → TimesNewRomanPSMT, 9.5pt
business_website   → TimesNewRomanPSMT, 9.5pt
purchase_price      → TimesNewRomanPSMT, 9.5pt
commission_percentage → TimesNewRomanPS-BoldMT, 9.5pt
```

Font detection in the current script is correct. The bug is purely in how the
*replacement* text gets drawn into the redaction box.

### Fix

Use `add_redact_annot()` only to blank out the placeholder (no replacement text
passed), then draw the replacement separately with `insert_text()`, which respects
`fontsize` exactly and does not auto-shrink. `insert_text()`'s origin point is the
**baseline**, not the box top, so the y-coordinate needs a small descender
correction (~0.2 × fontsize works well for Times New Roman) instead of using the
box's top or bottom directly.

```python
def fill_bla_pdf(template_values: dict[str, str], output_path: str) -> str:
    """Fill the BLA PDF template with real values, return output path.

    Detects bold/regular font + size per placeholder from the source PDF,
    erases the placeholder via redaction, then draws the replacement text
    at the exact detected font size using insert_text() (NOT redaction's
    built-in text param, which auto-shrinks to fit the box and was causing
    small / vertically-sunken replacement text).
    """
    FONT_MAP = {
        "TimesNewRomanPSMT": "tiro",
        "TimesNewRomanPS-BoldMT": "tibo",
        "TimesNewRomanPS-ItalicMT": "tiri",
        "TimesNewRomanPS-BoldItalicMT": "tibi",
        "ArialMT": "helv",
        "Arial-BoldMT": "hebo",
    }

    doc = fitz.open(BLA_TEMPLATE_PDF)

    for page in doc:
        # 1. Detect font + size per placeholder (unchanged from before)
        placeholder_fonts: dict[str, tuple[str, float]] = {}
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if "lines" not in block:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    for m in re.finditer(r'\{\{(\w+)\}\}', span["text"]):
                        var_name = m.group(1)
                        if var_name in template_values and var_name not in placeholder_fonts:
                            pymupdf_font = FONT_MAP.get(span["font"], "tiro")
                            placeholder_fonts[var_name] = (pymupdf_font, span["size"])

        # 2. Redact — ERASE ONLY, no replacement text here.
        #    Collect insertion jobs to run after apply_redactions().
        insertion_jobs = []  # (rect, value, font_name, font_size)
        for var_name, value in template_values.items():
            if not value:
                continue
            search = f"{{{{{var_name}}}}}"
            instances = page.search_for(search)
            font_name, font_size = placeholder_fonts.get(var_name, ("tiro", 9.5))
            for inst in instances:
                page.add_redact_annot(inst, fill=(1, 1, 1))  # blank fill, no text
                insertion_jobs.append((inst, value, font_name, font_size))

        page.apply_redactions()

        # 3. Draw replacement text ourselves at the correct baseline.
        #    insert_text()'s origin is the baseline; the span bbox's bottom
        #    edge (rect.y1) sits slightly below the baseline (descender),
        #    so subtract ~0.2 * fontsize to land exactly on the line.
        for rect, value, font_name, font_size in insertion_jobs:
            baseline_y = rect.y1 - (font_size * 0.2)
            page.insert_text(
                (rect.x0, baseline_y),
                value,
                fontname=font_name,
                fontsize=font_size,
                color=(0, 0, 0),
            )

    doc.save(output_path)
    doc.close()

    # Verify no placeholders remain (unchanged)
    doc2 = fitz.open(output_path)
    remaining = []
    for i, page in enumerate(doc2):
        found = re.findall(r'\{\{\w+\}\}', page.get_text())
        remaining.extend([(i + 1, f) for f in found])
    doc2.close()

    if remaining:
        print(f"⚠️  Unfilled placeholders: {remaining}")
    else:
        print("✅ All placeholders filled")

    return output_path
```

**Tested locally** against the real `template.pdf` with the `johnny test rocket`
dry-run values — confirmed all fields (business name, address, website, price,
commission, seller name/title) now render at full 9.5pt, correctly bold/regular,
sitting on the line, matching surrounding body text exactly. No regressions on
pages 1–3.

---

## Root cause #2: signature / date field placement

The hardcoded bounds in `generate_bla.py`:

```python
SELLER_SIG_BOUNDS  = {"X": 288, "Y": 405, "Width": 140, "Height": 25}
SELLER_DATE_BOUNDS = {"X": 310, "Y": 355, "Width": 135, "Height": 15}
BROKER_SIG_BOUNDS  = {"X": 54,  "Y": 405, "Width": 140, "Height": 25}
BROKER_DATE_BOUNDS = {"X": 76,  "Y": 355, "Width": 135, "Height": 15}
```

were hand-measured. Checked against the actual rendered PDF, the real positions
of the "Date:" and "Signature:" labels + underscore lines on page 3 are:

```
Broker Date underscore:      x=54,  y=358.3–368.8
Broker Signature label:      x=54,  y=392.2–402.7
Broker Signature underscore: x=54,  y=413.1–423.6
Seller Date underscore:      x=288, y=358.3–368.8
Seller Signature label:      x=288, y=392.2–402.7
Seller Signature underscore: x=288, y=413.1–423.6
```

X-axis assignment is correct (Seller = right column = x≈288, Broker = left column
= x≈54 — matches the script). The Y values are close but not exact: the script's
signature box is `Y: 405, H: 25` (spans 405–430) vs. the actual underscore at
413.1–423.6 — about 8pt high. Probably the source of "not lined up where they
should be."

### Fix: derive bounds from the PDF instead of hardcoding them

Rather than re-measuring by hand again (which will just drift the next time the
template changes), pull the label/underscore positions directly out of the PDF
at fill-time, the same way we already do for the placeholder fonts. This makes
the signature/date placement self-correcting if the template is ever edited.

```python
def get_signature_bounds(template_pdf_path: str) -> dict[str, dict]:
    """Derive signature/date field bounds from the actual 'Date:'/'Signature:'
    label positions on the signature page, instead of hand-measured constants.
    Assumes Broker is left column, Seller is right column (page 3 layout)."""
    doc = fitz.open(template_pdf_path)
    page = doc[2]  # signature page — adjust if layout changes
    blocks = page.get_text("dict")["blocks"]

    labels = {}  # (text_prefix, x_position) -> bbox
    for block in blocks:
        if "lines" not in block:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                txt = span["text"]
                if txt.startswith("Date:"):
                    col = "broker" if span["bbox"][0] < 200 else "seller"
                    labels[f"{col}_date"] = span["bbox"]
                elif txt.strip() == "Signature:" or txt.startswith("Signature:"):
                    col = "broker" if span["bbox"][0] < 200 else "seller"
                    labels[f"{col}_sig_label"] = span["bbox"]
                elif set(txt.strip()) == {"_"} or (txt.strip().startswith("_") and len(txt.strip()) > 5):
                    col = "broker" if span["bbox"][0] < 200 else "seller"
                    # underscore line right after the signature label = the sig line
                    if f"{col}_sig_label" in labels and f"{col}_sig_line" not in labels:
                        labels[f"{col}_sig_line"] = span["bbox"]

    doc.close()

    def bounds_for(line_bbox, width=140, height=25):
        x0, y0, x1, y1 = line_bbox
        return {"X": round(x0, 1), "Y": round(y0 - 2, 1), "Width": width, "Height": height}

    return {
        "seller_sig": bounds_for(labels["seller_sig_line"]),
        "seller_date": bounds_for(labels["seller_date"], width=135, height=15),
        "broker_sig": bounds_for(labels["broker_sig_line"]),
        "broker_date": bounds_for(labels["broker_date"], width=135, height=15),
    }
```

Call this once and feed the results into `build_multipart_body()` instead of the
hardcoded `SELLER_SIG_BOUNDS` etc. If the underscore-line detection above proves
fragile against the live template (text extraction sometimes splits underscore
runs unpredictably), the fallback is to just nudge the existing hardcoded Y
values down by ~8pt (`Y: 405` → `Y: 413`) as a quick patch — but the derived
version is the right long-term fix since it survives template edits.

---

## What to verify after patching

1. Run `generate_bla.py "Contact Name" --dry-run --preview` and check the
   page-3 PNG preview — signature/date boxes should sit directly on the
   underscore lines, not above or below.
2. Send one more live test to `cabreland@gmail.com` and open it in BoldSign
   to confirm the signature field (the actual clickable BoldSign widget, not
   just our drawn text) lines up with the underscore on the rendered PDF.
3. Spot check a longer business name (something close to the column width)
   to make sure `insert_text()` doesn't run past the line — `insert_text()`
   doesn't auto-wrap, so a very long business name could overflow off the
   page edge in a way the old shrink-to-fit redaction approach (accidentally)
   prevented. Worth adding a max-width check / font-size step-down only as a
   safety net, not as the primary sizing mechanism.
