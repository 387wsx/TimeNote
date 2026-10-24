# -*- coding: utf-8 -*-
"""
极简倒计时提醒器（改进版）—— Windows 10 / 11

保留原版全部核心功能：
  · 设定倒计时 → 到点弹窗 → 显示 N 秒 → 消失 → 循环，直到停止
  · 弹窗置顶、不抢焦点、不进 Alt+Tab
  · 自定义文字 / 弹出位置（九宫格+随机）/ 窗口大小（屏幕 1%~25%）
  · 配置自动保存到用户目录

相较原版，针对五个维度做了优化（改动清单见文件末尾）：
  1. 代码结构   —— 拆分 Win32 封装 / 配置模型 / 状态机 / UI，消除 500 行巨类
  2. 计时精度   —— time.monotonic() 截止时刻调度，消除漂移并记录回调延迟
  3. 状态管理   —— 显式状态机 + 单一 after 句柄，杜绝多个定时器竞争
  4. 用户交互   —— 弹窗右键停止、输入即时校验、运行状态提示
  5. 异常处理   —— logging 日志、配置字段校验、Win32 返回值检查

运行：  python reminder_improved.py
打包：  pyinstaller -F -w reminder_improved.py
"""

import ctypes
import ctypes.wintypes as wt
import json
import logging
import math
import os
import random
import re
import shutil
import sys
import threading
import queue
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, time as dtime
from enum import Enum
from tkinter import messagebox, colorchooser, filedialog, simpledialog

# ==========================================================
#  日志（改进：统一记录，打包 -w 无控制台时也能排障）
#  容错：日志文件打不开时回退到程序目录，再不行静默，绝不拖垮主程序。
# ==========================================================
log = logging.getLogger("countdown_reminder")


def _setup_logging():
    candidates = [
        os.path.join(os.path.expanduser("~"), ".countdown_reminder.log"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     ".countdown_reminder.log"),
    ]
    handler = None
    for path in candidates:
        try:
            handler = logging.FileHandler(path, encoding="utf-8")
            break
        except Exception:
            continue
    if handler is None:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


_setup_logging()


# ==========================================================
#  Win32 接口（改进：返回值检查 + 工作区回退）
# ==========================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE      = -20
WS_EX_TOPMOST    = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

HWND_TOPMOST   = -1
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE     = 0x0001
SWP_NOMOVE     = 0x0002
SWP_NOZORDER   = 0x0004
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040

GA_ROOT         = 2
SPI_GETWORKAREA = 0x0030
SM_CXSCREEN     = 0
SM_CYSCREEN     = 1

if ctypes.sizeof(ctypes.c_void_p) == 8:
    _GetWindowLong = user32.GetWindowLongPtrW
    _SetWindowLong = user32.SetWindowLongPtrW
    _GetClassLong  = user32.GetClassLongPtrW
    _SetClassLong  = user32.SetClassLongPtrW
    _LONG = ctypes.c_ssize_t
else:
    _GetWindowLong = user32.GetWindowLongW
    _SetWindowLong = user32.SetWindowLongW
    _GetClassLong  = user32.GetClassLongW
    _SetClassLong  = user32.SetClassLongW
    _LONG = ctypes.c_long

_GetWindowLong.restype  = _LONG
_GetWindowLong.argtypes = [wt.HWND, ctypes.c_int]
_SetWindowLong.restype  = _LONG
_SetWindowLong.argtypes = [wt.HWND, ctypes.c_int, _LONG]
_GetClassLong.restype   = _LONG
_GetClassLong.argtypes  = [wt.HWND, ctypes.c_int]
_SetClassLong.restype   = _LONG
_SetClassLong.argtypes  = [wt.HWND, ctypes.c_int, _LONG]

GCL_STYLE      = -26
CS_DROPSHADOW  = 0x00020000

user32.SetWindowPos.restype  = ctypes.c_bool
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetAncestor.restype  = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
user32.SystemParametersInfoW.restype  = ctypes.c_bool
user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_uint]
user32.GetSystemMetrics.restype  = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype  = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

TOPMOST_HWND = wt.HWND(-1)


def root_hwnd(widget):
    """取 Tk 控件对应的顶层窗口句柄"""
    h = widget.winfo_id()
    if not h:
        return 0
    r = user32.GetAncestor(wt.HWND(h), GA_ROOT)
    v = getattr(r, "value", r)
    return int(v) if v else h


def make_noactivate(hwnd):
    """置顶 / 不进 Alt+Tab / 永不抢焦点"""
    h = wt.HWND(hwnd)
    ex = _GetWindowLong(h, GWL_EXSTYLE)
    _SetWindowLong(h, GWL_EXSTYLE,
                   ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)


def enable_drop_shadow(hwnd):
    """给 TkTopLevel 类开启系统投影，之后创建的弹窗自带一层柔光。

    类样式对“之后创建”的窗口生效，所以要在创建弹窗之前调用；
    主窗口已经创建，不受影响。无边框弹窗靠这层投影和桌面分开。
    """
    try:
        h = wt.HWND(hwnd)
        style = _GetClassLong(h, GCL_STYLE)
        if not style & CS_DROPSHADOW:
            _SetClassLong(h, GCL_STYLE, style | CS_DROPSHADOW)
    except Exception as e:
        log.warning("开启窗口投影失败：%s", e)


def notification_busy():
    """全屏游戏 / 演示 / 专注模式下返回 True，提醒可延后。"""
    try:
        state = ctypes.c_int()
        ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        # 只对真正的全屏（D3D / 演示模式）延后，QUNS_BUSY 在普通桌面也会误报
        return state.value in (3, 4)
    except Exception:
        return False


def set_pos(hwnd, x, y, w, h):
    return user32.SetWindowPos(wt.HWND(hwnd), TOPMOST_HWND,
                               int(x), int(y), int(w), int(h),
                               SWP_NOACTIVATE | SWP_SHOWWINDOW)


def work_area():
    """主显示器工作区（已排除任务栏）；失败时回退到全屏尺寸"""
    r = wt.RECT()
    ok = user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    if ok:
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    log.warning("获取工作区失败，回退到全屏尺寸")
    return 0, 0, user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)


# ==========================================================
#  外观常量（Windows 10 扁平化：浅灰底 + 白色卡片 + 主题蓝）
# ==========================================================
APP_BG      = "#FFFFFF"   # 整体白底，不再用卡片和描边做分区
CARD_BG     = "#FFFFFF"   # 卡片/输入框底色
FIELD_BG    = "#F4F4F5"   # 无边框输入框的填充色
FG          = "#1F1F1F"   # 主文字
FG_SOFT     = "#5C5C5C"   # 次级文字
SUBTLE      = "#8A8A8A"   # 说明文字
ACCENT      = "#0078D7"   # Win10 主题蓝
ACCENT_H    = "#106EBE"   # 悬停
ACCENT_P    = "#005A9E"   # 按下
ACCENT_SOFT = "#E5F1FB"   # 浅蓝选中/聚焦底
LINE        = "#ECECEC"   # 分隔线
BORDER      = "#D6D6D6"   # 控件描边
HOVER_BG    = "#F2F2F2"   # 次级按钮悬停
PRESS_BG    = "#EBEBEB"   # 次级按钮按下
GHOST_H     = "#E9E9E9"   # 文字按钮悬停
GHOST_P     = "#E0E0E0"
TRACK       = "#D9D9D9"   # 滑杆轨道
ERROR       = "#C42B1C"   # Win10 错误红
DANGER      = "#C42B1C"
DANGER_BG   = "#FDF3F4"
DANGER_H    = "#FAE6E8"
DANGER_P    = "#F5D7DA"
DANGER_EDGE = "#E9BFC3"
WHITE       = "#FFFFFF"
ACCENT_FG   = "#FFFFFF"
DISABLED_BG = "#F7F7F7"
DISABLED_FG = "#B3B3B3"
DISABLED_EDGE = "#E6E6E6"

LIGHT_THEME = {
    "APP_BG": "#FFFFFF", "CARD_BG": "#FFFFFF", "FIELD_BG": "#F4F4F5",
    "FG": "#1F1F1F", "FG_SOFT": "#5C5C5C", "SUBTLE": "#8A8A8A",
    "ACCENT": "#0078D7", "ACCENT_H": "#106EBE", "ACCENT_P": "#005A9E",
    "ACCENT_SOFT": "#E5F1FB", "ACCENT_FG": "#FFFFFF",
    "LINE": "#ECECEC", "BORDER": "#D6D6D6",
    "HOVER_BG": "#F2F2F2", "PRESS_BG": "#EBEBEB",
    "GHOST_H": "#E9E9E9", "GHOST_P": "#E0E0E0",
    "TRACK": "#D9D9D9", "ERROR": "#C42B1C",
    "DANGER": "#C42B1C", "DANGER_BG": "#FDF3F4",
    "DANGER_H": "#FAE6E8", "DANGER_P": "#F5D7DA",
    "DANGER_EDGE": "#E9BFC3",
    "DISABLED_BG": "#F7F7F7", "DISABLED_FG": "#B3B3B3",
    "DISABLED_EDGE": "#E6E6E6",
}

DARK_THEME = {
    "APP_BG": "#1F1F1F", "CARD_BG": "#252526", "FIELD_BG": "#2D2D30",
    "FG": "#F2F2F2", "FG_SOFT": "#CFCFCF", "SUBTLE": "#9A9A9A",
    "ACCENT": "#4CC2FF", "ACCENT_H": "#6FD0FF", "ACCENT_P": "#3AA9E0",
    "ACCENT_SOFT": "#203A4A", "ACCENT_FG": "#16232B",
    "LINE": "#3A3A3A", "BORDER": "#3F3F3F",
    "HOVER_BG": "#333333", "PRESS_BG": "#3A3A3A",
    "GHOST_H": "#2E2E2E", "GHOST_P": "#383838",
    "TRACK": "#3F3F3F", "ERROR": "#FF7B72",
    "DANGER": "#FF7B72", "DANGER_BG": "#3A2426",
    "DANGER_H": "#4A2A2D", "DANGER_P": "#553035",
    "DANGER_EDGE": "#6B3A3E",
    "DISABLED_BG": "#2A2A2A", "DISABLED_FG": "#6E6E6E",
    "DISABLED_EDGE": "#333333",
}

F_UI      = ("Microsoft YaHei UI", 10)
F_UI_SM   = ("Microsoft YaHei UI", 9)
F_SECTION = ("Microsoft YaHei UI", 10, "bold")
F_TITLE   = ("Microsoft YaHei UI", 15, "bold")

POSITIONS = ["左上", "上", "右上", "左", "中", "右", "左下", "下", "右下", "随机"]

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".countdown_reminder.json")

DEFAULT_TEXT = "该起来活动一下啦 👀"

# 更多选项里的预设
TEXT_COLORS = ["#1F1F1F", "#5C5C5C", "#0078D7", "#C42B1C", "#107C10", "#8E6C1F"]
POPUP_BGS   = ["#FFFFFF", "#F7F7F7", "#EAF3FB", "#FFF8E1", "#F0F7EE", "#FDEEF2"]
FONT_CHOICES = [
    "Microsoft YaHei UI", "Microsoft YaHei", "DengXian", "SimHei",
    "SimSun", "KaiTi", "FangSong", "Segoe UI", "Arial", "Consolas",
    "Georgia", "Times New Roman",
]
SIZE_CHOICES = ["自动"] + [str(n) for n in (12, 14, 16, 18, 20, 24, 28, 32, 36, 48)]

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
WEEK_LABELS = ["一", "二", "三", "四", "五", "六", "日"]
SOUND_CHOICES = [("soft", "柔和"), ("clear", "清脆"), ("wood", "木质")]


# ==========================================================
#  配置模型（改进：字段类型校验 + 容错，替代裸 dict）
# ==========================================================
DEFAULT_REPO = "默认"


