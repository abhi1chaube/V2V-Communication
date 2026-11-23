/* =========================================================
   V2V FLEET CONTROL SYSTEM
   FRONTEND - DYNAMIC FLEET

   Vehicles are loaded from GET /api/fleet. Cards, route
   markers, table rows, selector options, alerts and filter
   counts are all generated from that list, so adding a
   vehicle (Vehicle Manager -> BACKEND/data/vehicles.json,
   see vehicle-manager.js) needs no change in this file,
   index.html or style.css.

   CONFIGURATION
========================================================= */

const localHostnames = ["localhost", "127.0.0.1", "[::1]"];
const configuredApiBaseUrl = window.V2V_CONFIG?.apiBaseUrl?.trim();
const isLocalDashboard = window.location.protocol === "file:"
    || localHostnames.includes(window.location.hostname);
const defaultApiBaseUrl = isLocalDashboard
    ? "http://127.0.0.1:5000"
    : "";
const API_BASE_URL = (configuredApiBaseUrl || defaultApiBaseUrl)
    .replace(/\/+$/, "");

const REFRESH_INTERVAL =
    800;

/* Every poll reads each vehicle from ThingsBoard, so larger
   fleets poll more slowly. */
const LARGE_FLEET_SIZE =
    5;

const LARGE_FLEET_REFRESH_INTERVAL =
    2000;

const HEALTH_INTERVAL =
    10000;

/* A vehicle whose last_seen is older than this is offline. */
const OFFLINE_AFTER_SECONDS =
    30;

const LOW_BATTERY_PERCENT =
    20;

const MOVE_DURATION =
    3200;

const CLIENT_LOG_LIMIT =
    20;

const LOG_DISPLAY_LIMIT =
    12;

/* Route positions in % of the route width. */
const ROUTE = {

    stop1: 5,

    start: 8,

    loading: 50,

    warehouse: 92
};

/* Vertical lane spacing for route markers (px). Two vehicles
   keep the original -12px / +12px offsets. */
const LANE_SPACING =
    24;

const MAX_LANE_SPREAD =
    48;

/* Colours with an exact palette class (.accent-*) in style.css. */
const ACCENT_CLASSES = [
    "yellow",
    "blue"
];


/* =========================================================
   BACKEND ENDPOINTS
========================================================= */

const API = {

    fleet:
        `${API_BASE_URL}/api/fleet`,

    health:
        `${API_BASE_URL}/api/health`,

    communication:
        `${API_BASE_URL}/api/communication`,

    events:
        /* More than LOG_DISPLAY_LIMIT, so J2735 BSM lines do not
           crowd command and safety events out of the fetch. */
        `${API_BASE_URL}/api/events?limit=30`,

    start:
        `${API_BASE_URL}/api/start`,

    loadingPoint:
        `${API_BASE_URL}/api/loading-point`,

    loadingStart:
        `${API_BASE_URL}/api/loading/start`,

    loadingUpdate:
        `${API_BASE_URL}/api/loading/update`,

    warehouse:
        `${API_BASE_URL}/api/warehouse`,

    /* Completes a journey at the warehouse and performs the V2V
       handover to the next vehicle (logged by the backend). */
    complete:
        `${API_BASE_URL}/api/complete`,

    reset:
        `${API_BASE_URL}/api/reset`,

    emergency:
        `${API_BASE_URL}/api/emergency-stop`,

    emergencyRelease:
        `${API_BASE_URL}/api/emergency-release`
};


/* =========================================================
   GLOBAL STATE
========================================================= */

const state = {

    backendOnline: false,

    healthData: null,

    communicationData: null,

    /* vehicle_id -> normalized vehicle, in fleet order */
    fleet: new Map(),

    order: [],

    /* True after the first successful /api/fleet response. */
    fleetLoaded: false,

    selectedVehicle: null,

    search: "",

    filter: "all",

    alertSignature: null,

    commandBusy: false,

    /* True only after the backend confirmed the emergency stop. */
    emergency: false,

    /* An emergency stop / release request is in flight. */
    emergencyRequest: false,

    /* Bumped by Reset and Emergency Stop. Every running cycle
       step remembers the value it started with and stops after
       its current await when the value changed. */
    cycleGeneration: 0,

    visualMove: null,

    loadingCycle: false,

    handoverRunning: false,

    fetchPromise: null,

    backendEvents: [],

    clientLog: [],

    /* Activity Log rows whose J2735 message is expanded (by logKey). */
    expandedLogs: new Set(),

    /* Last rendered log content, so polls only touch the DOM
       when something changed. */
    logSignature: null
};


/* Generated DOM elements, keyed by vehicle_id. */
const elements = {

    cards: new Map(),

    route: new Map(),

    rows: new Map()
};


/* =========================================================
   FILTERS AND ALERT RULES
   Add an entry here to add a filter button or alert type.
========================================================= */

const FILTERS = [

    {
        id: "all",
        label: "All Vehicles",
        test: () => true
    },

    {
        id: "online",
        label: "Online",
        test: vehicle => isOnline(vehicle)
    },

    {
        id: "offline",
        label: "Offline",
        test: vehicle => !isOnline(vehicle)
    },

    {
        id: "active",
        label: "Active Missions",
        test: vehicle => missionKind(vehicle) === "active"
    },

    {
        id: "idle",
        label: "Idle",
        test: vehicle => missionKind(vehicle) === "idle"
    },

    {
        id: "emergency",
        label: "Emergency",
        test: vehicle => vehicle.emergency_status
    }
];


const ALERT_RULES = [

    {
        type: "emergency",
        label: "Emergency Active",
        severity: "critical",
        test: vehicle => vehicle.emergency_status
    },

    {
        type: "offline",
        label: "Vehicle Offline",
        severity: "critical",
        test: vehicle => !isOnline(vehicle)
    },

    {
        /* Only the reported communication_status is used here,
           not the v2v_status fallback, which defaults to
           "disconnected" when nothing was reported. */
        type: "communication",
        label: "Communication Lost",
        severity: "warning",
        test: vehicle =>
            isOnline(vehicle) &&
            hasValue(vehicle.reported_communication_status) &&
            norm(vehicle.reported_communication_status) !== "connected"
    },

    {
        type: "battery",
        label: `Battery Below ${LOW_BATTERY_PERCENT}%`,
        severity: "warning",
        test: vehicle =>
            vehicle.battery_level !== null &&
            vehicle.battery_level < LOW_BATTERY_PERCENT
    },

    {
        type: "mission",
        label: "Mission Failed",
        severity: "critical",
        test: vehicle => missionKind(vehicle) === "failed"
    }
];


/* =========================================================
   BASIC HELPERS
========================================================= */

const $ = id =>
    document.getElementById(id);


function num(
    value,
    fallback = 0
) {

    const n =
        Number(value);

    return Number.isFinite(n)
        ? n
        : fallback;
}


function norm(value) {

    return String(
        value ?? ""
    )
        .toLowerCase()
        .trim()
        .replace(/-/g, "_")
        .replace(/\s+/g, "_");
}


function hasValue(value) {

    return (
        value !== undefined &&
        value !== null &&
        value !== ""
    );
}


function clampPercent(value) {

    return Math.max(
        0,
        Math.min(
            100,
            num(value)
        )
    );
}


function displayText(value) {

    return hasValue(value)
        ? String(value).replace(/_/g, " ").toUpperCase()
        : "—";
}


function locationText(value) {

    const v =
        norm(value);

    if (
        v.includes("stop_1")
    ) {
        return "Stop 1";
    }

    if (
        v.includes("stop_2")
    ) {
        return "Stop 2";
    }

    if (
        v.includes("stop_3") ||
        v.includes("warehouse")
    ) {
        return "Warehouse";
    }

    return value
        ? String(value)
        : "Unknown";
}


/* =========================================================
   VEHICLE IDENTITY HELPERS
========================================================= */

function vehicleNumber(vehicleId) {

    const match =
        String(vehicleId ?? "").match(/(\d+)\s*$/);

    return match
        ? match[1].padStart(2, "0")
        : "";
}


function shortName(vehicleId) {

    const number =
        vehicleNumber(vehicleId);

    return number
        ? `V${number}`
        : String(vehicleId ?? "");
}


function vehicleName(vehicleId) {

    return (
        state.fleet.get(vehicleId)?.name ||
        vehicleId ||
        "Vehicle"
    );
}


/* "Name (ID)", or just the name when it is the ID (the
   Vehicle Manager uses the name as the vehicle ID). */
function vehicleLabel(name, vehicleId) {

    return !name || name === vehicleId
        ? String(vehicleId ?? "")
        : `${name} (${vehicleId})`;
}


function joinNames(vehicleIds) {

    const names =
        vehicleIds.map(vehicleName);

    if (names.length <= 1) {
        return names.join("");
    }

    return (
        `${names.slice(0, -1).join(", ")} ` +
        `and ${names[names.length - 1]}`
    );
}


function getVehicle(vehicleId) {

    return state.fleet.get(vehicleId) || {};
}


function fleetVehicles() {

    return state.order
        .map(id => state.fleet.get(id))
        .filter(Boolean);
}


function firstVehicleId() {

    return state.order[0] || null;
}


function nextVehicleId(vehicleId) {

    const index =
        state.order.indexOf(vehicleId);

    if (
        index < 0 ||
        index >= state.order.length - 1
    ) {
        return null;
    }

    return state.order[index + 1];
}


/* =========================================================
   TIMESTAMPS
========================================================= */

/* The backend sends UTC ISO 8601 with a "Z" suffix
   (2026-11-16T06:08:41Z). Older ThingsBoard values may have
   no zone and microseconds: zone-less values are treated as
   UTC and trimmed to milliseconds before parsing. */
function parseTimestamp(value) {

    if (!hasValue(value)) {
        return null;
    }

    let text =
        String(value).trim();

    if (/^\d+(\.\d+)?$/.test(text)) {

        const epoch =
            Number(text);

        return epoch > 1e12
            ? epoch
            : epoch * 1000;
    }

    text =
        text.replace(/(\.\d{3})\d+/, "$1");

    if (!/(z|[+-]\d{2}:?\d{2})$/i.test(text)) {
        text += "Z";
    }

    const time =
        Date.parse(text);

    return Number.isFinite(time)
        ? time
        : null;
}


function formatLastSeen(value) {

    const time =
        parseTimestamp(value);

    if (time === null) {
        return "—";
    }

    const seconds =
        Math.max(
            0,
            Math.round((Date.now() - time) / 1000)
        );

    const clock =
        new Date(time).toLocaleTimeString();

    if (seconds < 60) {
        return `${clock} (${seconds}s ago)`;
    }

    if (seconds < 3600) {
        return `${clock} (${Math.round(seconds / 60)}m ago)`;
    }

    return new Date(time).toLocaleString();
}


