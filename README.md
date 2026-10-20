# GIF Maker Pro+

A PySide6 desktop GUI for converting videos to high-quality, well-optimized GIFs, powered by FFmpeg (palettegen/paletteuse) and optional Gifsicle post-optimization.

一个基于 PySide6 的桌面 GUI，将视频转换为高质量、体积优化的 GIF。底层使用 FFmpeg（palettegen/paletteuse 调色板方案），并可选 Gifsicle 二次优化。

[中文文档 / Chinese README](docs/README.zh-CN.md)

## Features / 功能

- **Palette-based encoding** — FFmpeg `palettegen` + `paletteuse` for vivid colors, full control over dither, stats mode, alpha threshold and bayer scale.
  **调色板编码** — 完整暴露 palettegen/paletteuse 参数：抖动算法、stats_mode、透明阈值、bayer_scale 等。
- **Optional Gifsicle optimization** — optimize level, color reduction, lossy compression, loop count, frame delay, disposal, interlace.
  **可选 Gifsicle 二次优化** — 优化级别、颜色数、有损压缩、循环次数、帧延迟、disposal、交错输出。
- **Trim, speed, reverse, crop, scale** — start time, duration, playback speed, reverse, pixel-accurate crop, resolution with aspect-ratio preservation.
  **裁剪与变换** — 起始时间、时长、变速、倒放、裁切、缩放（可保持比例）。
- **Live command preview** — see the exact FFmpeg/Gifsicle command line before running it.
  **命令实时预览** — 运行前即可看到完整命令行。
- **Background worker with progress & cancel** — transcode runs in a QThread with progress reporting and safe cancellation.
  **后台线程与进度** — QThread 后台转码，支持进度显示与安全中止。
- **Drag & drop** — drop a video file onto the window to get started.
  **拖放支持** — 拖入视频即可开始。

## Requirements / 环境要求

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html) (with `ffprobe`)
- [Gifsicle](https://www.lcdf.org/gifsicle/) (optional, bundled for Windows in `third_party/gifsicle/`)
- Python packages / Python 依赖：

```bash
pip install PySide6
```

## Getting Started / 快速开始

```bash
git clone https://github.com/uronic0x/gif-maker.git
cd gif-maker-pro-plus
pip install PySide6
python src/gif_maker.py
```

Tip: put `ffmpeg.exe` next to `src/gif_maker.py` or add it to your `PATH`. The app also remembers your chosen binary paths.

提示：把 `ffmpeg.exe` 放在 `src/gif_maker.py` 同目录或加入 `PATH`。程序会记住你手动选择的路径。

## Project Layout / 目录结构

```
├── src/
│   └── gif_maker.py          # Main application / 主程序
├── third_party/
│   └── gifsicle/             # Bundled gifsicle binary (Windows) / 附带的 gifsicle
├── docs/
│   └── README.zh-CN.md       # Chinese README / 中文说明
├── LICENSE
└── .gitignore
```

## License / 许可证

This project is licensed under the MIT License — see [LICENSE](LICENSE).

Gifsicle is bundled under the GNU GPL v2 (see `third_party/gifsicle/`). Binary and corresponding source are available at https://www.lcdf.org/gifsicle/.

本项目采用 MIT 许可证。捆绑的 Gifsicle 遵循 GNU GPL v2（源码见其官网）。
