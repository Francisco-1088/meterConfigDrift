# config_drift.py — Configuration Drift Tool

A standalone Flask web app that compares VLAN and SSID configuration between any two Meter networks, across any companies your API token has access to. Differences are highlighted in real time with expandable detail rows.

---

## File Dependencies

| File | Role |
|------|------|
| `config_drift.py` | The entire application — Python backend, Flask routes, HTML/CSS/JS all in one file |
| `compliance_config.py` | API credentials and company slugs |
| `requirements.txt` | Python package dependencies (Flask, requests) |

### `compliance_config.py` fields

```python
API_URL   = "https://api.meter.com/api/v1/graphql"
API_TOKEN = "v2.public...."          # Bearer token from Meter dashboard
COMPANIES = ["meter", "slug-two"]    # All company slugs available for selection in the UI
```

All three fields are required. Add or remove slugs from `COMPANIES` to control which companies appear in the network dropdowns.

---

## How to Run

### Prerequisites

```bash
cd meterPublicApi
source .venv/bin/activate
pip install -r requirements.txt   # flask + requests already installed
```

### Start the server

```bash
python3 config_drift.py
```

Starts on **port 8083** by default. Override with `PORT`:

```bash
PORT=9000 python3 config_drift.py
```

Open `http://localhost:8083` in a browser. Network lists load automatically at startup.

### Port reference (all standalone tools)

| Tool | Default port |
|------|-------------|
| `multi_company_server.py` | 8081 |
| `pci_report.py` | 8082 |
| `config_drift.py` | 8083 |

---

## How the Code Works

### Startup

On startup, a daemon thread calls `_preload_networks()`, which iterates every slug in `COMPANIES` and calls `networksForCompany` for each one. Results are stored in `_networks_cache` (`{ slug: [{UUID, label}] }`). This is fast — it fetches only names, no device or config data.

### When the user selects both networks

`POST /api/compare` is called with `{ uuidA, uuidB }`. The server fetches VLANs and SSIDs for both networks **in parallel** using two daemon threads, then returns:

```json
{
  "a": { "vlans": [...], "ssids": [...] },
  "b": { "vlans": [...], "ssids": [...] }
}
```

Each side is fetched with a single bundled GraphQL query:

```graphql
{
  vlans(networkUUID: "...") {
    UUID name vlanID isEnabled
    ipV4ClientGateway ipV4ClientPrefixLength ipV4ClientAssignmentProtocol
  }
  ssids: ssidsForNetwork(networkUUID: "...") {
    UUID ssid isEnabled band
  }
}
```

The browser JS then computes the diff entirely client-side and renders the results.

### Rate-limit handling

Every GraphQL response is checked for `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers. If remaining requests fall below `PROACTIVE_THRESHOLD = 20`, the next request sleeps until the reset time. HTTP 429 responses read `Retry-After` and back off. Requests retry up to `MAX_RETRIES = 3` times with exponential backoff on timeout.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the single-page UI |
| `GET` | `/api/networks` | Returns all companies and their networks from the startup cache |
| `POST` | `/api/networks/refresh` | Re-runs `_preload_networks()` in a background thread |
| `POST` | `/api/compare` | Body: `{ uuidA, uuidB }` — fetches and returns VLANs + SSIDs for both networks |

`/api/networks` also returns a `ready` boolean that is `false` while the initial preload is still running. The browser polls every 3 seconds until `ready` is `true`.

---

## UI Walkthrough

### Topbar

| Element | Function |
|---------|----------|
| ↺ Refresh | Re-fetches the network list from the API and repopulates dropdowns |
| 🌙 / ☀️ | Light/dark mode toggle; preference saved to `localStorage` |
| Status indicator | Shows loading state, network count, or error messages |

### Dropdowns

Four dropdowns in a `Company A / Network A — vs — Company B / Network B` layout. Selecting any company filters the network dropdown to that company's networks. The comparison fires automatically when both a network on the A side and a network on the B side are selected.

You can compare:
- Two networks in the **same company** (e.g. primary vs. a branch)
- Two networks in **different companies**
- The **same network against itself** (will show everything as matching)

### Summary bar

Seven counters appear once a comparison is rendered:

| Counter | Meaning |
|---------|---------|
| VLANs match | VLAN IDs present in both networks |
| VLAN only-one | VLAN IDs that exist on only one side |
| VLAN field diffs | VLANs present in both but with differing name, gateway, prefix, DHCP mode, or enabled state |
| SSIDs match | SSIDs present in both networks |
| SSID only-one | SSIDs that exist on only one side |
| SSID field diffs | SSIDs present in both but with differing band or enabled state |
| Total diffs | Sum of all only-one + field diffs |

### Results grid

Two side-by-side cards — **VLANs** and **SSIDs** — each listing every item found across both networks.

#### Row color coding

| Indicator | Meaning |
|-----------|---------|
| `✓ Both` (green) | Item exists in both networks |
| `← A only` / `→ B only` (red) | Item exists on only one side |
| `≠ diff` (yellow badge) | Item exists in both but at least one field differs |

#### Expanding a row

Click any row to expand it and see a three-column comparison table:

| Column 1 | Column 2 | Column 3 |
|----------|----------|----------|
| Field name | Network A value | Network B value |

Fields that differ between the two networks are highlighted in **yellow**. Fields that match are shown in the default text color.

**VLAN fields compared:** Name, Gateway IP, Prefix length, DHCP mode, Enabled

**SSID fields compared:** SSID name, Band, Enabled

---

## Differences from the Config Drift Tab in `multi_company_server.py`

| Aspect | `config_drift.py` (standalone) | `multi_company_server.py` (embedded tab) |
|--------|-------------------------------|------------------------------------------|
| Data source | Fetches on demand when networks are selected | Uses pre-fetched in-memory snapshot |
| Latency | ~1–2s API call per comparison | Instant (data already cached) |
| Extra counters | VLAN field diffs, SSID field diffs | Not shown separately |
| Dependencies | Only `compliance_config.py` | Requires full `config.py` + all fetch steps |
| Port | 8083 | Part of the 8081 server |
