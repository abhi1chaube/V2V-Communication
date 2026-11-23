# =========================================================
# PER-VEHICLE COMMAND SERIALISATION
# =========================================================
#
# Two dashboards (or a dashboard and a script) can send
# commands for the same vehicle at the same moment. Each
# command validates the vehicle's state and then changes it;
# without serialisation both could pass validation before
# either changes anything (for example two STARTs).
#
# command_lock(vehicle, ...) runs one command at a time per
# vehicle. The route validates INSIDE the lock, so the second
# command sees the state the first one left.
#
# Emergency stop, release and reset deliberately do NOT take
# these locks: a stop must never wait behind a slow command.
# The state transitions re-check the emergency latch inside the
# state lock (services/state.py), so a command that was already
# validated still refuses to move a vehicle stopped meanwhile.
# =========================================================

from contextlib import contextmanager
from threading import Lock

from services.settings import settings

from .common import ApiError


_locks = {}

_locks_guard = Lock()


def _lock_for(vehicle_id):

    with _locks_guard:

        lock = _locks.get(vehicle_id)

        if lock is None:
            lock = _locks[vehicle_id] = Lock()

        return lock


@contextmanager
def command_lock(*vehicle_ids):
    """Hold the command lock of each vehicle (deduplicated, sorted to avoid deadlock).

    Raises ApiError 409 COMMAND_IN_PROGRESS if another command
    holds a lock for longer than COMMAND_LOCK_TIMEOUT_S.
    """

    wanted = sorted({vehicle_id for vehicle_id in vehicle_ids if vehicle_id})

    acquired = []

    try:

        for vehicle_id in wanted:

            lock = _lock_for(vehicle_id)

            if not lock.acquire(timeout=settings.command_lock_timeout_s):

                raise ApiError(
                    f"Another command for {vehicle_id} is still running. Try again.",
                    409,
                    "COMMAND_IN_PROGRESS"
                )

            acquired.append(lock)

        yield

    finally:

        for lock in reversed(acquired):
            lock.release()


@contextmanager
def try_lock(vehicle_id):
    """Take the vehicle's command lock only if it is free (yields True/False).

    For the autopilot on the world engine's thread: it must never
    wait for a command (a command can itself be waiting for the
    engine, e.g. a handover negotiation).
    """

    lock = _lock_for(vehicle_id)

    acquired = lock.acquire(blocking=False)

    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def forget(vehicle_id):
    """Drop the lock of a vehicle removed from the registry."""

    with _locks_guard:
        _locks.pop(vehicle_id, None)
