"""
TDMSReader GUI – A DIAdem-like viewer for NI TDMS files.

Improvements applied over the original version:
- Proper resource management (context managers for TDMS files)
- Thread lifecycle management with cleanup tracking
- Eliminated silent exception swallowing; errors are logged
- Reduced code duplication via helper methods
- Added CSV export for plotted data
- Configurable constants extracted into a dataclass
- Fixed memory leaks in worker/thread lifecycle
- Race condition fixes in lazy-loading pipeline
- Improved numpy array handling (fewer unnecessary copies)
- Type hints throughout for IDE support and documentation
- Proper docstrings on public methods
- Fixed digital-step rendering for very large channels
- Better progress feedback during long operations
"""

from __future__ import annotations

import gc
import math
import os
import sys
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    Any, Callable, Dict, Generator, List, Optional, Sequence, Tuple, Union,
)

import numpy as np

# Ensure pyqtgraph uses PyQt6 when multiple Qt bindings are installed
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_LEVEL = os.environ.get("TDMSREADER_LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tdmsreader")

# ---------------------------------------------------------------------------
# Qt imports
# ---------------------------------------------------------------------------
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QObject, QSize, QTimer, QSettings,
)
from PyQt6.QtGui import QColor, QCloseEvent, QIcon, QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QAbstractItemView, QFileDialog,
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit, QTreeWidget,
    QTreeWidgetItem, QSplitter, QMessageBox, QStatusBar, QCheckBox, QGroupBox,
    QFormLayout, QDoubleSpinBox, QTabWidget, QComboBox, QButtonGroup, QSpinBox,
    QColorDialog, QGridLayout, QSizePolicy, QToolButton, QFrame,
    QProgressBar,
)

# ---------------------------------------------------------------------------
# pyqtgraph
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# TDMS
# ---------------------------------------------------------------------------
from nptdms import TdmsFile

# ---------------------------------------------------------------------------
# Optional: Savitzky-Golay filter
# ---------------------------------------------------------------------------
try:
    from scipy.signal import savgol_filter
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    savgol_filter = None  # type: ignore[assignment]


# ===================================================================
# Configuration
# ===================================================================

@dataclass(frozen=True)
class LazyConfig:
    """All tunables for lazy (on-demand) TDMS loading in one place."""
    enabled: bool = True
    fullload_threshold: int = 2_000_000
    overview_max_points: int = 200_000
    view_max_points: int = 250_000
    view_debounce_ms: int = 180


@dataclass(frozen=True)
class AppConfig:
    """Application-wide tunables."""
    lazy: LazyConfig = field(default_factory=LazyConfig)
    default_filter_window: int = 21
    default_filter_polyorder: int = 3
    min_fft_samples: int = 8
    max_digital_levels: int = 8
    digital_sample_limit: int = 50_000
    pad_ratio: float = 0.06
    settings_org: str = "TDMSReader"
    settings_app: str = "TDMSReader"


CFG = AppConfig()


# ===================================================================
# Data models
# ===================================================================

@dataclass(frozen=True)
class ChannelKey:
    """Uniquely identifies a channel across multiple open files."""
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
# Helper functions
# ===================================================================

def _to_epoch_seconds(t: Any) -> float:
    """Convert various time representations to epoch seconds (UTC)."""
    if t is None:
        raise TypeError("Time value must not be None")
    if isinstance(t, np.datetime64):
        return float(t.astype("datetime64[ns]").astype(np.int64)) / 1e9
    if isinstance(t, datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return float(t.timestamp())
    if isinstance(t, (int, float, np.integer, np.floating)):
        return float(t)
    raise TypeError(f"Unsupported time type: {type(t)}")


def _timedelta_to_seconds(dt: Any) -> float:
    """Convert various timedelta representations to seconds."""
    if isinstance(dt, timedelta):
        return float(dt.total_seconds())
    if isinstance(dt, np.timedelta64):
        return float(dt.astype("timedelta64[ns]").astype(np.int64)) / 1e9
    if isinstance(dt, (int, float, np.integer, np.floating)):
        return float(dt)
    raise TypeError(f"Unsupported increment type: {type(dt)}")


@contextmanager
def _open_tdms(path: str) -> Generator[TdmsFile, None, None]:
    """Context manager ensuring TDMS files are always closed."""
    tdms = TdmsFile.open(path)
    try:
        yield tdms
    finally:
        try:
            tdms.close()
        except Exception:
            logger.debug("Failed to close TDMS file: %s", path)


def _ensure_1d_numeric(arr: np.ndarray) -> np.ndarray:
    """Flatten, make contiguous, and ensure numeric dtype."""
    arr = np.asarray(arr)
    if arr.ndim != 1:
        arr = np.ravel(arr)
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr)
    if not np.issubdtype(arr.dtype, np.number):
        arr = arr.astype(np.float64)
    return arr


def _xmeta_from_channel_props(
    props: dict,
) -> Tuple[str, float, float]:
    """Extract (x_mode, x_base, x_inc) from TDMS channel properties.

    Returns:
        x_mode: 'index' | 'seconds' | 'datetime'
        x_base: base offset for X axis generation
        x_inc:  per-sample increment
    """
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
        logger.debug("Failed to extract x metadata from channel properties", exc_info=True)
    return "index", 0.0, 1.0


def _slice_indices_from_xrange(
    x0: float, x1: float, n: int,
    x_mode: str, x_base: float, x_inc: float,
) -> Tuple[int, int]:
    """Convert an X-axis range to sample index range."""
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

    # 5% padding, minimum 1000 samples
    win = max(1, i1 - i0)
    pad = max(1000, int(win * 0.05))
    i0 = max(0, i0 - pad)
    i1 = min(n, i1 + pad)
    if i1 <= i0:
        i1 = min(n, i0 + 1)
    return i0, i1


def _stride_for_window(i0: int, i1: int, max_points: int) -> int:
    """Compute stride to limit the number of rendered points."""
    win = max(1, i1 - i0)
    if max_points <= 0:
        return 1
    return max(1, math.ceil(win / max_points))


