"""Condition script: exit 0 if there are ghl_updated BoldSign events (data room pending), exit 1 otherwise."""
import sys
import httpx

SYNC_URL = "https://energetic-antelope-119.convex.site/api/viktor-sync"
SYNC_SECRET = "ebb-sync-k7X9mP2vQ4nR8wL1"

try:
    resp = httpx.post(
        SYNC_URL,
        json={"action": "has_ghl_updated_boldsign_events"},
        headers={"Authorization": f"Bearer {SYNC_SECRET}"},
        timeout=10,
    )
    data = resp.json()
    if data.get("hasPending"):
        print("Found ghl_updated BoldSign events — data room pending")
        sys.exit(0)
except Exception as e:
    print(f"Error checking: {e}")

sys.exit(1)
