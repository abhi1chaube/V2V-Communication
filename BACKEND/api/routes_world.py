# =========================================================
# WORLD MODEL ENDPOINTS (/api/v1)
# =========================================================

from fastapi import APIRouter

from services import fleet
from services import world

from .common import error, success


router = APIRouter(prefix="/api/v1")


@router.get("/world", tags=["World"], summary="Route map")
def get_world():
    """The yard as a graph: nodes (stops, junctions, corners, charging bay)
    with x/y in metres, one-way segments with length, lane and speed limit,
    shared lanes (one lane used in both directions), the stop each mission
    leg ends at, and the world engine's status."""

    return success(
        "World retrieved.",
        {
            **world.world.describe(),
            "engine": world.engine.status()
        }
    )


@router.get("/poses", tags=["World"], summary="Every vehicle's position")
def get_poses():
    """Continuous position of every vehicle, in fleet order: segment and
    distance along it (or the node it is parked at), x/y, heading, actual
    speed, destination, distance and ETA."""

    poses = world.engine.poses_snapshot()

    return success(
        "Poses retrieved.",
        {
            "count": len(poses),
            "poses": poses,
            "engine": world.engine.status()
        }
    )


@router.get("/vehicles/{vehicle}/pose", tags=["World"], summary="One vehicle's position")
def get_pose(vehicle: str):
    """`vehicle` accepts a name or alias (Vehicle_01, 01, 1, yellow)."""

    vehicle_id = fleet.resolve(vehicle)

    if vehicle_id is None:
        return error(
            f"Vehicle must be one of: {fleet.describe()}.",
            400,
            "INVALID_VEHICLE"
        )

    return success(
        "Pose retrieved.",
        world.engine.pose(vehicle_id)
    )
