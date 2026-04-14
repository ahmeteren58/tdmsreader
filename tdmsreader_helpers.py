import sys
import math
import os
import logging
import threading

# Ensure pyqtgraph uses PyQt6 if multiple Qt bindings are installed
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
import gc
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Generator, List, Optional, Tuple

import numpy as np

# ---------------------------
# Logging
# ---------------------------
_LOG_LEVEL = os.environ.get("TDMSREADER_LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tdmsreader")

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QSize, QTimer, QSettings
from PyQt6.QtGui import QColor, QCloseEvent, QIcon, QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QAbstractItemView, QFileDialog,
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit, QTreeWidget,
    QTreeWidgetItem, QSplitter, QMessageBox, QStatusBar, QCheckBox, QGroupBox,
    QFormLayout, QDoubleSpinBox, QTabWidget, QComboBox, QButtonGroup, QSpinBox,
    QColorDialog, QGridLayout, QSizePolicy, QToolButton, QFrame,
    QProgressBar, QMenu, QDialog, QTextBrowser,
)

import pyqtgraph as pg
from pyqtgraph import PlotWidget
from pyqtgraph.graphicsItems.DateAxisItem import DateAxisItem

try:
    import OpenGL  # noqa: F401
    pg.setConfigOption("useOpenGL", True)
    pg.setConfigOption("enableExperimental", True)
except ImportError:
    pass

pg.setConfigOptions(antialias=False)

from nptdms import TdmsFile

try:
    from scipy.signal import savgol_filter
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    savgol_filter = None  # type: ignore[assignment]


# ===================================================================
# Configuration
# ===================================================================

LAZY_ENABLE = True
LAZY_FULLLOAD_THRESHOLD = 2_000_000
LAZY_OVERVIEW_MAX_POINTS = 200_000
LAZY_VIEW_MAX_POINTS = 250_000
LAZY_VIEW_DEBOUNCE_MS = 180
MAX_RECENT_FILES = 10


# ===================================================================
# Helpers – TDMS context manager
# ===================================================================

@contextmanager
def _open_tdms(path: str) -> Generator[TdmsFile, None, None]:
    tdms = TdmsFile.open(path)
    try:
        yield tdms
    finally:
        try:
            tdms.close()
        except Exception:
            logger.debug("TDMS kapatılamadı: %s", path)


# ===================================================================
# Helpers – time / numeric
# ===================================================================

def _to_epoch_seconds(t: Any) -> float:
    if t is None:
        raise TypeError("Zaman değeri None olamaz")
    if isinstance(t, np.datetime64):
        return float(t.astype("datetime64[ns]").astype(np.int64)) / 1e9
    if isinstance(t, datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return float(t.timestamp())
    if isinstance(t, (int, float, np.integer, np.floating)):
        return float(t)
    raise TypeError(f"Desteklenmeyen zaman tipi: {type(t)}")


def _timedelta_to_seconds(dt: Any) -> float:
    if isinstance(dt, timedelta):
        return float(dt.total_seconds())
    if isinstance(dt, np.timedelta64):
        return float(dt.astype("timedelta64[ns]").astype(np.int64)) / 1e9
    if isinstance(dt, (int, float, np.integer, np.floating)):
        return float(dt)
    raise TypeError(f"Desteklenmeyen artış tipi: {type(dt)}")


def _ensure_1d_numeric(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr)
    if arr.ndim != 1:
        arr = np.ravel(arr)
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr)
    if not np.issubdtype(arr.dtype, np.number):
        arr = arr.astype(np.float64)
    return arr


def _xmeta_from_channel_props(props: dict) -> Tuple[str, float, float]:
    try:
        if props and "wf_increment" in props:
            inc = _timedelta_to_seconds(props["wf_increment"])
            start_offset = float(props.get("wf_start_offset", 0.0) or 0.0)
            if "wf_start_time" in props:
                start_epoch = _to_epoch_seconds(props["wf_start_time"])
                if abs(start_epoch) < 86400 * 2:
                    return "seconds", start_offset, float(inc)
                return "datetime", float(start_epoch + start_offset), float(inc)
            return "seconds", start_offset, float(inc)
    except Exception:
        logger.debug("X meta verisi okunamadı", exc_info=True)
    return "index", 0.0, 1.0


