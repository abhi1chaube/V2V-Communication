# Frontend Fleet Refactor: Two Fixed Vehicles → Any Number

Written for: developers maintaining `FRONTEND/` and the backend endpoints it calls.
See `BACKEND/FLEET_REFACTOR.md` for the backend registry this builds on.

## 1. Dependency map

**Files**
- `index.html`
- `style.css`
- `script.js`

The backend serves no templates. The page is static and calls `http://127.0.0.1:5000`. (The backend has since moved from Flask to FastAPI with an identical API; see `BACKEND/FLEET_REFACTOR.md` section 7. Nothing in this frontend changed.)

**API calls**

| Method | Path | Called from |
|---|---|---|
| GET | `/api/fleet` (new) | `getFleetData()`. Polled every 800 ms, or every 2 s above 5 vehicles. |
| GET | `/api/health` | `checkHealth()`. At startup, then every 10 s. |
| GET | `/api/communication` | `getCommunicationData()` |
| GET | `/api/events?limit=8` | `getEvents()` |
| POST | `/api/start`, `/api/loading-point`, `/api/loading/start`, `/api/loading/update`, `/api/warehouse`, `/api/reset`, `/api/emergency-stop` | workflow and command functions |

`/api/vehicles` is no longer called by the dashboard. It is unchanged for any other consumer.

**Vehicle-specific references removed**
- **HTML:**
  - the `#vehicleCardYellow` and `#vehicleCardBlue` cards, with their `yellow*` / `blue*` IDs
  - the `#yellowRoadVehicle` and `#blueRoadVehicle` markers
  - the `"V01 ↔ V02"` text
- **CSS:**
  - `.yellow-card` / `.blue-card`
  - `.vehicle-indicator.yellow` / `.blue`
  - `#yellowStatus`, `#blueStatus`, and the other ID rules
  - `.yellow-fill` / `.blue-fill`
  - `.yellow-road-vehicle` / `.blue-road-vehicle`
  - `.yellow-mini` / `.blue-mini`
  - the fixed `grid-template-columns: 1fr 1fr`
- **JS:** every function that took or hard-coded `"yellow"` / `"blue"`. The full list is in section 3.

## 2. Backend changes this depends on

| File | Change | Why | Risk |
|---|---|---|---|
| `BACKEND/app.py` | New `GET /api/fleet` returns `{count, vehicles: [...]}` in registry order | The dashboard needs an ordered list, not a colour-keyed object | Additive. No existing route changed. |
| `BACKEND/app.py` | `state_to_telemetry` also sends `mission_status` | Without it, the next ThingsBoard read reverted the mission set by a local command | ThingsBoard receives one extra key that already exists in simulator telemetry |
| `BACKEND/services/state.py` | Each record has `vehicle_id`. New fields default to `None`: `next_action`, `last_seen`, `communication_status`, `vehicle_online`, `battery_level`, `signal_strength`, `destination`, `distance_to_destination`. `apply_telemetry_state` copies them (plus `mission_status`) **only if ThingsBoard reported them**. | These fields existed in ThingsBoard but never reached the API | Default values from `get_latest_state` (for example `mission_status="UNKNOWN"`) are ignored, using the new `_reported` list |
| `BACKEND/services/thingsboard.py` | Reads the four new keys, adds `_reported` to `get_latest_state()`, and no longer repeats the GET on every call (the retry now runs only after a 401) | Supplies the fields above and halves ThingsBoard traffic | If a deployment relied on the duplicate request (none known), it no longer happens |

## 3. Code changes

### 3.1 `index.html`

**Old:** two hand-written cards, about 190 lines each. Example:
```html
<article id="vehicleCardYellow" class="vehicle-card yellow-card">
  ...
  <strong id="yellowStatus">READY</strong>
```

**New:** an empty container plus one template that is cloned per vehicle.
```html
<section id="vehicleGrid" class="vehicle-dashboard" aria-label="Vehicles">
    <p id="vehicleEmpty" class="vehicle-empty">Waiting for fleet data...</p>
</section>
...
<template id="vehicleCardTemplate">
  <article class="vehicle-card">
    ...
    <strong data-field="status">—</strong>
```
The two road vehicles became `<template id="roadVehicleTemplate">` inside `#routeMap`.