/* Same format as backend event timestamps (UTC, seconds,
   "Z"), so client and backend log entries sort together. */
function utcTimestamp() {

    return new Date()
        .toISOString()
        .replace(/\.\d{3}Z$/, "Z");
}


/* A stored UTC timestamp shown in the viewer's local time:
   2026-11-16 11:38:41. Unparseable text is shown as is. */
function formatLocalTimestamp(value) {

    const time =
        parseTimestamp(value);

    if (time === null) {
        return hasValue(value) ? String(value) : "--:--:--";
    }

    const date =
        new Date(time);

    const pad = number =>
        String(number).padStart(2, "0");

    return (
        `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
        `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    );
}


/* =========================================================
   NORMALIZE BACKEND VEHICLE
   The only place that knows backend field names. Everything
   else uses the dashboard names below.
========================================================= */

function normalizeVehicle(raw) {

    const vehicle =
        raw || {};

    return {

        ...vehicle,

        vehicle_id:
            vehicle.vehicle_id,

        name:
            vehicle.name ||
            vehicle.vehicle_name ||
            vehicle.vehicle_id,

        color:
            vehicle.color || null,

        vehicle_status:
            vehicle.vehicle_status ??
            vehicle.status ??
            "unknown",

        communication_status:
            vehicle.communication_status ??
            vehicle.v2v_status ??
            null,

        reported_communication_status:
            vehicle.communication_status ?? null,

        mission_status:
            vehicle.mission_status ?? null,

        next_action:
            vehicle.next_action ?? null,

        speed:
            num(
                vehicle.speed ??
                vehicle.vehicle_speed ??
                vehicle.current_speed ??
                vehicle.telemetry?.speed
            ),

        /* battery_percentage / network_signal are backend
           placeholders (always 100), so they are not used. */
        battery_level:
            hasValue(vehicle.battery_level)
                ? num(vehicle.battery_level)
                : null,

        location:
            vehicle.location ??
            vehicle.current_stop ??
            null,

        destination:
            vehicle.destination ?? null,

        distance_to_destination:
            hasValue(vehicle.distance_to_destination)
                ? num(vehicle.distance_to_destination)
                : null,

        load_percentage:
            clampPercent(
                vehicle.load_percentage
            ),

        signal_strength:
            hasValue(vehicle.signal_strength)
                ? num(vehicle.signal_strength)
                : null,

        last_seen:
            hasValue(vehicle.last_seen)
                ? vehicle.last_seen
                : null,

        emergency_status:
            vehicle.emergency_stop === true ||
            vehicle.emergency_status === true
    };
}


/* =========================================================
   VEHICLE CLASSIFICATION
========================================================= */

function isOnline(vehicle) {

    if (
        !state.backendOnline ||
        !vehicle
    ) {
        return false;
    }

    if (vehicle._thingsboard_error) {
        return false;
    }

    if (norm(vehicle.vehicle_online) === "false") {
        return false;
    }

    const seen =
        parseTimestamp(vehicle.last_seen);

    if (seen === null) {
        return true;
    }

    return (
        (Date.now() - seen) / 1000 <=
        OFFLINE_AFTER_SECONDS
    );
}


function missionKind(vehicle) {

    const mission =
        String(vehicle?.mission_status ?? "").toUpperCase();

    if (
        mission.includes("FAIL") ||
        mission.includes("ERROR")
    ) {
        return "failed";
    }

    if (mission === "COMPLETED") {
        return "completed";
    }

    if (mission === "EMERGENCY") {
        return "stopped";
    }

    if (
        ["", "IDLE", "WAITING", "READY", "NONE", "UNKNOWN"]
            .includes(mission)
    ) {
        return "idle";
    }

    return "active";
}


/* =========================================================
   DOM HELPERS
========================================================= */

function field(
    root,
    name
) {

    return root
        ? root.querySelector(`[data-field="${name}"]`)
        : null;
}


/* Only touch the DOM when the text changes. */
function setText(
    element,
    text
) {

    if (
        element &&
        element.textContent !== text
    ) {
        element.textContent = text;
    }
}


function applyAccent(
    element,
    color
) {

    if (!element) {
        return;
    }

    const value =
        norm(color);

    ACCENT_CLASSES.forEach(
        name => {
            element.classList.toggle(
                `accent-${name}`,
                value === name
            );
        }
    );

    if (
        value &&
        !ACCENT_CLASSES.includes(value) &&
        CSS.supports("color", String(color))
    ) {

        element.style.setProperty(
            "--vehicle-accent",
            String(color)
        );

    } else {

        element.style.removeProperty(
            "--vehicle-accent"
        );
    }
}


/* Keep one child per vehicle id, in fleet order. Other
   children of the container are left alone. */
function syncKeyedChildren(
    container,
    ids,
    registry,
    create
) {

    registry.forEach(
        (element, id) => {

            if (!ids.includes(id)) {

                element.remove();

                registry.delete(id);
            }
        }
    );


    ids.forEach(
        id => {

            if (!registry.has(id)) {

                registry.set(
                    id,
                    create(id)
                );
            }
        }
    );


    const current = [...container.children]
        .filter(element => element.dataset.vehicleId)
        .map(element => element.dataset.vehicleId);


    if (current.join("|") !== ids.join("|")) {

        ids.forEach(
            id => container.appendChild(
                registry.get(id)
            )
        );
    }
}


/* =========================================================
   BACKEND STATUS
========================================================= */

function setBackendStatus(
    online
) {

    state.backendOnline =
        online;


    document
        .querySelectorAll(
            ".system-status, .backend-status"
        )
        .forEach(
            el => {

                el.classList.toggle(
                    "online",
                    online
                );

                el.classList.toggle(
                    "offline",
                    !online
                );


                const dot =
                    el.querySelector(
                        ".status-dot"
                    );


                const text =
                    el.querySelector(
                        ".status-text"
                    );


                if (dot) {

                    dot.style.background =
                        online
                            ? "#22c55e"
                            : "#ef4444";
                }


                if (text) {

                    text.textContent =
                        online
                            ? "BACKEND ONLINE"
                            : "BACKEND OFFLINE";
                }
            }
        );


    const systemText =
        $("systemStatusText");


    if (systemText) {

        systemText.textContent =
            online
                ? "SYSTEM ONLINE"
                : "SYSTEM OFFLINE";
    }
}


/* =========================================================
   HEALTH CHECK
   Polled every HEALTH_INTERVAL so the ThingsBoard status
   stays current.
========================================================= */

async function checkHealth() {

    try {

        const response =
            await fetch(
                API.health,
                {
                    method: "GET",
                    cache: "no-store"
                }
            );


        if (!response.ok) {

            throw new Error(
                `HTTP ${response.status}`
            );
        }


        const data =
            await response.json();


        const online =
            data?.success === true;


        state.healthData =
            data;


        setBackendStatus(
            online
        );


        updateSensorHealth();


        updateCommunication(
            state.communicationData
        );


        return online;

    } catch (error) {

        console.error(
            "Health check:",
            error
        );


        state.healthData =
            null;


        setBackendStatus(
            false
        );


        updateSensorHealth();


        return false;
    }
}


async function getCommunicationData() {

    try {

        const response =
            await fetch(
                API.communication,
                {
                    method: "GET",
                    cache: "no-store"
                }
            );


        if (!response.ok) {

            throw new Error(
                `Communication API HTTP ${response.status}`
            );
        }


        const result =
            await response.json();


        state.communicationData =
            result?.data || null;


        updateCommunication(
            state.communicationData
        );


    } catch (error) {

        console.error(
            "Communication data:",
            error
        );


        state.communicationData =
            null;


        updateCommunication(
            null
        );
    }
}


/* =========================================================
   GET FLEET DATA
========================================================= */

function setFleet(list) {

    const fleet =
        new Map();

    const order =
        [];


    list.forEach(
        raw => {

            const vehicle =
                normalizeVehicle(raw);


            if (
                !hasValue(vehicle.vehicle_id) ||
                fleet.has(vehicle.vehicle_id)
            ) {
                return;
            }


            fleet.set(
                vehicle.vehicle_id,
                vehicle
            );

            order.push(
                vehicle.vehicle_id
            );
        }
    );


    state.fleet =
        fleet;

    state.order =
        order;


    if (!fleet.has(state.selectedVehicle)) {

        state.selectedVehicle =
            order[0] || null;
    }
}


async function getFleetData() {

    try {

        const response =
            await fetch(
                API.fleet,
                {
                    method: "GET",
                    cache: "no-store"
                }
            );


        if (!response.ok) {

            throw new Error(
                `Fleet API HTTP ${response.status}`
            );
        }


        const result =
            await response.json();


        const vehicles =
            result?.data?.vehicles;


        if (!Array.isArray(vehicles)) {

            throw new Error(
                "Invalid fleet response"
            );
        }


        const previousOrder =
            state.order.join("|");


        setFleet(
            vehicles
        );


        /* Devices deleted in ThingsBoard are removed by the
           backend; the Activity Log shows its event. */
        const removed =
            result?.data?.removed || [];

        if (removed.length) {

            setCommandStatus(
                `${joinNames(removed)} removed: device deleted in ThingsBoard.`
            );
        }


        /* Lets the Vehicle Manager follow vehicles added or
           removed outside this page (ThingsBoard, another tab,
           a hand edit of vehicles.json). */
        if (
            state.fleetLoaded &&
            previousOrder !== state.order.join("|")
        ) {
            window.dispatchEvent(
                new Event("fleet:changed")
            );
        }

        state.fleetLoaded =
            true;


        /* The backend is the source of truth for the emergency
           stop (it may have been set or released in another
           tab, or by Reset). */
        if (!state.emergencyRequest) {

            state.emergency =
                fleetVehicles().some(
                    vehicle => vehicle.emergency_status
                );
        }


        setBackendStatus(
            true
        );


        renderFleet();


        await getCommunicationData();


        await getEvents();


        updateButtons();


        return true;


    } catch (error) {

        console.error(
            "Fleet data:",
            error
        );


        setBackendStatus(
            false
        );


        renderFleet();


        return false;
    }
}


/* =========================================================
   REFRESH
   Polls never overlap. force=true (used after commands)
   waits for any running poll, then fetches fresh data.
========================================================= */

async function refreshFleet(
    force = false
) {

    if (state.fetchPromise) {

        if (!force) {
            return state.fetchPromise;
        }

        await state.fetchPromise;
    }


    const request =
        getFleetData();


    state.fetchPromise =
        request;


    try {

        return await request;

    } finally {

        if (state.fetchPromise === request) {

            state.fetchPromise =
                null;
        }
    }
}


