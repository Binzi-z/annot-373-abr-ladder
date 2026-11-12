"""按目标 VMAF 二分 + 插值收敛每一档的最低码率。

每档（分辨率）流程：
  1. 先探边界 lo/hi 两个码率，夹住目标 VMAF（高端不够就几何翻倍）；
  2. 二分 max_bisections 次（每次把区间砍半），得到一串观测点；
  3. 在最贴近目标的两个观测点之间对 bitrate→VMAF 做线性插值，解出目标码率；
  4. 对插值结果再做一次验证探针，实际没过线就向上取到已验证的观测点。

约束：相邻档 VMAF 落差 < 2 —— 通过相邻档 target_vmaf 之差 < 2 来保证
（实测 VMAF 围绕目标收敛，报告里会给出真实落差供核验）。
"""


def _interpolate_bitrate(p_lo, p_hi, target_vmaf):
    """在两个观测点之间线性插值出达到 target_vmaf 的码率。"""
    b_lo, v_lo = p_lo["bitrate_kbps"], p_lo["vmaf_mean"]
    b_hi, v_hi = p_hi["bitrate_kbps"], p_hi["vmaf_mean"]
    if v_hi == v_lo:
        return b_hi
    ratio = (target_vmaf - v_lo) / (v_hi - v_lo)
    ratio = max(0.0, min(1.0, ratio))
    return b_lo + ratio * (b_hi - b_lo)


def find_min_bitrate(probe_fn, target_vmaf, lo_kbps, hi_kbps,
                     max_bisections=5):
    """返回 (选定码率, 观测轨迹, 状态)。probe_fn(bitrate)->metrics dict。"""
    trail = []

    def probe(b):
        m = probe_fn(round(b))
        trail.append({
            "bitrate_kbps": round(b),
            "vmaf_mean": round(m["vmaf_mean"], 3),
            "vmaf_min": round(m["vmaf_min"], 3),
            "bitrate_kbps_measured": m.get("bitrate_kbps_measured"),
        })
        return m

    # 1) 夹住目标：低端要低于目标，高端要高于目标
    low = probe(lo_kbps)
    high = probe(hi_kbps)
    while high["vmaf_mean"] < target_vmaf and hi_kbps < 20000:
        hi_kbps *= 2
        high = probe(hi_kbps)
    if low["vmaf_mean"] >= target_vmaf:
        # 最低码率已过线（通常出现在高质量源 / 目标定得低）
        return round(lo_kbps), trail, "already_passes_at_lo"

    lo, hi = lo_kbps, hi_kbps
    # 2) 二分
    for _ in range(max_bisections):
        mid = round((lo + hi) / 2)
        m = probe(mid)
        if m["vmaf_mean"] >= target_vmaf:
            hi = mid
        else:
            lo = mid

    # 取最贴近目标的下方/上方观测点
    below = [t for t in trail if t["vmaf_mean"] < target_vmaf]
    above = [t for t in trail if t["vmaf_mean"] >= target_vmaf]
    p_lo = max(below, key=lambda t: t["bitrate_kbps"])
    p_hi = min(above, key=lambda t: t["bitrate_kbps"])

    # 3) 插值
    interp = round(_interpolate_bitrate(p_lo, p_hi, target_vmaf))
    interp = max(lo_kbps, min(interp, p_hi["bitrate_kbps"]))

    # 4) 验证探针：没过线就向上取到已验证的 hi 观测点
    verify = probe(interp)
    if verify["vmaf_mean"] >= target_vmaf:
        return interp, trail, "interpolated_verified"
    return round(p_hi["bitrate_kbps"]), trail, "rounded_up_to_verified_point"


def build_rung_specs(rungs):
    """校验阶梯规格：相邻档目标 VMAF 落差必须 < 2。"""
    rungs = sorted(rungs, key=lambda r: r["height"], reverse=True)
    for a, b in zip(rungs, rungs[1:]):
        gap = a["target_vmaf"] - b["target_vmaf"]
        if gap >= 2.0:
            raise ValueError(
                f"相邻档目标 VMAF 落差 {gap:.1f} >= 2（{a['name']}->{b['name']}）"
            )
    return rungs


def check_bitrate_monotonicity(rungs, min_ratio=1.15):
    """非破坏地检测码率倒挂，返回告警列表（不改动任何码率）。

    码率倒挂（低分辨率档反而要更高码率）说明该分辨率对当前内容低效，
    常见于强噪声/高频纹理源：缩放消不掉这类不可压缩成分，上采样还会被
    VMAF 罚分。任务的硬约束是相邻档 VMAF 落差 < 2，并非码率单调，因此
    默认保留各档搜索到的最优码率，只把倒挂显式上报，交由运维决策。
    """
    rungs = sorted(rungs, key=lambda r: r["height"], reverse=True)
    warnings = []
    for upper, lower in zip(rungs, rungs[1:]):
        if lower["bitrate_kbps"] >= upper["bitrate_kbps"]:
            warnings.append({
                "upper": upper["name"],
                "lower": lower["name"],
                "upper_kbps": upper["bitrate_kbps"],
                "lower_kbps": lower["bitrate_kbps"],
                "reason": "低分辨率档码率反超高档，该分辨率对此内容可能低效",
            })
        elif lower["bitrate_kbps"] > round(upper["bitrate_kbps"] / min_ratio):
            warnings.append({
                "upper": upper["name"],
                "lower": lower["name"],
                "upper_kbps": upper["bitrate_kbps"],
                "lower_kbps": lower["bitrate_kbps"],
                "reason": f"相邻档码率间隔小于 {min_ratio} 倍，ABR 切换区分度不足",
            })
    return warnings


def enforce_spacing(rungs, min_ratio=1.15):
    """可选的严格单调模式：自顶向下把每档码率夹到 <= 上一档 / min_ratio。

    仅在需要 HLS BANDWIDTH 严格单调的场景显式启用。被夹的档实际 VMAF 会
    低于其目标值，调用方应在报告里读取 spacing_adjusted 并知情。
    默认流程用 check_bitrate_monotonicity 只告警、不裁剪。
    """
    rungs = sorted(rungs, key=lambda r: r["height"], reverse=True)
    for r in rungs:
        r.setdefault("bitrate_kbps_searched", r["bitrate_kbps"])

    for i in range(1, len(rungs)):
        upper, lower = rungs[i - 1], rungs[i]
        cap = round(upper["bitrate_kbps"] / min_ratio)
        if lower["bitrate_kbps"] > cap:
            lower["bitrate_kbps"] = cap
            lower["spacing_adjusted"] = True
    return rungs
