"""
Check for ✅/❌ reactions on pending call extraction cards in #call-review.
Approves or rejects extractions based on Slack reactions.

On approval:
  1. Match seller email → GHL contact (or create new)
  2. Push extracted fields to GHL custom fields
  3. Push call summary to GHL contact notes
  4. Auto-assign contact/opp to whoever recorded the call
  5. Advance seller pipeline opp → "Qualified" stage (discovery call already happened)
  6. Auto-send BLA via BoldSign (if contact has required fields)
  7. Store BoldSign document ID in GHL
  8. Advance pipeline → "Listing Agreement Sent"

Runs as a script cron every 5 minutes.

Usage:
    uv run python skills/deal_management/scripts/check_extraction_approvals.py
"""
import asyncio
import json
import sys

sys.path.insert(0, "/work")
import httpx
from sdk.tools.slack_admin_tools import coworker_get_slack_reactions, coworker_list_slack_users
from sdk.tools.pd_highlevel_oauth import (
    pd_highlevel_oauth_proxy_get,
    pd_highlevel_oauth_proxy_post,
    pd_highlevel_oauth_proxy_put,
    pd_highlevel_oauth_upsert_contact,
    pd_highlevel_oauth_update_contact,
)
from sdk.tools.viktor_spaces_tools import query_app_database

SYNC_URL = "https://energetic-antelope-119.convex.site/api/viktor-sync"
SYNC_SECRET = "ebb-sync-k7X9mP2vQ4nR8wL1"
LOCATION_ID = "VrIFtlCW5GvoCpf0Spte"
GHL_API = "https://services.leadconnectorhq.com"
GHL_HEADERS = {"Version": "2021-07-28"}

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
# BLA send is decoupled from this script.
# It fires via a separate event-driven trigger when a human moves
# the opp to "Ready to Send BLA" stage in the Seller Pipeline.

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
# Set after GHL field creation (populated at runtime)
# BOLDSIGN_DOC_ID_FIELD is handled by the BLA send script, not this approval flow

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
# GHL PUSH PIPELINE (fires on approval)
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
            # Skip internal EBB team emails
            if email.endswith("@exclusivebusinessbrokers.com"):
                continue
            if email.endswith("@highincomesociety.com"):
                continue
            return email
    except (json.JSONDecodeError, TypeError):
        pass
    return None


async def find_or_create_contact(seller_email: str, seller_name: str | None) -> str | None:
    """Search GHL for a contact by email, create if not found. Return contactId."""
    # Search by email
    try:
        result = await pd_highlevel_oauth_proxy_get(
            url=f"{GHL_API}/contacts/",
            query_params={
                "locationId": LOCATION_ID,
                "query": seller_email,
            },
            headers=GHL_HEADERS,
        )
        parsed = json.loads(result.get("content", "{}"))
        body = parsed.get("body", parsed)
        contacts = body.get("contacts", [])

        if contacts:
            contact_id = contacts[0].get("id")
            contact_name = contacts[0].get("name") or contacts[0].get("firstName", "")
            print(f"    🔗 Matched GHL contact: {contact_name} ({contact_id})")
            return contact_id
    except Exception as e:
        print(f"    ⚠️ Error searching contacts: {e}")

    # Not found — create
    if seller_name:
        try:
            result = await pd_highlevel_oauth_upsert_contact(
                locationId=LOCATION_ID,
                name=seller_name,
                email=seller_email,
            )
            content = result.get("content", "{}")
            parsed = json.loads(content) if isinstance(content, str) else content
            body = parsed.get("body", parsed)
            contact = body.get("contact", body)
            contact_id = contact.get("id")
            if contact_id:
                print(f"    ✨ Created GHL contact: {seller_name} ({contact_id})")
                return contact_id
        except Exception as e:
            print(f"    ⚠️ Error creating contact: {e}")

    return None