@dataclass
class Config:
    repos: list = field(default_factory=lambda: [
        {"name": DEFAULT_REPO, "phrases": [DEFAULT_TEXT], "mode": "sequence"}
    ])
    active_repo: str = DEFAULT_REPO
    sound: bool = True
    sound_name: str = "soft"
    interval: float = 30.0
    duration: float = 5.0
    position: str = "右下"
    size: float = 20.0
    # 计划
    schedule_mode: str = "interval"          # interval / clock
    clock_times: list = field(default_factory=lambda: ["10:00"])
    weekdays: list = field(default_factory=lambda: list(range(7)))
    active_enabled: bool = False
    active_start: str = "09:00"
    active_end: str = "18:00"
    fullscreen_defer: bool = True
    snooze_minutes: int = 5
    # 系统
    autostart: bool = False
    hotkeys: bool = True
    close_to_tray: bool = True
    theme: str = "light"
    # 更多选项
    font_family: str = "Microsoft YaHei UI"
    font_size: int = 0            # 0 = 自动
    text_color: str = "#1F1F1F"
    popup_bg: str = "#FFFFFF"

    # 允收范围：UI 校验与配置读取共用同一套，避免“存得进读不出”
    MIN_SECONDS = 0.1
    MAX_SECONDS = 86400.0
    MIN_SIZE    = 1.0
    MAX_SIZE    = 25.0

    @staticmethod
    def seconds(value):
        """把 value 规整成合法的秒数（有限值 + 范围），否则抛 ValueError"""
        x = float(value)
        if not math.isfinite(x):
            raise ValueError(f"数值 {x!r} 不是有限数")
        if not (Config.MIN_SECONDS <= x <= Config.MAX_SECONDS):
            raise ValueError(
                f"数值 {x} 超出范围 [{Config.MIN_SECONDS}, {Config.MAX_SECONDS}]")
        return x

    @staticmethod
    def percent(value):
        """把 value 规整成合法的窗口尺寸百分比"""
        x = float(value)
        if not math.isfinite(x):
            raise ValueError(f"数值 {x!r} 不是有限数")
        if not (Config.MIN_SIZE <= x <= Config.MAX_SIZE):
            raise ValueError(
                f"数值 {x} 超出范围 [{Config.MIN_SIZE}, {Config.MAX_SIZE}]")
        return x

    @classmethod
    def repo_list(cls, value):
        """校验并规整仓库列表；允许空仓库（新建后还没填词）。"""
        if not isinstance(value, list) or not value:
            raise ValueError("至少需要一个词库")
        out, names = [], set()
        for item in value[:20]:
            if not isinstance(item, dict):
                raise ValueError("词库条目必须是对象")
            name = str(item.get("name", "")).strip()
            if not name or len(name) > 24:
                raise ValueError(f"词库名不合法：{name!r}")
            if name in names:
                raise ValueError(f"词库名重复：{name}")
            names.add(name)
            phrases = item.get("phrases", [])
            if not isinstance(phrases, list):
                raise ValueError(f"词库 {name} 的词必须是列表")
            items = []
            for text in phrases[:200]:
                s = str(text).strip()
                if s:
                    items.append(s[:200])
            mode = item.get("mode", "sequence")
            if mode not in ("sequence", "random"):
                raise ValueError(f"未知轮换方式 {mode!r}")
            out.append({"name": name, "phrases": items, "mode": mode})
        return out

    @classmethod
    def _coerce(cls, key, value):
        if key == "position":
            if value not in POSITIONS:
                raise ValueError(f"未知位置 {value!r}")
            return value
        if key == "size":
            return cls.percent(value)
        if key == "repos":
            return cls.repo_list(value)
        if key == "active_repo":
            name = str(value).strip()
            if not name or len(name) > 24:
                raise ValueError(f"当前词库名不合法：{value!r}")
            return name
        if key == "sound":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if key in ("active_enabled", "fullscreen_defer", "autostart", "hotkeys",
                   "close_to_tray"):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if key == "theme":
            if value not in ("light", "dark"):
                raise ValueError(f"未知主题 {value!r}")
            return value
        if key == "sound_name":
            if value not in dict(SOUND_CHOICES):
                raise ValueError(f"未知音色 {value!r}")
            return value
        if key == "schedule_mode":
            if value not in ("interval", "clock"):
                raise ValueError(f"未知计划模式 {value!r}")
            return value
        if key == "clock_times":
            if not isinstance(value, list):
                raise ValueError("clock_times 必须是列表")
            out = []
            for item in value[:20]:
                s = str(item).strip()
                if not TIME_RE.match(s):
                    raise ValueError(f"时间格式应为 HH:MM：{item!r}")
                if s not in out:
                    out.append(s)
            if not out:
                raise ValueError("至少需要一个定时时间")
            return sorted(out)
        if key == "weekdays":
            if not isinstance(value, list):
                raise ValueError("weekdays 必须是列表")
            out = []
            for item in value:
                n = int(item)
                if n < 0 or n > 6:
                    raise ValueError(f"星期取值应为 0–6：{item!r}")
                if n not in out:
                    out.append(n)
            if not out:
                raise ValueError("至少选择一天")
            return sorted(out)
        if key in ("active_start", "active_end"):
            s = str(value).strip()
            if not TIME_RE.match(s):
                raise ValueError(f"时间格式应为 HH:MM：{value!r}")
            return s
        if key == "snooze_minutes":
            n = int(value)
            if not 1 <= n <= 60:
                raise ValueError(f"稍后分钟数 {n} 超出范围 1–60")
            return n
        if key in ("interval", "duration"):
            return cls.seconds(value)
        if key in ("text_color", "popup_bg"):
            s = str(value).strip()
            if not COLOR_RE.match(s):
                raise ValueError(f"颜色必须是 #RRGGBB 形式：{value!r}")
            return s.upper()
        if key == "font_family":
            s = str(value).strip()
            if not s or len(s) > 64:
                raise ValueError(f"字体名不合法：{value!r}")
            return s
        if key == "font_size":
            n = int(value)
            if n != 0 and not (8 <= n <= 96):
                raise ValueError(f"字号 {n} 超出范围（0=自动，8–96）")
            return n
        raise KeyError(key)

    @classmethod
    def load(cls, path=CONFIG_PATH):
        cfg = cls()
        try:
            # utf-8-sig：兼容记事本等编辑器写入的 BOM，否则整份配置会被当成损坏
            with open(path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return cfg
        except Exception as e:
            log.warning("读取配置失败，使用默认值：%s", e)
            # 留一份现场，方便排查，也避免下次启动继续踩同一个坑
            try:
                os.replace(path, path + ".bak")
                log.info("损坏的配置已备份为 %s.bak", path)
            except OSError as be:
                log.warning("备份损坏配置失败：%s", be)
            return cfg
        if not isinstance(raw, dict):
            log.warning("配置内容不是对象，使用默认值")
            return cfg
        # 旧配置迁移：text / phrases + phrase_mode -> repos
        if "repos" not in raw:
            legacy_text = raw.pop("text", None)
            legacy_phrases = raw.pop("phrases", None)
            legacy_mode = raw.pop("phrase_mode", "sequence")
            items = []
            if isinstance(legacy_phrases, list):
                items = [str(x).strip() for x in legacy_phrases if str(x).strip()]
            elif isinstance(legacy_text, str) and legacy_text.strip():
                items = [legacy_text.strip()]
            if not items:
                items = [DEFAULT_TEXT]
            if legacy_mode not in ("sequence", "random"):
                legacy_mode = "sequence"
            raw["repos"] = [{"name": DEFAULT_REPO, "phrases": items,
                             "mode": legacy_mode}]
            raw["active_repo"] = DEFAULT_REPO
        else:
            for old in ("text", "phrases", "phrase_mode"):
                raw.pop(old, None)
        for key, value in raw.items():
            try:
                setattr(cfg, key, cls._coerce(key, value))
            except KeyError:
                log.info("忽略未知配置字段 %s", key)
            except (TypeError, ValueError) as e:
                log.warning("忽略非法配置字段 %s=%r：%s", key, value, e)
        names = [repo["name"] for repo in cfg.repos]
        if not names:
            cfg.repos = [{"name": DEFAULT_REPO, "phrases": [DEFAULT_TEXT],
                          "mode": "sequence"}]
            names = [DEFAULT_REPO]
        if cfg.active_repo not in names:
            cfg.active_repo = names[0]
        return cfg

    def save(self, path=CONFIG_PATH):
        """先写临时文件再原子替换，避免写一半崩溃把配置写坏"""
        tmp = path + ".tmp"
        bak = path + ".bak"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(path):
                try:
                    shutil.copy2(path, bak)
                except OSError as e:
                    log.warning("备份旧配置失败：%s", e)
            os.replace(tmp, path)
            return True
        except Exception as e:
            log.error("保存配置失败：%s", e)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False


def resource_path(name):
    """打包/源码运行都能定位资源文件。"""
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def apply_window_icon(widget):
    path = resource_path("app.ico")
    if os.path.exists(path):
        try:
            widget.iconbitmap(path)
        except Exception:
            pass


def _fmt_num(x):
    """float 转字符串，整数时不带 .0"""
    return str(int(x)) if float(x).is_integer() else str(float(x))


_CHIMES = {}
_CHIME_PATHS = {}

_CHIME_SPECS = {
    "soft": ((784.0, 0.13), (1046.5, 0.24)),
    "clear": ((1046.5, 0.10), (1318.5, 0.10), (1568.0, 0.20)),
    "wood": ((392.0, 0.08), (294.0, 0.16)),
}


def _chime_wav(name="soft"):
    """合成提示音（正弦波 + 淡入淡出），按音色缓存成 WAV 字节。"""
    if name in _CHIMES:
        return _CHIMES[name]
    import io
    import struct
    import wave

    rate = 22050
    data = bytearray()
    for freq, dur in _CHIME_SPECS.get(name, _CHIME_SPECS["soft"]):
        n = int(rate * dur)
        decay = 2.2 if name != "wood" else 5.0
        for i in range(n):
            t = i / rate
            attack = min(1.0, i / (rate * 0.012))
            release = min(1.0, (n - i) / (rate * 0.03))
            env = attack * release * math.exp(-decay * t / dur)
            value = int(0.22 * 32767 * env * math.sin(2 * math.pi * freq * t))
            data += struct.pack("<h", value)
        data += b"\x00\x00" * int(rate * 0.012)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(data))
    _CHIMES[name] = buf.getvalue()
    return _CHIMES[name]


def _chime_path(name="soft"):
    """把提示音缓存成临时 WAV 文件；winsound 不支持内存异步播放。"""
    if name in _CHIME_PATHS:
        path = _CHIME_PATHS[name]
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            return path
    import tempfile
    path = os.path.join(tempfile.gettempdir(),
                        f"countdown_reminder_chime_v3_{name}.wav")
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 1000:
            with open(path, "wb") as f:
                f.write(_chime_wav(name))
        _CHIME_PATHS[name] = path
        return path
    except OSError as e:
        log.warning("提示音缓存写入失败：%s", e)
        return None


# ==========================================================
#  状态机（改进：用显式状态替代 running 布尔 + 多个 after 句柄）
# ==========================================================
class Phase(Enum):
    IDLE     = "空闲"
    COUNTING = "倒计时中"
    SHOWING  = "显示中"


# ==========================================================
#  自绘扁平控件（纯 tkinter，无第三方依赖）
# ==========================================================
class FlatButton(tk.Frame):
    """Win10 风格按钮：1px 描边 + 纯平色块 + 悬停/按下反馈。

    用 Frame 包住 Label 来画 1px 边框（tk.Button 的 relief 做不出细边），
    比原生按钮少一层立体阴影，配色完全可控，也支持禁用态。
    """

    _PALETTE = {
        # kind: (常态底, 悬停底, 按下底, 文字, 描边)
        "primary":   (ACCENT, ACCENT_H, ACCENT_P, ACCENT_FG, ACCENT),
        "secondary": (FIELD_BG, "#ECECEE", "#E2E2E4", FG, FIELD_BG),
        "ghost":     (APP_BG, GHOST_H, GHOST_P, FG_SOFT, APP_BG),
        "card_ghost": (CARD_BG, HOVER_BG, PRESS_BG, FG_SOFT, CARD_BG),
        "danger":    (DANGER_BG, DANGER_H, DANGER_P, DANGER, DANGER_EDGE),
    }

    def __init__(self, master, text, command, kind="secondary",
                 font=F_UI, padx=18, pady=7):
        super().__init__(master, bd=0, highlightthickness=0, bg=APP_BG)
        self._cmd    = command
        self._kind   = kind
        self._state  = "normal"
        self._hover  = False
        self._press  = False
        self._focus  = False
        self._label  = tk.Label(self, text=text, font=font, bd=0,
                                padx=padx, pady=pady, bg=CARD_BG, fg=FG,
                                takefocus=1)
        self._label.pack(padx=1, pady=1)
        self._label.bind("<Enter>", self._on_enter)
        self._label.bind("<Leave>", self._on_leave)
        self._label.bind("<Button-1>", self._on_press)
        self._label.bind("<ButtonRelease-1>", self._on_release)
        self._label.bind("<FocusIn>", self._on_focus_in)
        self._label.bind("<FocusOut>", self._on_focus_out)
        self._label.bind("<Return>", self._on_key)
        self._label.bind("<space>", self._on_key)
        self._paint()

    # ---------- 外部接口（尽量贴近 tk.Button 的用法） ----------
    def config(self, **kw):
        if "text" in kw:
            self._label.configure(text=kw.pop("text"))
        if "command" in kw:
            self._cmd = kw.pop("command")
        if "state" in kw:
            self._state = kw.pop("state")
            self._hover = self._press = False
        if "kind" in kw:
            self._kind = kw.pop("kind")
        if kw:
            self._label.configure(**kw)
        self._paint()

    configure = config

    def set_kind(self, kind):
        self._kind = kind
        self._paint()

    def invoke(self):
        if self._state != "disabled" and self._cmd:
            self._cmd()

    # ---------- 绘制 / 状态 ----------
    def _paint(self):
        if self._state == "disabled":
            base = hover = press = DISABLED_BG
            fg, edge = DISABLED_FG, DISABLED_EDGE
        else:
            base, hover, press, fg, edge = self._PALETTE[self._kind]
        bg = press if self._press else (hover if self._hover else base)
        if self._focus and self._state != "disabled":
            edge = ACCENT
        # 注意：本类把 configure 别名成了 config，这里必须显式调基类，
        # 否则 self.configure 又会落回 config，造成无限递归。
        tk.Frame.configure(self, bg=edge)
        self._label.configure(bg=bg, fg=fg, cursor=self._cursor())

    def _cursor(self):
        return "arrow" if self._state == "disabled" else "hand2"

    def _on_enter(self, _e=None):
        if self._state == "disabled":
            return
        self._hover = True
        self._paint()

    def _on_leave(self, _e=None):
        self._hover = False
        self._press = False
        self._paint()

    def _on_press(self, _e=None):
        if self._state == "disabled":
            return
        self._label.focus_set()
        self._press = True
        self._paint()

    def _on_release(self, event):
        if not self._press:
            return
        self._press = False
        self._paint()
        # 在按钮外松开不触发，和原生按钮行为一致
        if self.winfo_containing(event.x_root, event.y_root) in (self, self._label):
            self.invoke()

    def _on_focus_in(self, _e=None):
        self._focus = True
        self._paint()

    def _on_focus_out(self, _e=None):
        self._focus = False
        self._paint()

    def _on_key(self, _e=None):
        self._press = True
        self._paint()
        self._press = False
        self._paint()
        self.invoke()
        return "break"


def set_titlebar_theme(widget, dark):
    """Win10 1809+ / Win11 深色标题栏。"""
    try:
        hwnd = root_hwnd(widget)
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wt.HWND(hwnd), attr, ctypes.byref(value),
                ctypes.sizeof(value))
        # 强制重画非客户区，否则部分 Win10 版本要等窗口移动才变色
        user32.SetWindowPos(wt.HWND(hwnd), None, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
                            SWP_FRAMECHANGED)
    except Exception as e:
        log.debug("设置标题栏主题失败：%s", e)


def apply_palette(name):
    """切换全局浅色/深色色板，并重建 FlatButton 的调色表。"""
    palette = DARK_THEME if name == "dark" else LIGHT_THEME
    globals().update(palette)
    FlatButton._PALETTE = {
        "primary":   (ACCENT, ACCENT_H, ACCENT_P, ACCENT_FG, ACCENT),
        "secondary": (FIELD_BG, HOVER_BG, PRESS_BG, FG, FIELD_BG),
        "ghost":     (APP_BG, GHOST_H, GHOST_P, FG_SOFT, APP_BG),
        "card_ghost": (CARD_BG, HOVER_BG, PRESS_BG, FG_SOFT, CARD_BG),
        "danger":    (DANGER_BG, DANGER_H, DANGER_P, DANGER, DANGER_EDGE),
    }


