"""
Generate & send a Business Listing Agreement (BLA) for a seller contact.

Flow (zero-touch):
1. Search GHL for the contact by name/email
2. Pull all custom fields needed for the BLA
3. Fill the BLA PDF template (replace {{placeholders}} with real values)
4. Send the filled PDF to BoldSign with Signature + Date fields
5. Seller signs → Broker (Jarrod) counter-signs → Jack + Chris CC'd

Usage:
    uv run python skills/deal_management/scripts/generate_bla.py "Contact Name or Email"

Optional flags:
    --dry-run    Fill PDF but don't send to BoldSign (saves to /tmp)
    --preview    Also generate a PNG preview of page 3 (signature page)
"""

import asyncio
import base64
import json
import re
import sys
import uuid
from datetime import datetime, timedelta

import fitz  # PyMuPDF

# ── Config ──────────────────────────────────────────────────────────────────
GHL_LOCATION_ID = "VrIFtlCW5GvoCpf0Spte"
GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_HEADERS = {"Version": "2021-07-28"}

BLA_TEMPLATE_PDF = "skills/deal_management/references/business_listing_agreement_template_june_2026.pdf"
BOLDSIGN_BRAND_ID = "3ec989d6-4615-4f72-82c3-64863d0a123e"

# GHL custom field ID → logical key mapping
GHL_FIELD_ID_MAP = {
    "JVlSWIjaE9Zw3Nm7WyUP": "contact.business_name",
    "wwA1IyZoZvId5LhTysK8": "contact.business_address",
    "gBLRxnvDN0Im18Z434Zp": "contact.business_website",
    "x4zFeTQonNTUPMBNU6Pf": "contact.purchase_price",
    "hHD4Gk7WC2b3ogFNLaaq": "contact.commission_",
    "pZewuPB8FSoMcwPazNt1": "contact.seller_title",
}

BROKER = {
    "name": "Jarrod Swanger",
    "email": "jarrod@exclusivebusinessbrokers.com",
}
CC_RECIPIENTS = [
    {"email": "jack@exclusivebusinessbrokers.com", "name": "Jack Opsahl"},
    # Chris excluded — BoldSign sender identity can't be CC'd
]

# Signature/date field bounds are now DERIVED from the actual template PDF
# (see get_signature_bounds() below) instead of hand-measured constants.
# This survives template edits — if the BLA layout shifts, bounds shift with it.
SIGNATURE_PAGE_INDEX = 2  # page 3, 0-indexed


# ── GHL Helpers ─────────────────────────────────────────────────────────────

async def search_contact(query: str) -> dict | None:
    from sdk.tools.pd_highlevel_oauth import pd_highlevel_oauth_proxy_get
    result = await pd_highlevel_oauth_proxy_get(
        url=f"{GHL_API_BASE}/contacts/",
        query_params={"locationId": GHL_LOCATION_ID, "query": query},
        headers=GHL_HEADERS,
    )
    parsed = json.loads(result.get("content", "{}"))
    body = parsed.get("body", parsed)
    contacts = body.get("contacts", [])
    return contacts[0] if contacts else None


async def get_contact(contact_id: str) -> dict:
    from sdk.tools.pd_highlevel_oauth import pd_highlevel_oauth_proxy_get
    result = await pd_highlevel_oauth_proxy_get(
        url=f"{GHL_API_BASE}/contacts/{contact_id}",
        headers=GHL_HEADERS,
    )
    parsed = json.loads(result.get("content", "{}"))
    body = parsed.get("body", parsed)
    return body.get("contact", body)


# ── PDF Fill ────────────────────────────────────────────────────────────────

def extract_ghl_fields(contact: dict) -> dict[str, str]:
    """Extract custom field values from a GHL contact."""
    cf = contact.get("customFields", contact.get("customField", []))
    field_map = {}
    if isinstance(cf, list):
        for f in cf:
            fid = f.get("id", "")
            key = f.get("key") or GHL_FIELD_ID_MAP.get(fid, fid)
            val = f.get("value", "")
            if isinstance(val, list):
                val = val[0] if len(val) == 1 else ", ".join(val)
            field_map[key] = val
    elif isinstance(cf, dict):
        field_map = cf
    return field_map