def _slice_indices_from_xrange(
    x0: float, x1: float, n: int,
    x_mode: str, x_base: float, x_inc: float,
) -> Tuple[int, int]:
    if n <= 0:
        return 0, 0
    a, b = (min(x0, x1), max(x0, x1))
    if x_mode == "index":
        i0 = int(math.floor(a))
        i1 = int(math.ceil(b))
    elif math.isfinite(x_inc) and x_inc > 0:
        i0 = int(math.floor((a - x_base) / x_inc))
        i1 = int(math.ceil((b - x_base) / x_inc))
    else:
        i0, i1 = 0, n
    win = max(1, i1 - i0)
    pad = max(1000, int(win * 0.05))
    i0 = max(0, i0 - pad)
    i1 = min(n, i1 + pad)
    if i1 <= i0:
        i1 = min(n, i0 + 1)
    return i0, i1


def _stride_for_window(i0: int, i1: int, max_points: int) -> int:
    win = max(1, i1 - i0)
    if max_points <= 0:
        return 1
    return max(1, math.ceil(win / max_points))


def classify_and_extract_x(channel: Any, n_samples: int) -> Tuple[np.ndarray, str]:
    try:
        tt = channel.time_track()
        if tt is not None and len(tt) == n_samples:
            if isinstance(tt, np.ndarray) and np.issubdtype(tt.dtype, np.datetime64):
                x = tt.astype("datetime64[ns]").astype(np.int64).astype(np.float64) / 1e9
                return x, "datetime"
            if isinstance(tt, np.ndarray) and np.issubdtype(tt.dtype, np.number):
                return np.asarray(tt, dtype=np.float64), "seconds"
            if isinstance(tt, (list, tuple)) and tt and isinstance(tt[0], datetime):
                x = np.array([_to_epoch_seconds(v) for v in tt], dtype=np.float64)
                return x, "datetime"
    except Exception:
        pass
    props = getattr(channel, "properties", {}) or {}
    if "wf_increment" in props:
        try:
            inc = _timedelta_to_seconds(props["wf_increment"])
            start_offset = float(props.get("wf_start_offset", 0.0))
            if "wf_start_time" in props:
                start_epoch = _to_epoch_seconds(props["wf_start_time"])
                if abs(start_epoch) < 86400 * 2:
                    return start_offset + np.arange(n_samples, dtype=np.float64) * inc, "seconds"
                return (start_epoch + start_offset + np.arange(n_samples, dtype=np.float64) * inc), "datetime"
            return start_offset + np.arange(n_samples, dtype=np.float64) * inc, "seconds"
        except Exception:
            pass
    return np.arange(n_samples, dtype=np.float64), "index"


def robust_fs_from_x(x: np.ndarray) -> Optional[float]:
    if x is None or len(x) < 3:
        return None
    limit = 10_000
    subset = x[:limit] if x.size > limit else x
    dx = np.diff(subset.astype(np.float64))
    dx = dx[np.isfinite(dx)]
    if dx.size == 0:
        return None
    med = float(np.median(dx))
    return (1.0 / med) if med > 0 else None


def fs_value_to_hz(value: float, unit: str) -> Optional[float]:
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0.0:
        return None
    if str(unit or "Hz").strip().lower() == "khz":
        val *= 1e3
    return val


def apply_savgol_safe(y: np.ndarray, window_len: int, polyorder_hint: int = 3) -> np.ndarray:
    if savgol_filter is None:
        return y
    y = np.asarray(y, dtype=np.float64)
    n = y.size
    if n < 5:
        return y
    y_clean = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
    win = int(window_len)
    if win % 2 == 0:
        win += 1
    if win > n:
        win = n if (n % 2 == 1) else (n - 1)
    if win < 5:
        return y_clean
    poly = min(int(polyorder_hint), win - 2)
    if poly < 1:
        return y_clean
    return savgol_filter(y_clean, window_length=win, polyorder=poly, mode="interp")


def _safe_str(v: Any) -> str:
    s = str(v).strip()
    return "" if (not s or s.lower() in ("none", "nan")) else s


def infer_quantity_from_props(props: dict) -> str:
    if not props:
        return "Değer"
    preferred = [
        "quantity", "Quantity", "physical_quantity", "PhysicalQuantity",
        "measurement", "Measurement", "NI_MeasurementName", "NI_SignalName",
        "NI_UnitDescription", "unit_description", "UnitDescription",
        "NI_Description", "description", "Description", "NI_ChannelDescription",
    ]
    for k in preferred:
        if k in props:
            s = _safe_str(props[k])
            if s:
                return s[:80]
    tokens = ("quantity", "measure", "signal", "physical", "sensor", "unitdesc", "descr")
    for k, v in props.items():
        if any(tok in str(k).lower() for tok in tokens):
            s = _safe_str(v)
            if s:
                return s[:80]
    return "Değer"


