"""VMAF 打分：单进程 libvmaf 对整条采样 mezzanine 打分，支持训练/留出切分。

Windows 路径注意（本机实测，见项目记忆）：ffmpeg filtergraph 有两级解析，
`log_path` 里的盘符冒号无法可靠转义。规避办法：subprocess 用 cwd= 切到日志
所在目录，log_path 只给裸文件名，输入路径用相对该目录的正斜杠相对路径。

性能（1080p 本机实测）：n_threads=4 + n_subsample=2 约 45fps，子采样与全帧
均分差 ~0.001 VMAF，可安全用于收敛；最终成品验证可关子采样。
"""

import json
import os
import subprocess

from media import FFMPEG, run


def has_libvmaf():
    p = subprocess.run([FFMPEG, "-hide_banner", "-filters"], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return "libvmaf" in p.stdout


def _rel(path, base):
    return os.path.relpath(path, base).replace(os.sep, "/")


def score(dist, ref, work_dir, tag, n_threads=4, n_subsample=2):
    """对 dist vs ref 跑一次 libvmaf，返回均值/最差帧/逐帧分。

    dist/ref 必须同分辨率、帧对齐（都由同一套采样窗口生成）。
    结果写到 work_dir/vmaf_<tag>.json 并缓存（重跑时直接读）。
    """
    os.makedirs(work_dir, exist_ok=True)
    log_name = f"vmaf_{tag}.json"
    log_abs = os.path.join(work_dir, log_name)
    if not os.path.exists(log_abs):
        filt = (
            f"libvmaf=log_fmt=json:log_path={log_name}"
            f":n_threads={n_threads}:n_subsample={n_subsample}"
        )
        cmd = [
            FFMPEG, "-hide_banner", "-i", _rel(dist, work_dir),
            "-i", _rel(ref, work_dir),
            "-lavfi", filt, "-f", "null", "-",
        ]
        # cwd 切到 work_dir：规避 Windows 盘符冒号在 filtergraph 里的转义问题
        p = subprocess.run(cmd, cwd=work_dir, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
        if p.returncode != 0 or not os.path.exists(log_abs):
            raise RuntimeError(f"libvmaf failed for {tag}:\n{p.stdout[-2000:]}")

    with open(log_abs, encoding="utf-8") as f:
        data = json.load(f)
    frames = [float(fr["metrics"]["vmaf"]) for fr in data["frames"]]
    frame_nums = [int(fr["frameNum"]) for fr in data["frames"]]
    pooled = data.get("pooled_metrics", {}).get("vmaf", {})
    return {
        "mean": float(pooled.get("mean", sum(frames) / len(frames))),
        "min": float(min(frames)),
        "frames": frames,
        "frame_nums": frame_nums,
        "log": log_abs,
        "n_subsample": n_subsample,
    }


def split_train_holdout(score_result, n_segments, n_holdout=2):
    """按采样窗口边界把逐帧 VMAF 拆成训练集/留出集。

    mezzanine 由 n_segments 个等长窗口 concat 而成（同源同 fps → 等帧数），
    用 frameNum 所在区间归属窗口；最后 n_holdout 个窗口作留出集。
    返回 (train_mean, holdout_mean)，只用被子采样到的帧。
    """
    total_frames = max(score_result["frame_nums"]) + 1
    per_clip = total_frames / n_segments
    train_vals, hold_vals = [], []
    for num, val in zip(score_result["frame_nums"], score_result["frames"]):
        clip_idx = min(int(num / per_clip), n_segments - 1)
        if clip_idx >= n_segments - n_holdout:
            hold_vals.append(val)
        else:
            train_vals.append(val)
    train_mean = sum(train_vals) / len(train_vals)
    hold_mean = sum(hold_vals) / len(hold_vals) if hold_vals else None
    return train_mean, hold_mean


def vmaf(dist, ref, work_dir=None):
    """单文件便捷接口：返回 (mean_vmaf, log_path)。"""
    work_dir = work_dir or os.path.dirname(os.path.abspath(dist))
    tag = os.path.splitext(os.path.basename(dist))[0]
    r = score(dist, ref, work_dir, tag, n_subsample=1)
    return r["mean"], r["log"]


if __name__ == "__main__":
    print("libvmaf available:", has_libvmaf())
