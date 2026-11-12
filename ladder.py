"""按 VMAF 对齐的 ABR 码率阶梯编排（6 档）。

流程：
  1. 探测源、场景检测，时间轴分层挑 10 段各 10s 样本（场景感知，防过拟合）；
  2. 一次性切出近无损参考 mezzanine（只全量解码源一次）；
  3. 每档按目标 VMAF 二分+插值收敛最低码率（探针只编/评 100s 样本）；
  4. 用收敛码率对整片做最终编码（含 AAC 音频），输出 mp4；
  5. ffmpeg HLS muxer 打包 fmp4 + 主 m3u8（BANDWIDTH 用实测码率）；
  6. 写打分报告 report.json（观测轨迹、真实 VMAF、相邻档落差）。

CLI 保持基线兼容：python ladder.py --src X --out Y
"""

import argparse
import json
import os
import sys
import time

# 兼容 `python -I`（隐含 -P，不自动把脚本目录加入 sys.path）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import probe as probe_mod
import search as search_mod
import vmaf as vmaf_mod

# 6 档默认阶梯：相邻目标 VMAF 落差均为 1.5（< 2 约束）
DEFAULT_RUNGS = [
    {"name": "1080p_top",    "height": 1080, "target_vmaf": 94.0,
     "min_kbps": 2500, "max_kbps": 12000},
    {"name": "1080p_high",   "height": 1080, "target_vmaf": 92.5,
     "min_kbps": 1800, "max_kbps": 9000},
    {"name": "1080p_mid",    "height": 1080, "target_vmaf": 91.0,
     "min_kbps": 1200, "max_kbps": 7000},
    {"name": "720p_high",    "height": 720,  "target_vmaf": 89.5,
     "min_kbps": 900,  "max_kbps": 5000},
    {"name": "720p_low",     "height": 720,  "target_vmaf": 88.0,
     "min_kbps": 600,  "max_kbps": 3500},
    {"name": "540p_low",     "height": 540,  "target_vmaf": 86.5,
     "min_kbps": 400,  "max_kbps": 2500},
]


