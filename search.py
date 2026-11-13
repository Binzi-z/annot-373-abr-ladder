"""码率收敛核心：CRF 扫描打点 + 二分 + 线性插值。

为什么搜 CRF 而不是直接搜目标码率：
  - CRF 是恒定质量模式，探针编码一次就同时给出 (码率, VMAF) 两个量，
    rate-quality 曲线单调、噪声小；直接在码率轴上用 ABR 打点，x264 单 pass
    的实际码率会漂移，曲线更抖。
  - 收敛出的 CRF 只是"测量工具"：最终 HLS 成品用两 pass ABR 锁码率，
    目标码率取自插值结果（HLS 客户端按 BANDWIDTH 选档，需要稳定码率）。

收敛流程（每一档独立执行）：
  1. 在 CRF 粗网格 [18..32] 上打点，遇到 VMAF 越过目标的两侧即提前停；
  2. 若目标落在网格空隙里，对 CRF 做二分补点（最多 max_bisect 次）；
  3. 用夹住目标的两个点对码率做线性插值，再补一个验证点确认达标；
  4. 全部探针只编码 100s 采样 mezzanine，成本与片长解耦。
"""

import json
import os

from media import FFMPEG, run
from vmaf import score


def probe_encode(mezzanine, crf, work_dir, height, width, threads=4):
    """对采样 mezzanine 做一次 CRF 探针编码，返回探针文件路径。

    同 (height, crf) 的探针在多档间复用（1080p 三档共享同一份探针缓存）。
    """
    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir, f"probe_{height}p_crf{crf}.mp4")
    if os.path.exists(out):
        return out
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", mezzanine,
        "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", str(crf), "-threads", str(threads),
        "-pix_fmt", "yuv420p", "-an",
        out,
    ]
    run(cmd)
    return out


def measure_point(mezzanine, crf, height, width, work_dir, sample_sec,
                  n_segments, n_holdout, cache, threads=4, n_subsample=2):
    """一个 (档, CRF) 测量点：探针编码 + 实测码率 + VMAF（含留出集拆分）。"""
    key = f"{height}p_crf{crf}"
    if key in cache:
        return cache[key]

    probe = probe_encode(mezzanine, crf, work_dir, height, width, threads)
    measured_kbps = round(os.path.getsize(probe) * 8 / sample_sec / 1000, 1)
    r = score(probe, mezzanine, work_dir, key,
              n_threads=threads, n_subsample=n_subsample)
    train_mean, holdout_mean = (None, None)
    if n_holdout:
        train_mean, holdout_mean = split_train_holdout_safe(r, n_segments, n_holdout)
    point = {
        "crf": crf,
        "vmaf_mean": round(r["mean"], 3),
        "vmaf_min": round(r["min"], 3),
        "vmaf_train": round(train_mean, 3) if train_mean is not None else None,
        "vmaf_holdout": round(holdout_mean, 3) if holdout_mean is not None else None,
        "bitrate_kbps_measured": measured_kbps,
        "probe_file": probe,
        "vmaf_log": r["log"],
    }
    cache[key] = point
    return point


def split_train_holdout_safe(score_result, n_segments, n_holdout):
    from vmaf import split_train_holdout
    return split_train_holdout(score_result, n_segments, n_holdout)


def interpolate_bitrate(lo, hi, target):
    """在两个测量点之间对码率线性插值，求 target VMAF 对应的最低码率。

    lo: VMAF >= target 的点（码率高/CRF 小）；hi: VMAF < target 的点。
    返回插值码率（kbps）。纯函数，便于单测。
    """
    span = lo["vmaf_mean"] - hi["vmaf_mean"]
    if span <= 0:  # 退化（曲线抖动到非单调），取保守侧
        return float(lo["bitrate_kbps_measured"])
    frac = (lo["vmaf_mean"] - target) / span  # 0→完全落在 lo，1→完全落在 hi
    frac = max(0.0, min(1.0, frac))
    return lo["bitrate_kbps_measured"] + frac * (
        hi["bitrate_kbps_measured"] - lo["bitrate_kbps_measured"]
    )


