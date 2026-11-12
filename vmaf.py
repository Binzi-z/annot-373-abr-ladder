"""VMAF 计算（尚未实现）。

基线里只留了接口和 ffmpeg 的 libvmaf 调用示例，真正的采样、二分搜索、
阶梯收敛留给改造者实现。

ffmpeg 的 libvmaf 用法示例（本机 ffmpeg 9.0.2 自带 libvmaf）：
    ffmpeg -i dist.mp4 -i ref.mp4 -lavfi "[0:v][1:v]libvmaf=log_fmt=json:log_path=vmaf.json" -f null -
"""

import os
import subprocess

FFMPEG = os.environ.get("FFMPEG", r"D:\tools\ffmpeg\ffmpeg.exe")


def vmaf(dist, ref):
    """返回 (mean_vmaf, log_path)；未实现，调用即抛错。"""
    raise NotImplementedError(
        "VMAF 尚未实现：请用 libvmaf 滤镜实现打分，并在此基础上做码率二分搜索"
    )


def has_libvmaf():
    p = subprocess.run([FFMPEG, "-hide_banner", "-filters"], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return "libvmaf" in p.stdout


if __name__ == "__main__":
    print("libvmaf available:", has_libvmaf())