async def push_custom_fields(contact_id: str, extraction: dict) -> bool:
    """Push extracted fields to GHL contact custom fields."""
    custom_field_values = []

    # Map extraction fields → GHL custom field IDs
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
        await pd_highlevel_oauth_update_contact(
            contactId=contact_id,
            additionalOptions={"customFields": custom_field_values},
        )
        print(f"    📦 Pushed {len(custom_field_values)} custom fields to GHL")
        return True
    except Exception as e:
        print(f"    ⚠️ Error pushing custom fields: {e}")
        return False


async def push_contact_notes(contact_id: str, extraction: dict, recording: dict) -> bool:
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
        await pd_highlevel_oauth_proxy_post(
            url=f"{GHL_API}/contacts/{contact_id}/notes",
            json_body={"body": note_body, "userId": None},
            headers=GHL_HEADERS,
        )
        print(f"    📝 Pushed call notes to GHL contact")
        return True
    except Exception as e:
        print(f"    ⚠️ Error pushing notes: {e}")
        return False


async def auto_assign_contact(contact_id: str, recorder_email: str) -> bool:
    """Assign the GHL contact to whoever recorded the call."""
    ghl_user_id = EMAIL_TO_GHL_USER.get(recorder_email.lower().strip())
    if not ghl_user_id:
        print(f"    ℹ️ No GHL user mapping for recorder: {recorder_email}")
        return False

    try:
        await pd_highlevel_oauth_update_contact(
            contactId=contact_id,
            additionalOptions={"assignedTo": ghl_user_id},
        )
        print(f"    👤 Assigned contact to GHL user {ghl_user_id} ({recorder_email})")
        return True
    except Exception as e:
        print(f"    ⚠️ Error assigning contact: {e}")
        return False


async def advance_pipeline_stage(contact_id: str, recorder_email: str,
                                  target_stage: str = None) -> tuple[bool, str | None]:
    """Find seller pipeline opp for this contact and advance to target stage.
    
    Returns (success, opp_id).
    """
    if target_stage is None:
        target_stage = STAGE_DISCOVERY
    ghl_user_id = EMAIL_TO_GHL_USER.get(recorder_email.lower().strip())

    try:
        result = await pd_highlevel_oauth_proxy_get(
            url=f"{GHL_API}/opportunities/search",
            query_params={
                "location_id": LOCATION_ID,
                "pipeline_id": SELLER_PIPELINE_ID,
                "contact_id": contact_id,
                "limit": "5",
            },
            headers=GHL_HEADERS,
        )
        parsed = json.loads(result.get("content", "{}"))
        body = parsed.get("body", parsed)
        opps = body.get("opportunities", [])

        if not opps:
            # Create opp if none exists
            try:
                create_body = {
                    "pipelineId": SELLER_PIPELINE_ID,
                    "locationId": LOCATION_ID,
                    "name": "New Seller Lead",
                    "pipelineStageId": target_stage,
                    "contactId": contact_id,
                    "status": "open",
                }
                if ghl_user_id:
                    create_body["assignedTo"] = ghl_user_id

                create_result = await pd_highlevel_oauth_proxy_post(
                    url=f"{GHL_API}/opportunities/",
                    json_body=create_body,
                    headers=GHL_HEADERS,
                )
                create_parsed = json.loads(create_result.get("content", "{}"))
                create_body_resp = create_parsed.get("body", create_parsed)
                opp_id = create_body_resp.get("opportunity", {}).get("id")
                print(f"    🆕 Created seller opp at target stage")
                return True, opp_id
            except Exception as e:
                print(f"    ⚠️ Error creating opp: {e}")
                return False, None

        # Advance existing opp(s) — but never move backwards
        for opp in opps:
            opp_id = opp.get("id")
            current_stage = opp.get("pipelineStageId")

            # Don't move backwards — if opp is already at or past target, skip
            try:
                current_idx = SELLER_STAGE_ORDER.index(current_stage)
                target_idx = SELLER_STAGE_ORDER.index(target_stage)
                if current_idx >= target_idx:
                    stage_names = {
                        STAGE_INTERESTED: "Interested",
                        STAGE_DISCOVERY: "Discovery Call",
                        STAGE_QUALIFIED: "Qualified",
                    }
                    current_name = stage_names.get(current_stage, current_stage[:12])
                    print(f"    ⏭️ Opp {opp_id} already at '{current_name}' — skipping advance (won't move backwards)")
                    # Still assign if needed
                    if ghl_user_id:
                        try:
                            await pd_highlevel_oauth_proxy_put(
                                url=f"{GHL_API}/opportunities/{opp_id}",
                                json_body={"assignedTo": ghl_user_id},
                                headers=GHL_HEADERS,
                            )
                        except Exception:
                            pass
                    return True, opp_id
            except ValueError:
                # Stage not in our ordering list — proceed with advance
                pass

            try:
                update_body = {"pipelineStageId": target_stage}
                if ghl_user_id:
                    update_body["assignedTo"] = ghl_user_id

                await pd_highlevel_oauth_proxy_put(
                    url=f"{GHL_API}/opportunities/{opp_id}",
                    json_body=update_body,
                    headers=GHL_HEADERS,
                )
                stage_name = {
                    STAGE_DISCOVERY: "Discovery Call",
                    STAGE_QUALIFIED: "Qualified",
                }.get(target_stage, target_stage[:12])
                print(f"    📈 Advanced opp {opp_id} → {stage_name}")
                return True, opp_id
            except Exception as e:
                print(f"    ⚠️ Error advancing opp: {e}")

        return False, None

    except Exception as e:
        print(f"    ⚠️ Error searching opps: {e}")
        return False, None