**Added sections**, all using the existing `.panel` / `.info-box` look:
- Fleet Summary (six `data-stat` tiles) and Alerts (`aria-live="polite"`)
- Search box and filter button group
- Vehicle Detail (a `<select>` plus 11 `data-detail` fields) and Fleet Table (`<tbody>` generated)
- `#commandStatus` now has `role="status"` and `aria-live="polite"`. The load bar has `role="progressbar"` and `aria-valuenow`.

**Risk:** any external script that targeted the removed IDs (`#yellowStatus` and others) would break. None was found.

### 3.2 `style.css`

**Old:**
```css
.vehicle-dashboard { grid-template-columns: 1fr 1fr; }
.yellow-card::before { background: #facc15; }
.blue-card::before   { background: #2563eb; }
.yellow-fill { background: linear-gradient(90deg, #fde047, #eab308); }
.yellow-road-vehicle { margin-top: -12px; }
```

**New:**
```css
.vehicle-dashboard {
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
}

.vehicle-card, .road-vehicle, .vehicle-dot {
    --vehicle-accent: #64748b;   /* neutral default; other shades derived with color-mix() */
}

.accent-yellow { --vehicle-accent: #facc15; --vehicle-light: #fde047; --vehicle-deep: #eab308; ... }
.accent-blue   { --vehicle-accent: #2563eb; --vehicle-light: #60a5fa; --vehicle-deep: #2563eb; ... }

.vehicle-card::before         { background: var(--vehicle-accent); }
.vehicle-card .progress-fill  { background: linear-gradient(90deg, var(--vehicle-light), var(--vehicle-deep)); }
```

**Why each part:**
- `auto-fit` stretches two cards to half width each, which reproduces the old look, and wraps any number of cards.
- Yellow and blue keep their exact original shades.
- `Vehicle_NN_COLOR` from `.env` (any CSS colour) works through the inline `--vehicle-accent`.

**Added at the end:**
- styles for the new sections
- `minmax(0, …)` grid tracks, so the table scrolls instead of widening the page on mobile
- a 9 px minimum text size, where the compact layout had 6–8 px
- a `prefers-reduced-motion` block

**Risks:**
- `color-mix()` needs Chrome/Edge 111+, Firefox 113+, or Safari 16.2+. Older browsers lose the neutral and custom accents only; yellow and blue are unaffected.
- Labels at 6–8 px are now 9 px, so some cards are slightly taller.

### 3.3 `script.js`

Every function that took or hard-coded a colour was converted. The main changes:

| Old | New | Notes |
|---|---|---|
| `state.lastData = {yellow, blue}` | `state.fleet` (a `Map` keyed by `vehicle_id`) plus `state.order` | Filled by `setFleet()` from `/api/fleet` |
| `vehicleCard(color)`, `updateVehicleCard(color, v)` with colour IDs | `createVehicleCard(id)` and `updateVehicleCard(card, v)` using `[data-field]` | Status, brake, QR, radar, and load logic unchanged |
| `setRoutePosition("yellow", pos)` | `setRoutePosition(vehicleId, pos)` | Lane offset via `laneOffset()`. With 2 vehicles it gives the original −12/+12 px. |
| `updateRouteStops(yellow, blue)`, `updateRadar(yellow, blue)`, `updateSensorHealth(yellow, blue)`, `updateButtons()` | Iterate `fleetVehicles()` | |
| `startResume()`: yellow only | Starts `state.order[0]` | Same checks and messages |
| `startLoading()`: yellow, then blue | First vehicle in order at Stop 2 with load < 100 | When full: `startFleetHandover(id, next)`, or `startFinalWarehouseMovement(id)` if it is last |
| `startFleetHandover` / `animateFleetHandover` / `completeFleetHandover` (yellow → blue) | Same names with `(fromId, toId)` | Works for any consecutive pair |
| `startSecondFleetHandover` / `animateSecondWarehouseMovement` / `completeSecondWarehouseMovement` | `startFinalWarehouseMovement(id)` / `completeFinalWarehouseMovement(id)` | |
| Three copy-pasted `requestAnimationFrame` loops | `animateRoute(moves, {isActive, onEmergency, onComplete})` | |
| `postCommand(url, vehicle = "yellow")` | `vehicle` is required | |
| `emergencyCommand` sends `"yellow"` | Sends the first vehicle, plus `fleet: true` | The backend stops all vehicles |
| `resetCycle` reads `resetVehicles.yellow/.blue` | Iterates the returned object | |
| `addLog()` returned its argument | Stores client messages and merges them with backend events in `renderLog()` | **Bug fix:** the messages were never shown |
| `checkHealth()` ran once | Runs every 10 s | **Bug fix:** the ThingsBoard status went stale |
| `setInterval(getVehicleData, 800)` | `scheduleRefresh()` with an in-flight guard; `refreshFleet(true)` after commands | **Bug fix:** polls could overlap |
| `unusedDuplicateLoadingHandler()` | Deleted | Dead code that used undefined variables |