function pollInterval() {

    return state.order.length > LARGE_FLEET_SIZE
        ? LARGE_FLEET_REFRESH_INTERVAL
        : REFRESH_INTERVAL;
}


function scheduleRefresh() {

    setTimeout(
        async () => {

            await refreshFleet();

            scheduleRefresh();
        },
        pollInterval()
    );
}


/* =========================================================
   RENDER EVERYTHING FROM FLEET DATA
========================================================= */

function renderFleet() {

    renderVehicleCards();

    renderRouteVehicles();

    updateRadar();

    updateSensorHealth();

    renderOverview();

    renderAlerts();

    updateFilterCounts();

    renderSelector();

    renderDetail();

    renderTable();

    applyVisibility();
}


/* =========================================================
   VEHICLE CARDS
========================================================= */

function createVehicleCard(vehicleId) {

    const card =
        $("vehicleCardTemplate")
            .content
            .firstElementChild
            .cloneNode(true);


    card.dataset.vehicleId =
        vehicleId;


    card.addEventListener(
        "click",
        () => selectVehicle(vehicleId)
    );


    wireDeleteButton(
        field(card, "delete"),
        vehicleId
    );


    return card;
}


/* Delete buttons on cards and table rows. The confirmation,
   the "cycle running" guard and the refresh live in
   deleteManagedVehicle() (vehicle-manager.js). */
function wireDeleteButton(
    button,
    vehicleId
) {

    if (!button) {
        return;
    }


    button.setAttribute(
        "aria-label",
        `Delete ${vehicleId}`
    );


    button.addEventListener(
        "click",
        event => {

            /* Do not also select the card / row. */
            event.stopPropagation();

            if (typeof deleteManagedVehicle === "function") {
                deleteManagedVehicle(vehicleId);
            }
        }
    );


    button.addEventListener(
        "keydown",
        event => event.stopPropagation()
    );
}


function renderVehicleCards() {

    const grid =
        $("vehicleGrid");


    if (!grid) {
        return;
    }


    syncKeyedChildren(
        grid,
        state.order,
        elements.cards,
        createVehicleCard
    );


    state.order.forEach(
        vehicleId => updateVehicleCard(
            elements.cards.get(vehicleId),
            state.fleet.get(vehicleId)
        )
    );
}


function statusClass(
    status,
    emergency
) {

    if (emergency) {
        return "emergency";
    }

    if (status.includes("running")) {
        return "running";
    }

    if (status.includes("loading")) {
        return "loading";
    }

    if (status.includes("completed")) {
        return "completed";
    }

    if (status.includes("ready")) {
        return "ready";
    }

    return "waiting";
}


function updateVehicleCard(
    card,
    vehicle
) {

    if (
        !card ||
        !vehicle
    ) {
        return;
    }


    applyAccent(
        card,
        vehicle.color
    );


    card.classList.toggle(
        "selected",
        vehicle.vehicle_id === state.selectedVehicle
    );


    const number =
        vehicleNumber(vehicle.vehicle_id);


    setText(
        field(card, "name"),
        vehicle.name
    );


    setText(
        field(card, "tag"),
        number
            ? `APRILTAG TAG ${number}`
            : "APRILTAG TAG"
    );


    /* -----------------------------------------------------
       STATUS + BADGE
    ----------------------------------------------------- */

    const status =
        norm(vehicle.vehicle_status);

    const emergency =
        vehicle.emergency_status;


    const statusEl =
        field(card, "status");


    if (statusEl) {

        setText(
            statusEl,
            emergency
                ? "EMERGENCY STOP"
                : status.toUpperCase()
        );


        statusEl.classList.remove(
            "running",
            "loading",
            "waiting",
            "ready",
            "completed",
            "emergency"
        );


        statusEl.classList.add(
            statusClass(status, emergency)
        );
    }


    setText(
        field(card, "badge"),
        emergency
            ? "EMERGENCY"
            : status.toUpperCase()
    );


    /* -----------------------------------------------------
       LOCATION / SPEED / CAPACITY
    ----------------------------------------------------- */

    setText(
        field(card, "location"),
        locationText(vehicle.location)
    );


    setText(
        field(card, "speed"),
        `${num(vehicle.speed)} km/h`
    );


    setText(
        field(card, "capacity"),
        `${num(vehicle.capacity, 1000)} kg`
    );


    /* -----------------------------------------------------
       BRAKE
    ----------------------------------------------------- */

    const brake =
        field(card, "brake");


    if (brake) {

        const value =
            String(
                vehicle.braking_status ??
                vehicle.brake ??
                "unknown"
            );


        const normalized =
            norm(value);


        setText(
            brake,
            value.toUpperCase()
        );


        brake.classList.toggle(
            "applied",
            normalized.includes("applied")
        );


        brake.classList.toggle(
            "released",
            !normalized.includes("applied") &&
            normalized.includes("released")
        );
    }


    /* -----------------------------------------------------
       DIRECTION
    ----------------------------------------------------- */

    setText(
        field(card, "direction"),
        String(
            vehicle.motor_direction ??
            vehicle.direction ??
            "unknown"
        ).toUpperCase()
    );


    /* -----------------------------------------------------
       APRIL TAG
    ----------------------------------------------------- */

    const qr =
        field(card, "qr");


    if (qr) {

        const value =
            norm(
                vehicle.qr_status ??
                "not_detected"
            );


        const detected =
            value === "detected" ||
            value === "qr_detected";


        setText(
            qr,
            detected
                ? "DETECTED"
                : "NOT DETECTED"
        );


        qr.classList.toggle(
            "detected",
            detected
        );


        qr.classList.toggle(
            "not-detected",
            !detected
        );
    }


    /* -----------------------------------------------------
       RADAR
    ----------------------------------------------------- */

    const radar =
        field(card, "radar");


    if (radar) {

        const obstacle =
            norm(
                vehicle.radar_status ??
                "clear"
            ).includes("obstacle");


        setText(
            radar,
            obstacle
                ? "OBSTACLE"
                : "CLEAR"
        );


        radar.classList.toggle(
            "obstacle",
            obstacle
        );


        radar.classList.toggle(
            "clear",
            !obstacle
        );
    }


    /* -----------------------------------------------------
       LOAD
    ----------------------------------------------------- */

    const load =
        clampPercent(
            vehicle.load_percentage
        );


    setText(
        field(card, "load"),
        `${load}%`
    );


    setText(
        field(card, "load-footer"),
        `${load}%`
    );


    const progress =
        field(card, "progress");


    if (progress) {

        progress.setAttribute(
            "aria-valuenow",
            String(load)
        );
    }


    const fill =
        field(card, "progress-fill");


    if (fill) {

        fill.style.width =
            `${load}%`;
    }


    /* -----------------------------------------------------
       EMERGENCY CARD
    ----------------------------------------------------- */

    card.classList.toggle(
        "emergency-active",
        emergency
    );
}


/* =========================================================
   ROUTE MARKERS

   STOP 1    = 5%  (backend location)
   START     = 8%  (animation start)
   STOP 2    = 50%
   WAREHOUSE = 92%
========================================================= */

function createRoadVehicle(vehicleId) {

    const marker =
        $("roadVehicleTemplate")
            .content
            .firstElementChild
            .cloneNode(true);


    marker.dataset.vehicleId =
        vehicleId;


    setText(
        field(marker, "short-name"),
        shortName(vehicleId)
    );


    return marker;
}


function laneOffset(
    index,
    count
) {

    if (count <= 1) {
        return 0;
    }

    const spacing =
        Math.min(
            LANE_SPACING,
            MAX_LANE_SPREAD / (count - 1)
        );

    return (index - (count - 1) / 2) * spacing;
}


function renderRouteVehicles() {

    const map =
        $("routeMap");


    if (!map) {
        return;
    }


    syncKeyedChildren(
        map,
        state.order,
        elements.route,
        createRoadVehicle
    );


    state.order.forEach(
        (vehicleId, index) => {

            const marker =
                elements.route.get(vehicleId);


            applyAccent(
                marker,
                state.fleet.get(vehicleId)?.color
            );


            marker.style.marginTop =
                `${laneOffset(index, state.order.length)}px`;


            marker.style.zIndex =
                String(20 + index);
        }
    );


    updateRoute();
}


function backendPosition(
    location
) {

    const value =
        norm(location);


    if (
        value.includes(
            "stop_1"
        )
    ) {

        return ROUTE.stop1;
    }


    if (
        value.includes(
            "stop_2"
        )
    ) {

        return ROUTE.loading;
    }


    if (
        value.includes(
            "stop_3"
        ) ||
        value.includes(
            "warehouse"
        )
    ) {

        return ROUTE.warehouse;
    }


    return ROUTE.start;
}


function setRoutePosition(
    vehicleId,
    position
) {

    const element =
        elements.route.get(vehicleId);


    if (!element) {
        return;
    }


    element.style.left =
        `${position}%`;
}


/* Do NOT overwrite the frontend animation while a visual
   movement or handover is running. */
function updateRouteVehicle(
    vehicleId,
    vehicle
) {

    if (!vehicle) {
        return;
    }


    if (
        state.visualMove ||
        state.handoverRunning
    ) {

        return;
    }


    setRoutePosition(
        vehicleId,
        backendPosition(
            vehicle.location
        )
    );
}


function updateRouteStops() {

    const locations =
        fleetVehicles()
            .map(vehicle => vehicle.location)
            .filter(hasValue)
            .map(norm);


    const stops = {

        stop_1:
            document.querySelector(
                '.route-stop[data-position="start"] .stop-marker'
            ),

        stop_2:
            document.querySelector(
                '.route-stop[data-position="loading"] .stop-marker'
            ),

        stop_3:
            document.querySelector(
                '.route-stop[data-position="warehouse"] .stop-marker'
            )
    };


    const active = {

        stop_1:
            locations.some(l => l.includes("stop_1")),

        stop_2:
            locations.some(l => l.includes("stop_2")),

        stop_3:
            locations.some(
                l => l.includes("stop_3") || l.includes("warehouse")
            )
    };


    Object.entries(stops).forEach(
        ([stop, marker]) => {

            if (marker) {

                marker.classList.toggle(
                    "active",
                    active[stop]
                );
            }
        }
    );
}


function updateRoute() {

    state.order.forEach(
        vehicleId => updateRouteVehicle(
            vehicleId,
            state.fleet.get(vehicleId)
        )
    );


    updateRouteStops();
}


/* =========================================================
   RADAR (ANY VEHICLE)
========================================================= */

function fleetHasObstacle() {

    return fleetVehicles().some(
        vehicle => norm(vehicle.radar_status).includes("obstacle")
    );
}