def build_template_values(contact: dict, field_map: dict) -> dict[str, str]:
    """Map GHL fields → BLA template {{placeholder}} values."""
    now = datetime.now()
    end_date = now + timedelta(days=122)  # ~4 months

    # Format purchase price
    raw_price = str(field_map.get("contact.purchase_price", "") or "")
    if raw_price:
        try:
            num = float(raw_price.replace(",", "").replace("$", ""))
            raw_price = f"${num:,.0f}"
        except ValueError:
            if not raw_price.startswith("$"):
                raw_price = f"${raw_price}"

    # Commission — append % if not present
    raw_commission = str(field_map.get("contact.commission_", "") or "")
    if raw_commission and "%" not in raw_commission:
        raw_commission = f"{raw_commission}%"

    # Seller name
    first = contact.get("firstName", "")
    last = contact.get("lastName", "")
    seller_name = f"{first} {last}".strip()

    return {
        "start_date": now.strftime("%B %d, %Y"),
        "end_date": end_date.strftime("%B %d, %Y"),
        "business_name": field_map.get("contact.business_name", "") or contact.get("companyName", "") or "",
        "business_address": field_map.get("contact.business_address", "") or "",
        "business_website": field_map.get("contact.business_website", "") or "",
        "purchase_price_formatted": raw_price,
        "commission_percentage": raw_commission,
        "seller_name": seller_name,
        "seller_title": field_map.get("contact.seller_title", "") or "",
    }


def get_signature_bounds(template_pdf_path: str) -> dict[str, dict]:
    """Derive signature/date field bounds from the actual 'Date:'/'Signature:'
    label and underscore-line positions on the signing page, instead of
    hand-measured constants. Assumes Broker = left column, Seller = right
    column (matches current page-3 layout). Re-derives automatically if the
    template is ever edited — no more manual re-measuring from screenshots.
    """
    doc = fitz.open(template_pdf_path)
    page = doc[SIGNATURE_PAGE_INDEX]
    blocks = page.get_text("dict")["blocks"]

    labels: dict[str, tuple[float, float, float, float]] = {}
    for block in blocks:
        if "lines" not in block:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                txt = span["text"]
                if txt.startswith("Date:"):
                    col = "broker" if span["bbox"][0] < 200 else "seller"
                    labels[f"{col}_date"] = span["bbox"]
                elif txt.strip().startswith("Signature:"):
                    col = "broker" if span["bbox"][0] < 200 else "seller"
                    labels[f"{col}_sig_label"] = span["bbox"]
                elif txt.strip().startswith("_") and len(txt.strip()) > 5:
                    col = "broker" if span["bbox"][0] < 200 else "seller"
                    # First underscore run after the signature label = the sig line
                    if f"{col}_sig_label" in labels and f"{col}_sig_line" not in labels:
                        labels[f"{col}_sig_line"] = span["bbox"]
    doc.close()

    required = ["broker_date", "broker_sig_line", "seller_date", "seller_sig_line"]
    missing = [k for k in required if k not in labels]
    if missing:
        raise RuntimeError(
            f"Could not locate signature page anchors: {missing}. "
            f"Template layout may have changed — check SIGNATURE_PAGE_INDEX "
            f"and the label text on the signing page."
        )

    def bounds_for(bbox, width: float, height: float) -> dict:
        x0, y0, _, _ = bbox
        return {"X": round(x0, 1), "Y": round(y0 - 2, 1), "Width": width, "Height": height}

    return {
        "seller_sig": bounds_for(labels["seller_sig_line"], 140, 25),
        "seller_date": bounds_for(labels["seller_date"], 135, 15),
        "broker_sig": bounds_for(labels["broker_sig_line"], 140, 25),
        "broker_date": bounds_for(labels["broker_date"], 135, 15),
    }


