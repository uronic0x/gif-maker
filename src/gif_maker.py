from __future__ import annotations

import os
import re
import sys
import shlex
import shutil
import pathlib
import traceback
import subprocess
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot, Qt, QSettings, QUrl
from PySide6.QtGui import QCloseEvent, QFont, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QDoubleSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


# ============================================================
# 数据模型
# ============================================================

@dataclass(slots=True)
class GifJobConfig:
    input_path: pathlib.Path
    output_path: pathlib.Path

    ffmpeg_path: str
    gifsicle_path: str
    use_gifsicle: bool

    overwrite: bool
    open_after_finish: bool

    # 时间 / 基础视频参数
    start_time: str
    duration: str
    speed: float
    fps: int
    reverse: bool
    threads: int

    # 尺寸 / 裁切
    width: int
    height: int
    keep_aspect: bool
    scale_flags: str

    crop_enabled: bool
    crop_x: int
    crop_y: int
    crop_w: int
    crop_h: int

    # FFmpeg palettegen / paletteuse
    palette_colors: int
    palette_stats_mode: str
    reserve_transparent: bool
    dither: str
    bayer_scale: int
    diff_mode: str
    alpha_threshold: int

    # Gifsicle
    optimize_level: int
    final_colors: int
    lossy: int
    loop_enabled: bool
    loop_count: int
    delay_enabled: bool
    delay_cs: int
    disposal: str
    interlace: bool
    careful: bool

    # 兜底高级参数
    extra_ffmpeg_input_args: str
    extra_ffmpeg_output_args: str
    extra_gifsicle_args: str


@dataclass(slots=True)
class CommandBundle:
    ffmpeg_cmd: list[str]
    gifsicle_cmd: list[str] | None
    temp_gif: pathlib.Path | None


# ============================================================
# 命令构造
# ============================================================

def _split_extra_args(raw: str) -> list[str]:
    """把 UI 中的额外参数文本拆成命令参数。支持带引号的路径/参数。"""
    raw = raw.strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"额外参数格式错误: {exc}") from exc


def _quote_cmd(cmd: list[str]) -> str:
    """仅用于日志/预览，不参与执行。"""
    try:
        return shlex.join([str(x) for x in cmd])
    except Exception:
        return " ".join([str(x) for x in cmd])


def _normalize_bin_path(raw: str, fallback: str) -> str:
    raw = raw.strip()
    return raw if raw else fallback


def build_filter_complex(cfg: GifJobConfig) -> str:
    chain: list[str] = []

    if cfg.speed <= 0:
        raise ValueError("播放速度必须大于 0。")
    pts_factor = round(1.0 / cfg.speed, 6)
    if abs(pts_factor - 1.0) > 0.000001:
        chain.append(f"setpts={pts_factor}*PTS")

    if cfg.reverse:
        chain.append("reverse")

    if cfg.crop_enabled:
        if cfg.crop_w <= 0 or cfg.crop_h <= 0:
            raise ValueError("启用裁切时，裁切宽度和高度必须大于 0。")
        chain.append(f"crop={cfg.crop_w}:{cfg.crop_h}:{cfg.crop_x}:{cfg.crop_y}")

    if cfg.fps <= 0:
        raise ValueError("输出帧率必须大于 0。")
    chain.append(f"fps={cfg.fps}")

    if cfg.width > 0 or cfg.height > 0:
        flags = cfg.scale_flags.strip() or "lanczos"
        width_expr = str(cfg.width) if cfg.width > 0 else "-1"
        height_expr = str(cfg.height) if cfg.height > 0 else "-1"

        if cfg.keep_aspect:
            if cfg.width > 0 and cfg.height > 0:
                chain.append(
                    f"scale={cfg.width}:{cfg.height}:flags={flags}:force_original_aspect_ratio=decrease"
                )
            else:
                chain.append(f"scale={width_expr}:{height_expr}:flags={flags}")
        else:
            if cfg.width <= 0 or cfg.height <= 0:
                raise ValueError("关闭保持比例时，必须同时填写输出宽度和高度。")
            chain.append(f"scale={cfg.width}:{cfg.height}:flags={flags}")

    pre_chain = ",".join(chain)
    if pre_chain:
        pre_chain += ","

    palettegen_options = [
        f"max_colors={cfg.palette_colors}",
        f"stats_mode={cfg.palette_stats_mode}",
        f"reserve_transparent={1 if cfg.reserve_transparent else 0}",
    ]

    paletteuse_options = [
        f"dither={cfg.dither}",
        f"alpha_threshold={cfg.alpha_threshold}",
    ]
    if cfg.dither == "bayer":
        paletteuse_options.append(f"bayer_scale={cfg.bayer_scale}")
    if cfg.diff_mode != "none":
        paletteuse_options.append(f"diff_mode={cfg.diff_mode}")

    return (
        f"[0:v]{pre_chain}split[s0][s1];"
        f"[s0]palettegen={':'.join(palettegen_options)}[p];"
        f"[s1][p]paletteuse={':'.join(paletteuse_options)}"
    )