function updateRadar() {

    const obstacle =
        fleetHasObstacle();


    const light =
        $("radarLight");


    const status =
        $("radarStatus");


    const message =
        $("radarMessage");


    const radarObstacle =
        $("radarObstacle");


    if (light) {

        light.classList.remove(
            "green",
            "red"
        );


        light.classList.add(
            obstacle
                ? "red"
                : "green"
        );
    }


    if (status) {

        status.textContent =
            obstacle
                ? "OBSTACLE"
                : "CLEAR";
    }


    if (message) {

        message.textContent =
            obstacle
                ? "Obstacle detected"
                : "No obstacle detected";
    }


    if (radarObstacle) {

        radarObstacle.style.display =
            obstacle
                ? "block"
                : "none";
    }
}


/* =========================================================
   SENSOR HEALTH
========================================================= */

function updateSensorHealth() {

    setHealth(
        "radarSensorHealth",
        !fleetHasObstacle()
    );


    setHealth(
        "communicationHealth",
        state.backendOnline
    );


    setHealth(
        "cloudHealth",
        state.healthData?.thingsboard?.connected === true
    );
}


function updateCommunication(
    communication
) {

    const connected =
        communication?.connected === true;


    setHealth(
        "communicationHealth",
        connected
    );


    const status =
        $("communicationStatusText");

    const card =
        $("communicationStatus");


    if (card) {

        card.classList.toggle(
            "offline",
            !connected
        );
    }


    if (status) {

        const statuses =
            communication?.vehicle_statuses || {};

        const linked =
            state.order.filter(
                vehicleId => norm(statuses[vehicleId]) === "connected"
            ).length;


        status.textContent =
            connected
                ? `${linked}/${state.order.length} VEHICLES CONNECTED`
                : "V2V LINK OFFLINE";
    }
}


function setHealth(
    id,
    good
) {

    const element =
        $(id);


    if (!element) {
        return;
    }


    element.textContent =
        good
            ? "ONLINE"
            : "OFFLINE";


    element.classList.toggle(
        "health-good",
        good
    );


    element.classList.toggle(
        "health-bad",
        !good
    );
}


/* =========================================================
   FLEET OVERVIEW
========================================================= */

function renderOverview() {

    const vehicles =
        fleetVehicles();


    const online =
        vehicles.filter(isOnline).length;


    const counts = {

        total:
            vehicles.length,

        online,

        offline:
            vehicles.length - online,

        active:
            vehicles.filter(v => missionKind(v) === "active").length,

        completed:
            vehicles.filter(v => missionKind(v) === "completed").length,

        emergency:
            vehicles.filter(v => v.emergency_status).length
    };


    document
        .querySelectorAll("[data-stat]")
        .forEach(
            element => setText(
                element,
                String(counts[element.dataset.stat] ?? 0)
            )
        );
}


/* =========================================================
   ALERTS
========================================================= */

function collectAlerts() {

    const alerts =
        [];


    fleetVehicles().forEach(
        vehicle => {

            ALERT_RULES.forEach(
                rule => {

                    if (rule.test(vehicle)) {

                        alerts.push({
                            key: `${vehicle.vehicle_id}:${rule.type}`,
                            vehicleId: vehicle.vehicle_id,
                            label: rule.label,
                            severity: rule.severity
                        });
                    }
                }
            );
        }
    );


    /* Critical first; otherwise keep fleet order. */
    return alerts.sort(
        (a, b) =>
            (a.severity === "critical" ? 0 : 1) -
            (b.severity === "critical" ? 0 : 1)
    );
}


function renderAlerts() {

    const list =
        $("alertList");

    const count =
        $("alertCount");


    if (!list) {
        return;
    }


    const alerts =
        collectAlerts();


    if (count) {

        setText(
            count,
            String(alerts.length)
        );

        count.classList.toggle(
            "has-alerts",
            alerts.length > 0
        );
    }


    /* Re-render only when the set of alerts changes, so the
       aria-live region does not repeat every poll. */
    const signature =
        alerts.map(alert => alert.key).join("|");


    if (signature === state.alertSignature) {
        return;
    }


    state.alertSignature =
        signature;


    if (!alerts.length) {

        const empty =
            document.createElement("p");

        empty.className =
            "alert-empty";

        empty.textContent =
            "No active alerts.";

        list.replaceChildren(
            empty
        );

        return;
    }


    list.replaceChildren(
        ...alerts.map(
            alert => {

                const row =
                    document.createElement("div");

                row.className =
                    `alert-row ${alert.severity}`;


                const label =
                    document.createElement("strong");

                label.textContent =
                    alert.label;


                const name =
                    document.createElement("span");

                name.textContent =
                    vehicleName(alert.vehicleId);


                row.append(
                    label,
                    name
                );


                row.addEventListener(
                    "click",
                    () => selectVehicle(alert.vehicleId)
                );


                return row;
            }
        )
    );
}


/* =========================================================
   SEARCH + FILTERS
========================================================= */

function renderFilters() {

    const container =
        $("fleetFilters");


    if (!container) {
        return;
    }


    container.replaceChildren(
        ...FILTERS.map(
            filter => {

                const button =
                    document.createElement("button");

                button.type =
                    "button";

                button.className =
                    "filter-button";

                button.dataset.filter =
                    filter.id;

                button.textContent =
                    filter.label;

                button.setAttribute(
                    "aria-pressed",
                    String(filter.id === state.filter)
                );

                button.addEventListener(
                    "click",
                    () => setFilter(filter.id)
                );

                return button;
            }
        )
    );
}


function updateFilterCounts() {

    const vehicles =
        fleetVehicles();


    document
        .querySelectorAll("#fleetFilters .filter-button")
        .forEach(
            button => {

                const filter =
                    FILTERS.find(f => f.id === button.dataset.filter);


                if (!filter) {
                    return;
                }


                const count =
                    vehicles.filter(
                        vehicle => filter.test(vehicle)
                    ).length;


                setText(
                    button,
                    `${filter.label} (${count})`
                );
            }
        );
}


function setFilter(filterId) {

    state.filter =
        filterId;


    document
        .querySelectorAll("#fleetFilters .filter-button")
        .forEach(
            button => button.setAttribute(
                "aria-pressed",
                String(button.dataset.filter === filterId)
            )
        );


    applyVisibility();
}


/* Case-insensitive match on id or name. "15", "vehicle 15"
   and "Vehicle_15" all find Vehicle_15. */
function matchesSearch(
    vehicle,
    query
) {

    const text =
        norm(query);


    if (!text) {
        return true;
    }


    if (
        norm(vehicle.vehicle_id).includes(text) ||
        norm(vehicle.name).includes(text)
    ) {
        return true;
    }


    const number =
        vehicleNumber(vehicle.vehicle_id);

    const match =
        text.match(/^(?:vehicle_?)?0*(\d+)$/);


    return Boolean(
        number &&
        match &&
        Number(match[1]) === Number(number)
    );
}


function applyVisibility() {

    const filter =
        FILTERS.find(f => f.id === state.filter) ||
        FILTERS[0];


    let visible =
        0;


    state.order.forEach(
        vehicleId => {

            const vehicle =
                state.fleet.get(vehicleId);


            const show =
                filter.test(vehicle) &&
                matchesSearch(vehicle, state.search);


            if (show) {
                visible += 1;
            }


            const card =
                elements.cards.get(vehicleId);

            if (card) {
                card.hidden = !show;
            }


            const row =
                elements.rows.get(vehicleId);

            if (row) {
                row.hidden = !show;
            }
        }
    );


    const empty =
        $("vehicleEmpty");


    if (empty) {

        empty.hidden =
            visible > 0;


        let message =
            "No vehicles match the current search or filter.";


        if (!state.order.length) {

            message =
                state.backendOnline
                    ? "No vehicles registered. Use ADD NEW VEHICLE to add one."
                    : "Waiting for fleet data...";
        }


        setText(
            empty,
            message
        );
    }
}


/* =========================================================
   VEHICLE SELECTOR + DETAIL PANEL
========================================================= */

function selectVehicle(vehicleId) {

    if (!state.fleet.has(vehicleId)) {
        return;
    }


    state.selectedVehicle =
        vehicleId;


    renderSelector();

    renderDetail();


    elements.cards.forEach(
        (card, id) => card.classList.toggle(
            "selected",
            id === vehicleId
        )
    );


    elements.rows.forEach(
        (row, id) => row.classList.toggle(
            "selected",
            id === vehicleId
        )
    );
}


function renderSelector() {

    const select =
        $("vehicleSelect");


    if (!select) {
        return;
    }


    const signature =
        state.order
            .map(id => `${id}:${vehicleName(id)}`)
            .join("|");


    if (select.dataset.signature !== signature) {

        select.dataset.signature =
            signature;


        select.replaceChildren(
            ...state.order.map(
                vehicleId => {

                    const option =
                        document.createElement("option");

                    option.value =
                        vehicleId;

                    option.textContent =
                        vehicleLabel(vehicleName(vehicleId), vehicleId);

                    return option;
                }
            )
        );
    }


    if (
        state.selectedVehicle &&
        select.value !== state.selectedVehicle
    ) {

        select.value =
            state.selectedVehicle;
    }
}


function renderDetail() {

    const panel =
        $("vehicleDetail");


    if (!panel) {
        return;
    }


    const vehicle =
        state.fleet.get(state.selectedVehicle);


    setText(
        $("detailTitle"),
        vehicle
            ? vehicleLabel(vehicle.name, vehicle.vehicle_id)
            : "Select a vehicle"
    );


    const values = vehicle
        ? {

            vehicle_status:
                vehicle.emergency_status
                    ? "EMERGENCY STOP"
                    : displayText(vehicle.vehicle_status),

            mission_status:
                displayText(vehicle.mission_status),

            next_action:
                displayText(vehicle.next_action),

            battery_level:
                vehicle.battery_level === null
                    ? "n/a"
                    : `${vehicle.battery_level}%`,

            speed:
                `${num(vehicle.speed)} km/h`,

            location:
                hasValue(vehicle.location)
                    ? locationText(vehicle.location)
                    : "—",

            destination:
                hasValue(vehicle.destination)
                    ? locationText(vehicle.destination)
                    : "—",

            distance_to_destination:
                vehicle.distance_to_destination === null
                    ? "—"
                    : String(vehicle.distance_to_destination),

            communication_status:
                displayText(vehicle.communication_status),

            last_seen:
                formatLastSeen(vehicle.last_seen),

            signal_strength:
                vehicle.signal_strength === null
                    ? "n/a"
                    : String(vehicle.signal_strength)
        }
        : {};


    panel
        .querySelectorAll("[data-detail]")
        .forEach(
            element => setText(
                element,
                values[element.dataset.detail] ?? "—"
            )
        );
}


