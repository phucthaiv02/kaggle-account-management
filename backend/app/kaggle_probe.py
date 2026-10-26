"""Thu thập danh sách notebook (kernel) và trạng thái phiên chạy của MỘT tài khoản Kaggle.

Chạy trong tiến trình con riêng (token qua biến môi trường KAGGLE_API_TOKEN):
    python kaggle_probe.py '<tùy chọn JSON>'
và in đúng một dòng "@@KAM@@" + JSON ra stdout (CLI Kaggle có thể in thêm chữ hướng dẫn,
nên bên gọi chỉ đọc dòng có tiền tố này).

Dữ liệu lấy từ Kaggle SDK (đối chiếu mã nguồn kaggle 2.2.x). Giới hạn của API công khai:
- Không có API liệt kê các "interactive session" đang mở trong trình soạn thảo; chỉ thấy
  phiên chạy gần nhất của mỗi notebook của bạn (Save & Run All, hoặc push qua API).
- Không có API trả thời điểm bắt đầu/kết thúc chính xác. Thời điểm bắt đầu lấy từ
  `last_run_time`; thời lượng của phiên đã kết thúc được ước tính từ mốc thời gian lớn nhất
  trong log của phiên (tối đa vài phiên mỗi lần làm mới để không tốn thời gian).

Module này không import `kaggle` ở mức module để import vào ứng dụng không tốn thời gian.
"""
import json
import sys
import time
from datetime import datetime

