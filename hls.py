"""成品编码与 HLS 打包：两 pass ABR 锁码率 + ffmpeg hls muxer + master 清单。

探针阶段用 CRF 找质量点，成品阶段为什么改两 pass ABR：
  HLS 客户端按 master 清单里的 BANDWIDTH 选档，需要每档实际码率稳定可预期；
  两 pass 让 x264 按目标码率做全局码控分配，整段码率偏差通常 <3%。
"""

import os

from media import FFMPEG, run


def two_pass_encode(src, out_mp4, width, height, target_kbps, audio,
                    threads=4, preset="veryfast"):
    """对完整片源做两 pass 编码（音频只在第二遍处理），返回 (mp4路径, 日志)。"""
    base = os.path.splitext(out_mp4)[0]
    log_prefix = base + "_2pass"
    common = [
        "-c:v", "libx264", "-preset", preset,
        "-b:v", f"{target_kbps}k",
        "-maxrate", f"{int(target_kbps * 1.1)}k",
        "-bufsize", f"{int(target_kbps * 2)}k",
        "-pix_fmt", "yuv420p",
        "-threads", str(threads),
        "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1",
    ]
    # pass 1：无音频，输出到 NUL（Windows 下 ffmpeg 认 /dev/null 也行，用 NUL 更稳）
    p1 = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src, *common,
        "-pass", "1", "-passlogfile", log_prefix,
        "-an", "-f", "mp4", "NUL",
    ]
    run(p1)
    # pass 2：带音频
    p2 = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src, *common,
        "-pass", "2", "-passlogfile", log_prefix,
        "-movflags", "+faststart",
    ]
    if audio:
        p2 += ["-c:a", "aac", "-b:a", "128k"]
    else:
        p2 += ["-an"]
    p2 += [out_mp4]
    run(p2)
    # x264 两遍编码留下的统计文件
    for junk in (log_prefix + "-0.log", log_prefix + "-0.log.mbtree"):
        if os.path.exists(junk):
            try:
                os.remove(junk)
            except OSError:
                pass
    return out_mp4


def package_hls(mp4_path, hls_dir, name, target_kbps, audio, seg_sec=6):
    """把成品 mp4 转封装为 HLS（MPEG-TS，不重新编码），返回 playlist 相对路径。"""
    os.makedirs(hls_dir, exist_ok=True)
    playlist = os.path.join(hls_dir, "playlist.m3u8")
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", mp4_path,
        "-c", "copy",
        "-f", "hls",
        "-hls_time", str(seg_sec),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", os.path.join(hls_dir, "seg_%05d.ts"),
        playlist,
    ]
    run(cmd)
    return playlist


def write_master(entries, out_path, audio_kbps=128):
    """写 master.m3u8。entries: [(name, width, height, target_kbps, has_audio, playlist_rel)]。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for name, w, h, kbps, has_audio, rel in entries:
        overhead = audio_kbps if has_audio else 0
        bandwidth = int((kbps + overhead) * 1000 * 1.08)  # 8% 封装/突发余量
        codecs = 'avc1.4d401f,mp4a.40.2' if has_audio else 'avc1.4d401f'
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={w}x{h},CODECS="{codecs}"'
        )
        lines.append(rel.replace(os.sep, "/"))
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return out_path
