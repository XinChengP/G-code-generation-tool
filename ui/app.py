# -*- coding: utf-8 -*-
"""
CNC G 代码生成工具 —— 界面版（支持打包为独立 exe）
====================================================
设计约定（详见仓库《开发文档》）：
  * 核心算法一行不改：dxf2gcode.py / preview_gcode.py / _filter_dense.py
    以模块方式 import 后**只调用其公开函数**，算法逻辑零改动。
  * 双进程架构：界面进程点「开始转换」后，用 --worker 参数重启自身
    （脚本模式 = python app.py --worker；exe 模式 = CNCGcodeTool.exe --worker），
    worker 进程在内部直接调用核心模块完成全部文件，日志逐行写入 ui_tmp\job_xxx.log，
    界面进程 tail 该文件刷新显示。好处：
      - exe 完全自包含，对方机器无需安装 Python；
      - 取消 = 终止 worker 进程，随时可停；
      - 核心代码崩溃不会拖垮界面。
  * 程序号规则与 convert_all.bat 一致：转换前预占号，失败归还，
    成功文件的编号永远连续（O0001、O0002…）。

代码分五段：
  A. Settings         参数 + JSON 持久化 + 校验
  B. Worker           工作进程：import 核心模块、逐文件 过滤→转换→预览
  C. ConversionEngine 引擎：启动 worker + tail 日志文件（纯逻辑，无 UI，可脱离界面测试）
  D. App              Tkinter 界面
  E. main             入口（--worker / --smoke / 正常启动）
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Pillow 是项目既有依赖（preview 脚本也用它）
try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ----------------------------------------------------------------------
# 路径常量（exe 模式下根目录 = exe 所在目录，脚本模式 = ui\ 的上级）
# ----------------------------------------------------------------------

FROZEN = bool(getattr(sys, "frozen", False))
APP_SCRIPT = None if FROZEN else Path(__file__).resolve()
ROOT_DIR = (Path(sys.executable).resolve().parent if FROZEN
            else Path(__file__).resolve().parents[1])
CORE_DIR = ROOT_DIR / "core"

INPUT_DIR = ROOT_DIR / "input"
GCODE_DIR = ROOT_DIR / "gcode"
PREVIEW_DIR = ROOT_DIR / "preview"
UI_TMP_DIR = ROOT_DIR / "ui_tmp"            # 任务 JSON / 日志 / 预过滤中间产物

SETTINGS_FILE = ROOT_DIR / "ui_settings.json"

APP_TITLE = "CNC G 代码生成工具 v1.0"
DEFAULT_FONT = r"C:\Windows\Fonts\simhei.ttf"
SUPPORTED_EXT = (".dxf", ".dwf", ".dwfx")

# 原点选项：显示文本 -> 传给核心的值
ORIGIN_CHOICES = [
    ("右下 (默认)", "br"),
    ("左下", "bl"),
    ("左上", "tl"),
    ("右上", "tr"),
]

# 参数面板底部的固定提示：算法限制只记录不修，指向开发文档
_LIMIT_HINT = ("已知算法限制（POLYLINE 弧段、大图速度、整圆 I/J 等）"
               "本期不修，详见项目文档《开发文档》第 6 章")


# ======================================================================
# A. 设置：参数数据类 + JSON 持久化 + 校验
# ======================================================================

@dataclass
class Settings:
    """全部可调参数，默认值与 core 脚本的 CLI 默认值一一对应"""
    safe_z: float = 15.0            # 安全高度
    cut_depth: float = -0.2         # 下刀深度
    feed: float = 200.0             # 进给
    origin: str = "br"              # 原点
    snap_tol: float = 0.0           # 端点吸附容差
    prog_start: int = 1             # 批量起始程序号（1~9999）
    arc_mode: str = "auto"          # auto=R 优先(整圆回退 I/J) / ij=强制 I/J
    font_path: str = DEFAULT_FONT   # 文字轮廓化字体
    optimize: bool = True           # 链间排序优化
    filter_enabled: bool = False    # 是否先跑密集线过滤
    filter_threshold: float = 0.5   # 过滤阈值 mm

    def save(self) -> None:
        try:
            SETTINGS_FILE.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass                                  # 保存失败不致命，下次启动用默认

    @classmethod
    def load(cls) -> "Settings":
        st = cls()
        if not SETTINGS_FILE.is_file():
            return st
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return st
        for f in fields(cls):
            if f.name not in data:
                continue
            cur = getattr(st, f.name)
            try:
                if isinstance(cur, bool):
                    setattr(st, f.name, bool(data[f.name]))
                elif isinstance(cur, int):
                    setattr(st, f.name, int(data[f.name]))
                elif isinstance(cur, float):
                    setattr(st, f.name, float(data[f.name]))
                else:
                    setattr(st, f.name, str(data[f.name]))
            except (TypeError, ValueError):
                pass                              # 类型不对就用默认
        return st


def validate_settings(st: Settings) -> list:
    """参数校验，返回错误列表（空列表 = 通过）。独立成函数便于无界面测试。"""
    errors = []
    if st.cut_depth >= 0:
        errors.append("下刀深度应为负值（切入工件为负方向）")
    if st.feed <= 0:
        errors.append("进给速度必须大于 0")
    if st.snap_tol < 0:
        errors.append("吸附容差不能为负数")
    if not (1 <= st.prog_start <= 9999):
        errors.append("程序号起始值必须在 1 ~ 9999 之间")
    if st.filter_enabled and st.filter_threshold <= 0:
        errors.append("过滤阈值必须大于 0")
    if not st.font_path:
        errors.append("文字字体路径为空；若图纸确无文字可填默认黑体")
    elif not Path(st.font_path).is_file():
        errors.append(f"找不到字体文件：{st.font_path}\n"
                      "图纸含文字时转换会失败，请修正或点「浏览」重新选择")
    return errors


# ======================================================================
# B. Worker：工作进程逻辑（被 --worker 调起，无 UI）
#    直接 import 核心模块，只调用公开函数；日志写入文件由界面 tail。
# ======================================================================

class _LogWriter:
    """worker 的唯一输出通道：逐行写日志文件，特殊前缀承载结构化事件"""

    def __init__(self, path: Path) -> None:
        self._fh = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, s: str) -> int:
        if s:
            self._fh.write(s)
            self._fh.flush()
        return len(s) if s else 0

    def flush(self) -> None:
        self._fh.flush()

    def stage(self, text: str) -> None:
        self.write(f"@@STAGE {text}\n")

    def event(self, obj: dict) -> None:
        self.write("@@EVENT " + json.dumps(obj, ensure_ascii=False) + "\n")


def _import_core_modules() -> None:
    """把 core\ 加入模块搜索路径（脚本模式需要；exe 里模块已打包，无害）"""
    for p in (CORE_DIR, ROOT_DIR):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


def _worker_main(job_file: str) -> int:
    """
    工作进程入口：读任务 JSON，逐文件执行 预过滤(可选) → 转换 → 预览。
    退出码 0 = 全部处理完毕（单个文件失败不算进程失败，失败详情在日志里）。
    """
    _import_core_modules()
    job = json.loads(Path(job_file).read_text(encoding="utf-8"))
    settings = Settings(**job["settings"])
    writer = _LogWriter(Path(job["logfile"]))
    sys.stdout = writer          # 核心模块的 print 全部进日志文件
    sys.stderr = writer          # warnings / 库报错同样进日志

    files = job["files"]
    UI_TMP_DIR.mkdir(exist_ok=True)
    GCODE_DIR.mkdir(exist_ok=True)
    PREVIEW_DIR.mkdir(exist_ok=True)

    prog = settings.prog_start
    ok = fail = 0

    for idx, path in enumerate(files):
        name = Path(path).stem
        assigned = prog
        prog += 1                                   # 预占号（失败再归还）
        writer.event({"type": "status", "idx": idx,
                      "status": "running", "prog": assigned})
        writer.stage(f"━━━ [{idx + 1}/{len(files)}] "
                     f"{Path(path).name} → O{assigned:04d} ━━━")
        try:
            src = Path(path)
            conv_in = str(src)

            # (1) 预过滤：仅对 DXF 生效（复用 _filter_dense.main，argv 注入参数）
            use_filter = (settings.filter_enabled
                          and src.suffix.lower() == ".dxf")
            if settings.filter_enabled and not use_filter:
                writer.write("[提示] 该文件不是 DXF，跳过预过滤\n")
            if use_filter:
                writer.stage(f"  1/3 预过滤密集线（阈值 {settings.filter_threshold:g} mm）")
                filtered = UI_TMP_DIR / f"{name}_filtered.dxf"
                old_argv = sys.argv
                sys.argv = ["_filter_dense.py", "-i", str(src), "-o", str(filtered),
                            "-t", f"{settings.filter_threshold:g}"]
                try:
                    import _filter_dense
                    _filter_dense.main()
                finally:
                    sys.argv = old_argv
                conv_in = str(filtered)

            # (2) 转换：调用核心公开函数 dxf_to_gcode
            writer.stage("  2/3 转换为 G 代码")
            out = GCODE_DIR / f"{name}.txt"
            import dxf2gcode
            dxf2gcode.dxf_to_gcode(
                input_path=conv_in, output_path=str(out),
                safe_z=settings.safe_z, cut_depth=settings.cut_depth,
                feed=settings.feed, origin=settings.origin,
                snap_tol=settings.snap_tol, optimize=settings.optimize,
                font_path=settings.font_path, prog_num=assigned,
                arc_r=(settings.arc_mode == "auto"))

            # (3) 预览：直接调用核心的 解析→建轨迹→渲染
            writer.stage("  3/3 生成预览图")
            import preview_gcode
            gcode_events = preview_gcode.parse_gcode_file(str(out))
            if not gcode_events:
                raise RuntimeError("G 代码解析结果为空")
            toolpath = preview_gcode.build_toolpath(gcode_events)
            preview_gcode.render_pillow(toolpath,
                                        str(PREVIEW_DIR / f"{name}_full.png"),
                                        cutting_only=False)
            preview_gcode.render_pillow(toolpath,
                                        str(PREVIEW_DIR / f"{name}_cutting.png"),
                                        cutting_only=True)

            writer.event({"type": "status", "idx": idx,
                          "status": "ok", "prog": assigned})
            writer.event({"type": "image", "path": str(out)})
            ok += 1

        except (SystemExit, Exception) as exc:      # sys.exit / 任何异常都只废掉当前文件
            prog -= 1                               # 归还程序号，保证编号连续
            fail += 1
            writer.event({"type": "status", "idx": idx, "status": "fail"})
            reason = "转换失败（详见上方输出）" if isinstance(exc, SystemExit) else repr(exc)
            writer.write(f"[失败] {Path(path).name}：{reason}，"
                         f"程序号 O{assigned:04d} 已归还\n")

    writer.event({"type": "done", "ok": ok, "fail": fail})
    writer.write(f"━━━ 完成：{ok} 成功，{fail} 失败，共 {ok + fail} 个 ━━━\n")
    writer.flush()
    return 0


# ======================================================================
# C. 引擎：启动 worker 进程 + tail 日志文件（纯逻辑，无 UI，可脱离界面测试）
#    发给界面的事件元组：
#      ("log", 文本) / ("stage", 文本) / ("status", 下标, 状态, 程序号)
#      ("image", gcode路径) / ("done", 成功数, 失败数, 是否被取消)
# ======================================================================

class _LogTailer:
    """增量读取 worker 日志文件，按行分派（字节级偏移 + 半行缓冲）"""

    def __init__(self, emit) -> None:
        self._emit = emit                # 行处理器：engine._on_tail_line
        self._path = None
        self._offset = 0
        self._buf = b""

    def set_file(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._buf = b""

    def pump(self) -> None:
        if self._path is None:
            return
        try:
            with open(self._path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
                if chunk:
                    self._offset = f.tell()
        except OSError:
            return
        if not chunk:
            return
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            self._line(raw.decode("utf-8", "replace").rstrip("\r"))

    def pump_final(self) -> None:
        self.pump()
        if self._buf:
            self._line(self._buf.decode("utf-8", "replace").rstrip("\r"))
            self._buf = b""

    def _line(self, line: str) -> None:
        if line.startswith("@@EVENT "):
            try:
                ev = json.loads(line[8:])
            except json.JSONDecodeError:
                return
            kind = ev.get("type")
            if kind == "status":
                self._emit(("status", ev["idx"], ev["status"], ev.get("prog")))
            elif kind == "image":
                self._emit(("image", ev["path"]))
            elif kind == "done":
                self._emit(("__done", ev.get("ok", 0), ev.get("fail", 0)))
        elif line.startswith("@@STAGE "):
            self._emit(("stage", line[8:]))
        elif line:
            self._emit(("log", line))


class ConversionEngine:
    """在工作线程里运行：起 worker 子进程 → tail 日志 → 转发事件给队列"""

    def __init__(self) -> None:
        self._proc = None                 # worker 进程（供取消用）
        self.cancelled = False

    def cancel(self) -> None:
        """终止 worker（可在任意线程调用）"""
        self.cancelled = True
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    @staticmethod
    def _worker_args(job_file: Path) -> list:
        if FROZEN:
            return [sys.executable, "--worker", str(job_file)]
        return [sys.executable, str(APP_SCRIPT), "--worker", str(job_file)]

    def _on_tail_line(self, ev: tuple) -> None:
        """tailer 的行分派入口：拦截 done 事件做汇总，其余直接转发给界面队列"""
        if ev[0] == "__done":
            self._final_ok, self._final_fail = ev[1], ev[2]
            self._seen_done = True
        else:
            self.q.put(ev)

    def run_job(self, files: list, st: Settings, q: queue.Queue) -> None:
        self.cancelled = False
        self.q = q
        self._seen_done = False
        self._final_ok = self._final_fail = 0
        UI_TMP_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}"
        job_file = UI_TMP_DIR / f"job_{stamp}.json"
        log_file = UI_TMP_DIR / f"job_{stamp}.log"
        job_file.write_text(
            json.dumps({"files": [str(f) for f in files],
                        "settings": asdict(st),
                        "logfile": str(log_file)},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
        log_file.unlink(missing_ok=True)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("MPLBACKEND", "Agg")       # worker 里 matplotlib 无需 GUI 后端
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                self._worker_args(job_file), cwd=str(ROOT_DIR),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, creationflags=flags)
        except OSError as exc:
            q.put(("log", f"[失败] 无法启动工作进程：{exc}"))
            q.put(("done", 0, len(files), False))
            return

        tailer = _LogTailer(self._on_tail_line)
        tailer.set_file(log_file)
        while True:
            tailer.pump()
            if self.cancelled and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                except OSError:
                    pass
            if self._proc.poll() is not None:
                break
            time.sleep(0.1)
        tailer.pump_final()

        if self._seen_done:
            q.put(("done", self._final_ok, self._final_fail, self.cancelled))
        else:
            # worker 被取消/崩溃，没来得及发 done：交给界面兜底汇总
            note = "已取消" if self.cancelled else \
                   f"工作进程异常退出（代码 {self._proc.returncode}）"
            q.put(("log", f"[提示] {note}"))
            q.put(("done", -1, -1, self.cancelled))


# ======================================================================
# D. 界面
# ======================================================================

# 列表状态 -> (显示文本, 颜色)
_STATUS_TEXT = {"pending": ("待转", "#666666"), "running": ("运行中", "#0064c8"),
                "ok": ("成功", "#0a7a1e"), "fail": ("失败", "#c81414")}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)

        # 窗口尺寸按系统 DPI 缩放，保证 125%/150% 缩放屏上视觉大小一致
        try:
            scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            scale = 1.0
        self.geometry(f"{int(1200 * scale)}x{int(720 * scale)}")
        self.minsize(int(960 * scale), int(600 * scale))

        self.settings = Settings.load()
        self.engine = ConversionEngine()
        self.q = queue.Queue()
        self.worker = None
        self.running = False

        # 文件列表：[{path, status, prog}]
        self.items = []
        # 预览浏览状态
        self.preview_paths = []          # 本批次转换成功的 gcode 路径
        self.preview_idx = -1
        self.preview_mode = "full"       # full / cutting
        self._pil_img = None
        self._tk_photo = None
        self._resize_job = None

        self._build_style()
        self._build_layout()
        self._bind_events()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not FROZEN and not (CORE_DIR / "dxf2gcode.py").is_file():
            self.after(200, lambda: messagebox.showerror(
                "缺少核心脚本",
                f"未找到核心脚本：\n{CORE_DIR / 'dxf2gcode.py'}\n"
                "请确认本程序位于项目目录的 ui\\ 文件夹内。"))

    # ---- 界面基础 ----

    def _build_style(self) -> None:
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue

    def _build_layout(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ---------- 左列：文件 + 参数 ----------
        left = ttk.Frame(self, padding=(8, 8, 4, 8))
        left.grid(row=0, column=0, sticky="nsw")

        # ① 待转换文件
        box_files = ttk.LabelFrame(left, text=" ① 待转换文件（DXF / DWF / DWFx）",
                                   padding=6)
        box_files.pack(fill="x")
        self.listbox = tk.Listbox(box_files, height=8, activestyle="none",
                                  exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box_files, command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=sb.set)
        btn_row = ttk.Frame(box_files)
        btn_row.pack(fill="x", pady=(6, 0))
        ttk.Button(btn_row, text="添加文件", command=self._add_files).pack(side="left")
        ttk.Button(btn_row, text="移除选中", command=self._remove_selected).pack(
            side="left", padx=4)
        ttk.Button(btn_row, text="清空", command=self._clear_files).pack(side="left")

        # ② 参数
        box_params = ttk.LabelFrame(left, text=" ② 参数", padding=6)
        box_params.pack(fill="both", expand=True, pady=(8, 0))
        grid = ttk.Frame(box_params)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        def num_entry(r, c, name, label, width=8):
            ttk.Label(grid, text=label).grid(row=r, column=c, sticky="e", padx=(0, 3))
            var = tk.StringVar(value=self._fmt_num(getattr(self.settings, name)))
            e = ttk.Entry(grid, textvariable=var, width=width)
            e.grid(row=r, column=c + 1, sticky="we", pady=2)
            setattr(self, f"var_{name}", var)

        num_entry(0, 0, "safe_z", "安全高 Z")
        num_entry(0, 2, "cut_depth", "下刀深度")
        num_entry(1, 0, "feed", "进给 F")
        num_entry(1, 2, "snap_tol", "吸附容差")
        num_entry(2, 0, "prog_start", "程序号起")
        num_entry(2, 2, "filter_threshold", "过滤阈值")

        ttk.Label(grid, text="加工原点").grid(row=3, column=0, sticky="e", padx=(0, 3))
        self.var_origin = tk.StringVar(
            value=self._origin_label(self.settings.origin))
        ttk.Combobox(grid, textvariable=self.var_origin, state="readonly",
                     values=[lbl for lbl, _ in ORIGIN_CHOICES], width=9).grid(
            row=3, column=1, sticky="we", pady=2)

        # 圆弧格式
        arc_row = ttk.Frame(box_params)
        arc_row.pack(fill="x", pady=(6, 0))
        ttk.Label(arc_row, text="圆弧格式").pack(side="left")
        self.var_arc = tk.StringVar(value=self.settings.arc_mode)
        ttk.Radiobutton(arc_row, text="R 自动（推荐）", value="auto",
                        variable=self.var_arc).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(arc_row, text="强制 I/J", value="ij",
                        variable=self.var_arc).pack(side="left", padx=4)

        # 字体
        font_row = ttk.Frame(box_params)
        font_row.pack(fill="x", pady=(6, 0))
        ttk.Label(font_row, text="文字字体").pack(side="left")
        self.var_font = tk.StringVar(value=self.settings.font_path)
        ttk.Entry(font_row, textvariable=self.var_font).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(font_row, text="浏览", width=5,
                   command=self._browse_font).pack(side="left")

        # 开关
        opt_row = ttk.Frame(box_params)
        opt_row.pack(fill="x", pady=(6, 0))
        self.var_optimize = tk.BooleanVar(value=self.settings.optimize)
        ttk.Checkbutton(opt_row, text="路径优化（少空跑）",
                        variable=self.var_optimize).pack(side="left")
        self.var_filter = tk.BooleanVar(value=self.settings.filter_enabled)
        ttk.Checkbutton(opt_row, text="预过滤密集线",
                        variable=self.var_filter).pack(side="left", padx=(12, 0))

        ttk.Separator(box_params).pack(fill="x", pady=6)
        run_row = ttk.Frame(box_params)
        run_row.pack(fill="x")
        self.btn_start = ttk.Button(run_row, text="开始转换", command=self._start)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_cancel = ttk.Button(run_row, text="取消", command=self._cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=(6, 0))

        ttk.Label(box_params, text=_LIMIT_HINT, foreground="#888888",
                  wraplength=330, font=("Microsoft YaHei UI", 8)).pack(
            fill="x", pady=(8, 0))

        # ---------- 右列：预览 + 日志 ----------
        right = ttk.Frame(self, padding=(4, 8, 8, 8))
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)
        right.columnconfigure(0, weight=1)

        # ③ 预览
        box_prev = ttk.LabelFrame(right, text=" ③ 预览", padding=4)
        box_prev.grid(row=0, column=0, sticky="nsew")
        prev_bar = ttk.Frame(box_prev)
        prev_bar.pack(fill="x")
        self.var_mode = tk.StringVar(value="full")
        ttk.Radiobutton(prev_bar, text="完整轨迹", value="full",
                        variable=self.var_mode,
                        command=lambda: self._show_current()).pack(side="left")
        ttk.Radiobutton(prev_bar, text="纯切削", value="cutting",
                        variable=self.var_mode,
                        command=lambda: self._show_current()).pack(side="left", padx=4)
        ttk.Button(prev_bar, text="◀", width=3,
                   command=lambda: self._step_preview(-1)).pack(side="left", padx=(14, 0))
        ttk.Button(prev_bar, text="▶", width=3,
                   command=lambda: self._step_preview(1)).pack(side="left", padx=2)
        self.lbl_prev_name = ttk.Label(prev_bar, text="（暂无预览）",
                                       foreground="#666666")
        self.lbl_prev_name.pack(side="left", padx=8)
        self.prev_container = ttk.Frame(box_prev)
        self.prev_container.pack(fill="both", expand=True)
        self.lbl_image = ttk.Label(self.prev_container, anchor="center")
        self.lbl_image.pack(fill="both", expand=True)

        # ④ 日志
        box_log = ttk.LabelFrame(right, text=" ④ 日志", padding=4)
        box_log.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.txt_log = tk.Text(box_log, height=10, state="disabled",
                               font=("Consolas", 9), wrap="none",
                               background="#fafafa")
        log_sb = ttk.Scrollbar(box_log, command=self.txt_log.yview)
        self.txt_log.config(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True)
        for tag, color in (("stage", "#0050a0"), ("ok", "#0a7a1e"),
                           ("err", "#c81414")):
            self.txt_log.tag_configure(tag, foreground=color)
            if tag == "stage":
                self.txt_log.tag_configure(tag, font=("Microsoft YaHei UI", 9, "bold"))

        # 状态栏 + 打开目录
        bottom = ttk.Frame(self, padding=(8, 0, 8, 6))
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.lbl_status = ttk.Label(bottom, text="就绪。添加文件后点「开始转换」。")
        self.lbl_status.pack(side="left")
        for text, d in (("打开 input", INPUT_DIR), ("打开 gcode", GCODE_DIR),
                        ("打开 preview", PREVIEW_DIR)):
            ttk.Button(bottom, text=text, width=12,
                       command=lambda dd=d: self._open_dir(dd)).pack(
                side="right", padx=(4, 0))

    def _bind_events(self) -> None:
        self.lbl_image.bind("<Configure>", self._on_image_resize)

    # ---- 文件列表操作 ----

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self, title="选择 CAD 文件",
            initialdir=str(INPUT_DIR if INPUT_DIR.is_dir() else ROOT_DIR),
            filetypes=[("CAD 文件", "*.dxf *.dwf *.dwfx"), ("所有文件", "*.*")])
        added = 0
        for p in paths:
            if Path(p).suffix.lower() not in SUPPORTED_EXT:
                messagebox.showwarning("不支持的格式",
                                       f"已跳过不支持的文件：\n{p}")
                continue
            if any(it["path"] == str(p) for it in self.items):
                continue
            self.items.append({"path": str(p), "status": "pending", "prog": None})
            added += 1
        if added:
            self._render_list()

    def _remove_selected(self) -> None:
        for i in reversed(self.listbox.curselection()):
            del self.items[i]
        self._render_list()

    def _clear_files(self) -> None:
        self.items.clear()
        self._render_list()

    def _render_list(self) -> None:
        self.listbox.delete(0, "end")
        try:
            start = int(float(self.var_prog_start.get()))
        except (tk.TclError, ValueError, AttributeError):
            start = self.settings.prog_start
        for i, it in enumerate(self.items):
            text, color = _STATUS_TEXT[it["status"]]
            if it["prog"] is not None:
                prog_s = f"O{it['prog']:04d}"
            elif it["status"] == "fail":
                prog_s = "—"
            else:
                prog_s = f"O{max(1, min(9999, start + i)):04d}"
            self.listbox.insert(
                "end", f"{i + 1:>2}. {Path(it['path']).name}  →  {prog_s}  {text}")
            self.listbox.itemconfigure(i, foreground=color)

    # ---- 参数辅助 ----

    @staticmethod
    def _fmt_num(v) -> str:
        if isinstance(v, float) and v == int(v):
            return str(int(v))
        return str(v)

    @staticmethod
    def _int_of(var) -> int:
        try:
            return int(float(var.get()))
        except (tk.TclError, ValueError):
            return 0

    @staticmethod
    def _float_of(var) -> float:
        try:
            return float(var.get().strip())
        except (tk.TclError, ValueError):
            return float("nan")

    @staticmethod
    def _origin_label(code: str) -> str:
        for lbl, c in ORIGIN_CHOICES:
            if c == code:
                return lbl
        return ORIGIN_CHOICES[0][0]

    @staticmethod
    def _origin_code(label: str) -> str:
        for lbl, c in ORIGIN_CHOICES:
            if lbl == label:
                return c
        return "br"

    def _collect_settings(self) -> Settings:
        return Settings(
            safe_z=self._float_of(self.var_safe_z),
            cut_depth=self._float_of(self.var_cut_depth),
            feed=self._float_of(self.var_feed),
            origin=self._origin_code(self.var_origin.get()),
            snap_tol=self._float_of(self.var_snap_tol),
            prog_start=self._int_of(self.var_prog_start),
            arc_mode=self.var_arc.get(),
            font_path=self.var_font.get().strip(),
            optimize=self.var_optimize.get(),
            filter_enabled=self.var_filter.get(),
            filter_threshold=self._float_of(self.var_filter_threshold),
        )

    def _browse_font(self) -> None:
        p = filedialog.askopenfilename(
            parent=self, title="选择 TTF / TTC 字体",
            initialdir=r"C:\Windows\Fonts",
            filetypes=[("字体文件", "*.ttf *.ttc"), ("所有文件", "*.*")])
        if p:
            self.var_font.set(p)

    # ---- 转换流程 ----

    def _start(self) -> None:
        if self.running:
            return
        st = self._collect_settings()
        errs = validate_settings(st)
        if errs:
            messagebox.showerror("参数有误", "\n".join(f"· {e}" for e in errs))
            return
        if not self.items:
            messagebox.showinfo("提示", "请先点「添加文件」选择要转换的 CAD 文件")
            return

        self.settings = st
        st.save()

        self.running = True
        self.engine = ConversionEngine()
        self.preview_paths.clear()
        self.preview_idx = -1
        for it in self.items:
            it["status"], it["prog"] = "pending", None
        self._render_list()
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.listbox.config(state="disabled")
        self._log_clear()
        self.lbl_status.config(text="转换中…")

        self.worker = threading.Thread(
            target=self.engine.run_job,
            args=([it["path"] for it in self.items], st, self.q),
            daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        if self.running:
            self.engine.cancel()
            self.lbl_status.config(text="正在取消…")

    def _poll_queue(self) -> None:
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "log":
                    tag = "err" if ev[1].startswith(("[失败", "[错误")) else \
                          ("ok" if ev[1].startswith("[成功") else None)
                    self._log(ev[1], tag)
                elif kind == "stage":
                    self._log(ev[1], "stage")
                elif kind == "status":
                    _, idx, status, prog = ev
                    if 0 <= idx < len(self.items):
                        self.items[idx]["status"] = status
                        self.items[idx]["prog"] = prog
                        self._render_list()
                elif kind == "image":
                    self.preview_paths.append(ev[1])
                    self.preview_idx = len(self.preview_paths) - 1
                    self._show_current()
                elif kind == "done":
                    self._finish(*ev[1:])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _finish(self, ok: int, fail: int, cancelled: bool) -> None:
        self.running = False
        self.btn_start.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.listbox.config(state="normal")
        # worker 被强杀时，正在跑的文件停在"运行中"，这里统一标为失败
        for it in self.items:
            if it["status"] == "running":
                it["status"] = "fail"
        self._render_list()
        if ok < 0:            # 引擎兜底：worker 异常退出 / 取消，没有可靠计数
            tail = "已取消" if cancelled else "工作进程异常退出"
            summary = f"停止（{tail}），请查看日志"
        else:
            tail = "（已取消）" if cancelled else ""
            summary = f"完成{tail}：{ok} 成功，{fail} 失败，共 {ok + fail} 个"
        self._log(f"━━━ {summary} ━━━", "ok" if ok > 0 and fail == 0 and not cancelled
                  else "err")
        self.lbl_status.config(text=summary)

    # ---- 日志 ----

    def _log(self, text: str, tag: str = None) -> None:
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", text + "\n", tag or ())
        # 控制日志长度，防止大图把 Text 撑爆
        if int(self.txt_log.index("end-1c").split(".")[0]) > 8000:
            self.txt_log.delete("1.0", "1000.0")
        self.txt_log.config(state="disabled")
        if self.txt_log.yview()[1] > 0.98:            # 用户没往上翻才自动滚底
            self.txt_log.see("end")

    def _log_clear(self) -> None:
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.config(state="disabled")

    # ---- 预览 ----

    def _step_preview(self, delta: int) -> None:
        if not self.preview_paths:
            return
        self.preview_idx = (self.preview_idx + delta) % len(self.preview_paths)
        self._show_current()

    def _show_current(self) -> None:
        if not self.preview_paths or not (0 <= self.preview_idx <
                                          len(self.preview_paths)):
            self.lbl_prev_name.config(text="（暂无预览）")
            self.lbl_image.config(image="", text="转换完成后这里显示轨迹预览")
            self._tk_photo = None
            return
        self.preview_mode = self.var_mode.get()
        gcode_path = Path(self.preview_paths[self.preview_idx])
        png = PREVIEW_DIR / f"{gcode_path.stem}_{'full' if self.preview_mode == 'full' else 'cutting'}.png"
        self.lbl_prev_name.config(
            text=f"{self.preview_idx + 1}/{len(self.preview_paths)}  {gcode_path.stem}.txt")
        if not HAS_PIL:
            self.lbl_image.config(image="", text="缺少 Pillow，无法内嵌预览\n"
                                                 f"图片位置：{png}")
            return
        if not png.is_file():
            self.lbl_image.config(image="", text=f"预览图尚未生成：\n{png}")
            self._pil_img = None
            return
        try:
            self._pil_img = Image.open(png).convert("RGB")
        except OSError as exc:
            self.lbl_image.config(image="", text=f"预览图读取失败：{exc}")
            return
        self._render_image()

    def _render_image(self) -> None:
        if self._pil_img is None:
            return
        w = max(self.prev_container.winfo_width(), 120) - 8
        h = max(self.prev_container.winfo_height(), 80) - 8
        img = self._pil_img.copy()
        img.thumbnail((w, h))
        self._tk_photo = ImageTk.PhotoImage(img, master=self)
        self.lbl_image.config(image=self._tk_photo, text="")

    def _on_image_resize(self, _event) -> None:
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(150, self._render_image)

    # ---- 其他 ----

    @staticmethod
    def _open_dir(d: Path) -> None:
        d.mkdir(exist_ok=True)
        os.startfile(str(d))                      # 仅 Windows 环境运行

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("正在转换", "转换尚未结束，确定退出并终止转换？"):
                return
            self.engine.cancel()
        try:
            self.settings = self._collect_settings()
        except Exception:
            pass
        self.settings.save()
        self.destroy()


# ======================================================================
# E. 入口
# ======================================================================

def _enable_dpi_awareness() -> None:
    """
    高 DPI 屏适配：让进程感知 DPI（必须在创建任何窗口前调用）。
    不做这一步，Windows 会把整个窗口位图拉伸，字体发虚。
    """
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Per-Monitor DPI
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass                                         # 老系统保持默认


def main() -> None:
    # 工作进程模式：界面进程用 --worker 重启自身，本进程无 UI
    if "--worker" in sys.argv:
        i = sys.argv.index("--worker")
        job_file = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        if not job_file:
            sys.exit(2)
        sys.exit(_worker_main(job_file))

    _enable_dpi_awareness()
    if "--smoke" in sys.argv:
        # 自检模式：构建界面、渲染几帧、自动退出（供自动化测试）
        app = App()
        app.after(600, app.destroy)
        app.mainloop()
        print("SMOKE OK")
        return
    App().mainloop()


if __name__ == "__main__":
    main()