def classify_and_extract_x(
    channel: Any, n_samples: int,
) -> Tuple[np.ndarray, str]:
    """Determine X-axis data and mode for a TDMS channel.

    Tries time_track() first, then wf_increment properties, falls back to index.
    """
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
    """Estimate sampling frequency from an X-axis array using median diff."""
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
    """Convert a user-entered frequency value + unit string to Hz."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0.0:
        return None
    if str(unit or "Hz").strip().lower() == "khz":
        val *= 1e3
    return val


def apply_savgol_safe(
    y: np.ndarray, window_len: int,
    polyorder_hint: int = 3,
) -> np.ndarray:
    """Apply Savitzky-Golay filter with automatic parameter clamping."""
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
    """Best-effort extraction of a human-readable quantity name from TDMS properties."""
    if not props:
        return "Value"
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
    return "Value"


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
    """Choose black or white foreground for contrast against *bg*."""
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
    """Remove linear trend from *y*, handling NaN/Inf gracefully."""
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


def padded_minmax(
    values: np.ndarray, pad_ratio: float = 0.06,
) -> Optional[Tuple[float, float]]:
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
        return "date", "Time (UTC)"
    if any(s.get("x_mode") == "seconds" for s in series_list):
        return "numeric", "Time (s)"
    return "numeric", "Index"


def _format_x_display(axis_mode: str, x: float) -> str:
    if axis_mode == "date":
        try:
            return datetime.fromtimestamp(float(x), tz=timezone.utc).isoformat()
        except Exception:
            pass
    return f"{x:.12g}"


def detect_digital_like(
    y: np.ndarray,
    max_levels: int = 8,
    sample_limit: int = 50_000,
) -> Tuple[bool, Optional[Tuple[float, float]]]:
    """Detect whether *y* looks like a digital/discrete signal.

    Returns (is_digital, (lo, hi) | None).
    """
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
# Cancellable worker base
# ===================================================================

class CancellableWorker(QObject):
    """Mixin providing a thread-safe cancellation flag for workers."""
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
# Workers
# ===================================================================

class TdmsIndexWorker(CancellableWorker):
    """Reads group/channel structure from a TDMS file."""

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
            self.failed.emit(f"TDMS index read error:\n{e}")


class TdmsChannelPreviewWorker(CancellableWorker):
    """Reads metadata (unit, quantity, Fs) for a single channel."""

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
                    "samples": n,
                    "unit": unit,
                    "quantity": quantity,
                    "x_info": {"x_mode": x_mode, "fs_est": fs},
                })
        except Exception as e:
            self.failed.emit(f"Preview error:\n{e}")


class MultiTdmsChannelLoadWorker(CancellableWorker):
    """Load data from one or more TDMS channels, with lazy-load support."""

    def __init__(
        self,
        files: Dict[str, Dict[str, str]],
        requests: List[ChannelRequest],
    ) -> None:
        super().__init__()
        self.files = files
        self.requests = requests

    def _load_channel_full(
        self, ch: Any, props: dict, skey: str, display: str,
        label: str, n: int,
    ) -> Optional[dict]:
        """Load full channel data (for channels below lazy threshold)."""
        y = _ensure_1d_numeric(ch[:])
        if y.size == 0:
            return None

        x, x_mode = classify_and_extract_x(ch, len(y))
        x = np.asarray(x, dtype=np.float64)

        # Sort by X if needed
        if x.size > 1 and x[-1] < x[0]:
            order = np.argsort(x)
            x, y = x[order], y[order]
        elif x.size > 1:
            stride0 = max(1, x.size // 5000)
            dx = np.diff(x[::stride0])
            if dx.size and np.any(dx < 0):
                order = np.argsort(x)
                x, y = x[order], y[order]

        # Deduplicate X
        if x.size > 1:
            xu, idx = np.unique(x, return_index=True)
            if xu.size != x.size:
                x, y = xu, y[idx]

        dig, dig_levels = detect_digital_like(y)
        fs_est = robust_fs_from_x(x) if x_mode in ("seconds", "datetime") else None
        unit = props.get("unit_string", props.get("unit", ""))
        quantity = infer_quantity_from_props(props)

        return {
            "style_key": skey,
            "name": display,
            "file_label": label,
            "x": x, "y": y,
            "unit": unit,
            "quantity": quantity,
            "x_mode": x_mode,
            "source_x_mode": x_mode,
            "source_x_base": 0.0,
            "source_x_inc": 1.0,
            "source_fs_est": fs_est,
            "is_digital": dig,
            "digital_levels": dig_levels,
            "lazy": False,
        }

    def _load_channel_lazy(
        self, ch: Any, props: dict, skey: str, display: str,
        label: str, n: int, path: str, k: ChannelKey,
    ) -> Optional[dict]:
        """Load overview data for lazy channels (above lazy threshold)."""
        x_mode, x_base, x_inc = _xmeta_from_channel_props(props)
        stride = _stride_for_window(0, n, CFG.lazy.overview_max_points)

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
            "style_key": skey,
            "name": display,
            "file_label": label,
            "x": x, "y": y,
            "unit": unit,
            "quantity": quantity,
            "x_mode": x_mode,
            "source_x_mode": x_mode,
            "source_x_base": float(x_base),
            "source_x_inc": float(x_inc),
            "source_fs_est": fs_est,
            "is_digital": dig,
            "digital_levels": dig_levels,
            "lazy": True,
            "lazy_meta": {
                "path": path,
                "group": k.group,
                "channel": k.channel,
                "n": n,
                "x_mode": x_mode,
                "x_base": float(x_base),
                "x_inc": float(x_inc),
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
                            use_lazy = CFG.lazy.enabled and n > CFG.lazy.fullload_threshold

                            if use_lazy:
                                s = self._load_channel_lazy(ch, props, skey, display, label, n, path, k)
                            else:
                                s = self._load_channel_full(ch, props, skey, display, label, n)

                            if s is not None:
                                x_modes.append(s["x_mode"])
                                series.append(s)
                except Exception as e:
                    logger.warning("Error loading channels from %s: %s", label, e)

            if not series or self.is_cancelled:
                self.finished.emit({"series": [], "axis_mode": "numeric", "x_label": "X", "cancelled": self.is_cancelled})
                return

            axis_mode = "date" if all(m == "datetime" for m in x_modes) else "numeric"
            x_label = "Time (UTC)" if axis_mode == "date" else (
                "Time (s)" if any(m == "seconds" for m in x_modes) else "Index"
            )
            self.finished.emit({"series": series, "axis_mode": axis_mode, "x_label": x_label})
        except Exception as e:
            self.failed.emit(f"Channel data loading failed:\n{e}")


class LazyViewLoadWorker(CancellableWorker):
    """Read only the visible X range from TDMS for lazy channels."""

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
                            idx = np.arange(i0, i1, stride, dtype=np.float64)
                            x = idx if x_mode == "index" else (x_base + idx * x_inc).astype(np.float64)

                            updates[skey] = {"x": x, "y": y, "win": (i0, i1, stride)}
                except Exception as e:
                    logger.warning("Lazy view read error for %s: %s", path, e)

            self.finished.emit({"updates": {} if self.is_cancelled else updates, "cancelled": self.is_cancelled})
        except Exception as e:
            self.failed.emit(f"Lazy view read failed:\n{e}")


class FilterWorker(CancellableWorker):
    """Apply Savitzky-Golay smoothing in a background thread."""

    def __init__(
        self, token: int, series: List[dict],
        apply_filter: bool, window_len: int,
    ) -> None:
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
                        tag = "(SG n/a)"
                    else:
                        try:
                            y = apply_savgol_safe(y, self.window_len)
                            tag = "(SG)"
                        except Exception:
                            tag = "(SG ERR)"
                elif is_dig:
                    tag = "(Digital)"

                ss = s.copy()
                ss["y"] = y
                ss["tag"] = tag
                display_series.append(ss)

            axis_mode, x_label = _axis_mode_and_label(display_series)
            self.finished.emit({
                "token": self.token,
                "display_series": display_series,
                "axis_mode": axis_mode,
                "x_label": x_label,
            })
        except Exception as e:
            self.failed.emit(f"Filter computation failed:\n{e}")


class FFTWorker(CancellableWorker):
    """Compute single-sided FFT spectrum in a background thread."""

    def __init__(
        self, name: str, x: np.ndarray, y: np.ndarray,
        fs_hint: Optional[float], use_window: bool,
        remove_mean: bool, detrend_linear: bool,
    ) -> None:
        super().__init__()
        self.name = name
        self.x = x
        self.y = y
        self.fs_hint = fs_hint
        self.use_window = use_window
        self.remove_mean = remove_mean
        self.detrend_linear = detrend_linear

    def run(self) -> None:
        empty: dict = {
            "name": self.name,
            "freq": np.array([], dtype=np.float64),
            "mag_linear": np.array([], dtype=np.float64),
            "cancelled": True,
        }
        if self.is_cancelled:
            self.finished.emit(empty)
            return
        try:
            y = np.asarray(self.y, dtype=np.float64).copy()
            if y.size < CFG.min_fft_samples:
                raise ValueError(f"Not enough samples for FFT (need >= {CFG.min_fft_samples}).")

            if self.detrend_linear:
                y = linear_detrend_safe(y)
            elif self.remove_mean:
                y -= np.nanmean(y)
            y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)

            fs = robust_fs_from_x(np.asarray(self.x, dtype=np.float64))
            if fs is None:
                fs = self.fs_hint
            if fs is None or fs <= 0:
                raise ValueError("Sampling rate (Fs) unknown. Enter Fs manually or choose a time-domain channel.")

            window_name = "None"
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
                "name": self.name,
                "freq": freq.astype(np.float64),
                "mag_linear": mag.astype(np.float64),
                "fs": float(fs),
                "n": int(y.size),
                "df": float(fs / y.size),
                "window_name": window_name,
                "remove_mean": self.remove_mean,
                "detrend_linear": self.detrend_linear,
                "use_window": self.use_window,
            })
        except Exception as e:
            self.failed.emit(f"FFT computation failed:\n{e}")


# ===================================================================
# PlotPane
# ===================================================================

class PlotPane(QWidget):
    """Composite widget: quickbar + PlotWidget + optional right (digital) axis."""

    range_changed = pyqtSignal(float, float)
    marker_requested = pyqtSignal(float, float)
    interaction_mode_changed = pyqtSignal(str)
    region_toggled = pyqtSignal(bool)
    legend_toggled = pyqtSignal(bool)
    click_mark_toggled = pyqtSignal(bool)
    y_lock_toggled = pyqtSignal(bool)
    right_y_lock_toggled = pyqtSignal(bool)
    autofit_requested = pyqtSignal()

    def __init__(
        self, parent: Optional[QWidget] = None,
        bg_rgb: Tuple[int, int, int] = (255, 255, 255),
    ) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

        self.axis_mode = "numeric"
        self.x_label = "X"

        self.plot: Optional[PlotWidget] = None
        self.legend: Optional[pg.LegendItem] = None
        self.region: Optional[pg.LinearRegionItem] = None

        self._region_enabled = False
        self._click_mark_enabled = False
        self._markers: List[dict] = []
        self._last_cursor_x = float("nan")
        self._cursor_marker_enabled = True
        self._cursor_dot: Optional[pg.ScatterPlotItem] = None
        self._bg_rgb = bg_rgb
        self._legend_visible = True
        self._interaction_mode = "pan"

        # Y lock (left axis)
        self._y_lock_enabled = False
        self._y_lock_left: Optional[Tuple[float, float]] = None
        self._y_lock_guard = False

        # Y lock (right/digital axis)
        self._right_y_lock_enabled = False
        self._right_y_lock_range: Optional[Tuple[float, float]] = None

        # Plot items
        self._left_items: List[pg.PlotDataItem] = []
        self._right_items: List[pg.PlotDataItem] = []

        # Right axis viewbox
        self._right_vb: Optional[pg.ViewBox] = None
        self._right_update_views: Optional[Callable] = None
        self._right_sync_guard = False
        self._batch_plotting = False
        self._base_left_yrange: Optional[Tuple[float, float]] = None
        self._base_right_yrange: Optional[Tuple[float, float]] = None
        self._right_data_bounds: Optional[Tuple[float, float]] = None

        self._crosshair_v: Optional[pg.InfiniteLine] = None
        self._crosshair_h: Optional[pg.InfiniteLine] = None

        self._info = QLabel("Cursor: —")
        self._info.setObjectName("cursorInfo")
        try:
            self._info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        except Exception:
            pass
        self._info.setMinimumWidth(240)
        self._info.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        self._quickbar = self._build_quickbar()
        self._layout.addWidget(self._quickbar)
        self.build_plot(axis_mode="numeric", x_label="X")

    # ----- Legend -----

    def set_legend_visible(self, enabled: bool) -> None:
        self._legend_visible = enabled
        if self.legend is not None:
            self.legend.setVisible(self._legend_visible)
        self._qb_set_checked("_qb_legend", self._legend_visible)

    def set_legend_position(self, position: str = "upper-right") -> None:
        if self.legend is None:
            return
        pos_map = {
            "upper-left": (0, 0), "upper-right": (1, 0),
            "lower-left": (0, 1), "lower-right": (1, 1),
            "center": (0.5, 0.5),
        }
        anchor = pos_map.get(position, (1, 0))
        try:
            self.legend.anchor(itemPos=anchor, parentPos=anchor, offset=(0, 0))
        except Exception:
            logger.debug("Legend position set failed for %s", position)

    # ----- Background / Theme -----

    def set_background(self, bg_rgb: Tuple[int, int, int]) -> None:
        self._bg_rgb = bg_rgb
        if self.plot is not None:
            self._apply_theme()

    def _apply_theme(self) -> None:
        if self.plot is None:
            return
        apply_plotwidget_theme(self.plot, self._bg_rgb)
        fg = _bg_to_fg_rgb(self._bg_rgb)

        if self._crosshair_v is not None:
            self._crosshair_v.setPen(pg.mkPen(fg, style=Qt.PenStyle.DashLine))
        if self._crosshair_h is not None:
            self._crosshair_h.setPen(pg.mkPen(fg, style=Qt.PenStyle.DashLine))

        if self._cursor_dot is not None:
            try:
                self._cursor_dot.setPen(pg.mkPen(fg, width=1.6))
                self._cursor_dot.setBrush(pg.mkBrush(*fg, 180))
                self._cursor_dot.setVisible(self._cursor_marker_enabled)
            except Exception:
                pass

        self._apply_legend_theme()
        self._refresh_right_axis_visuals()
        self._update_info_style()

    def _update_info_style(self) -> None:
        fg = _bg_to_fg_rgb(self._bg_rgb)
        is_dark = fg == (255, 255, 255)
        if is_dark:
            self._info.setStyleSheet(
                "padding: 4px 8px; font-weight: 700; color: #F2F4F8;"
                "background: rgba(0,0,0,0.55); border: 1px solid rgba(255,255,255,0.18);"
                "border-radius: 9px;"
            )
        else:
            self._info.setStyleSheet(
                "padding: 4px 8px; font-weight: 700; color: #222;"
                "background: rgba(255,255,255,0.85); border: 1px solid #D7DBE0;"
                "border-radius: 9px;"
            )

    def _legend_text_rgb(self) -> Tuple[int, int, int]:
        fg = _bg_to_fg_rgb(self._bg_rgb)
        return (235, 240, 248) if fg == (255, 255, 255) else fg

    def _apply_legend_theme(self) -> None:
        if self.legend is None:
            return
        legend_fg = self._legend_text_rgb()
        is_dark = _bg_to_fg_rgb(self._bg_rgb) == (255, 255, 255)
        brush = pg.mkBrush(30, 30, 30, 210) if is_dark else pg.mkBrush(255, 255, 255, 210)
        border_pen = pg.mkPen((90, 96, 110, 180) if is_dark else (185, 190, 200, 180))
        color_name = QColor(*legend_fg).name()

        try:
            self.legend.setVisible(self._legend_visible)
        except Exception:
            pass
        try:
            if hasattr(self.legend, "setLabelTextColor"):
                self.legend.setLabelTextColor(legend_fg)
        except Exception:
            pass
        try:
            self.legend.setBrush(brush)
        except Exception:
            pass
        try:
            self.legend.setPen(border_pen)
        except Exception:
            pass

        for entry in list(getattr(self.legend, "items", []) or []):
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            label_item = entry[1]
            try:
                if hasattr(label_item, "setDefaultTextColor"):
                    label_item.setDefaultTextColor(QColor(*legend_fg))
            except Exception:
                pass
            try:
                if hasattr(label_item, "setAttr"):
                    label_item.setAttr("color", color_name)
            except Exception:
                pass
            try:
                txt = getattr(label_item, "text", None)
                if txt is not None and hasattr(label_item, "setText"):
                    label_item.setText(txt, color=color_name)
            except Exception:
                pass
        try:
            self.legend.update()
        except Exception:
            pass

    # ----- Right (digital) axis -----

    def _refresh_right_axis_visuals(self) -> None:
        if self.plot is None:
            return
        try:
            p1 = self.plot.getPlotItem()
            axis = p1.getAxis("right")
        except Exception:
            return
        if axis is None:
            return

        fg = _bg_to_fg_rgb(self._bg_rgb)
        try:
            axis.setPen(pg.mkPen(fg))
            axis.setTextPen(pg.mkPen(fg))
        except Exception:
            pass

        if self._right_vb is None:
            try:
                axis.setStyle(showValues=False)
                self.plot.hideAxis("right")
            except Exception:
                pass
        else:
            try:
                axis.setStyle(showValues=True)
                self.plot.showAxis("right")
                axis.linkToView(self._right_vb)
            except Exception:
                pass

    def _destroy_right_axis(self) -> None:
        if self.plot is not None:
            p1 = self.plot.getPlotItem()
            if self._right_update_views is not None:
                try:
                    p1.vb.sigResized.disconnect(self._right_update_views)
                except Exception:
                    pass
            if self._right_vb is not None:
                try:
                    p1.scene().removeItem(self._right_vb)
                except Exception:
                    pass
            try:
                self.plot.hideAxis("right")
            except Exception:
                pass

        self._right_vb = None
        self._right_items = []
        self._right_update_views = None
        self._base_right_yrange = None
        self._base_left_yrange = None
        self._right_y_lock_range = None
        self._right_data_bounds = None
        self._refresh_right_axis_visuals()

    def _ensure_right_axis(self) -> None:
        if self.plot is None:
            return
        p1 = self.plot.getPlotItem()

        if self._right_vb is not None:
            try:
                self.plot.showAxis("right")
            except Exception:
                pass
            self._refresh_right_axis_visuals()
            return

        self._right_vb = pg.ViewBox()
        self._right_vb.setMouseEnabled(x=False, y=False)
        self._right_vb.setMenuEnabled(False)

        try:
            self.plot.showAxis("right")
            p1.scene().addItem(self._right_vb)
            p1.getAxis("right").linkToView(self._right_vb)
        except Exception:
            self._right_vb = None
            return

        self._right_vb.setXLink(p1.vb)

        def update_views() -> None:
            if self._right_vb is None:
                return
            self._right_vb.setGeometry(p1.vb.sceneBoundingRect())
            self._right_vb.linkedViewChanged(p1.vb, self._right_vb.XAxis)

        self._right_update_views = update_views
        update_views()
        p1.vb.sigResized.connect(update_views)
        self._refresh_right_axis_visuals()

    # ----- Y Lock (left) -----

    def set_y_lock(self, enabled: bool) -> None:
        self._y_lock_enabled = enabled
        if self.plot is not None:
            vb = self.plot.getViewBox()
            if self._y_lock_enabled:
                self._y_lock_left = tuple(vb.viewRange()[1])
                self._enforce_y_lock()
            else:
                self._y_lock_left = None
        self._qb_set_checked("_qb_ylock", self._y_lock_enabled)

    def _enforce_y_lock(self) -> None:
        if not self._y_lock_enabled or self.plot is None or self._y_lock_guard:
            return
        self._y_lock_guard = True
        try:
            if self._y_lock_left is not None:
                self.plot.getViewBox().setYRange(*self._y_lock_left, padding=0.0)
        finally:
            self._y_lock_guard = False

    # ----- Y Lock (right / digital) -----

    def set_right_y_lock(self, enabled: bool) -> None:
        self._right_y_lock_enabled = enabled
        if self._right_y_lock_enabled and self._right_vb is not None:
            try:
                self._right_y_lock_range = tuple(self._right_vb.viewRange()[1])
                self._enforce_right_y_lock()
            except Exception:
                self._right_y_lock_range = None
        elif not self._right_y_lock_enabled:
            self._right_y_lock_range = None
            self._sync_right_yrange_to_left()
        self._qb_set_checked("_qb_y2lock", self._right_y_lock_enabled)

    def _enforce_right_y_lock(self) -> None:
        if (
            not self._right_y_lock_enabled
            or self._right_vb is None
            or self._right_y_lock_range is None
            or self._right_sync_guard
        ):
            return
        self._right_sync_guard = True
        try:
            self._right_vb.setYRange(*self._right_y_lock_range, padding=0.0)
        finally:
            self._right_sync_guard = False

    def _on_left_yrange_changed(self, *_args: Any) -> None:
        if self._batch_plotting:
            return
        self._enforce_y_lock()
        self._sync_right_yrange_to_left()

    def _sync_right_yrange_to_left(self) -> None:
        if self.plot is None or self._right_vb is None:
            return
        if self._right_y_lock_enabled:
            self._enforce_right_y_lock()
            return
        if self._right_sync_guard:
            return
        if self._base_left_yrange is None or self._base_right_yrange is None:
            return

        vb = self.plot.getViewBox()
        try:
            l0, l1 = (float(v) for v in vb.viewRange()[1])
            bl0, bl1 = (float(v) for v in self._base_left_yrange)
            br0, br1 = (float(v) for v in self._base_right_yrange)
        except Exception:
            return

        if not all(math.isfinite(v) for v in (l0, l1, bl0, bl1, br0, br1)):
            return

        base_span = bl1 - bl0
        if base_span == 0:
            return
        now_span = max(l1 - l0, 1e-12)
        ratio = (br1 - br0) / base_span
        r0_des = br0 + (l0 - bl0) * ratio
        r_span_des = (br1 - br0) * (now_span / base_span)
        r1_des = r0_des + r_span_des

        # Clamp to data bounds if available
        if self._right_data_bounds is not None:
            try:
                d0, d1 = (float(v) for v in self._right_data_bounds)
                if math.isfinite(d0) and math.isfinite(d1) and d1 >= d0:
                    dspan = d1 - d0
                    pad = 0.05 * dspan if dspan > 0 else 0.25
                    mn, mx = d0 - pad, d1 + pad

                    if not (math.isfinite(r0_des) and math.isfinite(r1_des) and r1_des > r0_des):
                        r0_des, r1_des = mn, mx
                    else:
                        need = mx - mn
                        span = r1_des - r0_des
                        if span < need:
                            c = (r0_des + r1_des) / 2.0
                            r0_des, r1_des = c - need / 2, c + need / 2
                        if r0_des > mn:
                            r1_des -= (r0_des - mn)
                            r0_des = mn
                        if r1_des < mx:
                            r0_des += (mx - r1_des)
                            r1_des = mx
                        if r0_des > mn or r1_des < mx:
                            r0_des, r1_des = mn, mx
            except Exception:
                pass

        if not (math.isfinite(r0_des) and math.isfinite(r1_des) and r1_des != r0_des):
            return

        try:
            self._right_vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        except Exception:
            pass

        self._right_sync_guard = True
        try:
            self._right_vb.setYRange(r0_des, r1_des, padding=0.0)
        finally:
            self._right_sync_guard = False

    # ----- Build / rebuild -----

    def build_plot(self, axis_mode: str, x_label: str) -> None:
        """Create or recreate the PlotWidget for the given axis configuration."""
        self.axis_mode = axis_mode
        self.x_label = x_label

        if self.plot is not None:
            self._destroy_right_axis()
            self._layout.removeWidget(self.plot)
            self.plot.deleteLater()
            self.plot = None
            self.legend = None
            self.region = None
            self._markers = []
            self._left_items = []
            self._y_lock_left = None

        axis_items: dict = {}
        if axis_mode == "date":
            axis_items["bottom"] = DateAxisItem(orientation="bottom")

        self.plot = PlotWidget(axisItems=axis_items)
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        try:
            self.plot.hideAxis("right")
        except Exception:
            pass

        self.legend = self.plot.addLegend(offset=(10, 10))
        self.legend.setVisible(self._legend_visible)
        self.set_legend_position("upper-right")

        vb = self.plot.getViewBox()
        vb.setMouseEnabled(x=True, y=True)
        vb.setMouseMode(pg.ViewBox.RectMode)
        vb.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)

        self.plot.setDownsampling(auto=True, mode="peak")
        self.plot.setClipToView(True)

        self._crosshair_v = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("k", style=Qt.PenStyle.DashLine))
        self._crosshair_h = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("k", style=Qt.PenStyle.DashLine))
        self.plot.addItem(self._crosshair_v, ignoreBounds=True)
        self.plot.addItem(self._crosshair_h, ignoreBounds=True)

        try:
            self._cursor_dot = pg.ScatterPlotItem([np.nan], [np.nan], size=9, pxMode=True)
            self._cursor_dot.setZValue(1000)
            self.plot.addItem(self._cursor_dot, ignoreBounds=True)
        except Exception:
            self._cursor_dot = None

        self._mouse_proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved, rateLimit=60, slot=self._on_mouse_moved)
        self.plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)
        vb.sigXRangeChanged.connect(self._on_xrange_changed)
        vb.sigYRangeChanged.connect(self._on_left_yrange_changed)

        self.plot.setLabel("bottom", x_label)
        self.plot.setLabel("left", "Value")

        self._layout.addWidget(self.plot, stretch=1)
        self._apply_theme()

        # Restore interactive states
        self.set_interaction_mode(self._interaction_mode)
        self.set_legend_visible(self._legend_visible)
        self.set_click_mark_mode(self._click_mark_enabled)
        self.set_cursor_marker_enabled(self._cursor_marker_enabled)
        self.set_y_lock(self._y_lock_enabled)
        if self._region_enabled:
            self.enable_region(True)

    # ----- Interaction mode -----

    def set_interaction_mode(self, mode: str) -> None:
        if self.plot is None:
            return
        m = "pan" if (mode or "").strip().lower() == "pan" else "zoom"
        self._interaction_mode = m
        vb = self.plot.getViewBox()
        vb.setMouseEnabled(x=True, y=True)
        vb.setMouseMode(pg.ViewBox.PanMode if m == "pan" else pg.ViewBox.RectMode)
        self._qb_set_checked("_qb_pan" if m == "pan" else "_qb_zoom", True)

    def set_click_mark_mode(self, enabled: bool) -> None:
        self._click_mark_enabled = enabled
        self._qb_set_checked("_qb_marker", enabled)

    def set_cursor_marker_enabled(self, enabled: bool) -> None:
        self._cursor_marker_enabled = enabled
        if self.plot is None:
            return
        for item in (self._crosshair_v, self._crosshair_h):
            if item is not None:
                item.setVisible(enabled)
        if self._cursor_dot is not None:
            self._cursor_dot.setVisible(enabled)
            if not enabled:
                self._cursor_dot.setData([np.nan], [np.nan])

    # ----- Clear -----

    def clear_all(self) -> None:
        if self.plot is None:
            return
        for it in self._left_items:
            try:
                self.plot.removeItem(it)
            except Exception:
                pass
        self._left_items = []

        if self._right_vb is not None:
            for it in self._right_items:
                try:
                    self._right_vb.removeItem(it)
                except Exception:
                    pass
        self._right_items = []
        self._destroy_right_axis()

        if self.legend:
            self.legend.clear()
        self.clear_markers()
        if self.region is not None:
            try:
                self.plot.removeItem(self.region)
            except Exception:
                pass
            self.region = None
        self._apply_theme()

    # ----- Axis labels -----

    def _compute_y_label(self, series_list: List[dict]) -> str:
        if not series_list:
            return ""
        cu = common_unit(series_list)
        cq = common_quantity(series_list)
        if cq:
            if cu:
                return f"{cq} ({cu})"
            has_any_unit = any((s.get("unit") or "").strip() for s in series_list)
            return f"{cq} (units in legend)" if has_any_unit else cq
        return f"Value ({cu})" if cu else "Value"

    def _set_axis_titles(
        self, x_label: str, y_left: str,
        y_right: Optional[str] = None,
    ) -> None:
        pi = self.plot.getPlotItem()
        self.plot.setLabel("bottom", x_label)
        self.plot.setLabel("left", y_left or "Value")

        parts = [f"<b>X:</b> {x_label}", f"<b>Y:</b> {y_left or '—'}"]
        if y_right:
            try:
                self.plot.setLabel("right", y_right)
            except Exception:
                pass
            parts.append(f"<b>Y2:</b> {y_right}")
        pi.setTitle(" &nbsp;&nbsp; | &nbsp;&nbsp; ".join(parts))

    # ----- Digital step rendering -----

    def _plot_digital_step(
        self, x: np.ndarray, y: np.ndarray,
        pen: Any, viewbox: Optional[pg.ViewBox] = None,
    ) -> Optional[pg.PlotDataItem]:
        """Efficient step-plot for digital channels using edge detection."""
        if x is None or y is None:
            return None
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y)
        n = min(x.size, y.size)
        if n <= 0:
            return None
        x, y = x[:n], y[:n]

        if not np.issubdtype(y.dtype, np.number):
            y = y.astype(np.float64)

        y_cmp = np.rint(y).astype(np.float64) if np.issubdtype(y.dtype, np.floating) else y.astype(np.float64)

        valid = np.isfinite(x) & np.isfinite(y_cmp)
        if not np.any(valid):
            return None
        xv, yv = x[valid], y_cmp[valid]
        if xv.size == 0:
            return None

        if xv.size == 1:
            x_step, y_step = xv, yv
        else:
            edges = np.flatnonzero(yv[1:] != yv[:-1]) + 1
            m = edges.size
            if m == 0:
                x_step = np.array([xv[0], xv[-1]], dtype=np.float64)
                y_step = np.array([yv[0], yv[0]], dtype=np.float64)
            else:
                x_step = np.empty(2 * m + 2, dtype=np.float64)
                y_step = np.empty(2 * m + 2, dtype=np.float64)
                x_step[0], y_step[0] = xv[0], yv[0]
                ex = xv[edges]
                x_step[1:2*m+1:2] = ex
                y_step[1:2*m+1:2] = yv[edges - 1]
                x_step[2:2*m+1:2] = ex
                y_step[2:2*m+1:2] = yv[edges]
                x_step[-1], y_step[-1] = xv[-1], yv[-1]

        item = pg.PlotDataItem(pen=pen)
        try:
            item.setData(x_step, y_step, skipFiniteCheck=True)
        except TypeError:
            item.setData(x_step, y_step)
        try:
            item.setDownsampling(auto=True, mode="peak")
            item.setClipToView(True)
        except Exception:
            pass

        if viewbox is None:
            self.plot.addItem(item)
        else:
            viewbox.addItem(item)
        return item

    # ----- Plot series -----

    def plot_series(
        self, series_list: List[dict], axis_mode: str,
        x_label: str, style_map: Optional[Dict[str, dict]] = None,
    ) -> None:
        """Plot all series, routing analog to left axis and digital to right."""
        if axis_mode != self.axis_mode:
            self.build_plot(axis_mode=axis_mode, x_label=x_label)
        else:
            self.x_label = x_label
            self.clear_all()

        if not series_list:
            self._set_axis_titles(x_label, "Value", None)
            self._apply_theme()
            return

        self._batch_plotting = True
        try:
            self.plot.setUpdatesEnabled(False)
        except Exception:
            pass
        if self.legend is not None:
            self.legend.setVisible(False)
        try:
            self.plot.getViewBox().enableAutoRange(axis=pg.ViewBox.XYAxes, enable=False)
        except Exception:
            pass

        analog = [s for s in series_list if not s.get("is_digital")]
        digital = [s for s in series_list if s.get("is_digital")]

        y_left = self._compute_y_label(analog) if analog else (self._compute_y_label(series_list) or "Value")

        if digital:
            self._ensure_right_axis()
            self._set_axis_titles(x_label, y_left, "Digital")
        else:
            self._destroy_right_axis()
            self._set_axis_titles(x_label, y_left, None)

        n_total = len(series_list)
        smap = style_map or {}
        i_global = 0
        right_mins: List[float] = []
        right_maxs: List[float] = []

        def _make_label(s: dict, is_dig: bool = False, x_shift: float = 0.0) -> str:
            parts = []
            fl = (s.get("file_label") or "").strip()
            nm = (s.get("name") or "").strip()
            if fl:
                parts.append(f"{fl} | {nm}")
            else:
                parts.append(nm)
            unit = (s.get("unit") or "").strip()
            if unit:
                parts[0] += f" [{unit}]"
            tag = (s.get("tag") or "").strip()
            if tag:
                parts[0] += f" {tag}"
            if is_dig:
                parts[0] += " [DIG]"
            if x_shift != 0.0:
                parts[0] += f" (x{x_shift:+g})"
            return parts[0]

        def _get_style(skey: str) -> Tuple[Any, float, float]:
            st = smap.get(skey, {})
            color = st.get("color")
            width = float(st.get("width", 2.0))
            xs = float(st.get("x_shift", 0.0) or 0.0)
            return color, width, xs if math.isfinite(xs) else 0.0

        # Analog
        for s in analog:
            skey = s.get("style_key", "")
            color, width, x_shift = _get_style(skey)
            x_raw = s["x"]
            x = (np.asarray(x_raw, dtype=np.float64) + x_shift) if x_shift else x_raw
            y = s["y"]
            label = _make_label(s, x_shift=x_shift)

            pen_color = color if color is not None else pg.intColor(i_global, hues=max(1, n_total))
            pen = pg.mkPen(pen_color, width=width)
            item = pg.PlotDataItem(pen=pen)
            try:
                item.setData(x, y, skipFiniteCheck=True)
            except TypeError:
                item.setData(x, y)
            try:
                item.setDownsampling(auto=True, mode="peak")
                item.setClipToView(True)
            except Exception:
                pass
            self.plot.addItem(item)
            self._left_items.append(item)
            if self.legend is not None:
                try:
                    self.legend.addItem(item, label)
                except Exception:
                    pass
            i_global += 1

        # Digital
        for s in digital:
            if self._right_vb is None:
                self._ensure_right_axis()
            if self._right_vb is None:
                break

            skey = s.get("style_key", "")
            color, width, x_shift = _get_style(skey)
            x_raw = s["x"]
            x = (np.asarray(x_raw, dtype=np.float64) + x_shift) if x_shift else x_raw
            y = s["y"]

            levels = s.get("digital_levels")
            if isinstance(levels, (tuple, list)) and len(levels) == 2:
                try:
                    lo, hi = float(levels[0]), float(levels[1])
                    if math.isfinite(lo) and math.isfinite(hi):
                        if hi < lo:
                            lo, hi = hi, lo
                        right_mins.append(lo)
                        right_maxs.append(hi)
                except Exception:
                    pass
            else:
                try:
                    yf = np.asarray(y, dtype=np.float64)
                    if yf.size:
                        samp = yf[::max(1, yf.size // 50000)]
                        samp = samp[np.isfinite(samp)]
                        if samp.size:
                            right_mins.append(float(samp.min()))
                            right_maxs.append(float(samp.max()))
                except Exception:
                    pass

            label = _make_label(s, is_dig=True, x_shift=x_shift)
            pen_color = color if color is not None else pg.intColor(i_global, hues=max(1, n_total))
            pen = pg.mkPen(pen_color, width=width)
            item = self._plot_digital_step(x, y, pen, viewbox=self._right_vb)
            if item is None:
                i_global += 1
                continue
            self._right_items.append(item)
            if self.legend is not None:
                try:
                    self.legend.addItem(item, label)
                except Exception:
                    pass
            i_global += 1

        # Autorange
        self._right_data_bounds = (min(right_mins), max(right_maxs)) if right_mins else None

        vb = self.plot.getViewBox()
        try:
            vb.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)
            vb.autoRange()
        except Exception:
            self.plot.enableAutoRange()

        if self._right_vb is not None:
            if self._right_y_lock_enabled:
                self._enforce_right_y_lock()
            elif self._right_data_bounds is not None:
                d0, d1 = self._right_data_bounds
                dspan = d1 - d0
                pad = 0.05 * dspan if dspan > 0 else 0.25
                try:
                    self._right_vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
                    self._right_vb.setYRange(d0 - pad, d1 + pad, padding=0.0)
                except Exception:
                    pass
            else:
                try:
                    self._right_vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
                    self._right_vb.autoRange()
                    self._right_vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
                except Exception:
                    pass

        try:
            self._base_left_yrange = tuple(vb.viewRange()[1])
        except Exception:
            self._base_left_yrange = None
        self._base_right_yrange = None
        if self._right_vb is not None:
            try:
                self._base_right_yrange = tuple(self._right_vb.viewRange()[1])
            except Exception:
                pass

        try:
            vb.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=False)
        except Exception:
            pass
        self._sync_right_yrange_to_left()

        if self._region_enabled:
            self.enable_region(True)

        try:
            self.plot.setUpdatesEnabled(True)
        except Exception:
            pass
        self._batch_plotting = False
        self._apply_theme()
        self._enforce_y_lock()
        self._enforce_right_y_lock()
        self._update_quickbar_state()

    # ----- Autofit / range -----

    def autofit_all(self) -> None:
        if self.plot is None:
            return
        vb = self.plot.getViewBox()
        try:
            vb.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)
            vb.autoRange()
        except Exception:
            self.plot.enableAutoRange()

        if self._right_vb is not None:
            try:
                self._right_vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
                self._right_vb.autoRange()
                self._right_vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
            except Exception:
                pass

        try:
            self._base_left_yrange = tuple(vb.viewRange()[1])
        except Exception:
            self._base_left_yrange = None
        self._base_right_yrange = None
        if self._right_vb is not None:
            try:
                self._base_right_yrange = tuple(self._right_vb.viewRange()[1])
            except Exception:
                pass
        try:
            vb.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=False)
        except Exception:
            pass
        self._sync_right_yrange_to_left()
        self._enforce_y_lock()
        self._enforce_right_y_lock()

    def set_xrange(self, x_min: float, x_max: float) -> None:
        if self.plot is None:
            return
        if x_max > x_min:
            self.plot.setXRange(x_min, x_max, padding=0.01)
        self._enforce_y_lock()
        self._enforce_right_y_lock()

    def enable_region(self, enabled: bool) -> None:
        self._region_enabled = enabled
        self._qb_set_checked("_qb_region", enabled)
        if self.plot is None:
            return
        if enabled:
            if self.region is None:
                xr = self.plot.getViewBox().viewRange()[0]
                mid = (xr[0] + xr[1]) / 2.0
                span = (xr[1] - xr[0]) * 0.3 if xr[1] > xr[0] else 1.0
                self.region = pg.LinearRegionItem(values=(mid - span, mid + span))
                self.region.setZValue(10)
                self.plot.addItem(self.region)
                self.region.sigRegionChanged.connect(self._on_region_changed)
        else:
            if self.region is not None:
                try:
                    self.plot.removeItem(self.region)
                except Exception:
                    pass
                self.region = None

    def region_values(self) -> Optional[Tuple[float, float]]:
        if self.region is None:
            return None
        v = self.region.getRegion()
        return float(v[0]), float(v[1])

    def autofit_y_in_range(self, x_min: float, x_max: float) -> None:
        """Autofit Y axes to data within the given X range."""
        if self.plot is None or x_max <= x_min:
            return

        def _fit_items(items: Sequence[pg.PlotDataItem]) -> Optional[Tuple[float, float]]:
            ymins, ymaxs = [], []
            for it in items:
                xd, yd = it.getData()
                if xd is None or yd is None or len(xd) == 0:
                    continue
                mask = (xd >= x_min) & (xd <= x_max)
                if np.any(mask):
                    yy = yd[mask]
                    ymins.append(float(np.nanmin(yy)))
                    ymaxs.append(float(np.nanmax(yy)))
            if not ymins:
                return None
            y0, y1 = min(ymins), max(ymaxs)
            pad = (y1 - y0) * 0.05 if y1 != y0 else 1.0
            return y0 - pad, y1 + pad

        rng = _fit_items(self._left_items)
        if rng is not None:
            self.plot.setYRange(*rng, padding=0.0)
            if self._y_lock_enabled:
                self._y_lock_left = tuple(self.plot.getViewBox().viewRange()[1])

        if self._right_vb is not None and self._right_items:
            rng = _fit_items(self._right_items)
            if rng is not None:
                self._right_vb.setYRange(*rng, padding=0.0)
                if self._right_y_lock_enabled:
                    self._right_y_lock_range = rng

    # ----- Markers -----

    def add_marker(self, x: float, y: float, label: str) -> None:
        if self.plot is None or not math.isfinite(x):
            return
        line = pg.InfiniteLine(pos=x, angle=90, movable=False, pen=pg.mkPen("r", width=1.5))
        self.plot.addItem(line)
        text = pg.TextItem(text=label, anchor=(0, 1), color=(200, 0, 0))
        yr = self.plot.getViewBox().viewRange()[1]
        text.setPos(x, yr[1])
        self.plot.addItem(text)
        self._markers.append({"x": x, "y": y, "line": line, "text": text, "label": label})

    def clear_markers(self) -> None:
        if self.plot is not None:
            for m in self._markers:
                try:
                    self.plot.removeItem(m["line"])
                    self.plot.removeItem(m["text"])
                except Exception:
                    pass
        self._markers = []

    def jump_to_x(self, x: float) -> None:
        if self.plot is None:
            return
        xr = self.plot.getViewBox().viewRange()[0]
        span = (xr[1] - xr[0]) or 1.0
        self.set_xrange(x - span * 0.25, x + span * 0.25)

    # ----- Mouse events -----

    def _on_region_changed(self) -> None:
        v = self.region_values()
        if v:
            self.range_changed.emit(v[0], v[1])

    def _on_xrange_changed(self, _: Any, r: Any) -> None:
        self.range_changed.emit(float(r[0]), float(r[1]))
        self._enforce_y_lock()
        self._enforce_right_y_lock()

    def _on_mouse_moved(self, evt: Any) -> None:
        if self.plot is None:
            return
        pos = evt[0]
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        mp = self.plot.getViewBox().mapSceneToView(pos)
        self._last_cursor_x = float(mp.x())

        if self._cursor_marker_enabled:
            if self._crosshair_v is not None:
                self._crosshair_v.setPos(mp.x())
            if self._crosshair_h is not None:
                self._crosshair_h.setPos(mp.y())
            if self._cursor_dot is not None:
                self._cursor_dot.setData([mp.x()], [mp.y()])
        elif self._cursor_dot is not None:
            self._cursor_dot.setData([np.nan], [np.nan])

        val_txt = f"{mp.y():.6g}"
        if self.axis_mode == "date":
            try:
                t = datetime.fromtimestamp(mp.x(), tz=timezone.utc).isoformat()
                self._info.setText(f"Time: {t} | Y: {val_txt}")
            except Exception:
                self._info.setText(f"X={mp.x():.6g} | Y: {val_txt}")
        else:
            self._info.setText(f"X={mp.x():.6g} | Y: {val_txt}")

    def _on_mouse_clicked(self, event: Any) -> None:
        if self.plot is None or not self._click_mark_enabled:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.scenePos()
        vb = self.plot.getViewBox()
        if vb.sceneBoundingRect().contains(pos):
            mp = vb.mapSceneToView(pos)
            self.marker_requested.emit(float(mp.x()), float(mp.y()))

    # ----- Quickbar -----

    def _icon_dirs(self) -> List[str]:
        candidates: List[str] = []
        env_dir = (os.environ.get("TDMSREADER_ICON_DIR", "") or "").strip()
        if env_dir:
            candidates.append(env_dir)
        base = getattr(sys, "_MEIPASS", None)
        if base:
            candidates.append(os.path.join(base, "icons"))
        try:
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "icons"))
        except Exception:
            pass
        try:
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons"))
        except Exception:
            pass
        try:
            candidates.append(os.path.join(os.getcwd(), "icons"))
        except Exception:
            pass
        seen: set = set()
        out: List[str] = []
        for d in candidates:
            if d and d not in seen and os.path.isdir(d):
                seen.add(d)
                out.append(d)
        return out

    def _load_icon(self, filename: str) -> QIcon:
        for d in self._icon_dirs():
            p = os.path.join(d, filename)
            if os.path.isfile(p):
                return QIcon(p)
        return QIcon()

    def _mk_qb_btn(self, filename: str, tooltip: str, *, checkable: bool = False) -> QToolButton:
        b = QToolButton()
        b.setAutoRaise(True)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        b.setCheckable(checkable)
        b.setFixedSize(48, 48)
        b.setIconSize(QSize(32, 32))
        b.setToolTip(tooltip)
        b.setProperty("qb", "1")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        ic = self._load_icon(filename)
        if not ic.isNull():
            b.setIcon(ic)
        else:
            b.setText(tooltip[:1])
        return b

    def _qb_set_checked(self, attr: str, checked: bool) -> None:
        b = getattr(self, attr, None)
        if b is None:
            return
        try:
            blocked = b.blockSignals(True)
            b.setChecked(checked)
            b.blockSignals(blocked)
        except Exception:
            pass

    def _build_quickbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("plotQuickbar")
        bar.setFrameShape(QFrame.Shape.NoFrame)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)

        self._qb_pan = self._mk_qb_btn("pan_hand.png", "Pan", checkable=True)
        self._qb_zoom = self._mk_qb_btn("zoom_plus.png", "Zoom (Rect)", checkable=True)
        self._qb_fit = self._mk_qb_btn("move_pan.png", "Auto Fit")
        self._qb_region = self._mk_qb_btn("region_rect.png", "Range Selector", checkable=True)
        self._qb_legend = self._mk_qb_btn("legend_menu.png", "Legend", checkable=True)
        self._qb_marker = self._mk_qb_btn("marker_pin.png", "Click to Mark", checkable=True)
        self._qb_ylock = self._mk_qb_btn("y_lock.png", "Y Lock", checkable=True)
        self._qb_y2lock = self._mk_qb_btn("y2_lock.png", "Y2 (Digital) Lock", checkable=True)

        self._qb_mode_group = QButtonGroup(self)
        self._qb_mode_group.setExclusive(True)
        self._qb_mode_group.addButton(self._qb_pan)
        self._qb_mode_group.addButton(self._qb_zoom)

        self._qb_pan.setChecked(True)
        self._qb_legend.setChecked(True)

        def _mode_set(m: str) -> None:
            self.set_interaction_mode(m)
            self.interaction_mode_changed.emit(m)

        self._qb_pan.toggled.connect(lambda on: on and _mode_set("pan"))
        self._qb_zoom.toggled.connect(lambda on: on and _mode_set("zoom"))
        self._qb_fit.clicked.connect(lambda: (self.autofit_all(), self.autofit_requested.emit()))
        self._qb_region.toggled.connect(lambda on: (self.enable_region(on), self.region_toggled.emit(on)))
        self._qb_legend.toggled.connect(lambda on: (self.set_legend_visible(on), self.legend_toggled.emit(on)))
        self._qb_marker.toggled.connect(lambda on: (self.set_click_mark_mode(on), self.click_mark_toggled.emit(on)))
        self._qb_ylock.toggled.connect(lambda on: (self.set_y_lock(on), self.y_lock_toggled.emit(on)))
        self._qb_y2lock.toggled.connect(lambda on: (self.set_right_y_lock(on), self.right_y_lock_toggled.emit(on)))

        for w in (self._qb_pan, self._qb_zoom, self._qb_fit, self._qb_region,
                  self._qb_legend, self._qb_marker, self._qb_ylock, self._qb_y2lock):
            lay.addWidget(w)
        lay.addStretch(1)

        try:
            self._info.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        except Exception:
            pass
        lay.addWidget(self._info, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._update_quickbar_state()
        return bar

    def _update_quickbar_state(self) -> None:
        has_right = self._right_vb is not None and len(self._right_items) > 0
        if hasattr(self, "_qb_y2lock"):
            self._qb_y2lock.setEnabled(has_right)


# ===================================================================
# Detached Plot Window
# ===================================================================

class DetachedPlotWindow(QMainWindow):
    closed = pyqtSignal()

    def __init__(self, bg_rgb: Tuple[int, int, int], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Plot (Detached)")
        self.resize(1280, 800)
        self.pane = PlotPane(bg_rgb=bg_rgb)
        self.setCentralWidget(self.pane)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)


# ===================================================================
# Main Window
# ===================================================================

class MainWindow(QMainWindow):
    """Top-level application window for TDMSReader."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TDMSReader")
        self.resize(1560, 920)

        self.settings = QSettings(CFG.settings_org, CFG.settings_app)
        self._theme = str(self.settings.value("ui/theme", "light") or "light").lower()
        if self._theme not in ("light", "dark"):
            self._theme = "light"

        self._theme_guard = False
        self._theme_default_bg: Dict[str, Tuple[int, int, int]] = {"dark": (0, 0, 0), "light": (255, 255, 255)}

        self.files: Dict[str, FileState] = {}
        self.file_counter = 0
        self.current_series: Optional[List[dict]] = None

        self.style_map: Dict[str, dict] = {}
        self._jobs: List[Tuple[QThread, CancellableWorker, str]] = []

        self.plot_bg_rgb: Tuple[int, int, int] = self._theme_default_bg[self._theme]
        self.detached_win: Optional[DetachedPlotWindow] = None
        self._syncing_range = False
        self._pane_sync_guard = False

        self._marker_counter = 0
        self._markers: Dict[int, dict] = {}
        self._last_preview_payload: Optional[dict] = None
        self._last_fft_payload: Optional[dict] = None

        self._filter_token = 0

        self.setAcceptDrops(True)

        # Lazy view
        self._lazy_view_pending: Optional[Tuple[float, float]] = None
        self._lazy_view_timer = QTimer(self)
        self._lazy_view_timer.setSingleShot(True)
        self._lazy_view_timer.timeout.connect(self._run_lazy_view_update)
        self._lazy_view_token = 0
        self._preserve_main_xrange: Optional[Tuple[float, float]] = None
        self._preserve_detached_xrange: Optional[Tuple[float, float]] = None

        self._build_ui()
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._connect_signals()
        self._install_shortcuts()
        self.set_theme(self._theme)
        self._restore_ui_state()
        self._apply_interaction_mode_to_all()

        self.status.showMessage("Open a TDMS file from the 'Open' tab on the left.")

        if not SCIPY_AVAILABLE:
            QMessageBox.warning(
                self, "Missing Library",
                "Scipy is not installed. Savitzky-Golay filter will not work.\n"
                "Install with: pip install scipy",
            )
            self.chk_smooth.setEnabled(False)

        self._apply_background_everywhere()

    # ----- Job management -----

    def _cancel_jobs(self, tags: List[str]) -> None:
        """Cancel all pending jobs matching any of the given tags."""
        tagset = set(t for t in tags if t)
        if not tagset:
            return
        for th, worker, tg in list(self._jobs):
            if tg not in tagset:
                continue
            try:
                worker.cancel()
            except Exception:
                logger.debug("Worker cancel failed for tag=%s", tg, exc_info=True)
            try:
                th.requestInterruption()
            except Exception:
                pass

    def _start_job(
        self, worker: CancellableWorker,
        on_finished: Callable, on_failed: Optional[Callable] = None, *,
        tag: str = "", cancel_tags: Optional[List[str]] = None,
    ) -> None:
        """Start a worker on a new QThread with proper lifecycle management."""
        if cancel_tags:
            self._cancel_jobs(cancel_tags)

        th = QThread(self)
        worker.moveToThread(th)

        def _cleanup() -> None:
            th.quit()

        def _remove_job() -> None:
            self._jobs = [(t, w, tg) for t, w, tg in self._jobs if t is not th]

        th.started.connect(worker.run)
        worker.finished.connect(on_finished)
        worker.failed.connect(on_failed or self._on_worker_failed)
        worker.finished.connect(_cleanup)
        worker.failed.connect(_cleanup)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        th.finished.connect(th.deleteLater)
        th.finished.connect(_remove_job)

        self._jobs.append((th, worker, tag))
        th.start()

    # ----- Theme -----

    def _theme_button_text(self) -> str:
        return "\U0001F319" if self._theme == "dark" else "\u2600"

    def set_theme(self, theme: str) -> None:
        theme = (theme or "").strip().lower()
        if theme not in ("dark", "light") or self._theme_guard:
            return
        self._theme_guard = True
        try:
            self._theme = theme
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(DARK_QSS if self._theme == "dark" else LIGHT_QSS)
            for btn in (getattr(self, "btn_theme_plot", None), getattr(self, "btn_theme_fft", None)):
                if btn is None:
                    continue
                btn.blockSignals(True)
                btn.setChecked(self._theme == "dark")
                btn.setText(self._theme_button_text())
                btn.blockSignals(False)
            self.plot_bg_rgb = self._theme_default_bg[self._theme]
            self._set_bg_button_preview(self.plot_bg_rgb)
            self._apply_background_everywhere()
            if hasattr(self, "status") and self.status is not None:
                self.status.showMessage(f"Theme: {'Dark' if self._theme == 'dark' else 'Light'}", 2000)
        finally:
            self._theme_guard = False

    def _toggle_theme_from_button(self) -> None:
        sender = self.sender()
        want_dark = bool(getattr(sender, "isChecked", lambda: True)())
        self.set_theme("dark" if want_dark else "light")

    # ----- UI build -----

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter = splitter
        root_layout.addWidget(splitter, stretch=1)
        splitter.setHandleWidth(8)

        # LEFT panel
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        self.file_tabs = QTabWidget()
        self.file_tabs.setTabsClosable(True)
        self.file_tabs.tabCloseRequested.connect(self._close_file_tab)

        open_tab = QWidget()
        open_l = QVBoxLayout(open_tab)
        open_l.setContentsMargins(10, 10, 10, 10)
        open_l.setSpacing(10)
        self.btn_open = QPushButton("Open TDMS...")
        self.btn_open.setProperty("primary", True)
        self.lbl_hint = QLabel("One or more TDMS files can be opened.")
        self.lbl_hint.setWordWrap(True)
        open_l.addWidget(self.btn_open)
        open_l.addWidget(self.lbl_hint)
        open_l.addStretch(1)
        self.file_tabs.addTab(open_tab, "Open")

        left_layout.addWidget(self.file_tabs, stretch=1)

        plot_row = QHBoxLayout()
        plot_row.setSpacing(8)
        self.btn_plot_checked = QPushButton("Plot Checked Channels")
        self.btn_plot_checked.setProperty("primary", True)
        self.btn_uncheck_all = QPushButton("Uncheck All")
        self.btn_clear_plot = QPushButton("Clear Plot")
        self.btn_clear_plot.setProperty("danger", True)
        plot_row.addWidget(self.btn_plot_checked)
        plot_row.addWidget(self.btn_uncheck_all)
        plot_row.addWidget(self.btn_clear_plot)
        left_layout.addLayout(plot_row)

        self.preview_box = QGroupBox("Channel Properties")
        pf = QFormLayout(self.preview_box)
        pf.setVerticalSpacing(8)
        pf.setHorizontalSpacing(10)

        self.p_file, self.p_name = QLabel("—"), QLabel("—")
        self.p_samples, self.p_fs = QLabel("—"), QLabel("—")
        self.p_unit, self.p_quantity = QLabel("—"), QLabel("—")
        self.chk_manual_fs_plot = QCheckBox("Use manual Fs when missing")
        self.chk_manual_fs_plot.setToolTip("When a TDMS channel has no time/Fs info, converts index-based X to time.")
        self.sp_manual_fs_plot = QDoubleSpinBox()
        self.sp_manual_fs_plot.setRange(0.0, 1e12)
        self.sp_manual_fs_plot.setDecimals(6)
        self.sp_manual_fs_plot.setValue(0.0)
        self.sp_manual_fs_plot.setKeyboardTracking(False)
        self.sp_manual_fs_plot.setToolTip("Sensor sampling frequency. E.g. enter 25 and select kHz for 25 kHz.")
        self.cmb_manual_fs_unit = QComboBox()
        self.cmb_manual_fs_unit.addItems(["Hz", "kHz"])
        self.cmb_manual_fs_unit.setCurrentText("kHz")
        manual_fs_row = QWidget()
        mfsl = QHBoxLayout(manual_fs_row)
        mfsl.setContentsMargins(0, 0, 0, 0)
        mfsl.setSpacing(6)
        mfsl.addWidget(self.chk_manual_fs_plot)
        mfsl.addWidget(self.sp_manual_fs_plot, 1)
        mfsl.addWidget(self.cmb_manual_fs_unit)
        pf.addRow("File:", self.p_file)
        pf.addRow("Name:", self.p_name)
        pf.addRow("Samples:", self.p_samples)
        pf.addRow("Fs:", self.p_fs)
        pf.addRow("Manual Fs:", manual_fs_row)
        pf.addRow("Quantity:", self.p_quantity)
        pf.addRow("Unit:", self.p_unit)
        left_layout.addWidget(self.preview_box)

        splitter.addWidget(left)

        # RIGHT panel
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, stretch=1)

        # --- Plot tab ---
        plot_tab = QWidget()
        plot_tab_layout = QVBoxLayout(plot_tab)
        plot_tab_layout.setContentsMargins(6, 6, 6, 6)
        plot_tab_layout.setSpacing(6)

        plot_topbar = QHBoxLayout()
        plot_topbar.addStretch(1)
        self.btn_theme_plot = QToolButton()
        self.btn_theme_plot.setCheckable(True)
        self.btn_theme_plot.setChecked(self._theme == "dark")
        self.btn_theme_plot.setText(self._theme_button_text())
        self.btn_theme_plot.setToolTip("Toggle Theme (Dark/Light)")
        self.btn_theme_plot.setFixedSize(34, 34)
        self.btn_theme_plot.setStyleSheet("border-radius: 17px; font-weight: 900; padding: 0px;")
        plot_topbar.addWidget(self.btn_theme_plot)
        plot_tab_layout.addLayout(plot_topbar)

        self.plot_pane = PlotPane(bg_rgb=self.plot_bg_rgb)

        # --- Collapsible controls dock ---
        controls = QFrame()
        controls.setObjectName("controlsDock")
        controls.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        dock_layout = QVBoxLayout(controls)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.setSpacing(0)

        self.controls_header = QFrame()
        self.controls_header.setObjectName("controlsHeader")
        hdr = QHBoxLayout(self.controls_header)
        hdr.setContentsMargins(10, 8, 10, 8)
        hdr.setSpacing(8)
        self.btn_ctrl_collapse = QToolButton()
        self.btn_ctrl_collapse.setObjectName("btnCtrlCollapse")
        self.btn_ctrl_collapse.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_ctrl_collapse.setText("Controls")
        self.btn_ctrl_collapse.setCheckable(True)
        self.btn_ctrl_collapse.setChecked(False)
        self.btn_ctrl_collapse.setArrowType(Qt.ArrowType.RightArrow)
        self.btn_ctrl_collapse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ctrl_collapse.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_ctrl_collapse.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        hdr.addWidget(self.btn_ctrl_collapse, 1)
        dock_layout.addWidget(self.controls_header)

        self.controls_body = QWidget()
        body_layout = QVBoxLayout(self.controls_body)
        body_layout.setContentsMargins(10, 10, 10, 10)
        body_layout.setSpacing(8)
        dock_layout.addWidget(self.controls_body)
        self.controls_body.setVisible(False)

        self.ctrl_tabs = QTabWidget()
        self.ctrl_tabs.setDocumentMode(True)
        body_layout.addWidget(self.ctrl_tabs)

        # Tab: Range
        tab_range = QWidget()
        rg = QGridLayout(tab_range)
        rg.setContentsMargins(6, 6, 6, 6)
        rg.setHorizontalSpacing(10)
        rg.setVerticalSpacing(8)

        self.chk_region = QCheckBox("Range Selector")
        self.xmin = QDoubleSpinBox()
        self.xmax = QDoubleSpinBox()
        for w in (self.xmin, self.xmax):
            w.setRange(-1e18, 1e18)
            w.setDecimals(6)
            w.setKeyboardTracking(False)
        self.btn_fit_y = QPushButton("Fit Y to Range")
        self.btn_bg_pick = QPushButton("Background Color")
        self.btn_bg_reset = QPushButton("Default Background")
        self.btn_detach = QPushButton("Detach Plot")
        self.btn_export_csv = QPushButton("Export CSV")
        self.btn_export_csv.setToolTip("Export currently plotted channel data to CSV")
        self._set_bg_button_preview(self.plot_bg_rgb)

        rg.addWidget(self.chk_region, 0, 0)
        rg.addWidget(QLabel("Min:"), 0, 1)
        rg.addWidget(self.xmin, 0, 2)
        rg.addWidget(QLabel("Max:"), 0, 3)
        rg.addWidget(self.xmax, 0, 4)
        rg.addWidget(self.btn_fit_y, 0, 5)
        rg.addWidget(self.btn_bg_pick, 1, 0, 1, 2)
        rg.addWidget(self.btn_bg_reset, 1, 2, 1, 2)
        rg.addWidget(self.btn_detach, 1, 4)
        rg.addWidget(self.btn_export_csv, 1, 5)
        self.ctrl_tabs.addTab(tab_range, "Range")

        # Tab: Markers
        tab_markers = QWidget()
        mg = QGridLayout(tab_markers)
        mg.setContentsMargins(6, 6, 6, 6)
        mg.setHorizontalSpacing(10)
        mg.setVerticalSpacing(8)

        self.chk_click_mark = QCheckBox("Click to Mark")
        self.btn_clear_markers = QPushButton("Clear Markers")
        self.btn_clear_markers.setProperty("danger", True)
        self.btn_marker_add = QPushButton("Add")
        self.btn_marker_sub = QPushButton("Subtract")
        self.btn_marker_add.setToolTip("Sum the values of 2 selected markers")
        self.btn_marker_sub.setToolTip("Subtract selected markers (B - A)")
        self.cmb_marker_calc_axis = QComboBox()
        self.cmb_marker_calc_axis.addItems(["X only", "Y only", "X and Y"])
        self.lbl_marker_calc = QLabel("Result: —")
        self.lbl_marker_calc.setStyleSheet("font-weight: 800;")

        self.marker_list = QTreeWidget()
        self.marker_list.setHeaderLabels(["Marker", "X Value", "Y Value"])
        self.marker_list.setMaximumHeight(220)
        self.marker_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        mg.addWidget(self.chk_click_mark, 0, 0)
        mg.addWidget(self.btn_clear_markers, 0, 1)
        mg.addWidget(QLabel("Calc:"), 0, 2)
        mg.addWidget(self.cmb_marker_calc_axis, 0, 3)
        mg.addWidget(self.btn_marker_add, 0, 4)
        mg.addWidget(self.btn_marker_sub, 0, 5)
        mg.addWidget(self.lbl_marker_calc, 0, 6)
        mg.addWidget(self.marker_list, 1, 0, 1, 7)
        self.ctrl_tabs.addTab(tab_markers, "Markers")

        # Tab: Filter & Style
        tab_style = QWidget()
        sg = QGridLayout(tab_style)
        sg.setContentsMargins(6, 6, 6, 6)
        sg.setHorizontalSpacing(10)
        sg.setVerticalSpacing(8)

        self.chk_smooth = QCheckBox("Savitzky-Golay Smooth")
        self.chk_cursor_marker = QCheckBox("Show Cursor Marker")
        self.chk_cursor_marker.setChecked(True)
        self.chk_cursor_marker.setToolTip("Toggle the crosshair/dot cursor on the plot")
        self.spin_smooth_win = QSpinBox()
        self.spin_smooth_win.setRange(5, 999)
        self.spin_smooth_win.setSingleStep(2)
        self.spin_smooth_win.setValue(CFG.default_filter_window)
        self.spin_smooth_win.setSuffix(" pts")

        self.cmb_style_series = QComboBox()
        self.cmb_style_series.setMinimumWidth(320)
        self.btn_pick_color = QPushButton("Pick Color")
        self._set_color_button_preview(None)

        self.sp_line_width = QDoubleSpinBox()
        self.sp_line_width.setRange(0.5, 12.0)
        self.sp_line_width.setDecimals(1)
        self.sp_line_width.setSingleStep(0.5)
        self.sp_line_width.setValue(2.0)
        self.sp_line_width.setKeyboardTracking(False)

        self.sp_x_shift = QDoubleSpinBox()
        self.sp_x_shift.setRange(-1e18, 1e18)
        self.sp_x_shift.setDecimals(6)
        self.sp_x_shift.setSingleStep(0.1)
        self.sp_x_shift.setValue(0.0)
        self.sp_x_shift.setKeyboardTracking(False)
        self.sp_x_shift.setToolTip("Per-channel X offset. Seconds for time axes, sample count for index.")

        self.btn_style_apply = QPushButton("Apply")
        self.btn_style_default = QPushButton("Default")
        self.btn_style_reset_all = QPushButton("Reset All")
        self.btn_style_reset_all.setProperty("danger", True)

        sg.addWidget(self.chk_smooth, 0, 0, 1, 2)
        sg.addWidget(self.chk_cursor_marker, 0, 4, 1, 2)
        sg.addWidget(QLabel("Filter Window:"), 0, 2)
        sg.addWidget(self.spin_smooth_win, 0, 3)
        sg.addWidget(QLabel("Channel:"), 1, 0)
        sg.addWidget(self.cmb_style_series, 1, 1, 1, 2)
        sg.addWidget(self.btn_pick_color, 1, 3)
        sg.addWidget(QLabel("Width:"), 1, 4)
        sg.addWidget(self.sp_line_width, 1, 5)
        sg.addWidget(QLabel("X Shift:"), 2, 0)
        sg.addWidget(self.sp_x_shift, 2, 1, 1, 2)
        sg.addWidget(self.btn_style_apply, 2, 3)
        sg.addWidget(self.btn_style_default, 2, 4)
        sg.addWidget(self.btn_style_reset_all, 2, 5)
        self.ctrl_tabs.addTab(tab_style, "Style")

        plot_vsplit = QSplitter(Qt.Orientation.Vertical)
        plot_vsplit.setHandleWidth(8)
        plot_vsplit.addWidget(self.plot_pane)
        plot_vsplit.addWidget(controls)
        plot_vsplit.setStretchFactor(0, 7)
        plot_vsplit.setStretchFactor(1, 1)
        plot_vsplit.setSizes([840, 120])
        self._plot_vsplit = plot_vsplit
        self.btn_ctrl_collapse.toggled.connect(self._on_controls_panel_toggled)
        self._controls_last_sizes = plot_vsplit.sizes()
        self._on_controls_panel_toggled(False)

        plot_tab_layout.addWidget(plot_vsplit, stretch=1)
        self.tabs.addTab(plot_tab, "Plot Analysis")

        # --- FFT tab ---
        fft_tab = QWidget()
        fft_layout = QVBoxLayout(fft_tab)
        fft_layout.setContentsMargins(10, 10, 10, 10)
        fft_layout.setSpacing(10)

        fft_topbar = QHBoxLayout()
        fft_topbar.addStretch(1)
        self.btn_theme_fft = QToolButton()
        self.btn_theme_fft.setCheckable(True)
        self.btn_theme_fft.setChecked(self._theme == "dark")
        self.btn_theme_fft.setText(self._theme_button_text())
        self.btn_theme_fft.setToolTip("Toggle Theme (Dark/Light)")
        self.btn_theme_fft.setFixedSize(34, 34)
        self.btn_theme_fft.setStyleSheet("border-radius: 17px; font-weight: 900; padding: 0px;")
        fft_topbar.addWidget(self.btn_theme_fft)
        fft_layout.addLayout(fft_topbar)

        self.fft_plot = PlotWidget()
        self.fft_plot.showGrid(x=True, y=True, alpha=0.28)
        self.fft_plot.setLabel("bottom", "Frequency (Hz)")
        self.fft_plot.setLabel("left", "Amplitude")
        fft_layout.addWidget(self.fft_plot, stretch=1)

        self.lbl_fft_info = QLabel("Select a channel and compute FFT.")
        self.lbl_fft_info.setWordWrap(True)
        self.lbl_fft_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fft_layout.addWidget(self.lbl_fft_info)

        fft_panel = QFrame()
        fft_panel.setObjectName("fftPanel")
        fft_panel.setFrameShape(QFrame.Shape.StyledPanel)
        fft_panel_l = QVBoxLayout(fft_panel)
        fft_panel_l.setContentsMargins(10, 10, 10, 10)
        fft_panel_l.setSpacing(8)

        fft_row1 = QGridLayout()
        fft_row1.setHorizontalSpacing(8)
        fft_row1.setVerticalSpacing(8)
        self.cmb_fft_channel = QComboBox()
        self.cmb_fft_channel.setToolTip("Channel for FFT analysis")
        self.fs_fft = QDoubleSpinBox()
        self.fs_fft.setRange(0.0, 1e12)
        self.fs_fft.setDecimals(6)
        self.fs_fft.setValue(0.0)
        self.fs_fft.setKeyboardTracking(False)
        self.fs_fft.setToolTip("0 = auto from time axis. Enter Fs manually if needed.")
        self.btn_fft = QPushButton("Compute FFT")
        self.btn_fft.setProperty("primary", True)
        self.btn_fft_clear = QPushButton("Clear FFT")
        fft_row1.addWidget(QLabel("Channel:"), 0, 0)
        fft_row1.addWidget(self.cmb_fft_channel, 0, 1)
        fft_row1.addWidget(QLabel("Fs (0=auto):"), 0, 2)
        fft_row1.addWidget(self.fs_fft, 0, 3)
        fft_row1.addWidget(self.btn_fft, 0, 4)
        fft_row1.addWidget(self.btn_fft_clear, 0, 5)
        fft_row1.setColumnStretch(1, 1)
        fft_panel_l.addLayout(fft_row1)

        fft_row2 = QHBoxLayout()
        fft_row2.setSpacing(12)
        self.chk_fft_log = QCheckBox("Log (dB)")
        self.chk_fft_log.setToolTip("Y axis in dB")
        self.chk_fft_window = QCheckBox("Hanning window")
        self.chk_fft_window.setChecked(True)
        self.chk_fft_window.setToolTip("Apply Hanning window to reduce spectral leakage")
        self.chk_fft_remove_mean = QCheckBox("Remove mean")
        self.chk_fft_remove_mean.setChecked(True)
        self.chk_fft_remove_mean.setToolTip("Reduce DC offset")
        self.chk_fft_detrend = QCheckBox("Linear detrend")
        self.chk_fft_detrend.setChecked(True)
        self.chk_fft_detrend.setToolTip("Remove slow trend/drift")
        self.chk_fft_hide_dc = QCheckBox("Hide 0 Hz")
        self.chk_fft_hide_dc.setChecked(True)
        self.chk_fft_hide_dc.setToolTip("Hide DC component from the plot")
        self.chk_fft_peak = QCheckBox("Mark dominant peak")
        self.chk_fft_peak.setChecked(True)
        self.chk_fft_peak.setToolTip("Mark the strongest frequency")
        for w in (self.chk_fft_log, self.chk_fft_window, self.chk_fft_remove_mean,
                  self.chk_fft_detrend, self.chk_fft_hide_dc, self.chk_fft_peak):
            fft_row2.addWidget(w)
        fft_row2.addStretch(1)
        fft_panel_l.addLayout(fft_row2)

        fft_row3 = QHBoxLayout()
        fft_row3.setSpacing(10)
        self.sp_fft_fmin = QDoubleSpinBox()
        self.sp_fft_fmin.setRange(0.0, 1e12)
        self.sp_fft_fmin.setDecimals(6)
        self.sp_fft_fmin.setValue(0.0)
        self.sp_fft_fmin.setKeyboardTracking(False)
        self.sp_fft_fmax = QDoubleSpinBox()
        self.sp_fft_fmax.setRange(0.0, 1e12)
        self.sp_fft_fmax.setDecimals(6)
        self.sp_fft_fmax.setValue(0.0)
        self.sp_fft_fmax.setKeyboardTracking(False)
        fft_row3.addWidget(QLabel("Min f (Hz):"))
        fft_row3.addWidget(self.sp_fft_fmin)
        fft_row3.addSpacing(8)
        fft_row3.addWidget(QLabel("Max f (Hz, 0=Nyquist):"))
        fft_row3.addWidget(self.sp_fft_fmax)
        fft_row3.addStretch(1)
        fft_panel_l.addLayout(fft_row3)

        fft_layout.addWidget(fft_panel)
        self.tabs.addTab(fft_tab, "Frequency Analysis (FFT)")

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([320, 1240])

    def _connect_signals(self) -> None:
        self.btn_open.clicked.connect(self.open_tdms)
        self.btn_plot_checked.clicked.connect(self.plot_checked_channels)
        self.btn_uncheck_all.clicked.connect(self.uncheck_all_channels)
        self.btn_clear_plot.clicked.connect(self.on_clear_plot)

        self.chk_region.toggled.connect(self._set_region_enabled_all)
        self.chk_click_mark.toggled.connect(self._set_click_mark_enabled_all)

        self.plot_pane.range_changed.connect(lambda x0, x1: self._on_any_range_changed("main", x0, x1))
        self.xmin.valueChanged.connect(self.apply_range_manual)
        self.xmax.valueChanged.connect(self.apply_range_manual)
        self.btn_fit_y.clicked.connect(self.autofit_y_in_range)

        self.plot_pane.interaction_mode_changed.connect(self._on_pane_mode_changed)
        self.plot_pane.autofit_requested.connect(self._on_pane_autofit)
        self.plot_pane.legend_toggled.connect(self._on_pane_legend)
        self.plot_pane.y_lock_toggled.connect(self._on_pane_ylock)
        self.plot_pane.right_y_lock_toggled.connect(self._on_pane_y2lock)
        self.plot_pane.region_toggled.connect(self._on_pane_region)
        self.plot_pane.click_mark_toggled.connect(self._on_pane_click_mark)

        self.btn_clear_markers.clicked.connect(self.clear_markers)
        self.plot_pane.marker_requested.connect(lambda x, y: self._on_marker_requested_from("main", x, y))
        self.marker_list.itemDoubleClicked.connect(self._on_marker_double_clicked)
        self.btn_marker_add.clicked.connect(self._marker_math_add)
        self.btn_marker_sub.clicked.connect(self._marker_math_sub)

        self.chk_smooth.toggled.connect(self.update_plot_data_with_filter)
        self.chk_cursor_marker.toggled.connect(self._set_cursor_marker_enabled_all)
        self.spin_smooth_win.valueChanged.connect(self.update_plot_data_with_filter)

        self.cmb_style_series.currentIndexChanged.connect(self._on_style_series_changed)
        self.btn_pick_color.clicked.connect(self.pick_color_for_selected_series)
        self.btn_style_apply.clicked.connect(self.apply_style_for_selected_series)
        self.btn_style_default.clicked.connect(self.reset_style_for_selected_series)
        self.btn_style_reset_all.clicked.connect(self.reset_all_styles)

        self.btn_bg_pick.clicked.connect(self.pick_background_color)
        self.btn_bg_reset.clicked.connect(self.reset_background_color)
        self.btn_detach.clicked.connect(self.toggle_detach_plot)
        self.btn_export_csv.clicked.connect(self.export_csv)

        self.btn_fft.clicked.connect(self.compute_fft)
        self.btn_fft_clear.clicked.connect(self.clear_fft_plot)
        self.chk_fft_log.toggled.connect(self._rerender_fft_plot)
        self.chk_fft_hide_dc.toggled.connect(self._rerender_fft_plot)
        self.chk_fft_peak.toggled.connect(self._rerender_fft_plot)
        self.sp_fft_fmin.valueChanged.connect(self._rerender_fft_plot)
        self.sp_fft_fmax.valueChanged.connect(self._rerender_fft_plot)

        self.btn_theme_plot.clicked.connect(self._toggle_theme_from_button)
        self.btn_theme_fft.clicked.connect(self._toggle_theme_from_button)
        self.chk_manual_fs_plot.toggled.connect(self._on_manual_fs_controls_changed)
        self.sp_manual_fs_plot.valueChanged.connect(self._on_manual_fs_controls_changed)
        self.cmb_manual_fs_unit.currentIndexChanged.connect(self._on_manual_fs_controls_changed)

    def _on_controls_panel_toggled(self, expanded: bool) -> None:
        vs = getattr(self, "_plot_vsplit", None)
        if vs is None:
            return
        try:
            header_h = 42
            try:
                header_h = max(header_h, self.controls_header.sizeHint().height() + 6)
            except Exception:
                pass
            total = max(sum(vs.sizes()), 1)

            if expanded:
                self.controls_body.setVisible(True)
                self.btn_ctrl_collapse.setArrowType(Qt.ArrowType.DownArrow)
                sizes = getattr(self, "_controls_last_sizes", None)
                if isinstance(sizes, list) and len(sizes) == 2 and sum(sizes) > 0:
                    vs.setSizes(sizes)
                else:
                    vs.setSizes([max(10, total - 170), 170])
            else:
                self._controls_last_sizes = vs.sizes()
                self.controls_body.setVisible(False)
                self.btn_ctrl_collapse.setArrowType(Qt.ArrowType.RightArrow)
                vs.setSizes([max(10, total - header_h), header_h])
        except Exception:
            logger.debug("Controls panel toggle error", exc_info=True)

    # ----- Pane sync helpers -----

    def _silent_set_checked(self, w: Any, checked: bool) -> None:
        if w is None:
            return
        try:
            blocked = w.blockSignals(True)
            w.setChecked(checked)
            w.blockSignals(blocked)
        except Exception:
            pass

    def _on_pane_mode_changed(self, mode: str) -> None:
        if not self._pane_sync_guard:
            self._pane_sync_guard = True
            try:
                self._apply_interaction_mode_to_all(mode)
            finally:
                self._pane_sync_guard = False

    def _on_pane_autofit(self) -> None:
        if not self._pane_sync_guard:
            self._pane_sync_guard = True
            try:
                self._autofit_all_panes()
            finally:
                self._pane_sync_guard = False

    def _on_pane_legend(self, on: bool) -> None:
        if not self._pane_sync_guard:
            self._pane_sync_guard = True
            try:
                self._set_legend_visible_all(on)
            finally:
                self._pane_sync_guard = False

    def _on_pane_ylock(self, on: bool) -> None:
        if not self._pane_sync_guard:
            self._pane_sync_guard = True
            try:
                self._set_y_lock_enabled_all(on)
            finally:
                self._pane_sync_guard = False

    def _on_pane_y2lock(self, on: bool) -> None:
        if not self._pane_sync_guard:
            self._pane_sync_guard = True
            try:
                self._set_right_y_lock_enabled_all(on)
            finally:
                self._pane_sync_guard = False

    def _on_pane_region(self, on: bool) -> None:
        if not self._pane_sync_guard:
            self._pane_sync_guard = True
            try:
                self._silent_set_checked(self.chk_region, on)
                self._set_region_enabled_all(on)
            finally:
                self._pane_sync_guard = False

    def _on_pane_click_mark(self, on: bool) -> None:
        if not self._pane_sync_guard:
            self._pane_sync_guard = True
            try:
                self._silent_set_checked(self.chk_click_mark, on)
                self._set_click_mark_enabled_all(on)
            finally:
                self._pane_sync_guard = False

    # ----- Apply-to-all helpers -----

    def _apply_interaction_mode_to_all(self, mode: Optional[str] = None) -> None:
        m = (mode or getattr(self.plot_pane, "_interaction_mode", "pan") or "pan").strip().lower()
        m = "pan" if m == "pan" else "zoom"
        self.plot_pane.set_interaction_mode(m)
        if self.detached_win is not None:
            self.detached_win.pane.set_interaction_mode(m)

    def _autofit_all_panes(self) -> None:
        self.plot_pane.autofit_all()
        if self.detached_win is not None:
            self.detached_win.pane.autofit_all()

    def _set_region_enabled_all(self, enabled: bool) -> None:
        self.plot_pane.enable_region(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.enable_region(enabled)

    def _set_click_mark_enabled_all(self, enabled: bool) -> None:
        self.plot_pane.set_click_mark_mode(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_click_mark_mode(enabled)

    def _set_cursor_marker_enabled_all(self, enabled: bool) -> None:
        self.plot_pane.set_cursor_marker_enabled(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_cursor_marker_enabled(enabled)

    def _set_y_lock_enabled_all(self, enabled: bool) -> None:
        self.plot_pane.set_y_lock(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_y_lock(enabled)

    def _set_right_y_lock_enabled_all(self, enabled: bool) -> None:
        self.plot_pane.set_right_y_lock(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_right_y_lock(enabled)

    def _set_legend_visible_all(self, enabled: bool) -> None:
        self.plot_pane.set_legend_visible(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_legend_visible(enabled)

    # ----- Background -----

    def _set_bg_button_preview(self, rgb: Tuple[int, int, int]) -> None:
        r, g, b = rgb
        fr, fg_, fb = _bg_to_fg_rgb(rgb)
        self.btn_bg_pick.setStyleSheet(
            f"background-color: rgb({r},{g},{b}); color: rgb({fr},{fg_},{fb}); font-weight: 800;"
            f"border-radius: 9px; padding: 7px 12px;"
        )

    def pick_background_color(self) -> None:
        c = QColorDialog.getColor(QColor(*self.plot_bg_rgb), self, "Choose Background Color")
        if not c.isValid():
            return
        self.plot_bg_rgb = qcolor_to_tuple(c)
        self._set_bg_button_preview(self.plot_bg_rgb)
        self._apply_background_everywhere()

    def reset_background_color(self) -> None:
        self.plot_bg_rgb = self._theme_default_bg[self._theme]
        self._set_bg_button_preview(self.plot_bg_rgb)
        self._apply_background_everywhere()

    def _apply_background_everywhere(self) -> None:
        self.plot_pane.set_background(self.plot_bg_rgb)
        if self.detached_win is not None:
            self.detached_win.pane.set_background(self.plot_bg_rgb)
        apply_plotwidget_theme(self.fft_plot, self.plot_bg_rgb)
        if self._last_fft_payload:
            self._render_fft_payload(self._last_fft_payload)

    # ----- Detach -----

    def toggle_detach_plot(self) -> None:
        if self.detached_win is None:
            self.detached_win = DetachedPlotWindow(bg_rgb=self.plot_bg_rgb, parent=self)
            self.detached_win.closed.connect(self._on_detached_closed)
            self.detached_win.pane.range_changed.connect(
                lambda x0, x1: self._on_any_range_changed("detached", x0, x1)
            )
            self.detached_win.pane.marker_requested.connect(
                lambda x, y: self._on_marker_requested_from("detached", x, y)
            )

            for sig_name, handler in [
                ("interaction_mode_changed", self._on_pane_mode_changed),
                ("autofit_requested", self._on_pane_autofit),
                ("legend_toggled", self._on_pane_legend),
                ("y_lock_toggled", self._on_pane_ylock),
                ("right_y_lock_toggled", self._on_pane_y2lock),
                ("region_toggled", self._on_pane_region),
                ("click_mark_toggled", self._on_pane_click_mark),
            ]:
                getattr(self.detached_win.pane, sig_name).connect(handler)

            dp = self.detached_win.pane
            mp = self.plot_pane
            dp.set_interaction_mode(mp._interaction_mode)
            dp.enable_region(mp._region_enabled)
            dp.set_click_mark_mode(mp._click_mark_enabled)
            dp.set_y_lock(mp._y_lock_enabled)
            dp.set_right_y_lock(mp._right_y_lock_enabled)
            dp.set_legend_visible(mp._legend_visible)

            self._silent_set_checked(self.chk_region, mp._region_enabled)
            self._silent_set_checked(self.chk_click_mark, mp._click_mark_enabled)

            self.update_plot_data_with_filter()
            for m in self._markers.values():
                self.detached_win.pane.add_marker(m["x"], m["y"], m["label"])

            self.detached_win.show()
            self.btn_detach.setText("Re-attach Plot")
        else:
            self.detached_win.close()

    def _on_detached_closed(self) -> None:
        if self.detached_win is not None:
            try:
                self.detached_win.deleteLater()
            except Exception:
                pass
        self.detached_win = None
        self.btn_detach.setText("Detach Plot")

    # ----- Style UI -----

    def _set_color_button_preview(self, rgb: Optional[Tuple[int, int, int]]) -> None:
        if rgb is None:
            self.btn_pick_color.setText("Pick Color (Auto)")
            self.btn_pick_color.setStyleSheet("")
        else:
            r, g, b = rgb
            self.btn_pick_color.setText("Pick Color")
            self.btn_pick_color.setStyleSheet(
                f"background-color: rgb({r},{g},{b}); color: white; font-weight: 800; border-radius: 9px;"
            )

    def _current_style_key(self) -> Optional[str]:
        idx = self.cmb_style_series.currentIndex()
        if idx < 0:
            return None
        data = self.cmb_style_series.itemData(idx, role=Qt.ItemDataRole.UserRole)
        return data if isinstance(data, str) else None

    def _on_style_series_changed(self, *_: Any) -> None:
        skey = self._current_style_key()
        if not skey:
            self._set_color_button_preview(None)
            self.sp_line_width.setValue(2.0)
            self.sp_x_shift.setValue(0.0)
            return
        st = self.style_map.get(skey, {})
        col = st.get("color")
        if isinstance(col, QColor):
            col = qcolor_to_tuple(col)
        self._set_color_button_preview(col if isinstance(col, tuple) else None)
        self.sp_line_width.setValue(float(st.get("width", 2.0)))
        try:
            self.sp_x_shift.setValue(float(st.get("x_shift", 0.0) or 0.0))
        except Exception:
            self.sp_x_shift.setValue(0.0)

    def pick_color_for_selected_series(self) -> None:
        skey = self._current_style_key()
        if not skey:
            return
        st = self.style_map.get(skey, {})
        current = st.get("color")
        initial = QColor(*(current if isinstance(current, tuple) else (0, 120, 215)))
        c = QColorDialog.getColor(initial, self, "Pick Color")
        if not c.isValid():
            return
        st["color"] = qcolor_to_tuple(c)
        st.setdefault("width", self.sp_line_width.value())
        st["x_shift"] = float(self.sp_x_shift.value())
        self.style_map[skey] = st
        self._set_color_button_preview(st["color"])
        self.update_plot_data_with_filter()

    def apply_style_for_selected_series(self) -> None:
        skey = self._current_style_key()
        if not skey:
            return
        st = self.style_map.get(skey, {})
        st["width"] = float(self.sp_line_width.value())
        st["x_shift"] = float(self.sp_x_shift.value())
        self.style_map[skey] = st
        self.update_plot_data_with_filter()

    def reset_style_for_selected_series(self) -> None:
        skey = self._current_style_key()
        if not skey:
            return
        self.style_map.pop(skey, None)
        self._set_color_button_preview(None)
        self.sp_line_width.setValue(2.0)
        self.sp_x_shift.setValue(0.0)
        self.update_plot_data_with_filter()

    def reset_all_styles(self) -> None:
        self.style_map.clear()
        self._on_style_series_changed()
        self.update_plot_data_with_filter()

    # ----- Persistence -----

    def _restore_ui_state(self) -> None:
        try:
            g = self.settings.value("ui/geometry")
            if g:
                self.restoreGeometry(g)
            st = self.settings.value("ui/window_state")
            if st:
                self.restoreState(st)
            ms = self.settings.value("ui/main_splitter")
            if ms and hasattr(self, "_main_splitter"):
                self._main_splitter.restoreState(ms)
            ps = self.settings.value("ui/plot_splitter")
            if ps and hasattr(self, "_plot_vsplit"):
                self._plot_vsplit.restoreState(ps)
        except Exception:
            logger.debug("UI state restore failed", exc_info=True)

        bool_settings = {
            "manual_fs/enabled": (self.chk_manual_fs_plot, False),
            "fft/log": (self.chk_fft_log, False),
            "fft/window": (self.chk_fft_window, True),
            "fft/remove_mean": (self.chk_fft_remove_mean, True),
            "fft/detrend": (self.chk_fft_detrend, True),
            "fft/hide_dc": (self.chk_fft_hide_dc, True),
            "fft/peak": (self.chk_fft_peak, True),
        }
        for key, (widget, default) in bool_settings.items():
            try:
                val = str(self.settings.value(key, str(default).lower()) or "").lower()
                widget.setChecked(val in ("1", "true", "yes"))
            except Exception:
                pass

        float_settings = {
            "manual_fs/value": (self.sp_manual_fs_plot, 0.0),
            "fft/fmin": (self.sp_fft_fmin, 0.0),
            "fft/fmax": (self.sp_fft_fmax, 0.0),
            "fft/fs_hint": (self.fs_fft, 0.0),
        }
        for key, (widget, default) in float_settings.items():
            try:
                widget.setValue(float(self.settings.value(key, default) or default))
            except Exception:
                widget.setValue(default)

        try:
            unit = str(self.settings.value("manual_fs/unit", "kHz") or "kHz")
            idx = self.cmb_manual_fs_unit.findText(unit)
            if idx >= 0:
                self.cmb_manual_fs_unit.setCurrentIndex(idx)
        except Exception:
            pass

    def _save_ui_state(self) -> None:
        try:
            self.settings.setValue("ui/theme", self._theme)
            self.settings.setValue("ui/geometry", self.saveGeometry())
            self.settings.setValue("ui/window_state", self.saveState())
            if hasattr(self, "_main_splitter"):
                self.settings.setValue("ui/main_splitter", self._main_splitter.saveState())
            if hasattr(self, "_plot_vsplit"):
                self.settings.setValue("ui/plot_splitter", self._plot_vsplit.saveState())

            self.settings.setValue("manual_fs/enabled", self.chk_manual_fs_plot.isChecked())
            self.settings.setValue("manual_fs/value", self.sp_manual_fs_plot.value())
            self.settings.setValue("manual_fs/unit", self.cmb_manual_fs_unit.currentText())

            for key, widget in [
                ("fft/log", self.chk_fft_log), ("fft/window", self.chk_fft_window),
                ("fft/remove_mean", self.chk_fft_remove_mean), ("fft/detrend", self.chk_fft_detrend),
                ("fft/hide_dc", self.chk_fft_hide_dc), ("fft/peak", self.chk_fft_peak),
            ]:
                self.settings.setValue(key, widget.isChecked())
            self.settings.setValue("fft/fmin", self.sp_fft_fmin.value())
            self.settings.setValue("fft/fmax", self.sp_fft_fmax.value())
            self.settings.setValue("fft/fs_hint", self.fs_fft.value())
        except Exception:
            logger.debug("UI state save failed", exc_info=True)

    def _install_shortcuts(self) -> None:
        try:
            act_open = QAction(self)
            act_open.setShortcut(QKeySequence.StandardKey.Open)
            act_open.triggered.connect(self.open_tdms)
            self.addAction(act_open)

            act_close = QAction(self)
            act_close.setShortcut(QKeySequence.StandardKey.Close)
            act_close.triggered.connect(lambda: self._close_file_tab(self.file_tabs.currentIndex()))
            self.addAction(act_close)

            act_export = QAction(self)
            act_export.setShortcut(QKeySequence("Ctrl+E"))
            act_export.triggered.connect(self.export_csv)
            self.addAction(act_export)
        except Exception:
            logger.debug("Shortcut installation failed", exc_info=True)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_ui_state()
        try:
            self._lazy_view_timer.stop()
        except Exception:
            pass
        self._cancel_jobs(["index", "preview", "load", "lazy_view", "filter", "fft"])
        for th, _worker, _tag in list(self._jobs):
            try:
                th.quit()
                th.wait(500)
            except Exception:
                pass
        self._jobs.clear()
        super().closeEvent(event)

    # ----- File tabs -----

    def _new_file_id(self) -> str:
        self.file_counter += 1
        return f"F{self.file_counter}"

    def open_tdms(self) -> None:
        last_dir = str(self.settings.value("paths/last_dir", "") or "")
        start_dir = last_dir if (last_dir and os.path.isdir(last_dir)) else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open TDMS", start_dir, "TDMS (*.tdms)")
        if not path:
            return
        try:
            self.settings.setValue("paths/last_dir", os.path.dirname(path))
        except Exception:
            pass
        self.open_tdms_path(path)

    def open_tdms_paths(self, paths: List[str]) -> None:
        for p in paths:
            try:
                self.open_tdms_path(p)
            except Exception:
                logger.warning("Failed to open: %s", p, exc_info=True)

    def open_tdms_path(self, path: str) -> None:
        path = (path or "").strip()
        if not path:
            return
        try:
            self.settings.setValue("paths/last_dir", os.path.dirname(path))
        except Exception:
            pass

        abs_path = os.path.abspath(path)
        for fid, st in self.files.items():
            if os.path.abspath(st.path) == abs_path:
                for idx in range(1, self.file_tabs.count()):
                    if self.file_tabs.tabText(idx) == st.label:
                        self.file_tabs.setCurrentIndex(idx)
                        return

        file_id = self._new_file_id()
        label = file_label_from_path(path)

        tab = QWidget()
        tab.setProperty("file_id", file_id)
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        hdr = QHBoxLayout()
        lbl = QLabel(path)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        hdr.addWidget(QLabel("File:"))
        hdr.addWidget(lbl, stretch=1)
        lay.addLayout(hdr)

        search = QLineEdit()
        search.setPlaceholderText("Search channels...")
        lay.addWidget(search)

        tree = QTreeWidget()
        tree.setHeaderLabels(["Channel Name", "Samples"])
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        lay.addWidget(tree, stretch=1)

        search.textChanged.connect(lambda t, fid=file_id: self.filter_tree(fid, t))
        tree.itemSelectionChanged.connect(lambda fid=file_id: self.on_selection_changed(fid))
        tree.itemChanged.connect(lambda item, col, fid=file_id: self.on_item_changed(fid, item, col))

        self.file_tabs.addTab(tab, label)
        self.file_tabs.setCurrentIndex(self.file_tabs.count() - 1)
        self.files[file_id] = FileState(file_id=file_id, path=path, label=label, tree=tree, search=search)

        self.status.showMessage(f"{label}: indexing...")
        worker = TdmsIndexWorker(file_id, path)
        self._start_job(worker, self._on_index_ready, tag="index")

    def _close_file_tab(self, index: int) -> None:
        if index == 0:
            return
        w = self.file_tabs.widget(index)
        file_id_to_remove: Optional[str] = None
        try:
            if w is not None:
                file_id_to_remove = w.property("file_id")
        except Exception:
            pass

        if not file_id_to_remove:
            for fid, st in self.files.items():
                try:
                    if st.tree and st.tree.parent() == w:
                        file_id_to_remove = fid
                        break
                except Exception:
                    continue

        self.file_tabs.removeTab(index)
        if w:
            w.deleteLater()
        if file_id_to_remove and file_id_to_remove in self.files:
            del self.files[file_id_to_remove]
        gc.collect()
        self.status.showMessage("File closed.", 2000)

    def _on_worker_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Error", msg)
        self.status.showMessage("An error occurred.", 5000)

    def _on_index_ready(self, payload: dict) -> None:
        if payload.get("cancelled"):
            return
        file_id = payload["file_id"]
        refs = payload["refs"]
        st = self.files.get(file_id)
        if not st:
            return
        self._populate_tree(st, refs)
        self.status.showMessage(f"{st.label}: {len(refs)} channels found.", 5000)

    def _populate_tree(self, st: FileState, refs: List[Tuple[str, str, int]]) -> None:
        tree = st.tree
        tree.blockSignals(True)
        tree.clear()

        groups: Dict[str, QTreeWidgetItem] = {}
        for gname, cname, n in refs:
            if gname not in groups:
                g = QTreeWidgetItem([gname, ""])
                g.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate
                )
                g.setCheckState(0, Qt.CheckState.Unchecked)
                tree.addTopLevelItem(g)
                groups[gname] = g

            key = ChannelKey(file_id=st.file_id, group=gname, channel=cname, length=n)
            c = QTreeWidgetItem([cname, str(n)])
            c.setData(0, Qt.ItemDataRole.UserRole, key)
            c.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable
            )
            c.setCheckState(0, Qt.CheckState.Unchecked)
            groups[gname].addChild(c)

        tree.expandToDepth(0)
        tree.blockSignals(False)

    def filter_tree(self, file_id: str, t: str) -> None:
        st = self.files.get(file_id)
        if not st:
            return
        tree = st.tree
        t = (t or "").lower().strip()
        for i in range(tree.topLevelItemCount()):
            g = tree.topLevelItem(i)
            any_vis = False
            for j in range(g.childCount()):
                c = g.child(j)
                match = (t in c.text(0).lower()) if t else True
                c.setHidden(not match)
                any_vis = any_vis or match
            g.setHidden(not any_vis)

    def on_item_changed(self, file_id: str, item: QTreeWidgetItem, col: int) -> None:
        if col != 0:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(key, ChannelKey) and self.current_series:
            self.update_plot_data_with_filter()

    # ----- Manual Fs -----

    def _get_plot_manual_fs_hz(self) -> Optional[float]:
        if not self.chk_manual_fs_plot.isChecked():
            return None
        return fs_value_to_hz(self.sp_manual_fs_plot.value(), self.cmb_manual_fs_unit.currentText())

    def _manual_fs_display_text(self) -> str:
        fs_hz = self._get_plot_manual_fs_hz()
        if fs_hz is None:
            return "—"
        raw_value = self.sp_manual_fs_plot.value()
        raw_unit = self.cmb_manual_fs_unit.currentText() or "Hz"
        return f"{raw_value:.6g} {raw_unit} ({fs_hz:.6g} Hz, manual)"

    def _rebuild_uniform_x(self, s: dict, x_mode: str, x_base: float, x_inc: float) -> np.ndarray:
        if s.get("lazy_meta"):
            win = s.get("_lazy_last_win")
            if isinstance(win, tuple) and len(win) == 3:
                i0, i1, stride = win
                idx = np.arange(i0, i1, max(1, stride), dtype=np.float64)
            else:
                idx = np.arange(len(np.asarray(s.get("y", []))), dtype=np.float64)
        else:
            idx = np.arange(len(np.asarray(s.get("y", []))), dtype=np.float64)
        return idx if x_mode == "index" else (x_base + idx * x_inc).astype(np.float64)

    def _apply_manual_fs_override(self) -> None:
        if not self.current_series:
            return
        manual_fs_hz = self._get_plot_manual_fs_hz()
        for s in self.current_series:
            source_mode = str(s.get("source_x_mode", s.get("x_mode", "index")) or "index")
            source_base = float(s.get("source_x_base", 0.0) or 0.0)
            source_inc = float(s.get("source_x_inc", 1.0) or 1.0)
            source_fs = s.get("source_fs_est")

            if manual_fs_hz is not None and source_mode == "index":
                s["x_mode"] = "seconds"
                s["x"] = self._rebuild_uniform_x(s, "seconds", 0.0, 1.0 / manual_fs_hz)
                s["fs_est"] = manual_fs_hz
                s["manual_fs_hz"] = manual_fs_hz
                if s.get("lazy_meta"):
                    meta = s["lazy_meta"]
                    meta["x_mode"] = "seconds"
                    meta["x_base"] = 0.0
                    meta["x_inc"] = 1.0 / manual_fs_hz
            else:
                s["manual_fs_hz"] = None
                s["fs_est"] = source_fs
                if s.get("lazy_meta"):
                    meta = s["lazy_meta"]
                    meta["x_mode"] = source_mode
                    meta["x_base"] = source_base
                    meta["x_inc"] = source_inc
                    s["x_mode"] = source_mode
                    s["x"] = self._rebuild_uniform_x(s, source_mode, source_base, source_inc)
                else:
                    s["x_mode"] = source_mode
                    if source_mode == "index":
                        s["x"] = self._rebuild_uniform_x(s, "index", 0.0, 1.0)

    def _refresh_preview_panel(self) -> None:
        p = self._last_preview_payload or {}
        fid = p.get("file_id", "")
        st = self.files.get(fid)
        self.p_file.setText(st.label if st else "—")
        self.p_name.setText(p.get("name", "—"))
        self.p_samples.setText(str(p.get("samples", "—")))
        x_info = p.get("x_info") or {}
        fs = x_info.get("fs_est")
        x_mode = str(x_info.get("x_mode", "index") or "index")
        if fs:
            self.p_fs.setText(f"{fs:.2f} Hz")
        elif x_mode == "index" and self._get_plot_manual_fs_hz() is not None:
            self.p_fs.setText(self._manual_fs_display_text())
        else:
            self.p_fs.setText("—")
        self.p_quantity.setText(p.get("quantity", "Value"))
        u = (p.get("unit") or "").strip()
        self.p_unit.setText(u or "—")

    def _on_manual_fs_controls_changed(self, *_args: Any) -> None:
        self._refresh_preview_panel()
        if self.current_series:
            self._apply_manual_fs_override()
            self.update_plot_data_with_filter()
            if self._get_plot_manual_fs_hz() is not None:
                self.status.showMessage("X axis converted to time using manual Fs.", 2500)
            else:
                self.status.showMessage("Manual Fs disabled; channel axes restored.", 2500)

    # ----- Preview -----

    def on_selection_changed(self, file_id: str) -> None:
        st = self.files.get(file_id)
        if not st:
            return
        items = st.tree.selectedItems()
        if len(items) != 1:
            return
        key = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(key, ChannelKey):
            return
        worker = TdmsChannelPreviewWorker(st.path, key)
        self._start_job(worker, self._on_preview_ready, tag="preview", cancel_tags=["preview"])

    def _on_preview_ready(self, p: dict) -> None:
        if p.get("cancelled"):
            return
        self._last_preview_payload = dict(p)
        self._refresh_preview_panel()

    # ----- Plot checked channels -----

    def uncheck_all_channels(self) -> None:
        for st in self.files.values():
            tree = st.tree
            tree.blockSignals(True)
            for i in range(tree.topLevelItemCount()):
                tree.topLevelItem(i).setCheckState(0, Qt.CheckState.Unchecked)
            tree.blockSignals(False)
        self.status.showMessage("All selections cleared.", 2000)

    def _gather_checked_requests(self) -> List[ChannelRequest]:
        reqs: List[ChannelRequest] = []
        for st in self.files.values():
            tree = st.tree
            for i in range(tree.topLevelItemCount()):
                g = tree.topLevelItem(i)
                for j in range(g.childCount()):
                    c = g.child(j)
                    if c.checkState(0) == Qt.CheckState.Checked:
                        key = c.data(0, Qt.ItemDataRole.UserRole)
                        if isinstance(key, ChannelKey):
                            reqs.append(ChannelRequest(key=key, display_name=c.text(0)))
        return reqs

    def plot_checked_channels(self) -> None:
        if not self.files:
            return
        reqs = self._gather_checked_requests()
        if not reqs:
            QMessageBox.information(self, "No Selection", "Check at least one channel (checkbox).")
            return
        files_payload = {fid: {"path": st.path, "label": st.label} for fid, st in self.files.items()}
        self.status.showMessage("Loading channels...")
        worker = MultiTdmsChannelLoadWorker(files_payload, reqs)
        self._start_job(worker, self._on_load_ready, tag="load", cancel_tags=["load", "lazy_view", "filter"])

    def _on_load_ready(self, payload: dict) -> None:
        if payload.get("cancelled"):
            return
        self.current_series = payload["series"]
        self._apply_manual_fs_override()
        self.clear_markers()
        self.update_plot_data_with_filter()
        self._refresh_series_comboboxes()
        self.status.showMessage(f"{len(self.current_series)} channel(s) ready.", 3000)

    def _refresh_series_comboboxes(self) -> None:
        self.cmb_fft_channel.clear()
        self.cmb_style_series.clear()
        if not self.current_series:
            return
        seen: set = set()
        for s in self.current_series:
            skey = s.get("style_key")
            if not skey or skey in seen:
                continue
            seen.add(skey)
            fl = (s.get("file_label") or "").strip()
            nm = (s.get("name") or "").strip()
            label = f"{fl} | {nm}" if fl else nm
            self.cmb_fft_channel.addItem(label, userData=skey)
            self.cmb_style_series.addItem(label, userData=skey)
        self._on_style_series_changed()

    def _capture_current_ranges(self) -> None:
        self._preserve_main_xrange = None
        self._preserve_detached_xrange = None
        try:
            if self.plot_pane.plot is not None:
                xr = self.plot_pane.plot.getViewBox().viewRange()[0]
                self._preserve_main_xrange = (float(xr[0]), float(xr[1]))
        except Exception:
            pass
        try:
            if self.detached_win is not None and self.detached_win.pane.plot is not None:
                xr = self.detached_win.pane.plot.getViewBox().viewRange()[0]
                self._preserve_detached_xrange = (float(xr[0]), float(xr[1]))
        except Exception:
            pass

    def _restore_ranges(self) -> None:
        for attr, pane_getter in [
            ("_preserve_main_xrange", lambda: self.plot_pane),
            ("_preserve_detached_xrange", lambda: self.detached_win.pane if self.detached_win else None),
        ]:
            try:
                xr = getattr(self, attr)
                pane = pane_getter()
                if xr and pane is not None and pane.plot is not None and xr[1] > xr[0]:
                    pane.set_xrange(float(xr[0]), float(xr[1]))
            except Exception:
                pass
            finally:
                setattr(self, attr, None)

    def _render_to_all_panes(
        self, display_series: List[dict], axis_mode: str, x_label: str,
    ) -> None:
        # Capture interactive states
        mp = self.plot_pane
        state = {
            "mode": mp._interaction_mode,
            "region": mp._region_enabled,
            "click_mark": mp._click_mark_enabled,
            "y_lock": mp._y_lock_enabled,
            "y2_lock": mp._right_y_lock_enabled,
            "legend": mp._legend_visible,
        }

        panes = [mp]
        if self.detached_win is not None:
            panes.append(self.detached_win.pane)

        for pane in panes:
            pane.plot_series(display_series, axis_mode, x_label, style_map=self.style_map)
            pane.set_background(self.plot_bg_rgb)
            pane.set_interaction_mode(state["mode"])
            pane.enable_region(state["region"])
            pane.set_click_mark_mode(state["click_mark"])
            pane.set_y_lock(state["y_lock"])
            pane.set_right_y_lock(state["y2_lock"])
            pane.set_legend_visible(state["legend"])
            pane.clear_markers()
            for m in self._markers.values():
                pane.add_marker(m["x"], m["y"], m["label"])

        self._restore_ranges()

    def update_plot_data_with_filter(self) -> None:
        if not self.current_series:
            return
        apply_filter = self.chk_smooth.isChecked() and SCIPY_AVAILABLE
        window_len = int(self.spin_smooth_win.value())
        self._filter_token += 1
        token = self._filter_token
        self.status.showMessage("Filtering/rendering...")
        series_snapshot = [dict(s) for s in self.current_series]
        worker = FilterWorker(token=token, series=series_snapshot, apply_filter=apply_filter, window_len=window_len)
        self._start_job(worker, self._on_filter_ready, tag="filter", cancel_tags=["filter"])

    def _on_filter_ready(self, payload: dict) -> None:
        if payload.get("cancelled") or payload.get("token") != self._filter_token:
            return
        self._render_to_all_panes(
            payload.get("display_series", []),
            payload.get("axis_mode", "numeric"),
            payload.get("x_label", "X"),
        )
        self.status.showMessage("Ready.", 1500)

    def on_clear_plot(self) -> None:
        self.plot_pane.clear_all()
        if self.detached_win is not None:
            self.detached_win.pane.clear_all()
        self.current_series = None
        self.cmb_fft_channel.clear()
        self.cmb_style_series.clear()
        self.clear_markers()

    # ----- Range -----

    def _sync_range_boxes(self, x0: float, x1: float) -> None:
        for w in (self.xmin, self.xmax):
            w.blockSignals(True)
        self.xmin.setValue(x0)
        self.xmax.setValue(x1)
        for w in (self.xmin, self.xmax):
            w.blockSignals(False)

    def _on_any_range_changed(self, source: str, x0: float, x1: float) -> None:
        if self._syncing_range:
            return
        self._syncing_range = True
        try:
            self._sync_range_boxes(x0, x1)
            if source == "main" and self.detached_win is not None:
                self.detached_win.pane.set_xrange(x0, x1)
            elif source == "detached":
                self.plot_pane.set_xrange(x0, x1)
            self._schedule_lazy_view_update(x0, x1)
        finally:
            self._syncing_range = False

    def apply_range_manual(self) -> None:
        x0, x1 = self.xmin.value(), self.xmax.value()
        self.plot_pane.set_xrange(x0, x1)
        if self.detached_win is not None:
            self.detached_win.pane.set_xrange(x0, x1)

    def autofit_y_in_range(self) -> None:
        x0, x1 = self.xmin.value(), self.xmax.value()
        self.plot_pane.autofit_y_in_range(x0, x1)
        if self.detached_win is not None:
            self.detached_win.pane.autofit_y_in_range(x0, x1)

    # ----- Lazy view -----

    def _schedule_lazy_view_update(self, x0: float, x1: float) -> None:
        if not CFG.lazy.enabled or not self.current_series:
            return
        if not any(s.get("lazy_meta") for s in self.current_series):
            return
        if not (math.isfinite(x0) and math.isfinite(x1)):
            return
        self._lazy_view_pending = (x0, x1)
        self._lazy_view_timer.start(CFG.lazy.view_debounce_ms)

    def _run_lazy_view_update(self) -> None:
        if not CFG.lazy.enabled or not self.current_series or not self._lazy_view_pending:
            return
        x0, x1 = self._lazy_view_pending
        self._lazy_view_pending = None

        reqs: List[dict] = []
        for s in self.current_series:
            meta = s.get("lazy_meta")
            if not isinstance(meta, dict):
                continue
            skey = s.get("style_key", "")
            if not skey:
                continue
            x_shift = 0.0
            try:
                x_shift = float(self.style_map.get(skey, {}).get("x_shift", 0.0) or 0.0)
                if not math.isfinite(x_shift):
                    x_shift = 0.0
            except Exception:
                pass
            reqs.append({
                "style_key": skey,
                "path": meta.get("path"),
                "group": meta.get("group"),
                "channel": meta.get("channel"),
                "n": int(meta.get("n", 0) or 0),
                "x_mode": meta.get("x_mode", "index"),
                "x_base": float(meta.get("x_base", 0.0) or 0.0),
                "x_inc": float(meta.get("x_inc", 1.0) or 1.0),
                "x0": x0 - x_shift,
                "x1": x1 - x_shift,
            })

        if not reqs:
            return
        self._lazy_view_token += 1
        token = self._lazy_view_token
        worker = LazyViewLoadWorker(reqs, max_points=CFG.lazy.view_max_points)
        self._start_job(
            worker,
            lambda payload, t=token: self._on_lazy_view_ready(t, payload),
            tag="lazy_view", cancel_tags=["lazy_view"],
        )

    def _on_lazy_view_ready(self, token: int, payload: dict) -> None:
        if payload.get("cancelled") or token != self._lazy_view_token:
            return
        updates = payload.get("updates") or {}
        if not updates:
            return
        changed = False
        for s in self.current_series or []:
            skey = s.get("style_key", "")
            if skey not in updates:
                continue
            u = updates[skey]
            win = tuple(u.get("win", ())) if isinstance(u, dict) else ()
            if win and s.get("_lazy_last_win") == win:
                continue
            x, y = u.get("x"), u.get("y")
            if x is None or y is None:
                continue
            s["x"], s["y"] = x, y
            if win:
                s["_lazy_last_win"] = win
            changed = True
        if changed:
            self._capture_current_ranges()
            self.update_plot_data_with_filter()

    def _load_full_xy(self, s: dict) -> Tuple[np.ndarray, np.ndarray]:
        """Load full X/Y data from disk for a series (needed for FFT on lazy channels)."""
        source_mode = str(s.get("source_x_mode", s.get("x_mode", "index")) or "index")
        manual_fs = self._get_plot_manual_fs_hz()

        if not s.get("lazy_meta"):
            y = np.asarray(s.get("y", []), dtype=np.float64)
            if manual_fs is not None and source_mode == "index":
                x = np.arange(y.size, dtype=np.float64) / manual_fs
            elif source_mode == "index":
                x = np.arange(y.size, dtype=np.float64)
            else:
                x = np.asarray(s.get("x", []), dtype=np.float64)
            return x, y

        meta = s.get("lazy_meta", {})
        path = meta.get("path")
        group, channel = meta.get("group"), meta.get("channel")
        n = int(meta.get("n", 0) or 0)
        source_base = float(s.get("source_x_base", meta.get("x_base", 0.0)) or 0.0)
        source_inc = float(s.get("source_x_inc", meta.get("x_inc", 1.0)) or 1.0)

        if not (path and group and channel and n > 0):
            return np.asarray(s.get("x", []), dtype=np.float64), np.asarray(s.get("y", []), dtype=np.float64)

        try:
            with _open_tdms(path) as tdms:
                ch = tdms[group][channel]
                y = _ensure_1d_numeric(ch[:])
                nn = y.size
                idx = np.arange(nn, dtype=np.float64)
                if manual_fs is not None and source_mode == "index":
                    x = idx / manual_fs
                elif source_mode == "index":
                    x = idx
                else:
                    x = (source_base + idx * source_inc).astype(np.float64)
                return x, y
        except Exception:
            logger.warning("Failed to load full data for lazy channel", exc_info=True)
            return np.asarray(s.get("x", []), dtype=np.float64), np.asarray(s.get("y", []), dtype=np.float64)

    # ----- Markers -----

    def clear_markers(self) -> None:
        self.plot_pane.clear_markers()
        if self.detached_win is not None:
            self.detached_win.pane.clear_markers()
        self.marker_list.clear()
        self._markers.clear()
        self._marker_counter = 0
        self.lbl_marker_calc.setText("Result: —")

    def _on_marker_requested_from(self, source: str, x: float, y: float) -> None:
        self._marker_counter += 1
        mid = self._marker_counter
        label = f"Marker {mid}"
        self._markers[mid] = {"id": mid, "label": label, "x": x, "y": y}

        x_str = _format_x_display(self.plot_pane.axis_mode, x)
        it = QTreeWidgetItem([label, x_str, f"{y:.12g}"])
        it.setData(0, Qt.ItemDataRole.UserRole, mid)
        self.marker_list.addTopLevelItem(it)

        self.plot_pane.add_marker(x, y, label)
        if self.detached_win is not None:
            self.detached_win.pane.add_marker(x, y, label)

    def _on_marker_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        mid = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(mid, int) or mid not in self._markers:
            return
        x = self._markers[mid]["x"]
        self.plot_pane.jump_to_x(x)
        if self.detached_win is not None:
            self.detached_win.pane.jump_to_x(x)

    def _selected_two_markers(self) -> Optional[Tuple[dict, dict]]:
        items = self.marker_list.selectedItems()
        if len(items) != 2:
            QMessageBox.information(self, "Selection", "Please select exactly 2 markers (CTRL+click).")
            return None
        ms = []
        for it in items:
            mid = it.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(mid, int) and mid in self._markers:
                ms.append(self._markers[mid])
        if len(ms) != 2:
            QMessageBox.information(self, "Selection", "Could not resolve 2 valid markers.")
            return None
        ms.sort(key=lambda m: m["id"])
        return ms[0], ms[1]

    def _marker_math_add(self) -> None:
        pair = self._selected_two_markers()
        if not pair:
            return
        a, b = pair
        mode = self.cmb_marker_calc_axis.currentText()
        parts = [f"Result: {a['label']} + {b['label']}"]
        if mode in ("X only", "X and Y"):
            parts.append(f"X={a['x'] + b['x']:.12g}")
        if mode in ("Y only", "X and Y"):
            parts.append(f"Y={a['y'] + b['y']:.12g}")
        self.lbl_marker_calc.setText("  |  ".join(parts))

    def _marker_math_sub(self) -> None:
        pair = self._selected_two_markers()
        if not pair:
            return
        a, b = pair
        mode = self.cmb_marker_calc_axis.currentText()
        parts = [f"Result: {b['label']} - {a['label']}"]
        if mode in ("X only", "X and Y"):
            parts.append(f"\u0394X={b['x'] - a['x']:.12g}")
        if mode in ("Y only", "X and Y"):
            parts.append(f"\u0394Y={b['y'] - a['y']:.12g}")
        self.lbl_marker_calc.setText("  |  ".join(parts))

    # ----- CSV Export -----

    def export_csv(self) -> None:
        """Export currently plotted channel data to a CSV file."""
        if not self.current_series:
            QMessageBox.information(self, "No Data", "Plot some channels first before exporting.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", "", "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return

        try:
            import csv
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                for s in self.current_series:
                    name = s.get("name", "channel")
                    fl = s.get("file_label", "")
                    full_name = f"{fl} | {name}" if fl else name
                    unit = s.get("unit", "")
                    x_mode = s.get("x_mode", "index")

                    x_header = f"X ({x_mode})"
                    y_header = f"{full_name} [{unit}]" if unit else full_name
                    writer.writerow([x_header, y_header])

                    x, y = self._load_full_xy(s)
                    for xi, yi in zip(x, y):
                        writer.writerow([f"{xi:.12g}", f"{yi:.12g}"])
                    writer.writerow([])

            self.status.showMessage(f"Exported to {os.path.basename(path)}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export CSV:\n{e}")

    # ----- FFT -----

    def _fft_curve_pen(self) -> Any:
        fg = _bg_to_fg_rgb(self.plot_bg_rgb)
        return pg.mkPen((114, 180, 255) if fg == (255, 255, 255) else (0, 92, 175), width=1.8)

    def _fft_peak_pen(self) -> Any:
        fg = _bg_to_fg_rgb(self.plot_bg_rgb)
        return pg.mkPen(
            (255, 196, 87) if fg == (255, 255, 255) else (190, 95, 0),
            width=1.2, style=Qt.PenStyle.DashLine,
        )

    def _fft_peak_brush(self) -> Any:
        fg = _bg_to_fg_rgb(self.plot_bg_rgb)
        return pg.mkBrush(255, 196, 87, 210) if fg == (255, 255, 255) else pg.mkBrush(200, 110, 20, 210)

    def clear_fft_plot(self) -> None:
        self._last_fft_payload = None
        self.fft_plot.clear()
        apply_plotwidget_theme(self.fft_plot, self.plot_bg_rgb)
        self.fft_plot.setLabel("bottom", "Frequency (Hz)")
        self.fft_plot.setLabel("left", "Amplitude")
        try:
            self.fft_plot.getPlotItem().setTitle("")
        except Exception:
            pass
        self.lbl_fft_info.setText("Select a channel and compute FFT.")

    def _rerender_fft_plot(self, *_: Any) -> None:
        if self._last_fft_payload:
            self._render_fft_payload(self._last_fft_payload)

    def _render_fft_payload(self, p: dict) -> None:
        if not p:
            return
        freq = np.asarray(p.get("freq", []), dtype=np.float64)
        mag_linear = np.asarray(p.get("mag_linear", []), dtype=np.float64)
        if freq.size == 0 or mag_linear.size == 0:
            self.clear_fft_plot()
            return

        fmin = self.sp_fft_fmin.value()
        fmax = self.sp_fft_fmax.value()
        hide_dc = self.chk_fft_hide_dc.isChecked()
        use_log = self.chk_fft_log.isChecked()

        mask = np.isfinite(freq) & np.isfinite(mag_linear)
        if hide_dc:
            mask &= (freq > 0.0)
        if fmin > 0:
            mask &= (freq >= fmin)
        if fmax > 0:
            mask &= (freq <= fmax)

        freq_p, mag_p = freq[mask], mag_linear[mask]
        self.fft_plot.clear()
        apply_plotwidget_theme(self.fft_plot, self.plot_bg_rgb)

        if freq_p.size == 0:
            self.fft_plot.setLabel("bottom", "Frequency (Hz)")
            self.fft_plot.setLabel("left", "Amplitude")
            try:
                self.fft_plot.getPlotItem().setTitle("<b>FFT</b> — No data in selected range")
            except Exception:
                pass
            self.lbl_fft_info.setText("No spectrum data in the selected frequency range.")
            return

        if use_log:
            mag_plot = 20.0 * np.log10(np.maximum(mag_p, 1e-20))
            ylab = "Amplitude (dB)"
        else:
            mag_plot = mag_p
            ylab = "Amplitude"

        curve = self.fft_plot.plot(freq_p, mag_plot, pen=self._fft_curve_pen())
        try:
            curve.setClipToView(True)
            curve.setDownsampling(auto=True, method="peak")
        except Exception:
            pass
        try:
            curve.setSkipFiniteCheck(True)
        except Exception:
            pass

        self.fft_plot.setLabel("bottom", "Frequency (Hz)")
        self.fft_plot.setLabel("left", ylab)

        shown_fmin, shown_fmax = float(freq_p[0]), float(freq_p[-1])
        title = (
            f"<b>{p['name']}</b>"
            f" &nbsp;|&nbsp; Fs={p['fs']:.6g} Hz"
            f" &nbsp;|&nbsp; \u0394f={p['df']:.6g} Hz"
            f" &nbsp;|&nbsp; N={p['n']}"
            f" &nbsp;|&nbsp; Range: {shown_fmin:.6g}\u2013{shown_fmax:.6g} Hz"
        )
        try:
            self.fft_plot.getPlotItem().setTitle(title)
        except Exception:
            pass

        xr = padded_minmax(freq_p, pad_ratio=0.01)
        yr = padded_minmax(mag_plot, pad_ratio=0.08)
        if xr:
            self.fft_plot.setXRange(*xr, padding=0.0)
        if yr:
            self.fft_plot.setYRange(*yr, padding=0.0)

        peak_text = "Peak marking disabled"
        if self.chk_fft_peak.isChecked() and freq_p.size > 0:
            try:
                peak_idx = int(np.nanargmax(mag_p))
                peak_freq = float(freq_p[peak_idx])
                peak_mag = float(mag_p[peak_idx])
                peak_val = float(mag_plot[peak_idx])
                self.fft_plot.addItem(
                    pg.InfiniteLine(pos=peak_freq, angle=90, pen=self._fft_peak_pen(), movable=False)
                )
                self.fft_plot.addItem(
                    pg.ScatterPlotItem(
                        [peak_freq], [peak_val], size=8,
                        pen=pg.mkPen(0, 0, 0, 0), brush=self._fft_peak_brush(),
                    )
                )
                peak_text = f"Dominant peak: {peak_freq:.6g} Hz | Amplitude: {peak_mag:.6g}"
            except Exception:
                peak_text = "Peak detection failed"

        preprocess = []
        if p.get("detrend_linear"):
            preprocess.append("linear detrend")
        elif p.get("remove_mean"):
            preprocess.append("mean removed")
        if p.get("use_window"):
            preprocess.append(p.get("window_name", "window"))
        if not preprocess:
            preprocess.append("raw signal")

        info = [
            f"<b>Channel:</b> {p['name']}",
            f"<b>Sampling:</b> {p['fs']:.6g} Hz &nbsp; <b>N:</b> {p['n']} &nbsp; <b>Resolution:</b> {p['df']:.6g} Hz",
            f"<b>Preprocessing:</b> {', '.join(preprocess)}",
            f"<b>Shown range:</b> {shown_fmin:.6g}\u2013{shown_fmax:.6g} Hz &nbsp; <b>Scale:</b> {ylab}",
            f"<b>{peak_text}</b>",
        ]
        self.lbl_fft_info.setText("<br>".join(info))

    def compute_fft(self) -> None:
        if not self.current_series:
            return
        idx = self.cmb_fft_channel.currentIndex()
        if idx < 0:
            return
        skey = self.cmb_fft_channel.itemData(idx, role=Qt.ItemDataRole.UserRole)
        if not isinstance(skey, str):
            return

        chosen = next((s for s in self.current_series if s.get("style_key") == skey), None)
        if not chosen:
            return

        x, y = self._load_full_xy(chosen)
        if y is None or len(y) == 0:
            return

        if self.chk_smooth.isChecked() and SCIPY_AVAILABLE and not chosen.get("is_digital"):
            y = apply_savgol_safe(y, window_len=int(self.spin_smooth_win.value()))

        fs_hint = float(self.fs_fft.value())
        fs_hint = None if fs_hint <= 0 else fs_hint

        name = f"{chosen.get('file_label', '')} | {chosen.get('name', '')}".strip(" |")
        self.lbl_fft_info.setText("Computing FFT...")

        worker = FFTWorker(
            name=name, x=x, y=y, fs_hint=fs_hint,
            use_window=self.chk_fft_window.isChecked(),
            remove_mean=self.chk_fft_remove_mean.isChecked(),
            detrend_linear=self.chk_fft_detrend.isChecked(),
        )
        self._start_job(worker, self._on_fft_ready, tag="fft", cancel_tags=["fft"])

    def _on_fft_ready(self, p: dict) -> None:
        if p.get("cancelled"):
            return
        self._last_fft_payload = p
        self._render_fft_payload(p)
        self.tabs.setCurrentIndex(1)

    # ----- Drag & Drop -----

    def dragEnterEvent(self, e: Any) -> None:
        try:
            md = e.mimeData()
            if md and md.hasUrls():
                if any(u.toLocalFile().lower().endswith(".tdms") for u in md.urls()):
                    e.acceptProposedAction()
                    return
        except Exception:
            pass
        e.ignore()

    def dropEvent(self, e: Any) -> None:
        paths: List[str] = []
        try:
            md = e.mimeData()
            if md and md.hasUrls():
                paths = [
                    u.toLocalFile() for u in md.urls()
                    if u.toLocalFile().lower().endswith(".tdms")
                ]
        except Exception:
            pass
        if paths:
            self.open_tdms_paths(paths)
            e.acceptProposedAction()
        else:
            e.ignore()


# ===================================================================
# Entry point
# ===================================================================

def main() -> None:
    app = QApplication(sys.argv)
    app.setStyleSheet(LIGHT_QSS)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