SENTINEL = "@@KAM@@"
TERMINAL = {"complete", "error", "cancel_acknowledged"}
_GPU_HINTS = ("gpu", "nvidia", "t4", "p100", "l4", "a100", "h100")


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _same_time(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return datetime.fromisoformat(a.replace("Z", "+00:00")) == datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return a == b


def classify_accelerator(enable_gpu: bool, enable_tpu: bool, machine_shape: str) -> str:
    shape = (machine_shape or "").lower()
    if enable_tpu or "tpu" in shape:
        return "tpu"
    if enable_gpu or any(h in shape for h in _GPU_HINTS):
        return "gpu"
    return "cpu"


def fetch_meta(api, ref: str, version: int):
    """Metadata của đúng một phiên bản (ApiGetKernel): chính xác hơn dòng trong danh sách, vốn có thể thiếu loại máy."""
    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest

    owner, _, slug = ref.partition("/")
    with api.build_kaggle_client() as kaggle:
        req = ApiGetKernelRequest()
        req.user_name = owner
        req.kernel_slug = f"{slug}/{version}" if version else slug
        return kaggle.kernels.kernels_api_client.get_kernel(req).metadata


def parse_duration(log_text: str) -> float | None:
    """Mốc `time` lớn nhất trong log (giây kể từ lúc phiên bắt đầu); None nếu không đọc được."""
    try:
        data = json.loads(log_text)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("log") or data.get("events") or []
    best = None
    for e in data if isinstance(data, list) else []:
        t = e.get("time") if isinstance(e, dict) else None
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            best = t if best is None else max(best, t)
    return float(best) if best is not None else None


def _ref(k) -> str:
    ref = str(k.ref or "")
    if "/" in ref:
        return ref
    return f"{k.author}/{getattr(k, 'slug', '') or ref}"


def _build_item(k):
    ref, version, lrt = _ref(k), int(k.current_version_number or 0), _iso(k.last_run_time)
    shape = k.machine_shape or ""
    it = {
        "ref": ref, "version": version, "title": k.title or ref.split("/")[-1], "author": k.author or "",
        "last_run_time": lrt, "accelerator": classify_accelerator(bool(k.enable_gpu), bool(k.enable_tpu), shape),
        "machine_shape": shape, "accelerator_checked": False, "docker_image": k.docker_image or "",
        "enable_internet": bool(k.enable_internet), "is_private": bool(k.is_private),
        "kernel_type": k.kernel_type or "", "language": k.language or "", "total_votes": int(k.total_votes or 0),
        "competitions": [str(x) for x in (k.competition_data_sources or [])],
        "datasets": [str(x) for x in (k.dataset_data_sources or [])],
        "models": [str(x) for x in (k.model_data_sources or [])],
        "kernel_sources": [str(x) for x in (k.kernel_data_sources or [])],
        "status": None, "failure_message": None, "duration_seconds": None,
    }
    return ref, version, lrt, shape, it


def collect(api, opts: dict, clock=time.monotonic, meta_fn=fetch_meta) -> dict:
    limit = max(1, min(int(opts.get("limit", 30)), 100))
    status_limit = int(opts.get("status_limit", 20))
    duration_budget = int(opts.get("duration_budget", 3))
    deadline = clock() + float(opts.get("deadline_seconds", 30))
    accel_budget = int(opts.get("accel_budget", 8))
    known = opts.get("known") or {}  # ref -> hàng mới nhất đã biết của notebook đó

    kernels = api.kernels_list(page=1, page_size=limit, mine=True, sort_by="dateRun") or []
    items, partial, bad = [], False, []
    for idx, k in enumerate(kernels):
        try:
            ref, version, lrt, shape, it = _build_item(k)
        except (Exception, SystemExit) as e:  # một notebook hỏng/đã xóa dở dang không được làm hỏng cả lần quét
            bad.append(f"{getattr(k, 'ref', '?')}: {str(e)[:120]}")
            continue
        if it["accelerator"] != "cpu":  # danh sách đã báo rõ GPU/TPU
            it["accelerator_checked"] = True
        kn = known.get(ref)
        if kn and kn.get("version") == version and kn.get("accelerator_checked"):
            it["accelerator"], it["machine_shape"] = kn.get("accelerator") or it["accelerator"], kn.get("machine_shape") or shape
            it["accelerator_checked"] = True
        if kn and kn.get("version") == version and _same_time(kn.get("last_run_time"), lrt) and kn.get("status") in TERMINAL:
            # Phiên đã kết thúc và không có lần chạy mới: không cần hỏi lại Kaggle
            it["status"], it["failure_message"] = kn["status"], kn.get("failure_message")
            it["duration_seconds"] = kn.get("duration_seconds")
        elif idx < status_limit:
            if clock() >= deadline:
                partial = True
            else:
                try:
                    st = api.kernels_status(ref)
                    it["status"] = getattr(st.status, "name", str(st.status)).lower()
                    it["failure_message"] = st.failure_message or None
                except (Exception, SystemExit) as e:  # một notebook lỗi không làm hỏng cả danh sách
                    it["status_error"] = str(e)[:200]
                    low = it["status_error"].lower()
                    if "404" in low or "not found" in low or "does not exist" in low:
                        it["status"] = "deleted"  # Kaggle không còn tìm thấy notebook này
        items.append(it)

    # Danh sách chỉ nói "không GPU/TPU" có thể do thiếu dữ liệu: hỏi metadata từng phiên bản để chắc chắn
    asked = 0
    for it in items:
        if it["accelerator_checked"]:
            continue
        if asked >= accel_budget or clock() >= deadline:
            partial = partial or asked >= accel_budget
            break
        asked += 1
        try:
            m = meta_fn(api, it["ref"], it["version"])
        except (Exception, SystemExit):
            continue
        it["machine_shape"] = m.machine_shape or it["machine_shape"]
        it["accelerator"] = classify_accelerator(bool(m.enable_gpu), bool(m.enable_tpu), it["machine_shape"])
        it["accelerator_checked"] = True
    for it in items:
        if not it["accelerator_checked"]:
            it["accelerator"] = "unknown"  # chưa xác minh được: không khẳng định là CPU

    # Notebook đã lưu là đang chạy/chờ nhưng không còn trong danh sách (đã xóa, hoặc nằm ngoài trang đầu): hỏi thẳng Kaggle
    listed_refs = {i["ref"] for i in items}
    rechecked = {}
    for ref in list(opts.get("recheck") or [])[: int(opts.get("recheck_limit", 15))]:
        if ref in listed_refs:
            continue
        if clock() >= deadline:
            partial = True
            break
        try:
            st = api.kernels_status(ref)
            rechecked[ref] = {"status": getattr(st.status, "name", str(st.status)).lower(),
                              "failure_message": st.failure_message or None}
        except (Exception, SystemExit) as e:
            low = str(e).lower()
            if "404" in low or "not found" in low or "does not exist" in low:
                rechecked[ref] = {"status": "deleted", "failure_message": None}
            else:
                rechecked[ref] = {"error": str(e)[:200]}

    tried = 0
    for it in items:  # ước tính thời lượng cho vài phiên đã kết thúc chưa có số liệu
        if tried >= duration_budget or clock() >= deadline:
            break
        if it["status"] in TERMINAL and it["duration_seconds"] is None:
            try:
                d = parse_duration(api.kernels_logs(it["ref"]))
            except (Exception, SystemExit):
                continue  # lỗi tạm thời: thử lại ở lần sau
            tried += 1
            it["duration_seconds"] = d if d is not None else -1.0  # -1 = đã thử nhưng không xác định được

    try:
        user = api.get_config_value("username")
    except Exception:
        user = None
    # Chỉ lấy trang 1: nếu số notebook trả về ít hơn giới hạn thì đó là toàn bộ danh sách hiện có trên Kaggle
    return {"user": user, "listed": len(kernels), "complete": len(kernels) < limit, "partial": partial, "items": items, "bad": bad, "rechecked": rechecked}


_SUB_STATUS = {0: "pending", 1: "complete", 2: "error"}


def _slug(c) -> str:
    """Tên ngắn của competition (dùng trong URL/API) từ ref hoặc url."""
    ref = str(getattr(c, "ref", "") or "")
    if not ref:
        ref = str(getattr(c, "url", "") or "")
    return ref.rstrip("/").rsplit("/", 1)[-1]


def collect_submissions(api, opts: dict, clock=time.monotonic) -> dict:
    """Competition tài khoản đã tham gia + submission của từng cái.

    Mặc định chỉ lấy competition còn hạn (đang chấm/đang diễn ra); only_active=False lấy cả đã kết thúc.
    """
    only_active = bool(opts.get("only_active", True))
    max_comps = max(1, min(int(opts.get("max_competitions", 20)), 50))
    per_comp = max(1, min(int(opts.get("per_competition", 50)), 100))
    deadline = clock() + float(opts.get("deadline_seconds", 30))
    now = datetime.now().astimezone()

    resp = api.competitions_list(group="entered", category="all", sort_by="latestDeadline", page_size=max_comps)
    comps_raw = list(getattr(resp, "competitions", None) or [])
    comps, subs, partial, errors = [], [], False, {}
    for c in comps_raw:
        dl = getattr(c, "deadline", None)
        active = dl is None or (dl if dl.tzinfo else dl.astimezone()) > now
        if only_active and not active:
            continue
        slug = _slug(c)
        if not slug:
            continue
        comps.append({
            "slug": slug, "title": c.title or slug, "deadline": _iso(dl), "category": c.category or "",
            "reward": c.reward or "", "team_count": int(c.team_count or 0), "user_rank": int(c.user_rank or 0) or None,
            "max_daily_submissions": int(c.max_daily_submissions or 0) or None,
            "evaluation_metric": c.evaluation_metric or "", "url": c.url or "", "active": active,
            "submissions_disabled": bool(c.submissions_disabled), "submissions_fetched": False,
        })
    for comp in comps:
        if clock() >= deadline:
            partial = True
            break
        try:
            lst = api.competition_submissions(comp["slug"], page_size=per_comp) or []
        except (Exception, SystemExit) as e:  # một competition lỗi không làm hỏng các cái còn lại
            errors[comp["slug"]] = str(e)[:200]
            continue
        comp["submissions_fetched"] = True
        for s in lst:
            st = s.status
            st = getattr(st, "name", None) and st.name.lower() or _SUB_STATUS.get(getattr(st, "value", st), "unknown")
            subs.append({
                "competition": comp["slug"], "ref": int(s.ref), "date": _iso(s.date),
                "description": s.description or "", "file_name": s.file_name or "",
                "status": st, "public_score": s.public_score or None, "private_score": s.private_score or None,
                "error_description": s.error_description or None, "team_name": s.team_name or "",
                "submitted_by": s.submitted_by or "", "url": s.url or "", "total_bytes": int(s.total_bytes or 0),
            })
    return {"competitions": comps, "submissions": subs, "listed": len(comps_raw), "partial": partial, "errors": errors}


def main(argv: list[str]) -> None:
    opts = json.loads(argv[1]) if len(argv) > 1 else {}
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi

        api = KaggleApi()
        api.authenticate()
        out = collect_submissions(api, opts) if opts.get("mode") == "submissions" else collect(api, opts)
    except SystemExit:  # authenticate() gọi exit(1) khi xác thực thất bại
        out = {"error": "Xác thực Kaggle thất bại (token sai hoặc hết hạn)"}
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    sys.stdout.write("\n" + SENTINEL + json.dumps(out) + "\n")


if __name__ == "__main__":
    main(sys.argv)
