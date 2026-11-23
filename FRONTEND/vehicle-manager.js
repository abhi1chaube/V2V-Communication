/* =========================================================
   V2V FLEET CONTROL SYSTEM
   VEHICLE MANAGER

   Add, edit, delete and search fleet vehicles through
   /api/vehicles. The backend stores them in
   BACKEND/data/vehicles.json.

   Adding needs only a name (and optional colour): the backend
   creates the ThingsBoard device and reads its ID and access
   token ("auto" mode). "Link an existing ThingsBoard device"
   keeps the old way, for devices that already exist (e.g.
   real hardware).

   Vehicles whose device was created here are "managed": only
   those can be renamed or deleted in ThingsBoard from here.

   After every change the dashboard is refreshed with
   refreshFleet(true), so cards, route markers, the selector,
   the table, alerts and fleet statistics pick the vehicle up
   immediately.

   Loaded after script.js and uses its globals:
   API_BASE_URL, $, norm, applyAccent, refreshFleet,
   selectVehicle, state.
========================================================= */

const VEHICLES_API =
    `${API_BASE_URL}/api/vehicles`;

const QUOTA_API =
    `${API_BASE_URL}/api/thingsboard/quota`;


const manager = {

    /* Public records from GET /api/vehicles, in fleet order. */
    vehicles: [],

    search: "",

    /* Vehicle being edited, or null when adding. */
    editing: null,

    /* Adding: true = link an existing device, false = create one. */
    linkMode: false,

    /* {devices, max_devices, remaining} from ThingsBoard, or null. */
    quota: null
};


/* =========================================================
   API
========================================================= */

async function requestJson(
    url,
    options = {}
) {

    const response =
        await fetch(
            url,
            {
                cache: "no-store",
                headers: {
                    "Content-Type": "application/json"
                },
                ...options
            }
        );


    let result =
        null;

    try {
        result = await response.json();
    } catch {
        /* Non-JSON error page: fall back to the HTTP status. */
    }


    if (
        !response.ok ||
        result?.success === false
    ) {

        throw new Error(
            result?.message ||
            `HTTP ${response.status}`
        );
    }


    return result;
}


function vehicleUrl(name) {

    return `${VEHICLES_API}/${encodeURIComponent(name)}`;
}


/* =========================================================
   STATUS MESSAGES
========================================================= */

function setManagerStatus(
    message,
    tone = ""
) {

    const element =
        $("managerStatus");

    if (!element) {
        return;
    }

    element.textContent =
        message;

    element.dataset.tone =
        tone;
}


function setFormStatus(message) {

    const element =
        $("vehicleFormStatus");

    if (element) {
        element.textContent = message;
    }
}


/* A running cycle animates vehicles by name, so removing or
   renaming one mid-cycle would break the animation. */
function fleetCycleRunning() {

    return Boolean(
        state.commandBusy ||
        state.loadingCycle ||
        state.handoverRunning ||
        state.visualMove
    );
}


/* =========================================================
   THINGSBOARD DEVICE QUOTA
========================================================= */

function quotaText(quota) {

    if (
        !quota ||
        quota.max_devices == null
    ) {
        return "";
    }

    return (
        `${quota.devices} of ${quota.max_devices} ThingsBoard ` +
        `devices used (${quota.remaining} left)`
    );
}


function quotaFull() {

    return manager.quota?.remaining === 0;
}


async function loadQuota() {

    try {

        const result =
            await requestJson(QUOTA_API);

        manager.quota =
            result?.data?.quota || null;

    } catch {

        /* ThingsBoard unreachable: adding still reports a clear
           error from the backend, so just hide the counter. */
        manager.quota =
            null;
    }


    renderQuota();

    updateProvisionInfo();
}


function renderQuota() {

    const element =
        $("managerQuota");

    if (!element) {
        return;
    }

    const text =
        quotaText(manager.quota);

    element.hidden =
        !text;

    element.textContent =
        text;

    element.classList.toggle(
        "quota-full",
        quotaFull()
    );
}


