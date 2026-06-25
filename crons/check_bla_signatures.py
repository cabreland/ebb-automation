"""
Process BoldSign "Completed" webhook events → trigger seller onboarding.

Reads unprocessed events from Convex (stored by the BoldSign webhook handler)
and for each:
  1. Match BoldSign document ID → GHL contact via custom field
  2. Advance GHL pipeline → "Listing Agreement Signed - Deal Won"
  3. Download the signed BLA PDF
  4. Run seller onboarding (data room + portal sync)
  5. Store signed BLA in data room "07 — Listing Agreement" folder
  6. Mark event as processed

The BoldSign webhook fires instantly on completion. This cron picks up
events within ~1 min (condition-based: only runs when pending events exist).

Usage:
    uv run python skills/deal_management/scripts/check_bla_signatures.py
"""
import asyncio
import json
import sys
from datetime import datetime

sys.path.insert(0, "/work")
import httpx
from sdk.tools.pd_boldsign import pd_boldsign_proxy_get
from sdk.tools.pd_highlevel_oauth import (
    pd_highlevel_oauth_proxy_get,
    pd_highlevel_oauth_proxy_put,
)

# ── Config ───────────────────────────────────────────────────────────────
SYNC_URL = "https://energetic-antelope-119.convex.site/api/viktor-sync"
SYNC_SECRET = "ebb-sync-k7X9mP2vQ4nR8wL1"

LOCATION_ID = "VrIFtlCW5GvoCpf0Spte"
GHL_API = "https://services.leadconnectorhq.com"
GHL_HEADERS = {"Version": "2021-07-28"}

SELLER_PIPELINE_ID = "Pj4Z15z4bAywO3GIC0u3"
STAGE_BLA_SENT = "3f233619-1714-4a45-9fa6-7319ca3dd663"
STAGE_BLA_SIGNED = "effd008a-aabb-48d9-95e3-7710ec785f03"

# Custom field IDs — loaded from config at runtime
BOLDSIGN_DOC_ID_FIELD = None
DRIVE_FOLDER_ID_FIELD = "HfAcPqekpJecfIm0G15Z"


def load_config():
    """Load GHL config including BoldSign doc ID field."""
    global BOLDSIGN_DOC_ID_FIELD
    try:
        with open("/work/skills/deal_management/references/ghl_config.json") as f:
            config = json.load(f)
            BOLDSIGN_DOC_ID_FIELD = config.get("boldsign_document_id_field")
    except (FileNotFoundError, json.JSONDecodeError):
        pass


