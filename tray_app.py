#!/usr/bin/env python3
"""
求职面试助手 - 托盘应用
全局热键截图分析，后台静默运行

启动方式:
    python tray_app.py
    python tray_app.py --hotkey "ctrl+shift+a"

依赖:
    pip install pystray pillow easyocr langchain-openai pygetwindow pyperclip keyboard plyer
"""

import os
import sys
import json
import time
import queue
import ctypes
import tempfile
import threading
import winsound
import tkinter as tk
from tkinter import ttk, font as tkfont
from pathlib import Path
from typing import Optional

# ── 确保当前目录在 sys.path 中 ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 导入核心模块（复用所有分析逻辑） ──
from job_coach_cli import (
    analyze_screenshot_core, analyze_screenshot_core_with_feedback,
    extract_text_from_image,
    get_db_connection, get_or_create_company,
    get_or_create_active_session, save_conversation_turn,
    get_recent_context, get_window_by_title, capture_window_region,
    hash_text, init_db, DB_PATH,
    detect_content_type, detect_scene_lightweight,
    analyze_job_screenshot, tailor_resume,
    save_job_analysis, save_resume_version, load_resume_text,
    get_or_create_company_from_window_title,
    HAS_PYSTRAY, llm,
)

import keyboard
import pygetwindow as gw
import pyperclip
from PIL import Image, ImageGrab, ImageEnhance
from PIL import ImageTk
from PIL import ImageDraw as PILDraw

if HAS_PYSTRAY:
    import pystray

# ── 配置管理 ──
CONFIG_DIR = Path.home() / ".job_coach"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "hotkey": "ctrl+shift+x",
    "auto_start": False,
    "default_company_id": None,
    "notification_enabled": True,
    "enable_sound": True,
    "default_mode": "auto",
    "use_vision": True,
    "show_mode_notification": True,
    "enable_feedback": False,
    "user_name": "",
    "user_phone": "",
    "user_email": "",
    "default_resume_path": str(Path.home() / ".job_coach" / "resume.md"),
}

def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2, default=str),
        encoding='utf-8'
    )

config = load_config()


# ── 截图区域记忆 ──
REGIONS_FILE = CONFIG_DIR / "screenshot_regions.json"

def load_regions() -> dict:
    """加载截图区域配置，返回 regions 字典"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if REGIONS_FILE.exists():
        try:
            return json.loads(REGIONS_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {"default": None}

def save_regions(regions: dict):
    """保存截图区域配置"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    safe = {str(k): (v if isinstance(v, (dict, list, type(None))) else None)
            for k, v in regions.items()}
    REGIONS_FILE.write_text(
        json.dumps(safe, ensure_ascii=False, indent=2, default=str),
        encoding='utf-8'
    )

def get_active_region_name() -> str:
    """从 config 获取当前激活的区域名称，默认 'default'"""
    return config.get("active_region", "default")

def set_active_region_name(name: str):
    """保存当前激活的区域名称到 config"""
    config["active_region"] = name
    save_config(config)

def get_current_region() -> Optional[dict]:
    """获取当前激活区域的坐标，若未设置则返回 None"""
    regions = load_regions()
    name = get_active_region_name()
    region = regions.get(name)
    if region and region.get("left") is not None:
        return region
    return None


# ── 自动匹配公司 ──
def auto_match_company(window_title: str) -> Optional[int]:
    """从窗口标题匹配数据库中的公司，返回 company_id 或 None"""
    if not window_title:
        return config.get("default_company_id")

    conn = get_db_connection()
    companies = conn.execute("SELECT id, name FROM companies ORDER BY LENGTH(name) DESC").fetchall()
    conn.close()

    for c in companies:
        name = c["name"]
        if name and len(name) >= 2 and name in window_title:
            return c["id"]

    return config.get("default_company_id")


# ── 框选截图遮罩层 ──
class SelectionOverlay:
    """Snipaste 式全屏遮罩 + 框选截图：矩形内部透明，外部半透明黑色"""

    def __init__(self, parent):
        self.parent = parent
        self.start_x = None
        self.start_y = None
        self.rect_outline_id = None
        self.rect_clear_id = None
        self.info_text_id = None
        self.result_bbox = None

        # 截取全屏作为底图
        self.full_screenshot = ImageGrab.grab(all_screens=True)
        self.sw = self.full_screenshot.width
        self.sh = self.full_screenshot.height

        # 生成 70% 暗化版本（叠加半透明黑色）
        overlay_color = Image.new('RGBA', self.full_screenshot.size, (0, 0, 0, 179))
        self.darkened = Image.alpha_composite(
            self.full_screenshot.convert('RGBA'), overlay_color
        )

        # 全屏窗口（无窗口透明属性，靠图片合成实现透明洞效果）
        self.overlay = tk.Toplevel(parent)
        self.overlay.attributes('-fullscreen', True)
        self.overlay.attributes('-topmost', True)
        self.overlay.configure(bg='#000000')
        self.overlay.config(cursor='cross')

        self.canvas = tk.Canvas(
            self.overlay, bg='#000000',
            highlightthickness=0, cursor='cross'
        )
        self.canvas.pack(fill='both', expand=True)

        # 绘制暗化全屏底图
        self._tk_darkened = ImageTk.PhotoImage(self.darkened)
        self._bg_image_id = self.canvas.create_image(0, 0, anchor='nw', image=self._tk_darkened)

        # 预转换原始截图为 tk PhotoImage 切片（按需更新）
        self._tk_clear_cache = {}

        self.canvas.bind('<ButtonPress-1>', self._on_press)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.overlay.bind('<Escape>', lambda e: self._cancel())

        self.overlay.focus_force()
        self.overlay.lift()
        self.overlay.grab_set()

    def _get_clear_region(self, x1, y1, x2, y2):
        """截取原始截图的矩形区域，转为 tk PhotoImage"""
        x1, x2 = sorted([max(0, min(x1, self.sw)), max(0, min(x2, self.sw))])
        y1, y2 = sorted([max(0, min(y1, self.sh)), max(0, min(y2, self.sh))])
        if x2 <= x1 or y2 <= y1:
            return None
        key = (x1, y1, x2, y2)
        if key not in self._tk_clear_cache:
            crop = self.full_screenshot.crop((x1, y1, x2, y2))
            self._tk_clear_cache[key] = ImageTk.PhotoImage(crop)
        return self._tk_clear_cache[key]

    def _on_press(self, event):
        self.start_x = event.x_root
        self.start_y = event.y_root
        self._clear_overlay_items()
        # 初始状态：0×0 红色矩形框（无填充）
        self.rect_outline_id = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline='#ef4444', width=2, dash=(8, 4)
        )

    def _on_drag(self, event):
        if self.start_x is None:
            return
        x, y = event.x_root, event.y_root
        x1, x2 = min(self.start_x, x), max(self.start_x, x)
        y1, y2 = min(self.start_y, y), max(self.start_y, y)

        # 更新红色边框
        if self.rect_outline_id:
            self.canvas.coords(self.rect_outline_id, x1, y1, x2, y2)

        # 在暗化底图上"挖洞"——覆盖原始清晰截图
        if self.rect_clear_id:
            self.canvas.delete(self.rect_clear_id)
        clear_img = self._get_clear_region(x1, y1, x2, y2)
        if clear_img:
            self.rect_clear_id = self.canvas.create_image(x1, y1, anchor='nw', image=clear_img)
            # 确保层级: 暗化底图 < 清晰区域 < 红色边框
            self.canvas.tag_lower(self._bg_image_id)
            self.canvas.tag_raise(self.rect_outline_id)

        # 更新尺寸标签
        if self.info_text_id:
            if isinstance(self.info_text_id, tuple):
                for tid in self.info_text_id:
                    self.canvas.delete(tid)
            else:
                self.canvas.delete(self.info_text_id)
        label = f'{x2 - x1} × {y2 - y1}'
        lx = x2 + 8
        ly = y2 + 8
        tid_bg = self.canvas.create_text(
            lx + 1, ly + 1, text=label,
            fill='#000000', font=('Microsoft YaHei', 11, 'bold'), anchor='nw'
        )
        tid_fg = self.canvas.create_text(
            lx, ly, text=label,
            fill='#ffffff', font=('Microsoft YaHei', 11, 'bold'), anchor='nw'
        )
        self.info_text_id = (tid_bg, tid_fg)

    def _clear_overlay_items(self):
        for attr in ('rect_outline_id', 'rect_clear_id'):
            item = getattr(self, attr)
            if item:
                self.canvas.delete(item)
                setattr(self, attr, None)
        if self.info_text_id:
            if isinstance(self.info_text_id, tuple):
                for tid in self.info_text_id:
                    self.canvas.delete(tid)
            else:
                self.canvas.delete(self.info_text_id)
            self.info_text_id = None

    def _on_release(self, event):
        if self.start_x is None:
            self.result_bbox = None
        else:
            x1, y1 = min(self.start_x, event.x_root), min(self.start_y, event.y_root)
            x2, y2 = max(self.start_x, event.x_root), max(self.start_y, event.y_root)
            self.result_bbox = (x1, y1, x2, y2) if (x2 - x1 >= 10 and y2 - y1 >= 10) else None
        self.overlay.destroy()

    def _cancel(self):
        self.result_bbox = None
        self.overlay.destroy()

    def run(self) -> Optional[Image.Image]:
        """阻塞等待用户框选，返回截图 Image"""
        self.parent.wait_window(self.overlay)
        if self.result_bbox is None:
            return None
        try:
            return ImageGrab.grab(bbox=self.result_bbox, all_screens=True)
        except Exception:
            return None

    def run_bbox(self) -> Optional[tuple]:
        """阻塞等待用户框选，返回 bbox (left, top, right, bottom) 或 None"""
        self.parent.wait_window(self.overlay)
        return self.result_bbox