/* =========================================================
   LOAD + RENDER
========================================================= */

/* quiet=true keeps the current status message unless there
   is a configuration problem to report. */
async function loadManagedVehicles(quiet = false) {

    /* Deleting or auto-removing a vehicle can free a slot. */
    loadQuota();


    try {

        const result =
            await requestJson(VEHICLES_API);


        manager.vehicles =
            result?.data?.vehicles || [];


        renderManager();


        const problems =
            result?.data?.problems || [];


        if (
            quiet &&
            !problems.length
        ) {
            return true;
        }


        if (problems.length) {

            setManagerStatus(
                `Check configuration: ${problems.join("; ")}`,
                "warning"
            );

        } else {

            setManagerStatus(
                manager.vehicles.length
                    ? `${manager.vehicles.length} vehicle(s) registered.`
                    : "No vehicles registered yet. Click ADD NEW VEHICLE."
            );
        }


        return true;

    } catch (error) {

        setManagerStatus(
            `Unable to load vehicles: ${error.message}`,
            "error"
        );

        return false;
    }
}


function managerMatches(vehicle) {

    const query =
        norm(manager.search);

    if (!query) {
        return true;
    }

    return (
        norm(vehicle.name).includes(query) ||
        norm(vehicle.device_id).includes(query)
    );
}


function createManagerButton(
    label,
    className,
    ariaLabel,
    onClick
) {

    const button =
        document.createElement("button");

    button.type =
        "button";

    button.className =
        `manager-action ${className}`;

    button.textContent =
        label;

    button.setAttribute(
        "aria-label",
        ariaLabel
    );

    button.addEventListener(
        "click",
        onClick
    );

    return button;
}


function createManagerRow(vehicle) {

    const row =
        document.createElement("tr");


    const nameCell =
        document.createElement("td");

    const dot =
        document.createElement("span");

    dot.className =
        "vehicle-dot";

    applyAccent(
        dot,
        vehicle.color
    );

    const name =
        document.createElement("strong");

    name.textContent =
        vehicle.name;

    nameCell.append(
        dot,
        name
    );


    const deviceCell =
        document.createElement("td");

    deviceCell.className =
        "manager-mono";

    deviceCell.textContent =
        vehicle.device_id || "— missing —";


    const tokenCell =
        document.createElement("td");

    tokenCell.className =
        "manager-mono";

    tokenCell.textContent =
        vehicle.has_token
            ? vehicle.token_hint
            : "— missing —";


    const colorCell =
        document.createElement("td");

    colorCell.textContent =
        vehicle.color || "default";


    const sourceCell =
        document.createElement("td");

    const source =
        document.createElement("span");

    source.className =
        vehicle.managed
            ? "source-badge managed"
            : "source-badge linked";

    source.textContent =
        vehicle.managed
            ? "CREATED BY DASHBOARD"
            : "LINKED";

    source.title =
        vehicle.managed
            ? "This dashboard created the ThingsBoard device. Renaming renames it in ThingsBoard; delete can also delete it there."
            : "Linked to an existing ThingsBoard device. The device is never changed or deleted from here.";

    sourceCell.appendChild(
        source
    );


    const actionsCell =
        document.createElement("td");

    actionsCell.className =
        "manager-actions";

    actionsCell.append(

        createManagerButton(
            "EDIT",
            "edit",
            `Edit ${vehicle.name}`,
            () => openVehicleDialog(vehicle)
        ),

        createManagerButton(
            "DELETE",
            "delete",
            `Delete ${vehicle.name}`,
            () => deleteManagedVehicle(vehicle.name)
        )
    );


    row.append(
        nameCell,
        deviceCell,
        tokenCell,
        colorCell,
        sourceCell,
        actionsCell
    );


    if (
        !vehicle.device_id ||
        !vehicle.has_token
    ) {
        row.classList.add("incomplete");
    }


    return row;
}