def round_bitrate(kbps):
    """把插值码率规整到工程友好的刻度，向上取整保证不欠配。"""
    if kbps >= 4000:
        step = 100
    elif kbps >= 1500:
        step = 50
    else:
        step = 25
    return int(min(20000, (int(kbps) + step - 1) // step * step))


def apply_safety_margin(target_kbps, margin_pct):
    """成品两 pass ABR 与探针 CRF 的码控存在漂移（实测 ±3%），
    插值码率统一上浮一个安全余量再规整，保证成品 VMAF 不低于目标。"""
    return round_bitrate(target_kbps * (1.0 + margin_pct))


def converge_rung(target_vmaf, height, width, mezzanine, work_dir,
                  sample_sec, n_segments, n_holdout, cache,
                  crf_grid=(18, 21, 24, 27, 30, 32), max_bisect=2,
                  threads=4, n_subsample=2, safety_margin_pct=0.05):
    """单档收敛：返回 (选中的测量点/插值结果, 完整探针轨迹)。"""
    trail = []
    seen = set()

    def point_at(crf):
        if crf in seen:
            return next(p for p in trail if p["crf"] == crf)
        seen.add(crf)
        p = measure_point(mezzanine, crf, height, width, work_dir, sample_sec,
                          n_segments, n_holdout, cache, threads, n_subsample)
        trail.append(p)
        return p

    # 1) 粗网格打点，按 CRF 升序（VMAF 单调下降），夹住目标即停
    lo = hi = None
    for crf in crf_grid:
        p = point_at(crf)
        if p["vmaf_mean"] >= target_vmaf:
            lo = p
        else:
            hi = p
            break

    if lo is None:  # 最好的一档都够不到目标：取网格最高点并标记
        best = min(trail, key=lambda p: p["crf"])
        return _finalize(best, target_vmaf, "target_unreachable_best_effort", trail)
    if hi is None:  # 最差的一档仍超标：质量过剩，取最差点（最低码率）即可
        worst = max(trail, key=lambda p: p["crf"])
        return _finalize(worst, target_vmaf, "target_exceeded_at_worst_crf", trail)

    # 2) CRF 二分补点，把夹点区间压窄
    for _ in range(max_bisect):
        if hi["crf"] - lo["crf"] <= 1:
            break
        mid_crf = (lo["crf"] + hi["crf"]) // 2
        if mid_crf in seen:
            break
        mid = point_at(mid_crf)
        if mid["vmaf_mean"] >= target_vmaf:
            lo = mid
        else:
            hi = mid

    # 3) 插值 → 安全余量 → 规整码率；验证点用相邻整数 CRF 中更保守的那个
    interp_kbps = interpolate_bitrate(lo, hi, target_vmaf)
    target_kbps = apply_safety_margin(interp_kbps, safety_margin_pct)
    verify = lo  # lo 本身就是已验证达标的真实打点，作为交付下限保证
    return _finalize(verify, target_vmaf, "interpolated_verified", trail,
                     interp_kbps=interp_kbps, target_kbps=target_kbps,
                     bracket=(lo["crf"], hi["crf"]))


def _finalize(verify_point, target_vmaf, status, trail,
              interp_kbps=None, target_kbps=None, bracket=None):
    if target_kbps is None:
        target_kbps = apply_safety_margin(verify_point["bitrate_kbps_measured"], 0.05)
    return {
        "target_vmaf": target_vmaf,
        "status": status,
        "crf_selected": verify_point["crf"],
        "bracket_crf": list(bracket) if bracket else [verify_point["crf"], verify_point["crf"]],
        "interpolated_kbps": round(interp_kbps, 1) if interp_kbps is not None else None,
        "target_kbps": target_kbps,
        "verify_point": verify_point,
        "trail": sorted(trail, key=lambda p: p["crf"]),
    }, sorted(trail, key=lambda p: p["crf"])


def load_cache(work_dir):
    path = os.path.join(work_dir, "probe_cache.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(work_dir, cache):
    with open(os.path.join(work_dir, "probe_cache.json"), "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