def fill_bla_pdf(template_values: dict[str, str], output_path: str) -> str:
    """Fill the BLA PDF template with real values, return output path.

    Detects bold/regular font + size per placeholder from the source PDF,
    erases the placeholder via redaction, then draws the replacement text
    at the exact detected font size using insert_text(). We do NOT pass the
    replacement text to add_redact_annot()'s built-in text param — that mode
    auto-shrinks text to fit the redaction box, which was causing small,
    vertically-sunken replacement text (the box height is only ~1pt taller
    than the font, so longer replacement strings got squeezed).
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
        # First: detect font + size used for each placeholder on this page
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

        # Second: redact to ERASE ONLY (no replacement text passed to BoldSign-
        # style redaction — that's what auto-shrinks). Queue insertion jobs to
        # run after apply_redactions() commits the erase.
        insertion_jobs: list[tuple[fitz.Rect, str, str, float]] = []
        for var_name, value in template_values.items():
            if not value:
                continue
            search = f"{{{{{var_name}}}}}"
            instances = page.search_for(search)
            font_name, font_size = placeholder_fonts.get(var_name, ("tiro", 9.5))
            for inst in instances:
                page.add_redact_annot(inst, fill=(1, 1, 1))
                insertion_jobs.append((inst, value, font_name, font_size))

        page.apply_redactions()

        # Third: draw replacement text ourselves at the exact font size,
        # anchored to the correct baseline. insert_text()'s origin point IS
        # the baseline (not the box top), so we offset up from the box's
        # bottom edge by ~0.2 * fontsize to compensate for the descender gap.
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

    # Verify no placeholders remain
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


# ── BoldSign Send (Direct Document) ────────────────────────────────────────

def build_multipart_body(
    pdf_path: str,
    title: str,
    message: str,
    seller_name: str,
    seller_email: str,
    sig_bounds: dict[str, dict],
) -> tuple[str, str]:
    """Build multipart/form-data body for BoldSign /v1/document/send.

    sig_bounds comes from get_signature_bounds() — derived from the actual
    template PDF rather than hardcoded constants.

    Returns (content_type, base64_body).
    """
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    boundary = f"----BoldSignBoundary{uuid.uuid4().hex[:16]}"
    parts = []

    def add(name, value):
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}'
        )

    # Document settings
    add("Title", title)
    add("Message", message)
    add("BrandId", BOLDSIGN_BRAND_ID)
    add("EnableSigningOrder", "true")
    add("EnableAutoReminder", "true")
    add("ReminderDays", "3")
    add("ReminderCount", "3")
    add("ExpiryDays", "30")

    # Signer 1: Seller
    add("Signers[0][Name]", seller_name)
    add("Signers[0][EmailAddress]", seller_email)
    add("Signers[0][SignerOrder]", "1")
    add("Signers[0][SignerType]", "Signer")

    # Seller signature field
    add("Signers[0][FormFields][0][FieldType]", "Signature")
    add("Signers[0][FormFields][0][PageNumber]", "3")
    add("Signers[0][FormFields][0][Id]", "SellerSignature")
    add("Signers[0][FormFields][0][IsRequired]", "true")
    for k, v in sig_bounds["seller_sig"].items():
        add(f"Signers[0][FormFields][0][Bounds][{k}]", str(v))

    # Seller date field
    add("Signers[0][FormFields][1][FieldType]", "DateSigned")
    add("Signers[0][FormFields][1][PageNumber]", "3")
    add("Signers[0][FormFields][1][Id]", "SellerDate")
    add("Signers[0][FormFields][1][IsRequired]", "true")
    for k, v in sig_bounds["seller_date"].items():
        add(f"Signers[0][FormFields][1][Bounds][{k}]", str(v))

    # Signer 2: Broker (Jarrod)
    add("Signers[1][Name]", BROKER["name"])
    add("Signers[1][EmailAddress]", BROKER["email"])
    add("Signers[1][SignerOrder]", "2")
    add("Signers[1][SignerType]", "Signer")

    # Broker signature field
    add("Signers[1][FormFields][0][FieldType]", "Signature")
    add("Signers[1][FormFields][0][PageNumber]", "3")
    add("Signers[1][FormFields][0][Id]", "BrokerSignature")
    add("Signers[1][FormFields][0][IsRequired]", "true")
    for k, v in sig_bounds["broker_sig"].items():
        add(f"Signers[1][FormFields][0][Bounds][{k}]", str(v))

    # Broker date field
    add("Signers[1][FormFields][1][FieldType]", "DateSigned")
    add("Signers[1][FormFields][1][PageNumber]", "3")
    add("Signers[1][FormFields][1][Id]", "BrokerDate")
    add("Signers[1][FormFields][1][IsRequired]", "true")
    for k, v in sig_bounds["broker_date"].items():
        add(f"Signers[1][FormFields][1][Bounds][{k}]", str(v))

    # CC recipients
    for i, cc in enumerate(CC_RECIPIENTS):
        add(f"CC[{i}][EmailAddress]", cc["email"])
        add(f"CC[{i}][Name]", cc["name"])

    # Build text portion
    text_body = "\r\n".join(parts) + "\r\n"

    # File part
    file_header = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="Files"; filename="Business_Listing_Agreement.pdf"\r\n'
        f'Content-Type: application/pdf\r\n\r\n'
    )
    closing = f'\r\n--{boundary}--\r\n'

    full_body = text_body.encode() + file_header.encode() + pdf_bytes + closing.encode()
    body_b64 = base64.b64encode(full_body).decode()

    content_type = f"multipart/form-data; boundary={boundary}"
    return content_type, body_b64


async def send_to_boldsign(pdf_path: str, title: str, message: str,
                           seller_name: str, seller_email: str,
                           sig_bounds: dict[str, dict]) -> dict:
    """Send filled BLA PDF to BoldSign for e-signatures."""
    from sdk.tools.pd_boldsign import pd_boldsign_proxy_post

    content_type, body_b64 = build_multipart_body(
        pdf_path, title, message, seller_name, seller_email, sig_bounds
    )

    result = await pd_boldsign_proxy_post(
        "https://api.boldsign.com/v1/document/send",
        headers={"Content-Type": content_type},
        body_base64=body_b64,
        timeout_ms=120000,
    )
    return result


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run python generate_bla.py 'Contact Name or Email' [--dry-run] [--preview]")
        sys.exit(1)

    query = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    preview = "--preview" in sys.argv

    print(f"🔍 Searching GHL for: {query}")

    # 1. Find contact
    contact = await search_contact(query)
    if not contact:
        print(f"❌ NO_CONTACT_FOUND: {query}")
        sys.exit(1)

    contact_id = contact.get("id")
    name = f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
    email = contact.get("email", "")
    print(f"✅ Found: {name} ({email}) — ID: {contact_id}")

    # Get full details
    contact = await get_contact(contact_id)

    # 2. Extract fields and build template values
    field_map = extract_ghl_fields(contact)
    template_values = build_template_values(contact, field_map)

    print(f"\n📋 Template values:")
    missing = []
    for k, v in template_values.items():
        if v:
            print(f"  ✅ {k:30s} = {v}")
        else:
            print(f"  ⚠️  {k:30s} = (empty)")
            missing.append(k)

    # 3. Fill the PDF
    biz_name = template_values.get("business_name") or name
    output_pdf = f"/tmp/BLA_{biz_name.replace(' ', '_')}.pdf"
    print(f"\n📄 Filling BLA PDF...")
    fill_bla_pdf(template_values, output_pdf)
    print(f"   Saved to: {output_pdf}")

    # 4. Preview if requested
    if preview:
        doc = fitz.open(output_pdf)
        pix = doc[2].get_pixmap(dpi=150)
        preview_path = output_pdf.replace(".pdf", "_page3.png")
        pix.save(preview_path)
        doc.close()
        print(f"   Preview: {preview_path}")

    # 5. Send to BoldSign (unless dry run)
    if dry_run:
        print("\n🏁 DRY RUN — PDF filled but not sent to BoldSign")
        result = {"dry_run": True, "pdf_path": output_pdf, "missing_fields": missing}
    elif not email:
        print("❌ No email on contact — cannot send BoldSign document")
        sys.exit(1)
    else:
        title = f"Business Listing Agreement — {biz_name}"
        message = f"Please review and sign the Business Listing Agreement for {biz_name}."
        sig_bounds = get_signature_bounds(BLA_TEMPLATE_PDF)
        print(f"\n📤 Sending to BoldSign: {title}")
        result = await send_to_boldsign(output_pdf, title, message, name, email, sig_bounds)
        print(f"   BoldSign response: {json.dumps(result, indent=2, default=str)[:1000]}")

    # Output
    output = {
        "contact_id": contact_id,
        "contact_name": name,
        "contact_email": email,
        "business_name": biz_name,
        "template_values": template_values,
        "missing_fields": missing,
        "pdf_path": output_pdf,
        "boldsign": result if not dry_run else None,
    }
    print(f"\n📦 RESULT_JSON: {json.dumps(output, default=str)}")


if __name__ == "__main__":
    asyncio.run(main())