function renderManager() {

    const body =
        $("managerTableBody");

    if (!body) {
        return;
    }


    const visible =
        manager.vehicles.filter(managerMatches);


    const rows =
        visible.map(createManagerRow);


    if (!rows.length) {

        const row =
            document.createElement("tr");

        const cell =
            document.createElement("td");

        cell.colSpan =
            6;

        cell.className =
            "manager-empty";

        cell.textContent =
            manager.vehicles.length
                ? "No vehicles match the search."
                : "No vehicles registered yet.";

        row.appendChild(cell);

        rows.push(row);
    }


    body.replaceChildren(
        ...rows
    );


    const count =
        $("managerCount");

    if (count) {

        count.textContent =
            manager.search
                ? `${visible.length} of ${manager.vehicles.length} vehicles`
                : `${manager.vehicles.length} vehicles`;
    }
}


/* =========================================================
   ADD / EDIT DIALOG
========================================================= */

/* Next free Vehicle_NN after the highest number in use. */
function suggestVehicleName() {

    const highest =
        manager.vehicles.reduce(
            (max, vehicle) => {

                const match =
                    String(vehicle.name).match(/(\d+)$/);

                return match
                    ? Math.max(max, Number(match[1]))
                    : max;
            },
            0
        );

    return `Vehicle_${String(highest + 1).padStart(2, "0")}`;
}


/* Show or hide the existing-device fields. Hidden fields are
   disabled, so the browser does not validate them. */
function setLinkFields(show) {

    $("vehicleLinkFields").hidden =
        !show;

    $("vehicleDeviceInput").disabled =
        !show;

    $("vehicleTokenInput").disabled =
        !show;
}


function setLinkMode(on) {

    manager.linkMode =
        on;

    $("vehicleLinkToggle").checked =
        on;

    setLinkFields(on);

    $("vehicleTokenInput").required =
        on;

    $("vehicleTokenInput").placeholder =
        "Device access token";

    $("vehicleTokenHelp").textContent =
        "Stored on the server only. It is never shown again.";

    updateProvisionInfo();
}


/* The line under the colour field: what will happen in
   ThingsBoard when the form is saved. */
function updateProvisionInfo() {

    const info =
        $("vehicleProvisionInfo");

    const save =
        $("vehicleSaveButton");

    if (
        !info ||
        !save
    ) {
        return;
    }


    let text =
        "";

    let tone =
        "";

    let blocked =
        false;


    const vehicle =
        manager.editing;


    if (vehicle?.managed) {

        text =
            "ThingsBoard device created by this dashboard. Renaming the " +
            "vehicle also renames the device in ThingsBoard.";

    } else if (vehicle) {

        text =
            "Linked to an existing ThingsBoard device. The device is " +
            "never changed from here.";

    } else if (manager.linkMode) {

        text =
            "The vehicle will use the existing device below. Nothing is " +
            "created in ThingsBoard.";

    } else if (quotaFull()) {

        text =
            `ThingsBoard device limit reached: ${quotaText(manager.quota)}. ` +
            "Delete an unused device in ThingsBoard, upgrade the plan, " +
            "or link an existing device.";

        tone =
            "warning";

        blocked =
            true;

    } else {

        const quota =
            quotaText(manager.quota);

        text =
            "A ThingsBoard device with this name is created automatically, " +
            "with its device ID and access token." +
            (quota ? ` ${quota}.` : "");
    }


    info.textContent =
        text;

    info.dataset.tone =
        tone;

    save.disabled =
        blocked;

    save.textContent =
        !vehicle && !manager.linkMode
            ? "CREATE VEHICLE"
            : "SAVE VEHICLE";
}


