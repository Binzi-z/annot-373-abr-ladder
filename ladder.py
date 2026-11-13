"""按 VMAF 对齐的 ABR 码率阶梯编排。

流程（单条片源 -> 6 档 HLS 阶梯 + 打分报告）：
  1. 探测片源；全片场景切换检测 + 分层采样，抽 10 段 x10s 拼成参考 mezzanine；
  2. 每个分辨率各做一条同分辨率参考（VMAF 要求同分辨率比较）；
  3. 每档在采样上跑 CRF 探针 -> 实测 (码率, VMAF) 打点；二分补点 + 码率插值，
     收敛出满足目标 VMAF 的最低码率（探针在同分辨率档间缓存复用）；
  4. 收敛用训练窗口（前 8 段），留出窗口（后 2 段）做过拟合校验；
  5. 按插值码率对完整片源两 pass ABR 出成品 mp4，再转封装 HLS，写 master.m3u8；
  6. 对成品在全部 10 段上做最终 VMAF 验证（关子采样），校验相邻档落差 < 2，
     输出 report.json（含逐档轨迹、留出集偏差、码率倒挂告警、最差帧分）。

预算（10min/1080p、4 核，veryfast）：探针 ~18 次 x~88s + 成品 6x~7.5min
+ 验证 ~12min ≈ 2.1h，留 ~30% 余量满足 <=3h。详见 README「性能预算」。
"""

import argparse
import json
import os
import time

import media
import hls as hls_pkg
from search import converge_rung, load_cache, save_cache
from vmaf import score, split_train_holdout

# 6 档阶梯：3 个分辨率 x 6 个 VMAF 目标，相邻目标差 1.5（约束要求落差 < 2）
DEFAULT_RUNGS = [
    ("1080p_top",  1080, 94.0),
    ("1080p_high", 1080, 92.5),
    ("1080p_mid",  1080, 91.0),
    ("720p_high",   720, 89.5),
    ("720p_low",    720, 88.0),
    ("540p_low",    540, 86.5),
]


def log(msg):
    print(f"[ladder] {msg}", flush=True)