def common_unit(series_list: List[dict]) -> Optional[str]:
    units = [s.get("unit", "").strip() for s in series_list if s.get("unit", "").strip()]
    if not units:
        return None
    return units[0] if all(u == units[0] for u in units) else None


def common_quantity(series_list: List[dict]) -> Optional[str]:
    qs = [s.get("quantity", "").strip() for s in series_list if s.get("quantity", "").strip()]
    if not qs:
        return None
    return qs[0] if all(q == qs[0] for q in qs) else None


def qcolor_to_tuple(c: QColor) -> Tuple[int, int, int]:
    return (c.red(), c.green(), c.blue())


def file_label_from_path(path: str) -> str:
    return os.path.basename(path)


def style_key_for_channel(file_id: str, group: str, channel: str) -> str:
    return f"{file_id}|{group}|{channel}"


def _bg_to_fg_rgb(bg: Tuple[int, int, int]) -> Tuple[int, int, int]:
    lum = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]
    return (0, 0, 0) if lum > 140 else (255, 255, 255)


def apply_plotwidget_theme(pw: PlotWidget, bg_rgb: Tuple[int, int, int]) -> None:
    fg = _bg_to_fg_rgb(bg_rgb)
    pw.setBackground(bg_rgb)
    item = pw.getPlotItem()
    for ax_name in ("left", "bottom", "right"):
        axis = item.getAxis(ax_name)
        if axis is not None:
            axis.setPen(pg.mkPen(fg))
            axis.setTextPen(pg.mkPen(fg))


def linear_detrend_safe(y: np.ndarray) -> np.ndarray:
    yy = np.asarray(y, dtype=np.float64)
    if yy.size < 2:
        return np.nan_to_num(yy, nan=0.0, posinf=0.0, neginf=0.0)
    out = yy.copy()
    finite = np.isfinite(out)
    if np.count_nonzero(finite) < 2:
        return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)
    idx = np.arange(out.size, dtype=np.float64)
    try:
        m, b = np.polyfit(idx[finite], out[finite], 1)
        out[finite] -= (m * idx[finite] + b)
    except Exception:
        out[finite] -= float(np.nanmean(out[finite]))
    out[~finite] = 0.0
    return out


