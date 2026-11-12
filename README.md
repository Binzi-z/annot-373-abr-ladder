# abr-ladder

按 **VMAF 对齐的 ABR 码率阶梯**编排工具（Python + ffmpeg，无需 ffprobe）。

对同一片源跑多档分辨率 × 目标码率，抽 **10 段各 10 秒**样本算 VMAF，
用**二分 + 线性插值**收敛出每档达到目标 VMAF 的最低码率，输出 **6 档阶梯、
HLS（fmp4 + m3u8）与打分报告**。

## 用法

```bash
# 可选：生成测试素材（仓库不入库视频）
python make_sample.py --out sample/source_10min.mp4 --seconds 600

# 主流程（CLI 与基线兼容）
python ladder.py --src sample/source_10min.mp4 --out out
```

常用开关：`--segments 10 --clip-sec 10 --max-bisections 5 --n-threads 4
--n-subsample 2 --preset veryfast --keep-probes`。

## 流水线（6 步）

| 步骤 | 模块 | 做什么 |
| --- | --- | --- |
| 1 采样 | `probe.py` | 解析源（`ffmpeg -i`，无 ffprobe）；场景检测 `select='gt(scene,t)'`；时间轴**分层**挑 10×10s 片段 |
| 2 参考片 | `vmaf.py` | 一次 ffmpeg 把 10 段 `split→trim→concat` 成近无损 mezzanine（CRF14），**全片只全量解码这一次** |
| 3 搜索 | `search.py` + `vmaf.py` | 每档对 100s mezzanine 做 ABR 探针编码 + 单 libvmaf 打分；二分 5 次后线性插值，再打一验证针 |
| 4 终编码 | `ladder.py` | 用收敛码率对整片出 6 个 mp4（libx264 ABR + AAC，无音频源自动 `-an`） |
| 5 HLS | `ladder.py` | fmp4 分段、每档 playlist + `master.m3u8`（BANDWIDTH 取实测码率 ×1.1） |
| 6 报告 | `ladder.py` | 最终成片复核 VMAF、相邻档真实落差、全部探针轨迹写 `report.json` |

### 为什么探针只编 100s 而不是整片

探针要跑 ~50 次（6 档 × 8 针）。每次都编 10 分钟全片会超时；改成先把
100s 样本拼成 mezzanine，探针的编码/打分工作量降到全片的 1/6，而 VMAF
仍来自覆盖全片时间轴的 10 个分散片段。

## 6 档默认阶梯与「相邻档落差 < 2」

| 档 | 分辨率 | 目标 VMAF |
| --- | --- | --- |
| 1080p_top | 1920×1080 | 94.0 |
| 1080p_high | 1920×1080 | 92.5 |
| 1080p_mid | 1920×1080 | 91.0 |
| 720p_high | 1280×720 | 89.5 |
| 720p_low | 1280×720 | 88.0 |
| 540p_low | 960×540 | 86.5 |

相邻档**目标** VMAF 之差恒为 1.5（< 2），`build_rung_specs` 会硬校验；
终编码后再复核**实测**落差写入 `report.json.constraints`，任何 ≥2 的档对
都会标记到 `violations`。

### 码率倒挂：默认告警而非静默裁剪

任务的硬约束是相邻档 **VMAF** 落差 < 2，并**没有**要求码率严格单调。
某些内容（强噪声、高频纹理，如本仓库的 testsrc2+noise 合成源）里，降分辨率
消不掉不可压缩成分、上采样还会被 VMAF 罚分，导致低分辨率档反而需要更高码率。
此时 `check_bitrate_monotonicity` **保留各档搜索到的 VMAF 最优码率**，只把
倒挂/间隔不足作为告警写进 `report.json.constraints.bitrate_monotonicity_warnings`
和运行日志，由运维决定是否剔除该低效档。若确实需要 HLS BANDWIDTH 严格单调，
显式加 `--enforce-monotonic` 才会把倒挂档夹到「上一档 / 1.15」（被夹档实际
VMAF 会低于目标，并打 `spacing_adjusted` 标记）。

## 如何避免对单一场景过拟合

1. **时间轴分层采样**：均分成 10 个 bin，每 bin 必取一段，样本不会扎堆；
2. **场景感知**：bin 内优先锚定硬切点（`gt(scene,0.35)`），切点不足时
   回退到 bin 中心，兼顾「有切点的真实内容」和「连续运动长镜头」；
3. **报告每帧 VMAF 的 min 与 mean**：min 防止均值被平稳段抬高、掩盖复杂段塌方；
4. **子采样而非截帧**：`n_subsample=2` 隔帧打分，100s 内仍覆盖全部 10 段的
   时间跨度（实测与全帧打分均分差 ~0.001，吞吐翻倍）；
5. 换片源重跑即可——码率不是写死的，阶梯对每个源重新收敛。

## 时间预算（10 分钟 1080p，4 核）

本机实测（ffmpeg 9.0.2，libvmaf 4 线程 + 子采样 2，打分吞吐 ~45fps）：

| 阶段 | 实测耗时（冷启动一次性跑） |
| --- | --- |
| 场景检测 + 源探测 | ~2 分钟 |
| 参考 mezzanine（全片解码 1 次 + 100s CRF14） | ~3 分钟 |
| 探针 **50 次**，每针对完整 100s/1500 帧编码+打分 | **~88–95s/针 → ~73 分钟（主耗时）** |
| 6 档整片终编码（veryfast） | 实测 118–205s/档，合计 **~15.6 分钟** |
| HLS 打包（流复制）+ 报告汇总 | ~2 分钟 |
| **合计** | **约 95–100 分钟，远低于 3 小时预算** |

> 单针耗时由样本帧数决定：100s 探针 ~88s，30s 短样本仅 ~23s。
> 探针结果持久化在 `_work/probe_cache.json`、参考片也按路径复用，中断后
> 重跑只补未探的点（本次因中途调整策略续跑，已探的 35 针 0 秒命中，
> `report.json.total_sec=37.9min` 是续跑口径，非冷启动总耗时）。

## Windows 注意事项

libvmaf 的 `log_path` 里盘符冒号（`D:`）无法在 filtergraph 转义，
`vmaf.py` 统一把 ffmpeg 的 `cwd` 切到工作目录、`log_path` 只传裸文件名，
输入用相对路径。

## 产物

```
out/
  1080p_top.mp4 ... 540p_low.mp4   # 6 档成片
  master.m3u8                       # 主 playlist
  hls/<档>/playlist.m3u8 + init.mp4 + seg_*.m4s
  report.json                       # 码率/VMAF/落差/探针轨迹/约束校验
  _work/                            # mezzanine、缓存、打分日志（可清理）
```

## 约定

- 不提交 `sample/`、`out/` 与任何 mp4/m3u8/ts（见 `.gitignore`）；
- `ladder.py` 的 `--src` / `--out` 与基线保持兼容；
- 纯逻辑测试：`python test_search.py`（不依赖 ffmpeg）。