**New code:**
- `normalizeVehicle()` maps backend field names to dashboard names. It is the only place that knows the backend names.
- `isOnline()` and `missionKind()` classify vehicles.
- `FILTERS` and `ALERT_RULES` are config arrays. Add an entry to add a filter button or an alert type.
- `matchesSearch()`, `applyVisibility()`, `renderOverview()`, `renderAlerts()`, `renderSelector()`, `renderDetail()`, and `renderTable()` drive the new sections.
- `syncKeyedChildren()` adds, removes, and reorders generated elements without rebuilding them on each poll.

**Field mapping** (in `normalizeVehicle`):

| Dashboard field | Backend field |
|---|---|
| `vehicle_status` | `status` |
| `communication_status` | `communication_status`, falling back to `v2v_status` |
| `battery_level` | `battery_level` only. `battery_percentage` is a placeholder 100, so `n/a` is shown instead. |
| `signal_strength` | `signal_strength` only. Same reason for `network_signal`. |
| `emergency_status` | `emergency_stop` |

## 4. Behaviour notes and risks

- **Online definition.** A vehicle is online when the backend is up, it has no `_thingsboard_error`, `vehicle_online` is not false, and `last_seen` (if reported) is within 30 s. A vehicle whose ThingsBoard `last_seen` is stale, for example from an old simulator run, shows **Offline** even if dashboard commands still work.
- **The "Communication Lost" alert** uses only a reported `communication_status`. The simulator reports `DISCONNECTED` for most steps, so the alert fires while it runs.
- **Handover order** follows the registry: V01 → V02 → … → last vehicle, which does the final warehouse move. With two vehicles the behaviour and messages are the same as before.
- **Route map.** Vehicles at the same stop are spread across lanes, but with many vehicles they overlap. The map is a summary view. Use the table for exact positions.
- **Communication text** is now `"N/N VEHICLES CONNECTED"`. It was `"V01 ↔ V02 CONNECTED"`.

## 5. Verification performed

Tests ran in headless Edge against the real Flask app, with ThingsBoard faked so there was no network access. Extra vehicles were registered only through environment variables.

- **2 vehicles:**
  - Layout, colours, and lane offsets match the original.
  - START → loading → handover → final warehouse → RESET completes with the original status messages.
  - No page errors.
- **3 vehicles:** V01 → V02 → V03 chained handovers, final warehouse, and reset all complete.
- **5 and 20 vehicles:**
  - Cards wrap, and the table, selector, and filters render.
  - Overview counts are correct.
  - The planted low-battery, offline, and comms-lost alerts appear once each.
- **Search:** `2`, `vehicle 02`, and `Vehicle_02` each match only V02. `zzz` shows the empty message.
- **Filters and selector:** filters show correct counts. The selector updates the detail panel and the table selection.
- **Layout:** no horizontal page overflow at desktop, tablet, or mobile widths with 20 vehicles. The table scrolls inside its own container.
- **Not covered:**
  - Real ThingsBoard and hardware.
  - Emergency stop: `/api/emergency-stop` fails because of an existing backend bug (`HardwareController` has no `activate_emergency_stop`). See the backend notes.
  - Real-browser animation timing. Headless tests drove `requestAnimationFrame` from timers.
