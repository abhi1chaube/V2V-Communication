# =========================================================
# SEEDED SIMULATION RANDOMNESS
# =========================================================
#
# Every random choice in the simulation (packet loss, latency
# jitter, scenario events, ...) draws from a named stream
# derived from one seed (setting SIM_SEED). The same seed gives
# the same run; separate streams keep one feature's random
# draws from shifting another's.
#
#   rng = simrng.stream("v2v.loss")
#   if rng.random() < loss_rate: ...
# =========================================================

import hashlib
import random
from threading import Lock

from .settings import settings


_streams = {}

_lock = Lock()


def _seed_for(name, seed):

    digest = hashlib.sha256(f"{seed}:{name}".encode()).digest()

    return int.from_bytes(digest[:8], "big")


def stream(name):
    """The random.Random for `name` (created on first use from SIM_SEED)."""

    with _lock:

        rng = _streams.get(name)

        if rng is None:
            rng = _streams[name] = random.Random(_seed_for(name, settings.sim_seed))

        return rng


def reset(seed=None):
    """Forget all streams; they restart from `seed` (default SIM_SEED)."""

    with _lock:

        _streams.clear()

        if seed is not None:
            settings.sim_seed = int(seed)
