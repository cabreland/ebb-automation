/**
 * BoldSign Instant Processor — Convex Action
 *
 * Triggered immediately by scheduler.runAfter(0, ...) when a BoldSign
 * "Completed" webhook event is stored. Does the seller-facing work
 * instantly (seconds, not minutes):
 *
 *   1. GHL: Find contact by BoldSign doc ID custom field (searches BOTH pipelines)
 *   2. GHL: Advance pipeline → "Listing Agreement Signed - Deal Won"
 *      (Seller Pipeline OR Referred Deals — whichever the opp is on)
 *   3. GHL: Add confirmation note
 *   4. Portal: Update deal stage
 *
 * Data room creation + signed BLA storage follow via Viktor (heavier,
 * not seller-visible, uses Drive API which isn't available in Convex).
 *
 * LEAST-PRIVILEGE: This action only calls these GHL endpoints,
 * even though the API key has full scope:
 *   - GET  /contacts/{id}            (read single contact)
 *   - GET  /contacts/                (search by email — fallback only)
 *   - GET  /opportunities/search     (find opp by pipeline stage)
 *   - PUT  /opportunities/{id}       (advance pipeline stage)
 *   - POST /contacts/{id}/notes      (add note)
 *
 * BoldSign endpoints (if BOLDSIGN_API_KEY is set):
 *   - GET /v1/document/properties    (get signer email — fallback only)
 *   - GET /v1/document/download      (download signed PDF)
 */
import { internalAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { v } from "convex/values";

declare const process: { env: Record<string, string | undefined> };

// ── Config (all IDs from ghl_config.json) ────────────────────────────────
const GHL_API = "https://services.leadconnectorhq.com";
const LOCATION_ID = "VrIFtlCW5GvoCpf0Spte";
const BOLDSIGN_DOC_FIELD = "h00EbkYqD1xL16dtagDs";

// Both pipelines: BLA Sent → BLA Signed stage mapping
const PIPELINES = [
  {
    name: "Seller Pipeline",
    id: "Pj4Z15z4bAywO3GIC0u3",
    blaSentStage: "3f233619-1714-4a45-9fa6-7319ca3dd663",
    blaSignedStage: "effd008a-aabb-48d9-95e3-7710ec785f03",
  },
  {
    name: "Referred Deals",
    id: "4fJYvwNAi6G2oHev8QPr",
    blaSentStage: "08d6d35e-9fbb-46db-8339-3c128f1795bf",
    blaSignedStage: "956cc6b2-4797-4cc6-a2cb-33c011b1f2c1",
  },
] as const;

// ── Helpers ──────────────────────────────────────────────────────────────

function ghlHeaders(apiKey: string, includeContentType = false): Record<string, string> {
  const h: Record<string, string> = {
    Authorization: `Bearer ${apiKey}`,
    Version: "2021-07-28",
  };
  if (includeContentType) h["Content-Type"] = "application/json";
  return h;
}

async function ghlGet(apiKey: string, path: string, params?: Record<string, string>): Promise<any> {
  const url = new URL(`${GHL_API}${path}`);
  if (params) {
    for (const [k, val] of Object.entries(params)) {
      url.searchParams.set(k, val);
    }
  }
  const resp = await fetch(url.toString(), { headers: ghlHeaders(apiKey) });
  if (!resp.ok) {
    throw new Error(`GHL GET ${path} → ${resp.status}: ${await resp.text()}`);
  }
  return resp.json();
}

async function ghlPut(apiKey: string, path: string, body: any): Promise<any> {
  const resp = await fetch(`${GHL_API}${path}`, {
    method: "PUT",
    headers: ghlHeaders(apiKey, true),
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new Error(`GHL PUT ${path} → ${resp.status}: ${await resp.text()}`);
  }
  return resp.json();
}

async function ghlPost(apiKey: string, path: string, body: any): Promise<any> {
  const resp = await fetch(`${GHL_API}${path}`, {
    method: "POST",
    headers: ghlHeaders(apiKey, true),
    body: JSON.stringify(body),
  });
  // Notes endpoint may return 200 or 201
  if (!resp.ok) {
    throw new Error(`GHL POST ${path} → ${resp.status}: ${await resp.text()}`);
  }
  return resp.json();
}

// ── Main Action ──────────────────────────────────────────────────────────

export const processSignedBla = internalAction({
  args: { eventId: v.id("boldsignEvents") },
  handler: async (ctx, { eventId }) => {
    // 1. Read the event
    const event = await ctx.runQuery(
      (internal as any).boldsignFns.getEvent,
      { eventId }
    );
    if (!event || event.status !== "pending") {
      console.log(`BoldSign action: event ${eventId} not pending (status: ${event?.status}), skipping`);
      return;
    }

    const { documentId, documentTitle } = event;
    const ghlApiKey = process.env.GHL_API_KEY;
    if (!ghlApiKey) {
      console.error("BoldSign action: GHL_API_KEY not set");
      await ctx.runMutation(
        (internal as any).boldsignFns.markEventProcessed,
        { eventId, error: "GHL_API_KEY not configured in Convex env" }
      );
      return;
    }

    console.log(`⚡ BoldSign instant processor: ${documentTitle} (${documentId})`);

    // ── Step 1: Find GHL contact by BoldSign doc ID custom field ──────
    // Search BOTH pipelines (Seller + Referred Deals) for opps at BLA Sent stage
    let contactId: string | null = null;
    let oppId: string | null = null;
    let oppName: string | null = null;
    let matchedPipeline: typeof PIPELINES[number] | null = null;

    for (const pipeline of PIPELINES) {
      if (contactId) break;
      try {
        console.log(`BoldSign action: searching ${pipeline.name} at BLA Sent stage...`);
        const oppData = await ghlGet(ghlApiKey, "/opportunities/search", {
          location_id: LOCATION_ID,
          pipeline_id: pipeline.id,
          pipeline_stage_id: pipeline.blaSentStage,
          limit: "50",
        });
        const opps = oppData.opportunities || [];
        console.log(`BoldSign action: ${pipeline.name} — ${opps.length} opps at BLA Sent stage`);

        for (const opp of opps) {
          const cId = opp.contactId || opp.contact?.id;
          if (!cId) {
            console.log(`BoldSign action: opp ${opp.id} has no contactId, skipping`);
            continue;
          }

          console.log(`BoldSign action: checking contact ${cId} for opp "${opp.name}"`);
          const cData = await ghlGet(ghlApiKey, `/contacts/${cId}`);
          const contact = cData.contact || cData;
          const customFields = contact.customFields || [];

          for (const cf of customFields) {
            if (cf.id === BOLDSIGN_DOC_FIELD) {
              console.log(`BoldSign action: found BoldSign field — value="${cf.value}", looking for="${documentId}", match=${cf.value === documentId}`);
              if (cf.value === documentId) {
                contactId = cId;
                oppId = opp.id;
                oppName = opp.name || contact.contactName || contact.name || "Unknown";
                matchedPipeline = pipeline;
                break;
              }
            }
          }
          if (contactId) break;
        }
      } catch (e: any) {
        console.error(`BoldSign action: ${pipeline.name} search error:`, e.message);
      }
    }

    // ── Fallback: get signer email from BoldSign, search GHL ──────────
    if (!contactId) {
      const boldsignApiKey = process.env.BOLDSIGN_API_KEY;
      if (boldsignApiKey) {
        try {
          const docResp = await fetch(
            `https://api.boldsign.com/v1/document/properties?documentId=${documentId}`,
            { headers: { "X-API-KEY": boldsignApiKey } }
          );
          if (docResp.ok) {
            const docData = await docResp.json();
            const seller = (docData.signerDetails || []).find(
              (s: any) => (s.signerRole || "").toLowerCase() === "seller"
            );
            if (seller?.signerEmail) {
              console.log(`BoldSign action: fallback — searching GHL for ${seller.signerEmail}`);
              const searchData = await ghlGet(ghlApiKey, "/contacts/", {
                locationId: LOCATION_ID,
                query: seller.signerEmail,
              });
              const contacts = searchData.contacts || [];
              if (contacts.length > 0) {
                contactId = contacts[0].id;
                // Search both pipelines for this contact's opp
                for (const pipeline of PIPELINES) {
                  if (oppId) break;
                  const oppData2 = await ghlGet(ghlApiKey, "/opportunities/search", {
                    location_id: LOCATION_ID,
                    pipeline_id: pipeline.id,
                    contact_id: contactId!,
                    limit: "1",
                  });
                  const opps2 = oppData2.opportunities || [];
                  if (opps2.length > 0) {
                    oppId = opps2[0].id;
                    oppName = opps2[0].name || contacts[0].name;
                    matchedPipeline = pipeline;
                  }
                }
              }
            }
          }
        } catch (e: any) {
          console.error("BoldSign action: fallback search error:", e.message);
        }
      } else {
        console.log("BoldSign action: BOLDSIGN_API_KEY not set — skipping fallback lookup");
      }
    }

    if (!contactId) {
      const err = `Could not match document ${documentId} to any GHL contact`;
      console.error(`BoldSign action: ${err}`);
      await ctx.runMutation(
        (internal as any).boldsignFns.markEventProcessed,
        { eventId, error: err }
      );
      return;
    }

    // Default to Seller Pipeline if matched via fallback without pipeline context
    if (!matchedPipeline) {
      matchedPipeline = PIPELINES[0];
      console.log("BoldSign action: no pipeline context from match — defaulting to Seller Pipeline");
    }

    console.log(`BoldSign action: matched → ${oppName} on ${matchedPipeline.name} (contact: ${contactId})`);

    // ── Step 2: Advance pipeline → Listing Agreement Signed - Deal Won ─
    const targetStage = matchedPipeline.blaSignedStage;
    let pipelineAdvanced = false;
    if (oppId) {
      try {
        await ghlPut(ghlApiKey, `/opportunities/${oppId}`, {
          pipelineStageId: targetStage,
        });
        pipelineAdvanced = true;
        console.log(`BoldSign action: ${matchedPipeline.name} advanced → Listing Agreement Signed - Deal Won ✅`);
      } catch (e: any) {
        console.error("BoldSign action: pipeline advance error:", e.message);
      }
    }

    // ── Step 3: Add GHL note ──────────────────────────────────────────
    try {
      await ghlPost(ghlApiKey, `/contacts/${contactId}/notes`, {
        body: `✅ BLA fully signed (BoldSign: ${documentId}). Pipeline auto-advanced to "Listing Agreement Signed - Deal Won" on ${matchedPipeline.name}. Onboarding initiated.`,
      });
      console.log("BoldSign action: GHL note added ✅");
    } catch (e: any) {
      // Non-critical — log and continue
      console.log("BoldSign action: note add failed (non-critical):", e.message);
    }

    // ── Step 4: Update portal deal stage ──────────────────────────────
    try {
      await ctx.runMutation(
        (internal as any).viktorSync.syncStage,
        {
          ghlContactId: contactId,
          stage: targetStage,
          stageLabel: "Listing Agreement Signed - Deal Won",
        }
      );
      console.log("BoldSign action: portal stage synced ✅");
    } catch (e: any) {
      // Deal may not exist in portal yet — that's expected
      console.log("BoldSign action: portal sync skipped (deal may not exist yet)");
    }

    // ── Step 5: Mark event as GHL-updated ─────────────────────────────
    // Data room creation + signed BLA storage follow via Viktor
    await ctx.runMutation(
      (internal as any).boldsignFns.markGhlUpdated,
      {
        eventId,
        ghlContactId: contactId,
        oppId: oppId || undefined,
        oppName: oppName || undefined,
        results: JSON.stringify({
          pipelineAdvanced,
          pipelineName: matchedPipeline.name,
          targetStage,
          contactId,
          oppId,
          oppName,
          processedAt: new Date().toISOString(),
          awaitingOnboarding: true,
        }),
      }
    );

    console.log(`⚡ BoldSign instant processor: DONE — ${oppName} (${matchedPipeline.name}) updated in ${Date.now() - event.receivedAt}ms`);
  },
});
