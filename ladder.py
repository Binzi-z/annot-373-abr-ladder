"""按 VMAF 对齐的 ABR 码率阶梯编排。

流程（三阶段，全部产物可复现）：

  A. CRF 扫描：对同一片源跑多档 CRF（默认 18/22/26/30/34/38），
     每档在 10 个 10 秒分层抽样段上算 VMAF，得到 (实际码率 -> VMAF) 实测曲线；
  B. 二分 + 插值收敛：6 个目标 VMAF 档（目标网格相邻落差 1.6，给实测噪声
     留余量），先用实测曲线线性插值出初始码率，再在"达标 / 不达标"两侧二分，
     收敛到满足目标 VMAF 的最低码率（相对容差 8%，每档最多 4 次评估）；
     收敛后还有硬约束修复阶段，保证相邻档实测 VMAF 落差严格 < 2；
  C. 定型产出：按收敛码率出 6 档 mp4（复用已有探针，不重复编码），
     切 HLS（GOP 与 6s 分片对齐）、写 master.m3u8，并输出 report.json / report.md。

防过拟合不是一句口号，落在多处机制上（详见 report.md 与 README）：
  1. 分层 + 抖动抽样（vmaf.pick_segments），10 段覆盖全片时间轴而非固定场景；
  2. 判定指标同时看帧均值 / p10 / 最差帧 / 调和均值 / 段间标准差，单段拖后腿会被记录；
  3. 固定 VMAF 模型版本与随机种子，换 seed / 换片源即可复核曲线是否稳定。

CLI 兼容基线：``--src`` / ``--out`` 保持不变。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vmaf as vlib  # noqa: E402

FFMPEG = os.environ.get("FFMPEG", vlib.FFMPEG)

# ---- 默认参数（4 核 / 3 小时预算的标定值，见 README 预算表） ----
DEFAULT_CRFS = [18, 22, 26, 30, 34, 38]
DEFAULT_SEGMENTS = 10
DEFAULT_SEG_LEN = 10.0
DEFAULT_RUNGS = 6
DEFAULT_VMAF_GAP = 1.6          # 目标网格相邻落差，给实测噪声留余量
GAP_HARD_LIMIT = 2.0            # 需求硬约束：相邻档实测 VMAF 落差必须 < 2
DEFAULT_PRESET = "veryfast"
DEFAULT_JOBS = 4                # 4 核机器：4 个单线程任务并行
X264_THREADS = 1                # 并行度交给进程池，避免线程超额订阅
VMAF_THREADS = 1
REL_TOL = 0.08                  # 二分收敛的相对码率容差
MAX_EVAL_PER_RUNG = 4           # 每档 ABR 评估上限（含首次插值点）
MAX_EVAL_TOTAL = 26             # 全局 ABR 评估上限（预算熔断，含修复）
MIN_KBPS = 150
RUNG_SPACING = 1.2              # 相邻档码率至少拉开 20%
AUDIO_KBPS = 128
HLS_SEC = 6                     # HLS 分片时长（秒），GOP 与之对齐


# ---------------------------------------------------------------- 基础工具

def run(cmd):
    print("$", " ".join(str(c) for c in cmd))
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


def encode(src, out_path, preset, *, crf=None, video_kbps=None, gop=None):
    """单次 x264 编码。CRF 模式或 ABR（带 maxrate/bufsize 封顶）二选一。

    gop 给定时固定 GOP 长度并关闭场景切换强制关键帧，使 HLS 分片严格
    落在关键帧边界（默认 6s）。
    """
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-c:v", "libx264", "-preset", preset,
        "-threads", str(X264_THREADS),
        "-pix_fmt", "yuv420p",
    ]
    if gop:
        cmd += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
    if crf is not None:
        cmd += ["-crf", str(crf)]
    else:
        cmd += [
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{int(video_kbps * 1.5)}k",
            "-bufsize", f"{int(video_kbps * 2)}k",
        ]
    cmd += [
        "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k",
        "-movflags", "+faststart",
        out_path,
    ]
    t0 = time.time()
    p = run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"encode failed -> {out_path}\n{p.stdout[-2000:]}")
    return round(time.time() - t0, 1)


def measure_file(path, duration):
    """返回文件的总码率（按体积精确计算）与视频流声明码率。"""
    size = os.path.getsize(path)
    total_kbps = round(size * 8 / 1000 / duration, 1)
    try:
        info = vlib.probe(path)
        video_kbps = info["video_kbps"] or round(total_kbps - AUDIO_KBPS, 1)
    except RuntimeError:
        video_kbps = round(total_kbps - AUDIO_KBPS, 1)
    return {"bytes": size, "total_kbps": total_kbps, "video_kbps": video_kbps}


# ---------------------------------------------------------------- 插值曲线

class RateQualityCurve:
    """对实测 (video_kbps -> vmaf) 点做分段线性插值，越界按端点钳制。"""

    def __init__(self, points):
        pts = sorted(points, key=lambda p: p[0])
        # 强制单调不减（实测噪声可能出现微小倒挂），下限语义更安全
        mono = []
        best = -1.0
        for kbps, vmaf in pts:
            best = max(best, vmaf)
            mono.append((kbps, best))
        self.points = mono

    def vmaf_at(self, kbps):
        pts = self.points
        if kbps <= pts[0][0]:
            return pts[0][1]
        if kbps >= pts[-1][0]:
            return pts[-1][1]
        for (k0, v0), (k1, v1) in zip(pts, pts[1:]):
            if k0 <= kbps <= k1:
                if k1 == k0:
                    return v1
                return v0 + (v1 - v0) * (kbps - k0) / (k1 - k0)
        return pts[-1][1]

    def kbps_for(self, target_vmaf):
        """反查达到目标 VMAF 的码率（线性插值，钳制在实测区间内）。"""
        pts = self.points
        if target_vmaf <= pts[0][1]:
            return pts[0][0]
        if target_vmaf >= pts[-1][1]:
            return pts[-1][0]
        for (k0, v0), (k1, v1) in zip(pts, pts[1:]):
            if v0 <= target_vmaf <= v1:
                if v1 == v0:
                    return k1
                return k0 + (k1 - k0) * (target_vmaf - v0) / (v1 - v0)
        return pts[-1][0]


def round_kbps(kbps):
    return int(max(MIN_KBPS, round(kbps / 10.0) * 10))


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="VMAF-aligned ABR ladder")
    ap.add_argument("--src", required=True, help="source video")
    ap.add_argument("--out", default="out", help="output dir")
    ap.add_argument("--crfs", type=int, nargs="+", default=DEFAULT_CRFS,
                    help="阶段 A 的 CRF 扫描档")
    ap.add_argument("--segments", type=int, default=DEFAULT_SEGMENTS,
                    help="VMAF 抽样段数")
    ap.add_argument("--seg-len", type=float, default=DEFAULT_SEG_LEN,
                    help="每个抽样段长度（秒）")
    ap.add_argument("--rungs", type=int, default=DEFAULT_RUNGS,
                    help="输出档位数")
    ap.add_argument("--vmaf-gap", type=float, default=DEFAULT_VMAF_GAP,
                    help=f"目标网格的相邻 VMAF 落差（< {GAP_HARD_LIMIT}，"
                         "留余量给实测噪声；硬约束由修复阶段兜底）")
    ap.add_argument("--preset", default=DEFAULT_PRESET, help="x264 preset")
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                    help="并行任务数（4 核机器建议 4，每个任务单线程）")
    ap.add_argument("--seed", type=int, default=42, help="抽样抖动种子")
    ap.add_argument("--keep-work", action="store_true",
                    help="保留中间探针文件（默认收尾清理）")
    args = ap.parse_args()

    if args.vmaf_gap >= GAP_HARD_LIMIT:
        raise SystemExit(f"--vmaf-gap 必须 < {GAP_HARD_LIMIT}（需求硬约束）")
    if not os.path.exists(args.src):
        raise SystemExit(f"source not found: {args.src}")
    if not vlib.has_libvmaf():
        raise SystemExit("当前 ffmpeg 不带 libvmaf，无法计算 VMAF")

    os.makedirs(args.out, exist_ok=True)
    work = os.path.join(args.out, ".work")
    log_root = os.path.join(work, "logs")
    os.makedirs(work, exist_ok=True)

    wall_start = time.time()
    info = vlib.probe(args.src)
    duration, fps = info["duration_sec"], info["fps"]
    print(f"source: {args.src}  duration={duration}s  {info['width']}x"
          f"{info['height']}@{fps}fps")

    sample_segments = vlib.pick_segments(
        duration, n=args.segments, seg_len=args.seg_len, seed=args.seed
    )
    gop = int(round(fps * HLS_SEC))
    print("sample segments:", sample_segments)

    # 共享状态：评估缓存、在途事件、全局预算
    eval_lock = threading.Lock()
    eval_cache = {}        # kbps -> record
    in_flight = {}         # kbps -> Event，防止同码率被两个任务重复编码
    eval_budget = {"left": MAX_EVAL_TOTAL}
    timings = {"encode_sec": 0.0, "score_sec": 0.0}

    def evaluate(kbps, tag):
        """以指定视频码率 ABR 编码全片并在样本段上打分（带缓存与预算）。"""
        kbps = round_kbps(kbps)
        with eval_lock:
            if kbps in eval_cache:
                print(f"  [{tag}] {kbps}k -> cache hit "
                      f"VMAF {eval_cache[kbps]['stats']['vmaf_mean']}")
                return kbps, eval_cache[kbps]
            waiter = in_flight.get(kbps)
            if waiter is None:
                if eval_budget["left"] <= 0:
                    return kbps, None
                eval_budget["left"] -= 1
                done = threading.Event()
                in_flight[kbps] = done
                mp4 = os.path.join(work, f"eval_{tag}_{kbps}k.mp4")
            else:
                done = None
        if done is None:
            waiter.wait()
            with eval_lock:
                return kbps, eval_cache.get(kbps)

        log_dir = os.path.join(log_root, f"eval_{tag}_{kbps}k")
        try:
            enc_sec = encode(args.src, mp4, args.preset, video_kbps=kbps, gop=gop)
            t0 = time.time()
            stats = vlib.score_segments(
                mp4, args.src, sample_segments, fps, log_dir, VMAF_THREADS
            )
            score_sec = round(time.time() - t0, 1)
            measure = measure_file(mp4, duration)
            record = {"stats": stats, "measure": measure,
                      "encode_sec": enc_sec, "score_sec": score_sec,
                      "file": mp4, "budget_left_after": eval_budget["left"]}
            with eval_lock:
                eval_cache[kbps] = record
                timings["encode_sec"] += enc_sec
                timings["score_sec"] += score_sec
            print(f"  [{tag}] {kbps}k -> VMAF {stats['vmaf_mean']} "
                  f"(p10={stats['vmaf_p10']}, min={stats['vmaf_min']}, "
                  f"stdev={stats['seg_stdev']})")
            return kbps, record
        finally:
            # 无论成功失败都释放等待者，避免其他任务在 Event 上永久挂起
            with eval_lock:
                in_flight.pop(kbps, None)
                done.set()

    # ----------------------------------------------------- 阶段 A：CRF 扫描
    print(f"\n== Stage A: CRF sweep {args.crfs} ({args.jobs} parallel) ==")
    sweep = []

    def sweep_one(crf):
        mp4 = os.path.join(work, f"probe_crf{crf}.mp4")
        log_dir = os.path.join(log_root, f"probe_crf{crf}")
        enc_sec = encode(args.src, mp4, args.preset, crf=crf, gop=gop)
        t0 = time.time()
        stats = vlib.score_segments(
            mp4, args.src, sample_segments, fps, log_dir, VMAF_THREADS
        )
        score_sec = round(time.time() - t0, 1)
        measure = measure_file(mp4, duration)
        return {"crf": crf, "stats": stats, "measure": measure,
                "encode_sec": enc_sec, "score_sec": score_sec, "file": mp4}

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(sweep_one, c): c for c in args.crfs}
        for fut in as_completed(futs):
            rec = fut.result()
            sweep.append(rec)
            print(f"  CRF {rec['crf']}: {rec['measure']['video_kbps']}k -> "
                  f"VMAF {rec['stats']['vmaf_mean']}")
    sweep.sort(key=lambda r: r["crf"])
    timings["sweep_encode_sec"] = round(sum(r["encode_sec"] for r in sweep), 1)
    timings["sweep_score_sec"] = round(sum(r["score_sec"] for r in sweep), 1)

    curve = RateQualityCurve(
        [(r["measure"]["video_kbps"], r["stats"]["vmaf_mean"]) for r in sweep]
    )
    vmaf_lo = curve.points[0][1]
    vmaf_hi = curve.points[-1][1]

    # 目标 VMAF 网格：顶端取实测上限 -0.1，底端保证落在实测区间内，
    # 等间距 6 档且严格满足相邻落差 < 2
    top_target = round(vmaf_hi - 0.1, 2)
    bottom_floor = round(vmaf_lo + 0.2, 2)
    bottom_target = max(bottom_floor, round(top_target - args.vmaf_gap * 5, 2))
    targets = [round(top_target - (top_target - bottom_target) * i /
                     (args.rungs - 1), 2) for i in range(args.rungs)]
    print("\ntarget VMAF grid (high -> low):", targets)

    cap_kbps = round_kbps(curve.points[-1][0] * 1.25)

    # ----------------------------------------------- 阶段 B：逐档二分收敛
    print(f"\n== Stage B: binary-search minimum bitrate per rung ==")

    def converge(rung_idx, target):
        """在 (不可行下界, 可行上界) 区间内二分，找达标的最低码率。

        每次评估后维护：
          infeasible = 已测且 VMAF < target 的最高码率（区间下边界）
          feasible   = 已测且 VMAF >= target 的最低码率（区间上边界）
        下一刀取两者线性插值点（VMAF-码率近似线性，比纯中点更快收敛）；
        当可行点与下边界的相对距离 <= REL_TOL 即认定已找到最低码率。
        """
        tag = f"r{rung_idx + 1}"
        trials = []
        k = round_kbps(curve.kbps_for(target))
        infeasible = None      # (kbps, vmaf)
        feasible = None        # (kbps, vmaf)
        converged = False
        for _ in range(MAX_EVAL_PER_RUNG):
            k, rec = evaluate(k, tag)
            if rec is None:
                break  # 全局预算熔断
            v = rec["stats"]["vmaf_mean"]
            trials.append({"video_kbps": k, "vmaf_mean": v,
                           "vmaf_p10": rec["stats"]["vmaf_p10"],
                           "vmaf_min": rec["stats"]["vmaf_min"]})
            if v >= target:
                feasible = (k, v) if feasible is None else min(
                    feasible, (k, v), key=lambda p: p[0])
            else:
                infeasible = (k, v) if infeasible is None else max(
                    infeasible, (k, v), key=lambda p: p[0])

            if feasible is not None:
                k_best, _ = feasible
                if infeasible is None:
                    # 还没有不可行边界：按容差向下探一刀，确认能否更低
                    k_new = round_kbps(k_best * (1 - REL_TOL))
                else:
                    k_lo, v_lo = infeasible
                    k_hi, v_hi = feasible
                    if (k_hi - k_lo) / k_hi <= REL_TOL:
                        converged = True
                        break
                    # 区间内按 VMAF 线性插值下一刀
                    k_new = round_kbps(
                        k_lo + (target - v_lo) * (k_hi - k_lo) / (v_hi - v_lo)
                    )
            else:
                # 全部不达标：向最高尝试点上方 1.5 倍扩界
                k_new = round_kbps(max(t["video_kbps"] for t in trials) * 1.5)

            k_new = round_kbps(min(cap_kbps, max(MIN_KBPS, k_new)))
            if k_new in {t["video_kbps"] for t in trials}:
                k_new = round_kbps(k_new + 20)
                if k_new in {t["video_kbps"] for t in trials} or k_new > cap_kbps:
                    break
            k = k_new

        if feasible is not None:
            chosen_kbps, chosen_vmaf = feasible
            mode = "binary_converged" if converged else "best_measured"
        else:
            # 预算熔断或始终不达标：用曲线插值兜底并显式标记
            chosen_kbps = round_kbps(curve.kbps_for(target))
            chosen_vmaf = round(curve.vmaf_at(chosen_kbps), 3)
            mode = "interpolated_fallback"
        return {"rung": rung_idx, "name": f"1080p_{tag}",
                "target_vmaf": target, "video_kbps": chosen_kbps,
                "measured_vmaf": chosen_vmaf, "mode": mode,
                "trials": trials}

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        rungs = list(pool.map(lambda i: converge(i, targets[i]),
                              range(args.rungs)))

    # 相邻档码率间距保护（高档 / 低档 >= 1.2），自上而下修正
    print("\nrung spacing guard (x%.1f)..." % RUNG_SPACING)
    for i in range(1, len(rungs)):
        upper, cur = rungs[i - 1], rungs[i]
        limit = round_kbps(upper["video_kbps"] / RUNG_SPACING)
        if cur["video_kbps"] > limit:
            old = cur["video_kbps"]
            k, rec = evaluate(limit, f"{cur['name']}_guard")
            if rec is not None and rec["stats"]["vmaf_mean"] >= cur["target_vmaf"]:
                cur["video_kbps"] = k
                cur["measured_vmaf"] = rec["stats"]["vmaf_mean"]
                cur["spacing_adjusted_from"] = old
                cur["guard_verified"] = True
                print(f"  {cur['name']}: {old}k -> {k}k (spacing guard, "
                      f"VMAF {rec['stats']['vmaf_mean']} >= {cur['target_vmaf']})")
            else:
                # 压到间距上限会跌破 VMAF 目标：回退，优先保画质约束
                cur["guard_reverted"] = True
                print(f"  {cur['name']}: keep {old}k (guard limit {k}k would "
                      f"breach VMAF target {cur['target_vmaf']})")

    # 硬约束修复：任何相邻档实测 VMAF 落差 >= 2 都必须处理。
    # 做法：把低档码率向高档方向抬到两点中点，实测达标（落差缩小）才采纳，
    # 每对相邻档最多 2 次；预算熔断或抬了也不收敛则显式标记未解决，不隐瞒。
    print(f"\ngap repair (hard limit: adjacent drop < {GAP_HARD_LIMIT})...")
    for i in range(1, len(rungs)):
        upper, cur = rungs[i - 1], rungs[i]
        for attempt in range(2):
            drop = round(upper["measured_vmaf"] - cur["measured_vmaf"], 3)
            if drop < GAP_HARD_LIMIT:
                break
            k_new = round_kbps(
                (upper["video_kbps"] + cur["video_kbps"]) / 2
            )
            if not (cur["video_kbps"] < k_new < upper["video_kbps"]):
                cur["gap_repair_unresolved"] = True
                break
            k, rec = evaluate(k_new, f"{cur['name']}_gap{attempt}")
            if rec is None:
                cur["gap_repair_unresolved"] = True
                print(f"  {cur['name']}: drop {drop} unresolved (budget exhausted)")
                break
            new_v = rec["stats"]["vmaf_mean"]
            cur["trials"].append({
                "video_kbps": k, "vmaf_mean": new_v,
                "vmaf_p10": rec["stats"]["vmaf_p10"],
                "vmaf_min": rec["stats"]["vmaf_min"], "purpose": "gap_repair",
            })
            if new_v > cur["measured_vmaf"]:
                old = cur["video_kbps"]
                cur["video_kbps"] = k
                cur["measured_vmaf"] = new_v
                cur["gap_repaired_from"] = old
                print(f"  {cur['name']}: {old}k -> {k}k, drop {drop} -> "
                      f"{round(upper['measured_vmaf'] - new_v, 3)}")
            else:
                cur["gap_repair_unresolved"] = True
                print(f"  {cur['name']}: drop {drop} unresolved (raise did not help)")
                break

    # ------------------------------------------------- 阶段 C：定型与打包
    print("\n== Stage C: finalize renditions + HLS ==")
    hls_dir = os.path.join(args.out, "hls")
    os.makedirs(hls_dir, exist_ok=True)

    def finalize(rung):
        name = rung["name"]
        final_mp4 = os.path.join(args.out, f"{name}.mp4")
        kbps = rung["video_kbps"]
        source_mp4 = eval_cache.get(kbps, {}).get("file")
        if source_mp4 and os.path.exists(source_mp4):
            shutil.copyfile(source_mp4, final_mp4)      # 复用，不重复编码
        else:
            encode(args.src, final_mp4, args.preset, video_kbps=kbps, gop=gop)
        measure = measure_file(final_mp4, duration)

        # HLS：流拷贝切分片，不二次编码；GOP 已在编码时与分片时长对齐
        seg_pattern = os.path.join(hls_dir, f"{name}_%03d.ts")
        m3u8 = os.path.join(hls_dir, f"{name}.m3u8")
        cmd = [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", final_mp4, "-c", "copy",
            "-f", "hls", "-hls_time", str(HLS_SEC),
            "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", seg_pattern,
            m3u8,
        ]
        p = run(cmd)
        if p.returncode != 0:
            raise RuntimeError(f"HLS failed for {name}\n{p.stdout[-1500:]}")
        rung.update(measure)
        rung["file"] = final_mp4
        rung["hls"] = m3u8
        # 取该档实测分段细节（兜底档可能没有 eval 记录）
        rec = eval_cache.get(kbps)
        rung["segment_detail"] = rec["stats"]["per_segment"] if rec else None
        return rung

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        rungs = list(pool.map(finalize, rungs))

    # master.m3u8（BANDWIDTH 用含音频的总码率，单位 bps）
    master = os.path.join(args.out, "master.m3u8")
    lines = ["#EXTM3U"]
    for r in rungs:
        bw = int(r["total_kbps"] * 1000)
        rel = f"hls/{r['name']}.m3u8"
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION='
            f'{info["width"]}x{info["height"]},FRAME-RATE={fps:g}'
        )
        lines.append(rel)
    with open(master, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    # ------------------------------------------------------------- 报告
    wall = round(time.time() - wall_start, 1)
    adjacent = [round(rungs[i - 1]["measured_vmaf"] -
                      rungs[i]["measured_vmaf"], 3)
                for i in range(1, len(rungs))]
    report = {
        "source": os.path.abspath(args.src),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_info": info,
        "config": {
            "preset": args.preset,
            "jobs": args.jobs,
            "x264_threads_per_job": X264_THREADS,
            "crf_sweep": args.crfs,
            "sample_segments": args.segments,
            "sample_segment_len_sec": args.seg_len,
            "sample_seed": args.seed,
            "sample_windows": sample_segments,
            "vmaf_model": vlib.VMAF_MODEL,
            "vmaf_target_gap": args.vmaf_gap,
            "vmaf_gap_hard_limit": GAP_HARD_LIMIT,
            "rung_count": args.rungs,
            "rel_tol": REL_TOL,
            "max_eval_per_rung": MAX_EVAL_PER_RUNG,
            "max_eval_total": MAX_EVAL_TOTAL,
        },
        "crf_sweep": [
            {"crf": r["crf"], **r["measure"],
             "vmaf": r["stats"]["vmaf_mean"],
             "vmaf_p10": r["stats"]["vmaf_p10"],
             "vmaf_min": r["stats"]["vmaf_min"],
             "seg_stdev": r["stats"]["seg_stdev"],
             "seg_mean_harmonic": r["stats"]["seg_mean_harmonic"],
             "encode_sec": r["encode_sec"], "score_sec": r["score_sec"]}
            for r in sweep
        ],
        "target_vmaf_grid": targets,
        "rungs": [
            {k: v for k, v in r.items() if k != "file"} for r in rungs
        ],
        "ladder_check": {
            "adjacent_vmaf_drops": adjacent,
            "all_drops_below_hard_limit": all(d < GAP_HARD_LIMIT for d in adjacent),
            "gap_repairs": [
                {"name": r["name"],
                 "from_kbps": r.get("gap_repaired_from"),
                 "unresolved": r.get("gap_repair_unresolved", False)}
                for r in rungs if "gap_repaired_from" in r
                or r.get("gap_repair_unresolved")
            ],
            "bitrate_monotonic": all(
                rungs[i - 1]["video_kbps"] > rungs[i]["video_kbps"]
                for i in range(1, len(rungs))
            ),
        },
        "runtime": {
            "wall_sec": wall,
            "sweep_encode_sec": timings.get("sweep_encode_sec"),
            "sweep_score_sec": timings.get("sweep_score_sec"),
            "eval_encode_sec_sum": round(timings["encode_sec"], 1),
            "eval_score_sec_sum": round(timings["score_sec"], 1),
            "eval_budget_left": eval_budget["left"],
        },
        "master_playlist": master,
    }
    report_path = os.path.join(args.out, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    write_markdown(report, os.path.join(args.out, "report.md"), sweep, rungs)

    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\ndone in {wall}s")
    print(f"  renditions : {args.out}/1080p_r1..r{args.rungs}.mp4")
    print(f"  HLS        : {master} (+ {hls_dir}/*.m3u8, *.ts)")
    print(f"  reports    : {report_path}, {os.path.join(args.out, 'report.md')}")


def write_markdown(report, path, sweep, rungs):
    """人类可读打分报告：阶梯、逐档试错轨迹、防过拟合分段指标。"""
    cfg = report["config"]
    out = [
        "# VMAF 对齐 ABR 阶梯报告",
        "",
        f"- 片源：`{report['source']}`（{report['source_info']['duration_sec']}s, "
        f"{report['source_info']['width']}x{report['source_info']['height']}, "
        f"{report['source_info']['fps']} fps）",
        f"- VMAF 模型：`{cfg['vmaf_model']}`；抽样：{cfg['sample_segments']} 段 × "
        f"{cfg['sample_segment_len_sec']}s，seed={cfg['sample_seed']}",
        f"- preset={cfg['preset']}，jobs={cfg['jobs']}（每任务 {cfg['x264_threads_per_job']} 线程）",
        f"- 总耗时：{report['runtime']['wall_sec']}s",
        "",
        "## 1. CRF 扫描实测曲线",
        "",
        "| CRF | 视频码率(kbps) | 总码率(kbps) | VMAF 均值 | p10 | 最差帧 | 段间stdev |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sweep:
        out.append(
            f"| {r['crf']} | {r['measure']['video_kbps']} | "
            f"{r['measure']['total_kbps']} | {r['stats']['vmaf_mean']} | "
            f"{r['stats']['vmaf_p10']} | {r['stats']['vmaf_min']} | "
            f"{r['stats']['seg_stdev']} |"
        )

    out += [
        "",
        f"## 2. 收敛后的 {cfg['rung_count']} 档阶梯",
        "",
        "目标 VMAF 网格（高→低）：" + ", ".join(str(t) for t in report["target_vmaf_grid"]),
        "",
        "| 档位 | 目标VMAF | 实测VMAF | 视频码率(kbps) | 总码率(kbps) | 收敛方式 | 试错次数 | 备注 |",
        "|---|---:|---:|---:|---:|---|---:|---|",
    ]
    for r in rungs:
        notes = []
        if r.get("gap_repaired_from"):
            notes.append(f"落差修复 {r['gap_repaired_from']}k→{r['video_kbps']}k")
        if r.get("gap_repair_unresolved"):
            notes.append("落差未完全修复")
        if r.get("guard_reverted"):
            notes.append("间距保护回退")
        out.append(
            f"| {r['name']} | {r['target_vmaf']} | {r['measured_vmaf']} | "
            f"{r['video_kbps']} | {r['total_kbps']} | {r['mode']} | "
            f"{len(r['trials'])} | {'；'.join(notes) or '—'} |"
        )
    out += [
        "",
        f"相邻档 VMAF 落差：{report['ladder_check']['adjacent_vmaf_drops']} "
        f"（硬约束 < {cfg['vmaf_gap_hard_limit']} 全部满足：**"
        f"{report['ladder_check']['all_drops_below_hard_limit']}**）",
        "",
        "## 3. 每档二分试错轨迹",
        "",
    ]
    for r in rungs:
        out.append(f"### {r['name']}（目标 {r['target_vmaf']}，定稿 {r['video_kbps']}k）")
        out.append("")
        out.append("| 试次 | 视频码率(kbps) | VMAF | p10 | 最差帧 | 判定 |")
        out.append("|---:|---:|---:|---:|---:|---|")
        for idx, t in enumerate(r["trials"], 1):
            if t.get("purpose") == "gap_repair":
                verdict = "落差修复"
            else:
                verdict = "达标" if t["vmaf_mean"] >= r["target_vmaf"] else "不达标"
            out.append(
                f"| {idx} | {t['video_kbps']} | {t['vmaf_mean']} | "
                f"{t['vmaf_p10']} | {t['vmaf_min']} | {verdict} |"
            )
        out.append("")

    n_seg = cfg["sample_segments"]
    out += [
        f"## 4. 防过拟合：分段打分（{n_seg} 个抽样段）",
        "",
        f"每档在 {n_seg} 个分层抖动抽样段上的逐段 VMAF 均值；段间 stdev 越大说明画质越"
        "依赖场景，换片源/换 seed 应复核。",
        "",
        "| 档位 | " + " | ".join(f"段{i}" for i in range(cfg["sample_segments"])) + " |",
        "|---" + "|---:" * cfg["sample_segments"] + "|",
    ]
    for r in rungs:
        detail = r.get("segment_detail")
        if not detail:
            continue
        cells = " | ".join(f"{s['mean']}" for s in detail)
        out.append(f"| {r['name']} | {cells} |")

    out += [
        "",
        "## 5. 如何避免对单一场景过拟合",
        "",
        "1. **分层 + 抖动抽样**：时间轴等分 10 个 strata，段起点在 strata 内由固定"
        "种子伪随机抖动（`vmaf.pick_segments`），覆盖片头/片中/片尾与不同运动强度，"
        "不允许锚定在某个固定场景；换 `--seed` 可立即做稳健性复核。",
        "2. **多指标判定**：除帧均值外同时报告 p10、最差帧、段均值调和平均与段间"
        "标准差；均值达标但个别场景崩掉会在 p10/min/stdev 上暴露，而不是被平均掩盖。",
        "3. **曲线只插值不外延**：二分的初始点与兜底值都钳制在 CRF 扫描的实测区间"
        "内，避免在没有实测数据的码率区凭空外推。",
        "4. **模型与参数固化**：VMAF 模型版本（vmaf_v0.6.1）、抽样种子、preset、"
        "CRF 扫描档全部写入 report.json，跨机/跨片源结果可复现、可对比。",
        "5. **片源级隔离**：每条片源独立生成曲线与阶梯，不把上一条片子的码率结论"
        "带到下一条；上线前应在多种内容类型（对话/运动/动画/暗场）上各跑一遍并比较。",
        "",
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out))


if __name__ == "__main__":
    main()
