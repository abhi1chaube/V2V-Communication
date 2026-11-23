# =========================================================
# TIMESTAMPS
# =========================================================
#
# Every timestamp the backend stores or sends is UTC in
# ISO 8601 with a "Z" suffix, for example
#   2026-11-16T06:08:41Z        (seconds)
#   2026-11-16T06:08:41.123Z    (milliseconds)
#
# The same format sorts correctly as plain text, and browsers
# parse it directly. Convert to local time only for display.
# =========================================================

from datetime import datetime, timezone


def utc_now():
    """The current time as an aware UTC datetime."""

    return datetime.now(timezone.utc)


def utc_iso(moment=None, milliseconds=False):
    """UTC ISO 8601 text with a Z suffix."""

    moment = (moment or utc_now()).astimezone(timezone.utc)

    return moment.isoformat(
        timespec="milliseconds" if milliseconds else "seconds"
    ).replace("+00:00", "Z")
