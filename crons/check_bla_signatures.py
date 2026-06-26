"""
Signed BLA Follow-On Processor
================================
Picks up BoldSign events marked `ghl_updated` by the instant processor
and handles the follow-on work:

  1. Create Google Drive data room (9 folders + self-help guides)
  2. Download the signed BLA PDF from BoldSign
  3. Upload signed BLA to data room "07 — Listing Agreement" folder
  4. Update GHL contact with drive_folder_id
  5. Add GHL onboarding note with data room link
  6. Sync portal with deal + folder mapping
  7. Mark event as `processed`

The instant processor (Convex `processSignedBla`) already handles:
  - Pipeline advance → "Listing Agreement Signed"
  - BoldSign doc ID → GHL custom field
  - Confirmation note → GHL
  - Portal stage sync

This script handles everything that requires Google Drive access.

Triggered by condition: has_ghl_updated_boldsign_events.py
Cron: /boldsign/check-signatures (every 2 min, condition-gated)

Usage:
    uv run python skills/deal_management/scripts/check_bla_signatures.py
"""
import asyncio
import json
import re
import sys
from datetime import datetime

sys.path.insert(0, "/work")
import httpx
from sdk.tools.pd_boldsign import pd_boldsign_proxy_get
from sdk.tools.pd_highlevel_oauth import (
    pd_highlevel_oauth_proxy_get,
    pd_highlevel_oauth_update_contact,
    pd_highlevel_oauth_proxy_post,
)
from sdk.tools.gdrive import (
    gdrive_create_folder,
    gdrive_google_docs_create,
    gdrive_move,
    gdrive_upload_file,
)

# ── Config ───────────────────────────────────────────────────────────────
SYNC_URL = "https://energetic-antelope-119.convex.site/api/viktor-sync"
SYNC_SECRET = "ebb-sync-k7X9mP2vQ4nR8wL1"

LOCATION_ID = "VrIFtlCW5GvoCpf0Spte"
GHL_API = "https://services.leadconnectorhq.com"
GHL_HEADERS = {"Version": "2021-07-28"}

PENDING_DEALS_FOLDER = "1PznxONg94CN2wJuUjZmcIRMkgYWr0b60"
DRIVE_FOLDER_ID_FIELD = "HfAcPqekpJecfIm0G15Z"
BOLDSIGN_DOC_ID_FIELD = "h00EbkYqD1xL16dtagDs"
BUSINESS_NAME_FIELD = "JVlSWIjaE9Zw3Nm7WyUP"

# 9-folder data room structure
SUBFOLDER_NAMES = [
    "01 — Financials",
    "02 — Org Structure",
    "03 — Operations",
    "04 — Legal & Assets",
    "05 — Marketing & Analytics",
    "06 — Signed NDA",
    "07 — Listing Agreement",
    "08 — LOI",
    "09 — Data Room (buyer-facing)",
]

GUIDE_CONTENT = {
    "01 — Financials": "📋 WHAT GOES HERE — Financials\n\nPRIORITY — Upload First\n\n✅ Tax Returns — last 3 years\n✅ Monthly P&L — trailing 12 months\n✅ Year-End P&L — last 3 full fiscal years\n✅ Current Balance Sheet\n\nFormat: PDF for tax docs. Excel/CSV for P&L.",
    "02 — Org Structure": "📋 WHAT GOES HERE — Org Structure\n\nPRIORITY — Upload Alongside Financials\n\n✅ Org Chart — names, roles, reporting lines\n✅ Employee/Contractor Roster\n✅ Key person dependencies",
    "03 — Operations": "📋 WHAT GOES HERE — Operations\n\n• Business Description / Overview\n• Customer & Revenue Concentration\n• Vendor / Supplier Contracts\n• Technology stack\n• SOPs / Process documentation",
    "04 — Legal & Assets": "📋 WHAT GOES HERE — Legal & Assets\n\n• IP — trademarks, patents, domains\n• Lease / Rental Agreements\n• Corporate docs\n• Insurance\n• Pending or past litigation",
    "05 — Marketing & Analytics": "📋 WHAT GOES HERE — Marketing & Analytics\n\n• Website analytics — 12-month trend\n• Marketing channel breakdown\n• Social metrics\n• Ad spend + ROAS\n• Email list size + engagement",
    "06 — Signed NDA": "📋 WHAT GOES HERE — Signed NDA\n\nNDAs are auto-filed here. No action needed.",
    "07 — Listing Agreement": "📋 WHAT GOES HERE — Listing Agreement\n\nYour signed listing agreement is stored here automatically.",
    "08 — LOI": "📋 WHAT GOES HERE — LOI\n\nLetters of Intent are filed here when received.",
    "09 — Data Room (buyer-facing)": "📋 WHAT GOES HERE — Data Room (Buyer-Facing)\n\n⚠️ DO NOT UPLOAD FILES DIRECTLY. Assembled by EBB team after docs in 01–05 are reviewed.",
}


