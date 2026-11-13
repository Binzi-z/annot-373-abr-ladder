"""固定码率阶梯转码（基线实现）。

当前行为：
  - 读入一个源文件，按写死的 CRF 列表转 3 档；
  - 每档输出 mp4（H.264/AAC），并写一份 report.json 记录体积与码率；
  - 不计算任何画质指标，也不做码率优化。

这是给后续改造用的基线：要把它换成"按 VMAF 对齐的 ABR 阶梯"。
"""

import argparse
import json
import os
import subprocess
import sys
import time

# 写死的阶梯：档位名 -> CRF
LADDER = {
    "1080p_high": 18,
    "1080p_mid": 23,
    "1080p_low": 28,
}

FFMPEG = os.environ.get("FFMPEG", r"D:\tools\ffmpeg\ffmpeg.exe")


def run(cmd):
    print("$", " ".join(cmd))
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def duration_seconds(path):
    """用 ffmpeg -i 解析时长（不依赖 ffprobe）。"""
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in p.stdout.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            hms = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = hms.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return None


def transcode(src, out_path, crf):
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_path,
    ]
    p = run(cmd)
    if p.returncode != 0:
        print(p.stdout[-2000:])
        raise SystemExit("transcode failed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="source video")
    ap.add_argument("--out", default="out", help="output dir")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        raise SystemExit(f"source not found: {args.src}")

    os.makedirs(args.out, exist_ok=True)
    dur = duration_seconds(args.src)
    print(f"source: {args.src}  duration={dur}s")

    report = {"source": args.src, "duration_sec": dur, "renditions": []}
    started = time.time()

    for name, crf in LADDER.items():
        out_path = os.path.join(args.out, f"{name}.mp4")
        t0 = time.time()
        transcode(args.src, out_path, crf)
        size = os.path.getsize(out_path)
        entry = {
            "name": name,
            "crf": crf,
            "file": out_path,
            "bytes": size,
            "bitrate_kbps": round(size * 8 / 1000 / dur, 1) if dur else None,
            "encode_sec": round(time.time() - t0, 1),
        }
        report["renditions"].append(entry)
        print(f"  {name}: {entry['bytes']} bytes, {entry['bitrate_kbps']} kbps, {entry['encode_sec']}s")

    report["total_sec"] = round(time.time() - started, 1)
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"done in {report['total_sec']}s -> {os.path.join(args.out, 'report.json')}")


if __name__ == "__main__":
    main()
