"""Gộp kết quả thu thập session của Kaggle vào dữ liệu đã lưu (logic thuần, không gọi mạng)."""
from . import kaggle_probe

TERMINAL = kaggle_probe.TERMINAL
ACTIVE = {"queued", "running"}

# Mọi hàng gửi lên PostgREST phải có đúng tập khóa này (xem db.upsert)
ROW_KEYS = (
    "account_id", "ref", "version", "title", "author", "status", "failure_message", "last_run_time",
    "duration_seconds", "accelerator", "machine_shape", "docker_image", "enable_internet", "is_private",
    "kernel_type", "language", "total_votes", "competitions", "datasets", "models", "kernel_sources",
    "first_seen_at", "updated_at", "status_changed_at",
)


def probe_opts(known_rows: list[dict], limit: int, status_limit: int, duration_budget: int, deadline: float) -> dict:
    """Tùy chọn gửi cho kaggle_probe, kèm hàng mới nhất đã biết của từng notebook để khỏi hỏi lại."""
    latest: dict[str, dict] = {}
    for r in known_rows:
        cur = latest.get(r["ref"])
        if cur is None or r["version"] > cur["version"]:
            latest[r["ref"]] = r
    known = {
        ref: {k: r.get(k) for k in ("version", "last_run_time", "status", "failure_message", "duration_seconds")}
        for ref, r in latest.items()
    }
    return {
        "limit": limit, "status_limit": status_limit, "duration_budget": duration_budget,
        "deadline_seconds": deadline, "known": known,
    }


def _row(account_id: int, it: dict, p: dict | None, now: str) -> dict:
    same_run = bool(p) and kaggle_probe._same_time(p.get("last_run_time"), it.get("last_run_time"))
    status = it.get("status") or (p["status"] if same_run else "unknown")
    got_status = it.get("status") is not None
    failure = it.get("failure_message") if got_status else (p.get("failure_message") if same_run else None)
    dur = it.get("duration_seconds")
    if dur is None and same_run:
        dur = p.get("duration_seconds")
    changed = p.get("status_changed_at") if same_run and p["status"] == status else now
    return {
        "account_id": account_id, "ref": it["ref"], "version": it["version"], "title": it["title"],
        "author": it["author"], "status": status, "failure_message": failure,
        "last_run_time": it["last_run_time"], "duration_seconds": dur, "accelerator": it["accelerator"],
        "machine_shape": it["machine_shape"], "docker_image": it["docker_image"],
        "enable_internet": it["enable_internet"], "is_private": it["is_private"],
        "kernel_type": it["kernel_type"], "language": it["language"], "total_votes": it["total_votes"],
        "competitions": it["competitions"], "datasets": it["datasets"], "models": it["models"],
        "kernel_sources": it["kernel_sources"],
        "first_seen_at": (p.get("first_seen_at") if p else None) or now,
        "updated_at": now, "status_changed_at": changed or now,
    }


def merge(account_id: int, items: list[dict], known_rows: list[dict], now: str) -> list[dict]:
    """Trả về các hàng cần upsert: hàng mới/đổi của các notebook vừa liệt kê, cộng thêm các phiên bản cũ
    đang ở trạng thái queued/running nhưng đã có phiên bản mới hơn (đã kết thúc, trạng thái cuối không rõ)."""
    prev = {(r["ref"], r["version"]): r for r in known_rows}
    out: dict[tuple, dict] = {}
    newest: dict[str, int] = {}
    for it in items:
        key = (it["ref"], it["version"])
        out[key] = _row(account_id, it, prev.get(key), now)
        newest[it["ref"]] = max(newest.get(it["ref"], -1), it["version"])
    for r in known_rows:
        key = (r["ref"], r["version"])
        if key in out or r["ref"] not in newest or r["version"] >= newest[r["ref"]] or r["status"] not in ACTIVE:
            continue
        stale = {k: r.get(k) for k in ROW_KEYS}
        stale.update(status="unknown", updated_at=now, status_changed_at=now)
        out[key] = stale
    assert all(set(v) == set(ROW_KEYS) for v in out.values())
    return list(out.values())
