# abr-ladder

按 **VMAF 对齐的 ABR 码率阶梯**编排工具（Python + ffmpeg/libvmaf）。

给定一条片源，自动收敛出 6 档阶梯（1080p×3 / 720p×2 / 540p×1），使每档刚好
达到目标 VMAF、相邻档 VMAF 落差约 1.5（硬约束 < 2），并产出 HLS（master.m3u8
+ 每档 playlist + TS 分片）和完整打分报告 `report.json`。

## 架构

| 文件 | 职责 |
| --- | --- |
| `media.py` | 媒体探测（不依赖 ffprobe）、场景切换检测、分层+场景对齐采样、采样窗口拼接 |
| `vmaf.py` | libvmaf 打分（单进程整条 mezzanine）、训练/留出窗口拆分 |
| `search.py` | CRF 探针打点、CRF 二分补点、码率线性插值、安全余量与刻度规整 |
| `hls.py` | 成品两 pass ABR 编码、HLS 转封装、master.m3u8 生成 |
| `ladder.py` | 编排入口（`--src/--out` 与基线兼容） |
| `test_search.py` | 纯逻辑单测（不调 ffmpeg）：`python -m unittest discover -s . -t .` |

### 收敛流程（每档独立）

1. **采样**：全片场景切换检测（`select='gt(scene,0.35)'`）+ 等宽分层，抽
   10 段×10s 共 100s 拼成参考 mezzanine；每个分辨率各做一条同分辨率参考
   （VMAF 要求同分辨率比较，低分辨率档的参考与失真都 scale 到该档分辨率）。
2. **CRF 探针**：在 CRF 网格 18/21/24/27/30/32 上对采样段编码+打分，得到
   `(实测码率, VMAF)` 点；同分辨率的档共享探针缓存（1080p 三档只编一份）。
3. **二分**：目标 VMAF 落在网格空隙时，对 CRF 轴二分补点（默认上限 2 次），
   把夹点区间压窄。
4. **插值**：用夹住目标的两个真实打点对**码率**做线性插值，得到"满足目标
   VMAF 的最低码率"，上浮 5% 安全余量（吸收 CRF→两 pass ABR 的码控漂移），
   再按 25/50/100kbps 刻度向上取整。
5. **成品**：对完整片源两 pass ABR 编码（`maxrate=1.1x, bufsize=2x`），
   再 `-c copy` 转封装 HLS，写 master 清单。
6. **终验**：对成品在全部 10 段上关子采样重打 VMAF，校验相邻档落差 < 2、
   训练/留出偏差，写入报告；异常只**告警不静默改数**。

为什么搜 CRF 而不是直接搜码率：CRF 是恒定质量模式，一次探针同时给出单调的
(码率, VMAF) 曲线；单 pass ABR 的实际码率会漂移，曲线更抖。CRF 只当测量尺，
交付物仍是码率锁定的两 pass ABR（HLS 客户端按 BANDWIDTH 选档）。

## 用法

```bash
# （可选）生成测试素材
python make_sample.py --out sample/source_1080p.mp4 --seconds 30

# 正式跑：10 段 x 10s、尾部 2 段留出、4 线程
python ladder.py --src sample/source_10min.mp4 --out out

# 快速冒烟（少段、短窗口）
python ladder.py --src sample/source_1080p.mp4 --out out_smoke \
    --segments 3 --clip-sec 5 --holdout 1 --max-bisect 1

# 只跑到探针收敛（不编成品，用于调 VMAF 目标）
python ladder.py --src ... --out out --skip-final --skip-hls --skip-verify
```

常用参数：`--segments/--clip-sec/--holdout`、`--max-bisect`、`--subsample`
（探针帧子采样，默认 2）、`--safety-margin`（默认 0.05）、`--no-scene`
（纯分层随机，关掉场景对齐）、`--overfit-warn`（默认 2.0）。

产物：