async def push_to_ghl(extraction_id: str, extraction: dict, recording: dict) -> dict:
    """
    Full GHL push pipeline — runs after extraction is approved.

    Returns dict with results for each step + contact_id and seller_email.
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
        contact_id = await find_or_create_contact(seller_email, seller_name)

    if not contact_id:
        print(f"    ❌ Could not match/create GHL contact")
        return results

    results["contact_matched"] = True
    results["contact_id"] = contact_id
    results["seller_email"] = seller_email

    # 3. Push custom fields
    results["fields_pushed"] = await push_custom_fields(contact_id, extraction)

    # 4. Push call notes
    results["notes_pushed"] = await push_contact_notes(contact_id, extraction, recording)

    # 5. Auto-assign to recorder
    results["assigned"] = await auto_assign_contact(contact_id, recorder_email)

    # 6. Advance pipeline stage to Qualified (discovery call already happened)
    advanced, _ = await advance_pipeline_stage(contact_id, recorder_email, STAGE_QUALIFIED)
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
                reviewer_id = users[0]  # First person to react
            elif name in ("x", "negative_squared_cross_mark", "no_entry_sign") and users:
                rejected = True
                reviewer_id = users[0]

        if approved:
            reviewer_name = await get_user_name(reviewer_id)
            success = await approve_extraction(ext_id, reviewer_name)
            status = "✅ APPROVED" if success else "⚠️ APPROVE FAILED"
            print(f"  {business}: {status} by {reviewer_name}")

            if success:
                # Fetch full extraction + recording data for GHL push
                full_data = await get_extraction_with_recording(ext_id)
                if full_data:
                    recording = full_data.get("recording", {})

                    # Push to GHL (fields + notes + assign + stage advance ONLY)
                    # BLA send is NOT triggered here — it's decoupled and fires
                    # only when a human moves the opp to "Ready to Send BLA" stage
                    push_results = await push_to_ghl(ext_id, full_data, recording)
                    ghl_status = "✅" if push_results["contact_matched"] else "⚠️ PARTIAL"
                    print(f"  {business}: GHL push {ghl_status}")

                    if push_results["contact_matched"]:
                        print(f"  {business}: ✅ Data pushed. BLA will send when opp is moved to 'Ready to Send BLA' stage.")
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