# ── Convex API ───────────────────────────────────────────────────────────

async def get_ghl_updated_events() -> list[dict]:
    """Get BoldSign events at ghl_updated status (data room pending)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SYNC_URL,
            json={"action": "get_ghl_updated_boldsign_events"},
            headers={"Authorization": f"Bearer {SYNC_SECRET}"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("data", [])
    return []


async def mark_event_processed(event_id: str, ghl_contact_id: str = None,
                                deal_id: str = None, result: str = None,
                                error: str = None):
    """Mark a BoldSign event as fully processed in Convex."""
    async with httpx.AsyncClient() as client:
        await client.post(
            SYNC_URL,
            json={
                "action": "mark_boldsign_event_processed",
                "eventId": event_id,
                "ghlContactId": ghl_contact_id,
                "dealId": deal_id,
                "onboardingResult": result,
                "error": error,
            },
            headers={"Authorization": f"Bearer {SYNC_SECRET}"},
            timeout=15,
        )


# ── GHL Helpers ──────────────────────────────────────────────────────────

async def get_contact_by_doc_id(document_id: str) -> dict | None:
    """Find GHL contact that has this BoldSign document ID in custom field."""
    try:
        # Search contacts with this BoldSign doc ID
        result = await pd_highlevel_oauth_proxy_get(
            url=f"{GHL_API}/contacts/",
            query_params={"locationId": LOCATION_ID, "query": document_id},
            headers=GHL_HEADERS,
        )
        parsed = json.loads(result.get("content", "{}"))
        body = parsed.get("body", parsed)
        contacts = body.get("contacts", [])

        # Check each contact's custom fields
        for contact in contacts:
            for cf in contact.get("customFields", []):
                if cf.get("id") == BOLDSIGN_DOC_ID_FIELD and cf.get("value") == document_id:
                    return contact
    except Exception as e:
        print(f"    ⚠️ Error searching by doc ID: {e}")
    return None


async def get_contact_by_email(email: str) -> dict | None:
    """Search GHL for contact by email."""
    try:
        result = await pd_highlevel_oauth_proxy_get(
            url=f"{GHL_API}/contacts/",
            query_params={"locationId": LOCATION_ID, "query": email},
            headers=GHL_HEADERS,
        )
        parsed = json.loads(result.get("content", "{}"))
        body = parsed.get("body", parsed)
        contacts = body.get("contacts", [])
        if contacts:
            return contacts[0]
    except Exception as e:
        print(f"    ⚠️ Error searching by email: {e}")
    return None


async def get_signer_email(document_id: str) -> str | None:
    """Get seller's email from BoldSign document details."""
    try:
        result = await pd_boldsign_proxy_get(
            f"https://api.boldsign.com/v1/document/properties?documentId={document_id}"
        )
        parsed = json.loads(result.get("content", "{}"))
        body = parsed.get("body", parsed)
        for signer in body.get("signerDetails", []):
            if signer.get("signerRole", "").lower() == "seller":
                return signer.get("signerEmail")
    except Exception as e:
        print(f"    ⚠️ Error getting signer email: {e}")
    return None


def get_company_name(contact: dict) -> str:
    """Extract company name from contact (companyName or business_name custom field)."""
    name = contact.get("companyName", "")
    if not name:
        for cf in contact.get("customFields", []):
            if cf.get("id") == BUSINESS_NAME_FIELD:
                name = cf.get("value", "")
                break
    return name or "Unknown Business"


# ── Data Room Creation ───────────────────────────────────────────────────

