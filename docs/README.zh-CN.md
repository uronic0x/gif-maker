# GIF Maker Pro+（中文说明）

一个基于 PySide6 的桌面 GUI，将视频转换为高质量、体积优化的 GIF。底层使用 FFmpeg（palettegen/paletteuse 调色板方案），并可选 Gifsicle 二次优化。

[English README](../README.md)

## 功能

- **调色板编码** — 完整暴露 palettegen/paletteuse 参数：抖动算法、stats_mode、透明阈值、bayer_scale 等。
- **可选 Gifsicle 二次优化** — 优化级别、颜色数、有损压缩、循环次数、帧延迟、disposal、交错输出。
- **裁剪与变换** — 起始时间、时长、变速、倒放、裁切、缩放（可保持比例）。
- **命令实时预览** — 运行前即可看到完整命令行。
- **后台线程与进度** — QThread 后台转码，支持进度显示与安全中止。
- **拖放支持** — 拖入视频即可开始。

## 环境要求

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html)（含 `ffprobe`）
- [Gifsicle](https://www.lcdf.org/gifsicle/)（可选；Windows 版已附带在 `third_party/gifsicle/`）

```bash
pip install PySide6
```

## 快速开始

```bash
git clone https://github.com/uronic0x/gif-maker.git
cd gif-maker-pro-plus
pip install PySide6
python src/gif_maker.py
```

提示：把 `ffmpeg.exe` 放在 `src/gif_maker.py` 同目录或加入 `PATH`。程序会记住你手动选择的路径。

## 目录结构

```
├── src/
│   └── gif_maker.py          # 主程序
├── third_party/
│   └── gifsicle/             # 附带的 gifsicle（Windows 二进制）
├── docs/
│   └── README.zh-CN.md       # 本文件
├── LICENSE
└── .gitignore
```

## 许可证

本项目采用 MIT 许可证。捆绑的 Gifsicle 遵循 GNU GPL v2（源码见 https://www.lcdf.org/gifsicle/）。
