"""ffmpeg 探测与采样工具。

只依赖 ffmpeg（本机没有 ffprobe）：
  - probe_source：解析时长 / 分辨率 / 帧率 / 音频参数；
  - detect_scenes：用 select='gt(scene,t)' + showinfo 找硬切点；
  - pick_segments：时间轴分层 + 场景感知地挑 10 段样本，避免单一镜头过拟合。
"""

import os
import re
import subprocess

FFMPEG = os.environ.get("FFMPEG", r"D:\tools\ffmpeg\ffmpeg.exe")

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_LINE_RE = re.compile(r"Stream.*Video:.*")
_AUDIO_LINE_RE = re.compile(r"Stream.*Audio:.*")
_RES_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"([\d.]+)\s*fps")
_SHOWINFO_RE = re.compile(r"showinfo.*?pts_time:([\d.]+)")


def run(cmd, cwd=None):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=cwd,
    )


def probe_source(path):
    """用 `ffmpeg -i` 解析源元数据，不依赖 ffprobe。"""
    p = run([FFMPEG, "-hide_banner", "-i", path])
    text = p.stdout
    m = _DURATION_RE.search(text)
    if not m:
        raise RuntimeError(f"无法解析时长: {path}")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    width = height = fps = None
    mv = _VIDEO_LINE_RE.search(text)
    if mv:
        line = mv.group(0)
        mr = _RES_RE.search(line)
        if mr:
            width, height = int(mr.group(1)), int(mr.group(2))
        mf = _FPS_RE.search(line)
        if mf:
            fps = float(mf.group(1))
    return {
        "path": os.path.abspath(path),
        "duration_sec": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": bool(_AUDIO_LINE_RE.search(text)),
    }


def detect_scenes(path, threshold=0.35):
    """返回硬切时间点（秒）列表。

    只做场景检测、不编码输出，showinfo 把命中帧的 pts_time 打到日志。
    threshold 越高只认越硬的切，默认 0.35 对常规内容较稳。
    """
    cmd = [
        FFMPEG, "-hide_banner", "-i", path,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-",
    ]
    p = run(cmd)
    return [float(x) for x in _SHOWINFO_RE.findall(p.stdout)]


def pick_segments(duration, n=10, clip_sec=10, scenes=None):
    """时间轴分层 + 场景感知地挑 n 段，每段 clip_sec 秒。

    防过拟合的第一道关：
      1. 把时间轴等分成 n 个 bin，保证每个区间都有样本（不会扎堆在开头）；
      2. 每个 bin 优先取落在其中的硬切点附近，没有切点就取 bin 中心；
      3. 全部片段去重、夹在 [0, duration] 内。

    短片（时长 < n*clip_sec）时自动缩短每段长度，尽量铺满时间轴。
    """
    clip_sec = min(clip_sec, max(0.5, duration / n))
    scenes = sorted(scenes or [])
    segs, seen = [], set()
    bin_w = duration / n
    for i in range(n):
        lo, hi = i * bin_w, (i + 1) * bin_w
        center = (lo + hi) / 2
        # 选 bin 内离中心最近的硬切点；没有就用中心
        candidates = [s for s in scenes if lo <= s < hi]
        anchor = min(candidates, key=lambda s: abs(s - center)) if candidates else center
        start = max(0.0, min(anchor, duration - clip_sec))
        start = round(start, 3)
        key = round(start, 1)
        if key in seen:
            continue
        seen.add(key)
        segs.append((start, round(start + clip_sec, 3)))
    return segs
