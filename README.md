# abr-ladder

视频转码的 **VMAF 对齐 ABR 码率阶梯** 工具（Python + ffmpeg，无 ffprobe 依赖）。

## 它解决什么问题

基线用写死的 CRF 阶梯，画质忽好忽坏、体积不可控，也回答不了"相邻档到底差多少"。
现在改为：先实测片源的 **码率→VMAF 曲线**，再按目标 VMAF 网格二分收敛出每一档
**达标所需的最低码率**，最后输出 6 档 mp4、HLS（m3u8 + ts）与打分报告。

## 三阶段流程

```
Stage A  CRF 扫描      6 档 CRF（18/22/26/30/34/38）并行编码，
                       每档在 10 个 10 秒分层抽样段上算 VMAF
                       → 得到实测 (video_kbps → VMAF) 曲线
Stage B  二分 + 插值    6 个目标 VMAF（目标网格相邻落差 1.6，给实测噪声留余量；
                       收敛后还有硬约束修复，保证相邻档实测落差严格 < 2），
                       曲线插值给初始点，在可行/不可行区间内二分，
                       收敛到达标的最低码率（容差 8%，每档 ≤4 次评估）
Stage C  定型产出       复用已有探针文件出 6 档 mp4（不重复编码），
                       流拷贝切 6s HLS 分片，写 master.m3u8、
                       report.json、report.md
```

## 用法

```bash
# 0) 确认 libvmaf 可用（或 python vmaf.py <dist.mp4> <ref.mp4> 全片打分）
python vmaf.py

# 1) 生成测试素材（仓库不提交视频二进制）
python make_sample.py --out sample/source_1080p.mp4 --seconds 600

# 2) 跑完整阶梯（CLI 与基线兼容：--src / --out）
python ladder.py --src sample/source_1080p.mp4 --out out

# 常用调参
python ladder.py --src x.mp4 --out out \
  --crfs 18 22 26 30 34 38 \   # Stage A 扫描档
  --segments 10 --seg-len 10 \ # VMAF 抽样：10 段 × 10s
  --rungs 6 --vmaf-gap 1.6 \   # 输出 6 档；目标网格落差 1.6，硬约束 <2 由修复阶段兜底
  --jobs 4 --preset veryfast \ # 4 核：4 个单线程任务并行
  --seed 42                    # 抽样抖动种子，换种子即可复核稳健性
```

产物：

```
out/
  1080p_r1.mp4 ... 1080p_r6.mp4   # 6 档，r1 画质最高
  master.m3u8                     # HLS master（BANDWIDTH/RESOLUTION/FRAME-RATE）
  hls/1080p_rN.m3u8 + 1080p_rN_*.ts
  report.json                     # 机器可读：曲线、每档试错轨迹、分段指标、耗时
  report.md                       # 人类可读打分报告
```

## 环境（本机已实测）

- Python 3.12.1，**纯标准库**，无第三方依赖；运行不受信素材请用 `python -I`
- ffmpeg 9.0.2（本机 `D:\tools\ffmpeg\ffmpeg.exe`，带 `libvmaf`），
  可用 `set FFMPEG=...` 覆盖
- **不需要 ffprobe**：分辨率/时长/fps 由 `ffmpeg -i` 输出解析（`vmaf.probe`）
- VMAF 模型固定为 libvmaf 内置 `vmaf_v0.6.1`，版本写入报告保证可复现
- Windows 注意：盘符冒号在 filtergraph 里无法可靠转义，打分时把 ffmpeg 的
  工作目录切到日志目录、log_path 只传裸文件名（见 `vmaf.score_segments`）

## 3 小时预算怎么守住（10 分钟 1080p / 4 核）

并行模型：`--jobs 4` 个 ffmpeg 进程，**每个进程 x264 与 libvmaf 都限定单线程**
（`-threads 1` / `n_threads=1`），4 核恰好满载且不发生线程超额订阅。

