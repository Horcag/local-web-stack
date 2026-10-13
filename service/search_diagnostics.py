"""Separate reported engine evidence from engines merely requested by a caller."""

from collections import Counter


def engine_status(requested: str, payload: dict, accepted: list[dict]) -> list[dict]:
    raw: Counter[str] = Counter()
    for item in payload.get("results", []):
        if isinstance(item, dict):
            engines = item.get("engines") or [item.get("engine", "")]
            if isinstance(engines, str):
                engines = [engines]
            raw.update(set(str(e) for e in engines if e))
    kept: Counter[str] = Counter()
    for item in accepted:
        kept.update(set(item.get("engines") or [item.get("engine", "")]))
    errors = {}
    for error in payload.get("unresponsive_engines", []):
        if isinstance(error, (list, tuple)) and len(error) >= 2:
            errors[str(error[0])] = str(error[1])[:300]
    selected = list(dict.fromkeys(e.strip() for e in requested.split(",") if e.strip()))
    receipts = []
    for engine in dict.fromkeys([*selected, *raw, *errors]):
        error = errors.get(engine, "")
        suspended = "suspend" in error.lower() or "disabled" in error.lower()
        if raw[engine]:
            state = "returned" if kept[engine] else "filtered"
        elif error:
            state = "suspended" if suspended else "error"
        else:
            state = "silent_empty"
        receipts.append(
            {
                "engine": engine,
                "requested": engine in selected,
                "attempted": True if raw[engine] else False if suspended else None,
                "reported": bool(raw[engine] or error),
                "raw_count": raw[engine],
                "accepted_count": kept[engine],
                "status": state,
                "error": error or None,
            }
        )
    return receipts
