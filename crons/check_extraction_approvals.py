"""
Check for ✅/❌ reactions on pending call extraction cards in #call-review.
Approves or rejects extractions based on Slack reactions.

On approval:
  1. Match seller email → GHL contact (or create new)
  2. Push extracted fields to GHL custom fields
  3. Push call summary to GHL contact notes
  4. Auto-assign contact/opp to whoever recorded the call
  5. Advance seller pipeline opp → "Qualified" stage (discovery call already happened)

BLA send is decoupled — fires only when a human moves the opp to
"Listing Agreement Sent" stage in the Seller Pipeline.

Runs as a script cron every 5 minutes.

Usage:
    uv run python skills/deal_management/scripts/check_extraction_approvals.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/work")
import httpx
from sdk.tools.slack_admin_tools import coworker_get_slack_reactions, coworker_list_slack_users
from sdk.tools.viktor_spaces_tools import query_app_database

SYNC_URL = "https://energetic-antelope-119.convex.site/api/viktor-sync"
SYNC_SECRET = "ebb-sync-k7X9mP2vQ4nR8wL1"
LOCATION_ID = "VrIFtlCW5GvoCpf0Spte"
GHL_API = "https://services.leadconnectorhq.com"
GHL_PIT_KEY = os.environ.get("GHL_PIT_KEY", "pit-216d7602-3cd8-4aee-9a59-a378beea9537")

def _ghl_headers() -> dict:
    """Standard headers for direct GHL PIT-key API calls."""
    return {
        "Authorization": f"Bearer {GHL_PIT_KEY}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }

# ── Seller Pipeline ──────────────────────────────────────────────────────
SELLER_PIPELINE_ID = "Pj4Z15z4bAywO3GIC0u3"
STAGE_INTERESTED = "fbfcc821-f046-4bc9-9a1e-b06b3f8bae68"
STAGE_DISCOVERY = "fb67e8d3-8b4a-4663-a414-7d112eeb9faf"
STAGE_QUALIFIED = "54074be3-1289-4c6b-a4cf-62f971e719dd"

# Stage ordering (index = position). Used to prevent moving opps backwards.
SELLER_STAGE_ORDER = [
    STAGE_INTERESTED,                                    # 0  Interested
    STAGE_DISCOVERY,                                     # 1  Discovery Call
    STAGE_QUALIFIED,                                     # 2  Qualified
    "3f233619-1714-4a45-9fa6-7319ca3dd663",              # 3  Listing Agreement Sent
    "effd008a-aabb-48d9-95e3-7710ec785f03",              # 4  Listing Agreement Signed - Deal Won
]

# ── Referred Deals Pipeline ─────────────────────────────────────────────
REFERRED_PIPELINE_ID = "4fJYvwNAi6G2oHev8QPr"
REFERRED_STAGE_REFERRED = "98645c59-8c71-45dc-adcc-ebb7040519f1"
REFERRED_STAGE_DISCOVERY = "3699dd20-3c5d-41cf-b862-0002053bd4b1"
REFERRED_STAGE_QUALIFIED = "f51c3a35-336c-4bba-8f66-7a8345f11e39"
REFERRED_STAGE_ORDER = [
    REFERRED_STAGE_REFERRED,                              # 0  Referred
    REFERRED_STAGE_DISCOVERY,                             # 1  Discovery Scheduled
    REFERRED_STAGE_QUALIFIED,                             # 2  Qualified
    "08d6d35e-9fbb-46db-8339-3c128f1795bf",              # 3  Listing Agreement Sent
    "956cc6b2-4797-4cc6-a2cb-33c011b1f2c1",              # 4  Listing Agreement Signed - Deal Won
    "a91536a6-d967-43d3-b726-0ec5a14327d4",              # 5  Onboarding/Financials Requested
]
# Map pipeline_id → (stage_order, target_stage_on_approval)
# On ✅ approval: advance to Qualified (discovery call already happened)
PIPELINE_CONFIG = {
    SELLER_PIPELINE_ID: (SELLER_STAGE_ORDER, STAGE_QUALIFIED),
    REFERRED_PIPELINE_ID: (REFERRED_STAGE_ORDER, REFERRED_STAGE_QUALIFIED),
}

# ── GHL Custom Field IDs ────────────────────────────────────────────────
CUSTOM_FIELDS = {
    "business_name": "JVlSWIjaE9Zw3Nm7WyUP",
    "business_website": "gBLRxnvDN0Im18Z434Zp",
    "business_address": "wwA1IyZoZvId5LhTysK8",
    "seller_title": "pZewuPB8FSoMcwPazNt1",
    "annual_rev": "xT5rD1suTZntGKcysQAs",
    "ebitda": "Ip3xl0jFghpFjxJraQpK",
    "purchase_price": "x4zFeTQonNTUPMBNU6Pf",
}

# ── Fathom recorder email → GHL user ID ─────────────────────────────────
EMAIL_TO_GHL_USER = {
    "chris@exclusivebusinessbrokers.com": "OIXp9oPFccdqnSltxVK6",
    "jack@exclusivebusinessbrokers.com": "2lUpZhHNFWWJ8D6d1KtI",
    "jarrod@exclusivebusinessbrokers.com": "zUK2XpaJHuGn1moKdGTL",
    "prat@exclusivebusinessbrokers.com": "xLIUsPff6IoqmcUFjU9H",
    "ty@exclusivebusinessbrokers.com": "UxNRKIoziTzUzWTpewKO",
    "sarb@exclusivebusinessbrokers.com": "WwBwgNmrIJfwu82fOCgT",
    "aiden@highincomesociety.com": "nNBLMzsYLqH12a4wR5wb",
}

# Map of Slack user IDs → display names (cached per run)
_user_cache: dict[str, str] = {}


async def get_user_name(user_id: str) -> str:
    """Look up display name for a Slack user ID."""
    if not _user_cache:
        users = await coworker_list_slack_users(include_bots=False)
        for u in users.users:
            _user_cache[u["id"]] = u.get("display_name") or u.get("real_name") or u.get("name", "Unknown")
    return _user_cache.get(user_id, f"User {user_id}")


async def get_pending_extractions() -> list[dict]:
    """Get all pending_review extractions with Slack refs."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SYNC_URL,
            json={"action": "get_pending_extractions"},
            headers={"Authorization": f"Bearer {SYNC_SECRET}"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("data", [])
    return []


async def get_extraction_with_recording(extraction_id: str) -> dict | None:
    """Fetch full extraction + recording data from Convex."""
    try:
        result = await query_app_database(
            project_name="ebb-client-portal",
            function_name="fathomFns:getExtraction",
            environment="dev",
            args={"id": extraction_id},
        )
        return result.data
    except Exception as e:
        print(f"    ⚠️ Error fetching extraction: {e}")
        return None


async def approve_extraction(extraction_id: str, reviewer_name: str) -> bool:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SYNC_URL,
            json={
                "action": "approve_extraction",
                "extractionId": extraction_id,
                "reviewerName": reviewer_name,
            },
            headers={"Authorization": f"Bearer {SYNC_SECRET}"},
            timeout=15,
        )
        return resp.status_code == 200