/* =========================================================
   FLEET TABLE
========================================================= */

const TABLE_COLUMNS = [
    "vehicle",
    "status",
    "mission",
    "location",
    "load",
    "speed",
    "comms",
    "online",
    "actions"
];


function createTableRow(vehicleId) {

    const row =
        document.createElement("tr");


    row.dataset.vehicleId =
        vehicleId;

    row.tabIndex =
        0;


    TABLE_COLUMNS.forEach(
        column => {

            const cell =
                document.createElement("td");

            cell.dataset.col =
                column;

            row.appendChild(
                cell
            );
        }
    );


    const dot =
        document.createElement("span");

    dot.className =
        "vehicle-dot";


    const name =
        document.createElement("span");

    name.dataset.role =
        "name";


    row.firstElementChild.append(
        dot,
        name
    );


    const remove =
        document.createElement("button");

    remove.type =
        "button";

    remove.className =
        "vehicle-delete-button";

    remove.title =
        "Remove this vehicle from the fleet";

    remove.textContent =
        "✕";

    wireDeleteButton(
        remove,
        vehicleId
    );

    row.querySelector('[data-col="actions"]').append(
        remove
    );


    row.addEventListener(
        "click",
        () => selectVehicle(vehicleId)
    );


    row.addEventListener(
        "keydown",
        event => {

            if (
                event.key === "Enter" ||
                event.key === " "
            ) {

                event.preventDefault();

                selectVehicle(vehicleId);
            }
        }
    );


    return row;
}


function renderTable() {

    const body =
        $("fleetTableBody");


    if (!body) {
        return;
    }


    syncKeyedChildren(
        body,
        state.order,
        elements.rows,
        createTableRow
    );


    state.order.forEach(
        vehicleId => {

            const vehicle =
                state.fleet.get(vehicleId);

            const row =
                elements.rows.get(vehicleId);

            const online =
                isOnline(vehicle);


            row.classList.toggle(
                "selected",
                vehicleId === state.selectedVehicle
            );


            applyAccent(
                row.querySelector(".vehicle-dot"),
                vehicle.color
            );


            setText(
                row.querySelector('[data-role="name"]'),
                vehicleLabel(vehicle.name, vehicleId)
            );


            const cells = {

                status:
                    vehicle.emergency_status
                        ? "EMERGENCY STOP"
                        : displayText(vehicle.vehicle_status),

                mission:
                    displayText(vehicle.mission_status),

                location:
                    locationText(vehicle.location),

                load:
                    `${vehicle.load_percentage}%`,

                speed:
                    `${num(vehicle.speed)} km/h`,

                comms:
                    displayText(vehicle.communication_status),

                online:
                    online
                        ? "ONLINE"
                        : "OFFLINE"
            };


            Object.entries(cells).forEach(
                ([column, text]) => setText(
                    row.querySelector(`[data-col="${column}"]`),
                    text
                )
            );


            const onlineCell =
                row.querySelector('[data-col="online"]');

            onlineCell.className =
                online
                    ? "health-good"
                    : "health-bad";
        }
    );
}


/* =========================================================
   COMMAND STATUS
========================================================= */

function setCommandStatus(
    message
) {

    const element =
        $("commandStatus");


    if (element) {

        element.textContent =
            message;
    }
}


/* =========================================================
   ACTIVITY LOG
   Client messages are kept locally and merged with backend
   events, so the poll does not wipe them.
========================================================= */

function addLog(
    message
) {

    state.clientLog.unshift({
        timestamp: utcTimestamp(),
        message: String(message),
        client: true
    });


    if (state.clientLog.length > CLIENT_LOG_LIMIT) {

        state.clientLog.length =
            CLIENT_LOG_LIMIT;
    }


    renderLog();


    return message;
}


async function getEvents() {

    try {

        const response =
            await fetch(
                API.events,
                {
                    method: "GET",
                    cache: "no-store"
                }
            );


        if (!response.ok) {

            throw new Error(
                `Events API HTTP ${response.status}`
            );
        }


        const result =
            await response.json();


        state.backendEvents =
            Array.isArray(result?.data)
                ? result.data
                : [];


        renderLog();


    } catch (error) {

        console.error(
            "Events data:",
            error
        );
    }
}


/* J2735-styled BSM or custom V2V message carried by an event. */
function logMessageBody(event) {

    return (
        event?.details?.j2735 ||
        event?.details?.v2v_message ||
        null
    );
}


function logKey(event) {

    return event.client
        ? `c:${event.timestamp}:${event.message}`
        : `e:${event.id}`;
}


function toggleLogEntry(key) {

    if (state.expandedLogs.has(key)) {
        state.expandedLogs.delete(key);
    } else {
        state.expandedLogs.add(key);
    }

    state.logSignature =
        null;

    renderLog();


    /* Re-rendering replaced the button; keep keyboard focus. */
    const button =
        [...document.querySelectorAll("#activityLog .log-summary")]
            .find(element => element.dataset.key === key);

    button?.focus();
}


function createLogEntry(event) {

    const body =
        logMessageBody(event);

    const key =
        logKey(event);

    const expanded =
        Boolean(body) &&
        state.expandedLogs.has(key);


    const row =
        document.createElement("div");

    row.className = [
        "log-entry",
        event.client ? "client" : "",
        body ? "j2735" : "",
        expanded ? "expanded" : ""
    ].filter(Boolean).join(" ");


    const time =
        document.createElement("span");

    time.className =
        "log-time";

    time.textContent =
        formatLocalTimestamp(event.timestamp);

    if (hasValue(event.timestamp)) {
        time.title = `${event.timestamp} (UTC)`;
    }


    if (!body) {

        const text =
            document.createElement("span");

        text.className =
            "log-message";

        text.textContent =
            event.message ||
            "Backend event";

        row.append(
            time,
            text
        );

        return row;
    }


    /* J2735 row: the summary is a button that shows the JSON. */
    const isBsm =
        body.messageName === "BasicSafetyMessage";

    const summary =
        document.createElement("button");

    summary.type =
        "button";

    summary.className =
        "log-message log-summary";

    summary.dataset.key =
        key;

    summary.setAttribute(
        "aria-expanded",
        String(expanded)
    );

    summary.title =
        expanded
            ? "Hide the J2735-styled message"
            : "Show the J2735-styled message";


    const chip =
        document.createElement("span");

    chip.className =
        isBsm
            ? "log-chip bsm"
            : "log-chip v2v";

    chip.textContent =
        isBsm ? "BSM" : "V2V";


    const text =
        document.createElement("span");

    /* The chip replaces the "[BSM]" / "[V2V]" prefix. */
    text.textContent =
        String(event.message || "").replace(/^\[(BSM|V2V)\]\s*/, "");


    const caret =
        document.createElement("span");

    caret.className =
        "log-caret";

    caret.setAttribute(
        "aria-hidden",
        "true"
    );

    caret.textContent =
        expanded ? "▾" : "▸";


    summary.append(
        chip,
        text,
        caret
    );

    summary.addEventListener(
        "click",
        () => toggleLogEntry(key)
    );


    row.append(
        time,
        summary
    );


    if (expanded) {

        const panel =
            document.createElement("div");

        panel.className =
            "log-json-panel";

        const note =
            document.createElement("p");

        note.className =
            "log-json-note";

        note.textContent =
            body.x_format || "";

        const json =
            document.createElement("pre");

        json.className =
            "log-json";

        json.textContent =
            JSON.stringify(body, null, 2);

        panel.append(
            note,
            json
        );

        row.appendChild(
            panel
        );
    }


    return row;
}


function renderLog() {

    const log =
        $("activityLog");


    if (!log) {
        return;
    }


    const entries = [
        ...state.clientLog,
        ...state.backendEvents
    ]
        .sort(
            (a, b) =>
                ((parseTimestamp(b.timestamp) ?? 0) -
                    (parseTimestamp(a.timestamp) ?? 0)) ||
                (num(b.id) - num(a.id))
        )
        .slice(0, LOG_DISPLAY_LIMIT);


    /* Keep the "Waiting for backend" placeholder until there
       is something to show. */
    if (!entries.length) {
        return;
    }


    const keys =
        entries.map(logKey);


    /* Forget expanded rows that scrolled out of the log. */
    state.expandedLogs.forEach(
        key => {
            if (!keys.includes(key)) {
                state.expandedLogs.delete(key);
            }
        }
    );


    const signature =
        keys.join("|") +
        "#" +
        [...state.expandedLogs].join("|");

    if (signature === state.logSignature) {
        return;
    }

    state.logSignature =
        signature;


    log.replaceChildren(
        ...entries.map(createLogEntry)
    );
}


/* =========================================================
   NORMAL BACKEND COMMAND
========================================================= */

async function postCommand(
    url,
    vehicle,
    extra = {}
) {

    if (
        state.commandBusy
    ) {

        return false;
    }


    state.commandBusy =
        true;


    updateButtons();


    try {

        console.log(
            "Sending command:",
            url,
            {
                vehicle,
                ...extra
            }
        );


        const response =
            await fetch(
                url,
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({
                            vehicle,
                            ...extra
                        })
                }
            );


        let result = {};


        try {

            result =
                await response.json();

        } catch {

            result = {};
        }


        console.log(
            "Command response:",
            result
        );


        if (
            !response.ok ||
            result?.success === false
        ) {

            throw new Error(
                result?.message ||
                result?.error ||
                `HTTP ${response.status}`
            );
        }


        await refreshFleet(true);


        return result;


    } catch (error) {

        console.error(
            "Command error:",
            error
        );


        setCommandStatus(
            `Command failed: ${error.message}`
        );


        addLog(
            `Command failed: ${error.message}`
        );


        return false;


    } finally {

        state.commandBusy =
            false;


        updateButtons();
    }
}


/* =========================================================
   DIRECT COMMAND

   Used ONLY for automatic handover.

   This does NOT use commandBusy because the two vehicles in
   a handover are commanded independently.
========================================================= */

async function directCommand(
    url,
    vehicle,
    extra = {}
) {

    try {

        console.log(
            "Automatic command:",
            url,
            vehicle,
            extra
        );


        const response =
            await fetch(
                url,
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify({
                            vehicle,
                            ...extra
                        })
                }
            );


        let result = {};


        try {

            result =
                await response.json();

        } catch {

            result = {};
        }


        console.log(
            "Automatic command response:",
            result
        );


        if (
            !response.ok ||
            result?.success === false
        ) {

            throw new Error(
                result?.message ||
                result?.error ||
                `HTTP ${response.status}`
            );
        }


        return true;


    } catch (error) {

        console.error(
            "Automatic command failed:",
            error
        );


        setCommandStatus(
            `Automatic command failed: ${error.message}`
        );


        addLog(
            `Automatic command failed: ${error.message}`
        );


        return false;
    }
}


