# EBB Automation Scripts

Internal automation scripts for Exclusive Business Brokers — BLA generation, GHL workflows, and deal management tools.

## Scripts

### `bla/generate_bla.py` — Business Listing Agreement Generator

Zero-touch BLA generation and e-signature flow:

1. **Search** GHL for a seller contact by name or email
2. **Extract** custom fields (business name, address, price, commission, etc.)
3. **Fill** the BLA PDF template with matched fonts (bold/regular, 9.5pt Times New Roman)
4. **Send** the pre-filled PDF to BoldSign for e-signatures
5. **Signing order:** Seller signs first → Broker (Jarrod) counter-signs → Jack CC'd

```bash
# Full send (live)
python bla/generate_bla.py "seller@example.com"

# Preview only — fill PDF, don't send
python bla/generate_bla.py "seller@example.com" --dry-run --preview
```

**Template variables filled automatically from GHL:**
| Variable | Source |
|---|---|
| `{{start_date}}` | Current date |
| `{{end_date}}` | +4 months |
| `{{business_name}}` | GHL custom field |
| `{{business_address}}` | GHL custom field |
| `{{business_website}}` | GHL custom field |
| `{{purchase_price_formatted}}` | GHL custom field (formatted as $X,XXX) |
| `{{commission_percentage}}` | GHL custom field |
| `{{seller_name}}` | GHL first + last name |
| `{{seller_title}}` | GHL custom field |

**E-Signature fields (BoldSign):**
- Seller: Signature + Date on page 3, aligned to underscore lines
- Broker: Signature + Date on page 3, aligned to underscore lines

### `config/ghl_fields.json`

GHL field mappings, pipeline stage IDs, and team user IDs.

## Tech Stack

- **PDF fill:** PyMuPDF (fitz) — redact + replace with font-matched text
- **E-signatures:** BoldSign API (`/v1/document/send`)
- **CRM:** GoHighLevel (GHL) API
- **Runtime:** Python 3.13+ / Viktor AI agent

## Integration Keys

| Service | Status |
|---|---|
| GHL (HighLevel) | ✅ Connected via OAuth |
| BoldSign | ✅ Production API key |
| Google Drive | ✅ Connected |

---

*Maintained by Viktor AI · Exclusive Business Brokers Inc.*