def build_commands(cfg: GifJobConfig) -> CommandBundle:
    ffmpeg_bin = _normalize_bin_path(cfg.ffmpeg_path, "ffmpeg")
    gifsicle_bin = _normalize_bin_path(cfg.gifsicle_path, "gifsicle")

    temp_gif: pathlib.Path | None = None
    ffmpeg_output = cfg.output_path

    if cfg.use_gifsicle:
        temp_gif = cfg.output_path.parent / f"__gifmaker_raw_{os.getpid()}_{abs(hash(str(cfg.output_path))) & 0xffff:x}.gif"
        ffmpeg_output = temp_gif

    ffmpeg_cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-y" if cfg.overwrite else "-n",
    ]

    if cfg.threads > 0:
        ffmpeg_cmd.extend(["-threads", str(cfg.threads)])

    ffmpeg_cmd.extend(_split_extra_args(cfg.extra_ffmpeg_input_args))

    if cfg.start_time.strip():
        ffmpeg_cmd.extend(["-ss", cfg.start_time.strip()])

    ffmpeg_cmd.extend(["-i", str(cfg.input_path)])

    if cfg.duration.strip():
        ffmpeg_cmd.extend(["-t", cfg.duration.strip()])

    ffmpeg_cmd.extend([
        "-filter_complex",
        build_filter_complex(cfg),
        "-an",
        "-progress",
        "pipe:1",
        "-nostats",
    ])

    ffmpeg_cmd.extend(_split_extra_args(cfg.extra_ffmpeg_output_args))
    ffmpeg_cmd.append(str(ffmpeg_output))

    gifsicle_cmd: list[str] | None = None
    if cfg.use_gifsicle:
        gifsicle_cmd = [
            gifsicle_bin,
            f"-O{cfg.optimize_level}",
            f"--colors={cfg.final_colors}",
        ]

        if cfg.lossy > 0:
            gifsicle_cmd.append(f"--lossy={cfg.lossy}")

        if cfg.loop_enabled:
            gifsicle_cmd.append(f"--loopcount={cfg.loop_count}")

        if cfg.delay_enabled:
            gifsicle_cmd.append(f"--delay={cfg.delay_cs}")

        if cfg.disposal != "default":
            gifsicle_cmd.append(f"--disposal={cfg.disposal}")

        if cfg.interlace:
            gifsicle_cmd.append("--interlace")

        if cfg.careful:
            gifsicle_cmd.append("--careful")

        gifsicle_cmd.extend(_split_extra_args(cfg.extra_gifsicle_args))
        gifsicle_cmd.extend([str(ffmpeg_output), "-o", str(cfg.output_path)])

    return CommandBundle(ffmpeg_cmd=ffmpeg_cmd, gifsicle_cmd=gifsicle_cmd, temp_gif=temp_gif)


# ============================================================
# 后台工作线程
# ============================================================

