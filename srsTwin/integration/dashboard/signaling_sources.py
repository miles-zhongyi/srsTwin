"""Bridge to poc_StressTest dual-source signaling dispatcher."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_POC = _HERE.parent.parent.parent / "poc_StressTest"
if _POC.is_dir() and str(_POC) not in sys.path:
    sys.path.insert(0, str(_POC))

_dispatcher = None
_init_error: str | None = None


def get_signaling_dispatcher():
    global _dispatcher, _init_error
    if _dispatcher is not None or _init_error is not None:
        return _dispatcher, _init_error
    try:
        os.environ.setdefault(
            "DIGITAL_TWIN_DIR",
            str(_HERE.parent.parent / "digital-twin"),
        )
        from common.signaling import get_dispatcher  # noqa: WPS433

        _dispatcher = get_dispatcher()
    except Exception as exc:
        _init_error = str(exc)
    return _dispatcher, _init_error


def describe_sources() -> dict:
    disp, err = get_signaling_dispatcher()
    if disp is None:
        return {"error": err, "entries": []}
    return disp.describe_sources()


def save_sources(sources: dict) -> dict:
    disp, err = get_signaling_dispatcher()
    if disp is None:
        return {"error": err}
    from common.signaling import clear_dispatcher_cache, get_dispatcher  # noqa: WPS433

    cfg = disp._sources
    for name, mode in sources.items():
        cfg.set_mode(name, str(mode))
    cfg.save()
    clear_dispatcher_cache()
    return get_dispatcher().describe_sources()