function openVehicleDialog(vehicle = null) {

    const dialog =
        $("vehicleDialog");

    const form =
        $("vehicleForm");

    if (
        !dialog ||
        !form
    ) {
        return;
    }


    manager.editing =
        vehicle;


    form.reset();

    setFormStatus("");


    $("vehicleDialogTitle").textContent =
        vehicle
            ? `Edit ${vehicle.name}`
            : "Add New Vehicle";


    $("vehicleNameInput").value =
        vehicle ? vehicle.name : suggestVehicleName();

    $("vehicleColorInput").value =
        vehicle?.color || "";


    /* The link toggle is only offered when adding. */
    $("vehicleLinkToggleRow").hidden =
        Boolean(vehicle);


    if (!vehicle) {

        setLinkMode(false);

        /* Fresh quota for the info line. */
        loadQuota();

    } else if (vehicle.managed) {

        /* The device belongs to the vehicle: not editable. */
        manager.linkMode =
            false;

        setLinkFields(false);

        updateProvisionInfo();

    } else {

        manager.linkMode =
            true;

        setLinkFields(true);

        $("vehicleDeviceInput").value =
            vehicle.device_id || "";

        const token =
            $("vehicleTokenInput");

        token.required =
            false;

        token.placeholder =
            "Leave blank to keep the current token";

        $("vehicleTokenHelp").textContent =
            `Current token: ${vehicle.token_hint || "none"}. Stored on the server only.`;

        updateProvisionInfo();
    }


    dialog.showModal();


    const name =
        $("vehicleNameInput");

    name.focus();

    name.select();
}


function closeVehicleDialog() {

    const dialog =
        $("vehicleDialog");

    if (dialog?.open) {
        dialog.close();
    }
}


function firstInvalidMessage(form) {

    const invalid =
        form.querySelector(":invalid");

    if (!invalid) {
        return "";
    }

    const label =
        invalid
            .closest(".form-field")
            ?.querySelector(".info-label")
            ?.textContent
            .trim() || "Field";

    invalid.focus();

    if (invalid.validity.valueMissing) {
        return `${label} is required.`;
    }

    return `${label} has an invalid format.`;
}


function buildPayload() {

    const vehicle =
        manager.editing;

    const payload = {

        name:
            $("vehicleNameInput").value.trim(),

        color:
            $("vehicleColorInput").value.trim()
    };


    if (
        !vehicle &&
        !manager.linkMode
    ) {

        payload.mode =
            "auto";

        return payload;
    }


    if (vehicle?.managed) {
        return payload;
    }


    if (!vehicle) {
        payload.mode = "link";
    }

    payload.device_id =
        $("vehicleDeviceInput").value.trim();

    const token =
        $("vehicleTokenInput").value.trim();

    if (
        token ||
        !vehicle
    ) {
        payload.token = token;
    }


    return payload;
}


async function submitVehicleForm(event) {

    event.preventDefault();


    const form =
        $("vehicleForm");


    const invalid =
        firstInvalidMessage(form);

    if (invalid) {

        setFormStatus(invalid);

        return;
    }


    const vehicle =
        manager.editing;

    const payload =
        buildPayload();


    if (
        vehicle &&
        payload.name !== vehicle.name &&
        fleetCycleRunning()
    ) {

        setFormStatus(
            "Wait for the current cycle to finish before renaming a vehicle."
        );

        return;
    }


    const save =
        $("vehicleSaveButton");

    const label =
        save.textContent;

    save.disabled =
        true;

    if (payload.mode === "auto") {

        save.textContent =
            "CREATING DEVICE...";

        setFormStatus("");
    }


    try {

        const result =
            await requestJson(
                vehicle ? vehicleUrl(vehicle.name) : VEHICLES_API,
                {
                    method: vehicle ? "PUT" : "POST",
                    body: JSON.stringify(payload)
                }
            );


        closeVehicleDialog();


        /* The backend logs the change as an event, so the
           Activity Log shows it without a client entry. */
        setManagerStatus(
            result.message,
            "success"
        );


        await afterFleetChange(
            result?.data?.vehicle?.name
        );

    } catch (error) {

        setFormStatus(
            error.message
        );

    } finally {

        save.textContent =
            label;

        updateProvisionInfo();
    }
}