def build(args):
    t_start = time.time()
    out = args.out
    work = os.path.join(out, "_work")
    hls_root = os.path.join(out, "hls")
    os.makedirs(work, exist_ok=True)

    info = media.media_info(args.src)
    dur, src_w, src_h, fps = info["duration_sec"], info["width"], info["height"], info["fps"]
    log(f"source {args.src}  {src_w}x{src_h}@{fps}fps  {dur}s  audio={info['has_audio']}")

    # ---- 1) 场景切换 + 分层采样 -------------------------------------------------
    cuts = media.detect_scene_cuts(args.src, args.scene_threshold) if not args.no_scene else []
    segments = media.select_segments(dur, args.segments, args.clip_sec,
                                     scene_cuts=cuts, seed=args.seed)
    sample_sec = round(sum(b - a for a, b in segments), 3)
    log(f"scene cuts={len(cuts)}; {len(segments)} segments x {args.clip_sec}s "
        f"= {sample_sec}s scored content; holdout=last {args.holdout}")

    # ---- 2) 每个分辨率一条参考 mezzanine（只做一次重编码） ---------------------
    heights = sorted({h for _, h, _ in DEFAULT_RUNGS}, reverse=True)
    mezz = {}
    for h in heights:
        w = round(src_w * h / src_h)
        w -= w % 2
        path = os.path.join(work, f"reference_{h}p.mp4")
        if not os.path.exists(path):
            media.extract_mezzanine(args.src, segments, path, size=f"{w}x{h}")
        mezz[h] = (path, w)
        log(f"mezzanine {h}p -> {path}")

    # ---- 3) 逐档 CRF 探针 + 二分 + 插值收敛 ------------------------------------
    cache = load_cache(work)
    rungs_out, trails_out = [], {}
    for name, height, target_vmaf in DEFAULT_RUNGS:
        t0 = time.time()
        mezz_path, width = mezz[height]
        result, trail = converge_rung(
            target_vmaf, height, width, mezz_path, work, sample_sec,
            args.segments, args.holdout, cache,
            max_bisect=args.max_bisect, threads=args.threads,
            n_subsample=args.subsample, safety_margin_pct=args.safety_margin,
        )
        save_cache(work, cache)
        trails_out[name] = trail
        v = result["verify_point"]
        log(f"{name}: target_vmaf={target_vmaf} -> {result['target_kbps']}kbps "
            f"(crf~{result['crf_selected']}, bracket={result['bracket_crf']}, "
            f"train={v['vmaf_train']}, holdout={v['vmaf_holdout']}, "
            f"{round(time.time()-t0,1)}s)")
        rungs_out.append({
            "name": name, "height": height, "width": width,
            "target_vmaf": target_vmaf,
            "bitrate_kbps": result["target_kbps"],
            "interpolated_kbps": result["interpolated_kbps"],
            "crf_selected": result["crf_selected"],
            "bracket_crf": result["bracket_crf"],
            "search_status": result["status"],
            "probe_vmaf_train": v["vmaf_train"],
            "probe_vmaf_holdout": v["vmaf_holdout"],
        })

    # ---- 4) 成品两 pass ABR + HLS 打包 -----------------------------------------
    master_entries = []
    for rung in rungs_out:
        name, h, w = rung["name"], rung["height"], rung["width"]
        kbps = rung["bitrate_kbps"]
        t0 = time.time()
        mp4 = os.path.join(out, f"{name}.mp4")
        if not args.skip_final:
            hls_pkg.two_pass_encode(args.src, mp4, w, h, kbps,
                                    audio=info["has_audio"], threads=args.threads)
        rung["file"] = mp4
        rung["bytes"] = os.path.getsize(mp4)
        rung["bitrate_kbps_measured_total"] = round(rung["bytes"] * 8 / dur / 1000, 1)
        rung["encode_sec"] = round(time.time() - t0, 1)

        rend_dir = os.path.join(hls_root, name)
        if not args.skip_hls:
            hls_pkg.package_hls(mp4, rend_dir, name, kbps,
                                audio=info["has_audio"], seg_sec=args.hls_time)
        rel_playlist = os.path.relpath(os.path.join(rend_dir, "playlist.m3u8"), out)
        master_entries.append((name, w, h, kbps, info["has_audio"], rel_playlist))
        log(f"{name}: final {rung['bitrate_kbps_measured_total']}kbps "
            f"(target {kbps}), HLS -> {rel_playlist}, {rung['encode_sec']}s")

    if not args.skip_hls:
        hls_pkg.write_master(master_entries, os.path.join(out, "master.m3u8"),
                             audio_kbps=128 if info["has_audio"] else 0)

    # ---- 5) 成品最终 VMAF 验证（全 10 段、关子采样） ---------------------------
    warnings_list = []
    prev = None
    for rung in rungs_out:
        name, h = rung["name"], rung["height"]
        mezz_path, _ = mezz[h]
        if args.skip_verify:
            break
        # 对成品重新抽同样的窗口（保证与参考帧对齐）；强制重抽，不吃旧成品的缓存
        dist_mezz = os.path.join(work, f"final_{name}.mp4")
        media.extract_mezzanine(rung["file"], segments, dist_mezz,
                                size=f"{rung['width']}x{h}", force=True)
        # 失真文件已重建，旧 libvmaf 日志按 tag 复用会读到旧分，先作废
        stale_log = os.path.join(work, f"vmaf_final_{name}.json")
        if os.path.exists(stale_log):
            os.remove(stale_log)
        r = score(dist_mezz, mezz_path, work, f"final_{name}",
                  n_threads=args.threads, n_subsample=1)
        train_m, hold_m = split_train_holdout(r, args.segments, args.holdout)
        rung["vmaf_final_mean"] = round(r["mean"], 3)
        rung["vmaf_final_min"] = round(r["min"], 3)
        rung["vmaf_final_holdout"] = round(hold_m, 3) if hold_m is not None else None
        gap_overfit = round(abs(r["mean"] - hold_m), 3) if hold_m is not None else None
        rung["holdout_gap"] = gap_overfit
        if gap_overfit is not None and gap_overfit > args.overfit_warn:
            warnings_list.append({
                "type": "holdout_overfit", "rung": name,
                "detail": f"训练窗口与留出窗口 VMAF 差 {gap_overfit} > {args.overfit_warn}，"
                          f"采样可能偏向某些场景，建议增加场景多样性重跑",
            })
        if prev is not None:
            gap = round(prev["vmaf_final_mean"] - r["mean"], 3)
            rung["vmaf_gap_vs_above"] = gap
            if gap >= 2.0:
                warnings_list.append({
                    "type": "adjacent_gap_violation", "rung": name,
                    "detail": f"与上一档 VMAF 落差 {gap} >= 2，约束未满足",
                })
            if gap <= 0.5:
                warnings_list.append({
                    "type": "quality_plateau", "rung": name,
                    "upper_rung": prev["name"], "gap": gap,
                    "detail": "该档与上一档 VMAF 几乎无差异（落差 <= 0.5）：此分辨率/目标"
                              "对此内容已进入质量平台，该档可能是无效档，实际部署可合并；"
                              "常见于全帧噪声/合成源（降分辨率消不掉噪声）",
                })
        # 目标够不到（最差 CRF 仍超标）时单独提示
        if rung["search_status"] == "target_exceeded_at_worst_crf":
            warnings_list.append({
                "type": "target_unreachable", "rung": name,
                "target_vmaf": rung["target_vmaf"],
                "final_vmaf": rung["vmaf_final_mean"],
                "detail": "最粗 CRF(32) 的 VMAF 仍高于目标：该档无法进一步降质，"
                          "已按最低码率交付，建议用更难压的真实片源复核或合并该档",
            })
        prev = rung

    # ---- 6) 码率倒挂检测（只告警，不静默裁剪，见项目记忆中的病理源结论） -------
    for upper, lower in zip(rungs_out, rungs_out[1:]):
        if lower["bitrate_kbps"] > upper["bitrate_kbps"]:
            warnings_list.append({
                "type": "bitrate_inversion", "upper": upper["name"], "lower": lower["name"],
                "upper_kbps": upper["bitrate_kbps"], "lower_kbps": lower["bitrate_kbps"],
                "detail": "低分辨率档码率反超高档：该分辨率对此内容低效（常见于全帧噪声/"
                          "合成源），保留实测结果并告警，不人为裁剪 VMAF 目标",
            })

    report = {
        "source": os.path.abspath(args.src),
        "source_meta": info,
        "sampling": {
            "n_segments": args.segments, "clip_sec": args.clip_sec,
            "scored_seconds": sample_sec,
            "scene_cut_count": len(cuts), "scene_threshold": args.scene_threshold,
            "seed": args.seed, "holdout_segments": args.holdout,
            "segments": segments,
        },
        "vmaf_config": {
            "engine": "ffmpeg libvmaf (vmaf_v0.6.1 default model)",
            "probe_n_subsample": args.subsample, "verify_n_subsample": 1,
            "n_threads": args.threads,
            "compare_rule": "同分辨率比较：低分辨率档的参考与失真都 scale 到该档分辨率",
        },
        "rate_control": {
            "probe": "CRF 恒定质量打点（veryfast，仅采样段）",
            "final": "两 pass ABR（veryfast，全片，maxrate=1.1x / bufsize=2x）",
            "bitrate_safety_margin_pct": args.safety_margin,
            "rounding": "插值码率上浮余量后按 25/50/100kbps 刻度向上取整",
        },
        "rungs": rungs_out,
        "constraints": {
            "adjacent_vmaf_gap_target": 1.5,
            "adjacent_vmaf_gap_limit_lt": 2.0,
            "violations": [w for w in warnings_list if w["type"] == "adjacent_gap_violation"],
            "warnings": warnings_list,
        },
        "probe_trails": trails_out,
        "total_sec": round(time.time() - t_start, 1),
    }
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log(f"done in {report['total_sec']}s -> {os.path.join(out, 'report.json')}")
    if warnings_list:
        log(f"{len(warnings_list)} warning(s) in report.constraints.warnings")
    return report


