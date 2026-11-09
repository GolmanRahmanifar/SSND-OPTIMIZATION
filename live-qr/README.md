# Freight Network Lab — Live Logistics Game

Paper-based game + **one QR form for each of four teams** + real-time projector results.

## Run the real application

Download this branch and open its `live-qr` directory. On Windows, double-click `START_WINDOWS.bat`. On macOS/Linux, run `bash START_MAC_LINUX.sh`. Requires Python 3 and one-time dependency install.

The terminal prints three links and a generated **facilitator password**:

- **QR projector:** `http://YOUR_LAPTOP_WIFI_IP:8765/`
- **Facilitator:** `http://localhost:8765/control`
- **Live graph:** `http://YOUR_LAPTOP_WIFI_IP:8765/leaderboard`

The laptop and all team phones must be on the **same Wi-Fi** and must be permitted to communicate. Every QR code is dynamically generated for its team, pointing to the server's actual configured IP. Local Wi-Fi may have client isolation; use a trusted hotspot if needed.

### Live flow

1. Show the four QR codes on the projector and start the 30/60-minute countdown.
2. Groups solve the game on paper, then one member scans the QR and submits **Trucks, Total KM, Average Load Factor**, with optional photo.
3. The server calculates `€500 * active_trucks + €1 * total_km` but keeps results unverified.
4. Facilitator reviews each team's paper route plan and approves/rejects it through `/control`.
5. Click **Reveal leaderboard** to display the verified winner and live cost/distance/truck/utilization charts. Export results as CSV for Excel.

**Only verified plans are ranked.** Load factor is a comparison KPI, **not** the competition objective. The metrics-only form cannot establish that a truck route is actually feasible; that requires manual verification.

### Internet hosting

GitHub Pages cannot host this Python server. For HTTPS access, deploy `live-qr` on a Python web-hosting service and set `PUBLIC_BASE_URL=https://YOUR_HOST` and `FNL_ADMIN_PASSWORD=STRONG_SECRET`. Use **persistent storage** for the `data/` directory. On some free hosting tiers storage disappears on restart.

### Technical notes

- One Python standard-library HTTP server plus `qrcode[pil]`.
- Team results persisted in local SQLite `data/freight_network_lab.sqlite3`.
- Team QR invitations have distinct unguessable tokens. The facilitator view requires a separate login.
- To rehearse, use **Load four reference plans**, verify them manually, reveal, then **Clear all team submissions** before live play.
- Do not expose the `data/` folder or facilitator password in a public repository.
- Dashboard and team form are responsive; designed for 16:9 projection and phone browsers.
