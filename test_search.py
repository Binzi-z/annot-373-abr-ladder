"""纯逻辑单元测试（不调用 ffmpeg）：python -m unittest -v test_search"""

import unittest

from media import select_segments
from search import interpolate_bitrate, round_bitrate, apply_safety_margin
from vmaf import split_train_holdout


class TestInterpolation(unittest.TestCase):
    def test_basic_linear(self):
        # 目标正好在两点 VMAF 中点 -> 码率也应在中点
        lo = {"vmaf_mean": 92.0, "bitrate_kbps_measured": 3000.0}
        hi = {"vmaf_mean": 90.0, "bitrate_kbps_measured": 2000.0}
        self.assertAlmostEqual(interpolate_bitrate(lo, hi, 91.0), 2500.0)

    def test_target_at_lo_returns_lo_bitrate(self):
        lo = {"vmaf_mean": 92.0, "bitrate_kbps_measured": 3000.0}
        hi = {"vmaf_mean": 90.0, "bitrate_kbps_measured": 2000.0}
        self.assertAlmostEqual(interpolate_bitrate(lo, hi, 92.0), 3000.0)

    def test_target_near_hi(self):
        lo = {"vmaf_mean": 92.0, "bitrate_kbps_measured": 3000.0}
        hi = {"vmaf_mean": 90.0, "bitrate_kbps_measured": 2000.0}
        # 90.1 -> 距 hi 10% -> 码率靠近 2000
        self.assertAlmostEqual(interpolate_bitrate(lo, hi, 90.1), 2050.0)

    def test_non_monotonic_falls_back_to_safe_side(self):
        lo = {"vmaf_mean": 91.0, "bitrate_kbps_measured": 3000.0}
        hi = {"vmaf_mean": 91.0, "bitrate_kbps_measured": 2000.0}  # span=0 退化
        self.assertEqual(interpolate_bitrate(lo, hi, 91.0), 3000.0)

    def test_bitrate_inversion_shape_preserved(self):
        # 低分辨率低效场景：hi 点码率反而更高，插值结果也应如实反映（交给上层告警）
        lo = {"vmaf_mean": 90.0, "bitrate_kbps_measured": 2200.0}
        hi = {"vmaf_mean": 88.0, "bitrate_kbps_measured": 4000.0}
        val = interpolate_bitrate(lo, hi, 89.0)
        self.assertAlmostEqual(val, 3100.0)


class TestRoundBitrate(unittest.TestCase):
    def test_steps_always_round_up(self):
        self.assertEqual(round_bitrate(1499), 1500)
        self.assertEqual(round_bitrate(1501), 1550)
        self.assertEqual(round_bitrate(4001), 4100)
        self.assertEqual(round_bitrate(99), 100)
        self.assertEqual(round_bitrate(20000), 20000)
        self.assertEqual(round_bitrate(99999), 20000)  # 封顶


class TestSafetyMargin(unittest.TestCase):
    def test_margin_then_round_up_never_below_interpolation(self):
        # 任何情况下上浮+取整后的码率都不低于原始插值
        for kbps in (300, 789, 1499, 1501, 3999, 4001, 7777):
            self.assertGreaterEqual(apply_safety_margin(kbps, 0.05), kbps)

    def test_five_percent_margin_example(self):
        # 3000 * 1.05 = 3150，落在 1500-4000 档（step 50），向上取整还是 3150
        self.assertEqual(apply_safety_margin(3000.0, 0.05), 3150)
        # 4000 * 1.05 = 4200，step 100
        self.assertEqual(apply_safety_margin(4000.0, 0.05), 4200)
        # 1000 * 1.05 = 1050，step 50
        self.assertEqual(apply_safety_margin(1000.0, 0.05), 1050)


class TestSegmentSelection(unittest.TestCase):
    def test_covers_full_timeline_within_bounds(self):
        segs = select_segments(600.0, 10, 10.0, seed=1)
        self.assertEqual(len(segs), 10)
        for start, end in segs:
            self.assertGreaterEqual(start, 0.0)
            self.assertLessEqual(end, 600.0)
            self.assertAlmostEqual(end - start, 10.0)
        # 第一段落在第一层、最后一段落在最后一层 -> 覆盖头和尾
        self.assertLess(segs[0][0], 60.0)
        self.assertGreater(segs[-1][0], 540.0 - 1e-9)

    def test_deterministic_with_seed(self):
        a = select_segments(600.0, 10, 10.0, seed=373)
        b = select_segments(600.0, 10, 10.0, seed=373)
        self.assertEqual(a, b)

    def test_scene_cuts_are_preferred(self):
        # 每层放一个切点，窗口起点应贴住切点
        cuts = [30.0, 90.0, 150.0, 210.0, 270.0, 330.0, 390.0, 450.0, 510.0, 570.0]
        segs = select_segments(600.0, 10, 10.0, scene_cuts=cuts, seed=2)
        for (start, _), cut in zip(segs, cuts):
            self.assertAlmostEqual(start, cut)

    def test_no_overlap_when_stratum_wide_enough(self):
        segs = select_segments(600.0, 10, 10.0, seed=7)
        for (_, e1), (s2, _) in zip(segs, segs[1:]):
            self.assertLessEqual(e1, s2 + 1e-9)

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            select_segments(30.0, 10, 10.0)


class TestHoldoutSplit(unittest.TestCase):
    def test_split_by_frame_ranges(self):
        # 10 个窗口，每窗口 100 帧；训练窗口帧值=90，留出窗口（800..999）帧值=80
        frames = [90.0 if i < 800 else 80.0 for i in range(1000)]
        result = {"frames": frames, "frame_nums": list(range(1000))}
        train, hold = split_train_holdout(result, 10, 2)
        self.assertAlmostEqual(train, 90.0)
        self.assertAlmostEqual(hold, 80.0)

    def test_subsampled_frames_classified_correctly(self):
        # 模拟 n_subsample=2：只有偶数帧被打分
        nums = list(range(0, 1000, 2))
        result = {
            "frames": [90.0] * len(nums),
            "frame_nums": nums,
        }
        train, hold = split_train_holdout(result, 10, 2)
        # 留出窗口是帧 800..999；偶数帧共 100 个
        self.assertAlmostEqual(hold, 90.0)
        self.assertAlmostEqual(train, 90.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