def main():
    ap = argparse.ArgumentParser(description="VMAF-aligned ABR ladder orchestration")
    ap.add_argument("--src", required=True, help="source video")
    ap.add_argument("--out", default="out", help="output dir (CLI 兼容基线)")
    ap.add_argument("--segments", type=int, default=10, help="采样段数")
    ap.add_argument("--clip-sec", type=float, default=10.0, help="每段秒数")
    ap.add_argument("--holdout", type=int, default=2, help="留出验证段数（尾部 N 段）")
    ap.add_argument("--max-bisect", type=int, default=2, help="每档 CRF 二分补点上限")
    ap.add_argument("--subsample", type=int, default=2, help="探针 VMAF 帧子采样（1=全帧）")
    ap.add_argument("--threads", type=int, default=4, help="ffmpeg/libvmaf 线程数")
    ap.add_argument("--scene-threshold", type=float, default=0.35)
    ap.add_argument("--seed", type=int, default=373)
    ap.add_argument("--overfit-warn", type=float, default=2.0,
                    help="训练/留出 VMAF 偏差超过该值则告警")
    ap.add_argument("--safety-margin", type=float, default=0.05,
                    help="插值码率上浮比例，吸收探针 CRF→两pass ABR 的码控漂移")
    ap.add_argument("--hls-time", type=int, default=6)
    ap.add_argument("--no-scene", action="store_true", help="跳过场景检测（纯分层随机）")
    ap.add_argument("--skip-final", action="store_true", help="只跑到探针收敛")
    ap.add_argument("--skip-hls", action="store_true")
    ap.add_argument("--skip-verify", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        raise SystemExit(f"source not found: {args.src}")
    build(args)


if __name__ == "__main__":
    main()