async def get_pending_events() -> list[dict]:
    """Get unprocessed BoldSign webhook events from Convex."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SYNC_URL,
            json={"action": "get_pending_boldsign_events"},
            headers={"Authorization": f"Bearer {SYNC_SECRET}"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("data", [])
    return []


async def mark_event_processed(event_id: str, ghl_contact_id: str = None,
                                deal_id: str = None, result: str = None,
                                error: str = None):
    """Mark a BoldSign event as processed in Convex."""
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


async def find_contact_by_boldsign_doc(document_id: str) -> dict | None:
    """Search GHL contacts for one with matching BoldSign Document ID."""
    if not BOLDSIGN_DOC_ID_FIELD:
        return None

    try:
        # Search all opps at "Listing Agreement Sent" stage
        result = await pd_highlevel_oauth_proxy_get(
            url=f"{GHL_API}/opportunities/search",
            query_params={
                "location_id": LOCATION_ID,
                "pipeline_id": SELLER_PIPELINE_ID,
                "pipeline_stage_id": STAGE_BLA_SENT,
                "limit": "50",
            },
            headers=GHL_HEADERS,
        )
        parsed = json.loads(result.get("content", "{}"))
        body = parsed.get("body", parsed)
        opps = body.get("opportunities", [])

        for opp in opps:
            contact_id = opp.get("contactId") or opp.get("contact", {}).get("id")
            if not contact_id:
                continue

            # Check custom field
            contact_result = await pd_highlevel_oauth_proxy_get(
                url=f"{GHL_API}/contacts/{contact_id}",
                headers=GHL_HEADERS,
            )
            c_parsed = json.loads(contact_result.get("content", "{}"))
            c_body = c_parsed.get("body", c_parsed)
            contact = c_body.get("contact", c_body)

            for cf in contact.get("customFields", []):
                if cf.get("id") == BOLDSIGN_DOC_ID_FIELD and cf.get("value") == document_id:
                    return {
                        "contact_id": contact_id,
                        "contact": contact,
                        "opp_id": opp.get("id"),
                        "opp_name": opp.get("name"),
                    }
    except Exception as e:
        print(f"    ⚠️ Error searching for contact: {e}")
    return None


async def find_contact_from_boldsign_details(document_id: str) -> dict | None:
    """Fallback: get signer email from BoldSign doc, search GHL by email."""
    try:
        result = await pd_boldsign_proxy_get(
            f"https://api.boldsign.com/v1/document/properties?documentId={document_id}"
        )
        parsed = json.loads(result.get("content", "{}"))
        body = parsed.get("body", parsed)

        for signer in body.get("signerDetails", []):
            role = signer.get("signerRole", "")
            if role.lower() == "seller":
                email = signer.get("signerEmail", "")
                if email:
                    # Search GHL by email
                    search_result = await pd_highlevel_oauth_proxy_get(
                        url=f"{GHL_API}/contacts/",
                        query_params={
                            "locationId": LOCATION_ID,
                            "query": email,
                        },
                        headers=GHL_HEADERS,
                    )
                    s_parsed = json.loads(search_result.get("content", "{}"))
                    s_body = s_parsed.get("body", s_parsed)
                    contacts = s_body.get("contacts", [])
                    if contacts:
                        contact_id = contacts[0].get("id")
                        contact_name = contacts[0].get("name", "")

                        # Find their opp
                        opp_result = await pd_highlevel_oauth_proxy_get(
                            url=f"{GHL_API}/opportunities/search",
                            query_params={
                                "location_id": LOCATION_ID,
                                "pipeline_id": SELLER_PIPELINE_ID,
                                "contact_id": contact_id,
                                "limit": "1",
                            },
                            headers=GHL_HEADERS,
                        )
                        o_parsed = json.loads(opp_result.get("content", "{}"))
                        o_body = o_parsed.get("body", o_parsed)
                        opps = o_body.get("opportunities", [])

                        return {
                            "contact_id": contact_id,
                            "contact": contacts[0],
                            "opp_id": opps[0].get("id") if opps else None,
                            "opp_name": opps[0].get("name") if opps else contact_name,
                        }
    except Exception as e:
        print(f"    ⚠️ Error getting BoldSign doc details: {e}")
    return None


async def advance_to_signed(opp_id: str) -> bool:
    """Advance opportunity to Listing Agreement Signed stage."""
    try:
        await pd_highlevel_oauth_proxy_put(
            url=f"{GHL_API}/opportunities/{opp_id}",
            json_body={"pipelineStageId": STAGE_BLA_SIGNED},
            headers=GHL_HEADERS,
        )
        print(f"    📈 Advanced opp → Listing Agreement Signed")
        return True
    except Exception as e:
        print(f"    ⚠️ Error advancing opp: {e}")
        return False


async def run_seller_onboarding(contact_id: str) -> dict:
    """Run the seller onboarding pipeline (data room + portal sync)."""
    try:
        from skills.deal_management.scripts.seller_onboarding import onboard_seller
        result = await onboard_seller(contact_id)
        return result or {}
    except Exception as e:
        print(f"    ❌ Onboarding failed: {e}")
        return {"error": str(e)}


async def download_signed_bla(document_id: str) -> str | None:
    """Download the signed BLA PDF from BoldSign."""
    try:
        result = await pd_boldsign_proxy_get(
            f"https://api.boldsign.com/v1/document/download?documentId={document_id}"
        )
        parsed = json.loads(result.get("content", "{}"))
        status_code = parsed.get("status_code", 200)

        if status_code == 200:
            import base64
            body_raw = parsed.get("body", "")
            if body_raw:
                filepath = f"/work/temp/signed_bla_{document_id[:8]}.pdf"
                if isinstance(body_raw, str):
                    with open(filepath, "wb") as f:
                        f.write(base64.b64decode(body_raw))
                    print(f"    📥 Downloaded signed BLA → {filepath}")
                    return filepath
        print(f"    ⚠️ Could not download signed BLA: {status_code}")
    except Exception as e:
        print(f"    ⚠️ Error downloading signed BLA: {e}")
    return None


async def store_in_data_room(contact_id: str, signed_bla_path: str, business_name: str) -> bool:
    """Upload signed BLA to the contact's data room Listing Agreement folder."""
    if not signed_bla_path:
        return False

    try:
        from sdk.tools.gdrive import gdrive_list_folder, gdrive_upload_file

        # Get drive folder ID from contact
        result = await pd_highlevel_oauth_proxy_get(
            url=f"{GHL_API}/contacts/{contact_id}",
            headers=GHL_HEADERS,
        )
        parsed = json.loads(result.get("content", "{}"))
        body = parsed.get("body", parsed)
        contact = body.get("contact", body)

        drive_folder_id = None
        for cf in contact.get("customFields", []):
            if cf.get("id") == DRIVE_FOLDER_ID_FIELD:
                drive_folder_id = cf.get("value")
                break

        if not drive_folder_id:
            print(f"    ℹ️ No Drive folder on contact — BLA saved locally only")
            return False

        # Find "07 — Listing Agreement" subfolder
        listing = await gdrive_list_folder(folder_path=drive_folder_id)
        items = listing.get("items", [])
        if isinstance(items, str):
            items = json.loads(items) if items.startswith("[") else []

        la_folder_id = None
        for item in items:
            name = item.get("name", "")
            if "listing agreement" in name.lower() or "07" in name:
                la_folder_id = item.get("id")
                break

        if la_folder_id:
            today = datetime.now().strftime("%Y-%m-%d")
            safe_name = business_name.replace(" ", "_").replace("/", "-")
            filename = f"Signed_BLA_{safe_name}_{today}.pdf"
            await gdrive_upload_file(
                file_path=signed_bla_path,
                filename=filename,
                parent_folder_id=la_folder_id,
            )
            print(f"    📁 Stored signed BLA in data room: {filename}")
            return True
        else:
            print(f"    ⚠️ No Listing Agreement folder found in data room")
    except Exception as e:
        print(f"    ⚠️ Error storing in data room: {e}")
    return False