def show_selection_overlay(parent) -> Optional[tuple]:
    """弹出框选遮罩，返回 bbox 或 None（供区域设置使用）"""
    overlay = SelectionOverlay(parent)
    return overlay.run_bbox()


def prompt_for_region_name(parent) -> str:
    """弹出输入框让用户输入区域名称，默认使用活动窗口标题"""
    default_name = "default"
    try:
        active = gw.getActiveWindow()
        if active and active.title:
            default_name = active.title[:20]
    except Exception:
        pass

    from tkinter import simpledialog
    name = simpledialog.askstring(
        "区域名称",
        "请为截图区域命名（最多20字符）：",
        initialvalue=default_name,
        parent=parent
    )
    if name and name.strip():
        return name.strip()[:20]
    return default_name


def set_region_interactive(parent, region_name: str = None) -> bool:
    """交互式设置截图区域：框选 → 命名 → 保存 → 激活"""
    bbox = show_selection_overlay(parent)
    if not bbox:
        return False

    if region_name is None:
        region_name = prompt_for_region_name(parent)

    regions = load_regions()
    regions[region_name] = {
        "left": bbox[0], "top": bbox[1],
        "right": bbox[2], "bottom": bbox[3],
        "title": region_name
    }
    save_regions(regions)
    set_active_region_name(region_name)
    show_notification('区域已保存', f'截图区域 "{region_name}" 已设置并激活')
    return True


def capture_selection(parent, use_saved: bool = True) -> Optional[Image.Image]:
    """截图函数：优先使用保存的区域，否则全屏截图"""
    if use_saved:
        region = get_current_region()
        if region:
            bbox = (region['left'], region['top'], region['right'], region['bottom'])
            try:
                return ImageGrab.grab(bbox=bbox, all_screens=True)
            except Exception:
                pass  # 坐标越界，降级为全屏

    return ImageGrab.grab(all_screens=True)


# ── 通知展示 ──
def show_notification(title: str, message: str, timeout: int = 5):
    """尝试 Windows 原生通知，失败则静默"""
    if not config.get("notification_enabled", True):
        return
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message[:256],
            app_name='求职面试助手',
            timeout=timeout
        )
    except Exception:
        pass


def _setup_popup_timer(popup, close_btn, timeout=8):
    """为弹窗设置自动关闭定时器，鼠标悬停暂停"""
    remaining = [timeout]  # 用列表实现闭包引用
    timer_id = [None]

    def _tick():
        remaining[0] -= 1
        if remaining[0] <= 0:
            popup.destroy()
        else:
            close_btn.configure(text=f'✕ {remaining[0]}')
            timer_id[0] = popup.after(1000, _tick)

    def _pause(event):
        if timer_id[0]:
            popup.after_cancel(timer_id[0])
            timer_id[0] = None

    def _resume(event):
        if timer_id[0] is None and popup.winfo_exists():
            timer_id[0] = popup.after(1000, _tick)

    popup.bind('<Enter>', _pause, add='+')
    popup.bind('<Leave>', _resume, add='+')
    timer_id[0] = popup.after(1000, _tick)
    return _pause, _resume


def show_interview_feedback_popup(parent, data: dict):
    """显示面试反馈结果——含原建议/面试官评估/优化建议三栏，12秒自动关闭"""
    popup = tk.Toplevel(parent)
    popup.overrideredirect(True)
    popup.attributes('-topmost', True)
    popup.configure(bg='#313244', bd=1, relief='solid')

    sw, sh = parent.winfo_screenwidth(), parent.winfo_screenheight()
    pw, ph = 480, 520
    popup.geometry(f'{pw}x{ph}+{sw - pw - 20}+{sh - ph - 80}')

    # 标题栏
    title_frame = tk.Frame(popup, bg='#45475a', height=32, cursor='fleur')
    title_frame.pack(fill='x')
    title_frame.pack_propagate(False)
    ttl = tk.Label(
        title_frame, text=f'  面试分析（含评估） {data.get("company_name", "")}',
        fg='#cdd6f4', bg='#45475a', font=tkfont.Font(size=9, weight='bold')
    )
    ttl.pack(side='left', pady=4)

    close_btn = tk.Label(
        title_frame, text='✕ 12', fg='#cdd6f4', bg='#45475a',
        font=tkfont.Font(size=10, weight='bold'), cursor='hand2', padx=10
    )
    close_btn.pack(side='right', pady=4)
    close_btn.bind('<Button-1>', lambda e: popup.destroy())

    _setup_popup_timer(popup, close_btn, timeout=12)

    content = tk.Frame(popup, bg='#1e1e2e')
    content.pack(fill='both', expand=True, padx=10, pady=6)

    perspective = data.get("interviewer_perspective", {})
    original = data.get("original_suggestions", "")
    optimized = data.get("suggestions", "")

    # ── 面试官视角评估（顶部横幅） ──
    score = perspective.get("score", 0)
    assessment = perspective.get("assessment", "无评估")
    score_color = '#a6e3a1' if score >= 70 else ('#f9e2af' if score >= 40 else '#f38ba8')

    assess_frame = tk.Frame(content, bg='#313244')
    assess_frame.pack(fill='x', pady=(0, 8))

    tk.Label(
        assess_frame, text=f' 面试官评分: {score}/100',
        fg=score_color, bg='#313244',
        font=tkfont.Font(size=10, weight='bold')
    ).pack(anchor='w', padx=8, pady=(6, 2))

    tk.Label(
        assess_frame, text=f'  {assessment}',
        fg='#cdd6f4', bg='#313244',
        font=tkfont.Font(size=9), wraplength=440,
        anchor='w', justify='left'
    ).pack(fill='x', padx=8, pady=(0, 2))

    # 优缺点
    strengths = perspective.get("strengths", [])
    weaknesses = perspective.get("weaknesses", [])
    if strengths or weaknesses:
        tags_frame = tk.Frame(assess_frame, bg='#313244')
        tags_frame.pack(fill='x', padx=8, pady=(0, 6))
        for s in strengths[:2]:
            tk.Label(
                tags_frame, text=f'  ✓ {s[:20]}', fg='#a6e3a1', bg='#313244',
                font=tkfont.Font(size=8)
            ).pack(side='left', padx=2)
        for w in weaknesses[:2]:
            tk.Label(
                tags_frame, text=f'  ✗ {w[:20]}', fg='#f38ba8', bg='#313244',
                font=tkfont.Font(size=8)
            ).pack(side='left', padx=2)

    # ── 原始建议 ──
    tk.Label(
        content, text='原始建议', fg='#6c7086', bg='#1e1e2e',
        font=tkfont.Font(size=9, weight='bold')
    ).pack(anchor='w', pady=(4, 2))

    if isinstance(original, str):
        original_lines = [s.strip() for s in original.split('\n') if s.strip()]
    else:
        original_lines = original if isinstance(original, list) else []

    original_text = tk.Text(content, height=3, bg='#313244', fg='#bac2de',
                            font=tkfont.Font(size=9), wrap='word',
                            relief='flat', bd=4, padx=6, pady=4)
    original_text.insert('1.0', '\n'.join(original_lines[:3]) if original_lines else original[:300])
    original_text.configure(state='disabled')
    original_text.pack(fill='x')

    # ── 优化建议 ──
    tk.Label(
        content, text='优化建议', fg='#a6e3a1', bg='#1e1e2e',
        font=tkfont.Font(size=9, weight='bold')
    ).pack(anchor='w', pady=(8, 2))

    if isinstance(optimized, str):
        optimized_lines = [s.strip() for s in optimized.split('\n') if s.strip()]
    else:
        optimized_lines = optimized if isinstance(optimized, list) else []

    optimized_text = tk.Text(content, height=3, bg='#1e1e2e', fg='#cdd6f4',
                             font=tkfont.Font(size=9), wrap='word',
                             relief='flat', bd=4, padx=6, pady=4,
                             highlightbackground='#a6e3a1', highlightthickness=1)
    optimized_text.insert('1.0', '\n'.join(optimized_lines[:3]) if optimized_lines else optimized[:300])
    optimized_text.configure(state='disabled')
    optimized_text.pack(fill='x')

    # ── 原文预览 ──
    ocr_text = data.get("ocr_text", "")
    if ocr_text:
        tk.Frame(content, bg='#45475a', height=1).pack(fill='x', pady=4)
        tk.Label(
            content, text=f'原文: {ocr_text[:100]}',
            fg='#585b70', bg='#1e1e2e',
            font=tkfont.Font(size=8), wraplength=440, justify='left'
        ).pack(fill='x')

    # ── 按钮栏 ──
    btn_frame = tk.Frame(content, bg='#1e1e2e')
    btn_frame.pack(fill='x', pady=(8, 0))

    tk.Button(
        btn_frame, text='复制优化版', bg='#a6e3a1', fg='#1e1e2e', relief='flat',
        font=tkfont.Font(size=9, weight='bold'), padx=10, pady=4,
        command=lambda: _do_copy(optimized, btn_frame)
    ).pack(side='left', padx=2)

    tk.Button(
        btn_frame, text='复制原版', bg='#45475a', fg='#cdd6f4', relief='flat',
        font=tkfont.Font(size=9), padx=10, pady=4,
        command=lambda: _do_copy(original, btn_frame)
    ).pack(side='left', padx=2)

    tk.Button(
        btn_frame, text='关闭', bg='#313244', fg='#cdd6f4', relief='flat',
        font=tkfont.Font(size=9), padx=16, pady=4,
        command=popup.destroy
    ).pack(side='right', padx=2)

    popup.focus_force()
    popup.lift()


