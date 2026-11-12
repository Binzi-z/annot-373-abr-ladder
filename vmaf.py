"""VMAF 打分与片源探针（基于 ffmpeg 内置 libvmaf 滤镜）。

设计要点：
  - 不依赖 ffprobe：分辨率 / 时长 / fps 全部从 ``ffmpeg -i`` 的 stderr 解析；
  - 打分以"样本段"为单位：多个样本段在**单个 ffmpeg 进程**内 split→trim→concat
    后挂一个 libvmaf 实例，逐段返回 mean/min，调用方据此做多场景统计，
    而不是只看全片一个均值（避免单一场景过拟合）；
  - libvmaf 模型版本固定为内置默认 vmaf_v0.6.1，并在报告里记录，保证可复现；
  - 打分前用 ``fps`` 滤镜把参考/失真两路统一成 CFR，帧数可预测、两路严格对齐。

libvmaf 基本用法（本机 ffmpeg 9.0.2 自带）：
    ffmpeg -i dist.mp4 -i ref.mp4 \
      -lavfi "[0:v][1:v]libvmaf=log_fmt=json:log_path=vmaf.json" -f null -
"""

import json
import os
import re
import subprocess

FFMPEG = os.environ.get("FFMPEG", r"D:\tools\ffmpeg\ffmpeg.exe")

# 固定模型：libvmaf 内置默认值，显式写出来便于报告记录与跨机复现
VMAF_MODEL = "vmaf_v0.6.1"


def run(cmd, cwd=None):
    """执行命令并返回 CompletedProcess（不抛异常，由调用方判断 returncode）。"""
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=cwd,
    )


def has_libvmaf():
    p = run([FFMPEG, "-hide_banner", "-filters"])
    return "libvmaf" in p.stdout


def probe(path):
    """从 ``ffmpeg -i`` 输出解析片源元数据，不依赖 ffprobe。

    返回 dict: duration_sec / width / height / fps / video_kbps(可能为 None)。
    """
    p = run([FFMPEG, "-hide_banner", "-i", path])
    text = p.stdout or ""

    duration = None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        h, mm, ss = m.groups()
        duration = int(h) * 3600 + int(mm) * 60 + float(ss)

    width = height = fps = None
    vm = re.search(
        r"Video:.*?,\s*(\d{2,5})x(\d{2,5}).*?"
        r"(?:(\d+(?:\.\d+)?)\s+fps|(\d+(?:\.\d+)?)\s+tbr)",
        text,
    )
    if vm:
        width, height = int(vm.group(1)), int(vm.group(2))
        fps = float(vm.group(3) or vm.group(4))

    video_kbps = None
    bm = re.search(r"Video:.*?(\d+)\s+kb/s", text)
    if bm:
        video_kbps = int(bm.group(1))

    if not (duration and width and height and fps):
        raise RuntimeError(f"probe failed for {path}:\n{text[-1500:]}")
    return {
        "duration_sec": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "video_kbps": video_kbps,
    }


def pick_segments(duration, n=10, seg_len=10.0, seed=42):
    """分层 + 确定性抖动地抽取 n 个样本段，返回 [(start, end), ...]。

    - 先把时间轴等分成 n 个 strata，每个 strata 内用伪随机抖动选一个起点，
      避免每段都卡在片头/固定位置（那是最容易过拟合的采法）；
    - 素材不够长时等比缩短段长，保证 n 段互不重叠且全部落在片内；
    - seed 固定 => 同一片源可复现，换 seed 即可做跨种子稳健性复核。
    """
    import random

    rng = random.Random(seed)
    seg_len = min(float(seg_len), duration * 0.9 / n)
    if seg_len <= 0:
        raise ValueError("source too short to sample")
    stride = duration / n
    segments = []
    for i in range(n):
        slot_lo = i * stride
        slot_hi = min((i + 1) * stride, duration) - seg_len
        start = rng.uniform(slot_lo, max(slot_lo, slot_hi))
        # 浮点保护：末段不得越界
        start = min(start, duration - seg_len)
        segments.append((round(start, 3), round(start + seg_len, 3)))
    return segments


