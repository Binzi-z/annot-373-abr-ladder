"""VMAF 打分：分段采样 + 单次 libvmaf 调用。

本机实测（Windows 11 + ffmpeg 9.0.2，1080p）：
- filtergraph 选项里的盘符冒号（D:）无法可靠转义 → 把 cwd 切到工作目录，
  log_path 只传裸文件名，输入文件用相对路径（正斜杠）；
- 多段打分用单进程 concat → 一个 libvmaf，比每段各起一个 ffmpeg 快 ~40%；
- n_threads=4 + n_subsample=2 ≈ 45fps，与全帧打分均分差 ~0.001 VMAF，
  收敛阶段可安全使用；最终报告可用 n_subsample=1 复核。
"""

import json
import os
import re
import subprocess

FFMPEG = os.environ.get("FFMPEG", r"D:\tools\ffmpeg\ffmpeg.exe")


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def has_libvmaf():
    p = run([FFMPEG, "-hide_banner", "-filters"])
    return "libvmaf" in p.stdout


def probe_info(path):
    """用 ffmpeg -i 解析 (duration_sec, width, height, fps)；不依赖 ffprobe。"""
    p = run([FFMPEG, "-hide_banner", "-i", path])
    dur = w = h = None
    fps = None
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stdout)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    mv = re.search(r"Video:.*", p.stdout)
    if mv:
        line = mv.group(0)
        mr = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if mr:
            w, h = int(mr.group(1)), int(mr.group(2))
        mf = re.search(r"([\d.]+) fps", line)
        if mf:
            fps = float(mf.group(1))
    return dur, w, h, (fps or 30.0)


def pick_segments(duration, n=10, seg_len=10.0):
    """在整片上等距挑 n 段（跳过首尾各 5%），返回起始秒列表。

    分层抽样：段均匀铺满全片，避免只盯单一场景导致阶梯对该片源过拟合。
    """
    n = max(1, min(n, int(duration // max(seg_len, 1.0)) or 1))
    head = duration * 0.05
    usable = duration * 0.9 - seg_len
    if usable <= 0:
        return [0.0]
    step = usable / n
    return [round(head + i * step, 3) for i in range(n)]


def encode_segments(src, segs, seg_len, width, height, crf, fps, workdir, tag,
                    preset="veryfast"):
    """把采样段按 (width,height,crf) 编成探针片段，返回 (文件名列表, 总字节)。"""
    names, total = [], 0
    for i, s in enumerate(segs):
        name = f"probe_{tag}_{i:02d}.mp4"
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{s:.3f}", "-i", src, "-t", f"{seg_len:.3f}",
               "-vf", f"scale={width}:{height},fps={fps:g}",
               "-c:v", "libx264", "-preset", preset, "-crf", f"{crf:.2f}",
               "-pix_fmt", "yuv420p", "-an", name]
        p = run(cmd, cwd=workdir)
        if p.returncode != 0:
            raise RuntimeError(f"probe encode failed: {p.stdout[-1500:]}")
        names.append(name)
        total += os.path.getsize(os.path.join(workdir, name))
    return names, total


def score_segments(dist_names, ref, segs, seg_len, dist_w, dist_h,
                   ref_w, ref_h, fps, workdir, tag, n_subsample=2):
    """单次 libvmaf 给全部探针段打分（低分辨率档先双三次上采样回源分辨率）。

    返回 dict(mean, min_seg, p5_frame, per_seg, log_path)。
    """
    n = len(dist_names)
    log_name = f"vmaf_{tag}.json"
    ref_rel = os.path.relpath(os.path.abspath(ref), workdir).replace("\\", "/")

    fg = ["".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}[dc]"]
    chain = "[dc]"
    if (dist_w, dist_h) != (ref_w, ref_h):
        fg.append(f"[dc]scale={ref_w}:{ref_h}:flags=bicubic[ds]")
        chain = "[ds]"
    for i, s in enumerate(segs):
        fg.append(f"[{n}:v]trim=start={s:.3f}:end={s + seg_len:.3f},"
                  f"setpts=PTS-STARTPTS[r{i}]")
    fg.append("".join(f"[r{i}]" for i in range(n)) + f"concat=n={n}[rc]")
    fg.append(f"{chain}[rc]libvmaf=n_threads=4:n_subsample={n_subsample}"
              f":log_fmt=json:log_path={log_name}")

    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    for name in dist_names:
        cmd += ["-i", name]
    cmd += ["-i", ref_rel, "-lavfi", ";".join(fg), "-f", "null", "-"]
    p = run(cmd, cwd=workdir)
    if p.returncode != 0:
        raise RuntimeError(f"libvmaf failed: {p.stdout[-1500:]}")

    log_path = os.path.join(workdir, log_name)
    with open(log_path, encoding="utf-8") as f:
        data = json.load(f)
    frames = [fr["metrics"]["vmaf"] for fr in data["frames"]]
    mean = data["pooled_metrics"]["vmaf"]["mean"]

    # 按段切分逐帧分数（探针已 fps 归一，各段帧数一致）
    k = max(1, len(frames) // n)
    per_seg = []
    for i in range(n):
        chunk = frames[i * k:(i + 1) * k] or frames[-k:]
        per_seg.append(round(sum(chunk) / len(chunk), 3))
    srt = sorted(frames)
    p5_frame = srt[max(0, len(srt) // 20)]
    return {
        "mean": round(mean, 3),
        "min_seg": round(min(per_seg), 3),
        "p5_frame": round(p5_frame, 3),
        "per_seg": per_seg,
        "log_path": log_path,
    }


def vmaf(dist, ref):
    """单对整段打分，返回 (mean_vmaf, log_path)。"""
    workdir = os.path.dirname(os.path.abspath(dist)) or "."
    dur, w, h, fps = probe_info(dist)
    _, rw, rh, _ = probe_info(ref)
    res = score_segments([os.path.basename(dist)], ref, [0.0], dur, w, h,
                         rw, rh, fps, workdir, "pair", n_subsample=1)
    return res["mean"], res["log_path"]


if __name__ == "__main__":
    print("libvmaf available:", has_libvmaf())
