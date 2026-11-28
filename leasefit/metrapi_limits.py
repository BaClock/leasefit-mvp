"""Coordinate one API key across web and worker containers on the same host."""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from time import sleep, time

from django.conf import settings


class QuotaExceeded(Exception):
    pass


@contextmanager
def locked_file(name, *, blocking=True):
    directory = settings.METRAPI_STATE_DIRECTORY
    if not directory:
        yield None
        return
    import fcntl

    key_hash = sha256(settings.METRAPI_API_KEY.encode()).hexdigest()
    with (Path(directory) / f"{key_hash}.{name}").open("a+", encoding="utf-8") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield file


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


def read_state(file):
    path = Path(file.name).with_suffix(".json")
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(file, state):
    path = Path(file.name).with_suffix(".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)


def reserve_request():
    while True:
        with locked_file("requests") as file:
            if file is None:
                return
            state = read_state(file)
            day = utc_day()
            used = state.get("used", 0) if state.get("day") == day else 0
            if used >= settings.METRAPI_DAILY_LIMIT:
                raise QuotaExceeded
            now = time()
            delay = state.get("next", 0) - now
            if delay <= 0:
                write_state(file, {"day": day, "used": used + 1, "next": now + 2.1})
                return
        sleep(delay)


def pause_for_quota():
    with locked_file("requests") as file:
        if file is not None:
            write_state(file, {"day": utc_day(), "used": settings.METRAPI_DAILY_LIMIT})


def quota_paused():
    with locked_file("requests") as file:
        state = read_state(file) if file is not None else {}
        return state.get("day") == utc_day() and state.get("used", 0) >= settings.METRAPI_DAILY_LIMIT
