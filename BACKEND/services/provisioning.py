# =========================================================
# VEHICLE PROVISIONING
# =========================================================
#
# Adds, renames and removes vehicles together with their
# ThingsBoard devices, so the dashboard only needs a name.
#
# Add (auto):
#   1. validate name/colour locally (nothing created yet)
#   2. check the ThingsBoard device limit
#   3. check the device name is free in ThingsBoard
#   4. create the device           -> device ID
#   5. read its credentials        -> access token
#   6. save to data/vehicles.json  (managed=True)
#   If 5 or 6 fails, the device from step 4 is deleted again,
#   so a failed add never leaves an orphan device using quota.
#
# Only vehicles with managed=True (device created here) are
# ever renamed or deleted in ThingsBoard. Linked vehicles
# (existing device ID + token) are never touched there.
# =========================================================

from . import fleet
from . import thingsboard


# =========================================================
# ERRORS
# =========================================================

def _limit_error(quota):

    return fleet.FleetError(
        f"ThingsBoard device limit reached ({quota['devices']} of "
        f"{quota['max_devices']} devices used). Delete an unused device "
        "in ThingsBoard or upgrade the plan, or link an existing device.",
        "DEVICE_LIMIT_REACHED",
        409
    )


# =========================================================
# QUOTA
# =========================================================

def device_quota():
    """{"devices", "max_devices", "remaining"} or None if unknown."""

    quota = thingsboard.get_device_quota()

    if quota is None:
        return None

    remaining = None

    if quota["max_devices"] is not None and quota["devices"] is not None:

        remaining = max(
            0,
            quota["max_devices"] - quota["devices"]
        )

    return {
        **quota,
        "remaining": remaining
    }


# =========================================================
# ADD
# =========================================================

def provision_vehicle(name, color=None):
    """Create the ThingsBoard device and register the vehicle.

    Raises fleet.FleetError for anything the user can fix,
    thingsboard.ThingsBoardError / requests errors otherwise.
    """

    name, color = fleet.check_new_vehicle(
        name,
        color
    )

    quota = device_quota()

    if quota and quota["remaining"] == 0:
        raise _limit_error(quota)

    if thingsboard.find_device_by_name(name) is not None:

        raise fleet.FleetError(
            f"ThingsBoard already has a device named {name}. Choose "
            "another name, or use 'Link existing device' with that "
            "device's ID and access token.",
            "DEVICE_NAME_TAKEN",
            409
        )

    device_id = thingsboard.create_device(
        name,
        label=name
    )

    try:

        token = thingsboard.get_device_access_token(
            device_id
        )

        return fleet.add_vehicle(
            name,
            device_id,
            token,
            color,
            managed=True
        )

    except BaseException:

        try:
            thingsboard.delete_device(device_id)
        except Exception as cleanup_error:
            print(
                f"[provisioning] Could not delete device {device_id} "
                f"after a failed add; delete it in ThingsBoard: "
                f"{cleanup_error}"
            )

        raise


# =========================================================
# RENAME
# =========================================================

def rename_managed_device(current, new_name):
    """Rename the ThingsBoard device of a managed vehicle first.

    Returns a callable that undoes the rename, or None when
    there is nothing to rename in ThingsBoard.
    """

    vehicle = fleet.require(current)

    if not vehicle.managed:
        return None

    new_name = fleet.check_rename(
        current,
        new_name
    )

    if new_name == vehicle.name:
        return None

    existing = thingsboard.find_device_by_name(new_name)

    if (
        existing is not None
        and existing.get("id", {}).get("id") != vehicle.device_id
    ):

        raise fleet.FleetError(
            f"ThingsBoard already has a device named {new_name}.",
            "DEVICE_NAME_TAKEN",
            409
        )

    thingsboard.rename_device(
        vehicle.device_id,
        new_name
    )

    return lambda: thingsboard.rename_device(
        vehicle.device_id,
        vehicle.name
    )


# =========================================================
# DELETE
# =========================================================

def remove_vehicle(name, delete_device=False):
    """Remove a vehicle; optionally delete its ThingsBoard device too.

    The device is deleted first: if that fails, nothing is
    removed. Linked (not managed) devices are never deleted.
    """

    vehicle = fleet.require(name)

    if delete_device:

        if not vehicle.managed:

            raise fleet.FleetError(
                f"{vehicle.name} is linked to a device that was not created "
                "by this dashboard, so it is not deleted in ThingsBoard. "
                "Remove it from the dashboard only.",
                "DEVICE_NOT_MANAGED",
                400
            )

        thingsboard.delete_device(
            vehicle.device_id
        )

    return fleet.delete_vehicle(name)
