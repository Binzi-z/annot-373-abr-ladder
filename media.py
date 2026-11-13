"""ffmpeg 封装：媒体探测、场景切换检测、分层采样、片段抽取。

只依赖 ffmpeg（不依赖 ffprobe），与基线 ladder.py 的约定一致。
"""

import os
import random
import re
import subprocess

FFMPEG = os.environ.get("FFMPEG", r"D:\tools\ffmpeg\ffmpeg.exe")

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")
_WH_RE = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})[,\s]")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_AUDIO_RE = re.compile(r"^\s*Stream #\d+:\d+.*Audio:", re.MULTILINE)


def run(cmd, check=True):
    """跑 ffmpeg，返回 CompletedProcess（合并 stdout/stderr）。"""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and p.returncode != 0:
        tail = "\n".join(p.stdout.splitlines()[-40:])
        raise RuntimeError(f"command failed ({p.returncode}):\n{' '.join(cmd)}\n{tail}")
    return p


def media_info(path):
    """用 `ffmpeg -i` 解析时长/分辨率/fps/有无音频（不需要 ffprobe）。"""
    p = run([FFMPEG, "-hide_banner", "-i", path], check=False)
    txt = p.stdout
    m = _DURATION_RE.search(txt)
    if not m:
        raise RuntimeError(f"cannot parse duration of {path}")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    wh = _WH_RE.search(txt)
    width, height = (int(wh.group(1)), int(wh.group(2))) if wh else (None, None)
    fps_m = _FPS_RE.search(txt)
    fps = float(fps_m.group(1)) if fps_m else None
    return {
        "path": os.path.abspath(path),
        "duration_sec": round(duration, 3),
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": bool(_AUDIO_RE.search(txt)),
    }


def detect_scene_cuts(path, threshold=0.35):
    """返回场景切换时间点（秒）列表。用 showinfo 解析 pts_time。

    testsrc2 这类合成源没有自然切点时返回空列表，由调用方走兜底策略。
    """
    cmd = [
        FFMPEG, "-hide_banner", "-i", path,
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    p = run(cmd, check=False)
    cuts = []
    for line in p.stdout.splitlines():
        if "pts_time:" in line and "showinfo" in line:
            m = re.search(r"pts_time:(\d+\.?\d*)", line)
            if m:
                cuts.append(float(m.group(1)))
    return cuts


def select_segments(duration, n_segments, clip_sec, scene_cuts=(), seed=373):
    """分层 + 场景对齐采样：把时间轴切成 n 段等长 strata，每层取一个窗口。

    防过拟合的第一道闸：
      - 等宽分层保证 10 个窗口覆盖全片（头/中/尾都有），而不是扎堆在好压的片段；
      - 每层内若存在场景切换点，优先把窗口起点贴在切点上（覆盖新场景的建立过程，
        这通常是最难压的部分），多个候选时用固定 seed 做可复现的抖动选择；
      - 没有切点时用可复现的分层随机抖动，避免每次都取整十秒的确定性偏差；
      - 窗口不越界、互不重叠（层宽 >= clip_sec 时天然不重叠）。
    """
    rng = random.Random(seed)
    usable = max(0.0, duration - clip_sec)
    strata_w = duration / n_segments
    if strata_w < clip_sec:
        raise ValueError(
            f"source too short: {n_segments}x{clip_sec}s strata need >= "
            f"{n_segments * clip_sec / 2:.0f}s source, got {duration:.1f}s"
        )
    segments = []
    for i in range(n_segments):
        lo = i * strata_w
        hi = min((i + 1) * strata_w - clip_sec, usable)
        if hi < lo:
            hi = lo
        cuts_in_stratum = [t for t in scene_cuts if lo <= t <= hi]
        if cuts_in_stratum:
            start = rng.choice(cuts_in_stratum)
        else:
            start = lo + rng.uniform(0.0, max(0.0, hi - lo))
        start = round(min(start, usable), 3)
        segments.append([start, round(start + clip_sec, 3)])
    return segments


def extract_mezzanine(src, segments, out_path, size=None, force=False):
    """把多个 [start,end] 窗口抽出来拼成一条参考 mezzanine（重编码只做一次）。

    size=None 时保持原分辨率；否则 scale 到指定 WxH（低分辨率档的参考也要
    下采样，VMAF 要求同分辨率比较）。force=True 时忽略已有文件重抽
    （成品码率变了，验证用的失真 mezzanine 必须随之重建，不能复用旧的）。
    """
    if os.path.exists(out_path) and not force:
        return out_path
    parts = []
    for idx, (start, end) in enumerate(segments):
        parts += ["-ss", str(start), "-to", str(end), "-i", src]
    n = len(segments)
    streams = "".join(f"[{idx}:v]" for idx in range(n))
    vf = f"{streams}concat=n={n}:v=1:a=0[v]"
    if size:
        vf = f"{streams}concat=n={n}:v=1:a=0[cat];[cat]scale={size}:flags=lanczos,setsar=1[v]"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        *parts,
        "-filter_complex", vf, "-map", "[v]",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    run(cmd)
    return out_path