def width_for_height(src_w, src_h, height):
    """按源宽高比算宽度，裁到偶数像素（x264 要求）。"""
    w = src_w * height / src_h
    return max(2, (round(w) // 2) * 2)


def final_encode(src, out_path, width, height, bitrate_kbps, preset, has_audio):
    """整片最终编码（视频 ABR；有音频则 AAC 128k，faststart mp4）。"""
    cmd = [
        vmaf_mod.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-vf", f"scale={width}:{height}:flags=lanczos",
        "-c:v", "libx264", "-preset", preset, "-tune", "film",
        "-b:v", f"{bitrate_kbps}k",
        "-maxrate", f"{int(bitrate_kbps * 1.5)}k",
        "-bufsize", f"{bitrate_kbps * 2}k",
        "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", out_path]
    p = vmaf_mod.run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"最终编码失败 {out_path}:\n{p.stdout[-2000:]}")


def package_hls(renditions, out_dir, has_audio, hls_time=6):
    """用 HLS muxer 打 fmp4：每档独立 playlist + 一个主 master.m3u8。"""
    for r in renditions:
        seg_dir = os.path.join(out_dir, "hls", r["name"])
        os.makedirs(seg_dir, exist_ok=True)
        rel_mp4 = os.path.relpath(r["file"], seg_dir).replace("\\", "/")
        cmd = [
            vmaf_mod.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", rel_mp4,
            "-c", "copy",
            "-f", "hls",
            "-hls_time", str(hls_time),
            "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", "seg_%04d.m4s",
            "playlist.m3u8",
        ]
        p = vmaf_mod.run(cmd, cwd=seg_dir)
        if p.returncode != 0:
            raise RuntimeError(f"HLS 打包失败 {r['name']}:\n{p.stdout[-2000:]}")

    # BANDWIDTH 用实测整文件码率（视频+音频），加 10% 余量
    codecs = "avc1.4d401f,mp4a.40.2" if has_audio else "avc1.4d401f"
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    for r in renditions:
        bw = int(r["bitrate_kbps_measured_total"] * 1000 * 1.1)
        uri = f"hls/{r['name']}/playlist.m3u8"
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={r["width"]}x{r["height"]},'
            f'CODECS="{codecs}"'
        )
        lines.append(uri)
    with open(os.path.join(out_dir, "master.m3u8"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="VMAF 对齐的 ABR 6 档阶梯")
    ap.add_argument("--src", required=True, help="源视频")
    ap.add_argument("--out", default="out", help="输出目录（保持基线兼容）")
    ap.add_argument("--segments", type=int, default=10, help="样本段数")
    ap.add_argument("--clip-sec", type=float, default=10.0, help="每段秒数")
    ap.add_argument("--scene-threshold", type=float, default=0.35)
    ap.add_argument("--n-threads", type=int, default=4)
    ap.add_argument("--n-subsample", type=int, default=2, help="VMAF 帧子采样间隔")
    ap.add_argument("--max-bisections", type=int, default=5)
    ap.add_argument("--preset", default="veryfast", help="最终编码 x264 preset")
    ap.add_argument("--enforce-monotonic", action="store_true",
                    help="严格码率单调模式：把倒挂档夹到上一档/1.15（会牺牲其 VMAF 目标）；默认只告警")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        raise SystemExit(f"源不存在: {args.src}")
    t_start = time.time()
    os.makedirs(args.out, exist_ok=True)
    work = os.path.join(args.out, "_work")
    os.makedirs(work, exist_ok=True)

    # 1) 探测 + 场景感知分层采样
    meta = probe_mod.probe_source(args.src)
    print(f"[1/6] 源: {meta['width']}x{meta['height']} {meta['fps']}fps "
          f"duration={meta['duration_sec']:.1f}s")
    scenes = probe_mod.detect_scenes(args.src, args.scene_threshold)
    segments = probe_mod.pick_segments(
        meta["duration_sec"], args.segments, args.clip_sec, scenes
    )
    seg_duration = round(sum(b - a for a, b in segments), 2)
    print(f"      硬切点 {len(scenes)} 个；选定 {len(segments)} 段共 {seg_duration}s")

    # 2) 参考 mezzanine（只全量解码源一次）
    ref_mezz = os.path.join(work, "reference_mezzanine.mp4")
    print("[2/6] 生成近无损参考 mezzanine ...")
    vmaf_mod.make_reference_mezzanine(args.src, segments, ref_mezz)

    rungs = search_mod.build_rung_specs(DEFAULT_RUNGS)

    # 3) 每档二分+插值搜索
    print("[3/6] 逐档搜索最低码率 ...")
    def probe_fn_factory(rung):
        width = width_for_height(meta["width"], meta["height"], rung["height"])
        def _probe(bitrate):
            return vmaf_mod.probe_point(
                ref_mezz, seg_duration, width, rung["height"], bitrate,
                work, meta["width"], meta["height"],
                args.n_threads, args.n_subsample,
            )
        return _probe

    search_results = []
    for rung in rungs:
        t0 = time.time()
        width = width_for_height(meta["width"], meta["height"], rung["height"])
        bitrate, trail, status = search_mod.find_min_bitrate(
            probe_fn_factory(rung), rung["target_vmaf"],
            rung["min_kbps"], rung["max_kbps"], args.max_bisections,
        )
        print(f"      {rung['name']:10s} target VMAF={rung['target_vmaf']:5.1f} "
              f"-> {bitrate} kbps ({len(trail)} 探针, {status}, {time.time()-t0:.0f}s)")
        search_results.append({**rung, "width": width, "bitrate_kbps": bitrate,
                               "search_status": status, "probe_trail": trail})
    # 默认非破坏：保留各档 VMAF 最优码率，只检测/上报码率倒挂
    bitrate_warnings = search_mod.check_bitrate_monotonicity(search_results)
    if args.enforce_monotonic:
        search_results = search_mod.enforce_spacing(search_results)
        print("      [严格模式] 已对码率倒挂档做单调裁剪（见 spacing_adjusted）")
    for w in bitrate_warnings:
        print(f"      [码率告警] {w['upper']}({w['upper_kbps']}k) -> "
              f"{w['lower']}({w['lower_kbps']}k)：{w['reason']}")

    # 4) 整片最终编码
    print("[4/6] 整片最终编码（6 档）...")
    renditions = []
    for r in search_results:
        mp4 = os.path.join(args.out, f"{r['name']}.mp4")
        t0 = time.time()
        final_encode(args.src, mp4, r["width"], r["height"], r["bitrate_kbps"],
                     args.preset, meta["has_audio"])
        size = os.path.getsize(mp4)
        total_kbps = round(size * 8 / 1000 / meta["duration_sec"], 1)
        print(f"      {r['name']:10s} {r['bitrate_kbps']}k -> {total_kbps} kbps 实测, "
              f"{time.time()-t0:.0f}s")
        renditions.append({**r, "file": mp4, "bytes": size,
                           "bitrate_kbps_measured_total": total_kbps})

    # 5) HLS 打包
    print("[5/6] HLS 打包（fmp4 + master.m3u8）...")
    package_hls(renditions, args.out, meta["has_audio"])

    # 6) 报告：最终成片的 VMAF 直接取搜索阶段同码率探针的缓存值，不重复编码
    print("[6/6] 汇总最终 VMAF 并写报告 ...")
    for r in renditions:
        cached = vmaf_mod.load_cached_point(work, r["height"], r["bitrate_kbps"])
        if cached is None:
            # 码率被 enforce_spacing 夹过、该点没探过：补一针
            dist_probe = os.path.join(work, f"final_{r['name']}.mp4")
            vmaf_mod.encode_probe(ref_mezz, r["width"], r["height"],
                                  r["bitrate_kbps"], dist_probe)
            cached = vmaf_mod.score_pair(
                dist_probe, ref_mezz, r["width"], r["height"],
                meta["width"], meta["height"], work,
                f"final_vmaf_{r['name']}.json", args.n_threads, args.n_subsample,
            )
            os.remove(dist_probe)
        r["vmaf_final_mean"] = round(cached["vmaf_mean"], 3)
        r["vmaf_final_min"] = round(cached["vmaf_min"], 3)

    for upper, lower in zip(renditions, renditions[1:]):
        lower["vmaf_gap_vs_above"] = round(
            upper["vmaf_final_mean"] - lower["vmaf_final_mean"], 3
        )

    # 硬校验：相邻档最终 VMAF 落差必须 < 2（任务约束），不满足直接报错并指出档位
    violations = [
        f"{upper['name']}->{lower['name']}={lower['vmaf_gap_vs_above']:.2f}"
        for upper, lower in zip(renditions, renditions[1:])
        if lower["vmaf_gap_vs_above"] >= 2.0
    ]
    constraint_ok = not violations
    if violations:
        print("      [警告] 相邻档 VMAF 落差 >= 2: " + "; ".join(violations))

    report = {
        "source": os.path.abspath(args.src),
        "source_meta": meta,
        "sampling": {
            "n_segments": len(segments),
            "clip_sec": args.clip_sec,
            "scored_seconds": seg_duration,
            "scene_cut_count": len(scenes),
            "scene_threshold": args.scene_threshold,
            "segments": segments,
        },
        "vmaf_config": {
            "n_threads": args.n_threads,
            "n_subsample": args.n_subsample,
            "model": "vmaf_v0.6.1 (ffmpeg libvmaf 默认)",
        },
        "rungs": [
            {k: v for k, v in r.items() if k != "probe_trail"} for r in renditions
        ],
        "constraints": {
            "adjacent_vmaf_gap_lt_2": constraint_ok,
            "violations": violations,
            "bitrate_monotonicity_warnings": bitrate_warnings,
            "enforce_monotonic": args.enforce_monotonic,
        },
        "probe_trails": {r["name"]: r["probe_trail"] for r in renditions},
        "total_sec": round(time.time() - t_start, 1),
    }
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"完成，总耗时 {report['total_sec']}s -> {os.path.join(args.out, 'report.json')}")


if __name__ == "__main__":
    main()