def score_segments(dist, ref, segments, fps, log_dir, n_threads=1):
    """对若干样本段在**单个 ffmpeg 进程**内完成打分，返回多场景统计。

    实现：两路输入各自 split 后按段 trim，再 concat 成一条拼接流，
    只挂一个 libvmaf 实例。相比"每段起一个进程"：
      - 片源只解码一次，进程/滤镜初始化开销只付一次；
      - 评估成本与段数近似线性、与片长解耦（trim 之外的帧直接丢弃）；
      - 逐帧分按各段预期帧数切回每段统计，信息不丢。

    返回:
      {
        "per_segment": [{"start","end","frames","mean","min"}, ...],
        "vmaf_mean": 帧加权总均值,
        "vmaf_min":  所有帧的最差分,
        "vmaf_p10":   10 分位（尾部风险）,
        "seg_mean_harmonic": 段均值的调和平均（对差场景更敏感）,
        "seg_stdev": 段间标准差（场景间波动，越大说明越不稳）,
      }
    """
    os.makedirs(log_dir, exist_ok=True)
    log_name = "vmaf_concat.json"
    log_path = os.path.join(log_dir, log_name)
    n = len(segments)
    rel_dist = os.path.relpath(os.path.abspath(dist), log_dir).replace("\\", "/")
    rel_ref = os.path.relpath(os.path.abspath(ref), log_dir).replace("\\", "/")

    chains = [
        f"[0:v]split={n}" + "".join(f"[a{i}]" for i in range(n)),
        f"[1:v]split={n}" + "".join(f"[b{i}]" for i in range(n)),
    ]
    for i, (start, end) in enumerate(segments):
        chains.append(
            f"[a{i}]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
            f"fps={fps}[d{i}]"
        )
        chains.append(
            f"[b{i}]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
            f"fps={fps}[r{i}]"
        )
    chains.append("".join(f"[d{i}]" for i in range(n)) +
                  f"concat=n={n}:v=1[vd]")
    chains.append("".join(f"[r{i}]" for i in range(n)) +
                  f"concat=n={n}:v=1[vr]")
    chains.append(
        f"[vd][vr]libvmaf=log_fmt=json:log_path={log_name}"
        f":n_threads={n_threads}:model=version={VMAF_MODEL}[out]"
    )
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", rel_dist, "-i", rel_ref,
        "-filter_complex", ";".join(chains),
        "-map", "[out]", "-f", "null", "-",
    ]
    p = run(cmd, cwd=log_dir)
    if p.returncode != 0 or not os.path.exists(log_path):
        raise RuntimeError(
            f"libvmaf concat failed for {dist}:\n{p.stdout[-1500:]}"
        )
    with open(log_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    all_scores = [float(fr["metrics"]["vmaf"]) for fr in data["frames"]]
    if not all_scores:
        raise RuntimeError(f"no VMAF frames in {log_path}")

    # 按各段预期帧数切分；首尾取整误差并入最后一段
    expected = [max(1, int(round((e - s) * fps))) for s, e in segments]
    if sum(expected) != len(all_scores):
        expected[-1] += len(all_scores) - sum(expected)
    per_segment, cursor = [], 0
    for (start, end), cnt in zip(segments, expected):
        scores = all_scores[cursor:cursor + cnt]
        cursor += cnt
        per_segment.append({
            "start": start, "end": end, "frames": len(scores),
            "mean": round(sum(scores) / len(scores), 3),
            "min": round(min(scores), 3),
        })

    total = len(all_scores)
    ordered = sorted(all_scores)
    p10_idx = max(0, int(round(0.10 * (total - 1))))
    seg_means = [s["mean"] for s in per_segment]
    seg_avg = sum(seg_means) / len(seg_means)
    return {
        "per_segment": per_segment,
        "vmaf_mean": round(sum(all_scores) / total, 3),
        "vmaf_min": round(min(all_scores), 3),
        "vmaf_p10": round(ordered[p10_idx], 3),
        "seg_mean_harmonic": round(
            len(seg_means) / sum(1.0 / v for v in seg_means), 3
        ),
        "seg_stdev": round(
            (sum((v - seg_avg) ** 2 for v in seg_means) / len(seg_means))
            ** 0.5, 3
        ),
    }


def vmaf(dist, ref, log_path="vmaf_full.json"):
    """全片打分的便捷接口：返回 (mean_vmaf, log_path)。"""
    info = probe(ref)
    log_dir = os.path.dirname(os.path.abspath(log_path))
    os.makedirs(log_dir, exist_ok=True)
    stats = score_segments(
        dist, ref, [(0.0, info["duration_sec"])], info["fps"], log_dir
    )
    return stats["vmaf_mean"], os.path.join(log_dir, "vmaf_concat.json")


if __name__ == "__main__":
    import sys

    print("ffmpeg:", FFMPEG)
    print("libvmaf available:", has_libvmaf())
    if len(sys.argv) == 3:
        mean, path = vmaf(sys.argv[1], sys.argv[2])
        print(f"VMAF mean={mean} log={path}")
    else:
        print("usage: python vmaf.py <dist.mp4> <ref.mp4>")