async def reject_extraction(extraction_id: str, reviewer_name: str, note: str = "Rejected via Slack") -> bool:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SYNC_URL,
            json={
                "action": "reject_extraction",
                "extractionId": extraction_id,
                "reviewerName": reviewer_name,
                "reviewNote": note,
            },
            headers={"Authorization": f"Bearer {SYNC_SECRET}"},
            timeout=15,
        )
        return resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────
# GHL PUSH PIPELINE — Direct httpx calls with PIT key
# ─────────────────────────────────────────────────────────────────────────

def _extract_seller_email(recording: dict) -> str | None:
    """Pull the external invitee email from Fathom calendar_invitees."""
    raw = recording.get("calendarInvitees")
    if not raw:
        return None
    try:
        invitees = json.loads(raw)
        for inv in invitees:
            email = inv.get("email", "").lower().strip()
            if not email:
                continue
            if email.endswith("@exclusivebusinessbrokers.com"):
                continue
            if email.endswith("@highincomesociety.com"):
                continue
            return email
    except (json.JSONDecodeError, TypeError):
        pass
    return None


async def find_or_create_contact(client: httpx.AsyncClient, seller_email: str, seller_name: str | None) -> str | None:
    """Search GHL for a contact by email, create if not found. Return contactId."""
    try:
        resp = await client.get(
            f"{GHL_API}/contacts/",
            params={"locationId": LOCATION_ID, "query": seller_email},
            headers=_ghl_headers(),
            timeout=15,
        )
        data = resp.json()
        contacts = data.get("contacts", [])

        if contacts:
            contact_id = contacts[0].get("id")
            contact_name = contacts[0].get("name") or contacts[0].get("firstName", "")
            print(f"    🔗 Matched GHL contact: {contact_name} ({contact_id})")
            return contact_id
    except Exception as e:
        print(f"    ⚠️ Error searching contacts: {e}")

    # Not found — create via upsert
    if seller_name:
        try:
            parts = seller_name.split(" ", 1)
            body = {
                "locationId": LOCATION_ID,
                "email": seller_email,
                "firstName": parts[0],
                "lastName": parts[1] if len(parts) > 1 else "",
                "source": "Viktor Automation",
            }
            resp = await client.post(
                f"{GHL_API}/contacts/upsert",
                json=body,
                headers=_ghl_headers(),
                timeout=15,
            )
            data = resp.json()
            contact = data.get("contact", {})
            contact_id = contact.get("id")
            if contact_id:
                print(f"    ✨ Created GHL contact: {seller_name} ({contact_id})")
                return contact_id
        except Exception as e:
            print(f"    ⚠️ Error creating contact: {e}")

    return None