async def process_completed_event(event: dict) -> dict:
    """Process a single BoldSign Completed event."""
    document_id = event.get("documentId", "")
    document_title = event.get("documentTitle", "Unknown")
    event_id = event.get("_id", "")

    print(f"\n  🎉 Processing: {document_title} ({document_id[:12]}...)")

    results = {
        "contact_found": False,
        "pipeline_advanced": False,
        "onboarding_complete": False,
        "bla_stored": False,
    }

    # 1. Find the GHL contact
    match = await find_contact_by_boldsign_doc(document_id)
    if not match:
        # Fallback: use BoldSign signer email
        match = await find_contact_from_boldsign_details(document_id)

    if not match:
        error_msg = f"Could not match document {document_id} to any GHL contact"
        print(f"    ❌ {error_msg}")
        await mark_event_processed(event_id, error=error_msg)
        return results

    contact_id = match["contact_id"]
    opp_id = match.get("opp_id")
    business_name = match.get("opp_name", "Unknown")
    results["contact_found"] = True
    print(f"    🔗 Matched → {business_name} (contact: {contact_id})")

    # 2. Advance pipeline → Listing Agreement Signed
    if opp_id:
        results["pipeline_advanced"] = await advance_to_signed(opp_id)

    # 3. Run seller onboarding (data room + portal sync)
    onboard_result = await run_seller_onboarding(contact_id)
    if "error" not in onboard_result:
        results["onboarding_complete"] = True
        print(f"    ✅ Onboarding complete")

    # 4. Download signed BLA and store in data room
    signed_path = await download_signed_bla(document_id)
    if signed_path:
        results["bla_stored"] = await store_in_data_room(
            contact_id, signed_path, business_name
        )

    # 5. Mark event processed
    await mark_event_processed(
        event_id,
        ghl_contact_id=contact_id,
        deal_id=business_name,
        result=json.dumps(results),
    )

    return results


async def main():
    load_config()

    print("🔍 Checking for completed BLA events...")

    events = await get_pending_events()
    if not events:
        print("   No pending events.")
        return

    print(f"   Found {len(events)} pending event(s)")

    for event in events:
        results = await process_completed_event(event)
        status = "✅" if results["onboarding_complete"] else "⚠️"
        print(f"  {status} {event.get('documentTitle', '?')}: {results}")

    print(f"\n📊 Processed {len(events)} event(s)")


if __name__ == "__main__":
    asyncio.run(main())