/* =========================================================
   DELETE
========================================================= */

/* Resolves {confirmed, deleteDevice}. */
function askDelete(vehicle) {

    const dialog =
        $("deleteDialog");


    $("deleteDialogTitle").textContent =
        `Delete ${vehicle.name}?`;

    $("deleteDialogText").textContent =
        vehicle.managed
            ? `${vehicle.name} is removed from the dashboard and data/vehicles.json. ` +
              "Its ThingsBoard device was created by this dashboard; tick the box " +
              "to delete the device as well. Otherwise it stays in ThingsBoard."
            : `${vehicle.name} is removed from the dashboard and data/vehicles.json. ` +
              "Its ThingsBoard device is linked, not created here, so it is not changed.";


    const row =
        $("deleteDeviceRow");

    const toggle =
        $("deleteDeviceToggle");

    const confirm =
        $("deleteConfirmButton");

    row.hidden =
        !vehicle.managed;

    toggle.checked =
        false;

    confirm.textContent =
        "DELETE VEHICLE";

    toggle.onchange = () => {

        confirm.textContent =
            toggle.checked
                ? "DELETE VEHICLE + DEVICE"
                : "DELETE VEHICLE";
    };


    dialog.returnValue =
        "";

    dialog.showModal();


    return new Promise(
        resolve => dialog.addEventListener(
            "close",
            () => resolve({
                confirmed: dialog.returnValue === "delete",
                deleteDevice: Boolean(vehicle.managed && toggle.checked)
            }),
            { once: true }
        )
    );
}


async function deleteManagedVehicle(name) {

    if (fleetCycleRunning()) {

        setManagerStatus(
            "Wait for the current cycle to finish before deleting a vehicle.",
            "warning"
        );

        return;
    }


    const vehicle =
        manager.vehicles.find(v => v.name === name) ||
        { name, managed: false };


    const { confirmed, deleteDevice } =
        await askDelete(vehicle);

    if (!confirmed) {
        return;
    }


    try {

        const result =
            await requestJson(
                vehicleUrl(name) + (deleteDevice ? "?delete_device=true" : ""),
                {
                    method: "DELETE"
                }
            );


        setManagerStatus(
            result.message,
            "success"
        );


        await afterFleetChange();

    } catch (error) {

        setManagerStatus(
            `Delete failed: ${error.message}`,
            "error"
        );
    }
}


/* =========================================================
   REFRESH AFTER A CHANGE
========================================================= */

async function afterFleetChange(selectName = null) {

    const message =
        $("managerStatus")?.textContent;


    await loadManagedVehicles();


    /* Keep the success message instead of the generic count. */
    if (message) {

        setManagerStatus(
            message,
            "success"
        );
    }


    await refreshFleet(true);


    if (
        selectName &&
        state.fleet.has(selectName)
    ) {
        selectVehicle(selectName);
    }
}


/* =========================================================
   INITIALIZE
========================================================= */

function initializeVehicleManager() {

    document
        .querySelectorAll('[data-action="add-vehicle"]')
        .forEach(
            button => button.addEventListener(
                "click",
                () => openVehicleDialog()
            )
        );


    $("vehicleForm")?.addEventListener(
        "submit",
        submitVehicleForm
    );


    $("vehicleCancelButton")?.addEventListener(
        "click",
        closeVehicleDialog
    );


    $("vehicleLinkToggle")?.addEventListener(
        "change",
        event => {

            setLinkMode(
                event.target.checked
            );

            setFormStatus("");
        }
    );


    $("managerSearch")?.addEventListener(
        "input",
        event => {

            manager.search =
                event.target.value;

            renderManager();
        }
    );


    /* Vehicles removed because their ThingsBoard device was
       deleted (or changed in another tab) leave this table
       too, without a page reload. */
    window.addEventListener(
        "fleet:changed",
        () => loadManagedVehicles(true)
    );


    loadManagedVehicles();
}


initializeVehicleManager();
