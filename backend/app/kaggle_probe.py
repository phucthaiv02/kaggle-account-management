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


def collect(api, opts: dict, clock=time.monotonic) -> dict:
    limit = max(1, min(int(opts.get("limit", 30)), 100))
    status_limit = int(opts.get("status_limit", 20))
    duration_budget = int(opts.get("duration_budget", 3))
    deadline = clock() + float(opts.get("deadline_seconds", 30))
    known = opts.get("known") or {}  # ref -> hàng mới nhất đã biết của notebook đó

    kernels = api.kernels_list(page=1, page_size=limit, mine=True, sort_by="dateRun") or []
    items, partial = [], False
    for idx, k in enumerate(kernels):
        ref, version, lrt = _ref(k), int(k.current_version_number or 0), _iso(k.last_run_time)
        shape = k.machine_shape or ""
        it = {
            "ref": ref, "version": version, "title": k.title or ref.split("/")[-1], "author": k.author or "",
            "last_run_time": lrt, "accelerator": classify_accelerator(bool(k.enable_gpu), bool(k.enable_tpu), shape),
            "machine_shape": shape, "docker_image": k.docker_image or "",
            "enable_internet": bool(k.enable_internet), "is_private": bool(k.is_private),
            "kernel_type": k.kernel_type or "", "language": k.language or "", "total_votes": int(k.total_votes or 0),
            "competitions": [str(x) for x in (k.competition_data_sources or [])],
            "datasets": [str(x) for x in (k.dataset_data_sources or [])],
            "models": [str(x) for x in (k.model_data_sources or [])],
            "kernel_sources": [str(x) for x in (k.kernel_data_sources or [])],
            "status": None, "failure_message": None, "duration_seconds": None,
        }
        kn = known.get(ref)
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
        items.append(it)

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
    return {"user": user, "listed": len(kernels), "partial": partial, "items": items}


def main(argv: list[str]) -> None:
    opts = json.loads(argv[1]) if len(argv) > 1 else {}
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi

        api = KaggleApi()
        api.authenticate()
        out = collect(api, opts)
    except SystemExit:  # authenticate() gọi exit(1) khi xác thực thất bại
        out = {"error": "Xác thực Kaggle thất bại (token sai hoặc hết hạn)"}
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    sys.stdout.write("\n" + SENTINEL + json.dumps(out) + "\n")


if __name__ == "__main__":
    main(sys.argv)