async def create_data_room(company_name: str) -> dict:
    """Create 9-folder data room in Pending/Inactive Deals. Returns folder IDs."""
    print(f"    📁 Creating data room: {company_name}/")

    # Create parent folder
    result = await gdrive_create_folder(name=company_name, parent_path=PENDING_DEALS_FOLDER)
    parent_id = result.get("folder_id")
    if not parent_id:
        raise RuntimeError(f"Failed to create parent folder: {result}")
    print(f"    ✅ Parent folder created ({parent_id})")

    # Create 9 subfolders
    subfolder_ids = {}
    for name in SUBFOLDER_NAMES:
        sub = await gdrive_create_folder(name=name, parent_path=parent_id)
        sub_id = sub.get("folder_id")
        if not sub_id:
            print(f"    ⚠️ Failed to create {name}")
            continue
        subfolder_ids[name] = sub_id
        print(f"      ✅ {name}/")

    # Create guide docs in each subfolder
    for name, folder_id in subfolder_ids.items():
        content = GUIDE_CONTENT.get(name, "")
        if not content:
            continue
        try:
            doc = await gdrive_google_docs_create(title="📋 What Goes Here", content=content)
            doc_status = doc.get("status", "")
            match = re.search(r"ID: ([A-Za-z0-9_-]+)", str(doc_status))
            if match:
                doc_id = match.group(1)
                await gdrive_move(unified_uri=doc_id, destination_folder_id=folder_id)
        except Exception as e:
            print(f"      ℹ️ Guide doc for {name}: {e}")

    return {
        "folder_id": parent_id,
        "folder_link": f"https://drive.google.com/drive/folders/{parent_id}",
        "subfolder_ids": subfolder_ids,
    }


# ── BLA Download + Storage ───────────────────────────────────────────────

async def download_signed_bla(document_id: str) -> str | None:
    """Download signed BLA PDF from BoldSign. Returns local file path."""
    try:
        result = await pd_boldsign_proxy_get(
            f"https://api.boldsign.com/v1/document/download?documentId={document_id}"
        )
        parsed = json.loads(result.get("content", "{}"))
        if parsed.get("status_code", 200) == 200:
            import base64
            body_raw = parsed.get("body", "")
            if body_raw and isinstance(body_raw, str):
                filepath = f"/work/temp/signed_bla_{document_id[:8]}.pdf"
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(body_raw))
                print(f"    📥 Downloaded signed BLA ({filepath})")
                return filepath
        print(f"    ⚠️ BLA download returned status {parsed.get('status_code')}")
    except Exception as e:
        print(f"    ⚠️ Error downloading BLA: {e}")
    return None


async def store_bla_in_data_room(bla_path: str, listing_agreement_folder_id: str,
                                  company_name: str) -> bool:
    """Upload signed BLA PDF to the 07 — Listing Agreement subfolder."""
    if not bla_path or not listing_agreement_folder_id:
        return False
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        safe = company_name.replace(" ", "_").replace("/", "-")
        filename = f"Signed_BLA_{safe}_{today}.pdf"
        await gdrive_upload_file(
            file_path=bla_path,
            filename=filename,
            parent_folder_id=listing_agreement_folder_id,
        )
        print(f"    📄 Stored signed BLA: {filename}")
        return True
    except Exception as e:
        print(f"    ⚠️ Error uploading BLA: {e}")
        return False


# ── GHL Updates ──────────────────────────────────────────────────────────

async def update_ghl_drive_folder(contact_id: str, folder_id: str) -> bool:
    """Set drive_folder_id custom field on GHL contact."""
    try:
        await pd_highlevel_oauth_update_contact(
            contactId=contact_id,
            additionalOptions={
                "customFields": [{"id": DRIVE_FOLDER_ID_FIELD, "field_value": folder_id}]
            },
        )
        print(f"    📝 GHL drive_folder_id set")
        return True
    except Exception as e:
        print(f"    ⚠️ Error setting drive_folder_id: {e}")
        return False