def padded_minmax(values: np.ndarray, pad_ratio: float = 0.06) -> Optional[Tuple[float, float]]:
    arr = np.asarray(values, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None
    lo, hi = float(arr.min()), float(arr.max())
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return None
    if hi == lo:
        pad = max(1.0, abs(lo) * 0.1, 1e-12)
        return lo - pad, hi + pad
    pad = max((hi - lo) * pad_ratio, 1e-12)
    return lo - pad, hi + pad


def _axis_mode_and_label(series_list: List[dict]) -> Tuple[str, str]:
    if not series_list:
        return "numeric", "X"
    if all(s.get("x_mode") == "datetime" for s in series_list):
        return "date", "Zaman (UTC)"
    if any(s.get("x_mode") == "seconds" for s in series_list):
        return "numeric", "Zaman (s)"
    return "numeric", "İndeks"


def _format_x_display(axis_mode: str, x: float) -> str:
    if axis_mode == "date":
        try:
            return datetime.fromtimestamp(float(x), tz=timezone.utc).isoformat()
        except Exception:
            pass
    return f"{x:.12g}"


def detect_digital_like(
    y: np.ndarray, max_levels: int = 8, sample_limit: int = 50_000,
) -> Tuple[bool, Optional[Tuple[float, float]]]:
    if y is None:
        return False, None
    y = np.asarray(y)
    if y.size == 0:
        return False, None
    stride = max(1, y.size // sample_limit)
    yy = y[::stride]
    if np.issubdtype(yy.dtype, np.floating):
        yy = yy[np.isfinite(yy)]
        if yy.size == 0:
            return False, None
    uniq = np.unique(yy)
    if uniq.size <= 1 or uniq.size > max_levels:
        return False, None
    uf = uniq.astype(np.float64, copy=False)
    lo, hi = float(uf.min()), float(uf.max())
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi == lo:
        return False, None
    if np.all(np.abs(uf - np.round(uf)) < 1e-6) and (hi - lo) <= 10.0:
        return True, (lo, hi)
    if uniq.size <= 3:
        zz = (uf - lo) / (hi - lo)
        if np.all(np.abs(zz - np.round(zz)) < 1e-6):
            return True, (lo, hi)
    return False, None


# ===================================================================
# Theme QSS
# ===================================================================

LIGHT_QSS = """
QMainWindow { background: #F6F7F9; }
QLabel { color: #222; }
QLabel:disabled { color: #9AA3B2; }
QTabWidget::pane { border: 1px solid #D7DBE0; border-radius: 12px; background: #FFFFFF; }
QTabBar::tab { padding: 8px 12px; margin: 2px; border-radius: 9px; background: #EEF1F4; color: #222; }
QTabBar::tab:selected { background: #FFFFFF; border: 1px solid #D7DBE0; }
QGroupBox { border: 1px solid #D7DBE0; border-radius: 12px; margin-top: 14px; background: #FFFFFF; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #222; font-weight: 600; }
QPushButton, QToolButton { border: 1px solid #C9CED6; border-radius: 9px; padding: 7px 12px; background: #FFFFFF; color: #000; }
QPushButton:hover, QToolButton:hover { background: #F1F3F6; }
QPushButton:pressed, QToolButton:pressed { background: #E9ECF0; }
QPushButton[primary="true"] { background: #2ECC71; border-color: #26B863; color: white; font-weight: 700; }
QPushButton[primary="true"]:hover { background: #29C46B; }
QPushButton[danger="true"] { background: #E74C3C; border-color: #D94435; color: white; font-weight: 700; }
QPushButton[danger="true"]:hover { background: #DE4637; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { border: 1px solid #C9CED6; border-radius: 9px; padding: 5px 8px; background: #FFFFFF; selection-background-color: #e3ddff; }
QTreeWidget { border: 1px solid #D7DBE0; border-radius: 12px; background: #FFFFFF; }
QHeaderView::section { background: #F1F3F6; padding: 6px; border: none; border-right: 1px solid #D7DBE0; color: #222; font-weight: 600; }
QCheckBox, QRadioButton { spacing: 8px; }
QStatusBar { background: #FFFFFF; border-top: 1px solid #D7DBE0; }
QMenuBar { background: #F6F7F9; border-bottom: 1px solid #D7DBE0; padding: 2px; }
QMenuBar::item { padding: 6px 12px; border-radius: 6px; }
QMenuBar::item:selected { background: #E9ECF0; }
QMenu { background: #FFFFFF; border: 1px solid #D7DBE0; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 28px 6px 12px; border-radius: 4px; }
QMenu::item:selected { background: #E7F0FF; }
QMenu::separator { height: 1px; background: #D7DBE0; margin: 4px 8px; }
QFrame#plotQuickbar { background: rgba(255,255,255,0.70); border: 1px solid #D7DBE0; border-radius: 14px; }
QToolButton[qb="1"] { border: 1px solid #C9CED6; border-radius: 12px; padding: 0px; background: #FFFFFF; }
QToolButton[qb="1"]:hover { background: #F1F3F6; }
QToolButton[qb="1"]:pressed { background: #E9ECF0; }
QToolButton[qb="1"]:checked { background: #E7F0FF; border: 1px solid #4C8DFF; }
QToolButton[qb="1"]:checked:hover { background: #DCEAFF; }
QToolButton[qb="1"]:disabled { background: #F5F6F8; color: #9AA3B2; border-color: #D7DBE0; }
QFrame#controlsDock { border: 1px solid #D7DBE0; border-radius: 12px; background: #FFFFFF; }
QFrame#controlsHeader { background: #EEF1F4; border-bottom: 1px solid #D7DBE0; border-top-left-radius: 12px; border-top-right-radius: 12px; }
QToolButton#btnCtrlCollapse { border: 1px solid #C9CED6; border-radius: 9px; padding: 6px 10px; background: #FFFFFF; font-weight: 800; text-align: left; }
QToolButton#btnCtrlCollapse:hover { background: #F1F3F6; }
QToolButton#btnCtrlCollapse:pressed { background: #E9ECF0; }
QProgressBar { border: 1px solid #C9CED6; border-radius: 6px; background: #F1F3F6; text-align: center; }
QProgressBar::chunk { background: #2ECC71; border-radius: 5px; }
"""

DARK_QSS = """
QMainWindow { background: #0F1115; }
QLabel { color: #E8EAF0; }
QLabel:disabled { color: #7F889B; }
QTabWidget::pane { border: 1px solid #2A2F3A; border-radius: 12px; background: #141824; }
QTabBar::tab { padding: 8px 12px; margin: 2px; border-radius: 9px; background: #1D2230; color: #E8EAF0; }
QTabBar::tab:selected { background: #141824; border: 1px solid #2A2F3A; }
QGroupBox { border: 1px solid #2A2F3A; border-radius: 12px; margin-top: 14px; background: #141824; color: #E8EAF0; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #E8EAF0; font-weight: 600; }
QPushButton, QToolButton { border: 1px solid #3A4253; border-radius: 9px; padding: 7px 12px; background: #1A2030; color: #E8EAF0; }
QPushButton:hover, QToolButton:hover { background: #222A3D; }
QPushButton:pressed, QToolButton:pressed { background: #2A3550; }
QPushButton[primary="true"] { background: #2ECC71; border-color: #26B863; color: white; font-weight: 700; }
QPushButton[primary="true"]:hover { background: #29C46B; }
QPushButton[danger="true"] { background: #E74C3C; border-color: #D94435; color: white; font-weight: 700; }
QPushButton[danger="true"]:hover { background: #DE4637; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { border: 1px solid #3A4253; border-radius: 9px; padding: 5px 8px; background: #101624; color: #E8EAF0; selection-background-color: #2C3A5C; }
QTreeWidget { border: 1px solid #2A2F3A; border-radius: 12px; background: #101624; color: #E8EAF0; }
QHeaderView::section { background: #1A2030; padding: 6px; border: none; border-right: 1px solid #2A2F3A; color: #E8EAF0; font-weight: 600; }
QCheckBox, QRadioButton { spacing: 8px; color: #E8EAF0; }
QStatusBar { background: #141824; border-top: 1px solid #2A2F3A; color: #E8EAF0; }
QMenuBar { background: #0F1115; border-bottom: 1px solid #2A2F3A; padding: 2px; color: #E8EAF0; }
QMenuBar::item { padding: 6px 12px; border-radius: 6px; color: #E8EAF0; }
QMenuBar::item:selected { background: #222A3D; }
QMenu { background: #141824; border: 1px solid #2A2F3A; border-radius: 8px; padding: 4px; color: #E8EAF0; }
QMenu::item { padding: 6px 28px 6px 12px; border-radius: 4px; color: #E8EAF0; }
QMenu::item:selected { background: #2A3550; }
QMenu::separator { height: 1px; background: #2A2F3A; margin: 4px 8px; }
QFrame#plotQuickbar { background: rgba(20,24,36,0.80); border: 1px solid #2A2F3A; border-radius: 14px; }
QToolButton[qb="1"] { border: 1px solid #3A4253; border-radius: 12px; padding: 0px; background: #1A2030; color: #E8EAF0; }
QToolButton[qb="1"]:hover { background: #222A3D; }
QToolButton[qb="1"]:pressed { background: #2A3550; }
QToolButton[qb="1"]:checked { background: #2A3550; border: 1px solid #6C8DFF; }
QToolButton[qb="1"]:checked:hover { background: #324068; }
QToolButton[qb="1"]:disabled { background: #161B28; color: #7F889B; border-color: #2A2F3A; }
QFrame#controlsDock { border: 1px solid #2A2F3A; border-radius: 12px; background: #141824; }
QFrame#controlsHeader { background: #1D2230; border-bottom: 1px solid #2A2F3A; border-top-left-radius: 12px; border-top-right-radius: 12px; }
QToolButton#btnCtrlCollapse { border: 1px solid #2A2F3A; border-radius: 9px; padding: 6px 10px; background: #141824; color: #E8EAF0; font-weight: 800; text-align: left; }
QToolButton#btnCtrlCollapse:hover { background: #20273A; }
QToolButton#btnCtrlCollapse:pressed { background: #1B2131; }
QProgressBar { border: 1px solid #3A4253; border-radius: 6px; background: #1A2030; color: #E8EAF0; text-align: center; }
QProgressBar::chunk { background: #2ECC71; border-radius: 5px; }
"""


# ===================================================================
# Data models
# ===================================================================

@dataclass(frozen=True)
class ChannelKey:
    file_id: str
    group: str
    channel: str
    length: int


@dataclass
class ChannelRequest:
    key: ChannelKey
    display_name: str


@dataclass
class FileState:
    file_id: str
    path: str
    label: str
    tree: QTreeWidget
    search: QLineEdit


# ===================================================================
# Cancellable worker base
# ===================================================================

class CancellableWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()


# ===================================================================
# Workers (all use _open_tdms context manager for safe resource handling)
# ===================================================================

class TdmsIndexWorker(CancellableWorker):
    def __init__(self, file_id: str, path: str) -> None:
        super().__init__()
        self.file_id = file_id
        self.path = path

    def run(self) -> None:
        if self.is_cancelled:
            self.finished.emit({"file_id": self.file_id, "refs": [], "cancelled": True})
            return
        try:
            with _open_tdms(self.path) as tdms:
                refs: List[Tuple[str, str, int]] = []
                for group in tdms.groups():
                    if self.is_cancelled:
                        break
                    gname = group.name
                    for ch in group.channels():
                        try:
                            n = len(ch)
                        except Exception:
                            n = 0
                        refs.append((gname, ch.name, int(n)))
            if self.is_cancelled:
                self.finished.emit({"file_id": self.file_id, "refs": [], "cancelled": True})
                return
            self.finished.emit({"file_id": self.file_id, "refs": refs})
        except Exception as e:
            self.failed.emit(f"TDMS indeksi okunamadı:\n{e}")


class TdmsChannelPreviewWorker(CancellableWorker):
    def __init__(self, path: str, key: ChannelKey) -> None:
        super().__init__()
        self.path = path
        self.key = key

    def run(self) -> None:
        if self.is_cancelled:
            self.finished.emit({"file_id": self.key.file_id, "cancelled": True})
            return
        try:
            with _open_tdms(self.path) as tdms:
                ch = tdms[self.key.group][self.key.channel]
                props = getattr(ch, "properties", {}) or {}
                unit = props.get("unit_string", props.get("unit", ""))
                quantity = infer_quantity_from_props(props)
                try:
                    n = int(len(ch))
                except Exception:
                    n = 0
                x_mode = "index"
                fs: Optional[float] = None
                try:
                    x_mode, _base, x_inc = _xmeta_from_channel_props(props)
                    if x_mode in ("seconds", "datetime") and math.isfinite(x_inc) and x_inc > 0:
                        fs = 1.0 / x_inc
                except Exception:
                    x_mode = "index"
                self.finished.emit({
                    "file_id": self.key.file_id,
                    "name": f"{self.key.group}/{self.key.channel}",
                    "samples": n, "unit": unit, "quantity": quantity,
                    "x_info": {"x_mode": x_mode, "fs_est": fs},
                })
        except Exception as e:
            self.failed.emit(f"Önizleme hatası:\n{e}")


class MultiTdmsChannelLoadWorker(CancellableWorker):
    def __init__(self, files: Dict[str, Dict[str, str]], requests: List[ChannelRequest]) -> None:
        super().__init__()
        self.files = files
        self.requests = requests

    def _load_full(self, ch: Any, props: dict, skey: str, display: str, label: str, n: int) -> Optional[dict]:
        y = _ensure_1d_numeric(ch[:])
        if y.size == 0:
            return None
        x, x_mode = classify_and_extract_x(ch, len(y))
        x = np.asarray(x, dtype=np.float64)
        if x.size > 1 and x[-1] < x[0]:
            order = np.argsort(x)
            x, y = x[order], y[order]
        elif x.size > 1:
            stride0 = max(1, x.size // 5000)
            dx = np.diff(x[::stride0])
            if dx.size and np.any(dx < 0):
                order = np.argsort(x)
                x, y = x[order], y[order]
        if x.size > 1:
            xu, idx = np.unique(x, return_index=True)
            if xu.size != x.size:
                x, y = xu, y[idx]
        dig, dig_levels = detect_digital_like(y)
        fs_est = robust_fs_from_x(x) if x_mode in ("seconds", "datetime") else None
        unit = props.get("unit_string", props.get("unit", ""))
        quantity = infer_quantity_from_props(props)
        return {
            "style_key": skey, "name": display, "file_label": label,
            "x": x, "y": y, "unit": unit, "quantity": quantity,
            "x_mode": x_mode, "source_x_mode": x_mode,
            "source_x_base": 0.0, "source_x_inc": 1.0,
            "source_fs_est": fs_est,
            "is_digital": dig, "digital_levels": dig_levels, "lazy": False,
        }

    def _load_lazy(self, ch: Any, props: dict, skey: str, display: str,
                   label: str, n: int, path: str, k: ChannelKey) -> Optional[dict]:
        x_mode, x_base, x_inc = _xmeta_from_channel_props(props)
        stride = _stride_for_window(0, n, LAZY_OVERVIEW_MAX_POINTS)
        try:
            y = ch[0:n:stride]
        except Exception:
            indices = list(range(0, n, stride))
            y = np.asarray([ch[i] for i in indices])
        y = _ensure_1d_numeric(y)
        if y.size == 0:
            return None
        idx = np.arange(0, n, stride, dtype=np.float64)
        x = idx if x_mode == "index" else (x_base + idx * x_inc).astype(np.float64)
        dig, dig_levels = detect_digital_like(y)
        fs_est = (1.0 / x_inc) if x_mode in ("seconds", "datetime") and x_inc > 0 else None
        unit = props.get("unit_string", props.get("unit", ""))
        quantity = infer_quantity_from_props(props)
        return {
            "style_key": skey, "name": display, "file_label": label,
            "x": x, "y": y, "unit": unit, "quantity": quantity,
            "x_mode": x_mode, "source_x_mode": x_mode,
            "source_x_base": float(x_base), "source_x_inc": float(x_inc),
            "source_fs_est": fs_est,
            "is_digital": dig, "digital_levels": dig_levels, "lazy": True,
            "lazy_meta": {
                "path": path, "group": k.group, "channel": k.channel,
                "n": n, "x_mode": x_mode, "x_base": float(x_base), "x_inc": float(x_inc),
            },
            "_lazy_last_win": (0, n, stride),
        }

    def run(self) -> None:
        if self.is_cancelled:
            self.finished.emit({"series": [], "axis_mode": "numeric", "x_label": "X", "cancelled": True})
            return
        try:
            req_by_file: Dict[str, List[ChannelRequest]] = {}
            for r in self.requests:
                req_by_file.setdefault(r.key.file_id, []).append(r)
            series: List[dict] = []
            x_modes: List[str] = []
            for file_id, reqs in req_by_file.items():
                if self.is_cancelled:
                    break
                if file_id not in self.files:
                    continue
                path = self.files[file_id]["path"]
                label = self.files[file_id]["label"]
                try:
                    with _open_tdms(path) as tdms:
                        for req in reqs:
                            if self.is_cancelled:
                                break
                            k = req.key
                            try:
                                ch = tdms[k.group][k.channel]
                            except KeyError:
                                continue
                            props = getattr(ch, "properties", {}) or {}
                            try:
                                n = int(len(ch))
                            except Exception:
                                n = 0
                            if n <= 0:
                                continue
                            skey = style_key_for_channel(file_id, k.group, k.channel)
                            display = req.display_name.strip() or k.channel
                            use_lazy = LAZY_ENABLE and n > LAZY_FULLLOAD_THRESHOLD
                            if use_lazy:
                                s = self._load_lazy(ch, props, skey, display, label, n, path, k)
                            else:
                                s = self._load_full(ch, props, skey, display, label, n)
                            if s is not None:
                                x_modes.append(s["x_mode"])
                                series.append(s)
                except Exception as e:
                    logger.warning("Yükleme hatası (%s): %s", label, e)
            if not series or self.is_cancelled:
                self.finished.emit({"series": [], "axis_mode": "numeric", "x_label": "X", "cancelled": self.is_cancelled})
                return
            axis_mode = "date" if all(m == "datetime" for m in x_modes) else "numeric"
            x_label = "Zaman (UTC)" if axis_mode == "date" else ("Zaman (s)" if any(m == "seconds" for m in x_modes) else "İndeks")
            self.finished.emit({"series": series, "axis_mode": axis_mode, "x_label": x_label})
        except Exception as e:
            self.failed.emit(f"Kanal verisi yüklenemedi:\n{e}")


class LazyViewLoadWorker(CancellableWorker):
    def __init__(self, requests: List[dict], max_points: int) -> None:
        super().__init__()
        self.requests = requests
        self.max_points = max_points

    def run(self) -> None:
        if self.is_cancelled:
            self.finished.emit({"updates": {}, "cancelled": True})
            return
        try:
            by_path: Dict[str, List[dict]] = {}
            for r in self.requests:
                if r.get("path"):
                    by_path.setdefault(r["path"], []).append(r)
            updates: Dict[str, dict] = {}
            for path, reqs in by_path.items():
                if self.is_cancelled:
                    break
                try:
                    with _open_tdms(path) as tdms:
                        for r in reqs:
                            if self.is_cancelled:
                                break
                            skey = r.get("style_key", "")
                            g, c = r.get("group"), r.get("channel")
                            n = int(r.get("n", 0) or 0)
                            x_mode = r.get("x_mode", "index")
                            x_base = float(r.get("x_base", 0.0) or 0.0)
                            x_inc = float(r.get("x_inc", 1.0) or 1.0)
                            x0, x1 = float(r.get("x0", 0)), float(r.get("x1", 0))
                            if not (skey and g and c and n > 0):
                                continue
                            try:
                                ch = tdms[g][c]
                            except Exception:
                                continue
                            i0, i1 = _slice_indices_from_xrange(x0, x1, n, x_mode, x_base, x_inc)
                            stride = _stride_for_window(i0, i1, self.max_points)
                            try:
                                y = ch[i0:i1:stride]
                            except Exception:
                                indices = list(range(i0, i1, stride))
                                y = np.asarray([ch[i] for i in indices])
                            y = _ensure_1d_numeric(y)
                            idx_arr = np.arange(i0, i1, stride, dtype=np.float64)
                            x = idx_arr if x_mode == "index" else (x_base + idx_arr * x_inc).astype(np.float64)
                            updates[skey] = {"x": x, "y": y, "win": (i0, i1, stride)}
                except Exception as e:
                    logger.warning("Lazy view okuma hatası %s: %s", path, e)
            self.finished.emit({"updates": {} if self.is_cancelled else updates, "cancelled": self.is_cancelled})
        except Exception as e:
            self.failed.emit(f"Lazy view okunamadı:\n{e}")


class FilterWorker(CancellableWorker):
    def __init__(self, token: int, series: List[dict], apply_filter: bool, window_len: int) -> None:
        super().__init__()
        self.token = token
        self.series = series
        self.apply_filter = apply_filter
        self.window_len = window_len

    def run(self) -> None:
        if self.is_cancelled:
            self.finished.emit({"token": self.token, "display_series": [], "cancelled": True})
            return
        try:
            display_series: List[dict] = []
            for s in self.series:
                if self.is_cancelled:
                    break
                y = s["y"]
                tag = ""
                is_dig = bool(s.get("is_digital"))
                if self.apply_filter and not is_dig:
                    if not SCIPY_AVAILABLE:
                        tag = "(SG yok)"
                    else:
                        try:
                            y = apply_savgol_safe(y, self.window_len)
                            tag = "(SG)"
                        except Exception:
                            tag = "(SG HATA)"
                elif is_dig:
                    tag = "(Dijital)"
                ss = s.copy()
                ss["y"] = y
                ss["tag"] = tag
                display_series.append(ss)
            axis_mode, x_label = _axis_mode_and_label(display_series)
            self.finished.emit({"token": self.token, "display_series": display_series, "axis_mode": axis_mode, "x_label": x_label})
        except Exception as e:
            self.failed.emit(f"Filtreleme yapılamadı:\n{e}")


class FFTWorker(CancellableWorker):
    def __init__(self, name: str, x: np.ndarray, y: np.ndarray, fs_hint: Optional[float],
                 use_window: bool, remove_mean: bool, detrend_linear: bool) -> None:
        super().__init__()
        self.name = name
        self.x, self.y = x, y
        self.fs_hint = fs_hint
        self.use_window = use_window
        self.remove_mean = remove_mean
        self.detrend_linear = detrend_linear

    def run(self) -> None:
        empty: dict = {"name": self.name, "freq": np.array([], dtype=np.float64),
                       "mag_linear": np.array([], dtype=np.float64), "cancelled": True}
        if self.is_cancelled:
            self.finished.emit(empty)
            return
        try:
            y = np.asarray(self.y, dtype=np.float64).copy()
            if y.size < 8:
                raise ValueError("FFT için yeterli örnek yok (en az 8 gerekli).")
            if self.detrend_linear:
                y = linear_detrend_safe(y)
            elif self.remove_mean:
                y -= np.nanmean(y)
            y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
            fs = robust_fs_from_x(np.asarray(self.x, dtype=np.float64))
            if fs is None:
                fs = self.fs_hint
            if fs is None or fs <= 0:
                raise ValueError("Örnekleme hızı (Fs) bilinmiyor. Fs girin ya da zaman eksenli kanal seçin.")
            window_name = "Yok"
            coherent_gain = 1.0
            if self.use_window:
                win = np.hanning(y.size)
                coherent_gain = max(float(np.mean(win)), 1e-12)
                y *= win
                window_name = "Hanning"
            if self.is_cancelled:
                self.finished.emit(empty)
                return
            Y = np.fft.rfft(y)
            freq = np.fft.rfftfreq(y.size, d=1.0 / fs)
            mag = np.abs(Y) / max(1, y.size) / coherent_gain
            if mag.size > 2:
                mag[1:-1] *= 2.0
            self.finished.emit({
                "name": self.name, "freq": freq.astype(np.float64),
                "mag_linear": mag.astype(np.float64),
                "fs": float(fs), "n": int(y.size), "df": float(fs / y.size),
                "window_name": window_name,
                "remove_mean": self.remove_mean, "detrend_linear": self.detrend_linear,
                "use_window": self.use_window,
            })
        except Exception as e:
            self.failed.emit(f"FFT hesaplanamadı:\n{e}")