def show_result_popup(parent, suggestions, ocr_preview, company_name=""):
    """显示分析结果弹出窗口——右下角无边框，8秒自动关闭"""
    popup = tk.Toplevel(parent)
    popup.overrideredirect(True)
    popup.attributes('-topmost', True)
    popup.configure(bg='#313244', bd=1, relief='solid')

    sw, sh = parent.winfo_screenwidth(), parent.winfo_screenheight()
    pw, ph = 400, 340
    popup.geometry(f'{pw}x{ph}+{sw - pw - 20}+{sh - ph - 80}')

    # 标题栏（拖动 + ✕关闭）
    title_frame = tk.Frame(popup, bg='#45475a', height=32, cursor='fleur')
    title_frame.pack(fill='x')
    title_frame.pack_propagate(False)
    ttl = tk.Label(
        title_frame, text=f'  面试助手分析 {company_name}',
        fg='#cdd6f4', bg='#45475a', font=tkfont.Font(size=9, weight='bold')
    )
    ttl.pack(side='left', pady=4)

    close_btn = tk.Label(
        title_frame, text='✕ 8', fg='#cdd6f4', bg='#45475a',
        font=tkfont.Font(size=10, weight='bold'), cursor='hand2',
        padx=10
    )
    close_btn.pack(side='right', pady=4)
    close_btn.bind('<Button-1>', lambda e: popup.destroy())

    _setup_popup_timer(popup, close_btn)

    # 内容区
    content = tk.Frame(popup, bg='#1e1e2e')
    content.pack(fill='both', expand=True, padx=10, pady=6)

    if isinstance(suggestions, str):
        suggestions = [s.strip() for s in suggestions.split('\n') if s.strip()]

    for i, sug in enumerate(suggestions[:3]):
        row = tk.Frame(content, bg='#1e1e2e')
        row.pack(fill='x', pady=2)

        colors = ['#89b4fa', '#a6e3a1', '#fab387']
        tk.Label(
            row, text=f'{i+1}.', fg=colors[i % 3], bg='#1e1e2e',
            font=tkfont.Font(size=10), width=2
        ).pack(side='left')

        tk.Label(
            row, text=sug[:80], fg='#cdd6f4', bg='#1e1e2e',
            font=tkfont.Font(size=9), anchor='w', wraplength=300, justify='left'
        ).pack(side='left', fill='x', expand=True, padx=4)

        cp = tk.Label(
            row, text='复制', fg='#6c7086', bg='#1e1e2e',
            font=tkfont.Font(size=8), cursor='hand2'
        )
        cp.pack(side='right', padx=2)
        cp.bind('<Button-1>', lambda e, t=sug, w=cp: _do_copy(t, w))

    if ocr_preview:
        tk.Frame(content, bg='#45475a', height=1).pack(fill='x', pady=4)
        tk.Label(
            content, text=f'原文: {ocr_preview[:120]}',
            fg='#585b70', bg='#1e1e2e',
            font=tkfont.Font(size=8), wraplength=360, justify='left'
        ).pack(fill='x')

    popup.focus_force()
    popup.lift()


def _do_copy(text, widget):
    try:
        pyperclip.copy(text)
        orig = widget.cget('fg')
        widget.configure(fg='#1e1e2e')
        widget.after(400, lambda: widget.configure(fg=orig))
    except Exception:
        pass