async def push_custom_fields(client: httpx.AsyncClient, contact_id: str, extraction: dict) -> bool:
    """Push extracted fields to GHL contact custom fields."""
    custom_field_values = []

    mappings = [
        ("legalBusinessName", "business_name"),
        ("businessWebsite", "business_website"),
        ("businessAddress", "business_address"),
        ("sellerTitle", "seller_title"),
    ]

    for ext_key, cf_key in mappings:
        val = extraction.get(ext_key)
        if val and cf_key in CUSTOM_FIELDS:
            custom_field_values.append({
                "id": CUSTOM_FIELDS[cf_key],
                "field_value": str(val),
            })

    # Numeric fields
    if extraction.get("ttmRevenue") is not None:
        custom_field_values.append({
            "id": CUSTOM_FIELDS["annual_rev"],
            "field_value": str(extraction["ttmRevenue"]),
        })
    if extraction.get("ttmProfit") is not None:
        custom_field_values.append({
            "id": CUSTOM_FIELDS["ebitda"],
            "field_value": str(extraction["ttmProfit"]),
        })
    if extraction.get("askingPrice") is not None:
        custom_field_values.append({
            "id": CUSTOM_FIELDS["purchase_price"],
            "field_value": str(extraction["askingPrice"]),
        })

    if not custom_field_values:
        print("    ℹ️ No custom fields to push")
        return True

    try:
        resp = await client.put(
            f"{GHL_API}/contacts/{contact_id}",
            json={"customFields": custom_field_values},
            headers=_ghl_headers(),
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"    📦 Pushed {len(custom_field_values)} custom fields to GHL")
            return True
        else:
            print(f"    ⚠️ Custom fields push returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"    ⚠️ Error pushing custom fields: {e}")
        return False


async def push_contact_notes(client: httpx.AsyncClient, contact_id: str, extraction: dict, recording: dict) -> bool:
    """Push call summary and key details to GHL contact notes."""
    parts = []

    title = recording.get("title", "Discovery Call")
    recorded_by = recording.get("recordedByName", "Unknown")
    parts.append(f"📞 {title} (recorded by {recorded_by})")
    parts.append("")

    if extraction.get("callSummary"):
        parts.append(f"Summary: {extraction['callSummary']}")
        parts.append("")

    detail_lines = []
    if extraction.get("legalBusinessName"):
        detail_lines.append(f"Business: {extraction['legalBusinessName']}")
    if extraction.get("askingPrice"):
        detail_lines.append(f"Asking Price: ${extraction['askingPrice']:,.0f}")
    if extraction.get("ttmRevenue"):
        detail_lines.append(f"TTM Revenue: ${extraction['ttmRevenue']:,.0f}")
    if extraction.get("ttmProfit"):
        detail_lines.append(f"TTM Profit: ${extraction['ttmProfit']:,.0f}")
    if extraction.get("commissionPercent"):
        detail_lines.append(f"Commission: {extraction['commissionPercent']}")
    if extraction.get("reasonForSelling"):
        detail_lines.append(f"Reason for Selling: {extraction['reasonForSelling']}")
    if extraction.get("ownershipPercent"):
        detail_lines.append(f"Ownership: {extraction['ownershipPercent']}")

    if detail_lines:
        parts.append("Key Details:")
        parts.extend(f"  • {line}" for line in detail_lines)
        parts.append("")

    if extraction.get("keyQuotes"):
        parts.append("Key Quotes:")
        try:
            quotes = json.loads(extraction["keyQuotes"]) if isinstance(extraction["keyQuotes"], str) else extraction["keyQuotes"]
            if isinstance(quotes, list):
                for q in quotes[:5]:
                    parts.append(f'  "{q}"')
        except (json.JSONDecodeError, TypeError):
            parts.append(f"  {extraction['keyQuotes']}")
        parts.append("")

    if extraction.get("confidenceNotes"):
        parts.append(f"Notes: {extraction['confidenceNotes']}")

    note_body = "\n".join(parts)

    try:
        resp = await client.post(
            f"{GHL_API}/contacts/{contact_id}/notes",
            json={"body": note_body},
            headers=_ghl_headers(),
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"    📝 Pushed call notes to GHL contact")
            return True
        else:
            print(f"    ⚠️ Notes push returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"    ⚠️ Error pushing notes: {e}")
        return False


async def auto_assign_contact(client: httpx.AsyncClient, contact_id: str, recorder_email: str) -> bool:
    """Assign the GHL contact to whoever recorded the call."""
    ghl_user_id = EMAIL_TO_GHL_USER.get(recorder_email.lower().strip())
    if not ghl_user_id:
        print(f"    ℹ️ No GHL user mapping for recorder: {recorder_email}")
        return False

    try:
        resp = await client.put(
            f"{GHL_API}/contacts/{contact_id}",
            json={"assignedTo": ghl_user_id},
            headers=_ghl_headers(),
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"    👤 Assigned contact to GHL user {ghl_user_id} ({recorder_email})")
            return True
        else:
            print(f"    ⚠️ Assign returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"    ⚠️ Error assigning contact: {e}")
        return False


async def _advance_opp_on_pipeline(
    client: httpx.AsyncClient, pipeline_id: str, contact_id: str,
    ghl_user_id: str | None,
) -> tuple[bool, str | None]:
    """Search one pipeline for opps and advance. Returns (found_and_handled, opp_id)."""
    config = PIPELINE_CONFIG.get(pipeline_id)
    if not config:
        return False, None
    stage_order, target_stage = config

    try:
        resp = await client.get(
            f"{GHL_API}/opportunities/search",
            params={
                "location_id": LOCATION_ID,
                "pipeline_id": pipeline_id,
                "contact_id": contact_id,
                "limit": "5",
            },
            headers=_ghl_headers(),
            timeout=15,
        )
        data = resp.json()
        opps = data.get("opportunities", [])
    except Exception as e:
        print(f"    ⚠️ Error searching pipeline {pipeline_id}: {e}")
        return False, None

    if not opps:
        return False, None  # No opp on this pipeline

    for opp in opps:
        opp_id = opp.get("id")
        current_stage = opp.get("pipelineStageId")

        # Don't move backwards
        try:
            current_idx = stage_order.index(current_stage)
            target_idx = stage_order.index(target_stage)
            if current_idx >= target_idx:
                print(f"    ⏭️ Opp {opp_id} already at/past target — skipping advance")
                if ghl_user_id:
                    try:
                        await client.put(
                            f"{GHL_API}/opportunities/{opp_id}",
                            json={"assignedTo": ghl_user_id},
                            headers=_ghl_headers(),
                            timeout=15,
                        )
                    except Exception:
                        pass
                return True, opp_id
        except ValueError:
            pass

        try:
            update_body = {"pipelineStageId": target_stage}
            if ghl_user_id:
                update_body["assignedTo"] = ghl_user_id

            update_resp = await client.put(
                f"{GHL_API}/opportunities/{opp_id}",
                json=update_body,
                headers=_ghl_headers(),
                timeout=15,
            )
            if update_resp.status_code == 200:
                print(f"    📈 Advanced opp {opp_id} → target stage on pipeline {pipeline_id[:8]}…")
                return True, opp_id
            else:
                print(f"    ⚠️ Opp advance returned {update_resp.status_code}: {update_resp.text[:200]}")
        except Exception as e:
            print(f"    ⚠️ Error advancing opp: {e}")

    return False, None


async def advance_pipeline_stage(client: httpx.AsyncClient, contact_id: str, recorder_email: str) -> tuple[bool, str | None]:
    """Find opp on Seller Pipeline OR Referred Deals and advance.
    
    Searches both pipelines. Creates a new Seller Pipeline opp if none exists anywhere.
    Returns (success, opp_id).
    """
    ghl_user_id = EMAIL_TO_GHL_USER.get(recorder_email.lower().strip())

    # Try Seller Pipeline first
    found, opp_id = await _advance_opp_on_pipeline(client, SELLER_PIPELINE_ID, contact_id, ghl_user_id)
    if found:
        return True, opp_id

    # Try Referred Deals
    found, opp_id = await _advance_opp_on_pipeline(client, REFERRED_PIPELINE_ID, contact_id, ghl_user_id)
    if found:
        return True, opp_id

    # No opp on either pipeline — create on Seller Pipeline
    try:
        create_body = {
            "pipelineId": SELLER_PIPELINE_ID,
            "locationId": LOCATION_ID,
            "name": "New Seller Lead",
            "pipelineStageId": STAGE_QUALIFIED,
            "contactId": contact_id,
            "status": "open",
        }
        if ghl_user_id:
            create_body["assignedTo"] = ghl_user_id

        create_resp = await client.post(
            f"{GHL_API}/opportunities/",
            json=create_body,
            headers=_ghl_headers(),
            timeout=15,
        )
        if create_resp.status_code in (200, 201):
            opp_data = create_resp.json()
            opp_id = opp_data.get("opportunity", {}).get("id")
            print(f"    🆕 Created seller opp at Qualified stage")
            return True, opp_id
        else:
            print(f"    ⚠️ Opp creation returned {create_resp.status_code}: {create_resp.text[:200]}")
            return False, None
    except Exception as e:
        print(f"    ⚠️ Error creating opp: {e}")
        return False, None


async def push_to_ghl(client: httpx.AsyncClient, extraction_id: str, extraction: dict, recording: dict) -> dict:
    """
    Full GHL push pipeline — runs after extraction is approved.
    Uses direct httpx calls with PIT key (no OAuth proxy drafts).
    """
    results = {
        "contact_matched": False,
        "contact_id": None,
        "seller_email": None,
        "fields_pushed": False,
        "notes_pushed": False,
        "assigned": False,
        "stage_advanced": False,
    }

    print(f"  🚀 GHL Push Pipeline for: {extraction.get('legalBusinessName', 'Unknown')}")

    # 1. Find seller email from call invitees
    seller_email = _extract_seller_email(recording)
    seller_name = extraction.get("sellerName")
    recorder_email = recording.get("recordedByEmail", "")

    if not seller_email:
        print(f"    ⚠️ No external invitee email found in call data")
        contact_id = extraction.get("ghlContactId")
        if not contact_id:
            print(f"    ❌ Cannot match to GHL contact — no email and no ghlContactId")
            return results
    else:
        contact_id = await find_or_create_contact(client, seller_email, seller_name)

    if not contact_id:
        print(f"    ❌ Could not match/create GHL contact")
        return results

    results["contact_matched"] = True
    results["contact_id"] = contact_id
    results["seller_email"] = seller_email

    # 2. Push custom fields
    results["fields_pushed"] = await push_custom_fields(client, contact_id, extraction)

    # 3. Push call notes
    results["notes_pushed"] = await push_contact_notes(client, contact_id, extraction, recording)

    # 4. Auto-assign to recorder
    results["assigned"] = await auto_assign_contact(client, contact_id, recorder_email)

    # 5. Advance pipeline stage (searches Seller + Referred Deals)
    advanced, _ = await advance_pipeline_stage(client, contact_id, recorder_email)
    results["stage_advanced"] = advanced

    return results


# ─────────────────────────────────────────────────────────────────────────
# MAIN CRON LOGIC
# ─────────────────────────────────────────────────────────────────────────

async def main():
    pending = await get_pending_extractions()
    if not pending:
        print("No pending extractions to check.")
        return

    print(f"Checking {len(pending)} pending extraction(s) for reactions...")

    for ext in pending:
        ext_id = ext["_id"]
        channel_id = ext.get("slackChannelId")
        message_ts = ext.get("slackMessageTs")
        business = ext.get("legalBusinessName", "Unknown")

        if not channel_id or not message_ts:
            print(f"  {business}: No Slack ref, skipping")
            continue

        # Check reactions
        reactions_result = await coworker_get_slack_reactions(
            channel_id=channel_id,
            message_ts=message_ts,
        )

        if not reactions_result.found or not reactions_result.reactions:
            print(f"  {business}: No reactions yet")
            continue

        # Look for ✅ (white_check_mark) or ❌ (x)
        approved = False
        rejected = False
        reviewer_id = None

        for r in reactions_result.reactions:
            name = r["name"]
            users = r.get("users", [])
            if name in ("white_check_mark", "heavy_check_mark", "white_check_mark::skin-tone-2",
                        "white_check_mark::skin-tone-3", "white_check_mark::skin-tone-4",
                        "white_check_mark::skin-tone-5") and users:
                approved = True
                reviewer_id = users[0]
            elif name in ("x", "negative_squared_cross_mark", "no_entry_sign") and users:
                rejected = True
                reviewer_id = users[0]

        if approved:
            reviewer_name = await get_user_name(reviewer_id)
            success = await approve_extraction(ext_id, reviewer_name)
            status = "✅ APPROVED" if success else "⚠️ APPROVE FAILED"
            print(f"  {business}: {status} by {reviewer_name}")

            if success:
                full_data = await get_extraction_with_recording(ext_id)
                if full_data:
                    recording = full_data.get("recording", {})

                    # Push to GHL with a shared httpx client
                    async with httpx.AsyncClient() as ghl_client:
                        push_results = await push_to_ghl(ghl_client, ext_id, full_data, recording)

                    ghl_status = "✅" if push_results["contact_matched"] else "⚠️ PARTIAL"
                    print(f"  {business}: GHL push {ghl_status}")

                    if push_results["contact_matched"]:
                        # Log actual outcomes
                        outcomes = []
                        if push_results["fields_pushed"]:
                            outcomes.append("fields")
                        if push_results["notes_pushed"]:
                            outcomes.append("notes")
                        if push_results["assigned"]:
                            outcomes.append("assigned")
                        if push_results["stage_advanced"]:
                            outcomes.append("stage")
                        print(f"  {business}: ✅ Pushed: {', '.join(outcomes)}. BLA sends on stage move.")
                    elif not push_results.get("seller_email"):
                        print(f"  {business}: ⚠️ No seller email found in call data")
                else:
                    print(f"  {business}: ⚠️ Could not fetch extraction data for GHL push")

        elif rejected:
            reviewer_name = await get_user_name(reviewer_id)
            success = await reject_extraction(ext_id, reviewer_name)
            status = "❌ REJECTED" if success else "⚠️ REJECT FAILED"
            print(f"  {business}: {status} by {reviewer_name}")
        else:
            reaction_names = [r["name"] for r in reactions_result.reactions]
            print(f"  {business}: Reactions present ({reaction_names}) but no approve/reject")


if __name__ == "__main__":
    asyncio.run(main())
