"""
Generate & send a Business Listing Agreement (BLA) for a seller contact.

Flow (template-based):
1. Search GHL for the contact by name/email
2. Pull all custom fields needed for the BLA
3. Send via BoldSign /v1/template/send — pre-fills mapped form fields
4. Seller signs → Broker (Jarrod) counter-signs → Jack CC'd

Template: 0dd97a31-c600-4b5a-abd1-356ea2ad8745 (Clean Template v2)
  - Fields mapped in BoldSign editor by Chris
  - Seller role (index 1): 10 fields (address, dates, company name x3, website, name, title, sig, date)
  - Broker role (index 2): 6 fields (price, commission, engagement dates, sig, date)

Usage:
    uv run python skills/deal_management/scripts/generate_bla.py "Contact Name or Email"

Optional flags:
    --dry-run    Show field values but don't send to BoldSign
"""

import asyncio
import base64
import json
import sys
import uuid
from datetime import datetime, timedelta

# ── Config ──────────────────────────────────────────────────────────────────
GHL_LOCATION_ID = "VrIFtlCW5GvoCpf0Spte"
GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_HEADERS = {"Version": "2021-07-28"}

BOLDSIGN_TEMPLATE_ID = "0dd97a31-c600-4b5a-abd1-356ea2ad8745"
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

# Template field ID → GHL value key mapping
# All fillable fields are on the Seller role (index 1).
# Broker role (index 2) only has Signature1 + DatePicker1 (auto-fill on sign).
SELLER_TEXTBOX_FIELDS = {
    "SellerAddress":      "business_address",
    "WebsiteAddress":     "business_website",
    "SellerCompanyName":  "business_name",     # page 1 "under the name '___'"
    "TextBox2":           "business_name",     # page 1 "Broker, and ___"
    "TextBox3":           "business_name",     # page 3 "SELLER: ___"
    "SellerName":         "seller_name",       # page 3
    "SellerTitle":        "seller_title",      # page 3
    "ListingPrice":       "purchase_price_formatted",  # section 4
    "TextBox1":           "commission_percentage",      # section 5
}
SELLER_DATE_FIELDS = {
    "EditableDate1":      "start_date",        # "entered into as of"
    "EngagementStartDate": "start_date",       # "commencing on"
    "AgreementEndDate":    "end_date",         # "ending at 11:59 p.m. on"
}


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


# ── Field Extraction ───────────────────────────────────────────────────────

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
    """Map GHL fields → BLA template values."""
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
        "start_date": now.strftime("%m/%d/%Y"),               # MM/dd/yyyy for BoldSign date fields
        "start_date_text": now.strftime("%B %d, %Y"),          # Long form for display
        "end_date": end_date.strftime("%m/%d/%Y"),
        "end_date_text": end_date.strftime("%B %d, %Y"),
        "business_name": field_map.get("contact.business_name", "") or contact.get("companyName", "") or "",
        "business_address": field_map.get("contact.business_address", "") or "",
        "business_website": field_map.get("contact.business_website", "") or "",
        "purchase_price_formatted": raw_price,
        "commission_percentage": raw_commission,
        "seller_name": seller_name,
        "seller_title": field_map.get("contact.seller_title", "") or "",
    }


# ── BoldSign Template Send ─────────────────────────────────────────────────