# ── 岗位分析结果弹窗 ──
def show_job_result_popup(parent, data: dict):
    """显示岗位JD分析结果——右下角无边框，8秒自动关闭"""
    popup = tk.Toplevel(parent)
    popup.overrideredirect(True)
    popup.attributes('-topmost', True)
    popup.configure(bg='#313244', bd=1, relief='solid')

    sw, sh = parent.winfo_screenwidth(), parent.winfo_screenheight()
    pw, ph = 500, 600
    popup.geometry(f'{pw}x{ph}+{sw - pw - 20}+{sh - ph - 80}')

    # 标题栏（拖动 + ✕关闭）
    title_frame = tk.Frame(popup, bg='#45475a', height=32, cursor='fleur')
    title_frame.pack(fill='x')
    title_frame.pack_propagate(False)
    ttl = tk.Label(
        title_frame, text=f'  岗位分析 {data.get("company_name", "")}',
        fg='#cdd6f4', bg='#45475a', font=tkfont.Font(size=9, weight='bold')
    )
    ttl.pack(side='left', pady=4)

    close_btn = tk.Label(
        title_frame, text='✕ 8', fg='#cdd6f4', bg='#45475a',
        font=tkfont.Font(size=10, weight='bold'), cursor='hand2',
        padx=10
    )
    close_btn.pack(side='right', pady=4)
    close_btn.bind('<Button-1>', lambda e: popup.destroy())

    _setup_popup_timer(popup, close_btn)

    # 内容区
    content = tk.Frame(popup, bg='#1e1e2e')
    content.pack(fill='both', expand=True, padx=10, pady=6)

    # 坑位评估
    pitfall = data.get('pitfall_assessment', '')
    if pitfall:
        pitfall_frame = tk.Frame(content, bg='#313244', bd=0, highlightthickness=0)
        pitfall_frame.pack(fill='x', pady=(0, 8))
        tk.Label(
            pitfall_frame, text='⚠ ' + pitfall,
            fg='#f38ba8', bg='#313244',
            font=tkfont.Font(size=9), wraplength=450,
            anchor='w', justify='left'
        ).pack(fill='x', padx=8, pady=6)

    # 匹配度进度条
    match_score = data.get('match_score', 0)
    score_frame = tk.Frame(content, bg='#1e1e2e')
    score_frame.pack(fill='x', pady=4)

    tk.Label(
        score_frame, text='匹配度', fg='#cdd6f4', bg='#1e1e2e',
        font=tkfont.Font(size=10), width=8, anchor='w'
    ).pack(side='left')

    bar_canvas = tk.Canvas(score_frame, height=16, bg='#313244', highlightthickness=0)
    bar_canvas.pack(side='left', fill='x', expand=True, padx=8)

    bar_width = int((match_score / 100) * 350)
    bar_color = '#a6e3a1' if match_score >= 70 else ('#f9e2af' if match_score >= 40 else '#f38ba8')
    bar_canvas.create_rectangle(0, 0, bar_width, 16, fill=bar_color, outline='')
    bar_canvas.create_text(175, 8, text=f'{match_score}%', fill='#1e1e2e', font=tkfont.Font(size=9, weight='bold'))

    # 两列布局：符合项 / 缺口项
    cols = tk.Frame(content, bg='#1e1e2e')
    cols.pack(fill='x', pady=6)

    # 左列：符合项
    left_col = tk.Frame(cols, bg='#1e1e2e')
    left_col.pack(side='left', fill='both', expand=True, padx=(0, 4))

    tk.Label(
        left_col, text='符合项', fg='#a6e3a1', bg='#1e1e2e',
        font=tkfont.Font(size=9, weight='bold')
    ).pack(anchor='w', pady=(0, 2))

    for s in data.get('strengths', [])[:4]:
        tk.Label(
            left_col, text=f'  {s[:40]}', fg='#cdd6f4', bg='#1e1e2e',
            font=tkfont.Font(size=8), anchor='w', wraplength=200
        ).pack(fill='x', pady=1)

    # 右列：缺口项
    right_col = tk.Frame(cols, bg='#1e1e2e')
    right_col.pack(side='left', fill='both', expand=True, padx=(4, 0))

    tk.Label(
        right_col, text='缺口项', fg='#f38ba8', bg='#1e1e2e',
        font=tkfont.Font(size=9, weight='bold')
    ).pack(anchor='w', pady=(0, 2))

    for g in data.get('gaps', [])[:4]:
        tk.Label(
            right_col, text=f'  {g[:40]}', fg='#cdd6f4', bg='#1e1e2e',
            font=tkfont.Font(size=8), anchor='w', wraplength=200
        ).pack(fill='x', pady=1)

    # 简历修改建议（可滚动）
    advice = data.get('resume_advice', '')
    if advice:
        tk.Label(
            content, text='简历修改建议', fg='#89b4fa', bg='#1e1e2e',
            font=tkfont.Font(size=9, weight='bold')
        ).pack(anchor='w', pady=(6, 2))

        advice_text = tk.Text(content, height=4, bg='#313244', fg='#cdd6f4',
                              font=tkfont.Font(size=9), wrap='word',
                              relief='flat', bd=4, padx=6, pady=4)
        advice_text.insert('1.0', advice)
        advice_text.configure(state='disabled')
        advice_text.pack(fill='x')

    # 自荐话术（可滚动）
    self_intro = data.get('self_intro', '')
    if self_intro:
        tk.Label(
            content, text='自荐话术', fg='#fab387', bg='#1e1e2e',
            font=tkfont.Font(size=9, weight='bold')
        ).pack(anchor='w', pady=(6, 2))

        intro_text = tk.Text(content, height=3, bg='#313244', fg='#cdd6f4',
                             font=tkfont.Font(size=9), wrap='word',
                             relief='flat', bd=4, padx=6, pady=4)
        intro_text.insert('1.0', self_intro)
        intro_text.configure(state='disabled')
        intro_text.pack(fill='x')

    # 按钮栏
    btn_frame = tk.Frame(content, bg='#1e1e2e')
    btn_frame.pack(fill='x', pady=(8, 0))

    analysis_id = data.get('analysis_id')

    tk.Button(
        btn_frame, text='复制话术', bg='#45475a', fg='#cdd6f4', relief='flat',
        font=tkfont.Font(size=9), padx=10, pady=4,
        command=lambda: _do_copy(data.get('self_intro', ''), btn_frame)
    ).pack(side='left', padx=2)

    if analysis_id:
        tk.Button(
            btn_frame, text='生成简历', bg='#89b4fa', fg='#1e1e2e', relief='flat',
            font=tkfont.Font(size=9, weight='bold'), padx=10, pady=4,
            command=lambda aid=analysis_id: _do_tailor_resume(parent, aid, config.get('default_resume_path'))
        ).pack(side='left', padx=2)
    else:
        tk.Label(
            btn_frame, text='(未关联公司，无法生成简历)', fg='#585b70', bg='#1e1e2e',
            font=tkfont.Font(size=8)
        ).pack(side='left', padx=6)
        tk.Button(
            btn_frame, text='生成简历', bg='#45475a', fg='#585b70', relief='flat',
            font=tkfont.Font(size=9, weight='bold'), padx=10, pady=4,
            state='disabled'
        ).pack(side='left', padx=2)

    tk.Button(
        btn_frame, text='关闭', bg='#313244', fg='#cdd6f4', relief='flat',
        font=tkfont.Font(size=9), padx=16, pady=4,
        command=popup.destroy
    ).pack(side='right', padx=2)

    popup.focus_force()
    popup.lift()


def _do_tailor_resume(parent, analysis_id, resume_path):
    """后台生成简历 + 显示结果"""
    if not analysis_id:
        show_notification('错误', '未找到分析记录，无法生成简历')
        return

    resume_text = load_resume_text(resume_path)
    if not resume_text:
        show_notification('请先设置简历', '请在托盘菜单中设置默认简历文件路径')
        return

    def _work():
        try:
            result = tailor_resume(analysis_id, resume_path)
            parent.after(0, lambda: show_tailor_result_popup(parent, result))
        except Exception as e:
            parent.after(0, lambda: show_notification('生成失败', str(e)))

    threading.Thread(target=_work, daemon=True).start()


# ── 简历生成结果弹窗 ──
def show_tailor_result_popup(parent, data: dict):
    """显示简历定制结果"""
    popup = tk.Toplevel(parent)
    popup.title('简历定制结果')
    popup.geometry('600x650+150+40')
    popup.attributes('-topmost', True)
    popup.configure(bg='#1e1e2e')

    # 标题
    title_frame = tk.Frame(popup, bg='#313244', height=36)
    title_frame.pack(fill='x')
    title_frame.pack_propagate(False)
    tk.Label(
        title_frame, text='✏️ 简历定制完成', fg='#a6e3a1', bg='#313244',
        font=tkfont.Font(size=11, weight='bold')
    ).pack(side='left', padx=12, pady=6)

    content = tk.Frame(popup, bg='#1e1e2e')
    content.pack(fill='both', expand=True, padx=12, pady=6)

    # 修改说明
    changes = data.get('changes_summary', '')
    if changes:
        tk.Label(
            content, text='📋 修改说明', fg='#89b4fa', bg='#1e1e2e',
            font=tkfont.Font(size=10, weight='bold')
        ).pack(anchor='w', pady=(0, 2))

        changes_text = tk.Text(content, height=4, bg='#313244', fg='#cdd6f4',
                               font=tkfont.Font(size=9), wrap='word',
                               relief='flat', bd=4, padx=6, pady=4)
        changes_text.insert('1.0', changes)
        changes_text.configure(state='disabled')
        changes_text.pack(fill='x', pady=(0, 8))

    # 简历预览
    tk.Label(
        content, text='📄 定制后简历（Markdown）', fg='#a6e3a1', bg='#1e1e2e',
        font=tkfont.Font(size=10, weight='bold')
    ).pack(anchor='w', pady=(0, 2))

    resume_text = tk.Text(content, bg='#313244', fg='#cdd6f4',
                          font=tkfont.Font(size=9), wrap='word',
                          relief='flat', bd=4, padx=8, pady=6)
    resume_text.insert('1.0', data.get('tailored_resume', ''))
    resume_text.configure(state='disabled')

    scrollbar = ttk.Scrollbar(content, command=resume_text.yview)
    resume_text.configure(yscrollcommand=scrollbar.set)
    resume_text.pack(side='left', fill='both', expand=True)
    scrollbar.pack(side='right', fill='y')

    # 保存路径
    output_path = data.get('output_path', '')
    if output_path:
        tk.Label(
            content, text=f'💾 已保存: {output_path}',
            fg='#6c7086', bg='#1e1e2e',
            font=tkfont.Font(size=8)
        ).pack(anchor='w', pady=(4, 0))

    # 按钮栏
    btn_frame = tk.Frame(popup, bg='#1e1e2e')
    btn_frame.pack(fill='x', padx=12, pady=(0, 10))

    tk.Button(
        btn_frame, text='📋 复制', bg='#45475a', fg='#cdd6f4', relief='flat',
        font=tkfont.Font(size=9), padx=10, pady=4,
        command=lambda: _do_copy(data.get('tailored_resume', ''), btn_frame)
    ).pack(side='left', padx=2)

    tk.Button(
        btn_frame, text='📁 打开文件', bg='#45475a', fg='#cdd6f4', relief='flat',
        font=tkfont.Font(size=9), padx=10, pady=4,
        command=lambda: os.startfile(output_path) if output_path and os.path.exists(output_path) else None
    ).pack(side='left', padx=2)

    tk.Button(
        btn_frame, text='关闭', bg='#313244', fg='#cdd6f4', relief='flat',
        font=tkfont.Font(size=9), padx=16, pady=4,
        command=popup.destroy
    ).pack(side='right', padx=2)

    popup.focus_force()
    popup.lift()


# ── 历史记录窗口 ──
_history_window_instance = None

