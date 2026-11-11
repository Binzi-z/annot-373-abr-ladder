"""生成一段可复现的测试素材（1080p / 30s / 带运动与噪声）。

用途：仓库里不放视频二进制，跑之前用它生成 sample/ 素材。

    python make_sample.py --out sample/source_1080p.mp4 --seconds 30
"""

import argparse
import os
import subprocess

FFMPEG = os.environ.get("FFMPEG", r"D:\tools\ffmpeg\ffmpeg.exe")

# testsrc2 提供运动与颜色变化，再叠一点噪声，避免"纯色视频太好压"的偏差
FILTER = (
    "testsrc2=size=1920x1080:rate=30:duration={sec},"
    "noise=alls=12:allf=t+u"
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sample/source_1080p.mp4")
    ap.add_argument("--seconds", type=int, default=30)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", FILTER.format(sec=args.seconds),
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={args.seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        args.out,
    ]
    print("$", " ".join(cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        print(p.stdout[-2000:])
        raise SystemExit("sample generation failed")
    print(f"ok: {args.out} ({os.path.getsize(args.out)} bytes)")


if __name__ == "__main__":
    main()