class GifWorker(QThread):
    progress_changed = Signal(int, str)
    log_emitted = Signal(str)
    job_finished = Signal(bool, str)

    def __init__(self, cfg: GifJobConfig):
        super().__init__()
        self.cfg = cfg
        self.bundle: CommandBundle | None = None
        self._current_process: subprocess.Popen[str] | None = None
        self._cancel_requested = False
        self._ffmpeg_duration_seconds: float | None = None

    def run(self) -> None:
        try:
            self.bundle = build_commands(self.cfg)

            ok, msg = self._validate_environment()
            if not ok:
                self.job_finished.emit(False, msg)
                return

            self._ffmpeg_duration_seconds = self._resolve_expected_duration()

            self.progress_changed.emit(3, "准备 FFmpeg 转码...")
            self.log_emitted.emit("========== FFmpeg 命令 ==========\n" + _quote_cmd(self.bundle.ffmpeg_cmd) + "\n\n")

            ffmpeg_ok, ffmpeg_err = self._run_subprocess(
                self.bundle.ffmpeg_cmd,
                stage="ffmpeg",
                base_percent=3,
                span_percent=62,
            )
            if self._cancel_requested:
                self.job_finished.emit(False, "任务已取消。")
                return
            if not ffmpeg_ok:
                self.job_finished.emit(False, f"FFmpeg 阶段失败:\n{ffmpeg_err}".strip())
                return

            if self.cfg.use_gifsicle and self.bundle.gifsicle_cmd:
                self.progress_changed.emit(68, "准备 Gifsicle 二次优化...")
                self.log_emitted.emit("\n========== Gifsicle 命令 ==========\n" + _quote_cmd(self.bundle.gifsicle_cmd) + "\n\n")

                gifsicle_ok, gifsicle_err = self._run_subprocess(
                    self.bundle.gifsicle_cmd,
                    stage="gifsicle",
                    base_percent=68,
                    span_percent=30,
                )
                if self._cancel_requested:
                    self.job_finished.emit(False, "任务已取消。")
                    return
                if not gifsicle_ok:
                    self.job_finished.emit(False, f"Gifsicle 阶段失败:\n{gifsicle_err}".strip())
                    return

            self.progress_changed.emit(100, "处理完成")
            self.job_finished.emit(True, f"GIF 已导出:\n{self.cfg.output_path}")

        except Exception:
            self.job_finished.emit(False, "工作流执行异常:\n" + traceback.format_exc())
        finally:
            self._cleanup_temp_files()

    def cancel(self) -> None:
        self._cancel_requested = True
        proc = self._current_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _validate_environment(self) -> tuple[bool, str]:
        assert self.bundle is not None

        ffmpeg_bin = self.bundle.ffmpeg_cmd[0]
        if pathlib.Path(ffmpeg_bin).exists():
            pass
        elif not shutil.which(ffmpeg_bin):
            return False, f"未找到 FFmpeg:\n{ffmpeg_bin}\n\n请填写 ffmpeg 可执行文件路径，或把 ffmpeg 加入 PATH。"

        if self.cfg.use_gifsicle and self.bundle.gifsicle_cmd:
            gifsicle_bin = self.bundle.gifsicle_cmd[0]
            if pathlib.Path(gifsicle_bin).exists():
                pass
            elif not shutil.which(gifsicle_bin):
                return False, f"未找到 Gifsicle:\n{gifsicle_bin}\n\n可以手动选择 gifsicle 可执行文件，或取消启用 Gifsicle 二次优化。"

        if not self.cfg.input_path.exists():
            return False, f"输入文件不存在:\n{self.cfg.input_path}"

        if self.cfg.output_path.suffix.lower() != ".gif":
            return False, "输出文件扩展名必须是 .gif。"

        self.cfg.output_path.parent.mkdir(parents=True, exist_ok=True)

        if self.cfg.output_path.exists() and not self.cfg.overwrite:
            return False, f"输出文件已存在，且未启用覆盖:\n{self.cfg.output_path}"

        return True, "ok"

    def _resolve_expected_duration(self) -> float | None:
        """优先使用用户填写的 duration；否则用 ffprobe 估算进度。"""
        user_duration = self._parse_time_to_seconds(self.cfg.duration)
        if user_duration:
            return user_duration / max(self.cfg.speed, 0.0001)

        ffprobe_bin = self._guess_ffprobe_path()
        if not ffprobe_bin:
            return None

        cmd = [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(self.cfg.input_path),
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if proc.returncode == 0:
                duration = float(proc.stdout.strip())
                start = self._parse_time_to_seconds(self.cfg.start_time) or 0.0
                duration = max(duration - start, 0.0)
                return duration / max(self.cfg.speed, 0.0001)
        except Exception:
            return None
        return None

    def _guess_ffprobe_path(self) -> str | None:
        ffmpeg_raw = _normalize_bin_path(self.cfg.ffmpeg_path, "ffmpeg")
        ffmpeg_path = pathlib.Path(ffmpeg_raw)
        candidates: list[str] = []

        if ffmpeg_path.exists():
            exe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
            candidates.append(str(ffmpeg_path.with_name(exe_name)))

        candidates.append("ffprobe")

        for item in candidates:
            if pathlib.Path(item).exists() or shutil.which(item):
                return item
        return None

    @staticmethod
    def _parse_time_to_seconds(text: str) -> float | None:
        text = text.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return float(text)

        parts = text.split(":")
        try:
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + float(s)
            if len(parts) == 2:
                m, s = parts
                return int(m) * 60 + float(s)
        except ValueError:
            return None
        return None

    def _run_subprocess(
        self,
        cmd: list[str],
        stage: str,
        base_percent: int,
        span_percent: int,
    ) -> tuple[bool, str]:
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        stderr_tail: list[str] = []
        progress_cache: dict[str, str] = {}

        try:
            self._current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                startupinfo=startupinfo,
                creationflags=creationflags,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            return False, f"可执行文件不存在: {cmd[0]}\n{exc}"
        except Exception as exc:
            return False, f"无法启动子进程:\n{exc}"

        assert self._current_process.stdout is not None

        for raw_line in self._current_process.stdout:
            if self._cancel_requested:
                break

            line = raw_line.rstrip("\r\n")
            if not line:
                continue

            if stage == "ffmpeg" and "=" in line:
                key, value = line.split("=", 1)
                if key in {"frame", "fps", "stream_0_0_q", "bitrate", "total_size", "out_time_us", "out_time_ms", "out_time", "dup_frames", "drop_frames", "speed", "progress"}:
                    progress_cache[key] = value
                    self._emit_ffmpeg_progress(progress_cache, base_percent, span_percent)
                    continue

            stderr_tail.append(line)
            if len(stderr_tail) > 80:
                stderr_tail = stderr_tail[-80:]
            self.log_emitted.emit(line)

        return_code = self._current_process.wait()
        self._current_process = None

        if self._cancel_requested:
            return False, "用户已取消。"

        if stage != "ffmpeg":
            self.progress_changed.emit(base_percent + span_percent, "Gifsicle 优化完成")

        return return_code == 0, "\n".join(stderr_tail)

    def _emit_ffmpeg_progress(self, data: dict[str, str], base_percent: int, span_percent: int) -> None:
        percent = None
        out_time_us = data.get("out_time_us") or data.get("out_time_ms")

        if out_time_us and self._ffmpeg_duration_seconds:
            try:
                # FFmpeg 新版本 out_time_us 是微秒；部分旧输出字段叫 out_time_ms 但实际也是微秒。
                out_seconds = float(out_time_us) / 1_000_000.0
                ratio = max(0.0, min(out_seconds / self._ffmpeg_duration_seconds, 1.0))
                percent = base_percent + int(ratio * span_percent)
            except ValueError:
                percent = None

        if percent is None and data.get("progress") == "end":
            percent = base_percent + span_percent

        if percent is None:
            percent = min(base_percent + 10, base_percent + span_percent - 1)

        frame = data.get("frame", "?")
        speed = data.get("speed", "?")
        self.progress_changed.emit(percent, f"FFmpeg 转码中... frame={frame}, speed={speed}")

    def _cleanup_temp_files(self) -> None:
        if self.bundle and self.bundle.temp_gif and self.bundle.temp_gif.exists():
            try:
                self.bundle.temp_gif.unlink()
            except OSError:
                pass


# ============================================================
# 主窗口
# ============================================================

class GifMakerWindow(QMainWindow):
    VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.avi *.mov *.webm *.flv *.wmv);;所有文件 (*.*)"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("GIF 智能表情包压制工具 Pro+")
        self.resize(1180, 820)
        self.setMinimumSize(980, 720)
        self.setAcceptDrops(True)

        self.worker: GifWorker | None = None
        self.settings = QSettings("GifMakerProPlus", "GifMakerProPlus")

        script_dir = pathlib.Path(sys.argv[0]).resolve().parent
        self.default_ffmpeg = shutil.which("ffmpeg") or str(script_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"))
        self.default_gifsicle = shutil.which("gifsicle") or str(script_dir / ("gifsicle.exe" if sys.platform == "win32" else "gifsicle"))

        self._locked_widgets: list[QWidget] = []
        self._gifsicle_only_widgets: list[QWidget] = []
        self._crop_value_widgets: list[QWidget] = []
        self._loop_value_widgets: list[QWidget] = []
        self._delay_value_widgets: list[QWidget] = []

        self.setup_ui()
        self.connect_dependent_signals()
        self.apply_theme()
        self.load_settings()
        self.update_dependent_controls()
        self.connect_preview_signals()
        self.update_command_preview()

    # ---------------- UI 构建 ----------------

    def setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(16, 14, 16, 14)

        title = QLabel("GIF 智能表情包压制工具 Pro+")
        title.setObjectName("TitleLabel")
        subtitle = QLabel("拖入视频或选择文件；输入、参数、命令预览和日志均已拆成独立页面，避免互相挤压。")
        subtitle.setObjectName("SubtitleLabel")
        root.addWidget(title)
        root.addWidget(subtitle)

        # 关键调整：
        # 1. 不再把“输入区”和“日志区”固定堆在参数区上下。
        # 2. 所有大块内容都进入 Tab 页面切换，主窗口只保留标题、页面、底部状态/按钮。
        # 3. 这样每个设置页都能获得完整垂直空间，不会再被日志区遮挡。
        self.tabs = QTabWidget()
        self.tabs.setObjectName("MainTabs")
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.tabs.addTab(self._build_path_group(), "输入 / 引擎")
        self.tabs.addTab(self._build_basic_tab(), "基础参数")
        self.tabs.addTab(self._build_crop_tab(), "裁切")
        self.tabs.addTab(self._build_palette_tab(), "调色板")
        self.tabs.addTab(self._build_gifsicle_tab(), "Gifsicle")
        self.tabs.addTab(self._build_advanced_tab(), "高级 / 命令")
        self.tabs.addTab(self._build_log_group(), "日志")
        root.addWidget(self.tabs, stretch=1)

        root.addWidget(self._build_status_group(), stretch=0)

    def _wrap_scrollable_tab(self, content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(content)
        return area

    def _build_path_group(self) -> QGroupBox:
        group = QGroupBox("路径与引擎")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("选择或拖入输入视频...")
        self.input_btn = QPushButton("浏览")
        self.input_btn.clicked.connect(self.on_browse_input)
        input_row = QHBoxLayout()
        input_row.addWidget(self.input_edit)
        input_row.addWidget(self.input_btn)
        layout.addRow("输入视频:", input_row)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("输出 GIF 路径...")
        self.output_btn = QPushButton("另存为")
        self.output_btn.clicked.connect(self.on_browse_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit)
        output_row.addWidget(self.output_btn)
        layout.addRow("输出路径:", output_row)

        self.ffmpeg_edit = QLineEdit(self.default_ffmpeg)
        self.ffmpeg_btn = QPushButton("选择")
        self.ffmpeg_btn.clicked.connect(lambda: self.on_browse_exe(self.ffmpeg_edit, "选择 ffmpeg"))
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_edit)
        ffmpeg_row.addWidget(self.ffmpeg_btn)
        layout.addRow("FFmpeg:", ffmpeg_row)

        self.gifsicle_edit = QLineEdit(self.default_gifsicle)
        self.gifsicle_btn = QPushButton("选择")
        self.gifsicle_btn.clicked.connect(lambda: self.on_browse_exe(self.gifsicle_edit, "选择 gifsicle"))
        gifsicle_row = QHBoxLayout()
        gifsicle_row.addWidget(self.gifsicle_edit)
        gifsicle_row.addWidget(self.gifsicle_btn)
        layout.addRow("Gifsicle:", gifsicle_row)

        self.check_env_btn = QPushButton("检测引擎")
        self.check_env_btn.clicked.connect(self.on_check_environment)
        layout.addRow("", self.check_env_btn)

        self._locked_widgets.extend([
            self.input_edit,
            self.input_btn,
            self.output_edit,
            self.output_btn,
            self.ffmpeg_edit,
            self.ffmpeg_btn,
            self.gifsicle_edit,
            self.gifsicle_btn,
            self.check_env_btn,
        ])
        self._gifsicle_only_widgets.extend([self.gifsicle_edit, self.gifsicle_btn])
        return group

    def _build_basic_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(12)

        time_group = QGroupBox("时间与速度")
        time_layout = QFormLayout(time_group)
        time_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.start_edit = QLineEdit()
        self.start_edit.setPlaceholderText("可空，如 00:00:01.500 或 1.5")
        time_layout.addRow("开始时间 -ss:", self.start_edit)

        self.duration_edit = QLineEdit()
        self.duration_edit.setPlaceholderText("可空，如 00:00:03 或 3")
        time_layout.addRow("截取时长 -t:", self.duration_edit)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.05, 20.0)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSuffix(" x")
        time_layout.addRow("播放速度 setpts:", self.speed_spin)

        self.reverse_check = QCheckBox("反向播放 reverse（长视频可能更占内存）")
        time_layout.addRow("", self.reverse_check)

        outer.addWidget(time_group)

        video_group = QGroupBox("帧率与尺寸")
        video_layout = QFormLayout(video_group)
        video_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 120)
        self.fps_spin.setValue(15)
        self.fps_spin.setSuffix(" fps")
        video_layout.addRow("输出帧率 fps:", self.fps_spin)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(0, 4096)
        self.width_spin.setValue(320)
        self.width_spin.setSpecialValueText("自动")
        self.width_spin.setSuffix(" px")
        video_layout.addRow("输出宽度:", self.width_spin)

        self.height_spin = QSpinBox()
        self.height_spin.setRange(0, 4096)
        self.height_spin.setValue(0)
        self.height_spin.setSpecialValueText("自动")
        self.height_spin.setSuffix(" px")
        video_layout.addRow("输出高度:", self.height_spin)

        self.keep_aspect_check = QCheckBox("保持原比例")
        self.keep_aspect_check.setChecked(True)
        video_layout.addRow("", self.keep_aspect_check)

        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["lanczos", "spline", "bicubic", "bilinear", "area", "neighbor"])
        self.scale_combo.setCurrentText("lanczos")
        video_layout.addRow("缩放算法 flags:", self.scale_combo)

        outer.addWidget(video_group)

        run_group = QGroupBox("运行行为")
        run_layout = QFormLayout(run_group)
        run_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 64)
        self.threads_spin.setValue(0)
        self.threads_spin.setSpecialValueText("自动")
        run_layout.addRow("FFmpeg 线程:", self.threads_spin)

        self.overwrite_check = QCheckBox("覆盖已存在输出文件")
        self.overwrite_check.setChecked(True)
        run_layout.addRow("", self.overwrite_check)

        self.use_gifsicle_check = QCheckBox("启用 Gifsicle 二次优化")
        self.use_gifsicle_check.setChecked(True)
        run_layout.addRow("", self.use_gifsicle_check)

        self.open_after_check = QCheckBox("完成后自动打开输出 GIF")
        self.open_after_check.setChecked(False)
        run_layout.addRow("", self.open_after_check)

        outer.addWidget(run_group)
        outer.addStretch()

        self._locked_widgets.extend([
            self.start_edit,
            self.duration_edit,
            self.speed_spin,
            self.reverse_check,
            self.fps_spin,
            self.width_spin,
            self.height_spin,
            self.keep_aspect_check,
            self.scale_combo,
            self.threads_spin,
            self.overwrite_check,
            self.use_gifsicle_check,
            self.open_after_check,
        ])
        return tab

    def _build_crop_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)

        group = QGroupBox("FFmpeg crop 裁切")
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.crop_enabled_check = QCheckBox("启用裁切滤镜 crop=w:h:x:y")
        form.addRow("", self.crop_enabled_check)

        self.crop_x_spin = QSpinBox()
        self.crop_x_spin.setRange(0, 99999)
        self.crop_x_spin.setValue(0)
        form.addRow("X:", self.crop_x_spin)

        self.crop_y_spin = QSpinBox()
        self.crop_y_spin.setRange(0, 99999)
        self.crop_y_spin.setValue(0)
        form.addRow("Y:", self.crop_y_spin)

        self.crop_w_spin = QSpinBox()
        self.crop_w_spin.setRange(0, 99999)
        self.crop_w_spin.setSpecialValueText("未设置")
        self.crop_w_spin.setValue(0)
        form.addRow("宽度 W:", self.crop_w_spin)

        self.crop_h_spin = QSpinBox()
        self.crop_h_spin.setRange(0, 99999)
        self.crop_h_spin.setSpecialValueText("未设置")
        self.crop_h_spin.setValue(0)
        form.addRow("高度 H:", self.crop_h_spin)

        hint = QLabel("提示：裁切发生在 fps/scale 之前。W/H 必须大于 0 才能启用。")
        hint.setObjectName("HintLabel")
        form.addRow("", hint)

        layout.addWidget(group)
        layout.addStretch()

        self._locked_widgets.extend([
            self.crop_enabled_check,
            self.crop_x_spin,
            self.crop_y_spin,
            self.crop_w_spin,
            self.crop_h_spin,
        ])
        self._crop_value_widgets.extend([
            self.crop_x_spin,
            self.crop_y_spin,
            self.crop_w_spin,
            self.crop_h_spin,
        ])
        return tab

    def _build_palette_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(12)

        palette_group = QGroupBox("FFmpeg palettegen")
        palette_layout = QFormLayout(palette_group)
        palette_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.palette_color_spin = QSpinBox()
        self.palette_color_spin.setRange(2, 256)
        self.palette_color_spin.setValue(256)
        self.palette_color_spin.setSuffix(" 色")
        palette_layout.addRow("palette 最大颜色:", self.palette_color_spin)

        self.palette_stats_combo = QComboBox()
        self.palette_stats_combo.addItems(["full", "diff", "single"])
        self.palette_stats_combo.setCurrentText("full")
        palette_layout.addRow("stats_mode:", self.palette_stats_combo)

        self.reserve_transparent_check = QCheckBox("保留透明色 reserve_transparent")
        self.reserve_transparent_check.setChecked(True)
        palette_layout.addRow("", self.reserve_transparent_check)

        outer.addWidget(palette_group)

        use_group = QGroupBox("FFmpeg paletteuse")
        use_layout = QFormLayout(use_group)
        use_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.dither_combo = QComboBox()
        self.dither_combo.addItems(["sierra2_4a", "sierra2", "sierra3", "floyd_steinberg", "bayer", "heckbert", "none"])
        self.dither_combo.setCurrentText("sierra2_4a")
        use_layout.addRow("抖动 dither:", self.dither_combo)

        self.bayer_scale_spin = QSpinBox()
        self.bayer_scale_spin.setRange(0, 5)
        self.bayer_scale_spin.setValue(2)
        use_layout.addRow("bayer_scale:", self.bayer_scale_spin)

        self.diff_mode_combo = QComboBox()
        self.diff_mode_combo.addItems(["none", "rectangle"])
        self.diff_mode_combo.setCurrentText("none")
        use_layout.addRow("diff_mode:", self.diff_mode_combo)

        self.alpha_spin = QSpinBox()
        self.alpha_spin.setRange(0, 255)
        self.alpha_spin.setValue(128)
        use_layout.addRow("alpha_threshold:", self.alpha_spin)

        outer.addWidget(use_group)
        outer.addStretch()

        self._locked_widgets.extend([
            self.palette_color_spin,
            self.palette_stats_combo,
            self.reserve_transparent_check,
            self.dither_combo,
            self.bayer_scale_spin,
            self.diff_mode_combo,
            self.alpha_spin,
        ])
        return tab

    def _build_gifsicle_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(12)

        group = QGroupBox("Gifsicle 优化参数")
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.optimize_spin = QSpinBox()
        self.optimize_spin.setRange(0, 3)
        self.optimize_spin.setValue(3)
        self.optimize_spin.setPrefix("-O")
        form.addRow("优化级别:", self.optimize_spin)

        self.final_color_spin = QSpinBox()
        self.final_color_spin.setRange(2, 256)
        self.final_color_spin.setValue(128)
        self.final_color_spin.setSuffix(" 色")
        form.addRow("最终颜色 --colors:", self.final_color_spin)

        self.lossy_spin = QSpinBox()
        self.lossy_spin.setRange(0, 200)
        self.lossy_spin.setValue(0)
        self.lossy_spin.setSpecialValueText("关闭")
        form.addRow("有损压缩 --lossy:", self.lossy_spin)

        self.loop_enabled_check = QCheckBox("写入循环次数 --loopcount")
        self.loop_enabled_check.setChecked(True)
        form.addRow("", self.loop_enabled_check)

        self.loop_count_spin = QSpinBox()
        self.loop_count_spin.setRange(0, 99999)
        self.loop_count_spin.setValue(0)
        self.loop_count_spin.setSpecialValueText("无限循环 0")
        form.addRow("循环次数:", self.loop_count_spin)

        self.delay_enabled_check = QCheckBox("强制每帧延迟 --delay")
        self.delay_enabled_check.setChecked(False)
        form.addRow("", self.delay_enabled_check)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(1, 1000)
        self.delay_spin.setValue(6)
        self.delay_spin.setSuffix(" cs")
        form.addRow("帧延迟:", self.delay_spin)

        self.disposal_combo = QComboBox()
        self.disposal_combo.addItems(["default", "none", "background", "previous"])
        self.disposal_combo.setCurrentText("default")
        form.addRow("disposal:", self.disposal_combo)

        self.interlace_check = QCheckBox("交错输出 --interlace")
        form.addRow("", self.interlace_check)

        self.careful_check = QCheckBox("保守优化 --careful")
        form.addRow("", self.careful_check)

        hint = QLabel("说明：--lossy 需要你的 gifsicle 版本支持；如果不支持，日志会提示未知参数，可设回 0。")
        hint.setObjectName("HintLabel")
        form.addRow("", hint)

        outer.addWidget(group)
        outer.addStretch()

        self._locked_widgets.extend([
            self.optimize_spin,
            self.final_color_spin,
            self.lossy_spin,
            self.loop_enabled_check,
            self.loop_count_spin,
            self.delay_enabled_check,
            self.delay_spin,
            self.disposal_combo,
            self.interlace_check,
            self.careful_check,
        ])
        self._gifsicle_only_widgets.extend([
            self.optimize_spin,
            self.final_color_spin,
            self.lossy_spin,
            self.loop_enabled_check,
            self.loop_count_spin,
            self.delay_enabled_check,
            self.delay_spin,
            self.disposal_combo,
            self.interlace_check,
            self.careful_check,
        ])
        self._loop_value_widgets.append(self.loop_count_spin)
        self._delay_value_widgets.append(self.delay_spin)
        return tab

    def _build_advanced_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(12)

        extra_group = QGroupBox("高级追加参数")
        form = QFormLayout(extra_group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.extra_ffmpeg_input_edit = QLineEdit()
        self.extra_ffmpeg_input_edit.setPlaceholderText('例如：-hwaccel auto')
        form.addRow("FFmpeg 输入前参数:", self.extra_ffmpeg_input_edit)

        self.extra_ffmpeg_output_edit = QLineEdit()
        self.extra_ffmpeg_output_edit.setPlaceholderText('例如：-metadata comment="made by GifMaker"')
        form.addRow("FFmpeg 输出前参数:", self.extra_ffmpeg_output_edit)

        self.extra_gifsicle_edit = QLineEdit()
        self.extra_gifsicle_edit.setPlaceholderText("例如：--no-comments --no-names")
        form.addRow("Gifsicle 追加参数:", self.extra_gifsicle_edit)

        outer.addWidget(extra_group)

        preview_group = QGroupBox("命令预览")
        preview_layout = QVBoxLayout(preview_group)
        self.command_preview = QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setFont(QFont("Consolas", 9))
        self.command_preview.setMinimumHeight(150)
        preview_layout.addWidget(self.command_preview)
        outer.addWidget(preview_group, stretch=1)

        self._locked_widgets.extend([
            self.extra_ffmpeg_input_edit,
            self.extra_ffmpeg_output_edit,
            self.extra_gifsicle_edit,
        ])
        self._gifsicle_only_widgets.append(self.extra_gifsicle_edit)
        return tab

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("压制日志")
        self.log_tab = group
        group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(10)

        hint = QLabel("日志现在是独立页面，不再占用参数区空间。转码时可切换到这里查看 FFmpeg / Gifsicle 输出。")
        hint.setObjectName("HintLabel")
        layout.addWidget(hint)

        self.log_viewer = QPlainTextEdit()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setFont(QFont("Consolas", 9))
        self.log_viewer.setMaximumBlockCount(8000)
        self.log_viewer.setPlaceholderText("转码、调色板和压制日志会输出到这里...")
        self.log_viewer.setMinimumHeight(420)
        self.log_viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.log_viewer, stretch=1)
        return group

    def _build_status_group(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("StatusLabel")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximumHeight(26)
        layout.addWidget(self.progress_bar)

        row = QHBoxLayout()
        self.open_output_btn = QPushButton("打开输出目录")
        self.open_output_btn.clicked.connect(self.on_open_output_dir)
        row.addWidget(self.open_output_btn)

        self.action_btn = QPushButton("开始转码")
        self.action_btn.setObjectName("ActionButton")
        self.action_btn.setMinimumHeight(44)
        self.action_btn.clicked.connect(self.on_action_clicked)
        row.addWidget(self.action_btn, stretch=1)

        layout.addLayout(row)
        return wrapper

    # ---------------- 主题 ----------------

    def apply_theme(self) -> None:
        self.setStyleSheet("""
            QMainWindow {
                background-color: #18181b;
            }
            QWidget {
                color: #e5e7eb;
                font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif;
                font-size: 13px;
            }
            QLabel#TitleLabel {
                color: #ffffff;
                font-size: 22px;
                font-weight: 700;
            }
            QLabel#SubtitleLabel, QLabel#HintLabel {
                color: #a1a1aa;
            }
            QLabel#StatusLabel {
                color: #d4d4d8;
                padding-left: 2px;
            }
            QGroupBox {
                border: 1px solid #3f3f46;
                border-radius: 10px;
                margin-top: 10px;
                padding: 14px 14px 14px 14px;
                background-color: #27272a;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 6px;
                color: #60a5fa;
            }
            QTabWidget::pane {
                border: 1px solid #3f3f46;
                border-radius: 8px;
                top: -1px;
            }
            QTabBar::tab {
                background: #27272a;
                color: #d4d4d8;
                border: 1px solid #3f3f46;
                padding: 10px 22px;
                border-top-left-radius: 7px;
                border-top-right-radius: 7px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #3b82f6;
                color: #ffffff;
                border-color: #3b82f6;
            }
            QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background-color: #18181b;
                border: 1px solid #3f3f46;
                border-radius: 6px;
                padding: 6px;
                color: #ffffff;
                selection-background-color: #2563eb;
            }
            QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
                border: 1px solid #60a5fa;
            }
            QComboBox::drop-down {
                border: 0;
                width: 24px;
            }
            QCheckBox {
                spacing: 8px;
            }
            QPushButton {
                background-color: #3b82f6;
                color: #ffffff;
                border: none;
                border-radius: 7px;
                padding: 7px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #60a5fa;
            }
            QPushButton:pressed {
                background-color: #2563eb;
            }
            QPushButton:disabled {
                background-color: #52525b;
                color: #a1a1aa;
            }
            QPushButton#ActionButton[danger="true"] {
                background-color: #ef4444;
            }
            QPushButton#ActionButton[danger="true"]:hover {
                background-color: #f87171;
            }
            QProgressBar {
                border: 1px solid #3f3f46;
                border-radius: 7px;
                text-align: center;
                background-color: #18181b;
                color: #ffffff;
                font-weight: 700;
                min-height: 22px;
            }
            QProgressBar::chunk {
                background-color: #10b981;
                border-radius: 6px;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #3f3f46;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #60a5fa;
                width: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
            QScrollArea {
                background: transparent;
            }
            QScrollBar:vertical {
                border: none;
                background: #18181b;
                width: 10px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #3f3f46;
                border-radius: 5px;
                min-height: 25px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)

    # ---------------- 配置 / 状态 ----------------

    def collect_config(self, *, require_paths: bool = True, normalize_output_suffix: bool = True) -> GifJobConfig:
        input_text = self.input_edit.text().strip()
        output_text = self.output_edit.text().strip()

        if not input_text:
            if require_paths:
                raise ValueError("请先选择输入视频。")
            input_text = "input.mp4"
        if not output_text:
            if require_paths:
                raise ValueError("请先指定输出 GIF 路径。")
            output_text = "output.gif"

        output_path = pathlib.Path(output_text)
        if output_path.suffix.lower() != ".gif":
            output_path = output_path.with_suffix(".gif")
            if normalize_output_suffix:
                self.output_edit.setText(str(output_path))

        return GifJobConfig(
            input_path=pathlib.Path(input_text),
            output_path=output_path,
            ffmpeg_path=self.ffmpeg_edit.text().strip(),
            gifsicle_path=self.gifsicle_edit.text().strip(),
            use_gifsicle=self.use_gifsicle_check.isChecked(),
            overwrite=self.overwrite_check.isChecked(),
            open_after_finish=self.open_after_check.isChecked(),
            start_time=self.start_edit.text().strip(),
            duration=self.duration_edit.text().strip(),
            speed=self.speed_spin.value(),
            fps=self.fps_spin.value(),
            reverse=self.reverse_check.isChecked(),
            threads=self.threads_spin.value(),
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            keep_aspect=self.keep_aspect_check.isChecked(),
            scale_flags=self.scale_combo.currentText(),
            crop_enabled=self.crop_enabled_check.isChecked(),
            crop_x=self.crop_x_spin.value(),
            crop_y=self.crop_y_spin.value(),
            crop_w=self.crop_w_spin.value(),
            crop_h=self.crop_h_spin.value(),
            palette_colors=self.palette_color_spin.value(),
            palette_stats_mode=self.palette_stats_combo.currentText(),
            reserve_transparent=self.reserve_transparent_check.isChecked(),
            dither=self.dither_combo.currentText(),
            bayer_scale=self.bayer_scale_spin.value(),
            diff_mode=self.diff_mode_combo.currentText(),
            alpha_threshold=self.alpha_spin.value(),
            optimize_level=self.optimize_spin.value(),
            final_colors=self.final_color_spin.value(),
            lossy=self.lossy_spin.value(),
            loop_enabled=self.loop_enabled_check.isChecked(),
            loop_count=self.loop_count_spin.value(),
            delay_enabled=self.delay_enabled_check.isChecked(),
            delay_cs=self.delay_spin.value(),
            disposal=self.disposal_combo.currentText(),
            interlace=self.interlace_check.isChecked(),
            careful=self.careful_check.isChecked(),
            extra_ffmpeg_input_args=self.extra_ffmpeg_input_edit.text(),
            extra_ffmpeg_output_args=self.extra_ffmpeg_output_edit.text(),
            extra_gifsicle_args=self.extra_gifsicle_edit.text(),
        )

    def set_running_state(self, running: bool) -> None:
        for widget in self._locked_widgets:
            widget.setEnabled(not running)

        self.update_dependent_controls()

        self.action_btn.setText("中止转码" if running else "开始转码")
        self.action_btn.setProperty("danger", "true" if running else "false")
        self.action_btn.style().unpolish(self.action_btn)
        self.action_btn.style().polish(self.action_btn)

    def _is_running(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def connect_dependent_signals(self) -> None:
        self.use_gifsicle_check.toggled.connect(self.update_dependent_controls)
        self.crop_enabled_check.toggled.connect(self.update_dependent_controls)
        self.loop_enabled_check.toggled.connect(self.update_dependent_controls)
        self.delay_enabled_check.toggled.connect(self.update_dependent_controls)
        self.dither_combo.currentTextChanged.connect(self.update_dependent_controls)

    @Slot()
    def update_dependent_controls(self) -> None:
        can_edit = not self._is_running()
        use_gifsicle = self.use_gifsicle_check.isChecked()
        crop_enabled = self.crop_enabled_check.isChecked()
        loop_enabled = self.loop_enabled_check.isChecked()
        delay_enabled = self.delay_enabled_check.isChecked()
        is_bayer = self.dither_combo.currentText() == "bayer"

        for widget in self._gifsicle_only_widgets:
            widget.setEnabled(can_edit and use_gifsicle)

        for widget in self._crop_value_widgets:
            widget.setEnabled(can_edit and crop_enabled)

        for widget in self._loop_value_widgets:
            widget.setEnabled(can_edit and use_gifsicle and loop_enabled)

        for widget in self._delay_value_widgets:
            widget.setEnabled(can_edit and use_gifsicle and delay_enabled)

        self.bayer_scale_spin.setEnabled(can_edit and is_bayer)

    def connect_preview_signals(self) -> None:
        for line in [
            self.input_edit,
            self.output_edit,
            self.ffmpeg_edit,
            self.gifsicle_edit,
            self.start_edit,
            self.duration_edit,
            self.extra_ffmpeg_input_edit,
            self.extra_ffmpeg_output_edit,
            self.extra_gifsicle_edit,
        ]:
            line.textChanged.connect(self.update_command_preview)

        for spin in [
            self.speed_spin,
            self.fps_spin,
            self.width_spin,
            self.height_spin,
            self.threads_spin,
            self.crop_x_spin,
            self.crop_y_spin,
            self.crop_w_spin,
            self.crop_h_spin,
            self.palette_color_spin,
            self.bayer_scale_spin,
            self.alpha_spin,
            self.optimize_spin,
            self.final_color_spin,
            self.lossy_spin,
            self.loop_count_spin,
            self.delay_spin,
        ]:
            spin.valueChanged.connect(self.update_command_preview)

        for combo in [
            self.scale_combo,
            self.palette_stats_combo,
            self.dither_combo,
            self.diff_mode_combo,
            self.disposal_combo,
        ]:
            combo.currentTextChanged.connect(self.update_command_preview)

        for check in [
            self.reverse_check,
            self.keep_aspect_check,
            self.overwrite_check,
            self.use_gifsicle_check,
            self.open_after_check,
            self.crop_enabled_check,
            self.reserve_transparent_check,
            self.loop_enabled_check,
            self.delay_enabled_check,
            self.interlace_check,
            self.careful_check,
        ]:
            check.toggled.connect(self.update_command_preview)

    def load_settings(self) -> None:
        self.ffmpeg_edit.setText(self.settings.value("ffmpeg_path", self.default_ffmpeg, str))
        self.gifsicle_edit.setText(self.settings.value("gifsicle_path", self.default_gifsicle, str))
        self.fps_spin.setValue(int(self.settings.value("fps", 15)))
        self.width_spin.setValue(int(self.settings.value("width", 320)))
        self.height_spin.setValue(int(self.settings.value("height", 0)))
        self.final_color_spin.setValue(int(self.settings.value("final_colors", 128)))
        self.palette_color_spin.setValue(int(self.settings.value("palette_colors", 256)))
        self.use_gifsicle_check.setChecked(self.settings.value("use_gifsicle", "true") == "true")

    def save_settings(self) -> None:
        self.settings.setValue("ffmpeg_path", self.ffmpeg_edit.text().strip())
        self.settings.setValue("gifsicle_path", self.gifsicle_edit.text().strip())
        self.settings.setValue("fps", self.fps_spin.value())
        self.settings.setValue("width", self.width_spin.value())
        self.settings.setValue("height", self.height_spin.value())
        self.settings.setValue("final_colors", self.final_color_spin.value())
        self.settings.setValue("palette_colors", self.palette_color_spin.value())
        self.settings.setValue("use_gifsicle", "true" if self.use_gifsicle_check.isChecked() else "false")

    # ---------------- 事件处理 ----------------

    @Slot()
    def update_command_preview(self) -> None:
        try:
            cfg = self.collect_config(require_paths=False, normalize_output_suffix=False)
            bundle = build_commands(cfg)
            text = "FFmpeg:\n" + _quote_cmd(bundle.ffmpeg_cmd)
            if bundle.gifsicle_cmd:
                text += "\n\nGifsicle:\n" + _quote_cmd(bundle.gifsicle_cmd)
            else:
                text += "\n\nGifsicle:\n（已关闭二次优化）"
        except Exception as exc:
            text = f"命令暂不可生成：{exc}"

        self.command_preview.setPlainText(text)

    @Slot()
    def on_browse_input(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "选择输入视频", "", self.VIDEO_FILTER)
        if file_path:
            self.set_input_path(file_path)

    def set_input_path(self, file_path: str) -> None:
        self.input_edit.setText(file_path)
        p = pathlib.Path(file_path)
        suggested = p.with_name(f"{p.stem}_compressed.gif")
        self.output_edit.setText(str(suggested))

    @Slot()
    def on_browse_output(self) -> None:
        start_path = self.output_edit.text().strip() or str(pathlib.Path.home() / "output.gif")
        file_path, _ = QFileDialog.getSaveFileName(self, "保存 GIF 文件", start_path, "GIF 图像 (*.gif)")
        if file_path:
            if pathlib.Path(file_path).suffix.lower() != ".gif":
                file_path += ".gif"
            self.output_edit.setText(file_path)

    def on_browse_exe(self, target: QLineEdit, title: str) -> None:
        file_filter = "可执行文件 (*.exe);;所有文件 (*.*)" if sys.platform == "win32" else "所有文件 (*.*)"
        file_path, _ = QFileDialog.getOpenFileName(self, title, "", file_filter)
        if file_path:
            target.setText(file_path)

    @Slot()
    def on_check_environment(self) -> None:
        messages: list[str] = []
        for label, raw, fallback in [
            ("FFmpeg", self.ffmpeg_edit.text().strip(), "ffmpeg"),
            ("Gifsicle", self.gifsicle_edit.text().strip(), "gifsicle"),
        ]:
            bin_path = _normalize_bin_path(raw, fallback)
            ok = pathlib.Path(bin_path).exists() or shutil.which(bin_path)
            messages.append(f"{label}: {'可用' if ok else '未找到'}\n{bin_path}")

        QMessageBox.information(self, "引擎检测结果", "\n\n".join(messages))

    @Slot()
    def on_open_output_dir(self) -> None:
        output_text = self.output_edit.text().strip()
        if not output_text:
            QMessageBox.information(self, "提示", "尚未指定输出路径。")
            return

        output_path = pathlib.Path(output_text)
        folder = output_path if output_path.is_dir() else output_path.parent
        if not folder.exists():
            QMessageBox.information(self, "提示", f"目录不存在:\n{folder}")
            return

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    @Slot()
    def on_action_clicked(self) -> None:
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "中止确认",
                "转码任务正在进行，确定要中止吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.status_label.setText("正在中止后台进程...")
                self.worker.cancel()
            return

        try:
            cfg = self.collect_config()
            # 构造一次命令，提前暴露额外参数格式等错误。
            build_commands(cfg)
        except Exception as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            self.update_command_preview()
            return

        self.save_settings()
        self.log_viewer.clear()
        self.progress_bar.setValue(0)
        self.status_label.setText("启动中...")
        self.set_running_state(True)

        self.worker = GifWorker(cfg)
        self.worker.progress_changed.connect(self.on_worker_progress)
        self.worker.log_emitted.connect(self.on_worker_log)
        self.worker.job_finished.connect(self.on_worker_finished)
        self.worker.start()

    @Slot(int, str)
    def on_worker_progress(self, percent: int, desc: str) -> None:
        self.progress_bar.setValue(max(0, min(percent, 100)))
        self.status_label.setText(desc)

    @Slot(str)
    def on_worker_log(self, text: str) -> None:
        self.log_viewer.appendPlainText(text)
        scrollbar = self.log_viewer.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @Slot(bool, str)
    def on_worker_finished(self, success: bool, msg: str) -> None:
        self.set_running_state(False)

        if success:
            self.progress_bar.setValue(100)
            self.status_label.setText("工作流已结束: 成功")
            QMessageBox.information(self, "完成", msg)
            try:
                cfg = self.collect_config()
                if cfg.open_after_finish:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(cfg.output_path)))
            except Exception:
                pass
        else:
            if msg == "任务已取消。":
                self.status_label.setText("工作流已结束: 已取消")
                QMessageBox.information(self, "已取消", msg)
            else:
                self.progress_bar.setValue(0)
                self.status_label.setText("工作流已结束: 失败")
                QMessageBox.critical(self, "处理失败", msg)

        if self.worker:
            self.worker.wait(1500)
            self.worker = None

    # ---------------- 拖拽 / 关闭 ----------------

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        urls = event.mimeData().urls()
        if not urls:
            return
        local_file = urls[0].toLocalFile()
        if local_file:
            self.set_input_path(local_file)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.save_settings()
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "退出确认",
                "转码任务仍在进行，退出会中止任务。确定退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(2500)
        event.accept()


# ============================================================
# 全局异常处理
# ============================================================

def global_exception_hook(exc_type, exc_value, exc_tb) -> None:
    err_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(err_msg, file=sys.stderr)
    app = QApplication.instance()
    if app is not None:
        QMessageBox.critical(
            None,
            "系统未捕获异常",
            f"程序运行中发生异常，已捕获错误信息:\n\n{err_msg}",
        )
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def main() -> None:
    sys.excepthook = global_exception_hook
    app = QApplication(sys.argv)
    app.setApplicationName("GIF 智能表情包压制工具 Pro+")
    app.setOrganizationName("GifMakerProPlus")

    window = GifMakerWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