class HistoryWindow:
    def __init__(self, parent=None):
        global _history_window_instance
        # 单例：已存在则聚焦
        if _history_window_instance and _history_window_instance.window.winfo_exists():
            _history_window_instance.show()
            return

        self.window = tk.Toplevel(parent) if parent else tk.Tk()
        self.window.title('面试分析历史')
        self.window.geometry('700x500+100+100')
        self.window.configure(bg='#1e1e2e')
        self.window.attributes('-topmost', True)
        self.window.protocol('WM_DELETE_WINDOW', self._on_close)

        # 工具栏
        toolbar = tk.Frame(self.window, bg='#313244', height=40)
        toolbar.pack(fill='x')
        toolbar.pack_propagate(False)

        tk.Label(
            toolbar, text='📋 对话历史', fg='#cdd6f4', bg='#313244',
            font=tkfont.Font(size=11, weight='bold')
        ).pack(side='left', padx=12, pady=6)

        self.company_var = tk.StringVar(value='全部公司')
        self.company_menu = ttk.Combobox(
            toolbar, textvariable=self.company_var,
            values=['全部公司'], state='readonly', width=18
        )
        self.company_menu.pack(side='right', padx=12, pady=6)
        self.company_menu.bind('<<ComboboxSelected>>', lambda e: self._refresh())

        refresh_btn = tk.Button(
            toolbar, text='刷新', command=self._refresh,
            bg='#45475a', fg='#cdd6f4', relief='flat',
            font=tkfont.Font(size=9), padx=10
        )
        refresh_btn.pack(side='right', pady=6, padx=4)

        # 列表区域（Canvas + Scrollbar）
        list_frame = tk.Frame(self.window, bg='#1e1e2e')
        list_frame.pack(fill='both', expand=True)

        self.canvas = tk.Canvas(list_frame, bg='#1e1e2e', highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.canvas.yview)
        self.scroll_frame = tk.Frame(self.canvas, bg='#1e1e2e')

        self.scroll_frame.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.create_window((0, 0), window=self.scroll_frame, anchor='nw')
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        self.canvas.bind_all('<MouseWheel>', lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units'))

        self._refresh()
        self._load_company_list()
        _history_window_instance = self

    def _on_close(self):
        global _history_window_instance
        _history_window_instance = None
        self.window.destroy()

    def _load_company_list(self):
        conn = get_db_connection()
        companies = conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
        conn.close()
        self.company_menu['values'] = ['全部公司'] + [c['name'] for c in companies]

    def _refresh(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        company_filter = self.company_var.get()

        conn = get_db_connection()
        if company_filter != '全部公司':
            company_row = conn.execute("SELECT id FROM companies WHERE name = ?", (company_filter,)).fetchone()
            if company_row:
                rows = conn.execute(
                    """SELECT ct.*, s.company_id, c.name as company_name
                       FROM conversation_turns ct
                       JOIN interview_sessions s ON ct.session_id = s.id
                       JOIN companies c ON s.company_id = c.id
                       WHERE s.company_id = ?
                       ORDER BY ct.timestamp DESC LIMIT 50""",
                    (company_row['id'],)
                ).fetchall()
            else:
                rows = []
        else:
            rows = conn.execute(
                """SELECT ct.*, s.company_id, c.name as company_name
                   FROM conversation_turns ct
                   JOIN interview_sessions s ON ct.session_id = s.id
                   JOIN companies c ON s.company_id = c.id
                   ORDER BY ct.timestamp DESC LIMIT 50"""
            ).fetchall()
        conn.close()

        if not rows:
            tk.Label(
                self.scroll_frame, text='暂无分析记录。\n按 Ctrl+Shift+X 开始截图分析。',
                fg='#6c7086', bg='#1e1e2e', font=tkfont.Font(size=10)
            ).pack(pady=40)
            return

        for row in rows:
            card = tk.Frame(self.scroll_frame, bg='#313244', bd=0, highlightthickness=0)
            card.pack(fill='x', padx=10, pady=4)

            header = tk.Frame(card, bg='#313244')
            header.pack(fill='x', padx=10, pady=(8, 2))

            tk.Label(
                header, text=f"[{row['company_name']}] {row['timestamp']}",
                fg='#89b4fa', bg='#313244', font=tkfont.Font(size=9)
            ).pack(side='left')

            copy_btn = tk.Label(
                header, text='复制建议', fg='#a6e3a1', bg='#313244',
                font=tkfont.Font(size=8), cursor='hand2'
            )
            copy_btn.pack(side='right')
            copy_btn.bind('<Button-1>', lambda e, t=row['suggestions']: _do_copy(t, copy_btn))

            if row['suggestions']:
                sug_lines = row['suggestions'].split('\n')
                for line in sug_lines[:3]:
                    if line.strip():
                        tk.Label(
                            card, text=f'  • {line.strip()[:80]}',
                            fg='#cdd6f4', bg='#313244',
                            font=tkfont.Font(size=9), anchor='w', wraplength=620, justify='left'
                        ).pack(fill='x', padx=16, pady=1)

            if row['raw_ocr_text']:
                tk.Label(
                    card, text=f'  原文: {row["raw_ocr_text"][:100]}',
                    fg='#585b70', bg='#313244',
                    font=tkfont.Font(size=8), anchor='w', wraplength=620, justify='left'
                ).pack(fill='x', padx=16, pady=(1, 6))

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()


# ── 设置窗口 ──
def show_settings_window(parent=None):
    win = tk.Toplevel(parent) if parent else tk.Tk()
    win.title('设置')
    win.geometry('400x570+200+200')
    win.configure(bg='#1e1e2e')
    win.attributes('-topmost', True)
    win.resizable(False, False)

    cfg = load_config()

    # 标题
    tk.Label(
        win, text='⚙ 设置', fg='#cdd6f4', bg='#1e1e2e',
        font=tkfont.Font(size=12, weight='bold')
    ).pack(pady=(16, 12))

    # 热键说明（只读）
    tk.Label(
        win, text='快捷键（重启生效）:', fg='#a6adc8', bg='#1e1e2e',
        font=tkfont.Font(size=10), anchor='w'
    ).pack(fill='x', padx=20, pady=(6, 2))

    hotkey_info = tk.Frame(win, bg='#313244')
    hotkey_info.pack(fill='x', padx=20, pady=(0, 6))
    tk.Label(
        hotkey_info, text='Ctrl+Shift+Z  自动判断  |  Ctrl+Shift+X  岗位分析  |  Ctrl+Shift+C  面试辅助',
        fg='#cdd6f4', bg='#313244', font=tkfont.Font(size=9), padx=8, pady=6
    ).pack()

    # 默认模式
    mode_frame = tk.Frame(win, bg='#1e1e2e')
    mode_frame.pack(fill='x', padx=20, pady=6)

    tk.Label(
        mode_frame, text='默认模式:', fg='#cdd6f4', bg='#1e1e2e',
        font=tkfont.Font(size=10), width=10, anchor='w'
    ).pack(side='left')

    mode_var = tk.StringVar(value=cfg.get('default_mode', 'auto'))
    mode_menu = ttk.Combobox(
        mode_frame, textvariable=mode_var,
        values=['auto', 'job', 'interview'], state='readonly', width=18
    )
    mode_menu.pack(side='left', padx=8)

    # 多模态开关
    vision_frame = tk.Frame(win, bg='#1e1e2e')
    vision_frame.pack(fill='x', padx=20, pady=6)

    tk.Label(
        vision_frame, text='多模态分析:', fg='#cdd6f4', bg='#1e1e2e',
        font=tkfont.Font(size=10), width=10, anchor='w'
    ).pack(side='left')

    vision_var = tk.BooleanVar(value=cfg.get('use_vision', True))
    vision_check = tk.Checkbutton(
        vision_frame, variable=vision_var,
        bg='#1e1e2e', fg='#cdd6f4',
        selectcolor='#313244', activebackground='#1e1e2e',
        activeforeground='#cdd6f4'
    )
    vision_check.pack(side='left', padx=8)

    # 模式提示开关
    mode_notif_frame = tk.Frame(win, bg='#1e1e2e')
    mode_notif_frame.pack(fill='x', padx=20, pady=6)

    tk.Label(
        mode_notif_frame, text='模式提示:', fg='#cdd6f4', bg='#1e1e2e',
        font=tkfont.Font(size=10), width=10, anchor='w'
    ).pack(side='left')

    mode_notif_var = tk.BooleanVar(value=cfg.get('show_mode_notification', True))
    mode_notif_check = tk.Checkbutton(
        mode_notif_frame, variable=mode_notif_var,
        bg='#1e1e2e', fg='#cdd6f4',
        selectcolor='#313244', activebackground='#1e1e2e',
        activeforeground='#cdd6f4'
    )
    mode_notif_check.pack(side='left', padx=8)

    # 通知开关
    notif_frame = tk.Frame(win, bg='#1e1e2e')
    notif_frame.pack(fill='x', padx=20, pady=6)

    tk.Label(
        notif_frame, text='系统通知:', fg='#cdd6f4', bg='#1e1e2e',
        font=tkfont.Font(size=10), width=10, anchor='w'
    ).pack(side='left')

    notif_var = tk.BooleanVar(value=cfg.get('notification_enabled', True))
    notif_check = tk.Checkbutton(
        notif_frame, variable=notif_var,
        bg='#1e1e2e', fg='#cdd6f4',
        selectcolor='#313244', activebackground='#1e1e2e',
        activeforeground='#cdd6f4'
    )
    notif_check.pack(side='left', padx=8)

    # 提示音开关
    sound_frame = tk.Frame(win, bg='#1e1e2e')
    sound_frame.pack(fill='x', padx=20, pady=6)

    tk.Label(
        sound_frame, text='快捷键提示音:', fg='#cdd6f4', bg='#1e1e2e',
        font=tkfont.Font(size=10), width=10, anchor='w'
    ).pack(side='left')

    sound_var = tk.BooleanVar(value=cfg.get('enable_sound', True))
    sound_check = tk.Checkbutton(
        sound_frame, variable=sound_var,
        bg='#1e1e2e', fg='#cdd6f4',
        selectcolor='#313244', activebackground='#1e1e2e',
        activeforeground='#cdd6f4'
    )
    sound_check.pack(side='left', padx=8)

    # 面试官视角评估开关
    feedback_frame = tk.Frame(win, bg='#1e1e2e')
    feedback_frame.pack(fill='x', padx=20, pady=6)

    tk.Label(
        feedback_frame, text='面试官评估:', fg='#cdd6f4', bg='#1e1e2e',
        font=tkfont.Font(size=10), width=10, anchor='w'
    ).pack(side='left')

    feedback_var = tk.BooleanVar(value=cfg.get('enable_feedback', False))
    feedback_check = tk.Checkbutton(
        feedback_frame, variable=feedback_var,
        bg='#1e1e2e', fg='#cdd6f4',
        selectcolor='#313244', activebackground='#1e1e2e',
        activeforeground='#cdd6f4'
    )
    feedback_check.pack(side='left', padx=8)

    tk.Label(
        feedback_frame,
        text='面试回答 → 面试官评估 → 优化建议',
        fg='#585b70', bg='#1e1e2e',
        font=tkfont.Font(size=8)
    ).pack(side='left', padx=4)

    # 个人信息（用于简历占位符替换）
    tk.Frame(win, bg='#45475a', height=1).pack(fill='x', padx=20, pady=(8, 4))
    tk.Label(
        win, text='个人信息（生成简历时替换占位符）',
        fg='#a6adc8', bg='#1e1e2e',
        font=tkfont.Font(size=9)
    ).pack(anchor='w', padx=20, pady=(4, 2))

    info_fields = [
        ('姓名:', 'user_name', ''),
        ('手机号:', 'user_phone', ''),
        ('邮箱:', 'user_email', ''),
    ]
    info_vars = {}
    for label_text, cfg_key, _ in info_fields:
        info_frame = tk.Frame(win, bg='#1e1e2e')
        info_frame.pack(fill='x', padx=20, pady=3)

        tk.Label(
            info_frame, text=label_text, fg='#cdd6f4', bg='#1e1e2e',
            font=tkfont.Font(size=10), width=10, anchor='w'
        ).pack(side='left')

        var = tk.StringVar(value=cfg.get(cfg_key, ''))
        info_vars[cfg_key] = var
        tk.Entry(
            info_frame, textvariable=var, width=22,
            bg='#313244', fg='#cdd6f4', relief='flat',
            font=tkfont.Font(size=9), insertbackground='#cdd6f4'
        ).pack(side='left', padx=8, ipady=2)

    # 默认公司
    company_frame = tk.Frame(win, bg='#1e1e2e')
    company_frame.pack(fill='x', padx=20, pady=6)

    tk.Label(
        company_frame, text='默认公司:', fg='#cdd6f4', bg='#1e1e2e',
        font=tkfont.Font(size=10), width=10, anchor='w'
    ).pack(side='left')

    conn = get_db_connection()
    companies = conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    conn.close()
    company_names = ['无'] + [c['name'] for c in companies]

    default_company_name = '无'
    if cfg.get('default_company_id'):
        for c in companies:
            if c['id'] == cfg['default_company_id']:
                default_company_name = c['name']
                break

    company_var = tk.StringVar(value=default_company_name)
    company_menu = ttk.Combobox(
        company_frame, textvariable=company_var,
        values=company_names, state='readonly', width=18
    )
    company_menu.pack(side='left', padx=8)

    # 保存按钮
    def _save():
        new_cfg = {
            'hotkey': 'ctrl+shift+x',
            'auto_start': cfg.get('auto_start', False),
            'default_company_id': None,
            'default_mode': mode_var.get(),
            'use_vision': vision_var.get(),
            'show_mode_notification': mode_notif_var.get(),
            'notification_enabled': notif_var.get(),
            'enable_sound': sound_var.get(),
            'enable_feedback': feedback_var.get(),
            'user_name': info_vars['user_name'].get(),
            'user_phone': info_vars['user_phone'].get(),
            'user_email': info_vars['user_email'].get(),
        }
        if company_var.get() != '无':
            for c in companies:
                if c['name'] == company_var.get():
                    new_cfg['default_company_id'] = c['id']
                    break
        save_config(new_cfg)
        global config
        config = new_cfg
        win.destroy()
        show_notification('设置已保存', f'默认模式: {mode_var.get()}')

    tk.Button(
        win, text='保存', command=_save,
        bg='#89b4fa', fg='#1e1e2e', relief='flat',
        font=tkfont.Font(size=10, weight='bold'), padx=30, pady=6,
        activebackground='#74c7ec', activeforeground='#1e1e2e'
    ).pack(pady=16)

    win.focus_force()
    win.lift()


# ── 托盘应用 ──
class TrayApplication:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()

        self.config = load_config()
        self._running = True
        self._analyzing = False  # 分析状态锁，防止重复触发
        self.current_mode = self.config.get('default_mode', 'auto')

        # 队列：分析结果 → 主线程展示
        self.result_queue = queue.Queue()

        # 托盘图标（在后台线程运行）
        if HAS_PYSTRAY:
            self.icon_image = self._make_icon_image()
            self.tray_icon = None
        else:
            self.icon_image = None
            self.tray_icon = None

        # 启动热键监听
        self._start_hotkey()

        # 定期处理结果队列
        self._poll_results()

    def _make_icon_image(self):
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d = PILDraw.Draw(img)
        d.rounded_rectangle([6, 10, 56, 44], radius=6, fill='#89b4fa', outline='#45475a', width=2)
        d.polygon([(22, 44), (16, 56), (30, 44)], fill='#89b4fa')
        return img

    def _start_hotkey(self):
        hotkeys = {
            'ctrl+shift+z': 'auto',
            'ctrl+shift+x': 'job',
            'ctrl+shift+c': 'interview',
        }
        for hotkey, mode in hotkeys.items():
            try:
                keyboard.add_hotkey(
                    hotkey,
                    lambda m=mode: self._on_hotkey(m),
                    suppress=False
                )
                print(f"热键已注册: {hotkey} (模式: {mode})")
            except Exception as e:
                print(f"热键注册失败 ({hotkey}): {e}")
        if any('register' not in str(e) for e in []):
            print("尝试以管理员权限运行以获得全局热键支持。")

    def _on_hotkey(self, mode='auto'):
        """热键触发（在 keyboard 后台线程中调用）"""
        if self.config.get('enable_sound', True):
            try:
                winsound.MessageBeep()
            except Exception:
                pass
        self.root.after(0, self._do_capture_workflow, mode)

    def _do_capture_workflow(self, mode='auto'):
        """截图 → 分析 → 通知 完整流程（在主线程中运行）"""
        # 分析状态锁：防止连续快捷键触发多个分析
        if self._analyzing:
            print("[状态] 上一个分析任务尚未完成，忽略本次请求")
            if self.config.get('notification_enabled', True):
                self.root.after(0, lambda: show_notification(
                    '求职面试助手', '分析中，请稍候再试...', timeout=2))
            return

        # 更新当前模式（跟随快捷键）
        self.current_mode = mode
        mode_names = {'auto': '自动', 'job': '岗位', 'interview': '面试'}
        mode_name = mode_names.get(mode, mode)

        try:
            # 反馈：通知 + 鼠标指针
            if self.config.get('notification_enabled', True):
                if self.config.get('show_mode_notification', True):
                    show_notification('求职面试助手', f'[{mode_name}模式] 正在截图...', timeout=2)
                else:
                    show_notification('求职面试助手', '正在截图...', timeout=2)
            self.root.config(cursor='watch')
            self.root.update_idletasks()
            self.root.after(300, lambda: self.root.config(cursor=''))

            # 1. 获取当前活动窗口标题
            window_title = ""
            try:
                active_win = gw.getActiveWindow()
                if active_win:
                    window_title = active_win.title
            except Exception:
                pass

            # 2. 隐藏可能的悬浮窗，避免截到自身
            for w in list(self.root.winfo_children()):
                try:
                    if isinstance(w, tk.Toplevel) and w.winfo_exists() and w.winfo_viewable():
                        w.withdraw()
                except Exception:
                    pass
            self.root.update_idletasks()

            # 3. 截图（优先使用保存的区域，首次或重置后弹出框选）
            image = capture_selection(self.root, use_saved=True)
            if image is None:
                return

            # 4. 自动匹配公司
            company_id = auto_match_company(window_title)
            company_name = ""
            if company_id:
                conn = get_db_connection()
                row = conn.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()
                conn.close()
                if row:
                    company_name = f"({row['name']})"

            # 5. 保存临时文件
            tmp_path = Path(tempfile.gettempdir()) / "job_coach_temp.png"
            image.save(str(tmp_path))

            # 6. 后台分析（避免阻塞 UI）
            self._analyzing = True
            threading.Thread(
                target=self._analyze_in_background,
                args=(str(tmp_path), company_id, window_title, company_name, mode),
                daemon=True
            ).start()

        except Exception as e:
            print(f"截图分析出错: {e}")
        finally:
            self.root.config(cursor='')

    def _analyze_in_background(self, tmp_path, company_id, window_title, company_name, mode='auto'):
        """后台分析：根据模式执行 OCR / 多模态分析"""
        t_start = time.time()
        use_vision = self.config.get('use_vision', True)
        enable_feedback = self.config.get('enable_feedback', False)
        t_ocr_elapsed = 0

        try:
            # 模式路由：确定内容类型和分析方式
            if mode == 'job':
                content_type = 'job'
                skip_ocr = use_vision
            elif mode == 'interview':
                content_type = 'interview'
                skip_ocr = use_vision
            else:  # auto 模式：需要先判断场景
                skip_ocr = False
                # 轻量级场景判断（基于图片顶部 1/3 OCR）
                if self.config.get('notification_enabled', True):
                    self.root.after(0, lambda: show_notification(
                        '求职面试助手', '正在判断场景...', timeout=3))
                t_scene = time.time()
                content_type = detect_scene_lightweight(tmp_path)
                print(f"[计时] 场景判断: {content_type}, 耗时 {time.time() - t_scene:.2f}s")
                if use_vision:
                    skip_ocr = True

            if skip_ocr:
                # ── 多模态分析路径 ──
                if self.config.get('notification_enabled', True):
                    self.root.after(0, lambda: show_notification(
                        '求职面试助手', '正在分析截图...', timeout=3))
                t_llm = time.time()
                if content_type == 'job':
                    resume_text = load_resume_text(self.config.get('default_resume_path'))
                    result = analyze_job_screenshot(
                        tmp_path, company_id=company_id,
                        original_resume=resume_text,
                        window_title=window_title, use_vision=True
                    )
                    print(f"[计时] 多模态岗位分析: 耗时 {time.time() - t_llm:.2f}s")
                    if result.get("success"):
                        detected_cid = result.get("company_id")
                        if not company_name and detected_cid:
                            conn = get_db_connection()
                            row = conn.execute("SELECT name FROM companies WHERE id = ?", (detected_cid,)).fetchone()
                            conn.close()
                            company_name = f"({row['name']})" if row else ""
                        self.result_queue.put({
                            "type": "job", "data": result,
                            "company_name": company_name, "window_title": window_title,
                        })
                    else:
                        err_msg = result.get("pitfall_assessment") or "分析失败"
                        self.result_queue.put({"type": "error", "message": err_msg})
                else:
                    if enable_feedback:
                        # ── 面试反馈模式（多模态） ──
                        from vision_analyzer import analyze_interview_with_feedback
                        feedback_result = analyze_interview_with_feedback(tmp_path)
                        print(f"[计时] 多模态面试+反馈: 耗时 {time.time() - t_llm:.2f}s")
                        if feedback_result.get("success"):
                            self.result_queue.put({
                                "type": "interview_feedback",
                                "original_suggestions": feedback_result.get("original_suggestions", ""),
                                "suggestions": feedback_result.get("optimized_suggestions", ""),
                                "analysis": feedback_result.get("analysis", ""),
                                "interviewer_perspective": feedback_result.get("interviewer_perspective", {}),
                                "ocr_text": "",
                                "company_name": company_name,
                                "window_title": window_title,
                            })
                        else:
                            self.result_queue.put({"type": "error", "message": "面试分析失败"})
                    else:
                        result = analyze_screenshot_core(
                            tmp_path, company_id=company_id,
                            window_title=window_title, use_vision=True
                        )
                        print(f"[计时] 多模态面试分析: 耗时 {time.time() - t_llm:.2f}s")
                        if result.get("success"):
                            detected_cid = result.get("company_id")
                            if not company_name and detected_cid:
                                conn = get_db_connection()
                                row = conn.execute("SELECT name FROM companies WHERE id = ?", (detected_cid,)).fetchone()
                                conn.close()
                                company_name = f"({row['name']})" if row else ""
                            self.result_queue.put({
                                "type": "interview",
                                "suggestions": result.get("suggestions", ""),
                                "analysis": result.get("analysis", ""),
                                "ocr_text": result.get("ocr_text", ""),
                                "company_name": company_name,
                                "window_title": window_title,
                            })
                        else:
                            self.result_queue.put({"type": "error", "message": "未能识别文字"})
            else:
                # ── OCR + LLM 路径 ──
                if self.config.get('notification_enabled', True):
                    self.root.after(0, lambda: show_notification(
                        '求职面试助手', '正在识别文字...', timeout=3))

                # 图片缩小 50% 加速 OCR
                t_img = time.time()
                try:
                    img = Image.open(tmp_path)
                    w, h = img.size
                    img = img.resize((w // 2, h // 2), Image.LANCZOS)
                    img.save(tmp_path)
                    print(f"[计时] 图片缩小: {w}x{h} → {w//2}x{h//2}, "
                          f"耗时 {time.time() - t_img:.2f}s")
                except Exception as e:
                    print(f"[计时] 图片缩小失败: {e}")

                # OCR 识别
                t_ocr = time.time()
                ocr_text = extract_text_from_image(tmp_path)
                t_ocr_elapsed = time.time() - t_ocr
                print(f"[计时] OCR 识别: {len(ocr_text)} 字符, 耗时 {t_ocr_elapsed:.2f}s")

                if not ocr_text or not ocr_text.strip():
                    self.result_queue.put({"type": "error", "message": "未能识别文字"})
                    return

                # auto 模式下的内容类型检测
                if mode == 'auto':
                    t_type = time.time()
                    content_type = detect_content_type(ocr_text)
                    print(f"[计时] 内容类型检测: {content_type}, 耗时 {time.time() - t_type:.2f}s")

                t_llm = time.time()
                if content_type == 'job':
                    resume_text = load_resume_text(self.config.get('default_resume_path'))
                    result = analyze_job_screenshot(
                        tmp_path, company_id=company_id,
                        original_resume=resume_text, window_title=window_title
                    )
                    print(f"[计时] LLM 岗位分析: 耗时 {time.time() - t_llm:.2f}s")
                    if result.get("success"):
                        detected_cid = result.get("company_id")
                        if not company_name and detected_cid:
                            conn = get_db_connection()
                            row = conn.execute("SELECT name FROM companies WHERE id = ?", (detected_cid,)).fetchone()
                            conn.close()
                            company_name = f"({row['name']})" if row else ""
                        self.result_queue.put({
                            "type": "job", "data": result,
                            "company_name": company_name, "window_title": window_title,
                        })
                    else:
                        err_msg = result.get("pitfall_assessment") or result.get("ocr_text") or "未能识别文字"
                        self.result_queue.put({"type": "error", "message": err_msg})
                else:
                    if enable_feedback:
                        result = analyze_screenshot_core_with_feedback(
                            tmp_path, company_id=company_id, window_title=window_title
                        )
                        print(f"[计时] LLM 面试+反馈: 耗时 {time.time() - t_llm:.2f}s")
                        if result.get("success"):
                            detected_cid = result.get("company_id")
                            if not company_name and detected_cid:
                                conn = get_db_connection()
                                row = conn.execute("SELECT name FROM companies WHERE id = ?", (detected_cid,)).fetchone()
                                conn.close()
                                company_name = f"({row['name']})" if row else ""
                            self.result_queue.put({
                                "type": "interview_feedback",
                                "original_suggestions": result.get("original_suggestions", ""),
                                "suggestions": result.get("optimized_suggestions", ""),
                                "analysis": result.get("analysis", ""),
                                "interviewer_perspective": result.get("interviewer_perspective", {}),
                                "ocr_text": result.get("ocr_text", ""),
                                "company_name": company_name,
                                "window_title": window_title,
                            })
                        else:
                            self.result_queue.put({"type": "error", "message": "未能识别文字"})
                    else:
                        result = analyze_screenshot_core(
                            tmp_path, company_id=company_id, window_title=window_title
                        )
                        print(f"[计时] LLM 面试分析: 耗时 {time.time() - t_llm:.2f}s")
                        if result.get("success"):
                            detected_cid = result.get("company_id")
                            if not company_name and detected_cid:
                                conn = get_db_connection()
                                row = conn.execute("SELECT name FROM companies WHERE id = ?", (detected_cid,)).fetchone()
                                conn.close()
                                company_name = f"({row['name']})" if row else ""
                            self.result_queue.put({
                                "type": "interview",
                                "suggestions": result.get("suggestions", ""),
                                "analysis": result.get("analysis", ""),
                                "ocr_text": result.get("ocr_text", ""),
                                "company_name": company_name,
                                "window_title": window_title,
                            })
                        else:
                            self.result_queue.put({"type": "error", "message": "未能识别文字"})

            print(f"[计时] 总耗时: {time.time() - t_start:.2f}s "
                  f"(OCR: {t_ocr_elapsed:.2f}s)")

        except Exception as e:
            self.result_queue.put({
                "type": "error",
                "message": str(e),
            })
        finally:
            self._analyzing = False  # 释放分析锁
            try:
                Path(tmp_path).unlink()
            except Exception:
                pass

    def _poll_results(self):
        """定期检查分析结果队列并展示"""
        try:
            while True:
                data = self.result_queue.get_nowait()
                if data["type"] == "interview":
                    suggestions = data["suggestions"]
                    first_suggestion = ""
                    if isinstance(suggestions, str):
                        first_suggestion = suggestions.split('\n')[0][:60] if suggestions else "分析完成"
                    elif isinstance(suggestions, list) and suggestions:
                        first_suggestion = suggestions[0][:60]

                    show_notification(
                        f'面试助手 {data["company_name"]}',
                        first_suggestion
                    )
                    show_result_popup(
                        self.root,
                        data["suggestions"],
                        data["ocr_text"],
                        data["company_name"]
                    )
                elif data["type"] == "interview_feedback":
                    show_notification(
                        f'面试助手(含评估) {data["company_name"]}',
                        '分析完成，含面试官视角评估'
                    )
                    show_interview_feedback_popup(self.root, data)
                elif data["type"] == "job":
                    result_data = data["data"]
                    match_score = result_data.get('match_score', 0)
                    pitfall = result_data.get('pitfall_assessment', '')
                    summary = f'匹配度: {match_score}%'
                    if pitfall:
                        summary += f' ⚠{pitfall[:30]}'
                    show_notification(
                        f'岗位分析 {data["company_name"]}',
                        summary
                    )
                    show_job_result_popup(self.root, {
                        **result_data,
                        "company_name": data["company_name"],
                        "window_title": data["window_title"],
                    })
                elif data["type"] == "error":
                    show_notification('求职助手', data.get("message", "分析失败"))

        except queue.Empty:
            pass

        if self._running:
            self.root.after(300, self._poll_results)

    def _on_history(self):
        HistoryWindow(self.root)

    def _on_settings(self):
        show_settings_window(self.root)

    def _on_exit(self):
        self._running = False
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        if self.tray_icon and HAS_PYSTRAY:
            self.tray_icon.stop()
        # 必须在主线程销毁 tk root
        self.root.after(200, self.root.destroy)

    def _on_set_region(self):
        """立即框选，保存到当前激活的区域"""
        set_region_interactive(self.root, get_active_region_name())

    def _on_new_region(self):
        """框选并创建新区域"""
        set_region_interactive(self.root, region_name=None)

    def _on_overwrite_region(self, region_name):
        """覆盖指定区域"""
        set_region_interactive(self.root, region_name)

    def _on_switch_region(self, region_name):
        """切换到指定区域"""
        regions = load_regions()
        region = regions.get(region_name)
        title = region.get("title", region_name) if region else region_name
        set_active_region_name(region_name)
        show_notification('区域已切换', f'当前截图区域: {title}')

    def _on_reset_region(self):
        """删除当前区域的坐标配置，下次使用时重新框选"""
        name = get_active_region_name()
        regions = load_regions()
        if name in regions:
            regions[name] = None
            save_regions(regions)
            show_notification('区域已重置', f'"{name}" 区域已重置，下次使用将重新框选')

    def _on_set_resume(self):
        """打开文件选择器设置默认简历路径"""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title='选择简历 Markdown 文件',
            filetypes=[('Markdown 文件', '*.md'), ('所有文件', '*.*')],
            initialdir=str(Path.home())
        )
        if path:
            self.config['default_resume_path'] = path
            save_config(self.config)
            show_notification('简历已设置', f'默认简历: {Path(path).name}')

    def _on_open_resume_folder(self):
        """打开简历文件夹"""
        folder = Path.home() / ".job_coach"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def _on_switch_mode(self, mode):
        """切换到指定分析模式"""
        mode_names = {'auto': '自动判断', 'job': '岗位分析', 'interview': '面试辅助'}
        self.current_mode = mode
        show_notification('模式已切换', f'{mode_names.get(mode, mode)} 模式')

    def _make_tray_menu(self):
        regions = load_regions()
        active_name = get_active_region_name()

        # 切换区域子菜单
        switch_items = []
        for name, r in regions.items():
            if r and r.get("left") is not None:
                prefix = "✓ " if name == active_name else "  "
                title = r.get("title", name)
                switch_items.append(
                    pystray.MenuItem(
                        f'{prefix}{title}',
                        lambda n=name: self.root.after(0, self._on_switch_region, n)
                    )
                )
        if not switch_items:
            switch_items.append(pystray.MenuItem('(无已保存区域)', None, enabled=False))

        # 覆盖区域子菜单
        overwrite_items = [pystray.MenuItem('新建区域...', lambda: self.root.after(0, self._on_new_region))]
        saved_names = [name for name, r in regions.items() if r and r.get("left") is not None]
        if saved_names:
            overwrite_items.append(pystray.Menu.SEPARATOR)
            for name in saved_names:
                title = regions[name].get("title", name)
                overwrite_items.append(
                    pystray.MenuItem(
                        f'覆盖"{title}"',
                        lambda n=name: self.root.after(0, self._on_overwrite_region, n)
                    )
                )

        # 模式切换子菜单
        mode_names = {'auto': '自动判断 (Ctrl+Shift+Z)', 'job': '岗位分析 (Ctrl+Shift+X)', 'interview': '面试辅助 (Ctrl+Shift+C)'}
        mode_items = []
        for m, label in mode_names.items():
            prefix = "✓ " if self.current_mode == m else "  "
            mode_items.append(
                pystray.MenuItem(
                    f'{prefix}{label}',
                    lambda mode=m: self.root.after(0, self._on_switch_mode, mode)
                )
            )

        items = [
            pystray.MenuItem('📸 设置截图区域', lambda: self.root.after(0, self._on_set_region)),
            pystray.MenuItem('📸 设置截图区域为...', pystray.Menu(*overwrite_items)),
            pystray.MenuItem('📁 切换截图区域', pystray.Menu(*switch_items)),
            pystray.MenuItem('🔄 重置当前区域', lambda: self.root.after(0, self._on_reset_region)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('🎯 分析模式', pystray.Menu(*mode_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('📄 设置默认简历', lambda: self.root.after(0, self._on_set_resume)),
            pystray.MenuItem('📁 打开简历文件夹', lambda: self.root.after(0, self._on_open_resume_folder)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('📋 查看历史', lambda: self.root.after(0, self._on_history), default=True),
            pystray.MenuItem('⚙ 设置', lambda: self.root.after(0, self._on_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('❌ 退出', self._on_exit),
        ]
        return pystray.Menu(*items)

    def run(self):
        print(f"求职面试助手已启动")
        print(f"全局热键:")
        print(f"  Ctrl+Shift+Z → 自动判断模式")
        print(f"  Ctrl+Shift+X → 岗位分析模式")
        print(f"  Ctrl+Shift+C → 面试辅助模式")
        print(f"当前模式: {self.current_mode}")
        print(f"按热键进行截图分析，右键托盘图标查看更多选项。")

        # 启动托盘（后台线程）
        if HAS_PYSTRAY:
            self.tray_icon = pystray.Icon(
                'job_coach',
                self.icon_image,
                '求职面试助手',
                self._make_tray_menu()
            )
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        else:
            print("警告: pystray 未安装，托盘功能不可用。")
            print("pip install pystray")

        # 运行 tkinter 主循环
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._running = False
            if self.tray_icon:
                self.tray_icon.stop()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='求职面试助手 - 托盘应用')
    parser.add_argument('--hotkey', default=None, help='全局热键，默认 ctrl+shift+x')
    args = parser.parse_args()

    if args.hotkey:
        config['hotkey'] = args.hotkey
        save_config(config)

    # 确保数据库初始化
    init_db()

    app = TrayApplication()
    app.run()


if __name__ == '__main__':
    main()