class FlatSlider(tk.Canvas):
    """Win10 风格滑杆：细轨道 + 圆形滑块，替代 tk.Scale 的旧式外观。

    和 tk.Scale 一样绑定一个 IntVar/DoubleVar，并支持 command 回调、
    单击定位、拖动、左右方向键微调。
    """

    def __init__(self, master, variable, from_=1, to=25, command=None,
                 width=176, height=26, bg=CARD_BG):
        super().__init__(master, width=width, height=height, bg=bg,
                         bd=0, highlightthickness=0, cursor="hand2",
                         takefocus=1)
        self.var      = variable
        self.from_    = from_
        self.to       = to
        self.command  = command
        self._cw, self._ch = width, height
        self._hover   = False
        self._drag    = False

        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Left>", lambda e: self._nudge(-1))
        self.bind("<Right>", lambda e: self._nudge(1))
        self.bind("<Home>", lambda e: self._set(self.from_))
        self.bind("<End>", lambda e: self._set(self.to))
        variable.trace_add("write", lambda *_: self._redraw())
        self._redraw()

    # ---------- 数值 ----------
    def _ratio(self):
        span = float(self.to - self.from_) or 1.0
        v = float(self.var.get())
        return max(0.0, min(1.0, (v - self.from_) / span))

    def _value_from_x(self, x):
        w = self.winfo_width() or self._cw
        pad = 9
        span = max(1, w - 2 * pad)
        t = max(0.0, min(1.0, (x - pad) / span))
        return round(self.from_ + t * (self.to - self.from_))

    def _set(self, value):
        v = int(max(self.from_, min(self.to, value)))
        if float(self.var.get()) != v:
            self.var.set(v)
        if self.command:
            self.command(v)
        self._redraw()

    def _nudge(self, step):
        self._set(int(round(float(self.var.get()))) + step)

    # ---------- 交互 ----------
    def _on_enter(self, _e=None):
        self._hover = True
        self._redraw()

    def _on_leave(self, _e=None):
        self._hover = False
        self._drag = False
        self._redraw()

    def _on_click(self, event):
        self.focus_set()
        self._drag = True
        self._set(self._value_from_x(event.x))

    def _on_drag(self, event):
        if self._drag:
            self._set(self._value_from_x(event.x))

    def _on_release(self, _e=None):
        self._drag = False
        self._redraw()

    # ---------- 绘制 ----------
    def _redraw(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1:
            w = self._cw
        if h <= 1:
            h = self._ch
        pad, mid = 9, h / 2.0
        x = pad + self._ratio() * (w - 2 * pad)
        self.delete("all")
        self.create_line(pad, mid, w - pad, mid,
                         fill=TRACK, width=4, capstyle="round")
        if x > pad + 0.5:
            self.create_line(pad, mid, x, mid,
                             fill=ACCENT, width=4, capstyle="round")
        r = 8 if (self._hover or self._drag) else 7
        self.create_oval(x - r, mid - r, x + r, mid + r,
                         fill=ACCENT, outline=ACCENT)
        self.create_oval(x - 3, mid - 3, x + 3, mid + 3,
                         fill=WHITE, outline=WHITE)


class FlatSelect(tk.Frame):
    """无边框下拉选择器：自绘箭头 + 自绘列表，展开时淡入 + 下滑，收起时淡出。

    替代 ttk.Combobox，换来完全可控的浅色样式和动画；
    支持单击选择、鼠标悬停高亮、方向键/回车/Esc。
    """

    ROW_PAD = 6
    _active = None

    def __init__(self, master, variable, values, command=None,
                 width=180, height=None, font=F_UI_SM):
        if height is None:
            height = tkfont.Font(font=font).metrics("linespace") + 12
        super().__init__(master, bg=FIELD_BG, width=width, height=height,
                         bd=0, highlightthickness=0, cursor="hand2")
        self.pack_propagate(False)
        self.var = variable
        self.values = list(values)
        self.command = command
        self._font = font
        self._cw, self._ch = width, height
        self._open = False
        self._pop = None
        self._list = None
        self._anim = None
        self._geom = None
        self._top = self.winfo_toplevel()
        self._root_bind = None

        self._label = tk.Label(self, textvariable=variable, anchor="w",
                               bg=FIELD_BG, fg=FG, font=font, padx=10,
                               cursor="hand2")
        self._label.pack(side="left", fill="both", expand=True)
        self._arrow = tk.Canvas(self, width=24, height=height, bg=FIELD_BG,
                                bd=0, highlightthickness=0, cursor="hand2")
        self._arrow.pack(side="right", fill="y")
        self._draw_arrow()

        for w in (self, self._label, self._arrow):
            w.bind("<Button-1>", self._toggle)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        self.bind("<Destroy>", self._on_destroy)

    # ---------- 外部接口（兼容 Combobox 常用调用） ----------
    def get(self):
        return self.var.get()

    def set(self, value):
        self.var.set(value)

    def config(self, **kw):
        if "values" in kw:
            self.values = list(kw.pop("values"))
            if self._open:
                self._fill()
                self._resize_popup()
        if kw:
            super().config(**kw)

    configure = config

    # ---------- 外观 ----------
    def _draw_arrow(self, up=False):
        self._arrow.delete("all")
        cy = self._ch // 2
        if up:
            pts = (7, cy + 2, 12, cy - 3, 17, cy + 2)
        else:
            pts = (7, cy - 3, 12, cy + 2, 17, cy - 3)
        color = ACCENT if self._open else FG_SOFT
        self._arrow.create_line(*pts, fill=color, width=2,
                                capstyle="round", joinstyle="round")

    def _paint_bg(self, color):
        self.configure(bg=color)
        self._label.configure(bg=color)
        self._arrow.configure(bg=color)

    def _on_enter(self, _e=None):
        if not self._open:
            self._paint_bg("#ECECEE")

    def _on_leave(self, _e=None):
        if not self._open:
            self._paint_bg(FIELD_BG)

    # ---------- 展开 / 收起 ----------
    def _toggle(self, _e=None):
        if self._open:
            self._close()
        else:
            self._open_popup()
        return "break"

    def _create_popup(self):
        self._pop = tk.Toplevel(self)
        self._pop.overrideredirect(True)
        self._pop.configure(bg=CARD_BG)
        self._pop.attributes("-topmost", True)
        self._pop.withdraw()

        self._list = tk.Listbox(self._pop, bd=0, highlightthickness=0,
                                relief="flat", bg=CARD_BG, fg=FG,
                                selectbackground=ACCENT_SOFT,
                                selectforeground=FG, activestyle="none",
                                exportselection=False, font=self._font,
                                cursor="hand2")
        self._list.pack(fill="both", expand=True, padx=4, pady=4)
        self._list.bind("<Button-1>", self._on_item_click)
        self._list.bind("<Motion>", self._on_motion)
        self._list.bind("<MouseWheel>", self._on_wheel)
        self._list.bind("<Escape>", lambda e: (self._close(), "break")[1])
        self._list.bind("<Return>", lambda e: (self._choose_current(), "break")[1])
        self._list.bind("<Up>", lambda e: (self._move(-1), "break")[1])
        self._list.bind("<Down>", lambda e: (self._move(1), "break")[1])
        self._list.bind("<FocusOut>", lambda e: self._close())

    def _fill(self):
        self._list.delete(0, "end")
        for value in self.values:
            self._list.insert("end", value)
        current = self.var.get()
        if current in self.values:
            idx = self.values.index(current)
            self._list.selection_set(idx)
            self._list.activate(idx)
            self._list.see(idx)

    def _popup_height(self):
        line = tkfont.Font(font=self._font).metrics("linespace")
        rows = max(1, min(len(self.values), 12))
        return rows * (line + self.ROW_PAD) + 8

    def _resize_popup(self):
        if not self._open or not self._geom:
            return
        w, _h, x, y = self._geom
        h = self._popup_height()
        self._geom = (w, h, x, y)
        self._pop.geometry(f"{w}x{h}+{x}+{y}")

    def _open_popup(self):
        if not self.values:
            return
        if FlatSelect._active is not None and FlatSelect._active is not self:
            FlatSelect._active._close()
        FlatSelect._active = self
        self._cancel_anim()
        if self._pop is None:
            self._create_popup()

        self._fill()
        self._open = True
        self._paint_bg("#E9F2FB")
        self._draw_arrow(up=True)

        font = tkfont.Font(font=self._font)
        content = max((font.measure(str(v)) for v in self.values), default=0)
        w = max(72, content + 28)
        h = self._popup_height()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        if y + h > self.winfo_screenheight() - 8:
            y = self.winfo_rooty() - h - 2
        self._geom = (w, h, x, y)

        self._pop.geometry(f"{w}x{h}+{x}+{y + 6}")
        try:
            self._pop.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self._pop.deiconify()
        self._pop.lift()
        self._list.focus_force()
        self._root_bind = self._top.bind("<Button-1>", self._on_root_click,
                                         add="+")
        self._animate_in(1)

    def _animate_in(self, i, steps=5):
        self._anim = None
        if not self._open or self._pop is None:
            return
        w, h, x, y = self._geom
        alpha = min(1.0, i / steps)
        self._pop.geometry(f"{w}x{h}+{x}+{y + int(6 * (1 - alpha))}")
        try:
            self._pop.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if i < steps:
            self._anim = self.after(14, self._animate_in, i + 1)

    def _close(self):
        if not self._open:
            return
        self._open = False
        if FlatSelect._active is self:
            FlatSelect._active = None
        self._cancel_anim()
        if self._root_bind is not None:
            try:
                self._top.unbind("<Button-1>", self._root_bind)
            except tk.TclError:
                pass
            self._root_bind = None
        self._paint_bg(FIELD_BG)
        self._draw_arrow(up=False)
        if self._pop is not None:
            self._animate_out(1)

    def _animate_out(self, i, steps=4):
        self._anim = None
        if self._pop is None:
            return
        alpha = max(0.0, 1.0 - i / steps)
        try:
            self._pop.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if i < steps:
            self._anim = self.after(14, self._animate_out, i + 1)
        else:
            self._pop.withdraw()
            try:
                self._pop.attributes("-alpha", 1.0)
            except tk.TclError:
                pass

    def _cancel_anim(self):
        if self._anim is not None:
            try:
                self.after_cancel(self._anim)
            except tk.TclError:
                pass
            self._anim = None

    # ---------- 选择 ----------
    def _choose(self, index):
        if 0 <= index < len(self.values):
            self.var.set(self.values[index])
            if self.command:
                self.command()
        self._close()

    def _choose_current(self):
        cur = self._list.curselection()
        if cur:
            self._choose(cur[0])

    def _on_item_click(self, event):
        self._choose(self._list.nearest(event.y))
        return "break"

    def _on_motion(self, event):
        idx = self._list.nearest(event.y)
        if idx != self._list.index("active"):
            self._list.selection_clear(0, "end")
            self._list.selection_set(idx)
            self._list.activate(idx)

    def _on_wheel(self, event):
        self._list.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _move(self, step):
        cur = self._list.curselection()
        idx = (cur[0] if cur else -1) + step
        idx = max(0, min(len(self.values) - 1, idx))
        self._list.selection_clear(0, "end")
        self._list.selection_set(idx)
        self._list.activate(idx)
        self._list.see(idx)

    # ---------- 点击外部 / 销毁 ----------
    def _on_root_click(self, event):
        if not self._open:
            return
        widget = event.widget
        if self._inside(widget, self) or self._inside(widget, self._pop):
            return
        self._close()

    @staticmethod
    def _inside(widget, ancestor):
        w = widget
        while w is not None:
            if w is ancestor:
                return True
            w = getattr(w, "master", None)
        return False

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        self._cancel_anim()
        if FlatSelect._active is self:
            FlatSelect._active = None
        if self._pop is not None:
            try:
                self._pop.destroy()
            except tk.TclError:
                pass
            self._pop = None


class TextSwitch(tk.Frame):
    """极简文字切换：选中项用主题蓝加粗，未选中灰色，无边框。"""

    def __init__(self, master, options, variable, command=None, bg=CARD_BG):
        super().__init__(master, bg=bg)
        self.var = variable
        self.command = command
        self._labels = {}
        for value, text in options:
            label = tk.Label(self, text=text, bg=bg, fg=SUBTLE,
                             font=F_UI_SM, cursor="hand2", padx=2)
            label.pack(side="left", padx=(0, 10))
            label.bind("<Button-1>", lambda e, v=value: self._pick(v))
            self._labels[value] = label
        variable.trace_add("write", lambda *_: self._refresh())
        self._refresh()

    def _pick(self, value):
        if self.var.get() == value:
            return
        self.var.set(value)
        if self.command:
            self.command()

    def _refresh(self):
        try:
            current = self.var.get()
        except tk.TclError:
            return
        for value, label in self._labels.items():
            selected = value == current
            try:
                label.configure(fg=ACCENT if selected else SUBTLE,
                                font=("Microsoft YaHei UI", 9, "bold") if selected
                                else F_UI_SM)
            except tk.TclError:
                return


class FlatToggle(tk.Canvas):
    """无边框开关：灰色/蓝色胶囊 + 白色圆点，切换时快速滑动。"""

    def __init__(self, master, variable, command=None,
                 width=44, height=24, bg=CARD_BG):
        super().__init__(master, width=width, height=height, bg=bg,
                         bd=0, highlightthickness=0, cursor="hand2")
        self.var = variable
        self.command = command
        self._tw, self._th = width, height
        self._pos = 1.0 if variable.get() else 0.0
        self._anim = None
        self.bind("<Button-1>", self._toggle)
        variable.trace_add("write",
                           lambda *_: self._slide(1.0 if variable.get() else 0.0))
        self._draw()

    def _toggle(self, _e=None):
        self.var.set(not self.var.get())
        if self.command:
            self.command()
        return "break"

    def _slide(self, target, steps=4):
        if self._anim is not None:
            try:
                self.after_cancel(self._anim)
            except tk.TclError:
                pass
            self._anim = None
        if not self.winfo_exists():
            return
        start = self._pos

        def step(i):
            self._anim = None
            if not self.winfo_exists():
                return
            self._pos = start + (target - start) * i / steps
            self._draw()
            if i < steps:
                self._anim = self.after(14, step, i + 1)

        step(1)

    def _draw(self):
        self.delete("all")
        mid = self._th / 2
        color = ACCENT if self._pos >= 0.5 else "#D2D2D2"
        self.create_line(11, mid, self._tw - 11, mid,
                         width=self._th - 6, capstyle="round", fill=color)
        x = 11 + self._pos * (self._tw - 22)
        r = (self._th - 6) / 2
        self.create_oval(x - r, mid - r, x + r, mid + r,
                         fill=WHITE, outline="#C9C9C9", width=1)


class TrayIcon:
    """Windows 托盘图标：Shell_NotifyIcon + 隐藏窗口消息循环。"""

    WM_STOP = 0x8000 + 22
    ID_SHOW, ID_TOGGLE, ID_PAUSE, ID_EXIT = 1001, 1002, 1003, 1004

    def __init__(self, app):
        self.app = app
        self.available = False
        self._hwnd = None
        self._nid = None
        self._stopping = False
        self._queue = queue.Queue()
        self._ready = threading.Event()
        self.icon_path = resource_path("app.ico")
        self._wndproc = None
        try:
            threading.Thread(target=self._run, daemon=True,
                             name="countdown-tray").start()
            self._ready.wait(2)
        except Exception as e:
            log.warning("托盘初始化失败：%s", e)
        if self.available:
            self.app.root.after(100, self._poll)

    def _run(self):
        try:
            from ctypes import wintypes as w
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            shell32 = ctypes.windll.shell32

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT,
                                         w.WPARAM, w.LPARAM)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", w.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", w.HINSTANCE),
                    ("hIcon", w.HICON),
                    ("hCursor", w.HANDLE),
                    ("hbrBackground", w.HBRUSH),
                    ("lpszMenuName", w.LPCWSTR),
                    ("lpszClassName", w.LPCWSTR),
                ]

            class NOTIFYICONDATAW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", w.DWORD),
                    ("hWnd", w.HWND),
                    ("uID", w.UINT),
                    ("uFlags", w.UINT),
                    ("uCallbackMessage", w.UINT),
                    ("hIcon", w.HICON),
                    ("szTip", w.WCHAR * 128),
                    ("dwState", w.DWORD),
                    ("dwStateMask", w.DWORD),
                    ("szInfo", w.WCHAR * 256),
                    ("uVersion", w.UINT),
                    ("szInfoTitle", w.WCHAR * 64),
                    ("dwInfoFlags", w.DWORD),
                    ("guidItem", ctypes.c_byte * 16),
                    ("hBalloonIcon", w.HICON),
                ]

            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.CreateWindowExW.restype = w.HWND
            user32.CreateWindowExW.argtypes = [
                w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM,
                                              w.LPARAM]
            user32.DestroyWindow.argtypes = [w.HWND]
            user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
            user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND,
                                           w.UINT, w.UINT]
            user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
            user32.LoadIconW.restype = w.HICON
            user32.LoadIconW.argtypes = [w.HINSTANCE, w.LPCWSTR]
            user32.LoadImageW.restype = w.HANDLE
            user32.LoadImageW.argtypes = [
                w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int,
                w.UINT]
            user32.LoadCursorW.restype = w.HANDLE
            user32.LoadCursorW.argtypes = [w.HINSTANCE, w.LPCWSTR]
            user32.CreatePopupMenu.restype = w.HMENU
            user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t,
                                           w.LPCWSTR]
            user32.TrackPopupMenu.restype = ctypes.c_int
            user32.TrackPopupMenu.argtypes = [
                w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                w.HWND, w.LPVOID]
            user32.DestroyMenu.argtypes = [w.HMENU]
            user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
            user32.SetForegroundWindow.argtypes = [w.HWND]
            kernel32.GetModuleHandleW.restype = w.HINSTANCE
            kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
            shell32.Shell_NotifyIconW.argtypes = [
                w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

            WM_APP = 0x8000
            WM_TRAY = WM_APP + 20
            WM_STOP = WM_APP + 22
            WM_COMMAND, WM_CLOSE, WM_DESTROY = 0x0111, 0x0010, 0x0002
            WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0203, 0x0205
            NIM_ADD, NIM_DELETE = 0, 2
            NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
            IDC_ARROW, IDI_APPLICATION = 32512, 32512

            hinst = kernel32.GetModuleHandleW(None)
            class_name = f"CountdownReminderTray{id(self)}"

            def wndproc(hwnd, msg, wparam, lparam):
                try:
                    if msg == WM_TRAY:
                        event = int(lparam) & 0xFFFF
                        if event == WM_LBUTTONDBLCLK:
                            self._queue.put("show")
                        elif event == WM_RBUTTONUP:
                            self._show_menu(hwnd, user32, w)
                    elif msg == WM_COMMAND:
                        self._command(int(wparam) & 0xFFFF)
                    elif msg == WM_STOP:
                        try:
                            shell32.Shell_NotifyIconW(
                                NIM_DELETE, ctypes.byref(self._nid))
                        except Exception:
                            pass
                        user32.DestroyWindow(hwnd)
                    elif msg == WM_CLOSE:
                        user32.DestroyWindow(hwnd)
                    elif msg == WM_DESTROY:
                        user32.PostQuitMessage(0)
                except Exception as e:
                    log.warning("托盘消息处理失败：%s", e)
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc = WNDPROC(wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.hCursor = user32.LoadCursorW(None, w.LPCWSTR(IDC_ARROW))
            wc.lpszClassName = class_name
            user32.RegisterClassW(ctypes.byref(wc))

            hwnd = user32.CreateWindowExW(
                0, class_name, "CountdownReminderTray", 0, 0, 0, 0, 0,
                None, None, hinst, None)
            if not hwnd:
                log.warning("托盘窗口创建失败")
                self._ready.set()
                return
            self._hwnd = hwnd

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            hicon = 0
            if os.path.exists(self.icon_path):
                hicon = user32.LoadImageW(None, self.icon_path, 1, 32, 32, 0x10)
            if not hicon:
                hicon = user32.LoadIconW(None, w.LPCWSTR(IDI_APPLICATION))
            nid.hIcon = hicon
            nid.szTip = "倒计时提醒器"
            self._nid = nid
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                log.warning("托盘图标添加失败")
                user32.DestroyWindow(hwnd)
                self._ready.set()
                return
            self.available = True
            self._ready.set()

            msg = w.MSG()
            while not self._stopping:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log.warning("托盘线程异常：%s", e)
        finally:
            self.available = False
            self._ready.set()

    def _show_menu(self, hwnd, user32, w):
        running = self.app.phase is not Phase.IDLE
        paused = getattr(self.app, "_pause_until", None) is not None
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, self.ID_SHOW, "显示窗口")
        user32.AppendMenuW(menu, 0x800, 0, None)
        user32.AppendMenuW(menu, 0, self.ID_TOGGLE,
                           "停止" if running else "开始")
        user32.AppendMenuW(menu, 0, self.ID_PAUSE,
                           "继续" if paused else "暂停 30 分钟")
        user32.AppendMenuW(menu, 0x800, 0, None)
        user32.AppendMenuW(menu, 0, self.ID_EXIT, "退出")
        user32.SetForegroundWindow(hwnd)
        pt = w.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cmd = user32.TrackPopupMenu(
            menu, 0x0100 | 0x0002, pt.x, pt.y, 0, hwnd, None)
        user32.DestroyMenu(menu)
        user32.PostMessageW(hwnd, 0, 0, 0)
        if cmd:
            self._command(cmd)

    def _command(self, cmd):
        if cmd == self.ID_SHOW:
            self._queue.put("show")
        elif cmd == self.ID_TOGGLE:
            self._queue.put("toggle")
        elif cmd == self.ID_PAUSE:
            self._queue.put("pause")
        elif cmd == self.ID_EXIT:
            self._queue.put("exit")

    def _poll(self):
        try:
            while True:
                action = self._queue.get_nowait()
                if action == "show":
                    self.app._show_from_tray()
                elif action == "toggle":
                    self.app._hotkey_toggle()
                elif action == "pause":
                    self.app._hotkey_pause()
                elif action == "exit":
                    self.app.quit_app()
                    return
        except queue.Empty:
            pass
        if not self._stopping:
            try:
                self.app.root.after(120, self._poll)
            except tk.TclError:
                pass

    def stop(self):
        self._stopping = True
        if self.available and self._hwnd:
            try:
                ctypes.windll.user32.PostMessageW(
                    wt.HWND(self._hwnd), self.WM_STOP, 0, 0)
            except Exception:
                pass


class RepoManager(tk.Toplevel):
    """词库管理：新建 / 重命名 / 删除 / 导入 / 导出。"""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("词库管理")
        self.configure(bg=CARD_BG)
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.close)

        wrap = tk.Frame(self, bg=CARD_BG)
        wrap.pack(fill="both", expand=True, padx=20, pady=16)

        tk.Label(wrap, text="词库", bg=CARD_BG, fg=FG,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        tk.Label(wrap, text="每个仓有自己的词和顺序/随机，选中即切换当前仓",
                 bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(anchor="w", pady=(4, 10))

        self.listbox = tk.Listbox(wrap, height=8, font=F_UI, bg=FIELD_BG,
                                  fg=FG, bd=0, highlightthickness=0,
                                  selectbackground=ACCENT_SOFT,
                                  selectforeground=FG, activestyle="none",
                                  exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Double-Button-1>", lambda e: self._rename())

        btns = tk.Frame(wrap, bg=CARD_BG)
        btns.pack(fill="x", pady=(12, 0))
        for text, cmd in (("新建", self._new), ("重命名", self._rename),
                          ("删除", self._delete)):
            FlatButton(btns, text, cmd, padx=10, pady=4).pack(
                side="left", padx=(0, 8))
        FlatButton(btns, "导入", self._import, padx=10, pady=4).pack(
            side="right", padx=(8, 0))
        FlatButton(btns, "导出", self._export, padx=10, pady=4).pack(
            side="right")

        bottom = tk.Frame(wrap, bg=CARD_BG)
        bottom.pack(fill="x", pady=(10, 0))
        FlatButton(bottom, "关闭", self.close, kind="ghost",
                   padx=10, pady=4).pack(side="right")

        self._refresh()
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - 420) // 2
        y = app.root.winfo_rooty() + 40
        self.geometry(f"420x360+{x}+{y}")
        self.update_idletasks()
        apply_window_icon(self)
        set_titlebar_theme(self, self.app.cfg.theme == "dark")
        try:
            self.grab_set()
        except tk.TclError:
            pass

    # ---------- 列表 ----------
    def _refresh(self):
        names = self.app._repo_names()
        self.listbox.delete(0, "end")
        for name in names:
            self.listbox.insert("end", name)
        if self.app.active_repo in names:
            index = names.index(self.app.active_repo)
            self.listbox.selection_set(index)
            self.listbox.see(index)

    def _selected(self):
        selection = self.listbox.curselection()
        if selection:
            return self.listbox.get(selection[0])
        return self.app.active_repo

    def _on_select(self, _e=None):
        name = self._selected()
        if name:
            self.app._activate_repo(name)

    # ---------- 操作 ----------
    def _new(self):
        if self.app._create_repo(parent=self) is not None:
            self._refresh()

    def _rename(self):
        old = self._selected()
        if not old:
            return
        name = simpledialog.askstring("重命名词库", "名称：",
                                      initialvalue=old, parent=self)
        if name is None:
            return
        name = name.strip()
        if name == old:
            return
        problem = self.app._repo_name_problem(name, exclude=old)
        if problem:
            messagebox.showwarning("无法重命名", problem, parent=self)
            return
        self.app._commit_active_repo()
        repo = self.app._repo(old)
        if repo is None:
            return
        repo["name"] = name
        if self.app.active_repo == old:
            self.app.active_repo = name
        if old in self.app._repo_pos:
            self.app._repo_pos[name] = self.app._repo_pos.pop(old)
        self.app._refresh_repo_widgets()
        self._refresh()
        self.app._sync_cfg()
        self.app._save_cfg()

    def _delete(self):
        name = self._selected()
        if not name:
            return
        if len(self.app.repos) <= 1:
            messagebox.showwarning("至少保留一个", "不能删除最后一个词库。",
                                   parent=self)
            return
        if not messagebox.askyesno("删除词库",
                                   f"删除「{name}」？其中的词会一并删除。",
                                   parent=self):
            return
        self.app._commit_active_repo()
        self.app.repos = [r for r in self.app.repos if r["name"] != name]
        self.app._repo_pos.pop(name, None)
        if self.app.active_repo == name:
            self.app.active_repo = self.app.repos[0]["name"]
        self.app._refresh_repo_widgets()
        self._refresh()
        self.app._sync_cfg()
        self.app._save_cfg()

    def _read_lines(self, path):
        for encoding in ("utf-8-sig", "gbk"):
            try:
                with open(path, "r", encoding=encoding) as f:
                    text = f.read()
            except UnicodeDecodeError:
                continue
            except OSError as e:
                messagebox.showerror("读取失败", str(e), parent=self)
                return []
            return [line.strip() for line in text.splitlines()
                    if line.strip()][:200]
        messagebox.showerror("读取失败", "无法识别文件编码，请用 UTF-8 文本。",
                             parent=self)
        return []

    def _import(self):
        name = self._selected()
        if not name:
            return
        path = filedialog.askopenfilename(
            parent=self, title="导入词库",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        lines = self._read_lines(path)
        if not lines:
            messagebox.showwarning("没有内容", "文件里没有可导入的词。",
                                   parent=self)
            return
        if not messagebox.askyesno(
                "导入词库",
                f"用 {len(lines)} 条词替换「{name}」？\n原内容会被覆盖。",
                parent=self):
            return
        repo = self.app._repo(name)
        if repo is None:
            return
        repo["phrases"] = lines
        if self.app.active_repo == name:
            self.app._show_repo(repo)
        self.app._sync_cfg()
        self.app._save_cfg()

    def _export(self):
        name = self._selected()
        if not name:
            return
        repo = self.app._repo(name)
        if repo is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="导出词库", defaultextension=".txt",
            initialfile=f"{name}.txt",
            filetypes=[("文本文件", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write("\n".join(repo["phrases"]))
        except OSError as e:
            messagebox.showerror("导出失败", str(e), parent=self)

    def close(self):
        self.app._commit_active_repo()
        self.app._sync_cfg()
        self.app._save_cfg()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.app._manager = None
        self.destroy()


class TabBar(tk.Frame):
    """三个文字页签，选中项主题蓝加粗。"""

    def __init__(self, master, items, command):
        super().__init__(master, bg=CARD_BG)
        self.var = tk.StringVar(value=items[0][0])
        self.command = command
        self._labels = {}
        for key, text in items:
            label = tk.Label(self, text=text, bg=CARD_BG, fg=SUBTLE,
                             font=F_UI, cursor="hand2", padx=4)
            label.pack(side="left", padx=(0, 18))
            label.bind("<Button-1>", lambda e, k=key: self._pick(k))
            self._labels[key] = label
        self.var.trace_add("write", lambda *_: self._refresh())
        self._refresh()

    def _pick(self, key):
        self.var.set(key)
        if self.command:
            self.command(key)

    def _refresh(self):
        current = self.var.get()
        for key, label in self._labels.items():
            selected = key == current
            label.configure(fg=ACCENT if selected else SUBTLE,
                            font=("Microsoft YaHei UI", 10, "bold") if selected
                            else F_UI)


class SettingsWindow(tk.Toplevel):
    """设置窗：提醒 / 外观 / 系统，三页。"""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("设置")
        self.configure(bg=CARD_BG)
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.close)

        head = tk.Frame(self, bg=CARD_BG)
        head.pack(fill="x", padx=20, pady=(16, 6))
        tk.Label(head, text="设置", bg=CARD_BG, fg=FG,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")

        self.tabbar = TabBar(self,
                             [("reminder", "提醒"),
                              ("appearance", "外观"),
                              ("system", "系统")],
                             self._show)
        self.tabbar.pack(anchor="w", padx=20)

        self.container = tk.Frame(self, bg=CARD_BG)
        self.container.pack(fill="both", expand=True, padx=20, pady=(8, 0))

        self.pages = {
            "reminder": tk.Frame(self.container, bg=CARD_BG),
            "appearance": tk.Frame(self.container, bg=CARD_BG),
            "system": tk.Frame(self.container, bg=CARD_BG),
        }
        for page in self.pages.values():
            page.place(relwidth=1, relheight=1)
        self._build_reminder(self.pages["reminder"])
        self._build_appearance(self.pages["appearance"])
        self._build_system(self.pages["system"])
        self._show("reminder")

        bottom = tk.Frame(self, bg=CARD_BG)
        bottom.pack(fill="x", padx=20, pady=(8, 14))
        FlatButton(bottom, "关闭", self.close, kind="ghost",
                   padx=10, pady=4).pack(side="right")

        self.update_idletasks()
        w, h = 500, 480
        x = app.root.winfo_rootx() + (app.root.winfo_width() - w) // 2
        y = max(10, app.root.winfo_rooty() - 60)
        self.geometry(f"{w}x{h}+{max(10, x)}+{y}")
        self.update_idletasks()
        apply_window_icon(self)
        set_titlebar_theme(self, self.app.cfg.theme == "dark")

    # ---------- 小工具 ----------
    def _grid(self, page):
        grid = tk.Frame(page, bg=CARD_BG)
        grid.pack(fill="x", pady=6)
        grid.columnconfigure(1, weight=1)
        return grid

    def _label(self, grid, row, text):
        tk.Label(grid, text=text, bg=CARD_BG, fg=FG_SOFT, font=F_UI_SM,
                 anchor="w", width=9).grid(row=row, column=0, sticky="w",
                                           pady=6)

    def _toggle(self, parent, var, command=None):
        return FlatToggle(parent, var, command=command)

    def _show(self, key):
        page = self.pages.get(key)
        if page is not None:
            page.lift()

    def _refresh_plan(self, *_):
        clock = self.app.schedule_mode_var.get() == "clock"
        for widget, visible in ((self.interval_box, not clock),
                                (self.clock_box, clock)):
            try:
                if not widget.winfo_exists():
                    continue
                if visible:
                    widget.grid()
                else:
                    widget.grid_remove()
            except tk.TclError:
                pass

    def _on_sound_choice(self):
        self.app._sync_cfg()
        if self.app.sound_var.get():
            self.app._play_chime()

    # ---------- 提醒页 ----------
    def _build_reminder(self, page):
        grid = self._grid(page)

        self._label(grid, 0, "计划")
        plan = TextSwitch(grid, [("interval", "间隔"), ("clock", "定时")],
                          self.app.schedule_mode_var,
                          command=lambda: (self._refresh_plan(),
                                           self.app._sync_cfg()),
                          bg=CARD_BG)
        plan.grid(row=0, column=1, sticky="w", pady=4)

        self.interval_box = tk.Frame(grid, bg=CARD_BG)
        self.interval_box.grid(row=1, column=1, sticky="w", pady=4)
        tk.Label(self.interval_box, text="间隔", bg=CARD_BG, fg=FG_SOFT,
                 font=F_UI_SM).pack(side="left")
        self.app._mk_entry_widget(self.interval_box,
                                  self.app.interval_var).pack(
                                      side="left", padx=(6, 4), ipady=3)
        tk.Label(self.interval_box, text="秒", bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left")

        self.duration_box = tk.Frame(grid, bg=CARD_BG)
        self.duration_box.grid(row=2, column=1, sticky="w", pady=4)
        tk.Label(self.duration_box, text="显示", bg=CARD_BG, fg=FG_SOFT,
                 font=F_UI_SM).pack(side="left")
        self.app._mk_entry_widget(self.duration_box,
                                  self.app.duration_var).pack(
                                      side="left", padx=(6, 4), ipady=3)
        tk.Label(self.duration_box, text="秒", bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left")

        self.clock_box = tk.Frame(grid, bg=CARD_BG)
        self.clock_box.grid(row=3, column=1, sticky="w", pady=4)
        tk.Label(self.clock_box, text="时间", bg=CARD_BG, fg=FG_SOFT,
                 font=F_UI_SM).pack(side="left")
        entry = self.app._mk_entry_widget(self.clock_box,
                                          self.app.clock_times_var)
        entry.configure(width=18, justify="left")
        entry.pack(side="left", padx=(6, 6), ipady=3)
        tk.Label(self.clock_box, text="如 10:00, 14:30", bg=CARD_BG,
                 fg=SUBTLE, font=F_UI_SM).pack(side="left")

        self._label(grid, 4, "星期")
        week = tk.Frame(grid, bg=CARD_BG)
        week.grid(row=4, column=1, sticky="w", pady=4)
        self._week_labels = []
        for i, text in enumerate(WEEK_LABELS):
            label = tk.Label(week, text=text, bg=CARD_BG, fg=SUBTLE,
                             font=F_UI_SM, width=2, cursor="hand2")
            label.pack(side="left", padx=(0, 6))
            label.bind("<Button-1>", lambda e, i=i: self._toggle_week(i))
            self.app.weekday_vars[i].trace_add(
                "write", lambda *_, i=i: self._paint_week())
            self._week_labels.append(label)
        self._paint_week()

        self._label(grid, 5, "活跃时段")
        active = tk.Frame(grid, bg=CARD_BG)
        active.grid(row=5, column=1, sticky="w", pady=4)
        self._toggle(active, self.app.active_enabled_var).pack(side="left")
        tk.Label(active, text="只在", bg=CARD_BG, fg=FG_SOFT,
                 font=F_UI_SM).pack(side="left", padx=(10, 4))
        self.app._mk_entry_widget(active, self.app.active_start_var).pack(
            side="left", ipady=2)
        tk.Label(active, text="–", bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left", padx=3)
        self.app._mk_entry_widget(active, self.app.active_end_var).pack(
            side="left", ipady=2)
        tk.Label(active, text="提醒", bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left", padx=(4, 0))

        self._label(grid, 6, "全屏")
        full = tk.Frame(grid, bg=CARD_BG)
        full.grid(row=6, column=1, sticky="w", pady=4)
        self._toggle(full, self.app.fullscreen_var).pack(side="left")
        tk.Label(full, text="游戏/会议全屏时延后", bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left", padx=(10, 0))

        self._label(grid, 7, "提示音")
        sound = tk.Frame(grid, bg=CARD_BG)
        sound.grid(row=7, column=1, sticky="w", pady=4)
        FlatToggle(sound, self.app.sound_var,
                   command=self.app._on_sound_toggle).pack(side="left")
        tk.Label(sound, text="音色", bg=CARD_BG, fg=FG_SOFT,
                 font=F_UI_SM).pack(side="left", padx=(12, 6))
        FlatSelect(sound, self.app.sound_label_var,
                   [label for _key, label in SOUND_CHOICES],
                   command=self._on_sound_choice, width=84).pack(side="left")

        self._label(grid, 8, "稍后")
        snooze = tk.Frame(grid, bg=CARD_BG)
        snooze.grid(row=8, column=1, sticky="w", pady=4)
        self.app._mk_entry_widget(snooze, self.app.snooze_var).pack(
            side="left", ipady=2)
        tk.Label(snooze, text="分钟", bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left", padx=(6, 0))

        self._refresh_plan()
        self.app.schedule_mode_var.trace_add("write",
                                             lambda *_: self._refresh_plan())

    def _toggle_week(self, i):
        var = self.app.weekday_vars[i]
        new = not var.get()
        if not new and not any(v.get() for j, v in
                               enumerate(self.app.weekday_vars) if j != i):
            return
        var.set(new)
        self.app._sync_cfg()

    def _paint_week(self):
        for i, label in enumerate(getattr(self, "_week_labels", [])):
            on = self.app.weekday_vars[i].get()
            try:
                label.configure(fg=ACCENT if on else SUBTLE,
                                font=("Microsoft YaHei UI", 9, "bold") if on
                                else F_UI_SM)
            except tk.TclError:
                return

    # ---------- 外观页 ----------
    def _build_appearance(self, page):
        grid = self._grid(page)

        self._label(grid, 0, "主题")
        TextSwitch(grid, [("light", "浅色"), ("dark", "深色")],
                   self.app.theme_var, command=self.app._on_theme_change,
                   bg=CARD_BG).grid(row=0, column=1, sticky="w", pady=4)

        self._label(grid, 1, "字体")
        fontrow = tk.Frame(grid, bg=CARD_BG)
        fontrow.grid(row=1, column=1, sticky="w", pady=4)
        values = list(dict.fromkeys([self.app.cfg.font_family] + FONT_CHOICES))
        FlatSelect(fontrow, self.app.font_var, values,
                   command=self.app._sync_cfg, width=168).pack(side="left")
        tk.Label(fontrow, text="字号", bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left", padx=(10, 6))
        FlatSelect(fontrow, self.app.font_size_var, SIZE_CHOICES,
                   command=self.app._sync_cfg, width=72).pack(side="left")

        self._label(grid, 2, "文字颜色")
        self.app._text_swatches = []
        self.app._color_row(grid, 2, self.app._text_swatches,
                            TEXT_COLORS, self.app.text_color_var)

        self._label(grid, 3, "弹窗底色")
        self.app._bg_swatches = []
        self.app._color_row(grid, 3, self.app._bg_swatches,
                            POPUP_BGS, self.app.bg_color_var)

        self._label(grid, 4, "弹出位置")
        posrow = tk.Frame(grid, bg=CARD_BG)
        posrow.grid(row=4, column=1, sticky="w", pady=4)
        FlatSelect(posrow, self.app.pos_var, POSITIONS,
                   command=self.app._sync_cfg, width=90).pack(side="left")

        self._label(grid, 5, "窗口大小")
        sizerow = tk.Frame(grid, bg=CARD_BG)
        sizerow.grid(row=5, column=1, sticky="w", pady=4)
        FlatSlider(sizerow, self.app.size_var, from_=1, to=25,
                   command=self.app._on_size, width=150,
                   height=24).pack(side="left")
        self.app.lbl_size = tk.Label(sizerow, text=self.app._size_text,
                                     bg=CARD_BG, fg=SUBTLE,
                                     font=F_UI_SM, width=16, anchor="w")
        self.app.lbl_size.pack(side="left", padx=(8, 0))
        self.app._on_size()
        self.app._refresh_swatches()

    # ---------- 系统页 ----------
    def _build_system(self, page):
        grid = self._grid(page)

        self._label(grid, 0, "开机自启")
        FlatToggle(grid, self.app.autostart_var,
                   command=self.app._toggle_autostart).grid(
                       row=0, column=1, sticky="w", pady=6)

        self._label(grid, 1, "全局热键")
        hot = tk.Frame(grid, bg=CARD_BG)
        hot.grid(row=1, column=1, sticky="w", pady=6)
        FlatToggle(hot, self.app.hotkeys_var,
                   command=self.app._sync_cfg).pack(side="left")
        tk.Label(hot, text="Ctrl+Alt+R 开始/停止 · Ctrl+Alt+P 暂停",
                 bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left", padx=(10, 0))

        self._label(grid, 2, "关闭到托盘")
        tray = tk.Frame(grid, bg=CARD_BG)
        tray.grid(row=2, column=1, sticky="w", pady=6)
        FlatToggle(tray, self.app.tray_var,
                   command=self.app._sync_cfg).pack(side="left")
        tk.Label(tray, text="关窗口后收到托盘，托盘菜单退出",
                 bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left", padx=(10, 0))

        self._label(grid, 3, "词库")
        FlatButton(grid, "管理 / 导入 / 导出…",
                   self.app._open_repo_manager, kind="card_ghost",
                   padx=8, pady=3).grid(row=3, column=1, sticky="w", pady=6)

    # ---------- 显示 / 收起 ----------
    def show(self):
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def close(self):
        """收起而不是销毁：避免重复创建控件和变量 trace。"""
        self.app.cfg = self.app._collect_cfg()
        self.app._save_cfg()
        self.app._refresh_status()
        self.withdraw()


class CountdownReminder:

    def __init__(self):
        self.root = tk.Tk()
        _install_error_handling(self.root)
        self.root.title("倒计时提醒器")
        self.root.configure(bg=APP_BG)
        self.root.resizable(False, False)

        self.cfg = Config.load()

        # —— 词库（仓）——
        self.repos = [{"name": r["name"], "phrases": list(r["phrases"]),
                       "mode": r["mode"]} for r in self.cfg.repos]
        names = [r["name"] for r in self.repos]
        self.active_repo = (self.cfg.active_repo if self.cfg.active_repo in names
                            else names[0])
        self._repo_pos = {}          # 每个仓独立的顺序进度
        self._manager = None         # 词库管理窗口
        self._settings = None        # 设置窗口
        self._tray = None            # 托盘图标
        self._quitting = False
        self._bags = {}              # 随机不重复的洗牌袋

        # —— 运行期状态 ——
        self.phase          = Phase.IDLE
        self._after_id      = None   # 唯一的运行期定时器句柄
        self._preview_id    = None   # 预览用独立句柄
        self._heartbeat_id  = None   # 状态刷新心跳
        self._deadline      = None   # 当前阶段到期时刻（monotonic）
        self._last_arm_wall = None   # 上次排程的墙钟时刻（休眠检测）
        self._fit_cache     = None   # 字号自适应缓存
        self._progress_id   = None   # 弹窗底部进度线动画句柄
        self._progress_t0   = 0.0
        self._progress_total = 1.0
        self._fade_id       = None   # 弹窗淡入/淡出句柄
        self._pause_until   = None   # 暂停到（datetime）
        self._snooze_until  = None   # 稍后提醒（datetime）
        self._next_due      = None   # 下一次提醒（datetime）
        self._hotkey_state  = {}
        self._size_text     = ""

        self._init_vars()
        apply_palette(self.cfg.theme)
        self.root.configure(bg=APP_BG)
        self._build_main_ui()
        self._build_popup()
        if self.cfg.sound:
            try:
                _chime_wav(self.cfg.sound_name)
            except Exception:
                pass
        self._load_cfg_to_ui()
        self._on_size()
        self._center()
        apply_window_icon(self.root)
        set_titlebar_theme(self.root, self.cfg.theme == "dark")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", self._on_escape)
        self._apply_phase_ui()
        # 默认焦点放在内容框：避免按钮一启动就带焦点描边
        self.txt.focus_set()
        self._poll_hotkeys()
        self._tray = TrayIcon(self)

    def _init_vars(self):
        cfg = self.cfg
        self.interval_var = tk.StringVar(value=_fmt_num(cfg.interval))
        self.duration_var = tk.StringVar(value=_fmt_num(cfg.duration))
        self.font_var = tk.StringVar(value=cfg.font_family)
        self.font_size_var = tk.StringVar(
            value="自动" if cfg.font_size == 0 else str(cfg.font_size))
        self.text_color_var = tk.StringVar(value=cfg.text_color)
        self.bg_color_var = tk.StringVar(value=cfg.popup_bg)
        self.pos_var = tk.StringVar(value=cfg.position)
        self.size_var = tk.DoubleVar(value=cfg.size)
        self.sound_var = tk.BooleanVar(value=cfg.sound)
        self.sound_label_var = tk.StringVar(
            value=dict(SOUND_CHOICES).get(cfg.sound_name, "柔和"))
        self.schedule_mode_var = tk.StringVar(value=cfg.schedule_mode)
        self.clock_times_var = tk.StringVar(value=", ".join(cfg.clock_times))
        self.weekday_vars = [tk.BooleanVar(value=i in cfg.weekdays)
                             for i in range(7)]
        self.active_enabled_var = tk.BooleanVar(value=cfg.active_enabled)
        self.active_start_var = tk.StringVar(value=cfg.active_start)
        self.active_end_var = tk.StringVar(value=cfg.active_end)
        self.fullscreen_var = tk.BooleanVar(value=cfg.fullscreen_defer)
        self.snooze_var = tk.StringVar(value=str(cfg.snooze_minutes))
        self.autostart_var = tk.BooleanVar(value=cfg.autostart)
        self.hotkeys_var = tk.BooleanVar(value=cfg.hotkeys)
        self.tray_var = tk.BooleanVar(value=cfg.close_to_tray)
        self.theme_var = tk.StringVar(value=cfg.theme)
        self._text_swatches = []
        self._bg_swatches = []

    # ---------------- 词库（仓） ----------------
    def _repo_names(self):
        return [repo["name"] for repo in self.repos]

    def _repo(self, name=None):
        name = name or self.active_repo
        for repo in self.repos:
            if repo["name"] == name:
                return repo
        return self.repos[0] if self.repos else None

    def _commit_active_repo(self):
        """把编辑框和模式写回当前仓（允许空仓）。"""
        repo = self._repo()
        if repo is None:
            return
        repo["phrases"] = [line.strip() for line in
                           self.txt.get("1.0", "end-1c").splitlines()
                           if line.strip()]
        repo["mode"] = self.phrase_mode_var.get()

    def _show_repo(self, repo):
        if repo is None:
            return
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", "\n".join(repo["phrases"]))
        self.phrase_mode_var.set(repo["mode"])

    def _activate_repo(self, name):
        if name not in self._repo_names() or name == self.active_repo:
            return
        self._commit_active_repo()
        repo = self._repo(name)
        if repo is None:
            return
        self.active_repo = name
        self._show_repo(repo)
        self.repo_var.set(name)
        self._sync_cfg()

    def _refresh_repo_widgets(self):
        names = self._repo_names()
        if not names:
            return
        if self.active_repo not in names:
            self.active_repo = names[0]
        self.repo_select.configure(values=names)
        self.repo_var.set(self.active_repo)
        self._show_repo(self._repo())

    # ---------------- 新建仓 ----------------
    def _next_repo_name(self):
        names = set(self._repo_names())
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            name = f"仓{ch}"
            if name not in names:
                return name
        index = 2
        while f"仓{index}" in names:
            index += 1
        return f"仓{index}"

    def _repo_name_problem(self, name, exclude=None):
        name = name.strip()
        if not name:
            return "名称不能为空"
        if len(name) > 24:
            return "名称最多 24 个字符"
        if name in [r["name"] for r in self.repos if r["name"] != exclude]:
            return f"已经有名为「{name}」的词库"
        return None

    def _create_repo(self, parent=None):
        parent = parent or self.root
        name = simpledialog.askstring("新建词库", "名称：",
                                      initialvalue=self._next_repo_name(),
                                      parent=parent)
        if name is None:
            return None
        name = name.strip()
        problem = self._repo_name_problem(name)
        if problem:
            messagebox.showwarning("无法新建", problem, parent=parent)
            return None
        self._commit_active_repo()
        self.repos.append({"name": name, "phrases": [], "mode": "sequence"})
        self.active_repo = name
        self._repo_pos[name] = 0
        self._refresh_repo_widgets()
        self._sync_cfg()
        self._save_cfg()
        return name

    def _on_repo_switch(self):
        self._activate_repo(self.repo_var.get())

    def _on_phrase_mode(self):
        repo = self._repo()
        if repo is not None:
            repo["mode"] = self.phrase_mode_var.get()
        self._repo_pos[self.active_repo] = 0
        self._sync_cfg()

    # ---------------- 配置 <-> UI ----------------
    def _load_cfg_to_ui(self):
        self.repo_select.configure(values=self._repo_names())
        self.repo_var.set(self.active_repo)
        self._show_repo(self._repo())

    def _collect_cfg(self):
        """从当前界面变量采集配置（非法项回退到上次合法值）。"""
        def field(key, value):
            try:
                return Config._coerce(key, value)
            except (KeyError, TypeError, ValueError):
                return getattr(self.cfg, key)

        def seconds(var, key):
            try:
                return Config.seconds(var.get())
            except (TypeError, ValueError):
                return getattr(self.cfg, key)

        try:
            size = Config.percent(self.size_var.get())
        except (TypeError, ValueError):
            size = self.cfg.size

        font_size = 0 if self.font_size_var.get() == "自动" else self.font_size_var.get()

        # 定时时间：容许中英文逗号、空格分隔
        raw_times = re.split(r"[,，\s]+", self.clock_times_var.get().strip())
        times = [s for s in raw_times if TIME_RE.match(s)]
        if not times:
            times = list(self.cfg.clock_times)
        weekdays = [i for i, var in enumerate(self.weekday_vars) if var.get()]
        if not weekdays:
            weekdays = list(self.cfg.weekdays)
        sound_map = {label: key for key, label in SOUND_CHOICES}
        sound_name = sound_map.get(self.sound_label_var.get(),
                                   self.cfg.sound_name)
        try:
            snooze_value = int(float(self.snooze_var.get()))
        except (TypeError, ValueError):
            snooze_value = self.cfg.snooze_minutes

        self._commit_active_repo()
        repos = [{"name": repo["name"], "phrases": list(repo["phrases"]),
                  "mode": repo["mode"]} for repo in self.repos]
        names = [repo["name"] for repo in repos]
        active = self.active_repo if self.active_repo in names else names[0]

        return Config(
            repos=repos,
            active_repo=active,
            sound=field("sound", self.sound_var.get()),
            sound_name=field("sound_name", sound_name),
            interval=seconds(self.interval_var, "interval"),
            duration=seconds(self.duration_var, "duration"),
            position=field("position", self.pos_var.get()),
            size=size,
            schedule_mode=field("schedule_mode", self.schedule_mode_var.get()),
            clock_times=field("clock_times", times),
            weekdays=field("weekdays", weekdays),
            active_enabled=field("active_enabled", self.active_enabled_var.get()),
            active_start=field("active_start", self.active_start_var.get()),
            active_end=field("active_end", self.active_end_var.get()),
            fullscreen_defer=field("fullscreen_defer",
                                   self.fullscreen_var.get()),
            snooze_minutes=field("snooze_minutes", snooze_value),
            autostart=field("autostart", self.autostart_var.get()),
            hotkeys=field("hotkeys", self.hotkeys_var.get()),
            close_to_tray=field("close_to_tray", self.tray_var.get()),
            theme=field("theme", self.theme_var.get()),
            font_family=field("font_family", self.font_var.get()),
            font_size=field("font_size", font_size),
            text_color=field("text_color", self.text_color_var.get()),
            popup_bg=field("popup_bg", self.bg_color_var.get()),
        )

    def _open_repo_manager(self):
        self._commit_active_repo()
        if self._manager is not None and self._manager.winfo_exists():
            self._manager.lift()
            return
        self._manager = RepoManager(self)

    def _open_settings(self):
        if self.phase is not Phase.IDLE:
            return
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.show()
            return
        self._settings = SettingsWindow(self)

    # ---------------- 深色模式 ----------------
    def _on_theme_change(self):
        if self.phase is not Phase.IDLE:
            return
        self.cfg = self._collect_cfg()
        self._save_cfg()
        self.root.after(10, lambda: self._rebuild_for_theme(True))

    def _rebuild_for_theme(self, reopen=False):
        if self.phase is not Phase.IDLE:
            return
        for win in (self._settings, self._manager):
            if win is not None:
                try:
                    win.destroy()
                except tk.TclError:
                    pass
        self._settings = None
        self._manager = None
        self._cancel_preview()
        self._cancel_progress()
        self._cancel_fade()
        for child in list(self.root.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:
                pass
        apply_palette(self.cfg.theme)
        self.root.configure(bg=APP_BG)
        self._init_vars()
        self._build_main_ui()
        self._build_popup()
        self._load_cfg_to_ui()
        self._on_size()
        self._center()
        apply_window_icon(self.root)
        set_titlebar_theme(self.root, self.cfg.theme == "dark")
        self._apply_phase_ui()
        self.txt.focus_set()
        if reopen:
            self._open_settings()
            try:
                self._settings.tabbar._pick("appearance")
            except Exception:
                pass

    # ---------------- 开机自启 / 热键 ----------------
    def _autostart_command(self):
        exe = sys.executable
        if getattr(sys, "frozen", False):
            return f'"{exe}"'
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        runner = pyw if os.path.exists(pyw) else exe
        script = os.path.abspath(sys.argv[0])
        return f'"{runner}" "{script}"'

    def _set_autostart(self, enabled):
        try:
            import winreg
            path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0,
                                    winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, "CountdownReminder", 0,
                                      winreg.REG_SZ, self._autostart_command())
                else:
                    try:
                        winreg.DeleteValue(key, "CountdownReminder")
                    except FileNotFoundError:
                        pass
            return True
        except Exception as e:
            log.warning("设置开机自启失败：%s", e)
            return False

    def _toggle_autostart(self):
        wanted = self.autostart_var.get()
        if not self._set_autostart(wanted):
            self.autostart_var.set(not wanted)
            messagebox.showwarning("设置失败",
                                   "无法写入开机自启（详见日志）。",
                                   parent=self.root)
        self._sync_cfg()

    def _poll_hotkeys(self):
        try:
            ctrl = bool(user32.GetAsyncKeyState(0x11) & 0x8000)
            alt = bool(user32.GetAsyncKeyState(0x12) & 0x8000)
            for vk, action in ((0x52, self._hotkey_toggle),
                               (0x50, self._hotkey_pause)):
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                was = self._hotkey_state.get(vk, False)
                self._hotkey_state[vk] = down
                if down and not was and ctrl and alt and self.cfg.hotkeys:
                    action()
        except Exception:
            pass
        self.root.after(80, self._poll_hotkeys)

    def _hotkey_toggle(self):
        if self.phase is Phase.IDLE:
            self.start()
        else:
            self.stop()

    def _hotkey_pause(self):
        if self.phase is not Phase.IDLE:
            self._toggle_pause()

    def _save_cfg(self):
        if not self.cfg.save():
            messagebox.showwarning(
                "保存失败",
                "配置未能写入磁盘（详见日志），本次运行仍会正常生效。",
                parent=self.root,
            )

    # ---------------- 主界面 ----------------
    def _build_main_ui(self):
        outer = 24

        head = tk.Frame(self.root, bg=APP_BG)
        head.pack(fill="x", padx=outer, pady=(22, 0))
        tk.Label(head, text="倒计时提醒器", bg=APP_BG, fg=FG,
                 font=F_TITLE).pack(anchor="w")
        tk.Label(head, text="到点轻提醒，不打断手头的事",
                 bg=APP_BG, fg=SUBTLE, font=F_UI_SM).pack(anchor="w", pady=(4, 0))

        card = tk.Frame(self.root, bg=CARD_BG)
        card.pack(fill="x", padx=outer, pady=(16, 0))
        body = tk.Frame(card, bg=CARD_BG)
        body.pack(fill="both", expand=True, padx=20, pady=18)

        content_head = tk.Frame(body, bg=CARD_BG)
        content_head.pack(fill="x")
        tk.Label(content_head, text="提醒内容", bg=CARD_BG, fg=FG,
                 font=F_SECTION).pack(side="left")
        tk.Label(content_head, text="每行一条", bg=CARD_BG, fg=SUBTLE,
                 font=F_UI_SM).pack(side="left", padx=(8, 0))
        self.phrase_mode_var = tk.StringVar(value="sequence")
        self.mode_switch = TextSwitch(
            content_head,
            [("sequence", "顺序"), ("random", "随机")],
            self.phrase_mode_var,
            command=self._on_phrase_mode,
            bg=CARD_BG,
        )
        self.mode_switch.pack(side="right")

        self.txt = tk.Text(body, width=1, height=3, font=F_UI,
                           bg=FIELD_BG, fg=FG, relief="flat", wrap="word",
                           padx=12, pady=9, bd=0, insertbackground=FG,
                           highlightthickness=1, highlightbackground=FIELD_BG,
                           highlightcolor=ACCENT,
                           selectbackground=ACCENT_SOFT, selectforeground=FG)
        self.txt.pack(fill="x", pady=(8, 0))

        repo_row = tk.Frame(body, bg=CARD_BG)
        repo_row.pack(fill="x", pady=(8, 0))
        tk.Label(repo_row, text="词库", bg=CARD_BG, fg=FG_SOFT,
                 font=F_UI_SM).pack(side="left")
        self.repo_var = tk.StringVar(value=self.active_repo)
        self.repo_select = FlatSelect(repo_row, self.repo_var,
                                      self._repo_names(),
                                      command=self._on_repo_switch,
                                      width=84)
        self.repo_select.pack(side="left", padx=(8, 0))
        self.btn_new_repo = FlatButton(repo_row, "＋ 新建",
                                       self._create_repo, kind="secondary",
                                       padx=8, pady=3)
        self.btn_new_repo.pack(side="left", padx=(8, 0))

        # —— 状态提示 ——
        status = tk.Frame(self.root, bg=APP_BG)
        status.pack(fill="x", padx=outer + 2, pady=(12, 0))
        self.dot = tk.Label(status, text="●", bg=APP_BG, fg=SUBTLE,
                            font=F_UI_SM)
        self.dot.pack(side="left")
        self.lbl_status = tk.Label(status, text="空闲", bg=APP_BG, fg=SUBTLE,
                                   font=F_UI_SM)
        self.lbl_status.pack(side="left", padx=(6, 0))

        # —— 底部操作 ——
        btns = tk.Frame(self.root, bg=APP_BG)
        btns.pack(fill="x", padx=outer, pady=(14, 20), side="bottom")
        self.btn_start = FlatButton(btns, "开始", self.start, kind="primary",
                                    padx=30, pady=8)
        self.btn_start.pack(side="left")
        self.btn_pause = FlatButton(btns, "暂停", self._toggle_pause,
                                    padx=14)
        self.btn_pause.pack(side="left", padx=(10, 0))
        self.btn_preview = FlatButton(btns, "预览", self.preview, padx=16)
        self.btn_preview.pack(side="left", padx=(10, 0))
        self.btn_settings = FlatButton(btns, "设置", self._open_settings,
                                       kind="ghost", padx=10)
        self.btn_settings.pack(side="right")

    def _mk_entry_widget(self, parent, var):
        entry = tk.Entry(parent, width=6, font=F_UI, bg=FIELD_BG, fg=FG,
                         relief="flat", justify="center", bd=0,
                         insertbackground=FG, textvariable=var,
                         highlightthickness=1, highlightbackground=FIELD_BG,
                         highlightcolor=ACCENT,
                         selectbackground=ACCENT_SOFT, selectforeground=FG)
        return entry

    def _color_row(self, grid, row, bucket, colors, var):
        box = tk.Frame(grid, bg=CARD_BG)
        box.grid(row=row, column=1, sticky="w", pady=6)
        for color in colors:
            sw = tk.Canvas(box, width=18, height=18, bg=color, bd=0,
                           highlightthickness=2, highlightbackground=BORDER,
                           cursor="hand2")
            sw.pack(side="left", padx=(0, 6))
            sw.bind("<Button-1>", lambda e, c=color: self._set_color(var, c))
            bucket.append((sw, color))
        plus = tk.Label(box, text="＋", bg=CARD_BG, fg=SUBTLE, font=F_UI_SM,
                        width=2, cursor="hand2")
        plus.pack(side="left")
        plus.bind("<Button-1>", lambda e: self._pick_custom_color(var))
        return box

    def _set_color(self, var, color):
        var.set(color.upper())
        self._sync_cfg()
        self._refresh_swatches()

    def _pick_custom_color(self, var):
        parent = (self._settings
                  if self._settings is not None and self._settings.winfo_exists()
                  else self.root)
        _rgb, hex_color = colorchooser.askcolor(
            color=var.get(), parent=parent, title="选择颜色")
        if hex_color:
            var.set(hex_color.upper())
            self._sync_cfg()
            self._refresh_swatches()

    def _refresh_swatches(self):
        pairs = [(self._text_swatches, self.text_color_var),
                 (self._bg_swatches, self.bg_color_var)]
        for bucket, var in pairs:
            current = var.get().upper()
            for sw, color in bucket:
                sw.configure(highlightbackground=ACCENT
                             if color.upper() == current else BORDER)

    # ---------------- 词库 / 提示音 ----------------
    def _next_phrase(self):
        lines = [line.strip() for line in
                 self.txt.get("1.0", "end-1c").splitlines() if line.strip()]
        if not lines:
            return "时间到！"
        if len(lines) == 1:
            return lines[0]
        if self.phrase_mode_var.get() == "random":
            bag = [i for i in self._bags.get(self.active_repo, [])
                   if i < len(lines)]
            if not bag:
                bag = list(range(len(lines)))
                random.shuffle(bag)
                self._bags[self.active_repo] = bag
            index = bag.pop()
            self._bags[self.active_repo] = bag
            return lines[index]
        pos = self._repo_pos.get(self.active_repo, 0)
        index = pos % len(lines)
        self._repo_pos[self.active_repo] = (index + 1) % len(lines)
        return lines[index]

    def _on_sound_toggle(self):
        self.cfg = self._collect_cfg()
        if self.cfg.sound:
            self._play_chime()

    def _play_chime(self):
        if not self.cfg.sound:
            return
        try:
            import winsound
        except Exception as e:
            log.warning("提示音不可用：%s", e)
            return
        path = _chime_path(self.cfg.sound_name)
        if path:
            try:
                winsound.PlaySound(
                    path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC |
                    winsound.SND_NODEFAULT,
                )
                return
            except Exception as e:
                log.warning("播放提示音失败：%s", e)
        # 文件播放失败时退回系统提示音，至少别完全没声
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    def _sync_cfg(self):
        """更多选项里的改动即时生效（不写盘，开始/退出时统一保存）"""
        self.cfg = self._collect_cfg()
        self._fit_cache = None
        if hasattr(self, "_font"):
            self._font.configure(family=self.cfg.font_family)
            self._apply_popup_colors()

    def _apply_popup_colors(self):
        bg = self.cfg.popup_bg
        self.popup.configure(bg=bg)
        self._card.configure(bg=bg)
        self._popup_body.configure(bg=bg)
        self._progress.configure(bg=bg)
        self.popup_label.configure(bg=bg, fg=self.cfg.text_color)

    def _mk_entry(self, parent):
        return tk.Entry(parent, width=6, font=F_UI, bg=FIELD_BG, fg=FG,
                        relief="flat", justify="center", bd=0,
                        insertbackground=FG,
                        highlightthickness=1, highlightbackground=FIELD_BG,
                        highlightcolor=ACCENT,
                        selectbackground=ACCENT_SOFT, selectforeground=FG)

    def _mark_invalid(self, entry):
        entry.config(highlightbackground=ERROR, highlightcolor=ERROR)

    def _mark_valid(self, entry):
        entry.config(highlightbackground=FIELD_BG, highlightcolor=ACCENT)

    def _center(self):
        # 尺寸由内容决定：不同 DPI / 字体下都不会截断底部按钮
        self.root.update_idletasks()
        w = max(460, self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ---------------- 弹窗 ----------------
    def _build_popup(self):
        # 先给 TkTopLevel 类加投影，再建弹窗（类样式只影响之后创建的窗口）
        self.root.update_idletasks()      # 确保主窗口句柄已经真实创建
        root_h = root_hwnd(self.root)
        if root_h:
            enable_drop_shadow(root_h)

        self.popup = tk.Toplevel(self.root)
        self.popup.overrideredirect(True)
        self.popup.attributes("-topmost", True)
        self.popup.configure(bg=CARD_BG)
        self.popup.geometry("1x1+-4000+-4000")

        # 极简卡片：无描边，只用留白 + 底部 3px 进度线
        self._card = tk.Frame(self.popup, bg=CARD_BG)
        self._card.pack(fill="both", expand=True)
        self._popup_body = tk.Frame(self._card, bg=CARD_BG)
        self._popup_body.pack(fill="both", expand=True)
        self._progress = tk.Canvas(self._card, height=3, bg=CARD_BG,
                                   bd=0, highlightthickness=0)
        self._progress.pack(fill="x", side="bottom")

        self._font = tkfont.Font(family=self.cfg.font_family, size=-18)
        self.popup_label = tk.Label(self._popup_body, text="", bg=CARD_BG,
                                    fg=self.cfg.text_color, font=self._font,
                                    justify="center", wraplength=200,
                                    anchor="center")
        self.popup_label.place(relx=0.5, rely=0.5, anchor="center")

        # 单击=立即收起并重新计时；右键=停止（改进）
        # 注意：弹窗带 WS_EX_NOACTIVATE，永远拿不到键盘焦点，因此不要给它绑
        # 任何键盘事件（Esc 之类），那是不会触发的死代码。
        for w in (self.popup, self._card, self._popup_body,
                  self.popup_label):
            w.bind("<Button-1>", self._on_popup_click)
            w.bind("<Button-3>", self._on_popup_menu)

        self.popup.update_idletasks()
        hwnd = root_hwnd(self.popup)
        if hwnd:
            make_noactivate(hwnd)
        self.popup.withdraw()

    # ---------------- 尺寸 / 位置计算 ----------------
    def _calc_size(self):
        L, T, R, B = work_area()
        W, H = max(1, R - L), max(1, B - T)
        pct = float(self.size_var.get())
        w = int(W * pct / 100.0)
        h = int(H * pct / 100.0)
        return max(80, min(w, W)), max(52, min(h, H))

    def _calc_pos(self, w, h):
        L, T, R, B = work_area()
        W, H = R - L, B - T
        m = 24
        p = self.pos_var.get()
        if p not in POSITIONS:
            p = "右下"
        if p == "随机":
            p = random.choice(POSITIONS[:-1])

        left   = L + m
        center = L + (W - w) // 2
        right  = R - w - m
        top    = T + m
        middle = T + (H - h) // 2
        bottom = B - h - m

        table = {
            "左上": (left,   top),    "上": (center, top),    "右上": (right,  top),
            "左":   (left,   middle), "中": (center, middle), "右":  (right,  middle),
            "左下": (left,   bottom), "下": (center, bottom), "右下": (right,  bottom),
        }
        return table.get(p, (center, middle))

    def _on_size(self, _=None):
        w, h = self._calc_size()
        self._size_text = f"{int(self.size_var.get())}%  最大 {w}×{h}"
        label = getattr(self, "lbl_size", None)
        try:
            if label is not None and label.winfo_exists():
                label.config(text=self._size_text)
        except tk.TclError:
            pass

    # ---------------- 绘制弹窗 ----------------
    def _fit_font(self, text, w, h):
        """二分查找能完整放下的最大字号。

        实际换行由 Tk 按 wraplength 计算，和“总宽 ÷ 可用宽”的估算经常
        对不上（CJK 逐字断行、行尾留白），所以这里直接量 Label 的
        requested size，宁可在隐藏状态下多排几次版，也不让文字被裁掉。
        """
        family = self.cfg.font_family
        wanted = self.cfg.font_size
        key = (text, w, h, family, wanted)
        if self._fit_cache and self._fit_cache[0] == key:
            return self._fit_cache[1]
        # 留出内边距和底部 3px 进度线
        avail_w = max(24, w - 52)
        avail_h = max(20, h - 59)
        label = self.popup_label
        best = 9
        if wanted > 0:
            hi = min(96, wanted)
        else:
            # 自动模式封顶 30px：提醒不是海报，字太大反而显得土
            hi = max(9, min(int(h * 0.34), int(w * 0.16), 24))
        lo = 9
        while lo <= hi:
            mid = (lo + hi) // 2
            self._font.configure(size=-mid)
            label.configure(text=text, font=self._font, wraplength=avail_w)
            label.update_idletasks()
            # 换行后 Label 的请求宽度会等于 wraplength + 边框余量（约 2px），
            # 不能按 avail_w+1 卡太死，否则所有多行排版都会被误判为放不下。
            fits = (label.winfo_reqwidth() <= avail_w + 6
                    and label.winfo_reqheight() <= avail_h)
            if fits:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        self._fit_cache = (key, best)
        return best

    def _paint_popup(self, duration=None):
        text = self._next_phrase() or "时间到！"
        self._play_chime()
        # 百分比先当作“最大尺寸”，再按文字实际占用量收缩卡片，
        # 短句不会撑成一块大空白，长句仍可用满用户设定的尺寸。
        max_w, max_h = self._calc_size()

        self._font.configure(family=self.cfg.font_family)
        self._apply_popup_colors()
        fs = self._fit_font(text, max_w, max_h)
        self._font.configure(size=-fs)
        self.popup_label.configure(text=text, font=self._font,
                                   wraplength=max(24, max_w - 52))
        self.popup_label.update_idletasks()

        pad_x, pad_y, gap = 26, 24, 8
        w = min(max_w, max(160, self.popup_label.winfo_reqwidth() + 2 * pad_x))
        h = min(max_h, max(80, self.popup_label.winfo_reqheight()
                           + 2 * pad_y + gap + 3))
        x, y = self._calc_pos(w, h)

        was_visible = self.popup.state() != "withdrawn"
        self._cancel_fade()
        try:
            self.popup.attributes("-alpha", 1.0 if was_visible else 0.0)
        except tk.TclError:
            was_visible = True

        self.popup.geometry(f"{w}x{h}+{x}+{y}")
        self.popup.deiconify()
        self.popup.attributes("-topmost", True)

        hwnd = root_hwnd(self.popup)
        if hwnd:
            make_noactivate(hwnd)
            if not set_pos(hwnd, x, y, w, h):
                log.warning("设置弹窗位置/置顶失败")
        self._start_progress(duration)
        if not was_visible:
            self._fade_in()

    # ---------------- 淡入 / 淡出 ----------------
    def _fade_in(self, steps=6, interval=16):
        self._cancel_fade()

        def step(i):
            self._fade_id = None
            try:
                if self.popup.state() == "withdrawn":
                    return
                self.popup.attributes("-alpha", min(1.0, i / steps))
            except tk.TclError:
                return
            if i < steps:
                self._fade_id = self.root.after(interval, step, i + 1)

        step(1)

    def _fade_out(self, steps=5, interval=16):
        self._cancel_fade()
        try:
            start = float(self.popup.attributes("-alpha"))
        except (tk.TclError, ValueError):
            start = 1.0

        def step(i):
            self._fade_id = None
            alpha = max(0.0, start * (1.0 - i / steps))
            try:
                self.popup.attributes("-alpha", alpha)
                if i >= steps:
                    self.popup.withdraw()
                    self.popup.attributes("-alpha", 1.0)
                    return
            except tk.TclError:
                return
            self._fade_id = self.root.after(interval, step, i + 1)

        step(1)

    def _cancel_fade(self):
        if self._fade_id is not None:
            try:
                self.root.after_cancel(self._fade_id)
            except tk.TclError:
                pass
            self._fade_id = None

    # ---------------- 底部进度线 ----------------
    def _start_progress(self, duration=None):
        self._cancel_progress()
        total = self.cfg.duration if duration is None else duration
        self._progress_total = max(0.2, float(total))
        self._progress_t0 = time.monotonic()
        self._progress_tick()

    def _progress_tick(self):
        self._progress_id = None
        if self.popup.state() == "withdrawn":
            return
        remain = 1.0 - (time.monotonic() - self._progress_t0) / self._progress_total
        remain = max(0.0, min(1.0, remain))
        width = self._progress.winfo_width()
        self._progress.delete("all")
        if width > 1:
            self._progress.create_rectangle(
                0, 0, max(1, int(width * remain)), 3,
                fill=ACCENT, outline=ACCENT)
        if remain > 0:
            self._progress_id = self.root.after(33, self._progress_tick)

    def _cancel_progress(self):
        if self._progress_id is not None:
            try:
                self.root.after_cancel(self._progress_id)
            except tk.TclError:
                pass
            self._progress_id = None

    def _hide_popup(self):
        self._cancel_progress()
        try:
            if self.popup.state() == "withdrawn":
                return
            self._fade_out()
        except tk.TclError:
            pass

    def _on_popup_click(self, _event=None):
        """单击：收起并进入下一轮"""
        if self.phase is Phase.SHOWING:
            self._skip()
        else:
            self._hide_popup()

    def _on_popup_menu(self, event):
        """右键：稍后 / 跳过 / 停止"""
        menu = tk.Menu(self.root, tearoff=0, bg=CARD_BG, fg=FG,
                       activebackground=ACCENT_SOFT, activeforeground=FG,
                       bd=0, relief="flat", font=F_UI)
        try:
            minutes = int(float(self.cfg.snooze_minutes))
        except (TypeError, ValueError):
            minutes = 5
        menu.add_command(label=f"稍后 {minutes} 分钟", command=self._snooze)
        menu.add_command(label="跳过本次", command=self._skip)
        menu.add_separator()
        menu.add_command(label="停止", command=self.stop)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass

    def _on_popup_stop(self, _event=None):
        self.stop()

    # ---------------- 输入校验 ----------------
    @staticmethod
    def _parse(entry):
        s = entry.get().strip()
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    # ---------------- 计划 / 定时 ----------------
    def _allowed(self, dt):
        if dt.weekday() not in self.cfg.weekdays:
            return False
        if self.cfg.active_enabled and not self._in_active(dt.time()):
            return False
        return True

    def _in_active(self, t):
        try:
            start = datetime.strptime(self.cfg.active_start, "%H:%M").time()
            end = datetime.strptime(self.cfg.active_end, "%H:%M").time()
        except ValueError:
            return True
        if start == end:
            return True
        if start < end:
            return start <= t < end
        return t >= start or t < end

    def _next_allowed(self, dt):
        candidate = dt.replace(second=0, microsecond=0)
        if candidate < dt:
            candidate += timedelta(minutes=1)
        for _ in range(8 * 24 * 60):
            if self._allowed(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        return dt + timedelta(minutes=5)

    def _next_interval(self, now):
        due = now + timedelta(seconds=self.cfg.interval)
        if not self._allowed(due):
            due = self._next_allowed(due)
        return due

    def _next_clock(self, now):
        times = []
        for text in self.cfg.clock_times:
            try:
                times.append(datetime.strptime(text, "%H:%M").time())
            except ValueError:
                continue
        times.sort()
        for offset in range(0, 9):
            day = (now + timedelta(days=offset)).date()
            if day.weekday() not in self.cfg.weekdays:
                continue
            for t in times:
                candidate = datetime.combine(day, t)
                if candidate <= now:
                    continue
                if self.cfg.active_enabled and not self._in_active(t):
                    continue
                return candidate
        return now + timedelta(minutes=5)

    def _compute_due(self):
        now = datetime.now()
        if self._snooze_until and self._snooze_until > now:
            return self._snooze_until
        if self._pause_until and self._pause_until > now:
            return self._pause_until
        if self.cfg.schedule_mode == "clock":
            return self._next_clock(now)
        return self._next_interval(now)

    def _arm(self, delay_ms, callback):
        self._cancel_timer()
        self._after_id = self.root.after(max(1, int(delay_ms)), callback)

    def _cancel_timer(self):
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _cancel_preview(self):
        if self._preview_id is not None:
            try:
                self.root.after_cancel(self._preview_id)
            except tk.TclError:
                pass
            self._preview_id = None

    def _schedule_next(self):
        if self.phase is not Phase.COUNTING:
            return
        self._cancel_timer()
        self._next_due = self._compute_due()
        delay = (self._next_due - datetime.now()).total_seconds()
        self._arm(max(1, delay * 1000), self._on_due)
        self._refresh_status()

    def _on_due(self):
        self._after_id = None
        if self.phase is not Phase.COUNTING:
            return
        now = datetime.now()
        if self._snooze_until and now >= self._snooze_until:
            self._snooze_until = None
            self._show_reminder()
            return
        if self._pause_until and now >= self._pause_until:
            self._pause_until = None
            self._apply_phase_ui()
            self._schedule_next()
            return
        if (self.cfg.fullscreen_defer and notification_busy()):
            self._arm(3000, self._on_due)
            self._refresh_status()
            return
        self._show_reminder()

    def _show_reminder(self):
        self._paint_popup()
        self.phase = Phase.SHOWING
        self._deadline = time.monotonic() + self.cfg.duration
        self._arm(self.cfg.duration * 1000, self._on_show_elapsed)
        self._refresh_status()

    def _on_show_elapsed(self):
        self._after_id = None
        if self.phase is not Phase.SHOWING:
            return
        self._hide_popup()
        self.phase = Phase.COUNTING
        self._schedule_next()

    def _snooze(self):
        try:
            minutes = int(float(self.cfg.snooze_minutes))
        except (TypeError, ValueError):
            minutes = 5
        self._snooze_until = datetime.now() + timedelta(minutes=minutes)
        self._hide_popup()
        self.phase = Phase.COUNTING
        self._schedule_next()

    def _skip(self):
        self._hide_popup()
        self.phase = Phase.COUNTING
        self._schedule_next()

    # ---------------- 心跳（状态提示刷新） ----------------
    def _heartbeat(self):
        self._heartbeat_id = None
        if self.phase is Phase.IDLE:
            return
        self._refresh_status()
        self._heartbeat_id = self.root.after(1000, self._heartbeat)

    def _schedule_text(self):
        wd = list(self.cfg.weekdays)
        if wd == list(range(7)):
            prefix = "每天"
        elif wd == [0, 1, 2, 3, 4]:
            prefix = "工作日"
        else:
            prefix = "周" + "".join(WEEK_LABELS[i] for i in wd)
        if self.cfg.schedule_mode == "clock":
            times = self.cfg.clock_times
            shown = "、".join(times[:3]) + ("…" if len(times) > 3 else "")
            text = f"{prefix} {shown}"
        else:
            text = f"每 {_fmt_num(self.cfg.interval)} 秒"
            if prefix != "每天":
                text += f" · {prefix}"
        if self.cfg.active_enabled:
            text += f" · {self.cfg.active_start}–{self.cfg.active_end}"
        return text

    def _refresh_status(self):
        if self.phase is Phase.IDLE:
            self.lbl_status.config(text=f"空闲 · {self._schedule_text()}",
                                   fg=SUBTLE)
            self.dot.config(fg=SUBTLE)
            return
        if self._pause_until:
            self.lbl_status.config(
                text=f"已暂停至 {self._pause_until:%H:%M}", fg=FG_SOFT)
            self.dot.config(fg="#D9A400")
            return
        if self._snooze_until:
            self.lbl_status.config(
                text=f"稍后提醒 · {self._snooze_until:%H:%M}", fg=FG_SOFT)
            self.dot.config(fg=ACCENT)
            return
        if self.phase is Phase.SHOWING:
            self.lbl_status.config(text="显示中", fg=FG_SOFT)
            self.dot.config(fg=ACCENT)
            return
        due = self._next_due
        if due is None:
            self.lbl_status.config(text="倒计时中", fg=FG_SOFT)
        elif self.cfg.schedule_mode == "clock":
            self.lbl_status.config(text=f"下次 {due:%H:%M}", fg=FG_SOFT)
        else:
            now = datetime.now()
            remain = max(0, int((due - now).total_seconds()))
            if remain >= 3600 or due.date() != now.date():
                self.lbl_status.config(text=f"倒计时中 · 下次 {due:%H:%M}",
                                       fg=FG_SOFT)
            else:
                self.lbl_status.config(text=f"倒计时中 · 约 {remain} 秒",
                                       fg=FG_SOFT)
        self.dot.config(fg=ACCENT)

    # ---------------- 暂停 / 开始 / 停止 ----------------
    def _toggle_pause(self):
        if self.phase is Phase.IDLE:
            return
        if self._pause_until is not None:
            self._pause_until = None
            self._schedule_next()
        else:
            self._pause_until = datetime.now() + timedelta(minutes=30)
            self._snooze_until = None
            self._cancel_timer()
            self._hide_popup()
            self.phase = Phase.COUNTING
        self._apply_phase_ui()

    def start(self):
        if self.phase is not Phase.IDLE:
            return
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.close()
        self.cfg = self._collect_cfg()
        self._save_cfg()
        self._pause_until = None
        self._snooze_until = None
        self.phase = Phase.COUNTING
        self._apply_phase_ui()
        self.root.iconify()
        self._schedule_next()
        if self._heartbeat_id is None:
            self._heartbeat_id = self.root.after(1000, self._heartbeat)

    def stop(self):
        self.phase = Phase.IDLE
        self._pause_until = None
        self._snooze_until = None
        self._next_due = None
        self._cancel_timer()
        self._cancel_preview()
        self._hide_popup()
        self._apply_phase_ui()
        self.root.deiconify()

    def _apply_phase_ui(self):
        running = self.phase is not Phase.IDLE
        self.btn_start.config(text="停止" if running else "开始",
                              command=self.stop if running else self.start,
                              kind="danger" if running else "primary")
        paused = self._pause_until is not None
        self.btn_pause.config(text="继续" if paused else "暂停",
                              state="normal" if running else "disabled")
        self.btn_preview.config(state="disabled" if running else "normal")
        self.btn_settings.config(state="disabled" if running else "normal")
        self._refresh_status()

    def _on_escape(self, _event=None):
        if self.phase is not Phase.IDLE:
            self.stop()

    # ---------------- 预览 ----------------
    def preview(self):
        if self.phase is not Phase.IDLE:
            return
        self.cfg = self._collect_cfg()
        self._cancel_preview()
        try:
            dur = Config.seconds(self.duration_var.get())
        except (TypeError, ValueError):
            dur = self.cfg.duration
        dur = max(Config.MIN_SECONDS, dur)
        self._paint_popup(dur)
        self._preview_id = self.root.after(int(dur * 1000), self._preview_hide)

    def _preview_hide(self):
        self._preview_id = None
        self._hide_popup()

    # ---------------- 退出 ----------------
    def _show_from_tray(self):
        if self._quitting:
            return
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def _hide_to_tray(self):
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.close()
        self.root.withdraw()

    def on_close(self):
        if (self.cfg.close_to_tray and self._tray is not None
                and self._tray.available):
            self._hide_to_tray()
            return
        self.quit_app()

    def quit_app(self):
        if self._quitting:
            return
        self._quitting = True
        self.phase = Phase.IDLE
        self._cancel_timer()
        self._cancel_preview()
        self._cancel_progress()
        self._cancel_fade()
        if not self._collect_cfg().save():
            log.warning("退出时保存配置失败")
        if self._tray is not None:
            self._tray.stop()
        try:
            self.popup.destroy()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()


def _install_error_handling(root):
    """pythonw 下 stderr 是 None，回调异常会导致进程直接退出；这里兜底记录。"""
    if sys.stderr is None:
        try:
            path = os.path.join(os.path.expanduser("~"),
                                ".countdown_reminder.err.log")
            sys.stderr = open(path, "a", encoding="utf-8", buffering=1)
        except Exception:
            pass

    def _report(exc, val, tb):
        log.error("未捕获的界面异常", exc_info=(exc, val, tb))

    root.report_callback_exception = _report


def main():
    CountdownReminder().run()


if __name__ == "__main__":
    main()


# ==========================================================
#  改动清单（相对原版）
# ==========================================================
# 1. 代码结构：Config dataclass 取代裸 dict；Phase 状态机独立成枚举；
#    定时器/UI/Win32 各归其位；补类型与 docstring。
# 2. 计时精度：time.monotonic() 计算截止时刻并据此调度，回调记录延迟；
#    用 wall-clock 检测休眠/唤醒，唤醒后立即补弹。
# 3. 状态管理：单一 _after_id 句柄；start/stop/preview 全部走状态机，
#    消除 preview 与运行循环的 after 竞争。
# 4. 用户交互：弹窗右键停止；输入非法即时标红；状态栏显示剩余秒数；
#    Esc 在主窗口也可停止。
# 5. 异常处理：logging 统一记录（日志文件不可写时自动回退，绝不崩溃）；
#    配置逐字段校验；work_area/set_pos 返回值检查；保存失败弹窗提示。
#
# 【可能影响行为的改动】
#  · 时长下限：倒计时/显示允许 0.1s 起（原版运行期实际下限约 1s / 0.3s）。
#  · 配置保存失败会弹窗提示（原版静默失败）。
#  · 弹窗新增右键停止、主窗口 Esc 停止（纯新增，不覆盖原有操作）。
#  · 主窗口新增状态栏（纯新增）。
#
# 【第二轮修复】
#  · inf/nan/超大输入不再通过校验（原先会 OverflowError 且卡在“倒计时中”）。
#  · 时长为 0.1s 以下被拒，避免 after(1) 高频刷屏。
#  · 删除弹窗上永不触发的 Esc 绑定（WS_EX_NOACTIVATE 窗口没有键盘焦点）。
#  · 配置文件用 utf-8-sig 读取，带 BOM 不再静默丢配置；损坏文件备份为 .bak。
#  · 配置保存改为“临时文件 + fsync + 原子替换”，并保留 .bak。
#  · _collect_cfg 复用 Config 的范围校验，不再吞掉 0 / 写入非法值。
#
# 【界面改版（Win10 扁平）】
#  · 配色换成 Win10 设置页体系：浅灰底 #F3F3F3、白卡片、主题蓝 #0078D7。
#  · FlatButton：自绘 1px 描边按钮，替代带立体感的 tk.Button；
#    开始/停止在主题蓝与浅红之间切换，预览有明确禁用态。
#  · FlatSlider：Canvas 自绘细轨道 + 圆形滑块，替代 tk.Scale。
#  · 主界面改为“标题 + 白色卡片 + 状态行 + 底部按钮”的 Win10 分组布局。
#  · 主窗口尺寸由内容自适应，不再写死 440×460，避免高 DPI 下按钮被裁。
#
# 【第三轮（精简 + 更多选项 + 弹窗重做）】
#  · 主界面只保留“提醒内容 / 间隔 / 显示”三项，其余全部收进“更多选项”。
#  · 更多选项：字体、字号（自动或 12–48）、文字颜色、弹窗底色（预设 + 取色器）、
#    弹出位置、窗口大小、退出程序；展开/收起时窗口高度自动调整。
#  · 新增配置字段 font_family / font_size / text_color / popup_bg，
#    旧配置文件缺字段时自动用默认值，不影响升级。
#  · 弹窗去掉顶部蓝色横条，改成纯白（或自定义底色）卡片 + 居中文字 +
#    底部 3px 进度线，字号自动封顶 24px，不再撑满卡片像海报。
#  · 弹窗进度线随显示时长实时收缩，预览时按预览时长走。
#  · 弹窗字号自适应改为“固定字号优先，放不下才等比缩小”，并修正
#    换行后 Label 请求宽度比 wraplength 多 2px 导致的误判（长文本被缩得过小）。
#
# 【第四轮（修下拉 + 去边框 + 淡入淡出）】
#  · 修复字体/字号/位置下拉点不开：给 Listbox 设置的字体写成了
#    "Microsoft YaHei UI 10"，Tcl 会拆成 4 个词，弹列表时报
#    “expected integer but got YaHei”；改为 "{Microsoft YaHei UI} 10"。
#  · 下拉列表改为无滚动条（显式 height），并去掉容器立体边。
#  · 整个窗口改成白底、无卡片描边、无输入框描边的极简样式；
#    输入区用 #F4F4F5 填充区分，聚焦时才出现 1px 主题色。
#  · 弹窗去掉 1px 描边，纯白（或自定义底色）无边框卡片；
#    通过 CS_DROPSHADOW 给无边框弹窗补一层系统投影，避免糊在桌面上。
#  · 弹窗增加 100ms 左右淡入、80ms 淡出（wm attributes -alpha），
#    不干扰视线；淡出结束后自动复位 alpha，下次弹出正常。
#  · 弹窗尺寸改为“百分比 = 最大尺寸”，卡片按文字占用量收缩。
#
# 【第五轮（自绘下拉 + 展开动画）】
#  · 三个下拉从 ttk.Combobox 换成自绘 FlatSelect：无边框、无滚动条、
#    浅色列表、悬停高亮，支持方向键 / 回车 / Esc。
#  · 展开时约 70ms 淡入 + 6px 下滑，收起约 56ms 淡出；
#    展开时箭头变蓝并朝上，收起恢复灰色朝下。
#  · 点击列表项、点击窗口其他位置、点击另一个下拉都会正确收起；
#    窗口关闭时自动清理下拉弹层。
#  · 修复闪退：FlatSelect 曾把 tkinter 内部方法 _root 覆盖成 Tk 实例，
#    事件分发时抛 TypeError；pythonw 下 stderr 为空，异常会让进程直接退出。
#    属性改名为 _top，并增加 report_callback_exception 把异常写入日志。
# 【第六轮（词库 + 柔和提示音 + 下拉留白）】
#  · 下拉弹层宽度按列表内容计算（最小 72px），不再固定 140px
#    导致“自动/12/14”右边一大片空白；行距同时收紧。
#  · 新增词库：内容框每行一条，可只有一条，也可多条；
#    顶部可在“顺序 / 随机”之间切换，随机为等权随机。
#  · 新增柔和提示音：G5 -> C6 两音正弦 chime，带淡入淡出，
#    异步播放不阻塞界面；更多选项里可开关，开启时立即试听。
#  · 配置新增 phrases / phrase_mode / sound，
#    旧版 text 字段在读取时自动迁移成单条词库。
# 【第七轮（词库提示 + 提示音修复）】
#  · 词库下方加一行小字：“顺序：按行轮换 · 随机：等概率抽取”，
#    标题旁保留“每行一条”，不增加多余控件。
#  · 修复听不到提示音：winsound 不允许 SND_MEMORY + SND_ASYNC，
#    改为把 chime 缓存成临时 WAV 文件再异步播放；失败时退回系统提示音。
#  · 提示音音量从 0.16 提到 0.22，仍然保持轻。
# 【第八轮（多仓词库）】
#  · 配置从单 phrases 升级为 repos（name/phrases/mode）+ active_repo；
#    旧 text / phrases / phrase_mode 自动迁移成一个「默认」仓。
#  · 主界面“提醒内容”旁加仓下拉，切换即换词；每个仓独立保存
#    自己的顺序/随机方式与顺序进度。
#  · 更多选项 → 词库 → 管理：新建（空白仓）、重命名、删除、
#    导入 / 导出 .txt（一行一条，兼容 UTF-8 与 GBK）。
#  · 最多 20 个仓，每仓最多 200 条；删除当前仓会自动切到第一个。
# 【第九轮（建仓入口 + 精简）】
#  · 主界面新增“＋ 新建”按钮，直接建空白仓并切换过去；
#    不再需要先打开更多选项找管理入口。
#  · 仓下拉移到输入框下方工具行；标题行只保留“提醒内容 + 顺序/随机”，
#    长提示收成右侧一行“每行一条”。
#  · 更多选项移除“退出程序”一行并收紧间距，降低拥挤感。
# 【第十轮（设置窗 + 计划 / 暂停 / 音色 / 热键）】
#  · 主界面只留内容、仓、开始/暂停/预览/设置；原“更多选项”收进三页设置窗。
#  · 计划：间隔/定时、星期、活跃时段、全屏延后；状态行显示下一次。
#  · 弹窗右键：稍后 N 分钟 / 跳过本次 / 停止；主界面可暂停 30 分钟。
#  · 提示音三档音色；随机模式一轮内不重复。
#  · 开机自启（HKCU Run）；Ctrl+Alt+R 开始/停止、Ctrl+Alt+P 暂停。
# 【第十一轮（托盘 + 深色模式 + 单文件 EXE）】
#  · 托盘：Shell_NotifyIcon 隐藏窗口 + 菜单（显示/开始停止/暂停/退出）；
#    关闭窗口默认收到托盘，可在设置→系统关闭该行为。
#  · 深色模式：设置→外观→主题；主窗/设置/管理窗/标题栏一起切换，
#    切换后自动回到外观页；弹窗颜色仍可单独配置。
#  · PyInstaller 单文件：CountdownReminder.exe，11.7 MB，带图标/版本信息。