打分结构（关键提速）：10 个样本段不是起 10 个 ffmpeg，而是在**一个进程**里
`split→trim→concat` 后只挂一个 libvmaf 实例——进程初始化只付一次，逐段统计在
Python 侧按帧数切回，信息不丢。

基于本机实测吞吐（30s 素材、单线程）外推 10 分钟素材、10×10s=3000 帧采样：

| 实测项 | 单机实测 | 10 分钟素材外推（4 核，jobs=4） |
|---|---|---|
| libvmaf 打分 | ~10 帧/秒（单线程） | 单评估 3000 帧 ≈ 5 min；27 次评估 /4 并行 ≈ **34 min** |
| x264 veryfast 编码 | ~0.85x 实时（单线程） | 单评估编码 600s ≈ 12 min；27 次 /4 并行 ≈ **80 min** |
| Stage C 定型 | 0 次重编码（复用探针）+ HLS 流拷贝 | ≈ 5 min |
| **合计墙钟** | | **约 2 小时**，留 1.5 倍余量 |

> 注：30s 素材的冒烟墙钟（~12 min）受 27 个进程的固定启动开销主导，**不代表**
> 10 分钟素材的线性比例；长素材下固定开销被摊薄，上表按帧/秒吞吐外推更接近实际。
> 探针编码与最终成片是**同一个文件**（Stage C 直接复用），所以报告里的 VMAF 就是
> 发货文件的真实 VMAF，不存在"测量件与交付件不一致"的偏差。

预算护栏（写死在 `ladder.py`）：

- `MAX_EVAL_PER_RUNG=4`：单档评估上限，防止二分在噪声区反复横跳；
- `MAX_EVAL_TOTAL=26`：全局熔断（含间距保护与落差修复），最坏情况也封顶；
- 预算耗尽时该档回退为曲线插值结果并在报告里标 `interpolated_fallback`，不隐瞒；
- VMAF 只算 10×10s = 100s（全片的 1/6），打分工作量与片长解耦；
- 同码率请求带 in-flight 去重与缓存，两个档位收敛到同一码率不会重复编码。

机器偏慢、需要进一步压时间时（按收益排序）：

1. `--preset ultrafast`：编码是大头（~70%），换更快 preset 可显著降耗时；
   画质损失已计入 VMAF 闭环，曲线会自动抬高码率补偿，而不是默默掉画质；
2. `--crfs 18 26 34`：CRF 扫描从 6 档减到 3 档，少 3 次全片编码；
3. `--segments 8 --seg-len 8`：采样量降到 64s，打分工作量再降 1/3；
4. 这些旋钮都写进 report.json，调参后的结果仍可复现、可对比。

## 如何避免对单一场景过拟合

1. **分层 + 抖动抽样**（`vmaf.pick_segments`）：时间轴等分 10 个 strata，每段起点
   在 strata 内由固定种子伪随机抖动，覆盖片头/片中/片尾与不同运动强度，绝不锚定
   固定场景；`--seed` 换一个值就能跑稳健性复核。
2. **多指标而不是只看均值**：每档同时记录帧均值、p10、最差帧、段均值调和平均、
   段间 stdev。"均值达标但个别场景崩了"会在 p10/min/stdev 上暴露（报告第 4 节）。
3. **只插值不外延**：二分初始点与兜底值都钳制在 CRF 扫描的实测码率区间内，
   不在没有实测数据的区域凭空外推。
4. **模型与参数固化**：VMAF 模型版本、抽样种子、preset、CRF 档全部写进
   report.json，跨机/跨片源可复现可对比。
5. **片源级隔离 + 上线前多内容复核**：每条片源独立生成曲线，不沿用上一条片子的
   码率结论；上线前应在对话/运动/动画/暗场等多类内容上各跑一遍，比较段间 stdev
   与跨种子结果，stdev 明显偏大的内容类型需要单独建阶梯。

## 约定

- 不提交 `sample/`、`out/`、`out_test/` 与任何 mp4/m3u8/ts；
- `ladder.py` 的 CLI 参数（`--src` / `--out`）保持兼容。