/* =========================================================
   ROUTE ANIMATION (ANY VEHICLES, IN PARALLEL)

   moves: [{ vehicleId, from, to }]
========================================================= */

function animateRoute(
    moves,
    {
        isActive,
        onEmergency,
        onComplete
    }
) {

    const started =
        performance.now();


    function frame(now) {

        if (!isActive()) {
            return;
        }


        if (state.emergency) {

            onEmergency();

            return;
        }


        const progress =
            Math.min(
                1,
                (now - started) / MOVE_DURATION
            );


        moves.forEach(
            move => setRoutePosition(
                move.vehicleId,
                move.from + (move.to - move.from) * progress
            )
        );


        if (progress < 1) {

            requestAnimationFrame(
                frame
            );

            return;
        }


        moves.forEach(
            move => setRoutePosition(
                move.vehicleId,
                move.to
            )
        );


        onComplete();
    }


    requestAnimationFrame(
        frame
    );
}


function routeElementsExist(vehicleIds) {

    const missing =
        vehicleIds.filter(id => !elements.route.has(id));


    if (missing.length) {

        console.error(
            "Route vehicle elements not found:",
            missing
        );

        return false;
    }


    return true;
}


/* =========================================================
   FIRST VEHICLE: STOP 1 → STOP 2
========================================================= */

function animateVehicleToLoadingPoint(vehicleId) {

    if (
        state.visualMove
    ) {

        return;
    }


    if (!routeElementsExist([vehicleId])) {
        return;
    }


    const name =
        vehicleName(vehicleId);


    state.visualMove = {

        type:
            "to_loading",

        vehicleId
    };


    animateRoute(
        [
            {
                vehicleId,
                from: ROUTE.start,
                to: ROUTE.loading
            }
        ],
        {
            isActive: () => Boolean(state.visualMove),

            onEmergency: () => {

                state.visualMove =
                    null;

                setCommandStatus(
                    "Vehicle movement stopped by emergency stop."
                );

                addLog(
                    `${name} movement stopped by emergency.`
                );

                updateButtons();
            },

            onComplete: () => {

                setCommandStatus(
                    `${name} reached Loading Point.`
                );


                addLog(
                    `${name} reached Loading Point.`
                );


                postCommand(
                    API.loadingPoint,
                    vehicleId
                ).then(() => {

                    if (!state.emergency) {

                        state.visualMove = null;

                        updateButtons();
                    }
                });
            }
        }
    );
}


/* =========================================================
   CYCLE CANCELLATION
   Reset and Emergency Stop call cancelCycle(). A cycle step
   (loading loop, handover, warehouse move) checks
   cycleCancelled(generation) after every await, so it never
   sends another command once the cycle was cancelled, even if
   the emergency flag has been cleared again by a Reset.
========================================================= */

function cancelCycle() {

    state.cycleGeneration += 1;

    state.visualMove =
        null;

    state.loadingCycle =
        false;

    state.handoverRunning =
        false;
}


function cycleCancelled(generation) {

    return generation !== state.cycleGeneration;
}


/* =========================================================
   START / RESUME
   Starts the first vehicle in fleet order from Stop 1.
========================================================= */

async function startResume() {

    if (
        state.commandBusy ||
        state.handoverRunning
    ) {

        return;
    }


    if (
        state.emergency
    ) {

        setCommandStatus(
            "START blocked: emergency stop is active."
        );

        return;
    }


    const vehicleId =
        firstVehicleId();


    if (!vehicleId) {

        setCommandStatus(
            "START blocked: no vehicles registered."
        );

        return;
    }


    const vehicle =
        getVehicle(vehicleId);

    const name =
        vehicleName(vehicleId);


    if (
        vehicle.emergency_status
    ) {

        setCommandStatus(
            "START blocked: emergency stop is active."
        );

        return;
    }


    if (
        norm(
            vehicle.radar_status
        ).includes(
            "obstacle"
        )
    ) {

        setCommandStatus(
            "START blocked: radar detected an obstacle."
        );


        addLog(
            "START blocked by radar safety system."
        );


        return;
    }


    if (
        norm(
            vehicle.location
        ) !== "stop_1"
    ) {

        setCommandStatus(
            `START blocked: ${name} must be at Stop 1.`
        );


        return;
    }


    setCommandStatus(
        `Starting ${name}...`
    );


    addLog(
        "START / RESUME pressed."
    );


    const generation =
        state.cycleGeneration;


    const success =
        await postCommand(
            API.start,
            vehicleId
        );


    if (
        !success ||
        cycleCancelled(generation)
    ) {

        return;
    }


    setCommandStatus(
        `${name} started. Moving to Loading Point...`
    );


    addLog(
        `${name} started.`
    );


    animateVehicleToLoadingPoint(
        vehicleId
    );
}


/* =========================================================
   START LOADING
   Loads the first vehicle (in fleet order) that is at
   Stop 2 and not yet full. When it is full:
   - if another vehicle follows it: automatic handover
   - if it is the last vehicle: final warehouse movement
========================================================= */

async function startLoading() {

    if (
        state.commandBusy ||
        state.loadingCycle ||
        state.handoverRunning
    ) {
        return;
    }


    const vehicleId =
        state.order.find(
            id => {

                const candidate =
                    getVehicle(id);

                return (
                    norm(candidate.location).includes("stop_2") &&
                    num(candidate.load_percentage) < 100
                );
            }
        ) || null;


    if (!vehicleId) {

        setCommandStatus(
            "Loading blocked: no vehicle is ready at Loading Point."
        );

        return;
    }


    const data =
        getVehicle(vehicleId);


    const location =
        norm(
            data.location
        );

    const load =
        num(
            data.load_percentage
        );

    const brake =
        norm(
            data.braking_status ??
            data.brake
        );

    const radar =
        norm(
            data.radar_status
        );


    /* -----------------------------------------------------
       LOCATION CHECK
    ----------------------------------------------------- */

    if (
        !location.includes(
            "stop_2"
        )
    ) {

        setCommandStatus(
            "Loading blocked: vehicle is not at Loading Point."
        );

        return;
    }


    /* -----------------------------------------------------
       LOAD CHECK
    ----------------------------------------------------- */

    if (
        load >= 100
    ) {

        setCommandStatus(
            "Loading blocked: vehicle is already fully loaded."
        );

        return;
    }


    /* -----------------------------------------------------
       BRAKE CHECK
    ----------------------------------------------------- */

    if (
        brake !== "applied"
    ) {

        setCommandStatus(
            "Loading blocked: brake must be applied."
        );

        return;
    }


    /* -----------------------------------------------------
       RADAR CHECK
    ----------------------------------------------------- */

    if (
        radar.includes(
            "obstacle"
        )
    ) {

        setCommandStatus(
            "Loading blocked: radar detected an obstacle."
        );

        return;
    }


    /* -----------------------------------------------------
       EMERGENCY CHECK
    ----------------------------------------------------- */

    if (
        data.emergency_status ||
        state.emergency
    ) {

        setCommandStatus(
            "Loading blocked: emergency stop is active."
        );

        return;
    }


    const name =
        vehicleName(vehicleId);


    /* -----------------------------------------------------
       BACKEND LOADING START
    ----------------------------------------------------- */

    setCommandStatus(
        `${name} loading started...`
    );


    addLog(
        `${name} START LOADING pressed.`
    );


    const generation =
        state.cycleGeneration;


    const started =
        await postCommand(
            API.loadingStart,
            vehicleId
        );


    if (
        !started ||
        cycleCancelled(generation)
    ) {

        return;
    }


    /* -----------------------------------------------------
       FRONTEND LOADING CYCLE
    ----------------------------------------------------- */

    state.loadingCycle =
        true;


    updateButtons();


    setCommandStatus(
        `${name} loading: 0%`
    );


    addLog(
        `${name} loading started.`
    );


    /* Resume from the current load: the backend rejects a value
       lower than the current one (LOAD_DECREASE_REJECTED). */
    const loadingSteps = [
        20,
        40,
        60,
        80,
        100
    ].filter(
        step => step > load
    );


    for (
        const percentage
        of loadingSteps
    ) {


        await new Promise(
            resolve => {

                setTimeout(
                    resolve,
                    650
                );

            }
        );


        /* Reset or Emergency Stop while waiting: the cancel
           already cleared the flags and set the status. */
        if (
            cycleCancelled(generation)
        ) {

            return;
        }


        /* -------------------------------------------------
           EMERGENCY
        ------------------------------------------------- */

        if (
            state.emergency
        ) {

            state.loadingCycle =
                false;

            updateButtons();

            setCommandStatus(
                "Loading stopped: emergency stop is active."
            );

            addLog(
                `${name} loading interrupted by emergency stop.`
            );

            return;
        }


        if (
            getVehicle(vehicleId).emergency_status
        ) {

            state.loadingCycle =
                false;

            updateButtons();

            setCommandStatus(
                "Loading stopped: emergency stop is active."
            );

            addLog(
                `${name} loading interrupted by backend emergency stop.`
            );

            return;
        }


        /* -------------------------------------------------
           SEND LOAD UPDATE
        ------------------------------------------------- */

        const updated =
            await postCommand(
                API.loadingUpdate,
                vehicleId,
                {
                    load_percentage:
                        percentage
                }
            );


        if (
            cycleCancelled(generation)
        ) {

            return;
        }


        if (!updated) {

            state.loadingCycle =
                false;

            updateButtons();

            setCommandStatus(
                `${name} loading update failed.`
            );

            return;
        }


        setCommandStatus(
            `${name} loading: ${percentage}%`
        );


        addLog(
            `${name} load reached ${percentage}%.`
        );
    }


    /* -----------------------------------------------------
       LOADING COMPLETE
    ----------------------------------------------------- */

    state.loadingCycle =
        false;


    await refreshFleet(true);


    if (
        cycleCancelled(generation)
    ) {

        return;
    }


    updateButtons();


    setCommandStatus(
        `${name} fully loaded.`
    );


    addLog(
        `${name} loading completed.`
    );


    /* -----------------------------------------------------
       AUTOMATIC NEXT ACTION
    ----------------------------------------------------- */

    const nextId =
        nextVehicleId(vehicleId);


    if (nextId) {

        /*
           This vehicle: Stop 2 → Warehouse
           Next vehicle: Stop 1 → Stop 2
        */

        setCommandStatus(
            `${name} fully loaded. Starting automatic handover...`
        );


        await startFleetHandover(
            vehicleId,
            nextId
        );


    } else {

        /*
           Last vehicle: Stop 2 → Warehouse
        */

        setCommandStatus(
            `${name} fully loaded. Starting final warehouse movement...`
        );


        await startFinalWarehouseMovement(
            vehicleId
        );
    }
}


