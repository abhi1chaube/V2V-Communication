# =========================================================
# REQUEST BODY MODELS (API DOCUMENTATION)
# =========================================================
#
# These models describe the JSON bodies in /apidocs. The
# routes parse bodies with api.common.json_body() and keep
# their own validation, so error codes such as
# INVALID_PERCENTAGE stay exactly as the dashboard expects.
# =========================================================

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


VEHICLE_FIELD = Field(
    description="Vehicle name or alias: Vehicle_01, vehicle 01, 01, 1, or its colour (yellow).",
    examples=["Vehicle_01"]
)


class VehicleRef(BaseModel):
    vehicle: str = VEHICLE_FIELD


class LoadingUpdate(BaseModel):
    vehicle: str = VEHICLE_FIELD
    percentage: Optional[float] = Field(
        None, ge=0, le=100, description="Load in percent, 0-100.", examples=[60]
    )
    load_percentage: Optional[float] = Field(
        None, ge=0, le=100, description="Alias of `percentage`."
    )
    force: Optional[bool] = Field(
        False, description="Allow a value lower than the current load (to correct a wrong reading)."
    )


class CompleteJourney(BaseModel):
    vehicle: str = VEHICLE_FIELD
    handover: Optional[bool] = Field(
        True,
        description="Hand over to the next vehicle (default). false completes the last "
                    "vehicle of a run without starting the next cycle.",
    )


class ManualCommand(BaseModel):
    vehicle: str = VEHICLE_FIELD
    command: Literal["FORWARD", "BACKWARD", "LEFT", "RIGHT", "STOP"] = Field(
        description="Case-insensitive.", examples=["FORWARD"]
    )


class RadarInput(BaseModel):
    vehicle: str = VEHICLE_FIELD
    obstacle: Optional[Union[bool, str]] = Field(
        None, description="true/false (also 'obstacle'/'clear'). Ignored when `status` is given.", examples=[True]
    )
    status: Optional[Literal["clear", "obstacle"]] = None


class BrakeInput(BaseModel):
    vehicle: str = VEHICLE_FIELD
    applied: Optional[Union[bool, str]] = Field(
        True, description="true/false (also 'applied'/'released'). Default true.", examples=[True]
    )


class QrInput(BaseModel):
    vehicle: str = VEHICLE_FIELD
    detected: Optional[bool] = Field(None, description="Ignored when `status` is given.")
    status: Optional[Literal["detected", "not_detected"]] = None


class FleetScope(BaseModel):
    fleet: Optional[bool] = Field(
        None, description="Act on the whole fleet (the default when no vehicle is given).", examples=[True]
    )
    vehicle: Optional[str] = Field(None, description="Act on this vehicle only (release only).")


class VehicleCreate(BaseModel):
    name: str = Field(
        description="1-40 letters, digits, _ or -, starting with a letter. Used as the vehicle ID.",
        examples=["Vehicle_03"]
    )
    color: Optional[str] = Field(None, description="Colour name or hex value.", examples=["green"])
    mode: Optional[Literal["auto", "link"]] = Field(
        None,
        description="auto: the backend creates the ThingsBoard device. link: use an existing "
                    "device. Default is link when device_id/token are sent, else auto."
    )
    device_id: Optional[str] = Field(None, description="Link mode only. ThingsBoard device ID.")
    token: Optional[str] = Field(None, description="Link mode only. ThingsBoard device access token.")


class VehicleUpdate(BaseModel):
    name: Optional[str] = Field(None, examples=["Truck_03"])
    color: Optional[str] = Field(None, description="Blank removes the colour.", examples=["red"])
    device_id: Optional[str] = Field(None, description="Linked vehicles only.")
    token: Optional[str] = Field(None, description="Linked vehicles only. Blank keeps the current token.")


class ServoCommand(BaseModel):
    angle: int = Field(90, ge=0, le=180, examples=[90])


class DeviceCommand(BaseModel):
    command: str = Field(examples=["PING"])
