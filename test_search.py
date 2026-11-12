"""search.py 纯逻辑单测（不依赖 ffmpeg）。跑完即删。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import search

# check_bitrate_monotonicity：默认非破坏，只告警、不改码率
rungs = [
    {"name": "1080", "height": 1080, "bitrate_kbps": 5000},
    {"name": "720a", "height": 720, "bitrate_kbps": 3005},
    {"name": "720b", "height": 720, "bitrate_kbps": 4923},  # 倒挂
    {"name": "540", "height": 540, "bitrate_kbps": 2000},
]
before = [r["bitrate_kbps"] for r in rungs]
warns = search.check_bitrate_monotonicity([dict(r) for r in rungs])
after = [r["bitrate_kbps"] for r in rungs]
assert before == after, "非破坏检测不得改动码率"
assert any(w["lower"] == "720b" and "反超" in w["reason"] for w in warns), warns
print("PASS non-destructive inversion warning:", len(warns), "条告警")

# enforce_spacing：显式严格模式才裁剪到单调
out = search.enforce_spacing([dict(r) for r in rungs])
rates = [r["bitrate_kbps"] for r in out]
print("strict-mode rates:", rates)
for a, b in zip(rates, rates[1:]):
    assert b <= round(a / 1.15)
assert any(r.get("spacing_adjusted") for r in out)
print("PASS strict enforce_spacing clips when explicitly enabled")

# build_rung_specs：相邻目标 VMAF 落差 >= 2 必须报错
bad = [{"name": "a", "height": 1080, "target_vmaf": 95},
       {"name": "b", "height": 720, "target_vmaf": 92.9}]
try:
    search.build_rung_specs(bad)
    raise AssertionError("should have raised")
except ValueError:
    print("PASS gap>=2 rejected")
good = [{"name": "a", "height": 1080, "target_vmaf": 95},
        {"name": "b", "height": 720, "target_vmaf": 93.5}]
search.build_rung_specs(good)
print("PASS gap<2 accepted")

# 插值：构造过原点附近的线性关系，验证解出的码率
p_lo = {"bitrate_kbps": 1000, "vmaf_mean": 80.0}
p_hi = {"bitrate_kbps": 3000, "vmaf_mean": 90.0}
got = search._interpolate_bitrate(p_lo, p_hi, 85.0)
assert abs(got - 2000.0) < 1e-6, got
got2 = search._interpolate_bitrate(p_lo, p_hi, 95.0)  # 超出范围夹到 hi
assert got2 == 3000, got2
print("PASS interpolation")

# find_min_bitrate：用确定性 mock 探针 VMAF = 70 + bitrate/500（夹在 1000~15000）
def mock_probe(br):
    return {"vmaf_mean": min(100.0, 70 + br / 500.0), "vmaf_min": 0,
            "bitrate_kbps_measured": br}
br, trail, status = search.find_min_bitrate(mock_probe, 90.0, 500, 8000, max_bisections=4)
print("mock search ->", br, "kbps,", status, "probes:", len(trail))
assert 9500 <= br <= 10500, br  # 理论解 (90-70)*500=10000
print("PASS bisection+interpolation converges near analytic solution")

# 低端即过线：目标很低时直接返回 lo
br2, _, st2 = search.find_min_bitrate(mock_probe, 60.0, 500, 8000, max_bisections=2)
assert st2 == "already_passes_at_lo" and br2 == 500
print("PASS already-passes-at-lo branch")
print("ALL TESTS PASSED")