/* =========================================================
   AUTOMATIC FLEET HANDOVER

   FROM vehicle: STOP 2 → WAREHOUSE
   TO vehicle:   STOP 1 → STOP 2
========================================================= */

async function startFleetHandover(
    fromId,
    toId
) {

    if (
        state.handoverRunning
    ) {

        return;
    }


    if (
        state.emergency
    ) {

        setCommandStatus(
            "Handover blocked: emergency stop is active."
        );


        return;
    }


    const from =
        getVehicle(fromId);

    const to =
        getVehicle(toId);

    const fromName =
        vehicleName(fromId);

    const toName =
        vehicleName(toId);


    /* -----------------------------------------------------
       SAFETY CHECK
    ----------------------------------------------------- */

    if (
        from.emergency_status ||
        to.emergency_status
    ) {

        setCommandStatus(
            "Handover blocked: emergency stop is active."
        );


        return;
    }


    /* -----------------------------------------------------
       FROM VEHICLE MUST BE AT STOP 2 AND 100%
    ----------------------------------------------------- */

    if (
        !norm(
            from.location
        ).includes(
            "stop_2"
        )
    ) {

        setCommandStatus(
            `Handover blocked: ${fromName} is not at Loading Point.`
        );


        return;
    }


    if (
        num(
            from.load_percentage
        ) < 100
    ) {

        setCommandStatus(
            `Handover blocked: ${fromName} is not fully loaded.`
        );


        return;
    }


    /* -----------------------------------------------------
       TO VEHICLE MUST BE AT STOP 1
    ----------------------------------------------------- */

    if (
        !norm(
            to.location
        ).includes(
            "stop_1"
        )
    ) {

        setCommandStatus(
            `Handover blocked: ${toName} is not at Stop 1.`
        );


        return;
    }


    state.handoverRunning =
        true;


    state.visualMove =
        null;


    updateButtons();


    setCommandStatus(
        `Automatic handover: ${fromName} → Warehouse | ${toName} → Loading Point`
    );


    addLog(
        "Automatic fleet handover started."
    );


    const generation =
        state.cycleGeneration;


    /* -----------------------------------------------------
       BACKEND: FROM → WAREHOUSE
    ----------------------------------------------------- */

    const fromResult =
        await directCommand(
            API.warehouse,
            fromId
        );


    /* Reset / Emergency Stop meanwhile: do not start the next
       vehicle. */
    if (
        cycleCancelled(generation)
    ) {

        return;
    }


    if (!fromResult) {

        state.handoverRunning =
            false;


        updateButtons();


        return;
    }


    addLog(
        `${fromName} warehouse command accepted.`
    );


    /* -----------------------------------------------------
       BACKEND: FROM COMPLETES, V2V HANDOVER TO "TO"
       The backend marks the journey COMPLETED, logs the
       V2V-HANDOVER message and makes the next vehicle ready.
    ----------------------------------------------------- */

    const handoverResult =
        await directCommand(
            API.complete,
            fromId
        );


    if (
        cycleCancelled(generation)
    ) {

        return;
    }


    if (!handoverResult) {

        state.handoverRunning =
            false;


        updateButtons();


        setCommandStatus(
            `${fromName} could not hand over to ${toName}.`
        );


        return;
    }


    addLog(
        `${fromName} completed. V2V handover to ${toName}.`
    );


    /* -----------------------------------------------------
       BACKEND: TO START
    ----------------------------------------------------- */

    const toResult =
        await directCommand(
            API.start,
            toId
        );


    if (
        cycleCancelled(generation)
    ) {

        return;
    }


    if (!toResult) {

        state.handoverRunning =
            false;


        updateButtons();


        return;
    }


    addLog(
        `${toName} start command accepted.`
    );


    setCommandStatus(
        "Both vehicles moving..."
    );


    animateFleetHandover(
        fromId,
        toId
    );
}


function animateFleetHandover(
    fromId,
    toId
) {

    if (!routeElementsExist([fromId, toId])) {

        state.handoverRunning =
            false;


        updateButtons();


        return;
    }


    animateRoute(
        [
            {
                vehicleId: fromId,
                from: ROUTE.loading,
                to: ROUTE.warehouse
            },
            {
                vehicleId: toId,
                from: ROUTE.start,
                to: ROUTE.loading
            }
        ],
        {
            isActive: () => state.handoverRunning,

            onEmergency: () => {

                state.handoverRunning =
                    false;


                updateButtons();


                setCommandStatus(
                    "Fleet movement stopped by emergency stop."
                );


                addLog(
                    "Fleet movement stopped by emergency stop."
                );
            },

            onComplete: () => completeFleetHandover(
                fromId,
                toId
            )
        }
    );
}


async function completeFleetHandover(
    fromId,
    toId
) {

    if (
        state.emergency
    ) {

        state.handoverRunning =
            false;


        updateButtons();


        return;
    }


    const fromName =
        vehicleName(fromId);

    const toName =
        vehicleName(toId);

    const generation =
        state.cycleGeneration;


    setRoutePosition(
        fromId,
        ROUTE.warehouse
    );


    setRoutePosition(
        toId,
        ROUTE.loading
    );


    setCommandStatus(
        `${fromName} reached Warehouse. ${toName} reached Loading Point.`
    );


    addLog(
        `${fromName} reached Warehouse.`
    );


    /* -----------------------------------------------------
       TELL BACKEND THE NEXT VEHICLE REACHED STOP 2
    ----------------------------------------------------- */

    const toResult =
        await directCommand(
            API.loadingPoint,
            toId
        );


    if (
        cycleCancelled(generation)
    ) {

        return;
    }


    if (toResult) {

        addLog(
            `${toName} reached Loading Point.`
        );
    }


    /* -----------------------------------------------------
       REFRESH BACKEND DATA, KEEP FINAL VISUAL POSITIONS
    ----------------------------------------------------- */

    await refreshFleet(true);


    if (
        cycleCancelled(generation)
    ) {

        return;
    }


    setRoutePosition(
        fromId,
        ROUTE.warehouse
    );


    setRoutePosition(
        toId,
        ROUTE.loading
    );


    state.handoverRunning =
        false;


    updateButtons();


    setCommandStatus(
        `Fleet handover complete. ${fromName} is at Warehouse and ${toName} is at Loading Point.`
    );


    addLog(
        "Fleet handover completed successfully."
    );
}


/* =========================================================
   FINAL WAREHOUSE MOVEMENT

   LAST VEHICLE: STOP 2 → WAREHOUSE
========================================================= */

async function startFinalWarehouseMovement(
    vehicleId
) {

    if (
        state.handoverRunning
    ) {
        return;
    }


    if (
        state.emergency
    ) {

        setCommandStatus(
            "Warehouse movement blocked: emergency stop is active."
        );

        return;
    }


    const vehicle =
        getVehicle(vehicleId);

    const name =
        vehicleName(vehicleId);


    /* -----------------------------------------------------
       SAFETY CHECK
    ----------------------------------------------------- */

    if (
        vehicle.emergency_status
    ) {

        setCommandStatus(
            "Warehouse movement blocked: emergency stop is active."
        );

        return;
    }


    /* -----------------------------------------------------
       MUST BE AT STOP 2 AND 100%
    ----------------------------------------------------- */

    if (
        !norm(
            vehicle.location
        ).includes(
            "stop_2"
        )
    ) {

        setCommandStatus(
            `Warehouse movement blocked: ${name} is not at Loading Point.`
        );

        return;
    }


    if (
        num(
            vehicle.load_percentage
        ) < 100
    ) {

        setCommandStatus(
            `Warehouse movement blocked: ${name} is not fully loaded.`
        );

        return;
    }


    state.handoverRunning =
        true;


    state.visualMove =
        null;


    updateButtons();


    setCommandStatus(
        `${name} moving from Loading Point to Warehouse...`
    );


    addLog(
        `${name} warehouse movement started.`
    );


    /* -----------------------------------------------------
       FRONTEND ANIMATION, then the backend warehouse
       command in completeFinalWarehouseMovement().
    ----------------------------------------------------- */

    if (!routeElementsExist([vehicleId])) {

        state.handoverRunning =
            false;


        updateButtons();


        return;
    }


    animateRoute(
        [
            {
                vehicleId,
                from: ROUTE.loading,
                to: ROUTE.warehouse
            }
        ],
        {
            isActive: () => state.handoverRunning,

            onEmergency: () => {

                state.handoverRunning =
                    false;

                updateButtons();

                setCommandStatus(
                    `${name} movement stopped by emergency stop.`
                );

                addLog(
                    `${name} warehouse movement stopped by emergency stop.`
                );
            },

            onComplete: () => completeFinalWarehouseMovement(
                vehicleId
            )
        }
    );
}


async function completeFinalWarehouseMovement(
    vehicleId
) {

    if (
        state.emergency
    ) {

        state.handoverRunning =
            false;

        updateButtons();

        return;
    }


    const name =
        vehicleName(vehicleId);

    const generation =
        state.cycleGeneration;


    const result =
        await directCommand(
            API.warehouse,
            vehicleId
        );


    if (
        cycleCancelled(generation)
    ) {

        return;
    }


    if (!result) {

        state.handoverRunning =
            false;

        updateButtons();

        setCommandStatus(
            `${name} warehouse command failed.`
        );

        return;
    }


    addLog(
        `${name} warehouse command accepted.`
    );


    /* Last vehicle of the run: complete it without handing over,
       so the first vehicle is not restarted. */
    const completed =
        await directCommand(
            API.complete,
            vehicleId,
            {
                handover: false
            }
        );


    if (
        cycleCancelled(generation)
    ) {

        return;
    }


    if (completed) {

        addLog(
            `${name} journey completed.`
        );
    }


    /* This vehicle and every vehicle before it in the cycle
       are now at the warehouse. */
    const finished =
        state.order.slice(
            0,
            state.order.indexOf(vehicleId) + 1
        );


    finished.forEach(
        id => setRoutePosition(
            id,
            ROUTE.warehouse
        )
    );


    await refreshFleet(true);


    if (
        cycleCancelled(generation)
    ) {

        return;
    }


    state.handoverRunning =
        false;


    updateButtons();


    setCommandStatus(
        `Cycle complete. ${joinNames(finished)} ${finished.length > 1 ? "are" : "is"} at Warehouse.`
    );


    addLog(
        `${name} reached Warehouse.`
    );


    addLog(
        "Fleet cycle completed successfully."
    );
}


/* =========================================================
   RESET CYCLE
========================================================= */

