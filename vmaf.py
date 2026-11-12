"""VMAF 打分与 ABR 探针。

三段式设计（省时间的关键）：
  1. make_reference_mezzanine：一次性把 10 段样本从源里切出来拼成一个
     ~100s 的参考 mezzanine（只全量解码源一次，CRF14 近无损）；
  2. encode_probe：对 mezzanine 整段做一次目标分辨率/码率的 ABR 编码
     （只编 100s 而不是整片，探针编码量降到 1/6）；
  3. score_pair：失真片上采样回参考分辨率，单 libvmaf 一次打完 1500 帧。

Windows 注意：libvmaf 的 log_path 里盘符冒号无法在 filtergraph 转义，
ffmpeg 的 cwd 切到工作目录、log_path 只传裸文件名（见 README / 记忆）。
"""

import json
import os
import subprocess

from probe import FFMPEG, run


def trim_concat_filter(segments, in_label="0:v", out_label="vc"):
    """split -> 每路 trim/setpts -> concat，输出 [out_label]。"""
    n = len(segments)
    parts = [f"[{in_label}]split={n}" + "".join(f"[s{i}]" for i in range(n))]
    for i, (a, b) in enumerate(segments):
        parts.append(f"[s{i}]trim=start={a}:end={b},setpts=PTS-STARTPTS[t{i}]")
    parts.append(
        "".join(f"[t{i}]" for i in range(n))
        + f"concat=n={n}:v=1:a=0[{out_label}]"
    )
    return ";".join(parts)


def has_libvmaf():
    p = run([FFMPEG, "-hide_banner", "-filters"])
    return "libvmaf" in p.stdout


def load_cached_point(work_dir, height, bitrate_kbps):
    """读回搜索阶段已探针点的指标；没探过返回 None。"""
    cache_path = os.path.join(work_dir, "probe_cache.json")
    if not os.path.exists(cache_path):
        return None
    with open(cache_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    return cache.get(f"{height}p_{int(bitrate_kbps)}kbps")


def make_reference_mezzanine(src, segments, out_path, crf=14):
    """把样本段从源切出拼成近无损参考片，全程只全量解码源这一次。"""
    if os.path.exists(out_path):
        return out_path
    fg = trim_concat_filter(segments)
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-filter_complex", fg,
        "-map", "[vc]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-an",
        out_path,
    ]
    p = run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"参考 mezzanine 生成失败:\n{p.stdout[-2000:]}")
    return out_path


def encode_probe(ref_mezz, width, height, bitrate_kbps, out_path):
    """对参考片做一次 ABR 探针编码（视频流，无音频）。"""
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", ref_mezz,
        "-vf", f"scale={width}:{height}:flags=lanczos",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "film",
        "-b:v", f"{bitrate_kbps}k",
        "-maxrate", f"{int(bitrate_kbps * 1.5)}k",
        "-bufsize", f"{bitrate_kbps * 2}k",
        "-pix_fmt", "yuv420p", "-an",
        out_path,
    ]
    p = run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"探针编码失败 {height}p/{bitrate_kbps}k:\n{p.stdout[-2000:]}")
    return out_path


def parse_vmaf_json(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    pooled = data["pooled_metrics"]["vmaf"]
    per_frame = [fr["metrics"]["vmaf"] for fr in data["frames"]]
    return {
        "vmaf_mean": pooled["mean"],
        "vmaf_min": pooled["min"],
        "vmaf_harmonic_mean": pooled["harmonic_mean"],
        "frames_scored": len(per_frame),
        "per_frame": per_frame,
    }


def score_pair(dist, ref, dist_w, dist_h, ref_w, ref_h, work_dir,
               log_name, n_threads=4, n_subsample=2):
    """失真片 vs 参考片打分；cwd=work_dir 规避 log_path 盘符问题。"""
    os.makedirs(work_dir, exist_ok=True)
    if (dist_w, dist_h) != (ref_w, ref_h):
        vf = f"[0:v]scale={ref_w}:{ref_h}:flags=lanczos[d];[d][1:v]libvmaf="
    else:
        vf = "[0:v][1:v]libvmaf="
    vf += (f"n_threads={n_threads}:n_subsample={n_subsample}"
           f":log_fmt=json:log_path={log_name}")
    rel_dist = os.path.relpath(os.path.abspath(dist), work_dir).replace("\\", "/")
    rel_ref = os.path.relpath(os.path.abspath(ref), work_dir).replace("\\", "/")
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", rel_dist, "-i", rel_ref,
        "-lavfi", vf, "-f", "null", "-",
    ]
    p = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=work_dir,
    )
    if p.returncode != 0:
        raise RuntimeError(f"libvmaf 打分失败 ({log_name}):\n{p.stdout[-2000:]}")
    return parse_vmaf_json(os.path.join(work_dir, log_name))


def probe_point(ref_mezz, segments_duration, width, height, bitrate_kbps,
                work_dir, ref_w, ref_h, n_threads=4, n_subsample=2):
    """对 (height, bitrate) 做一次完整探针，结果持久化缓存。"""
    os.makedirs(work_dir, exist_ok=True)
    cache_path = os.path.join(work_dir, "probe_cache.json")
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)

    key = f"{height}p_{int(bitrate_kbps)}kbps"
    if key in cache:
        return cache[key]

    dist_path = os.path.join(work_dir, f"probe_{key}.mp4")
    encode_probe(ref_mezz, width, height, bitrate_kbps, dist_path)
    metrics = score_pair(
        dist_path, ref_mezz, width, height, ref_w, ref_h,
        work_dir, f"vmaf_{key}.json", n_threads, n_subsample,
    )
    measured_kbps = round(os.path.getsize(dist_path) * 8 / 1000 / segments_duration, 1)
    # 探针 mp4 打完分即删，避免几十个中间片占满磁盘；指标已进缓存
    os.remove(dist_path)
    metrics.update({
        "height": height,
        "width": width,
        "bitrate_kbps_target": bitrate_kbps,
        "bitrate_kbps_measured": measured_kbps,
    })
    # per-frame 数组不进缓存，避免缓存文件无限膨胀
    cache[key] = {k: v for k, v in metrics.items() if k != "per_frame"}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    return metrics


if __name__ == "__main__":
    print("libvmaf available:", has_libvmaf())