def build_template_send_body(
    seller_name: str,
    seller_email: str,
    template_values: dict[str, str],
) -> tuple[str, str]:
    """Build multipart/form-data for BoldSign /v1/template/send.

    Pre-fills all mapped form fields with GHL contact values.
    Signature and DateSigned fields are left for signers to complete.
    Note: templateId goes as a query parameter, NOT in the form body.

    Returns (content_type, base64_body).
    """
    boundary = f"----BoldSignBoundary{uuid.uuid4().hex[:16]}"
    parts = []

    def add(name, value):
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}'
        )

    # Document settings (templateId is a query param, not here)
    add("BrandId", BOLDSIGN_BRAND_ID)
    biz = template_values.get("business_name") or seller_name
    add("Title", f"Business Listing Agreement — {biz}")
    add("Message", f"Please review and sign the Business Listing Agreement for {biz}.")
    add("EnableSigningOrder", "true")
    add("EnableAutoReminder", "true")
    add("ReminderDays", "3")
    add("ReminderCount", "3")
    add("ExpiryDays", "30")

    # ── Role 0 = Seller (template index 1) ──
    add("Roles[0][RoleIndex]", "1")
    add("Roles[0][SignerName]", seller_name)
    add("Roles[0][SignerEmail]", seller_email)
    add("Roles[0][SignerOrder]", "1")

    seller_field_idx = 0

    # Textbox fields
    for field_id, value_key in SELLER_TEXTBOX_FIELDS.items():
        value = template_values.get(value_key, "")
        if value:
            add(f"Roles[0][ExistingFormFields][{seller_field_idx}][Id]", field_id)
            add(f"Roles[0][ExistingFormFields][{seller_field_idx}][Value]", value)
            seller_field_idx += 1

    # Editable date fields (MM/dd/yyyy format)
    for field_id, value_key in SELLER_DATE_FIELDS.items():
        value = template_values.get(value_key, "")
        if value:
            add(f"Roles[0][ExistingFormFields][{seller_field_idx}][Id]", field_id)
            add(f"Roles[0][ExistingFormFields][{seller_field_idx}][Value]", value)
            seller_field_idx += 1

    # ── Role 1 = Broker (template index 2) ──
    # Broker only has Signature1 + DatePicker1 (auto-fill on sign) — no ExistingFormFields needed
    add("Roles[1][RoleIndex]", "2")
    add("Roles[1][SignerName]", BROKER["name"])
    add("Roles[1][SignerEmail]", BROKER["email"])
    add("Roles[1][SignerOrder]", "2")

    # Build body
    text_body = "\r\n".join(parts) + "\r\n"
    closing = f'--{boundary}--\r\n'
    full_body = text_body.encode() + closing.encode()
    body_b64 = base64.b64encode(full_body).decode()

    content_type = f"multipart/form-data; boundary={boundary}"
    return content_type, body_b64


async def send_via_template(
    seller_name: str,
    seller_email: str,
    template_values: dict[str, str],
) -> dict:
    """Send BLA via BoldSign template with pre-filled fields."""
    from sdk.tools.pd_boldsign import pd_boldsign_proxy_post

    content_type, body_b64 = build_template_send_body(
        seller_name, seller_email, template_values
    )

    result = await pd_boldsign_proxy_post(
        f"https://api.boldsign.com/v1/template/send?templateId={BOLDSIGN_TEMPLATE_ID}",
        headers={"Content-Type": content_type},
        body_base64=body_b64,
        timeout_ms=120000,
    )
    return result


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run python generate_bla.py 'Contact Name or Email' [--dry-run]")
        sys.exit(1)

    query = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

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

    print(f"\n📋 Field mapping:")
    print(f"  Seller role fields:")
    for field_id, value_key in {**SELLER_TEXTBOX_FIELDS, **SELLER_DATE_FIELDS}.items():
        v = template_values.get(value_key, "")
        status = "✅" if v else "⚠️ "
        print(f"    {status} {field_id:25s} → {value_key:30s} = {v or '(empty)'}")
    print(f"  Broker role: Signature1 + DatePicker1 only (auto-fill on sign)")

    biz_name = template_values.get("business_name") or name

    # 3. Send via template (unless dry run)
    if dry_run:
        print("\n🏁 DRY RUN — values shown but not sent to BoldSign")
        result = {"dry_run": True, "missing_fields": missing}
    elif not email:
        print("❌ No email on contact — cannot send BoldSign document")
        sys.exit(1)
    else:
        print(f"\n📤 Sending via BoldSign template: Business Listing Agreement — {biz_name}")
        result = await send_via_template(name, email, template_values)
        parsed = json.loads(result.get("content", "{}"))
        body = parsed.get("body", parsed)
        status = parsed.get("status_code")
        print(f"   Status: {status}")
        print(f"   Response: {json.dumps(body, indent=2, default=str)[:1000]}")

    # Output
    output = {
        "contact_id": contact_id,
        "contact_name": name,
        "contact_email": email,
        "business_name": biz_name,
        "template_values": template_values,
        "missing_fields": missing,
        "boldsign": result if not dry_run else None,
    }
    print(f"\n📦 RESULT_JSON: {json.dumps(output, default=str)}")


if __name__ == "__main__":
    asyncio.run(main())