```
out/
  1080p_top.mp4 ... 540p_low.mp4     # 6 档成品（两 pass ABR）
  master.m3u8                        # 多码率清单（BANDWIDTH/RESOLUTION/CODECS）
  hls/<rung>/playlist.m3u8 + seg_*.ts
  report.json                        # 见下
  _work/                             # mezzanine、探针、逐点 VMAF 日志（可删/可复用）
```

`report.json` 关键字段：每档目标/实测码率、收敛状态与 CRF 夹点、插值码率、
成品全帧 VMAF 均值与最差帧、训练/留出 VMAF、相邻档落差；`probe_trails` 保存
每档完整探针轨迹；`constraints.warnings` 给出约束违反与告警。

## 性能预算（10min/1080p、4 核，<= 3h 的推导）

探针只编码 100s 采样段，成本与片长解耦（本机实测 veryfast 1080p 一针
"探针编码+libvmaf(subsample=2,4线程)" 约 60–90s）：

| 阶段 | 工作量 | 估算墙钟 |
| --- | --- | --- |
| 场景检测 + 3 条 mezzanine | 全片解码 1 遍 + 3×100s 轻编码 | ~8 min |
| 探针打点（含缓存复用、二分） | 约 16–20 针 × ~75s | ~22 min |
| 成品两 pass ABR ×6 | 600s 片 × 6 档 × 两遍，veryfast ~1.2x | ~45 min |
| 成品终验 VMAF（全帧、4线程） | 6 × 100s × ~25fps | ~12 min |
| HLS 转封装 + 杂项 | 仅 copy，不解码 | ~5 min |
| **合计** | | **≈ 1.6 h（余量后 < 2.5h）** |

提速手段：探针 VMAF 用 `n_subsample=2`（实测与全帧均分差 ~0.001）、同分辨率
探针缓存复用、采样段拼接后单进程打分（省 ffmpeg 初始化）、编码预设统一
veryfast（实测 medium→veryfast 对 VMAF 影响 <0.3，码率代价 ~5%，已被安全余量覆盖）。

## 如何避免对单一场景过拟合

1. **分层 + 场景对齐采样**：时间轴切 10 个等宽 stratum，每层必取一个窗口，
   头/中/尾全覆盖；层内优先把窗口贴在场景切点上（新场景建立段最难压），
   无切点时用固定 seed 的分层随机抖动，避免"整十秒"确定性偏差。`--seed`
   可复现，换 seed 即可做采样稳健性交叉验证。
2. **训练/留出双集**：收敛只看前 8 段，尾部 2 段全程不参与插值；终验同时
   报告两段 VMAF，偏差超过 `--overfit-warn`（默认 2 分）即在报告告警。
3. **100s/3000 帧的样本量**：单点噪声被平均，且每档都报告**最差帧 VMAF**，
   防止均值被好压的帧拉高。
4. **异常只告警不改数**：码率倒挂（低分辨率档反而更费码率，全帧噪声源的
   典型病理）、质量平台（相邻档无差异=无效档）、目标不可达都原样保留并在
   `constraints.warnings` 标注，绝不通过裁剪 VMAF 目标制造"好看的阶梯"。
5. **建议的片源级验证**：对多条不同类型片源（对话/运动/动画/暗场）分别跑，
   阶梯参数按群体分位数（如 P50 码率）定稿，而不是单条片源的最优点。

## 环境与已知事实

- Python 3.12、ffmpeg 9.0.2（`D:\tools\ffmpeg\ffmpeg.exe`，带 libvmaf；
  可用 `set FFMPEG=...` 覆盖），不需要 ffprobe。
- **Windows 路径坑**：libvmaf `log_path` 里的盘符冒号在 filtergraph 两级解析
  中无法可靠转义；`vmaf.py` 用 `cwd=` 切到日志目录 + 裸文件名规避。
- testsrc2+noise 合成源有病理效应（降分辨率消不掉全帧噪声），低分辨率档
  会出现质量平台/码率倒挂，属预期告警，真实片源一般不会如此。
- 不提交 `sample/`、`out*/` 与任何 mp4/m3u8/ts。
