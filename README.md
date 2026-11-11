# abr-ladder

视频转码的码率阶梯工具（Python + ffmpeg）。

## 现状（基线）

- `ladder.py`：按写死的 CRF 阶梯（18 / 23 / 28）转 3 档，产出 mp4 + `report.json`；
- `vmaf.py`：只有接口，调用会抛 `NotImplementedError`；
- `make_sample.py`：生成一段可复现的 1080p 测试素材。

**没有**画质度量、**没有**码率寻优，档位是拍脑袋定的。

## 环境

- Python 3.11+
- ffmpeg（本机 `D:\tools\ffmpeg\ffmpeg.exe`，支持 `libvmaf`）
- 可用 `set FFMPEG=...` 覆盖路径

## 用法

```bash
# 1) 生成 30 秒 1080p 测试素材
python make_sample.py --out sample/source_1080p.mp4 --seconds 30

# 2) 转码
python ladder.py --src sample/source_1080p.mp4 --out out

# 3) 看看 libvmaf 是否可用
python vmaf.py
```

产物：

```
out/
  1080p_high.mp4
  1080p_mid.mp4
  1080p_low.mp4
  report.json     # 每档的体积、码率、编码耗时
```

## 已知问题（要解决的）

1. 档位用固定 CRF，画质与体积不可控，相邻档可能出现"码率翻倍但肉眼无差"；
2. 没有 VMAF，无法回答"这一档到底比上一档差多少"；
3. 没有按"相邻档 VMAF 落差 < 2"的约束去收敛；
4. 报告里只有体积/码率，没有画质维度。

## 约定

- 不提交 `sample/`、`out/` 与任何 mp4；
- `ladder.py` 的 CLI 参数（`--src` / `--out`）保持兼容。
