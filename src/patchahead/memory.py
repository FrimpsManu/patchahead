"""Optional memory of past migrations (Redis-backed).

If ``REDIS_URL`` is set and ``redis`` is installed, PatchAhead records each
breaking-change pattern it has migrated and can retrieve similar prior
migrations by ``change_type``. Without Redis it degrades to an in-process
seed list so the "have we seen this before?" surface still demos.
"""

import json
import os

_URL = os.environ.get("REDIS_URL", "").strip()

_SEED = [
    {"change_type": "pagination", "title": "Stripe list pagination -> cursor", "outcome": "migrated"},
    {"change_type": "field_rename", "title": "user.name -> user.full_name", "outcome": "migrated"},
]


def _client():
    if not _URL:
        return None
    try:
        import redis

        return redis.from_url(_URL, decode_responses=True)
    except Exception:
        return None


def remember(breaking_change) -> None:
    r = _client()
    if r is None:
        return
    try:
        r.lpush(
            f"patchahead:migrations:{breaking_change.change_type}",
            json.dumps({"title": breaking_change.title, "outcome": "migrated"}),
        )
    except Exception:
        pass


def similar(breaking_change, limit: int = 3):
    r = _client()
    if r is None:
        return [s for s in _SEED if s["change_type"] == breaking_change.change_type][:limit]
    try:
        raw = r.lrange(f"patchahead:migrations:{breaking_change.change_type}", 0, limit - 1)
        return [json.loads(x) for x in raw]
    except Exception:
        return []