async function resetCycle() {

    if (
        state.commandBusy
    ) {

        return;
    }


    const confirmed =
        window.confirm(
            "Reset the complete vehicle cycle?"
        );


    if (!confirmed) {

        return;
    }


    /* -----------------------------------------------------
       STOP ALL FRONTEND ACTIONS
       (any pending cycle step sees the cancel and stops)
    ----------------------------------------------------- */

    cancelCycle();


    setCommandStatus(
        "Resetting fleet cycle..."
    );


    addLog(
        "RESET CYCLE pressed."
    );


    const resetResult =
        await postCommand(
            API.reset,
            firstVehicleId()
        );


    if (resetResult) {

        /* Reset also cleared every emergency stop. */
        state.emergency =
            false;


        const resetVehicles =
            resetResult?.data?.vehicles;


        if (
            resetVehicles &&
            typeof resetVehicles === "object"
        ) {

            Object.entries(resetVehicles).forEach(
                ([vehicleId, raw]) => {

                    const previous =
                        state.fleet.get(vehicleId);


                    if (!previous) {
                        return;
                    }


                    state.fleet.set(
                        vehicleId,
                        normalizeVehicle({
                            ...raw,
                            vehicle_id: vehicleId,
                            name: raw?.name || previous.name,
                            color: raw?.color || previous.color
                        })
                    );
                }
            );


            renderFleet();
        }


        state.order.forEach(
            vehicleId => setRoutePosition(
                vehicleId,
                ROUTE.start
            )
        );


        const syncFailed =
            resetResult?.data?.thingsboard_failed || [];


        if (syncFailed.length) {

            /* The backend message explains the risk. */
            setCommandStatus(
                resetResult.message
            );

            addLog(
                `Reset: ThingsBoard not updated for ${joinNames(syncFailed)}.`
            );

        } else {

            setCommandStatus(
                "Fleet cycle reset successfully."
            );

            addLog(
                "Fleet cycle reset successfully."
            );
        }
    }


    updateButtons();
}


/* =========================================================
   EMERGENCY STOP (WHOLE FLEET)
========================================================= */

async function emergencyStop() {

    /*
     * IMPORTANT:
     * Emergency button must always be able to work. It is only
     * blocked while its own request is in flight; after a
     * failure it can be pressed again straight away.
     */

    if (
        state.emergencyRequest
    ) {

        return;
    }


    /* -----------------------------------------------------
       STOP FRONTEND ANIMATION AND CYCLE IMMEDIATELY
       (before the backend answers)
    ----------------------------------------------------- */

    cancelCycle();


    state.emergencyRequest =
        true;


    updateButtons();


    setCommandStatus(
        "Activating emergency stop..."
    );


    addLog(
        "EMERGENCY STOP pressed."
    );


    const outcome =
        await emergencyCommand(
            API.emergency,
            { fleet: true }
        );


    state.emergencyRequest =
        false;


    /* Vehicles are stopped on the dashboard whenever the
       backend stopped them, even if the hardware or one
       vehicle failed (outcome.stopped). */
    if (outcome.stopped) {

        state.emergency =
            true;


        state.order.forEach(
            vehicleId => setRoutePosition(
                vehicleId,
                backendPosition(
                    getVehicle(vehicleId).location
                )
            )
        );
    }


    if (outcome.ok) {

        setCommandStatus(
            "Emergency stop activated."
        );


        addLog(
            "Emergency stop activated."
        );

    } else {

        setCommandStatus(
            `Emergency stop problem: ${outcome.message} ` +
            "You can press EMERGENCY STOP again."
        );
    }


    updateButtons();
}


/* =========================================================
   RELEASE EMERGENCY STOP (WHOLE FLEET)
   Vehicles stay stopped with brakes applied; START resumes.
========================================================= */

async function releaseEmergency() {

    if (
        state.emergencyRequest
    ) {

        return;
    }


    const confirmed =
        window.confirm(
            "Release the emergency stop for all vehicles?\n\n" +
            "Vehicles stay stopped with brakes applied until you press START."
        );

    if (!confirmed) {
        return;
    }


    state.emergencyRequest =
        true;


    updateButtons();


    setCommandStatus(
        "Releasing emergency stop..."
    );


    const outcome =
        await emergencyCommand(
            API.emergencyRelease,
            { fleet: true }
        );


    state.emergencyRequest =
        false;


    if (outcome.ok) {

        state.emergency =
            false;


        setCommandStatus(
            "Emergency stop released. Vehicles are stopped with brakes applied."
        );


        addLog(
            "Emergency stop released."
        );

    } else {

        setCommandStatus(
            `Release failed: ${outcome.message}`
        );
    }


    updateButtons();
}


/* =========================================================
   EMERGENCY COMMAND

   Separate from postCommand because postCommand is blocked
   when commandBusy. The backend stops every vehicle.
========================================================= */

/* Returns {ok, stopped, message}:
   ok      - the backend reported full success
   stopped - at least the dashboard state changed (the backend
             answered with vehicle data), even if not ok
   message - backend message or the network error */
async function emergencyCommand(
    url,
    body
) {

    let outcome = {
        ok: false,
        stopped: false,
        message: ""
    };


    try {

        const response =
            await fetch(
                url,
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify(body)
                }
            );


        let result = {};


        try {

            result =
                await response.json();

        } catch {

            result = {};
        }


        console.log(
            "Emergency response:",
            result
        );


        outcome = {

            ok:
                response.ok &&
                result?.success !== false,

            stopped:
                Boolean(result?.data?.vehicles) &&
                Object.keys(result.data.vehicles).length > 0,

            message:
                result?.message ||
                result?.error ||
                `HTTP ${response.status}`
        };


        const syncFailed =
            result?.data?.thingsboard_failed || [];

        if (syncFailed.length) {

            addLog(
                `ThingsBoard not updated for ${joinNames(syncFailed)}.`
            );
        }


    } catch (error) {

        console.error(
            "Emergency command:",
            error
        );

        outcome.message =
            error.message;
    }


    if (!outcome.ok) {

        addLog(
            `Emergency command failed: ${outcome.message}`
        );
    }


    await refreshFleet(true);


    return outcome;
}


/* =========================================================
   BUTTON STATE
========================================================= */

function canVehicleLoad(vehicle) {

    return (

        norm(vehicle.location).includes("stop_2") &&

        num(vehicle.load_percentage) < 100 &&

        norm(
            vehicle.braking_status ??
            vehicle.brake
        ) === "applied" &&

        !norm(vehicle.radar_status).includes("obstacle") &&

        !vehicle.emergency_status
    );
}


function updateButtons() {

    const start =
        $("startButton");

    const loading =
        $("loadingButton");

    const reset =
        $("resetButton");

    const emergency =
        $("emergencyButton");


    const vehicles =
        fleetVehicles();


    const emergencyActive =
        state.emergency ||
        vehicles.some(vehicle => vehicle.emergency_status);


    /* -----------------------------------------------------
       START / RESUME
    ----------------------------------------------------- */

    if (start) {

        start.disabled =

            !state.backendOnline ||

            !vehicles.length ||

            emergencyActive ||

            state.commandBusy ||

            state.loadingCycle ||

            state.handoverRunning;
    }


    /* -----------------------------------------------------
       LOADING BUTTON
       Enabled if ANY vehicle is ready to load.
    ----------------------------------------------------- */

    if (loading) {

        loading.disabled =

            !vehicles.some(canVehicleLoad) ||

            emergencyActive ||

            !state.backendOnline ||

            state.commandBusy ||

            state.loadingCycle ||

            state.handoverRunning;
    }


    /* -----------------------------------------------------
       RESET CYCLE
    ----------------------------------------------------- */

    if (reset) {

        reset.disabled =

            !state.backendOnline ||

            state.commandBusy ||

            state.loadingCycle ||

            state.handoverRunning;
    }


    /* -----------------------------------------------------
       EMERGENCY STOP
       Always available, except while its own request is in
       flight. Pressing it again re-sends the stop (needed if
       the hardware stop failed the first time).
    ----------------------------------------------------- */

    if (emergency) {

        emergency.disabled =
            state.emergencyRequest;
    }


    /* -----------------------------------------------------
       RELEASE EMERGENCY
       Shown only while an emergency stop is active.
    ----------------------------------------------------- */

    const release =
        $("releaseButton");

    if (release) {

        release.hidden =
            !emergencyActive;

        release.disabled =
            !state.backendOnline ||
            state.emergencyRequest;

        release.closest(".control-buttons")?.classList.toggle(
            "with-release",
            emergencyActive
        );
    }
}


/* =========================================================
   BUTTON + CONTROL CONNECTION
========================================================= */

function connectButtons() {

    const start =
        $("startButton");


    const loading =
        $("loadingButton");


    const reset =
        $("resetButton");


    const emergency =
        $("emergencyButton");


    if (start) {

        start.addEventListener(
            "click",
            startResume
        );
    }


    if (loading) {

        loading.addEventListener(
            "click",
            startLoading
        );
    }


    if (reset) {

        reset.addEventListener(
            "click",
            resetCycle
        );
    }


    if (emergency) {

        emergency.addEventListener(
            "click",
            emergencyStop
        );
    }


    $("releaseButton")?.addEventListener(
        "click",
        releaseEmergency
    );
}


function connectFleetControls() {

    const search =
        $("vehicleSearch");


    if (search) {

        search.addEventListener(
            "input",
            event => {

                state.search =
                    event.target.value;

                applyVisibility();
            }
        );
    }


    const select =
        $("vehicleSelect");


    if (select) {

        select.addEventListener(
            "change",
            event => selectVehicle(
                event.target.value
            )
        );
    }


    renderFilters();
}


/* =========================================================
   INITIALIZE DASHBOARD
========================================================= */

async function initializeDashboard() {

    console.log(
        "===================================="
    );


    console.log(
        "V2V FRONTEND - DYNAMIC FLEET"
    );


    console.log(
        "Backend:",
        API_BASE_URL
    );


    console.log(
        "===================================="
    );


    connectButtons();


    connectFleetControls();


    await checkHealth();


    await refreshFleet(true);


    setCommandStatus(

        state.backendOnline

            ? "System ready for commands."

            : "Waiting for backend connection..."
    );


    addLog(
        "Frontend controller started."
    );


    updateButtons();


    /* -----------------------------------------------------
       LIVE DATA REFRESH
       During animation, updateRouteVehicle() does not
       overwrite the animated positions.
    ----------------------------------------------------- */

    scheduleRefresh();


    setInterval(
        checkHealth,
        HEALTH_INTERVAL
    );
}


/* =========================================================
   START APPLICATION
========================================================= */

initializeDashboard();