async def add_onboarding_note(contact_id: str, company_name: str,
                               folder_link: str) -> bool:
    """Add onboarding note to GHL contact."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        await pd_highlevel_oauth_proxy_post(
            url=f"{GHL_API}/contacts/{contact_id}/notes",
            json_body={
                "body": (
                    f"🚀 Seller Onboarding — {today}\n\n"
                    f"Company: {company_name}\n"
                    f"Data Room: {folder_link}\n\n"
                    f"9 subfolders + self-help guides loaded.\n"
                    f"Folders: Financials · Org Structure · Operations · "
                    f"Legal & Assets · Marketing & Analytics · Signed NDA · "
                    f"Listing Agreement · LOI · Data Room (buyer-facing)"
                )
            },
            headers=GHL_HEADERS,
        )
        print(f"    📝 Onboarding note added to GHL")
        return True
    except Exception as e:
        print(f"    ⚠️ Error adding note: {e}")
        return False


# ── Portal Sync ──────────────────────────────────────────────────────────

async def sync_portal(contact_id: str, company_name: str,
                       folder_id: str, subfolder_ids: dict) -> bool:
    """Sync data room info to portal DB."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                SYNC_URL,
                json={
                    "action": "onboarding_complete",
                    "ghlContactId": contact_id,
                    "dealName": company_name,
                    "driveFolderId": folder_id,
                    "subfolderMap": subfolder_ids,
                },
                headers={"Authorization": f"Bearer {SYNC_SECRET}"},
                timeout=15,
            )
            if resp.status_code == 200:
                print(f"    ✅ Portal synced")
                return True
            print(f"    ⚠️ Portal sync failed: {resp.status_code}")
    except Exception as e:
        print(f"    ⚠️ Portal sync error: {e}")
    return False


# ── Main Processing ─────────────────────────────────────────────────────

async def process_event(event: dict) -> dict:
    """Process a single ghl_updated BoldSign event → data room + BLA storage."""
    document_id = event.get("documentId", "")
    document_title = event.get("documentTitle", "Unknown")
    event_id = event.get("_id", "")

    print(f"\n  🎉 Processing: {document_title} ({document_id[:12]}...)")

    results = {
        "contact_found": False,
        "data_room_created": False,
        "bla_stored": False,
        "ghl_updated": False,
        "portal_synced": False,
    }

    # 1. Find GHL contact
    contact = await get_contact_by_doc_id(document_id)
    if not contact:
        email = await get_signer_email(document_id)
        if email:
            contact = await get_contact_by_email(email)

    if not contact:
        error = f"Could not match document {document_id[:12]} to GHL contact"
        print(f"    ❌ {error}")
        await mark_event_processed(event_id, error=error)
        return results

    contact_id = contact.get("id")
    company_name = get_company_name(contact)
    contact_name = contact.get("contactName") or contact.get("name", "Unknown")
    results["contact_found"] = True
    print(f"    🔗 Matched: {contact_name} / {company_name} ({contact_id})")

    # 2. Create data room
    try:
        data_room = await create_data_room(company_name)
        folder_id = data_room["folder_id"]
        folder_link = data_room["folder_link"]
        subfolder_ids = data_room["subfolder_ids"]
        results["data_room_created"] = True
        print(f"    ✅ Data room created: {folder_link}")
    except Exception as e:
        error = f"Data room creation failed: {e}"
        print(f"    ❌ {error}")
        await mark_event_processed(event_id, ghl_contact_id=contact_id,
                                    deal_id=company_name, error=error)
        return results

    # 3. Download signed BLA + store in data room
    bla_path = await download_signed_bla(document_id)
    if bla_path:
        la_folder_id = subfolder_ids.get("07 — Listing Agreement")
        if la_folder_id:
            results["bla_stored"] = await store_bla_in_data_room(
                bla_path, la_folder_id, company_name
            )
        # Clean up temp file
        try:
            import os
            os.remove(bla_path)
        except OSError:
            pass

    # 4. Update GHL with data room folder ID
    results["ghl_updated"] = await update_ghl_drive_folder(contact_id, folder_id)
    await add_onboarding_note(contact_id, company_name, folder_link)

    # 5. Portal sync with folder mapping
    results["portal_synced"] = await sync_portal(
        contact_id, company_name, folder_id, subfolder_ids
    )

    # 6. Mark event fully processed
    await mark_event_processed(
        event_id,
        ghl_contact_id=contact_id,
        deal_id=company_name,
        result=json.dumps(results),
    )

    return results


async def main():
    print("🔍 Checking for signed BLA events needing data room creation...")

    events = await get_ghl_updated_events()
    if not events:
        print("   No ghl_updated events. Nothing to do.")
        return

    print(f"   Found {len(events)} event(s) pending data room creation")

    for event in events:
        results = await process_event(event)
        ok = results["data_room_created"]
        status = "✅ COMPLETE" if ok else "⚠️ PARTIAL"
        print(f"  {status}: {event.get('documentTitle', '?')}")
        for k, v in results.items():
            print(f"    {k}: {'✅' if v else '❌'}")

    print(f"\n📊 Processed {len(events)} event(s)")


if __name__ == "__main__":
    asyncio.run(main())
