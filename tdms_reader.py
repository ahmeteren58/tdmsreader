import sys
import math
import os
import logging
import threading

# Ensure pyqtgraph uses PyQt6 if multiple Qt bindings are installed
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt6")
import gc
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

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
    QApplication, QMainWindow, QWidget, QAbstractItemView, QFileDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem, QSplitter,
    QMessageBox, QStatusBar, QCheckBox, QGroupBox, QFormLayout, QDoubleSpinBox,
    QTabWidget, QComboBox, QButtonGroup, QSpinBox, QColorDialog,
    QGridLayout, QSizePolicy, QToolButton, QFrame
)

import pyqtgraph as pg
from pyqtgraph import PlotWidget
from pyqtgraph.graphicsItems.DateAxisItem import DateAxisItem

# OPTIMIZASYON: Grafik çiziminde donanım hızlandırma (OpenGL) kullanımı
try:
    import OpenGL

    pg.setConfigOption('useOpenGL', True)
    pg.setConfigOption('enableExperimental', True)
except ImportError:
    pass
# Çizgi yumuşatma (Antialias) performansı düşürebilir, varsayılan kapalı kalsın.
pg.setConfigOptions(antialias=False)

from nptdms import TdmsFile

# ---------------------------
# Lazy TDMS view loading (zoom/pan on-demand)
# ---------------------------
# Bu mekanizma, çok büyük kanallarda (milyonlarca örnek) başlangıçta sadece "overview"
# (seyrek örneklenmiş) veri yükler. Kullanıcı zoom/pan yaptıkça, sadece görünen X aralığına
# karşılık gelen örnekleri TDMS'ten tekrar okuyup grafiği günceller.
LAZY_ENABLE = True
LAZY_FULLLOAD_THRESHOLD = 2_000_000  # bunun altı: eski davranış (tam yükle)
LAZY_OVERVIEW_MAX_POINTS = 200_000  # ilk yüklemede en fazla bu kadar nokta
LAZY_VIEW_MAX_POINTS = 250_000  # görünür aralık için en fazla bu kadar nokta
LAZY_VIEW_DEBOUNCE_MS = 180  # pan/zoom sırasında debounce


def _xmeta_from_channel_props(props: dict) -> Tuple[str, float, float]:
    """(x_mode, x_base, x_inc) üretir.
    - x_mode: 'index' | 'seconds' | 'datetime'
    - x_base: index=0, seconds=start_offset, datetime=epoch_seconds_base
    - x_inc: index=1, seconds=dt, datetime=dt
    Not: time_track() tabanlı düzensiz X için uygun değil; o durumda lazy devre dışı bırakılır.
    """
    try:
        if props and "wf_increment" in props:
            inc = _timedelta_to_seconds(props["wf_increment"])
            start_offset = float(props.get("wf_start_offset", 0.0) or 0.0)

            if "wf_start_time" in props:
                start_epoch = _to_epoch_seconds_scalar(props["wf_start_time"])
                # Bazı dosyalarda wf_start_time "relative" gibi küçük olabilir; o durumda seconds say.
                if abs(start_epoch) < 86400 * 2:
                    return "seconds", start_offset, float(inc)
                return "datetime", float(start_epoch + start_offset), float(inc)

            return "seconds", start_offset, float(inc)
    except Exception:
        pass
    return "index", 0.0, 1.0


def _slice_indices_from_xrange(x0: float, x1: float, n: int, x_mode: str, x_base: float, x_inc: float) -> Tuple[
    int, int]:
    """X aralığını örnek indeks aralığına çevirir."""
    if n <= 0:
        return 0, 0
    a, b = (x0, x1) if x0 <= x1 else (x1, x0)
    # padding: görünür pencerenin %5'i kadar (en az 1000 sample)
    try:
        if x_mode == "index":
            i0 = int(math.floor(a))
            i1 = int(math.ceil(b))
        else:
            if not (math.isfinite(x_inc) and x_inc > 0):
                i0, i1 = 0, n
            else:
                i0 = int(math.floor((a - x_base) / x_inc))
                i1 = int(math.ceil((b - x_base) / x_inc))
    except Exception:
        i0, i1 = 0, n

    # padding
    try:
        win = max(1, i1 - i0)
        pad = max(1000, int(win * 0.05))
        i0 -= pad
        i1 += pad
    except Exception:
        pass

    if i0 < 0:
        i0 = 0
    if i1 > n:
        i1 = n
    if i1 <= i0:
        i1 = min(n, i0 + 1)
    return i0, i1


def _stride_for_window(i0: int, i1: int, max_points: int) -> int:
    win = max(1, int(i1 - i0))
    if max_points <= 0:
        return 1
    return max(1, int(math.ceil(win / float(max_points))))


# ---------------------------
# UI Theme (QSS)
# ---------------------------

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
/* ---- Plot quickbar (Diadem-like active states) ---- */
QFrame#plotQuickbar { background: rgba(255,255,255,0.70); border: 1px solid #D7DBE0; border-radius: 14px; }
QToolButton[qb="1"] { border: 1px solid #C9CED6; border-radius: 12px; padding: 0px; background: #FFFFFF; }
QToolButton[qb="1"]:hover { background: #F1F3F6; }
QToolButton[qb="1"]:pressed { background: #E9ECF0; }
QToolButton[qb="1"]:checked { background: #E7F0FF; border: 1px solid #4C8DFF; }
QToolButton[qb="1"]:checked:hover { background: #DCEAFF; }
QToolButton[qb="1"]:disabled { background: #F5F6F8; color: #9AA3B2; border-color: #D7DBE0; }

/* ---- Collapsible controls dock ---- */
QFrame#controlsDock { border: 1px solid #D7DBE0; border-radius: 12px; background: #FFFFFF; }
QFrame#controlsHeader { background: #EEF1F4; border-bottom: 1px solid #D7DBE0; border-top-left-radius: 12px; border-top-right-radius: 12px; }
QToolButton#btnCtrlCollapse { border: 1px solid #C9CED6; border-radius: 9px; padding: 6px 10px; background: #FFFFFF; font-weight: 800; text-align: left; }
QToolButton#btnCtrlCollapse:hover { background: #F1F3F6; }
QToolButton#btnCtrlCollapse:pressed { background: #E9ECF0; }

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
/* ---- Plot quickbar (Diadem-like active states) ---- */
QFrame#plotQuickbar { background: rgba(20,24,36,0.80); border: 1px solid #2A2F3A; border-radius: 14px; }
QToolButton[qb="1"] { border: 1px solid #3A4253; border-radius: 12px; padding: 0px; background: #1A2030; color: #E8EAF0; }
QToolButton[qb="1"]:hover { background: #222A3D; }
QToolButton[qb="1"]:pressed { background: #2A3550; }
QToolButton[qb="1"]:checked { background: #2A3550; border: 1px solid #6C8DFF; }
QToolButton[qb="1"]:checked:hover { background: #324068; }
QToolButton[qb="1"]:disabled { background: #161B28; color: #7F889B; border-color: #2A2F3A; }

/* ---- Collapsible controls dock ---- */
QFrame#controlsDock { border: 1px solid #2A2F3A; border-radius: 12px; background: #141824; }
QFrame#controlsHeader { background: #1D2230; border-bottom: 1px solid #2A2F3A; border-top-left-radius: 12px; border-top-right-radius: 12px; }
QToolButton#btnCtrlCollapse { border: 1px solid #2A2F3A; border-radius: 9px; padding: 6px 10px; background: #141824; color: #E8EAF0; font-weight: 800; text-align: left; }
QToolButton#btnCtrlCollapse:hover { background: #20273A; }
QToolButton#btnCtrlCollapse:pressed { background: #1B2131; }

"""

# --- Savitzky-Golay ---
try:
    from scipy.signal import savgol_filter

    SCIPY_AVAILABLE = True
except Exception:
    SCIPY_AVAILABLE = False


# ---------------------------
# Helpers
# ---------------------------

def _to_epoch_seconds_scalar(t) -> float:
    if t is None:
        raise TypeError("Zaman değeri None olamaz")
    if isinstance(t, np.datetime64):
        ns = t.astype("datetime64[ns]").astype(np.int64)
        return float(ns) / 1e9
    if isinstance(t, datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return float(t.timestamp())
    if isinstance(t, (int, float, np.integer, np.floating)):
        return float(t)
    raise TypeError(f"Desteklenmeyen zaman tipi: {type(t)}")


def _timedelta_to_seconds(dt) -> float:
    if isinstance(dt, timedelta):
        return float(dt.total_seconds())
    if isinstance(dt, np.timedelta64):
        ns = dt.astype("timedelta64[ns]").astype(np.int64)
        return float(ns) / 1e9
    if isinstance(dt, (int, float, np.integer, np.floating)):
        return float(dt)
    raise TypeError(f"Desteklenmeyen artış tipi: {type(dt)}")


def classify_and_extract_x(channel, n_samples: int) -> Tuple[np.ndarray, str]:
    try:
        tt = channel.time_track()
        if tt is not None and len(tt) == n_samples:
            if isinstance(tt, np.ndarray) and np.issubdtype(tt.dtype, np.datetime64):
                ns = tt.astype("datetime64[ns]").astype(np.int64)
                x = ns.astype(np.float64) / 1e9
                return x, "datetime"

            if isinstance(tt, np.ndarray) and np.issubdtype(tt.dtype, np.number):
                return np.array(tt, dtype=np.float64), "seconds"

            if isinstance(tt, (list, tuple)) and tt and isinstance(tt[0], datetime):
                x = np.array([_to_epoch_seconds_scalar(v) for v in tt], dtype=np.float64)
                return x, "datetime"
    except Exception:
        pass

    props = getattr(channel, "properties", {}) or {}
    if "wf_increment" in props:
        try:
            inc = _timedelta_to_seconds(props["wf_increment"])
            start_offset = float(props.get("wf_start_offset", 0.0))

            if "wf_start_time" in props:
                start_epoch = _to_epoch_seconds_scalar(props["wf_start_time"])
                if abs(start_epoch) < 86400 * 2:
                    x = start_offset + np.arange(n_samples, dtype=np.float64) * inc
                    return x, "seconds"

                x = start_epoch + start_offset + np.arange(n_samples, dtype=np.float64) * inc
                return x, "datetime"

            x = start_offset + np.arange(n_samples, dtype=np.float64) * inc
            return x, "seconds"
        except Exception:
            pass

    return np.arange(n_samples, dtype=np.float64), "index"


def robust_fs_from_x(x: np.ndarray) -> Optional[float]:
    if x is None or len(x) < 3:
        return None
    limit = 10000
    if x.size > limit:
        dx = np.diff(x[:limit].astype(np.float64))
    else:
        dx = np.diff(x.astype(np.float64))
    dx = dx[np.isfinite(dx)]
    if dx.size == 0:
        return None
    med = float(np.median(dx))
    if med <= 0:
        return None
    return 1.0 / med


def fs_value_to_hz(value: float, unit: str) -> Optional[float]:
    try:
        val = float(value)
    except Exception:
        return None
    if not math.isfinite(val) or val <= 0.0:
        return None

    u = str(unit or 'Hz').strip().lower()
    if u == 'khz':
        val *= 1e3
    return float(val)


def apply_savgol_safe(y: np.ndarray, window_len: int, polyorder_hint: int = 3) -> np.ndarray:
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


def _safe_str(v) -> str:
    s = str(v).strip()
    if not s or s.lower() in ("none", "nan"):
        return ""
    return s


def infer_quantity_from_props(props: dict) -> str:
    if not props:
        return "Değer"
    preferred_keys = [
        "quantity", "Quantity", "physical_quantity", "PhysicalQuantity",
        "measurement", "Measurement", "NI_MeasurementName", "NI_SignalName",
        "NI_UnitDescription", "unit_description", "UnitDescription",
        "NI_Description", "description", "Description", "NI_ChannelDescription",
    ]
    for k in preferred_keys:
        if k in props:
            s = _safe_str(props[k])
            if s:
                return s[:80]
    for k, v in props.items():
        lk = str(k).lower()
        if any(tok in lk for tok in ("quantity", "measure", "signal", "physical", "sensor", "unitdesc", "descr")):
            s = _safe_str(v)
            if s:
                return s[:80]
    return "Değer"


def common_unit(series_list: List[dict]) -> Optional[str]:
    units = []
    for s in series_list:
        u = (s.get("unit") or "").strip()
        if u:
            units.append(u)
    if not units:
        return None
    u0 = units[0]
    if all(u == u0 for u in units):
        return u0
    return None


def common_quantity(series_list: List[dict]) -> Optional[str]:
    qs = []
    for s in series_list:
        q = (s.get("quantity") or "").strip()
        if q:
            qs.append(q)
    if not qs:
        return None
    q0 = qs[0]
    if all(q == q0 for q in qs):
        return q0
    return None


def qcolor_to_tuple(c: QColor) -> Tuple[int, int, int]:
    return (c.red(), c.green(), c.blue())


def file_label_from_path(path: str) -> str:
    return os.path.basename(path)


def style_key_for_channel(file_id: str, group: str, channel: str) -> str:
    return f"{file_id}|{group}|{channel}"


def _bg_to_fg_rgb(bg: Tuple[int, int, int]) -> Tuple[int, int, int]:
    r, g, b = bg
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return (0, 0, 0) if lum > 140 else (255, 255, 255)


def apply_plotwidget_theme(pw: PlotWidget, bg_rgb: Tuple[int, int, int]):
    fg = _bg_to_fg_rgb(bg_rgb)
    pw.setBackground(bg_rgb)
    item = pw.getPlotItem()
    for ax in ("left", "bottom", "right"):
        axis = item.getAxis(ax)
        if axis is not None:
            axis.setPen(pg.mkPen(fg))
            axis.setTextPen(pg.mkPen(fg))


def linear_detrend_safe(y: np.ndarray) -> np.ndarray:
    yy = np.asarray(y, dtype=np.float64)
    if yy.size < 2:
        return np.nan_to_num(yy, nan=0.0, posinf=0.0, neginf=0.0)

    out = np.array(yy, dtype=np.float64, copy=True)
    finite = np.isfinite(out)
    if int(np.count_nonzero(finite)) < 2:
        return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)

    idx = np.arange(out.size, dtype=np.float64)
    x = idx[finite]
    z = out[finite]
    try:
        m, b = np.polyfit(x, z, 1)
        trend = m * idx + b
        out[finite] = out[finite] - trend[finite]
    except Exception:
        out[finite] = out[finite] - float(np.nanmean(z))

    out[~finite] = 0.0
    return out


def padded_minmax(values: np.ndarray, pad_ratio: float = 0.06) -> Optional[Tuple[float, float]]:
    arr = np.asarray(values, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None

    lo = float(np.min(arr))
    hi = float(np.max(arr))
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return None
    if hi == lo:
        pad = max(1.0, abs(lo) * 0.1, 1e-12)
        return lo - pad, hi + pad

    pad = max((hi - lo) * float(pad_ratio), 1e-12)
    return lo - pad, hi + pad


def _axis_mode_and_label_from_series(series_list: List[dict]) -> Tuple[str, str]:
    if not series_list:
        return "numeric", "X"
    all_dt = all((s.get("x_mode") == "datetime") for s in series_list)
    if all_dt:
        return "date", "Zaman (UTC)"
    any_sec = any((s.get("x_mode") == "seconds") for s in series_list)
    if any_sec:
        return "numeric", "Zaman (s)"
    return "numeric", "Index"


def _format_x_for_display(axis_mode: str, x: float) -> str:
    if axis_mode == "date":
        try:
            return datetime.fromtimestamp(float(x), tz=timezone.utc).isoformat()
        except Exception:
            return f"{x:.12g}"
    return f"{x:.12g}"


def compute_channel_stats(x: np.ndarray, y: np.ndarray,
                          x0: Optional[float] = None, x1: Optional[float] = None) -> Optional[dict]:
    """Bir kanal için temel istatistikleri hesaplar.

    İsteğe bağlı [x0, x1] aralığı verilirse sadece o pencere kullanılır.
    NaN/Inf değerler göz ardı edilir. Yeterli geçerli örnek yoksa None döner.
    """
    y = np.asarray(y, dtype=np.float64)
    if y.size == 0:
        return None

    if x0 is not None and x1 is not None and x is not None:
        xx = np.asarray(x, dtype=np.float64)
        if xx.size == y.size:
            lo, hi = (x0, x1) if x0 <= x1 else (x1, x0)
            sel = (xx >= lo) & (xx <= hi)
            if np.any(sel):
                y = y[sel]

    y = y[np.isfinite(y)]
    if y.size == 0:
        return None

    mean = float(np.mean(y))
    return {
        "count": int(y.size),
        "min": float(np.min(y)),
        "max": float(np.max(y)),
        "mean": mean,
        "std": float(np.std(y)),
        "rms": float(np.sqrt(np.mean(np.square(y)))),
        "p2p": float(np.max(y) - np.min(y)),
        "median": float(np.median(y)),
    }


def named_series_to_csv(named: List[Tuple[str, np.ndarray, np.ndarray]],
                        x_label: str = "x", delimiter: str = ",") -> str:
    """(name, x, y) üçlülerinden CSV metni üretir.

    Tüm kanallar aynı X eksenini paylaşıyorsa "wide" (geniş) biçim kullanılır:
        x, ch1, ch2, ...
    Aksi halde "long" (uzun) biçim kullanılır:
        channel, x, y
    """
    cleaned: List[Tuple[str, np.ndarray, np.ndarray]] = []
    for name, x, y in named:
        xa = np.asarray(x, dtype=np.float64).ravel()
        ya = np.asarray(y, dtype=np.float64).ravel()
        n = min(xa.size, ya.size)
        if n <= 0:
            continue
        cleaned.append((str(name), xa[:n], ya[:n]))

    if not cleaned:
        return ""

    def _fmt(v: float) -> str:
        return f"{v:.12g}"

    # Tüm kanalların X'i aynı mı? (geniş biçim için)
    x0 = cleaned[0][1]
    shareable = all(
        (xx.size == x0.size and np.allclose(xx, x0, rtol=1e-9, atol=0.0, equal_nan=True))
        for (_n, xx, _y) in cleaned
    )

    lines: List[str] = []
    if shareable:
        header = [x_label] + [name for (name, _x, _y) in cleaned]
        lines.append(delimiter.join(header))
        cols = [y for (_n, _x, y) in cleaned]
        for i in range(x0.size):
            row = [_fmt(float(x0[i]))] + [_fmt(float(c[i])) for c in cols]
            lines.append(delimiter.join(row))
    else:
        lines.append(delimiter.join(["channel", x_label, "y"]))
        for name, xx, yy in cleaned:
            for i in range(xx.size):
                lines.append(delimiter.join([name, _fmt(float(xx[i])), _fmt(float(yy[i]))]))

    return "\n".join(lines) + "\n"


def detect_digital_like(y: np.ndarray) -> Tuple[bool, Optional[Tuple[float, float]]]:
    """Digital benzeri kanalları tespit eder ve mümkünse (min,max) seviyelerini döndürür.
    Not: Performans için veriyi örnekler; bu nedenle seviyeler yaklaşık olabilir ama dijital kanallar için yeterlidir.
    """
    if y is None:
        return False, None
    y = np.asarray(y)
    if y.size == 0:
        return False, None

    max_n = 50000
    stride = max(1, int(y.size // max_n))
    yy = y[::stride]

    # NaN/inf temizle
    if np.issubdtype(yy.dtype, np.floating):
        yy = yy[np.isfinite(yy)]
        if yy.size == 0:
            return False, None

    uniq = np.unique(yy)
    if uniq.size <= 1 or uniq.size > 8:
        return False, None

    uf = uniq.astype(np.float64, copy=False)
    lo = float(np.min(uf))
    hi = float(np.max(uf))
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi == lo:
        return False, None

    # tam sayıya yakın mı?
    if np.all(np.abs(uf - np.round(uf)) < 1e-6):
        if (hi - lo) <= 10.0:
            return True, (lo, hi)

    # 2-3 seviyeli normalize dijital (0/1, 0/5 vb.) kontrolü
    if uniq.size <= 3:
        zz = (uf - lo) / (hi - lo)
        if np.all(np.abs(zz - np.round(zz)) < 1e-6):
            return True, (lo, hi)

    return False, None


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


# ---------------------------
# Workers
# ---------------------------

class TdmsIndexWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, file_id: str, path: str):
        super().__init__()
        self._cancel_event = threading.Event()
        self.file_id = file_id
        self.path = path

    def cancel(self):
        self._cancel_event.set()

    def _is_cancelled(self) -> bool:
        try:
            return bool(self._cancel_event.is_set())
        except Exception:
            return False

    def run(self):
        if self._is_cancelled():
            self.finished.emit({"file_id": self.file_id, "refs": [], "cancelled": True})
            return
        try:
            tdms = TdmsFile.open(self.path)
            refs = []
            for group in tdms.groups():
                if self._is_cancelled():
                    break
                gname = group.name
                for ch in group.channels():
                    try:
                        n = len(ch)
                    except Exception:
                        n = 0
                    refs.append((gname, ch.name, int(n)))
            try:
                tdms.close()
            except Exception:
                pass
            if self._is_cancelled():
                self.finished.emit({"file_id": self.file_id, "refs": [], "cancelled": True})
                return

            self.finished.emit({"file_id": self.file_id, "refs": refs})
        except Exception as e:
            self.failed.emit(f"TDMS indeksi okunamadı:\n{e}")


class TdmsChannelPreviewWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, path: str, key: ChannelKey):
        super().__init__()
        self._cancel_event = threading.Event()
        self.path = path
        self.key = key

    def cancel(self):
        self._cancel_event.set()

    def _is_cancelled(self) -> bool:
        try:
            return bool(self._cancel_event.is_set())
        except Exception:
            return False

    def run(self):
        if self._is_cancelled():
            self.finished.emit(
                {"file_id": self.key.file_id, "name": "", "samples": 0, "unit": "", "quantity": "", "x_info": {},
                 "preview": None, "cancelled": True})
            return
        tdms = None
        try:
            tdms = TdmsFile.open(self.path)
            ch = tdms[self.key.group][self.key.channel]
            props = getattr(ch, "properties", {}) or {}
            unit = props.get("unit_string", props.get("unit", ""))
            quantity = infer_quantity_from_props(props)

            try:
                n = int(len(ch))
            except Exception:
                n = 0

            x_mode = "index"
            fs = None
            try:
                x_mode, _x_base, x_inc = _xmeta_from_channel_props(props)
                if x_mode in ("seconds", "datetime") and math.isfinite(float(x_inc)) and float(x_inc) > 0.0:
                    fs = 1.0 / float(x_inc)
            except Exception:
                x_mode = "index"
                fs = None

            x_info = {"x_mode": x_mode, "fs_est": fs}

            self.finished.emit({
                "file_id": self.key.file_id,
                "name": f"{self.key.group}/{self.key.channel}",
                "samples": n,
                "unit": unit,
                "quantity": quantity,
                "x_info": x_info,
            })
        except Exception as e:
            self.failed.emit(f"Önizleme hatası:\n{e}")
        finally:
            try:
                if tdms is not None:
                    tdms.close()
            except Exception:
                pass


class MultiTdmsChannelLoadWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, files: Dict[str, Dict[str, str]], requests: List[ChannelRequest]):
        super().__init__()
        self._cancel_event = threading.Event()
        self.files = files
        self.requests = requests

    def cancel(self):
        self._cancel_event.set()

    def _is_cancelled(self) -> bool:
        try:
            return bool(self._cancel_event.is_set())
        except Exception:
            return False

    def run(self):
        if self._is_cancelled():
            self.finished.emit({"series": [], "axis_mode": "numeric", "x_label": "X", "cancelled": True})
            return
        try:
            req_by_file: Dict[str, List[ChannelRequest]] = {}
            for r in self.requests:
                req_by_file.setdefault(r.key.file_id, []).append(r)

            series: List[dict] = []
            x_modes: List[str] = []

            for file_id, reqs in req_by_file.items():
                if self._is_cancelled():
                    break
                if file_id not in self.files:
                    continue
                path = self.files[file_id]["path"]
                label = self.files[file_id]["label"]

                tdms = None
                try:
                    tdms = TdmsFile.open(path)
                    for req in reqs:
                        if self._is_cancelled():
                            break
                        k = req.key
                        try:
                            ch = tdms[k.group][k.channel]
                            props = getattr(ch, "properties", {}) or {}
                            unit = props.get("unit_string", props.get("unit", ""))
                            quantity = infer_quantity_from_props(props)
                            skey = style_key_for_channel(file_id, k.group, k.channel)
                            display = req.display_name.strip() or k.channel

                            # örnek sayısı
                            try:
                                n = int(len(ch))
                            except Exception:
                                n = 0
                            if n <= 0:
                                continue

                            # ---- Lazy / Full decision ----
                            use_lazy = bool(LAZY_ENABLE) and (n > int(LAZY_FULLLOAD_THRESHOLD))

                            if not use_lazy:
                                # Eski davranış: tam yükle
                                y = ch[:]
                                y = np.asarray(y)
                                if y.ndim != 1:
                                    y = np.ravel(y)
                                if not y.flags['C_CONTIGUOUS']:
                                    y = np.ascontiguousarray(y)
                                if not np.issubdtype(y.dtype, np.number):
                                    y = np.asarray(y, dtype=np.float64)
                                if y.size == 0:
                                    continue

                                x, x_mode = classify_and_extract_x(ch, len(y))
                                x = np.asarray(x, dtype=np.float64)

                                # gerekirse sırala (mevcut davranış)
                                sorted_applied = False
                                if x.size > 1:
                                    if x[-1] < x[0]:
                                        sorted_applied = True
                                    else:
                                        stride0 = max(1, int(x.size // 5000))
                                        dx = np.diff(x[::stride0]) if stride0 > 1 else np.diff(x)
                                        if dx.size and np.any(dx < 0):
                                            sorted_applied = True

                                    if sorted_applied:
                                        order = np.argsort(x)
                                        x = x[order]
                                        y = y[order]
                                        xu, idx = np.unique(x, return_index=True)
                                        if xu.size != x.size:
                                            x = xu
                                            y = y[idx]

                                x_modes.append(x_mode)
                                dig, dig_levels = detect_digital_like(y)

                                fs_est = robust_fs_from_x(x) if x_mode in ("seconds", "datetime") else None

                                series.append({
                                    "style_key": skey,
                                    "name": display,
                                    "file_label": label,
                                    "x": x,
                                    "y": y,
                                    "unit": unit,
                                    "quantity": quantity,
                                    "x_mode": x_mode,
                                    "source_x_mode": x_mode,
                                    "source_x_base": 0.0,
                                    "source_x_inc": 1.0,
                                    "source_fs_est": fs_est,
                                    "is_digital": bool(dig),
                                    "digital_levels": dig_levels,
                                    "lazy": False,
                                })
                                continue

                            # ---- Lazy: başlangıçta overview yükle ----
                            x_mode, x_base, x_inc = _xmeta_from_channel_props(props)

                            # overview stride
                            stride = _stride_for_window(0, n, int(LAZY_OVERVIEW_MAX_POINTS))
                            try:
                                y = ch[0:n:stride]
                            except Exception:
                                # bazı TDMS'lerde step slice çalışmayabilir; en azından aralıklı index toplayalım
                                idx = list(range(0, n, stride))
                                y = np.asarray([ch[i] for i in idx])
                            y = np.asarray(y)
                            if y.ndim != 1:
                                y = np.ravel(y)
                            if not y.flags['C_CONTIGUOUS']:
                                y = np.ascontiguousarray(y)
                            if not np.issubdtype(y.dtype, np.number):
                                y = np.asarray(y, dtype=np.float64)
                            if y.size == 0:
                                continue

                            # x üret
                            idx = np.arange(0, n, stride, dtype=np.float64)
                            if x_mode == "index":
                                x = idx
                            else:
                                x = (float(x_base) + idx * float(x_inc)).astype(np.float64)

                            x_modes.append(x_mode)
                            dig, dig_levels = detect_digital_like(y)

                            fs_est = (1.0 / float(x_inc)) if x_mode in ("seconds", "datetime") and float(x_inc) > 0.0 else None

                            series.append({
                                "style_key": skey,
                                "name": display,
                                "file_label": label,
                                "x": x,
                                "y": y,
                                "unit": unit,
                                "quantity": quantity,
                                "x_mode": x_mode,
                                "source_x_mode": x_mode,
                                "source_x_base": float(x_base),
                                "source_x_inc": float(x_inc),
                                "source_fs_est": fs_est,
                                "is_digital": bool(dig),
                                "digital_levels": dig_levels,
                                "lazy": True,
                                "lazy_meta": {
                                    "path": path,
                                    "group": k.group,
                                    "channel": k.channel,
                                    "n": int(n),
                                    "x_mode": x_mode,
                                    "x_base": float(x_base),
                                    "x_inc": float(x_inc),
                                },
                                "_lazy_last_win": (0, int(n), int(stride)),
                            })
                        except KeyError:
                            pass
                except Exception as e:
                    logger.warning("Hata (%s): %s", label, e)
                finally:
                    try:
                        if tdms is not None:
                            tdms.close()
                    except Exception:
                        pass

            if not series:
                self.finished.emit({"series": [], "axis_mode": "numeric", "x_label": "X"})
                return

            axis_mode = "date" if all(m == "datetime" for m in x_modes) else "numeric"
            if axis_mode == "date":
                x_label = "Zaman (UTC)"
            else:
                x_label = "Zaman (s)" if any(m == "seconds" for m in x_modes) else "Index"

            if self._is_cancelled():
                self.finished.emit({"series": [], "axis_mode": "numeric", "x_label": "X", "cancelled": True})
                return

            self.finished.emit({"series": series, "axis_mode": axis_mode, "x_label": x_label})
        except Exception as e:
            self.failed.emit(f"Kanal verisi yüklenemedi:\n{e}")


class LazyViewLoadWorker(QObject):
    """Pan/zoom ile görünür X aralığı değiştikçe sadece o aralığı TDMS'ten okuyup döndürür."""
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, requests: List[dict], max_points: int):
        super().__init__()
        self._cancel_event = threading.Event()
        self.requests = requests
        self.max_points = int(max_points)

    def cancel(self):
        self._cancel_event.set()

    def _is_cancelled(self) -> bool:
        try:
            return bool(self._cancel_event.is_set())
        except Exception:
            return False

    def run(self):
        if self._is_cancelled():
            self.finished.emit({"updates": {}, "cancelled": True})
            return
        try:
            if not self.requests:
                self.finished.emit({"updates": {}})
                return

            # group by path to avoid reopening file repeatedly
            by_path: Dict[str, List[dict]] = {}
            for r in self.requests:
                p = r.get("path")
                if not p:
                    continue
                by_path.setdefault(p, []).append(r)

            updates: Dict[str, dict] = {}

            for path, reqs in by_path.items():
                if self._is_cancelled():
                    break
                tdms = None
                try:
                    tdms = TdmsFile.open(path)
                    for r in reqs:
                        if self._is_cancelled():
                            break
                        skey = r.get("style_key", "")
                        g = r.get("group")
                        c = r.get("channel")
                        n = int(r.get("n", 0) or 0)
                        x_mode = r.get("x_mode", "index")
                        x_base = float(r.get("x_base", 0.0) or 0.0)
                        x_inc = float(r.get("x_inc", 1.0) or 1.0)
                        x0 = float(r.get("x0", 0.0))
                        x1 = float(r.get("x1", 0.0))
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
                            # fallback
                            idx = list(range(i0, i1, stride))
                            y = np.asarray([ch[i] for i in idx])

                        y = np.asarray(y)
                        if y.ndim != 1:
                            y = np.ravel(y)
                        if not y.flags['C_CONTIGUOUS']:
                            y = np.ascontiguousarray(y)
                        if not np.issubdtype(y.dtype, np.number):
                            y = np.asarray(y, dtype=np.float64)

                        idx = np.arange(i0, i1, stride, dtype=np.float64)
                        if x_mode == "index":
                            x = idx
                        else:
                            x = (float(x_base) + idx * float(x_inc)).astype(np.float64)

                        updates[skey] = {"x": x, "y": y, "win": (int(i0), int(i1), int(stride))}
                finally:
                    try:
                        if tdms is not None:
                            tdms.close()
                    except Exception:
                        pass

            if self._is_cancelled():
                self.finished.emit({"updates": {}, "cancelled": True})
                return

            self.finished.emit({"updates": updates})
        except Exception as e:
            self.failed.emit(f"Lazy view okunamadı:\n{e}")


class FilterWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, token: int, series: List[dict], apply_filter: bool, window_len: int):
        super().__init__()
        self._cancel_event = threading.Event()
        self.token = token
        self.series = series
        self.apply_filter = apply_filter
        self.window_len = window_len

    def cancel(self):
        self._cancel_event.set()

    def _is_cancelled(self) -> bool:
        try:
            return bool(self._cancel_event.is_set())
        except Exception:
            return False

    def run(self):
        if self._is_cancelled():
            self.finished.emit(
                {"token": self.token, "display_series": [], "axis_mode": "numeric", "x_label": "X", "cancelled": True})
            return
        try:
            if not self.series:
                self.finished.emit({"token": self.token, "display_series": [], "axis_mode": "numeric", "x_label": "X"})
                return

            display_series = []
            for s in self.series:
                if self._is_cancelled():
                    break
                y = s["y"]
                tag = ""
                is_dig = bool(s.get("is_digital", False))
                if self.apply_filter and not is_dig:
                    if not SCIPY_AVAILABLE:
                        tag = "(SG yok)"
                    else:
                        try:
                            y = apply_savgol_safe(y, window_len=int(self.window_len), polyorder_hint=3)
                            tag = "(SG)"
                        except Exception:
                            tag = "(SG HATA)"
                elif is_dig:
                    tag = "(Dijital)"
                ss = s.copy()
                ss["y"] = y
                ss["tag"] = tag
                display_series.append(ss)

            axis_mode, x_label = _axis_mode_and_label_from_series(display_series)
            self.finished.emit({
                "token": self.token,
                "display_series": display_series,
                "axis_mode": axis_mode,
                "x_label": x_label
            })
        except Exception as e:
            self.failed.emit(f"Filtreleme yapılamadı:\n{e}")


class FFTWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, name: str, x: np.ndarray, y: np.ndarray, fs_hint: Optional[float],
                 use_window: bool, remove_mean: bool, detrend_linear: bool):
        super().__init__()
        self._cancel_event = threading.Event()
        self.name = name
        self.x = x
        self.y = y
        self.fs_hint = fs_hint
        self.use_window = use_window
        self.remove_mean = remove_mean
        self.detrend_linear = detrend_linear

    def cancel(self):
        self._cancel_event.set()

    def _is_cancelled(self) -> bool:
        try:
            return bool(self._cancel_event.is_set())
        except Exception:
            return False

    def run(self):
        empty = {
            "name": self.name,
            "freq": np.array([], dtype=np.float64),
            "mag_linear": np.array([], dtype=np.float64),
            "cancelled": True,
        }
        if self._is_cancelled():
            self.finished.emit(empty)
            return
        try:
            if self._is_cancelled():
                self.finished.emit(empty)
                return

            y = np.array(self.y, dtype=np.float64, copy=True)
            if y.size < 8:
                raise ValueError("FFT için yeterli örnek yok.")

            if self.detrend_linear:
                y = linear_detrend_safe(y)
            elif self.remove_mean:
                y = y - np.nanmean(y)
            else:
                y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)

            fs = robust_fs_from_x(np.asarray(self.x, dtype=np.float64))
            if fs is None:
                fs = self.fs_hint
            if fs is None or fs <= 0:
                raise ValueError("Örnekleme hızı (Fs) bilinmiyor. (Fs girin ya da zaman eksenli kanal seçin)")

            window_name = "Yok"
            if self.use_window:
                win = np.hanning(y.size)
                window_name = "Hanning"
            else:
                win = np.ones(y.size, dtype=np.float64)

            # Coherent gain (amplitude) ve power normalizasyonu (PSD) için pencere katsayıları
            coherent_gain = float(np.mean(win)) if win.size else 1.0
            coherent_gain = coherent_gain if coherent_gain > 0 else 1.0
            power_norm = float(np.mean(np.square(win))) if win.size else 1.0
            power_norm = power_norm if power_norm > 0 else 1.0

            y = y * win

            y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
            if self._is_cancelled():
                self.finished.emit(empty)
                return

            Y = np.fft.rfft(y)
            n = int(y.size)
            freq = np.fft.rfftfreq(n, d=1.0 / fs)

            # Tek taraflı genlik spektrumu (amplitude)
            mag = np.abs(Y) / max(1, n)
            mag = mag / coherent_gain
            if mag.size > 2:
                mag[1:-1] *= 2.0

            # Tek taraflı güç spektral yoğunluğu (PSD), birim: <y-birimi>^2/Hz
            psd = (np.abs(Y) ** 2) / (fs * n * power_norm)
            if psd.size > 2:
                psd[1:-1] *= 2.0

            self.finished.emit({
                "name": self.name,
                "freq": freq.astype(np.float64),
                "mag_linear": mag.astype(np.float64),
                "psd_linear": psd.astype(np.float64),
                "fs": float(fs),
                "n": n,
                "df": float(fs / n),
                "window_name": window_name,
                "remove_mean": bool(self.remove_mean),
                "detrend_linear": bool(self.detrend_linear),
                "use_window": bool(self.use_window),
            })
        except Exception as e:
            self.failed.emit(f"FFT hesaplanamadı:\n{e}")


# ---------------------------
# Plot Pane (UPDATED: Lock for Digital Axis)
# ---------------------------

class PlotPane(QWidget):
    range_changed = pyqtSignal(float, float)
    marker_requested = pyqtSignal(float, float)

    # Quickbar -> external sync (MainWindow / Detached)
    interaction_mode_changed = pyqtSignal(str)  # 'pan' | 'zoom'
    region_toggled = pyqtSignal(bool)
    legend_toggled = pyqtSignal(bool)
    click_mark_toggled = pyqtSignal(bool)
    y_lock_toggled = pyqtSignal(bool)
    right_y_lock_toggled = pyqtSignal(bool)
    autofit_requested = pyqtSignal()

    def __init__(self, parent=None, bg_rgb: Tuple[int, int, int] = (255, 255, 255)):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

        self.axis_mode = "numeric"
        self.x_label = "X"

        self.plot: Optional[PlotWidget] = None
        self.legend = None
        self.region = None

        self._region_enabled = False
        self._click_mark_enabled = False
        self._markers = []
        self._last_cursor_x = float("nan")
        # Cursor marker (crosshair + optional dot)
        self._cursor_marker_enabled = True
        self._cursor_dot = None
        self._bg_rgb = bg_rgb

        self._legend_visible = True

        self._interaction_mode = "pan"  # 'pan' | 'zoom'

        # Y lock (Left)
        self._y_lock_enabled = False
        self._y_lock_left = None
        self._y_lock_guard = False

        # Y lock (Right - Digital)
        self._right_y_lock_enabled = False
        self._right_y_lock_range = None

        # Items
        self._left_items: List[pg.PlotDataItem] = []
        self._right_items: List[pg.PlotDataItem] = []

        # Right axis viewbox
        self._right_vb: Optional[pg.ViewBox] = None
        self._right_update_views = None

        # Sync
        self._right_sync_guard = False
        self._batch_plotting = False
        self._base_left_yrange: Optional[Tuple[float, float]] = None
        self._base_right_yrange: Optional[Tuple[float, float]] = None
        self._right_data_bounds = None

        self._info = QLabel("İmleç: —")
        self._info.setObjectName("cursorInfo")
        # Better placement: keep cursor info in the quickbar row (right side)
        try:
            self._info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        except Exception:
            pass
        self._info.setMinimumWidth(240)
        self._info.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        self._quickbar = self._build_quickbar()
        self._layout.addWidget(self._quickbar)

        self.build_plot(axis_mode="numeric", x_label="X")

    def set_legend_visible(self, enabled: bool):
        self._legend_visible = bool(enabled)
        if self.legend is not None:
            self.legend.setVisible(self._legend_visible)
        self._qb_set_checked("_qb_legend", self._legend_visible)

    def set_legend_position(self, position: str = "upper-right"):
        """
        Legend pozisyonunu ayarla.

        Args:
            position: 'upper-left', 'upper-right', 'lower-left', 'lower-right', 'center'
        """
        if self.legend is None:
            return

        position_map = {
            "upper-left": (0, 0),
            "upper-right": (1, 0),
            "lower-left": (0, 1),
            "lower-right": (1, 1),
            "center": (0.5, 0.5),
        }

        if position not in position_map:
            position = "upper-right"  # Default

        anchor = position_map[position]
        try:
            self.legend.anchor(itemPos=anchor, parentPos=anchor, offset=(0, 0))
        except Exception:
            pass  # Silence any anchor errors

    def set_background(self, bg_rgb: Tuple[int, int, int]):
        self._bg_rgb = bg_rgb
        if self.plot is not None:
            self._apply_theme()

    def set_y_lock(self, enabled: bool):
        self._y_lock_enabled = bool(enabled)
        if self.plot is None:
            self._qb_set_checked("_qb_ylock", self._y_lock_enabled)
            return
        vb = self.plot.getViewBox()
        if self._y_lock_enabled:
            self._y_lock_left = tuple(vb.viewRange()[1])
            self._enforce_y_lock()
        else:
            self._y_lock_left = None
        self._qb_set_checked("_qb_ylock", self._y_lock_enabled)

    def _enforce_y_lock(self):
        if not self._y_lock_enabled or self.plot is None:
            return
        if self._y_lock_guard:
            return
        self._y_lock_guard = True
        try:
            if self._y_lock_left is not None:
                y0, y1 = self._y_lock_left
                self.plot.getViewBox().setYRange(y0, y1, padding=0.0)
        finally:
            self._y_lock_guard = False

    def set_right_y_lock(self, enabled: bool):
        """Sağ (Dijital) eksen kilidini aç/kapat."""
        self._right_y_lock_enabled = bool(enabled)
        if self._right_y_lock_enabled:
            # Kilitleme anındaki aralığı kaydet
            if self._right_vb is not None:
                try:
                    self._right_y_lock_range = tuple(self._right_vb.viewRange()[1])
                    self._enforce_right_y_lock()
                except Exception:
                    self._right_y_lock_range = None
        else:
            self._right_y_lock_range = None
            # Kilidi açınca senkronizasyonu hemen tekrar çalıştır
            self._sync_right_yrange_to_left()
        self._qb_set_checked("_qb_y2lock", self._right_y_lock_enabled)

    def _enforce_right_y_lock(self):
        if not self._right_y_lock_enabled or self._right_vb is None or self._right_y_lock_range is None:
            return

        # Eğer zaten sync guard içindeysek tekrar girmeye gerek yok
        if self._right_sync_guard:
            return

        self._right_sync_guard = True
        try:
            r0, r1 = self._right_y_lock_range
            self._right_vb.setYRange(r0, r1, padding=0.0)
        except Exception:
            pass
        finally:
            self._right_sync_guard = False

    def _update_info_style(self):
        fg = _bg_to_fg_rgb(self._bg_rgb)
        is_dark_bg = (fg == (255, 255, 255))
        if is_dark_bg:
            self._info.setStyleSheet(
                "padding: 4px 8px; font-weight: 700; color: #F2F4F8;"
                "background: rgba(0,0,0,0.55); border: 1px solid rgba(255,255,255,0.18); border-radius: 9px;"
            )
        else:
            self._info.setStyleSheet(
                "padding: 4px 8px; font-weight: 700; color: #222;"
                "background: rgba(255,255,255,0.85); border: 1px solid #D7DBE0; border-radius: 9px;"
            )

    def _apply_theme(self):
        if self.plot is None:
            return
        apply_plotwidget_theme(self.plot, self._bg_rgb)

        fg = _bg_to_fg_rgb(self._bg_rgb)
        self._crosshair_v.setPen(pg.mkPen(fg, style=Qt.PenStyle.DashLine))
        self._crosshair_h.setPen(pg.mkPen(fg, style=Qt.PenStyle.DashLine))

        # Cursor dot color (match foreground)
        try:
            if getattr(self, "_cursor_dot", None) is not None:
                pen = pg.mkPen(fg, width=1.6)
                brush = pg.mkBrush(fg[0], fg[1], fg[2], 180)
                self._cursor_dot.setPen(pen)
                self._cursor_dot.setBrush(brush)
                self._cursor_dot.setVisible(bool(getattr(self, "_cursor_marker_enabled", True)))
        except Exception:
            pass

        self._apply_legend_theme()
        self._refresh_right_axis_visuals()
        self._update_info_style()

    def _legend_text_rgb(self) -> Tuple[int, int, int]:
        fg = _bg_to_fg_rgb(self._bg_rgb)
        return (235, 240, 248) if fg == (255, 255, 255) else fg

    def _apply_legend_theme(self):
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
            label = entry[1]
            try:
                if hasattr(label, "setDefaultTextColor"):
                    label.setDefaultTextColor(QColor(*legend_fg))
            except Exception:
                pass
            try:
                if hasattr(label, "setAttr"):
                    label.setAttr("color", color_name)
            except Exception:
                pass
            try:
                txt = getattr(label, "text", None)
                if txt is not None and hasattr(label, "setText"):
                    label.setText(txt, color=color_name)
            except Exception:
                pass

        try:
            self.legend.update()
        except Exception:
            pass

    def _refresh_right_axis_visuals(self):
        if self.plot is None:
            return

        try:
            p1 = self.plot.getPlotItem()
            axis = p1.getAxis("right")
        except Exception:
            axis = None

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
            except Exception:
                pass
            try:
                self.plot.hideAxis("right")
            except Exception:
                pass
            return

        try:
            axis.setStyle(showValues=True)
        except Exception:
            pass
        try:
            self.plot.showAxis("right")
        except Exception:
            pass
        try:
            axis.linkToView(self._right_vb)
        except Exception:
            pass


    def _destroy_right_axis(self):
        if self.plot is None:
            self._right_vb = None
            self._right_items = []
            self._right_update_views = None
            self._base_right_yrange = None
            self._base_left_yrange = None
            self._right_y_lock_range = None
            return

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

        self._right_vb = None
        self._right_items = []
        self._right_update_views = None

        try:
            self.plot.hideAxis("right")
        except Exception:
            pass

        self._right_data_bounds = None
        self._refresh_right_axis_visuals()

    def _ensure_right_axis(self):
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
        except Exception:
            pass

        try:
            p1.scene().addItem(self._right_vb)
        except Exception:
            self._right_vb = None
            return

        try:
            p1.getAxis("right").linkToView(self._right_vb)
        except Exception:
            pass

        self._right_vb.setXLink(p1.vb)

        def updateViews():
            if self._right_vb is None:
                return
            self._right_vb.setGeometry(p1.vb.sceneBoundingRect())
            self._right_vb.linkedViewChanged(p1.vb, self._right_vb.XAxis)

        self._right_update_views = updateViews
        updateViews()
        p1.vb.sigResized.connect(updateViews)
        self._refresh_right_axis_visuals()

    def _on_left_yrange_changed(self, *args):
        if getattr(self, "_batch_plotting", False):
            return
        self._enforce_y_lock()
        self._sync_right_yrange_to_left()

    def _sync_right_yrange_to_left(self):
        if self.plot is None or self._right_vb is None:
            return

        # YENİ: Eğer sağ eksen kilitliyse, senkronizasyon mantığını es geç ve kilidi uygula.
        if self._right_y_lock_enabled:
            self._enforce_right_y_lock()
            return

        if self._right_sync_guard:
            return
        if self._base_left_yrange is None or self._base_right_yrange is None:
            return

        vb = self.plot.getViewBox()
        if vb is None:
            return

        try:
            l_now = vb.viewRange()[1]
            l0, l1 = float(l_now[0]), float(l_now[1])
            bl0, bl1 = float(self._base_left_yrange[0]), float(self._base_left_yrange[1])
            br0, br1 = float(self._base_right_yrange[0]), float(self._base_right_yrange[1])
        except Exception:
            return

        if not all(map(math.isfinite, (l0, l1, bl0, bl1, br0, br1))):
            return

        base_span = (bl1 - bl0)
        if base_span == 0:
            return
        now_span = (l1 - l0)
        if now_span == 0:
            now_span = 1e-12

        ratio = (br1 - br0) / base_span
        r0_des = br0 + (l0 - bl0) * ratio
        r_span_des = (br1 - br0) * (now_span / base_span)
        r1_des = r0_des + r_span_des

        if self._right_data_bounds is not None:
            try:
                d0, d1 = float(self._right_data_bounds[0]), float(self._right_data_bounds[1])
                if math.isfinite(d0) and math.isfinite(d1):
                    if d1 < d0:
                        d0, d1 = d1, d0
                    dspan = d1 - d0
                    pad = 0.05 * dspan if dspan > 0 else 0.25
                    min_need = d0 - pad
                    max_need = d1 + pad

                    if math.isfinite(min_need) and math.isfinite(max_need):
                        if (not math.isfinite(r0_des)) or (not math.isfinite(r1_des)) or (r1_des <= r0_des):
                            r0_des, r1_des = min_need, max_need

                        span = r1_des - r0_des
                        need_span = max_need - min_need
                        if span < need_span:
                            c = (r0_des + r1_des) / 2.0
                            span = need_span
                            r0_des = c - span / 2.0
                            r1_des = c + span / 2.0

                        if r0_des > min_need:
                            shift = r0_des - min_need
                            r0_des -= shift
                            r1_des -= shift
                        if r1_des < max_need:
                            shift = max_need - r1_des
                            r0_des += shift
                            r1_des += shift

                        if r0_des > min_need or r1_des < max_need:
                            r0_des, r1_des = min_need, max_need
            except Exception:
                pass

        if (not math.isfinite(r0_des)) or (not math.isfinite(r1_des)) or (r1_des == r0_des):
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

    def build_plot(self, axis_mode: str, x_label: str):
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

        axis_items = {}
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
        self.set_legend_position("upper-right")  # Varsayılan pozisyon: sağ üst

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

        # Cursor dot (optional) - can be disabled from UI
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
        self.plot.setLabel("left", "Değer")

        self._layout.addWidget(self.plot, stretch=1)
        self._apply_theme()

        # Restore interactive states after rebuilding the plot
        self.set_interaction_mode(getattr(self, "_interaction_mode", "pan"))
        self.set_legend_visible(getattr(self, "_legend_visible", True))
        self.set_click_mark_mode(getattr(self, "_click_mark_enabled", False))
        self.set_cursor_marker_enabled(getattr(self, "_cursor_marker_enabled", True))
        self.set_y_lock(getattr(self, "_y_lock_enabled", False))
        if getattr(self, "_region_enabled", False):
            self.enable_region(True)

    def set_interaction_mode(self, mode: str):
        if self.plot is None:
            return
        m = (mode or "").strip().lower()
        self._interaction_mode = "pan" if m == "pan" else "zoom"
        vb = self.plot.getViewBox()
        vb.setMouseEnabled(x=True, y=True)
        vb.setMouseMode(pg.ViewBox.PanMode if m == "pan" else pg.ViewBox.RectMode)

        # Sync quickbar state (for programmatic updates)
        if m == "pan":
            self._qb_set_checked("_qb_pan", True)
        else:
            self._qb_set_checked("_qb_zoom", True)

    def set_click_mark_mode(self, enabled: bool):
        self._click_mark_enabled = bool(enabled)
        self._qb_set_checked("_qb_marker", self._click_mark_enabled)

    def set_cursor_marker_enabled(self, enabled: bool):
        """İmleç üzerindeki nokta/çapraz (cursor marker) görünürlüğü."""
        self._cursor_marker_enabled = bool(enabled)
        if self.plot is None:
            return
        try:
            self._crosshair_v.setVisible(self._cursor_marker_enabled)
            self._crosshair_h.setVisible(self._cursor_marker_enabled)
        except Exception:
            pass
        try:
            if getattr(self, "_cursor_dot", None) is not None:
                self._cursor_dot.setVisible(self._cursor_marker_enabled)
                if not self._cursor_marker_enabled:
                    self._cursor_dot.setData([np.nan], [np.nan])
        except Exception:
            pass

    def clear_all(self):
        if self.plot is None:
            return

        for it in list(self._left_items):
            try:
                self.plot.removeItem(it)
            except Exception:
                pass
        self._left_items = []

        if self._right_vb is not None:
            for it in list(self._right_items):
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

    def _compute_y_label(self, series_list: List[dict]) -> str:
        if not series_list:
            return ""
        cu = common_unit(series_list)
        cq = common_quantity(series_list)
        if cq:
            if cu:
                return f"{cq} ({cu})"
            any_unit = any((s.get("unit") or "").strip() for s in series_list)
            return f"{cq} (birimler lejantta)" if any_unit else cq
        return f"Değer ({cu})" if cu else "Değer"

    def _set_axis_titles(self, x_label: str, y_left: str, y_right: Optional[str] = None):
        pi = self.plot.getPlotItem()
        self.plot.setLabel("bottom", x_label)
        self.plot.setLabel("left", y_left if y_left else "Değer")

        if y_right:
            try:
                self.plot.setLabel("right", y_right)
            except Exception:
                pass
            title = f"<b>X:</b> {x_label} &nbsp;&nbsp; | &nbsp;&nbsp; <b>Y:</b> {y_left if y_left else '—'} &nbsp;&nbsp; | &nbsp;&nbsp; <b>Y2:</b> {y_right}"
        else:
            title = f"<b>X:</b> {x_label} &nbsp;&nbsp; | &nbsp;&nbsp; <b>Y:</b> {y_left if y_left else '—'}"
        pi.setTitle(title)

    def _plot_digital_step_compat(self, x, y, pen, viewbox: Optional[pg.ViewBox] = None):
        """Dijital kanallar için hızlı step çizimi.

        Eski yaklaşım (np.repeat ile 2N) büyük kanallarda ciddi yavaşlığa neden oluyordu.
        Burada, sadece seviye değişim noktalarını kullanarak 2*edges+2 noktaya indiriyoruz.
        """
        if x is None or y is None:
            return None

        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y)

        n = min(x.size, y.size)
        if n <= 0:
            return None
        if x.size != n:
            x = x[:n]
        if y.size != n:
            y = y[:n]

        # y sayısal değilse float'a çevir
        if not np.issubdtype(y.dtype, np.number):
            y = np.asarray(y, dtype=np.float64)

        # Dijitalde float değerler genellikle tam sayıdır; kenar tespitini stabil yapmak için yuvarla.
        if np.issubdtype(y.dtype, np.floating):
            y_cmp = np.rint(y).astype(np.float64, copy=False)
        else:
            y_cmp = y.astype(np.float64, copy=False)

        # geçersizleri at
        valid = np.isfinite(x) & np.isfinite(y_cmp)
        if not np.any(valid):
            return None
        xv = x[valid]
        yv = y_cmp[valid]
        if xv.size == 0:
            return None

        if xv.size == 1:
            x_step, y_step = xv, yv
        else:
            # seviye değişimleri
            edges = np.flatnonzero(yv[1:] != yv[:-1]) + 1
            m = int(edges.size)

            if m == 0:
                # sabit seviye: iki nokta yeterli
                x_step = np.array([xv[0], xv[-1]], dtype=np.float64)
                y_step = np.array([yv[0], yv[0]], dtype=np.float64)
            else:
                # 0 + (prev,new)*m + end  => 2m+2
                x_step = np.empty(2 * m + 2, dtype=np.float64)
                y_step = np.empty(2 * m + 2, dtype=np.float64)

                x_step[0] = xv[0]
                y_step[0] = yv[0]

                ex = xv[edges]
                y_prev = yv[edges - 1]
                y_new = yv[edges]

                x_step[1:2 * m + 1:2] = ex
                y_step[1:2 * m + 1:2] = y_prev

                x_step[2:2 * m + 1:2] = ex
                y_step[2:2 * m + 1:2] = y_new

                x_step[-1] = xv[-1]
                y_step[-1] = yv[-1]

        item = pg.PlotDataItem(pen=pen)
        try:
            item.setData(x_step, y_step, skipFiniteCheck=True)
        except TypeError:
            item.setData(x_step, y_step)

        # Zaten sıkıştırılmış veri; yine de clip-to-view faydalı
        try:
            item.setDownsampling(auto=True, mode='peak')
            item.setClipToView(True)
        except Exception:
            pass

        if viewbox is None:
            self.plot.addItem(item)
        else:
            viewbox.addItem(item)
        return item

    def plot_series(self, series_list: List[dict], axis_mode: str, x_label: str, style_map: Optional[dict] = None):
        if axis_mode != self.axis_mode:
            self.build_plot(axis_mode=axis_mode, x_label=x_label)
        else:
            self.x_label = x_label
            self.clear_all()

        if not series_list:
            self._set_axis_titles(x_label, "Değer", None)
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

        analog_series = [s for s in series_list if not bool(s.get("is_digital", False))]
        digital_series = [s for s in series_list if bool(s.get("is_digital", False))]

        y_left = self._compute_y_label(analog_series) if analog_series else (
                self._compute_y_label(series_list) or "Değer")

        if digital_series:
            self._ensure_right_axis()
            self._set_axis_titles(x_label, y_left, "Dijital")
        else:
            self._destroy_right_axis()
            self._set_axis_titles(x_label, y_left, None)

        n = len(series_list)
        i_global = 0

        right_mins = []
        right_maxs = []

        # --- analog on left ---
        for s in analog_series:
            x_raw = s["x"]
            y_raw = s["y"]
            skey = s.get("style_key") or ""
            st = (style_map or {}).get(skey, {})
            x_shift = float(st.get("x_shift", 0.0) or 0.0)
            if not math.isfinite(x_shift):
                x_shift = 0.0
            y_shift = float(st.get("y_shift", 0.0) or 0.0)
            if not math.isfinite(y_shift):
                y_shift = 0.0
            x = (np.asarray(x_raw, dtype=np.float64) + x_shift) if x_shift != 0.0 else x_raw
            y = (np.asarray(y_raw, dtype=np.float64) + y_shift) if y_shift != 0.0 else y_raw

            name = (s.get("name") or "").strip()
            unit = (s.get("unit") or "").strip()
            tag = (s.get("tag") or "").strip()
            file_label = (s.get("file_label") or "").strip()

            label = name
            if file_label:
                label = f"{file_label} | {label}"
            if unit:
                label += f" [{unit}]"
            if tag:
                label += f" {tag}"
            shift_parts = []
            if x_shift != 0.0:
                shift_parts.append(f"x{x_shift:+g}")
            if y_shift != 0.0:
                shift_parts.append(f"y{y_shift:+g}")
            if shift_parts:
                label += " (" + ", ".join(shift_parts) + ")"

            if st and st.get("color") is not None:
                color = st["color"]
            else:
                color = pg.intColor(i_global, hues=max(1, n))

            width = float(st.get("width", 2.0)) if st else 2.0
            pen = pg.mkPen(color, width=width)
            item = pg.PlotDataItem(pen=pen)

            try:
                item.setData(x, y, skipFiniteCheck=True)
            except TypeError:
                item.setData(x, y)
            try:
                item.setDownsampling(auto=True, mode='peak')
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

        # --- digital on right ---
        for s in digital_series:
            if self._right_vb is None:
                self._ensure_right_axis()
            if self._right_vb is None:
                break

            x_raw = s["x"]
            y_raw = s["y"]
            skey = s.get("style_key") or ""
            st = (style_map or {}).get(skey, {})
            x_shift = float(st.get("x_shift", 0.0) or 0.0)
            if not math.isfinite(x_shift):
                x_shift = 0.0
            y_shift = float(st.get("y_shift", 0.0) or 0.0)
            if not math.isfinite(y_shift):
                y_shift = 0.0
            x = (np.asarray(x_raw, dtype=np.float64) + x_shift) if x_shift != 0.0 else x_raw
            y = (np.asarray(y_raw, dtype=np.float64) + y_shift) if y_shift != 0.0 else y_raw

            levels = s.get("digital_levels")
            if isinstance(levels, (tuple, list)) and len(levels) == 2:
                try:
                    lo = float(levels[0]);
                    hi = float(levels[1])
                    if math.isfinite(lo) and math.isfinite(hi):
                        if hi < lo:
                            lo, hi = hi, lo
                        if hi > lo:
                            right_mins.append(lo + y_shift)
                            right_maxs.append(hi + y_shift)
                except Exception:
                    pass
            else:
                # fallback: küçük bir örnek üzerinde min/max
                try:
                    yy = np.asarray(y, dtype=np.float64)
                    if yy.size:
                        stride = max(1, int(yy.size // 50000))
                        samp = yy[::stride]
                        samp = samp[np.isfinite(samp)]
                        if samp.size:
                            right_mins.append(float(np.min(samp)))
                            right_maxs.append(float(np.max(samp)))
                except Exception:
                    pass
            name = (s.get("name") or "").strip()
            unit = (s.get("unit") or "").strip()
            tag = (s.get("tag") or "").strip()
            file_label = (s.get("file_label") or "").strip()

            label = name
            if file_label:
                label = f"{file_label} | {label}"
            if unit:
                label += f" [{unit}]"
            if tag:
                label += f" {tag}"
            label += " [DİJ]"
            shift_parts = []
            if x_shift != 0.0:
                shift_parts.append(f"x{x_shift:+g}")
            if y_shift != 0.0:
                shift_parts.append(f"y{y_shift:+g}")
            if shift_parts:
                label += " (" + ", ".join(shift_parts) + ")"

            if st and st.get("color") is not None:
                color = st["color"]
            else:
                color = pg.intColor(i_global, hues=max(1, n))

            width = float(st.get("width", 2.0)) if st else 2.0
            pen = pg.mkPen(color, width=width)

            item = self._plot_digital_step_compat(x, y, pen, viewbox=self._right_vb)
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

        # autorange
        if right_mins:
            self._right_data_bounds = (min(right_mins), max(right_maxs))
        else:
            self._right_data_bounds = None

        vb = self.plot.getViewBox()
        try:
            vb.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)
            vb.autoRange()
        except Exception:
            self.plot.enableAutoRange()

        if self._right_vb is not None:
            # Dijital eksen autoRange() büyük serilerde pahalı olabilir.
            # Eğer veri sınırlarını biliyorsak, doğrudan setYRange ile ayarla.
            if self._right_y_lock_enabled:
                self._enforce_right_y_lock()
            elif self._right_data_bounds is not None:
                try:
                    d0, d1 = float(self._right_data_bounds[0]), float(self._right_data_bounds[1])
                    if math.isfinite(d0) and math.isfinite(d1):
                        if d1 < d0:
                            d0, d1 = d1, d0
                        dspan = (d1 - d0)
                        pad = 0.05 * dspan if dspan > 0 else 0.25
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
                    try:
                        self._right_vb.autoRange()
                    except Exception:
                        pass

        try:
            self._base_left_yrange = tuple(vb.viewRange()[1])
        except Exception:
            self._base_left_yrange = None

        if self._right_vb is not None:
            try:
                self._base_right_yrange = tuple(self._right_vb.viewRange()[1])
            except Exception:
                self._base_right_yrange = None
        else:
            self._base_right_yrange = None

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

    def autofit_all(self):
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
        if self._right_vb is not None:
            try:
                self._base_right_yrange = tuple(self._right_vb.viewRange()[1])
            except Exception:
                self._base_right_yrange = None
        else:
            self._base_right_yrange = None

        try:
            vb.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=False)
        except Exception:
            pass

        self._sync_right_yrange_to_left()
        self._enforce_y_lock()
        self._enforce_right_y_lock()

    def set_xrange(self, x_min: float, x_max: float):
        if self.plot is None:
            return
        if x_max > x_min:
            self.plot.setXRange(x_min, x_max, padding=0.01)
        self._enforce_y_lock()
        self._enforce_right_y_lock()

    def enable_region(self, enabled: bool):
        self._region_enabled = bool(enabled)
        # Sync quickbar state (for programmatic updates)
        self._qb_set_checked("_qb_region", self._region_enabled)

        if self.plot is None:
            return

        if self._region_enabled:
            if self.region is None:
                vb = self.plot.getViewBox()
                xr = vb.viewRange()[0]
                mid = (xr[0] + xr[1]) / 2.0
                span = (xr[1] - xr[0]) * 0.3 if (xr[1] > xr[0]) else 1.0
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

    def region_values(self):
        if self.region is None:
            return None
        v = self.region.getRegion()
        return float(v[0]), float(v[1])

    def autofit_y_in_range(self, x_min: float, x_max: float):
        if self.plot is None or x_max <= x_min:
            return

        items = self._left_items
        if items:
            ymins, ymaxs = [], []
            for it in items:
                x, y = it.getData()
                if x is None or y is None or len(x) == 0:
                    continue
                mask = (x >= x_min) & (x <= x_max)
                if np.any(mask):
                    yy = y[mask]
                    ymins.append(float(np.nanmin(yy)))
                    ymaxs.append(float(np.nanmax(yy)))
            if ymins:
                y0, y1 = min(ymins), max(ymaxs)
                pad = (y1 - y0) * 0.05 if y1 != y0 else 1.0
                self.plot.setYRange(y0 - pad, y1 + pad, padding=0.0)
                if self._y_lock_enabled:
                    self._y_lock_left = tuple(self.plot.getViewBox().viewRange()[1])
                    self._enforce_y_lock()

        if self._right_vb is not None and self._right_items:
            ymins, ymaxs = [], []
            for it in self._right_items:
                x, y = it.getData()
                if x is None or y is None or len(x) == 0:
                    continue
                mask = (x >= x_min) & (x <= x_max)
                if np.any(mask):
                    yy = y[mask]
                    ymins.append(float(np.nanmin(yy)))
                    ymaxs.append(float(np.nanmax(yy)))
            if ymins:
                y0, y1 = min(ymins), max(ymaxs)
                pad = (y1 - y0) * 0.05 if y1 != y0 else 1.0
                self._right_vb.setYRange(y0 - pad, y1 + pad, padding=0.0)
                if self._right_y_lock_enabled:
                    self._right_y_lock_range = (y0 - pad, y1 + pad)
                    self._enforce_right_y_lock()

    def add_marker(self, x: float, y: float, label: str):
        if self.plot is None:
            return
        if x is None or not math.isfinite(float(x)):
            return
        line = pg.InfiniteLine(pos=float(x), angle=90, movable=False, pen=pg.mkPen("r", width=1.5))
        self.plot.addItem(line)
        text = pg.TextItem(text=label, anchor=(0, 1), color=(200, 0, 0))
        yr = self.plot.getViewBox().viewRange()[1]
        text.setPos(float(x), yr[1])
        self.plot.addItem(text)
        self._markers.append({"x": float(x), "y": float(y), "line": line, "text": text, "label": label})

    def clear_markers(self):
        if self.plot is None:
            self._markers = []
            return
        for m in self._markers:
            try:
                self.plot.removeItem(m["line"])
                self.plot.removeItem(m["text"])
            except Exception:
                pass
        self._markers = []

    def jump_to_x(self, x: float):
        if self.plot is None:
            return
        vb = self.plot.getViewBox()
        xr = vb.viewRange()[0]
        span = (xr[1] - xr[0]) or 1.0
        self.set_xrange(x - span * 0.25, x + span * 0.25)

    def _on_region_changed(self):
        if self.region:
            v = self.region_values()
            if v:
                self.range_changed.emit(v[0], v[1])

    def _on_xrange_changed(self, _, r):
        self.range_changed.emit(float(r[0]), float(r[1]))
        self._enforce_y_lock()
        self._enforce_right_y_lock()

    def _on_mouse_moved(self, evt):
        if self.plot is None:
            return
        pos = evt[0]
        if self.plot.sceneBoundingRect().contains(pos):
            mp = self.plot.getViewBox().mapSceneToView(pos)

            self._last_cursor_x = float("nan") if mp is None else float(mp.x())

            if getattr(self, "_cursor_marker_enabled", True):

                try:

                    self._crosshair_v.setPos(mp.x())

                    self._crosshair_h.setPos(mp.y())

                except Exception:

                    pass

                try:

                    if getattr(self, "_cursor_dot", None) is not None:
                        self._cursor_dot.setData([mp.x()], [mp.y()])

                except Exception:

                    pass

            else:

                try:

                    if getattr(self, "_cursor_dot", None) is not None:
                        self._cursor_dot.setData([np.nan], [np.nan])

                except Exception:

                    pass

            val_txt = f"{mp.y():.6g}"
            if self.axis_mode == "date":
                try:
                    t = datetime.fromtimestamp(mp.x(), tz=timezone.utc).isoformat()
                    self._info.setText(f"Zaman: {t} | Y: {val_txt}")
                except Exception:
                    self._info.setText(f"X={mp.x():.6g} | Y: {val_txt}")
            else:
                self._info.setText(f"X={mp.x():.6g} | Y: {val_txt}")

    def _on_mouse_clicked(self, event):
        if self.plot is None:
            return
        if not self._click_mark_enabled or event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.scenePos()
        vb = self.plot.getViewBox()
        if vb.sceneBoundingRect().contains(pos):
            mp = vb.mapSceneToView(pos)
            x = float(mp.x())
            y = float(mp.y())
            self.marker_requested.emit(x, y)

    # ---------------------------
    # Quickbar (icon toolbar near plot)
    # ---------------------------
    def _icon_dirs(self) -> List[str]:
        dirs: List[str] = []

        # Optional explicit icon folder (portable override)
        try:
            env_dir = (os.environ.get("TDMSREADER_ICON_DIR", "") or "").strip()
            if env_dir:
                dirs.append(env_dir)
        except Exception:
            pass

        # PyInstaller onefile temporary extraction folder
        try:
            base = getattr(sys, "_MEIPASS", None)
            if base:
                dirs.append(os.path.join(base, "icons"))
        except Exception:
            pass

        # Frozen executable folder (onedir / icons beside exe)
        try:
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            if exe_dir:
                dirs.append(os.path.join(exe_dir, "icons"))
        except Exception:
            pass

        # Script-local ./icons
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            dirs.append(os.path.join(here, "icons"))
        except Exception:
            pass

        # CWD ./icons (fallback)
        try:
            dirs.append(os.path.join(os.getcwd(), "icons"))
        except Exception:
            pass

        out: List[str] = []
        for d in dirs:
            if d and d not in out and os.path.isdir(d):
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
        b.setCheckable(bool(checkable))
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
            # fallback if icon file missing
            b.setText(tooltip[:1])
        return b

    def _qb_set_checked(self, attr: str, checked: bool):
        """Safely set quickbar check state without triggering signals."""
        b = getattr(self, attr, None)
        if b is None:
            return
        try:
            bs = b.blockSignals(True)
            b.setChecked(bool(checked))
            b.blockSignals(bs)
        except Exception:
            pass

    def _build_quickbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("plotQuickbar")
        bar.setFrameShape(QFrame.Shape.NoFrame)

        lay = QHBoxLayout(bar)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(6)

        # Mode buttons (exclusive)
        self._qb_pan = self._mk_qb_btn("pan_hand.png", "Kaydır (Pan)", checkable=True)
        self._qb_zoom = self._mk_qb_btn("zoom_plus.png", "Zoom (Rect)", checkable=True)
        self._qb_fit = self._mk_qb_btn("move_pan.png", "Oto Sığdır")
        self._qb_region = self._mk_qb_btn("region_rect.png", "Aralık Seçici", checkable=True)
        self._qb_legend = self._mk_qb_btn("legend_menu.png", "Lejant", checkable=True)
        self._qb_marker = self._mk_qb_btn("marker_pin.png", "Tıklayarak İşaretle", checkable=True)
        self._qb_ylock = self._mk_qb_btn("y_lock.png", "Y Kilidi", checkable=True)
        self._qb_y2lock = self._mk_qb_btn("y2_lock.png", "Y2 (Dijital) Kilidi", checkable=True)

        self._qb_mode_group = QButtonGroup(self)
        self._qb_mode_group.setExclusive(True)
        self._qb_mode_group.addButton(self._qb_pan)
        self._qb_mode_group.addButton(self._qb_zoom)

        # Defaults
        self._qb_pan.setChecked(True)
        self._qb_legend.setChecked(True)
        self._qb_region.setChecked(False)
        self._qb_marker.setChecked(False)
        self._qb_ylock.setChecked(False)
        self._qb_y2lock.setChecked(False)

        # Wire (local + emit for external sync)
        def _mode_set(m: str):
            self.set_interaction_mode(m)
            self.interaction_mode_changed.emit(m)

        def _do_fit():
            self.autofit_all()
            self.autofit_requested.emit()

        def _toggle_region(on: bool):
            self.enable_region(on)
            self.region_toggled.emit(bool(on))

        def _toggle_legend(on: bool):
            self.set_legend_visible(on)
            self.legend_toggled.emit(bool(on))

        def _toggle_marker(on: bool):
            self.set_click_mark_mode(on)
            self.click_mark_toggled.emit(bool(on))

        def _toggle_ylock(on: bool):
            self.set_y_lock(on)
            self.y_lock_toggled.emit(bool(on))

        def _toggle_y2lock(on: bool):
            self.set_right_y_lock(on)
            self.right_y_lock_toggled.emit(bool(on))

        self._qb_pan.toggled.connect(lambda on: on and _mode_set("pan"))
        self._qb_zoom.toggled.connect(lambda on: on and _mode_set("zoom"))
        self._qb_fit.clicked.connect(_do_fit)

        self._qb_region.toggled.connect(_toggle_region)
        self._qb_legend.toggled.connect(_toggle_legend)
        self._qb_marker.toggled.connect(_toggle_marker)
        self._qb_ylock.toggled.connect(_toggle_ylock)
        self._qb_y2lock.toggled.connect(_toggle_y2lock)

        # Layout
        for w in (self._qb_pan, self._qb_zoom, self._qb_fit, self._qb_region, self._qb_legend,
                  self._qb_marker, self._qb_ylock, self._qb_y2lock):
            lay.addWidget(w)

        lay.addStretch(1)

        # Cursor info (X/Y) on the right side of the quickbar
        try:
            self._info.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        except Exception:
            pass
        lay.addWidget(self._info, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Initial availability
        self._update_quickbar_state()
        return bar

    def _update_quickbar_state(self):
        # Y2 lock only makes sense when right axis exists
        has_right = (self._right_vb is not None and len(self._right_items) > 0)
        if hasattr(self, "_qb_y2lock"):
            self._qb_y2lock.setEnabled(bool(has_right))


# ---------------------------
# Detached plot window
# ---------------------------

class DetachedPlotWindow(QMainWindow):
    closed = pyqtSignal()

    def __init__(self, bg_rgb: Tuple[int, int, int], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Grafik (Büyük Pencere)")
        self.resize(1280, 800)
        self.pane = PlotPane(bg_rgb=bg_rgb)
        self.setCentralWidget(self.pane)

    def closeEvent(self, event: QCloseEvent):
        self.closed.emit()
        super().closeEvent(event)


# ---------------------------
# Main Window
# ---------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TDMSReader AKBGB ©")
        self.resize(1560, 920)

        self.settings = QSettings("TDMSReader", "TDMSReader")
        self._theme = str(self.settings.value("ui/theme", "light") or "light").lower()
        if self._theme not in ("light", "dark"):
            self._theme = "light"

        self._theme_guard = False
        self._theme_default_bg = {"dark": (0, 0, 0), "light": (255, 255, 255)}

        self.files: Dict[str, FileState] = {}
        self.file_counter = 0
        self.current_series: Optional[List[dict]] = None

        self.style_map: Dict[str, dict] = {}
        self._jobs: List[Tuple[QThread, QObject, str]] = []

        self.plot_bg_rgb: Tuple[int, int, int] = self._theme_default_bg[self._theme]
        self.detached_win: Optional[DetachedPlotWindow] = None
        self._syncing_range = False
        self._pane_sync_guard = False  # prevents feedback loops between panes/UI

        self._marker_counter = 0
        self._markers: Dict[int, dict] = {}
        self._last_preview_payload: Optional[dict] = None
        self._last_fft_payload: Optional[dict] = None

        self._filter_token = 0

        # Drag & Drop ile TDMS açma
        self.setAcceptDrops(True)

        # Lazy view (zoom/pan) güncellemesi
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

        self.status.showMessage("Sol üstteki 'Aç' sekmesinden TDMS dosyası ekleyin.")

        if not SCIPY_AVAILABLE:
            QMessageBox.warning(
                self, "Kütüphane Eksik",
                "Scipy yüklü değil. Savitzky-Golay filtresi çalışmayacak.\n"
                "Yüklemek için: pip install scipy"
            )
            self.chk_smooth.setEnabled(False)

        self._apply_background_everywhere()

    def _cancel_jobs(self, tags: List[str]):
        """Best-effort: aynı tip eski işleri iptal et (thread'i öldürmez; worker erken çıkmaya çalışır)."""
        if not tags:
            return
        tagset = set([t for t in tags if t])
        if not tagset:
            return

        for th, worker, tg in list(self._jobs):
            if tg not in tagset:
                continue
            try:
                if hasattr(worker, "cancel"):
                    worker.cancel()
            except Exception as e:
                logger.debug("Worker cancel hata (%s): %s", tg, e)
            try:
                th.requestInterruption()
            except Exception:
                pass

    def _start_job(self, worker: QObject, on_finished, on_failed=None, *, tag: str = "",
                   cancel_tags: Optional[List[str]] = None):
        if cancel_tags:
            self._cancel_jobs(cancel_tags)

        th = QThread(self)
        worker.moveToThread(th)

        def _cleanup():
            th.quit()

        def _remove_job():
            try:
                for i, (t, w, tg) in enumerate(self._jobs):
                    if t is th:
                        del self._jobs[i]
                        break
            except Exception as e:
                logger.debug("Job cleanup hata: %s", e)

        th.started.connect(worker.run)
        worker.finished.connect(on_finished)
        if on_failed is None:
            worker.failed.connect(self._on_worker_failed)
        else:
            worker.failed.connect(on_failed)

        worker.finished.connect(_cleanup)
        worker.failed.connect(_cleanup)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        th.finished.connect(th.deleteLater)
        th.finished.connect(_remove_job)

        self._jobs.append((th, worker, tag))
        th.start()

    # ---------------- Theme ----------------
    def _theme_button_text(self) -> str:
        return "🌙" if self._theme == "dark" else "☀"

    def set_theme(self, theme: str):
        theme = (theme or "").strip().lower()
        if theme not in ("dark", "light"):
            return
        if self._theme_guard:
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
                self.status.showMessage(f"Tema: {'Koyu' if self._theme == 'dark' else 'Açık'}", 2000)
        finally:
            self._theme_guard = False

    def _toggle_theme_from_button(self):
        sender = self.sender()
        want_dark = True
        if hasattr(sender, "isChecked"):
            want_dark = bool(sender.isChecked())
        self.set_theme("dark" if want_dark else "light")

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter = splitter
        root_layout.addWidget(splitter, stretch=1)
        splitter.setHandleWidth(8)

        # LEFT
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

        self.btn_open = QPushButton("TDMS Aç...")
        self.btn_open.setProperty("primary", True)

        self.lbl_hint = QLabel("Bir veya daha fazla TDMS açılabilir.")
        self.lbl_hint.setWordWrap(True)

        open_l.addWidget(self.btn_open)
        open_l.addWidget(self.lbl_hint)
        open_l.addStretch(1)
        self.file_tabs.addTab(open_tab, "Aç")

        left_layout.addWidget(self.file_tabs, stretch=1)

        plot_row = QHBoxLayout()
        plot_row.setSpacing(8)

        self.btn_plot_checked = QPushButton("İşaretli Kanalları Çizdir")
        self.btn_plot_checked.setProperty("primary", True)

        self.btn_uncheck_all = QPushButton("Kanal Seçimlerini Kaldır")
        self.btn_clear_plot = QPushButton("Grafiği Temizle")
        self.btn_clear_plot.setProperty("danger", True)

        plot_row.addWidget(self.btn_plot_checked)
        plot_row.addWidget(self.btn_uncheck_all)
        plot_row.addWidget(self.btn_clear_plot)
        left_layout.addLayout(plot_row)

        self.preview_box = QGroupBox("Kanal Özellikleri")
        pf = QFormLayout(self.preview_box)
        pf.setVerticalSpacing(8)
        pf.setHorizontalSpacing(10)

        self.p_file, self.p_name = QLabel("—"), QLabel("—")
        self.p_samples, self.p_fs = QLabel("—"), QLabel("—")
        self.p_unit, self.p_quantity = QLabel("—"), QLabel("—")
        self.chk_manual_fs_plot = QCheckBox("Fs yoksa manuel kullan")
        self.chk_manual_fs_plot.setToolTip("TDMS kanalında zaman/Fs bilgisi yoksa index tabanlı X eksenini zamana çevirir.")
        self.sp_manual_fs_plot = QDoubleSpinBox()
        self.sp_manual_fs_plot.setRange(0.0, 1e12)
        self.sp_manual_fs_plot.setDecimals(6)
        self.sp_manual_fs_plot.setValue(0.0)
        self.sp_manual_fs_plot.setKeyboardTracking(False)
        self.sp_manual_fs_plot.setToolTip("Sensör örnekleme frekansı. Örn. 25 kHz için 25 girip birimi kHz seçin.")
        self.cmb_manual_fs_unit = QComboBox()
        self.cmb_manual_fs_unit.addItems(["Hz", "kHz"])
        self.cmb_manual_fs_unit.setCurrentText("kHz")
        self.cmb_manual_fs_unit.setToolTip("Girilen manuel Fs değerinin birimi.")
        manual_fs_row = QWidget()
        manual_fs_row_l = QHBoxLayout(manual_fs_row)
        manual_fs_row_l.setContentsMargins(0, 0, 0, 0)
        manual_fs_row_l.setSpacing(6)
        manual_fs_row_l.addWidget(self.chk_manual_fs_plot)
        manual_fs_row_l.addWidget(self.sp_manual_fs_plot, 1)
        manual_fs_row_l.addWidget(self.cmb_manual_fs_unit)
        pf.addRow("Dosya:", self.p_file)
        pf.addRow("İsim:", self.p_name)
        pf.addRow("Örnek:", self.p_samples)
        pf.addRow("Fs:", self.p_fs)
        pf.addRow("Manuel Fs:", manual_fs_row)
        pf.addRow("Büyüklük:", self.p_quantity)
        pf.addRow("Birim:", self.p_unit)
        left_layout.addWidget(self.preview_box)

        splitter.addWidget(left)

        # RIGHT
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, stretch=1)

        # plot tab
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
        self.btn_theme_plot.setToolTip("Tema Değiştir (Koyu/Açık)")
        self.btn_theme_plot.setFixedSize(34, 34)
        self.btn_theme_plot.setStyleSheet("border-radius: 17px; font-weight: 900; padding: 0px;")
        plot_topbar.addWidget(self.btn_theme_plot)
        plot_tab_layout.addLayout(plot_topbar)

        self.plot_pane = PlotPane(bg_rgb=self.plot_bg_rgb)

        controls = QFrame()
        self.controls_panel = controls

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

        self.btn_ctrl_collapse.setText("Kontroller")

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

        self.controls_body.setVisible(False)  # default: collapsed

        self.ctrl_tabs = QTabWidget()

        self.ctrl_tabs.setDocumentMode(True)

        body_layout.addWidget(self.ctrl_tabs)

        # --- TAB 1: Aralık Seçici ---
        tab_range = QWidget()
        rg = QGridLayout(tab_range)
        rg.setContentsMargins(6, 6, 6, 6)
        rg.setHorizontalSpacing(10)
        rg.setVerticalSpacing(8)

        self.chk_region = QCheckBox("Aralık Seçici")
        self.xmin = QDoubleSpinBox()
        self.xmax = QDoubleSpinBox()
        for w in (self.xmin, self.xmax):
            w.setRange(-1e18, 1e18)
            w.setDecimals(6)
            w.setKeyboardTracking(False)

        self.btn_fit_y = QPushButton("Y Eksenini Sığdır")

        # Plot window actions
        self.btn_bg_pick = QPushButton("Arkaplan Rengi")
        self.btn_bg_reset = QPushButton("Varsayılan Arkaplan")
        self.btn_detach = QPushButton("Grafiği Ayır")
        self._set_bg_button_preview(self.plot_bg_rgb)

        # Export / analysis actions
        self.btn_export_csv = QPushButton("Veriyi CSV")
        self.btn_export_csv.setToolTip("Çizilen kanalları (aralık seçiliyse o aralığı) CSV olarak kaydet (Ctrl+E)")
        self.btn_export_png = QPushButton("Grafik PNG")
        self.btn_export_png.setToolTip("Grafiği PNG görüntü olarak kaydet")
        self.btn_stats = QPushButton("İstatistik")
        self.btn_stats.setToolTip("Çizilen kanallar için istatistikleri göster (Ctrl+I)")

        rg.addWidget(self.chk_region, 0, 0)
        rg.addWidget(QLabel("Min:"), 0, 1)
        rg.addWidget(self.xmin, 0, 2)
        rg.addWidget(QLabel("Max:"), 0, 3)
        rg.addWidget(self.xmax, 0, 4)
        rg.addWidget(self.btn_fit_y, 0, 5)

        rg.addWidget(self.btn_bg_pick, 1, 0, 1, 2)
        rg.addWidget(self.btn_bg_reset, 1, 2, 1, 2)
        rg.addWidget(self.btn_detach, 1, 4, 1, 2)

        rg.addWidget(self.btn_export_csv, 2, 0, 1, 2)
        rg.addWidget(self.btn_export_png, 2, 2, 1, 2)
        rg.addWidget(self.btn_stats, 2, 4, 1, 2)

        self.ctrl_tabs.addTab(tab_range, "Aralık")

        # --- TAB 2: İşaretçiler ---
        tab_markers = QWidget()
        mg = QGridLayout(tab_markers)
        mg.setContentsMargins(6, 6, 6, 6)
        mg.setHorizontalSpacing(10)
        mg.setVerticalSpacing(8)

        self.chk_click_mark = QCheckBox("Tıklayarak İşaretle")

        self.btn_clear_markers = QPushButton("İşaretçileri Temizle")
        self.btn_clear_markers.setProperty("danger", True)

        self.btn_marker_add = QPushButton("Topla")
        self.btn_marker_sub = QPushButton("Çıkar")
        self.btn_marker_add.setToolTip("Seçili 2 işaretçiyi topla")
        self.btn_marker_sub.setToolTip("Seçili 2 işaretçiyi çıkar (B - A)")

        self.cmb_marker_calc_axis = QComboBox()
        self.cmb_marker_calc_axis.addItems(["Sadece X", "Sadece Y", "X ve Y"])

        self.lbl_marker_calc = QLabel("Hesap: —")
        self.lbl_marker_calc.setStyleSheet("font-weight: 800;")

        self.marker_list = QTreeWidget()
        self.marker_list.setHeaderLabels(["İşaretçi", "X Değeri", "Y Değeri"])
        self.marker_list.setMaximumHeight(220)
        self.marker_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        mg.addWidget(self.chk_click_mark, 0, 0)
        mg.addWidget(self.btn_clear_markers, 0, 1)
        mg.addWidget(QLabel("Hesap:"), 0, 2)
        mg.addWidget(self.cmb_marker_calc_axis, 0, 3)
        mg.addWidget(self.btn_marker_add, 0, 4)
        mg.addWidget(self.btn_marker_sub, 0, 5)
        mg.addWidget(self.lbl_marker_calc, 0, 6)

        mg.addWidget(self.marker_list, 1, 0, 1, 7)

        self.ctrl_tabs.addTab(tab_markers, "İşaretçiler")

        # --- TAB 2: Filtre & Stil ---
        tab_style = QWidget()
        s = QGridLayout(tab_style)
        s.setContentsMargins(6, 6, 6, 6)
        s.setHorizontalSpacing(10)
        s.setVerticalSpacing(8)

        self.chk_smooth = QCheckBox("Savitzky-Golay ile Yumuşat")

        self.chk_cursor_marker = QCheckBox("İmleç Noktasını Göster")
        self.chk_cursor_marker.setChecked(True)
        self.chk_cursor_marker.setToolTip("Grafikte gezerken görünen imleç noktasını/kılavuzunu gizle-göster")

        self.spin_smooth_win = QSpinBox()
        self.spin_smooth_win.setRange(5, 999)
        self.spin_smooth_win.setSingleStep(2)
        self.spin_smooth_win.setValue(21)
        self.spin_smooth_win.setSuffix(" pts")

        self.cmb_style_series = QComboBox()
        self.cmb_style_series.setMinimumWidth(320)

        self.btn_pick_color = QPushButton("Renk Seç")
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
        self.sp_x_shift.setToolTip("Seçili shift kanalları için X kaydırma. Zaman/seconds için saniye, index için örnek birimi.")

        self.sp_y_shift = QDoubleSpinBox()
        self.sp_y_shift.setRange(-1e18, 1e18)
        self.sp_y_shift.setDecimals(6)
        self.sp_y_shift.setSingleStep(0.1)
        self.sp_y_shift.setValue(0.0)
        self.sp_y_shift.setKeyboardTracking(False)
        self.sp_y_shift.setToolTip("Seçili shift kanalları için Y kaydırma.")

        self.shift_series_tree = QTreeWidget()
        self.shift_series_tree.setColumnCount(1)
        self.shift_series_tree.setHeaderHidden(True)
        self.shift_series_tree.setRootIsDecorated(False)
        self.shift_series_tree.setUniformRowHeights(True)
        self.shift_series_tree.setMinimumHeight(140)
        self.shift_series_tree.setToolTip("Shift uygulanacak kanalları checkbox ile seçin.")

        self.btn_shift_apply = QPushButton("Shift Uygula")
        self.btn_shift_apply.setProperty("primary", True)
        self.btn_shift_apply.setToolTip("Girilen X/Y shift değerlerini işaretli kanallara uygular")
        self.btn_shift_reset = QPushButton("Shift Sıfırla")
        self.btn_shift_reset.setToolTip("İşaretli kanallardaki X/Y shift değerlerini sıfırlar")
        self.btn_shift_check_all = QPushButton("Tümünü Seç")
        self.btn_shift_uncheck_all = QPushButton("Seçimi Kaldır")

        self.btn_style_apply = QPushButton("Stili Uygula")
        self.btn_style_default = QPushButton("Varsayılan Stil")
        self.btn_style_reset_all = QPushButton("Tümünü Sıfırla")
        self.btn_style_reset_all.setProperty("danger", True)

        s.addWidget(self.chk_smooth, 0, 0, 1, 2)
        s.addWidget(self.chk_cursor_marker, 0, 4, 1, 2)
        s.addWidget(QLabel("Filtre Pencere Uzunluğu:"), 0, 2)
        s.addWidget(self.spin_smooth_win, 0, 3)

        s.addWidget(QLabel("Kanal (stil):"), 1, 0)
        s.addWidget(self.cmb_style_series, 1, 1, 1, 2)
        s.addWidget(self.btn_pick_color, 1, 3)
        s.addWidget(QLabel("Kalınlık:"), 1, 4)
        s.addWidget(self.sp_line_width, 1, 5)

        s.addWidget(QLabel("X Shift:"), 2, 0)
        s.addWidget(self.sp_x_shift, 2, 1)
        s.addWidget(QLabel("Y Shift:"), 2, 2)
        s.addWidget(self.sp_y_shift, 2, 3)
        s.addWidget(self.btn_shift_apply, 2, 4)
        s.addWidget(self.btn_shift_reset, 2, 5)

        s.addWidget(QLabel("Shift Kanalları:"), 3, 0)
        s.addWidget(self.shift_series_tree, 3, 1, 2, 5)

        s.addWidget(self.btn_shift_check_all, 5, 1)
        s.addWidget(self.btn_shift_uncheck_all, 5, 2)
        s.addWidget(self.btn_style_apply, 5, 3)
        s.addWidget(self.btn_style_default, 5, 4)
        s.addWidget(self.btn_style_reset_all, 5, 5)

        self.ctrl_tabs.addTab(tab_style, "Stil")

        plot_vsplit = QSplitter(Qt.Orientation.Vertical)
        plot_vsplit.setHandleWidth(8)
        plot_vsplit.addWidget(self.plot_pane)
        plot_vsplit.addWidget(controls)
        plot_vsplit.setStretchFactor(0, 7)
        plot_vsplit.setStretchFactor(1, 1)
        plot_vsplit.setSizes([840, 120])

        self._plot_vsplit = plot_vsplit
        # controls dock collapse/expand
        if hasattr(self, "btn_ctrl_collapse") and self.btn_ctrl_collapse is not None:
            self.btn_ctrl_collapse.toggled.connect(self._on_controls_panel_toggled)
        self._controls_last_sizes = plot_vsplit.sizes()

        # default collapsed
        self._on_controls_panel_toggled(False)

        plot_tab_layout.addWidget(plot_vsplit, stretch=1)
        self.tabs.addTab(plot_tab, "Grafik Analizi")

        # FFT tab
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
        self.btn_theme_fft.setToolTip("Tema Değiştir (Koyu/Açık)")
        self.btn_theme_fft.setFixedSize(34, 34)
        self.btn_theme_fft.setStyleSheet("border-radius: 17px; font-weight: 900; padding: 0px;")
        fft_topbar.addWidget(self.btn_theme_fft)
        fft_layout.addLayout(fft_topbar)

        self.fft_plot = PlotWidget()
        self.fft_plot.showGrid(x=True, y=True, alpha=0.28)
        self.fft_plot.setLabel("bottom", "Frekans (Hz)")
        self.fft_plot.setLabel("left", "Genlik")
        fft_layout.addWidget(self.fft_plot, stretch=1)

        self.lbl_fft_info = QLabel("FFT için bir kanal seçip hesaplama başlatın.")
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
        self.cmb_fft_channel.setToolTip("FFT analizinde kullanılacak kanal")

        self.fs_fft = QDoubleSpinBox()
        self.fs_fft.setRange(0.0, 1e12)
        self.fs_fft.setDecimals(6)
        self.fs_fft.setValue(0.0)
        self.fs_fft.setKeyboardTracking(False)
        self.fs_fft.setToolTip("0 ise zaman ekseninden otomatik hesaplanır. Gerekirse örnekleme frekansını manuel girin.")

        self.btn_fft = QPushButton("FFT Hesapla")
        self.btn_fft.setProperty("primary", True)
        self.btn_fft.setToolTip("Seçilen kanal için spektrumu yeniden hesaplar")

        self.btn_fft_clear = QPushButton("FFT Temizle")
        self.btn_fft_clear.setToolTip("FFT grafiğini ve özet bilgisini temizler")

        self.btn_fft_export = QPushButton("FFT CSV")
        self.btn_fft_export.setToolTip("Hesaplanan spektrumu (frekans, genlik, PSD) CSV olarak dışa aktar")

        fft_row1.addWidget(QLabel("Kanal:"), 0, 0)
        fft_row1.addWidget(self.cmb_fft_channel, 0, 1)
        fft_row1.addWidget(QLabel("Fs (0=auto):"), 0, 2)
        fft_row1.addWidget(self.fs_fft, 0, 3)
        fft_row1.addWidget(self.btn_fft, 0, 4)
        fft_row1.addWidget(self.btn_fft_clear, 0, 5)
        fft_row1.addWidget(self.btn_fft_export, 0, 6)
        fft_row1.setColumnStretch(1, 1)
        fft_panel_l.addLayout(fft_row1)

        fft_row2 = QHBoxLayout()
        fft_row2.setSpacing(12)

        self.chk_fft_log = QCheckBox("Log (dB)")
        self.chk_fft_log.setToolTip("Y eksenini dB cinsinden göster")
        self.chk_fft_window = QCheckBox("Hanning pencere")
        self.chk_fft_window.setChecked(True)
        self.chk_fft_window.setToolTip("Spektral sızıntıyı azaltmak için Hanning penceresi uygular")
        self.chk_fft_remove_mean = QCheckBox("Ortalamayı kaldır")
        self.chk_fft_remove_mean.setChecked(True)
        self.chk_fft_remove_mean.setToolTip("DC offset'i azaltır")
        self.chk_fft_detrend = QCheckBox("Lineer detrend")
        self.chk_fft_detrend.setChecked(True)
        self.chk_fft_detrend.setToolTip("Yavaş trend/drift bileşenini çıkarır; 0 Hz çevresini daha okunur yapar")
        self.chk_fft_hide_dc = QCheckBox("0 Hz'i gizle")
        self.chk_fft_hide_dc.setChecked(True)
        self.chk_fft_hide_dc.setToolTip("Grafikten DC bileşenini gizler")
        self.chk_fft_peak = QCheckBox("Baskın piki işaretle")
        self.chk_fft_peak.setChecked(True)
        self.chk_fft_peak.setToolTip("Filtrelenmiş spektrum üzerindeki en baskın frekansı işaretler")

        self.chk_fft_psd = QCheckBox("PSD (güç yoğunluğu)")
        self.chk_fft_psd.setChecked(False)
        self.chk_fft_psd.setToolTip("Genlik yerine tek taraflı güç spektral yoğunluğu (birim²/Hz) göster")

        for w in (self.chk_fft_log, self.chk_fft_psd, self.chk_fft_window, self.chk_fft_remove_mean,
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
        self.sp_fft_fmin.setToolTip("Grafikte gösterilecek minimum frekans. 0 bırakılırsa otomatik davranır.")

        self.sp_fft_fmax = QDoubleSpinBox()
        self.sp_fft_fmax.setRange(0.0, 1e12)
        self.sp_fft_fmax.setDecimals(6)
        self.sp_fft_fmax.setValue(0.0)
        self.sp_fft_fmax.setKeyboardTracking(False)
        self.sp_fft_fmax.setToolTip("Grafikte gösterilecek maksimum frekans. 0 ise Nyquist'e kadar gösterilir.")

        fft_row3.addWidget(QLabel("Min f (Hz):"))
        fft_row3.addWidget(self.sp_fft_fmin)
        fft_row3.addSpacing(8)
        fft_row3.addWidget(QLabel("Max f (Hz, 0=Nyquist):"))
        fft_row3.addWidget(self.sp_fft_fmax)
        fft_row3.addStretch(1)
        fft_panel_l.addLayout(fft_row3)

        fft_layout.addWidget(fft_panel)
        self.tabs.addTab(fft_tab, "Frekans Analizi (FFT)")

        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([320, 1240])

        self._build_menubar()

    def _build_menubar(self):
        mb = self.menuBar()
        mb.clear()

        # Dosya
        m_file = mb.addMenu("Dosya")
        act_open = QAction("TDMS Aç...", self)
        act_open.triggered.connect(self.open_tdms)
        m_file.addAction(act_open)

        self.menu_recent = m_file.addMenu("Son Dosyalar")
        self._rebuild_recent_menu()

        m_file.addSeparator()
        act_quit = QAction("Çıkış", self)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        # Dışa Aktar
        m_export = mb.addMenu("Dışa Aktar")
        act_csv = QAction("Çizilen Veriyi CSV...", self)
        act_csv.setShortcut("Ctrl+E")
        act_csv.triggered.connect(self.export_plotted_csv)
        m_export.addAction(act_csv)

        act_png = QAction("Grafik Görüntüsü (PNG)...", self)
        act_png.triggered.connect(self.export_plot_image)
        m_export.addAction(act_png)

        act_fft_csv = QAction("FFT CSV...", self)
        act_fft_csv.triggered.connect(self.export_fft_csv)
        m_export.addAction(act_fft_csv)

        # Analiz
        m_analyze = mb.addMenu("Analiz")
        act_stats = QAction("İstatistikler (Çizilen Kanallar)...", self)
        act_stats.setShortcut("Ctrl+I")
        act_stats.triggered.connect(self.show_statistics)
        m_analyze.addAction(act_stats)

    def _recent_files(self) -> List[str]:
        try:
            val = self.settings.value("paths/recent_files", [])
            if isinstance(val, str):
                val = [val] if val else []
            return [str(p) for p in (val or []) if p]
        except Exception:
            return []

    def _add_recent_file(self, path: str):
        try:
            path = os.path.abspath(path)
            recent = [p for p in self._recent_files() if os.path.abspath(p) != path]
            recent.insert(0, path)
            recent = recent[:10]
            self.settings.setValue("paths/recent_files", recent)
            self._rebuild_recent_menu()
        except Exception as e:
            logger.debug("Recent file kaydı başarısız: %s", e)

    def _rebuild_recent_menu(self):
        menu = getattr(self, "menu_recent", None)
        if menu is None:
            return
        menu.clear()
        recent = self._recent_files()
        if not recent:
            act = QAction("(boş)", self)
            act.setEnabled(False)
            menu.addAction(act)
            return
        for p in recent:
            act = QAction(p, self)
            act.triggered.connect(lambda _checked=False, path=p: self.open_tdms_path(path))
            menu.addAction(act)
        menu.addSeparator()
        act_clear = QAction("Listeyi Temizle", self)
        act_clear.triggered.connect(self._clear_recent_files)
        menu.addAction(act_clear)

    def _clear_recent_files(self):
        self.settings.setValue("paths/recent_files", [])
        self._rebuild_recent_menu()

    def _connect_signals(self):
        self.btn_open.clicked.connect(self.open_tdms)

        self.btn_plot_checked.clicked.connect(self.plot_checked_channels)
        self.btn_uncheck_all.clicked.connect(self.uncheck_all_channels)
        self.btn_clear_plot.clicked.connect(self.on_clear_plot)

        # --- Bottom controls (kept) ---
        self.chk_region.toggled.connect(self._set_region_enabled_all)
        self.chk_click_mark.toggled.connect(self._set_click_mark_enabled_all)

        self.plot_pane.range_changed.connect(lambda x0, x1: self._on_any_range_changed("main", x0, x1))
        self.xmin.valueChanged.connect(self.apply_range_manual)
        self.xmax.valueChanged.connect(self.apply_range_manual)
        self.btn_fit_y.clicked.connect(self.autofit_y_in_range)

        # --- Pane quickbar -> apply to both panes & sync UI ---
        self.plot_pane.interaction_mode_changed.connect(self._on_pane_mode_changed)
        self.plot_pane.autofit_requested.connect(self._on_pane_autofit)
        self.plot_pane.legend_toggled.connect(self._on_pane_legend)
        self.plot_pane.y_lock_toggled.connect(self._on_pane_ylock)
        self.plot_pane.right_y_lock_toggled.connect(self._on_pane_y2lock)
        self.plot_pane.region_toggled.connect(self._on_pane_region)
        self.plot_pane.click_mark_toggled.connect(self._on_pane_click_mark)

        # Markers
        self.btn_clear_markers.clicked.connect(self.clear_markers)
        self.plot_pane.marker_requested.connect(lambda x, y: self._on_marker_requested_from("main", x, y))
        self.marker_list.itemDoubleClicked.connect(self._on_marker_double_clicked)
        self.btn_marker_add.clicked.connect(self._marker_math_add)
        self.btn_marker_sub.clicked.connect(self._marker_math_sub)

        # Filter & style
        self.chk_smooth.toggled.connect(self.update_plot_data_with_filter)
        self.chk_cursor_marker.toggled.connect(self._set_cursor_marker_enabled_all)
        self.spin_smooth_win.valueChanged.connect(self.update_plot_data_with_filter)

        self.cmb_style_series.currentIndexChanged.connect(self._on_style_series_changed)
        self.btn_pick_color.clicked.connect(self.pick_color_for_selected_series)
        self.btn_style_apply.clicked.connect(self.apply_style_for_selected_series)
        self.btn_style_default.clicked.connect(self.reset_style_for_selected_series)
        self.btn_style_reset_all.clicked.connect(self.reset_all_styles)
        self.btn_shift_apply.clicked.connect(self.apply_shift_for_checked_series)
        self.btn_shift_reset.clicked.connect(self.reset_shift_for_checked_series)
        self.btn_shift_check_all.clicked.connect(lambda: self._set_all_shift_checks(True))
        self.btn_shift_uncheck_all.clicked.connect(lambda: self._set_all_shift_checks(False))

        # Background / detach
        self.btn_bg_pick.clicked.connect(self.pick_background_color)
        self.btn_bg_reset.clicked.connect(self.reset_background_color)
        self.btn_detach.clicked.connect(self.toggle_detach_plot)

        # Export / analysis
        self.btn_export_csv.clicked.connect(self.export_plotted_csv)
        self.btn_export_png.clicked.connect(self.export_plot_image)
        self.btn_stats.clicked.connect(self.show_statistics)

        # FFT
        self.btn_fft.clicked.connect(self.compute_fft)
        self.btn_fft_clear.clicked.connect(self.clear_fft_plot)
        self.btn_fft_export.clicked.connect(self.export_fft_csv)
        self.chk_fft_log.toggled.connect(self._rerender_fft_plot)
        self.chk_fft_psd.toggled.connect(self._rerender_fft_plot)
        self.chk_fft_hide_dc.toggled.connect(self._rerender_fft_plot)
        self.chk_fft_peak.toggled.connect(self._rerender_fft_plot)
        self.sp_fft_fmin.valueChanged.connect(self._rerender_fft_plot)
        self.sp_fft_fmax.valueChanged.connect(self._rerender_fft_plot)

        # Theme
        self.btn_theme_plot.clicked.connect(self._toggle_theme_from_button)
        self.btn_theme_fft.clicked.connect(self._toggle_theme_from_button)
        self.chk_manual_fs_plot.toggled.connect(self._on_manual_fs_controls_changed)
        self.sp_manual_fs_plot.valueChanged.connect(self._on_manual_fs_controls_changed)
        self.cmb_manual_fs_unit.currentIndexChanged.connect(self._on_manual_fs_controls_changed)

    def _on_controls_panel_toggled(self, expanded: bool):
        """Collapse/expand the bottom controls dock while keeping a slim header visible."""
        try:
            vs = getattr(self, "_plot_vsplit", None)
            if vs is None:
                return

            preferred_expanded_h = 120
            max_expanded_h = 140
            header_h = 42
            try:
                if hasattr(self, "controls_header") and self.controls_header is not None:
                    header_h = max(header_h, int(self.controls_header.sizeHint().height()) + 6)
            except Exception:
                pass

            controls = getattr(self, "controls_panel", None)
            total = sum(vs.sizes()) or 1

            if expanded:
                if hasattr(self, "controls_body") and self.controls_body is not None:
                    self.controls_body.setVisible(True)
                if hasattr(self, "btn_ctrl_collapse") and self.btn_ctrl_collapse is not None:
                    self.btn_ctrl_collapse.setArrowType(Qt.ArrowType.DownArrow)

                target_h = preferred_expanded_h
                sizes = getattr(self, "_controls_last_sizes", None)
                if isinstance(sizes, list) and len(sizes) == 2:
                    try:
                        target_h = int(sizes[1])
                    except Exception:
                        target_h = preferred_expanded_h
                target_h = max(preferred_expanded_h, min(target_h, max_expanded_h, max(0, total - 10)))

                try:
                    if controls is not None:
                        controls.setMinimumHeight(header_h)
                        controls.setMaximumHeight(max_expanded_h)
                except Exception:
                    pass
                vs.setSizes([max(10, total - target_h), target_h])
            else:
                cur = vs.sizes()
                if isinstance(cur, list) and len(cur) == 2:
                    saved_h = cur[1]
                    try:
                        saved_h = int(saved_h)
                    except Exception:
                        saved_h = preferred_expanded_h
                    saved_h = max(preferred_expanded_h, min(saved_h, max_expanded_h))
                    self._controls_last_sizes = [max(10, total - saved_h), saved_h]
                else:
                    self._controls_last_sizes = [max(10, total - preferred_expanded_h), preferred_expanded_h]

                if hasattr(self, "controls_body") and self.controls_body is not None:
                    self.controls_body.setVisible(False)
                if hasattr(self, "btn_ctrl_collapse") and self.btn_ctrl_collapse is not None:
                    self.btn_ctrl_collapse.setArrowType(Qt.ArrowType.RightArrow)
                try:
                    if controls is not None:
                        controls.setMinimumHeight(header_h)
                        controls.setMaximumHeight(header_h + 4)
                except Exception:
                    pass
                vs.setSizes([max(10, total - header_h), header_h])
        except Exception:
            # do not crash UI on toggle
            return

    # ---------------- pane <-> UI sync helpers ----------------
    def _silent_set_checked(self, w, checked: bool):
        if w is None:
            return
        try:
            bs = w.blockSignals(True)
            w.setChecked(bool(checked))
            w.blockSignals(bs)
        except Exception:
            pass

    def _on_pane_mode_changed(self, mode: str):
        if self._pane_sync_guard:
            return
        self._pane_sync_guard = True
        try:
            self._apply_interaction_mode_to_all(mode)
        finally:
            self._pane_sync_guard = False

    def _on_pane_autofit(self):
        if self._pane_sync_guard:
            return
        self._pane_sync_guard = True
        try:
            self._autofit_all_panes()
        finally:
            self._pane_sync_guard = False

    def _on_pane_legend(self, on: bool):
        if self._pane_sync_guard:
            return
        self._pane_sync_guard = True
        try:
            self._set_legend_visible_all(bool(on))
        finally:
            self._pane_sync_guard = False

    def _on_pane_ylock(self, on: bool):
        if self._pane_sync_guard:
            return
        self._pane_sync_guard = True
        try:
            self._set_y_lock_enabled_all(bool(on))
        finally:
            self._pane_sync_guard = False

    def _on_pane_y2lock(self, on: bool):
        if self._pane_sync_guard:
            return
        self._pane_sync_guard = True
        try:
            self._set_right_y_lock_enabled_all(bool(on))
        finally:
            self._pane_sync_guard = False

    def _on_pane_region(self, on: bool):
        if self._pane_sync_guard:
            return
        self._pane_sync_guard = True
        try:
            self._silent_set_checked(self.chk_region, bool(on))
            self._set_region_enabled_all(bool(on))
        finally:
            self._pane_sync_guard = False

    def _on_pane_click_mark(self, on: bool):
        if self._pane_sync_guard:
            return
        self._pane_sync_guard = True
        try:
            self._silent_set_checked(self.chk_click_mark, bool(on))
            self._set_click_mark_enabled_all(bool(on))
        finally:
            self._pane_sync_guard = False

    # ---------------- background controls ----------------
    def _set_bg_button_preview(self, rgb):
        r, g, b = rgb
        fg = _bg_to_fg_rgb(rgb)
        fr, fg_, fb = fg
        self.btn_bg_pick.setStyleSheet(
            f"background-color: rgb({r},{g},{b}); color: rgb({fr},{fg_},{fb}); font-weight: 800;"
            f"border-radius: 9px; padding: 7px 12px;"
        )

    def pick_background_color(self):
        initial = QColor(*self.plot_bg_rgb)
        c = QColorDialog.getColor(initial, self, "Grafik Arkaplan Rengi Seç")
        if not c.isValid():
            return
        self.plot_bg_rgb = qcolor_to_tuple(c)
        self._set_bg_button_preview(self.plot_bg_rgb)
        self._apply_background_everywhere()

    def reset_background_color(self):
        self.plot_bg_rgb = self._theme_default_bg[self._theme]
        self._set_bg_button_preview(self.plot_bg_rgb)
        self._apply_background_everywhere()

    def _apply_background_everywhere(self):
        self.plot_pane.set_background(self.plot_bg_rgb)
        if self.detached_win is not None:
            self.detached_win.pane.set_background(self.plot_bg_rgb)
        apply_plotwidget_theme(self.fft_plot, self.plot_bg_rgb)
        if getattr(self, "_last_fft_payload", None):
            self._render_fft_payload(self._last_fft_payload)

    # ---------------- legend ----------------
    def _set_legend_visible_all(self, enabled: bool):
        self.plot_pane.set_legend_visible(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_legend_visible(enabled)

    # ---------------- detach ----------------
    def toggle_detach_plot(self):
        if self.detached_win is None:
            self.detached_win = DetachedPlotWindow(bg_rgb=self.plot_bg_rgb, parent=self)
            self.detached_win.closed.connect(self._on_detached_closed)

            # Keep range + marker requests in sync with the main logic
            self.detached_win.pane.range_changed.connect(
                lambda x0, x1: self._on_any_range_changed("detached", x0, x1)
            )
            self.detached_win.pane.marker_requested.connect(
                lambda x, y: self._on_marker_requested_from("detached", x, y)
            )

            # Detached quickbar -> sync to both panes
            self.detached_win.pane.interaction_mode_changed.connect(self._on_pane_mode_changed)
            self.detached_win.pane.autofit_requested.connect(self._on_pane_autofit)
            self.detached_win.pane.legend_toggled.connect(self._on_pane_legend)
            self.detached_win.pane.y_lock_toggled.connect(self._on_pane_ylock)
            self.detached_win.pane.right_y_lock_toggled.connect(self._on_pane_y2lock)
            self.detached_win.pane.region_toggled.connect(self._on_pane_region)
            self.detached_win.pane.click_mark_toggled.connect(self._on_pane_click_mark)

            # Mirror current main pane state onto detached (and its quickbar)
            self.detached_win.pane.set_interaction_mode(getattr(self.plot_pane, "_interaction_mode", "pan"))
            self.detached_win.pane.enable_region(getattr(self.plot_pane, "_region_enabled", False))
            self.detached_win.pane.set_click_mark_mode(getattr(self.plot_pane, "_click_mark_enabled", False))
            self.detached_win.pane.set_y_lock(getattr(self.plot_pane, "_y_lock_enabled", False))
            self.detached_win.pane.set_right_y_lock(getattr(self.plot_pane, "_right_y_lock_enabled", False))
            self.detached_win.pane.set_legend_visible(getattr(self.plot_pane, "_legend_visible", True))

            # Ensure bottom UI reflects current state (region / click mark)
            self._silent_set_checked(self.chk_region, getattr(self.plot_pane, "_region_enabled", False))
            self._silent_set_checked(self.chk_click_mark, getattr(self.plot_pane, "_click_mark_enabled", False))

            self.update_plot_data_with_filter()

            for m in self._markers.values():
                self.detached_win.pane.add_marker(m["x"], m["y"], m["label"])

            self.detached_win.show()
            self.btn_detach.setText("Grafiği Geri Al (Kapat)")
        else:
            self.detached_win.close()

    def _on_detached_closed(self):
        if self.detached_win is not None:
            try:
                self.detached_win.deleteLater()
            except Exception:
                pass
        self.detached_win = None
        self.btn_detach.setText("Grafiği Ayır")

    # ---------------- apply-to-all ----------------
    def _apply_interaction_mode_to_all(self, mode: Optional[str] = None):
        m = (mode or getattr(self.plot_pane, "_interaction_mode", "pan") or "pan")
        m = (m or "pan").strip().lower()
        m = "pan" if m == "pan" else "zoom"
        self.plot_pane.set_interaction_mode(m)
        if self.detached_win is not None:
            self.detached_win.pane.set_interaction_mode(m)

    def _autofit_all_panes(self):
        self.plot_pane.autofit_all()
        if self.detached_win is not None:
            self.detached_win.pane.autofit_all()

    def _set_region_enabled_all(self, enabled: bool):
        self.plot_pane.enable_region(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.enable_region(enabled)

    def _set_click_mark_enabled_all(self, enabled: bool):
        self.plot_pane.set_click_mark_mode(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_click_mark_mode(enabled)

    def _set_cursor_marker_enabled_all(self, enabled: bool):
        self.plot_pane.set_cursor_marker_enabled(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_cursor_marker_enabled(enabled)

    def _set_y_lock_enabled_all(self, enabled: bool):
        self.plot_pane.set_y_lock(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_y_lock(enabled)

    def _set_right_y_lock_enabled_all(self, enabled: bool):  # YENİ
        self.plot_pane.set_right_y_lock(enabled)
        if self.detached_win is not None:
            self.detached_win.pane.set_right_y_lock(enabled)

    # ---------------- Style UI helpers ----------------
    def _set_color_button_preview(self, rgb_tuple_or_none):
        if rgb_tuple_or_none is None:
            self.btn_pick_color.setText("Renk Seç (Oto)")
            self.btn_pick_color.setStyleSheet("")
            return
        r, g, b = rgb_tuple_or_none
        self.btn_pick_color.setText("Renk Seç")
        self.btn_pick_color.setStyleSheet(
            f"background-color: rgb({r},{g},{b}); color: white; font-weight: 800; border-radius: 9px;"
        )

    def _current_style_key(self) -> Optional[str]:
        idx = self.cmb_style_series.currentIndex()
        if idx < 0:
            return None
        data = self.cmb_style_series.itemData(idx, role=Qt.ItemDataRole.UserRole)
        return data if isinstance(data, str) else None

    def _on_style_series_changed(self, *_):
        skey = self._current_style_key()
        if not skey:
            self._set_color_button_preview(None)
            self.sp_line_width.setValue(2.0)
            return

        st = self.style_map.get(skey) or {}
        col = st.get("color")
        if isinstance(col, QColor):
            col = qcolor_to_tuple(col)
        self._set_color_button_preview(col if isinstance(col, tuple) else None)

        self.sp_line_width.setValue(float(st.get("width", 2.0)))

    def pick_color_for_selected_series(self):
        skey = self._current_style_key()
        if not skey:
            return
        st = self.style_map.get(skey, {})
        current = st.get("color")
        if isinstance(current, tuple):
            initial = QColor(*current)
        elif isinstance(current, QColor):
            initial = current
        else:
            initial = QColor(0, 120, 215)

        c = QColorDialog.getColor(initial, self, "Renk Seç")
        if not c.isValid():
            return

        st["color"] = qcolor_to_tuple(c)
        st["width"] = float(st.get("width", self.sp_line_width.value()))

        self.style_map[skey] = st
        self._set_color_button_preview(st["color"])
        self.update_plot_data_with_filter()

    def apply_style_for_selected_series(self):
        skey = self._current_style_key()
        if not skey:
            return
        st = self.style_map.get(skey, {})
        st["width"] = float(self.sp_line_width.value())
        self.style_map[skey] = st
        self.update_plot_data_with_filter()

    def reset_style_for_selected_series(self):
        skey = self._current_style_key()
        if not skey:
            return
        st = dict(self.style_map.get(skey, {}) or {})
        st.pop("color", None)
        st.pop("width", None)
        if st:
            self.style_map[skey] = st
        elif skey in self.style_map:
            del self.style_map[skey]
        self._set_color_button_preview(None)
        self.sp_line_width.setValue(2.0)
        self.update_plot_data_with_filter()

    def _checked_shift_style_keys(self) -> List[str]:
        keys: List[str] = []
        tree = getattr(self, "shift_series_tree", None)
        if tree is None:
            return keys
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item is None or item.checkState(0) != Qt.CheckState.Checked:
                continue
            skey = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(skey, str) and skey:
                keys.append(skey)
        return keys

    def _set_all_shift_checks(self, checked: bool):
        tree = getattr(self, "shift_series_tree", None)
        if tree is None:
            return
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        tree.blockSignals(True)
        try:
            for i in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(i)
                if item is not None:
                    item.setCheckState(0, state)
        finally:
            tree.blockSignals(False)

    def apply_shift_for_checked_series(self):
        keys = self._checked_shift_style_keys()
        if not keys:
            QMessageBox.information(self, "Shift Seçimi Yok", "Shift uygulanacak en az bir kanalı işaretleyin.")
            return

        x_shift = float(self.sp_x_shift.value())
        y_shift = float(self.sp_y_shift.value())
        if not math.isfinite(x_shift):
            x_shift = 0.0
        if not math.isfinite(y_shift):
            y_shift = 0.0

        for skey in keys:
            st = dict(self.style_map.get(skey, {}) or {})
            if x_shift != 0.0:
                st["x_shift"] = x_shift
            else:
                st.pop("x_shift", None)
            if y_shift != 0.0:
                st["y_shift"] = y_shift
            else:
                st.pop("y_shift", None)
            if st:
                self.style_map[skey] = st
            elif skey in self.style_map:
                del self.style_map[skey]

        self.update_plot_data_with_filter()
        self.status.showMessage(f"Shift uygulandı: {len(keys)} kanal.", 2500)

    def reset_shift_for_checked_series(self):
        keys = self._checked_shift_style_keys()
        if not keys:
            QMessageBox.information(self, "Shift Seçimi Yok", "Shift sıfırlanacak en az bir kanalı işaretleyin.")
            return

        for skey in keys:
            st = dict(self.style_map.get(skey, {}) or {})
            st.pop("x_shift", None)
            st.pop("y_shift", None)
            if st:
                self.style_map[skey] = st
            elif skey in self.style_map:
                del self.style_map[skey]

        self.update_plot_data_with_filter()
        self.status.showMessage(f"Shift sıfırlandı: {len(keys)} kanal.", 2500)

    def reset_all_styles(self):
        self.style_map.clear()
        self._on_style_series_changed()
        self.update_plot_data_with_filter()

    # ---------------- Persistence ----------------
    def _restore_ui_state(self):
        """QSettings üzerinden temel UI durumunu geri yükle (best-effort)."""
        try:
            g = self.settings.value("ui/geometry", None)
            if g:
                self.restoreGeometry(g)

            st = self.settings.value("ui/window_state", None)
            if st:
                self.restoreState(st)

            ms = self.settings.value("ui/main_splitter", None)
            if ms and hasattr(self, "_main_splitter"):
                try:
                    self._main_splitter.restoreState(ms)
                except Exception:
                    pass

            ps = self.settings.value("ui/plot_splitter", None)
            if ps and hasattr(self, "_plot_vsplit"):
                try:
                    self._plot_vsplit.restoreState(ps)
                except Exception:
                    pass

            if hasattr(self, "chk_manual_fs_plot"):
                self.chk_manual_fs_plot.setChecked(str(self.settings.value("manual_fs/enabled", "false")).lower() in ("1", "true", "yes"))
            if hasattr(self, "sp_manual_fs_plot"):
                try:
                    self.sp_manual_fs_plot.setValue(float(self.settings.value("manual_fs/value", 0.0) or 0.0))
                except Exception:
                    self.sp_manual_fs_plot.setValue(0.0)
            if hasattr(self, "cmb_manual_fs_unit"):
                unit = str(self.settings.value("manual_fs/unit", "kHz") or "kHz")
                idx = self.cmb_manual_fs_unit.findText(unit)
                if idx >= 0:
                    self.cmb_manual_fs_unit.setCurrentIndex(idx)

            if hasattr(self, "chk_fft_log"):
                self.chk_fft_log.setChecked(str(self.settings.value("fft/log", "false")).lower() in ("1", "true", "yes"))
            if hasattr(self, "chk_fft_window"):
                self.chk_fft_window.setChecked(str(self.settings.value("fft/window", "true")).lower() in ("1", "true", "yes"))
            if hasattr(self, "chk_fft_remove_mean"):
                self.chk_fft_remove_mean.setChecked(str(self.settings.value("fft/remove_mean", "true")).lower() in ("1", "true", "yes"))
            if hasattr(self, "chk_fft_detrend"):
                self.chk_fft_detrend.setChecked(str(self.settings.value("fft/detrend", "true")).lower() in ("1", "true", "yes"))
            if hasattr(self, "chk_fft_hide_dc"):
                self.chk_fft_hide_dc.setChecked(str(self.settings.value("fft/hide_dc", "true")).lower() in ("1", "true", "yes"))
            if hasattr(self, "chk_fft_peak"):
                self.chk_fft_peak.setChecked(str(self.settings.value("fft/peak", "true")).lower() in ("1", "true", "yes"))
            if hasattr(self, "chk_fft_psd"):
                self.chk_fft_psd.setChecked(str(self.settings.value("fft/psd", "false")).lower() in ("1", "true", "yes"))
            if hasattr(self, "sp_fft_fmin"):
                try:
                    self.sp_fft_fmin.setValue(float(self.settings.value("fft/fmin", 0.0) or 0.0))
                except Exception:
                    self.sp_fft_fmin.setValue(0.0)
            if hasattr(self, "sp_fft_fmax"):
                try:
                    self.sp_fft_fmax.setValue(float(self.settings.value("fft/fmax", 0.0) or 0.0))
                except Exception:
                    self.sp_fft_fmax.setValue(0.0)
            if hasattr(self, "fs_fft"):
                try:
                    self.fs_fft.setValue(float(self.settings.value("fft/fs_hint", 0.0) or 0.0))
                except Exception:
                    self.fs_fft.setValue(0.0)
        except Exception as e:
            logger.debug("UI state restore başarısız: %s", e)

    def _save_ui_state(self):
        """QSettings'e temel UI durumunu kaydet (best-effort)."""
        try:
            self.settings.setValue("ui/theme", self._theme)
            self.settings.setValue("ui/geometry", self.saveGeometry())
            self.settings.setValue("ui/window_state", self.saveState())
            if hasattr(self, "_main_splitter"):
                self.settings.setValue("ui/main_splitter", self._main_splitter.saveState())
            if hasattr(self, "_plot_vsplit"):
                self.settings.setValue("ui/plot_splitter", self._plot_vsplit.saveState())
            if hasattr(self, "chk_manual_fs_plot"):
                self.settings.setValue("manual_fs/enabled", bool(self.chk_manual_fs_plot.isChecked()))
            if hasattr(self, "sp_manual_fs_plot"):
                self.settings.setValue("manual_fs/value", float(self.sp_manual_fs_plot.value()))
            if hasattr(self, "cmb_manual_fs_unit"):
                self.settings.setValue("manual_fs/unit", self.cmb_manual_fs_unit.currentText())

            if hasattr(self, "chk_fft_log"):
                self.settings.setValue("fft/log", bool(self.chk_fft_log.isChecked()))
            if hasattr(self, "chk_fft_window"):
                self.settings.setValue("fft/window", bool(self.chk_fft_window.isChecked()))
            if hasattr(self, "chk_fft_remove_mean"):
                self.settings.setValue("fft/remove_mean", bool(self.chk_fft_remove_mean.isChecked()))
            if hasattr(self, "chk_fft_detrend"):
                self.settings.setValue("fft/detrend", bool(self.chk_fft_detrend.isChecked()))
            if hasattr(self, "chk_fft_hide_dc"):
                self.settings.setValue("fft/hide_dc", bool(self.chk_fft_hide_dc.isChecked()))
            if hasattr(self, "chk_fft_peak"):
                self.settings.setValue("fft/peak", bool(self.chk_fft_peak.isChecked()))
            if hasattr(self, "chk_fft_psd"):
                self.settings.setValue("fft/psd", bool(self.chk_fft_psd.isChecked()))
            if hasattr(self, "sp_fft_fmin"):
                self.settings.setValue("fft/fmin", float(self.sp_fft_fmin.value()))
            if hasattr(self, "sp_fft_fmax"):
                self.settings.setValue("fft/fmax", float(self.sp_fft_fmax.value()))
            if hasattr(self, "fs_fft"):
                self.settings.setValue("fft/fs_hint", float(self.fs_fft.value()))
        except Exception as e:
            logger.debug("UI state save başarısız: %s", e)

    def _install_shortcuts(self):
        # Ctrl+O (Open)
        try:
            act_open = QAction(self)
            act_open.setShortcut(QKeySequence.StandardKey.Open)
            act_open.triggered.connect(self.open_tdms)
            self.addAction(act_open)

            # Ctrl+W / Ctrl+F4 (Close tab)
            act_close = QAction(self)
            act_close.setShortcut(QKeySequence.StandardKey.Close)
            act_close.triggered.connect(lambda: self._close_file_tab(self.file_tabs.currentIndex()))
            self.addAction(act_close)
        except Exception as e:
            logger.debug("Kısayollar kurulamadı: %s", e)

    def closeEvent(self, event: QCloseEvent):
        # Persist UI preferences
        self._save_ui_state()

        try:
            self._lazy_view_timer.stop()
        except Exception:
            pass

        # Best-effort cancel outstanding jobs
        try:
            self._cancel_jobs(["index", "preview", "load", "lazy_view", "filter", "fft"])
            for th, _worker, _tag in list(self._jobs):
                try:
                    th.quit()
                    th.wait(250)
                except Exception:
                    pass
        except Exception:
            pass

        super().closeEvent(event)

    # ---------------- File tabs ----------------
    def _new_file_id(self) -> str:
        self.file_counter += 1
        return f"F{self.file_counter}"

    def open_tdms(self):
        last_dir = str(self.settings.value("paths/last_dir", "") or "")
        start_dir = last_dir if (last_dir and os.path.isdir(last_dir)) else ""
        path, _ = QFileDialog.getOpenFileName(self, "TDMS Aç", start_dir, "TDMS (*.tdms)")
        if not path:
            return
        try:
            self.settings.setValue("paths/last_dir", os.path.dirname(path))
        except Exception as e:
            logger.debug("Last dir kaydı başarısız: %s", e)
        self.open_tdms_path(path)

    def open_tdms_paths(self, paths: List[str]):
        if not paths:
            return
        for p in paths:
            try:
                self.open_tdms_path(p)
            except Exception:
                pass

    def open_tdms_path(self, path: str):
        path = (path or "").strip()
        if not path:
            return

        try:
            self.settings.setValue("paths/last_dir", os.path.dirname(path))
        except Exception:
            pass

        if os.path.isfile(path):
            self._add_recent_file(path)

        for fid, st in self.files.items():
            if os.path.abspath(st.path) == os.path.abspath(path):
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
        hdr.addWidget(QLabel("Dosya:"))
        hdr.addWidget(lbl, stretch=1)
        lay.addLayout(hdr)

        search = QLineEdit()
        search.setPlaceholderText("Kanal ara...")
        lay.addWidget(search)

        tree = QTreeWidget()
        tree.setHeaderLabels(["Kanal Adı", "Veri Sayısı"])
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        lay.addWidget(tree, stretch=1)

        search.textChanged.connect(lambda t, fid=file_id: self.filter_tree(fid, t))
        tree.itemSelectionChanged.connect(lambda fid=file_id: self.on_selection_changed(fid))
        tree.itemChanged.connect(lambda item, col, fid=file_id: self.on_item_changed(fid, item, col))

        self.file_tabs.addTab(tab, label)
        self.file_tabs.setCurrentIndex(self.file_tabs.count() - 1)

        self.files[file_id] = FileState(file_id=file_id, path=path, label=label, tree=tree, search=search)

        self.status.showMessage(f"{label}: indeksleniyor...")

        worker = TdmsIndexWorker(file_id, path)
        self._start_job(worker, self._on_index_ready, self._on_worker_failed, tag="index")

    def _close_file_tab(self, index: int):
        if index == 0:
            return
        w = self.file_tabs.widget(index)

        file_id_to_remove = None

        # Öneri: file_id'yi tab widget üzerinde sakla (tree parent eşleştirmesine gerek kalmasın)
        try:
            if w is not None:
                file_id_to_remove = w.property("file_id")
        except Exception as e:
            logger.debug("Tab file_id okunamadı: %s", e)

        # Geriye dönük/fallback (eski davranış)
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
        self.status.showMessage("Dosya kapatıldı.", 2000)

    def _on_worker_failed(self, msg: str):
        QMessageBox.critical(self, "Hata", msg)
        self.status.showMessage("Hata oluştu.", 5000)

    def _on_index_ready(self, payload: dict):
        if (payload or {}).get("cancelled"):
            return

        file_id = payload["file_id"]
        refs = payload["refs"]
        st = self.files.get(file_id)
        if not st:
            return

        self.populate_tree(st, refs)
        self.status.showMessage(f"{st.label}: {len(refs)} kanal bulundu.", 5000)

    def populate_tree(self, st: FileState, refs: List[Tuple[str, str, int]]):
        tree = st.tree
        tree.blockSignals(True)
        tree.clear()

        groups: Dict[str, QTreeWidgetItem] = {}
        for gname, cname, n in refs:
            if gname not in groups:
                g = QTreeWidgetItem([gname, ""])
                g.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                g.setCheckState(0, Qt.CheckState.Unchecked)
                tree.addTopLevelItem(g)
                groups[gname] = g

            key = ChannelKey(file_id=st.file_id, group=gname, channel=cname, length=int(n))
            c = QTreeWidgetItem([cname, str(int(n))])
            c.setData(0, Qt.ItemDataRole.UserRole, key)
            c.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable)
            c.setCheckState(0, Qt.CheckState.Unchecked)
            groups[gname].addChild(c)

        tree.expandToDepth(0)
        tree.blockSignals(False)

    def filter_tree(self, file_id: str, t: str):
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

    def on_item_changed(self, file_id: str, item: QTreeWidgetItem, col: int):
        if col != 0:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(key, ChannelKey):
            return
        if self.current_series:
            self.update_plot_data_with_filter()

    def _get_plot_manual_fs_hz(self) -> Optional[float]:
        if not hasattr(self, "chk_manual_fs_plot") or not self.chk_manual_fs_plot.isChecked():
            return None
        value = float(self.sp_manual_fs_plot.value())
        unit = self.cmb_manual_fs_unit.currentText()
        return fs_value_to_hz(value, unit)

    def _manual_fs_display_text(self) -> str:
        fs_hz = self._get_plot_manual_fs_hz()
        if fs_hz is None:
            return "—"
        raw_value = float(self.sp_manual_fs_plot.value())
        raw_unit = self.cmb_manual_fs_unit.currentText() or "Hz"
        return f"{raw_value:.6g} {raw_unit} ({fs_hz:.6g} Hz, manuel)"

    def _rebuild_uniform_x_for_series(self, s: dict, x_mode: str, x_base: float, x_inc: float) -> np.ndarray:
        if bool(s.get("lazy_meta")):
            win = s.get("_lazy_last_win")
            if isinstance(win, tuple) and len(win) == 3:
                i0, i1, stride = win
                idx = np.arange(int(i0), int(i1), max(1, int(stride)), dtype=np.float64)
            else:
                idx = np.arange(len(np.asarray(s.get("y", []))), dtype=np.float64)
        else:
            idx = np.arange(len(np.asarray(s.get("y", []))), dtype=np.float64)

        if x_mode == "index":
            return idx
        return (float(x_base) + idx * float(x_inc)).astype(np.float64)

    def _apply_manual_fs_override_to_current_series(self):
        if not self.current_series:
            return

        manual_fs_hz = self._get_plot_manual_fs_hz()
        for s in self.current_series:
            source_mode = str(s.get("source_x_mode", s.get("x_mode", "index")) or "index")
            source_base = float(s.get("source_x_base", 0.0) or 0.0)
            source_inc = float(s.get("source_x_inc", 1.0) or 1.0)
            source_fs_est = s.get("source_fs_est")

            if manual_fs_hz is not None and source_mode == "index":
                s["x_mode"] = "seconds"
                s["x"] = self._rebuild_uniform_x_for_series(s, "seconds", 0.0, 1.0 / float(manual_fs_hz))
                s["fs_est"] = float(manual_fs_hz)
                s["manual_fs_hz"] = float(manual_fs_hz)
                if bool(s.get("lazy_meta")):
                    meta = s.get("lazy_meta") or {}
                    meta["x_mode"] = "seconds"
                    meta["x_base"] = 0.0
                    meta["x_inc"] = 1.0 / float(manual_fs_hz)
                    s["lazy_meta"] = meta
                continue

            s["manual_fs_hz"] = None
            s["fs_est"] = source_fs_est
            if bool(s.get("lazy_meta")):
                meta = s.get("lazy_meta") or {}
                meta["x_mode"] = source_mode
                meta["x_base"] = source_base
                meta["x_inc"] = source_inc
                s["lazy_meta"] = meta
                s["x_mode"] = source_mode
                s["x"] = self._rebuild_uniform_x_for_series(s, source_mode, source_base, source_inc)
            else:
                s["x_mode"] = source_mode
                if source_mode == "index":
                    s["x"] = self._rebuild_uniform_x_for_series(s, "index", 0.0, 1.0)

    def _refresh_preview_panel(self):
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

        self.p_quantity.setText(p.get("quantity", "Değer"))
        u = (p.get("unit") or "").strip()
        self.p_unit.setText(u if u else "—")

    def _on_manual_fs_controls_changed(self, *_args):
        self._refresh_preview_panel()
        if self.current_series:
            self._apply_manual_fs_override_to_current_series()
            self.update_plot_data_with_filter()
            if self._get_plot_manual_fs_hz() is not None:
                self.status.showMessage("Manuel Fs ile X ekseni zamana çevrildi.", 2500)
            else:
                self.status.showMessage("Manuel Fs kapatıldı; kanal eksenleri özgün haline döndürüldü.", 2500)

    # ---------------- Preview ----------------
    def on_selection_changed(self, file_id: str):
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
        self._start_job(worker, self._on_preview_ready, self._on_worker_failed, tag="preview", cancel_tags=["preview"])

    def _on_preview_ready(self, p: dict):
        if (p or {}).get("cancelled"):
            return
        self._last_preview_payload = dict(p or {})
        self._refresh_preview_panel()

    # ---------------- Plot selection ----------------
    def uncheck_all_channels(self):
        for st in self.files.values():
            tree = st.tree
            tree.blockSignals(True)
            for i in range(tree.topLevelItemCount()):
                g = tree.topLevelItem(i)
                g.setCheckState(0, Qt.CheckState.Unchecked)
            tree.blockSignals(False)
        self.status.showMessage("Tüm işaretler kaldırıldı.", 2000)

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
                            display_name = c.text(0)
                            reqs.append(ChannelRequest(key=key, display_name=display_name))
        return reqs

    def plot_checked_channels(self):
        if not self.files:
            return
        reqs = self._gather_checked_requests()
        if not reqs:
            QMessageBox.information(self, "Seçim Yok", "En az bir kanalı işaretleyin (checkbox).")
            return

        files_payload = {fid: {"path": st.path, "label": st.label} for fid, st in self.files.items()}

        self.status.showMessage("Kanallar yükleniyor...")
        worker = MultiTdmsChannelLoadWorker(files_payload, reqs)
        self._start_job(worker, self._on_load_ready, self._on_worker_failed, tag="load",
                        cancel_tags=["load", "lazy_view", "filter"])

    def _on_load_ready(self, payload: dict):
        if (payload or {}).get("cancelled"):
            return

        self.current_series = payload["series"]
        self._apply_manual_fs_override_to_current_series()
        self.clear_markers()
        self.update_plot_data_with_filter()
        self._refresh_series_comboboxes()
        self.status.showMessage(f"{len(self.current_series)} kanal hazır.", 3000)

    def _refresh_series_comboboxes(self):
        prev_style_skey = self._current_style_key()
        prev_shift_checked = set(self._checked_shift_style_keys())

        self.cmb_fft_channel.clear()
        self.cmb_style_series.clear()
        if hasattr(self, "shift_series_tree") and self.shift_series_tree is not None:
            self.shift_series_tree.clear()

        if not self.current_series:
            return

        seen = set()
        style_restore_index = -1
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
            if skey == prev_style_skey:
                style_restore_index = self.cmb_style_series.count() - 1

            if hasattr(self, "shift_series_tree") and self.shift_series_tree is not None:
                item = QTreeWidgetItem([label])
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                item.setData(0, Qt.ItemDataRole.UserRole, skey)
                item.setCheckState(0, Qt.CheckState.Checked if skey in prev_shift_checked else Qt.CheckState.Unchecked)
                self.shift_series_tree.addTopLevelItem(item)

        if style_restore_index >= 0:
            self.cmb_style_series.setCurrentIndex(style_restore_index)
        elif self.cmb_style_series.count() > 0:
            self.cmb_style_series.setCurrentIndex(0)
        self._on_style_series_changed()

    def _capture_current_ranges_for_replot(self):
        self._preserve_main_xrange = None
        self._preserve_detached_xrange = None

        try:
            if self.plot_pane.plot is not None:
                xr = self.plot_pane.plot.getViewBox().viewRange()[0]
                self._preserve_main_xrange = (float(xr[0]), float(xr[1]))
        except Exception:
            self._preserve_main_xrange = None

        try:
            if self.detached_win is not None and self.detached_win.pane.plot is not None:
                xr = self.detached_win.pane.plot.getViewBox().viewRange()[0]
                self._preserve_detached_xrange = (float(xr[0]), float(xr[1]))
        except Exception:
            self._preserve_detached_xrange = None

    def _restore_ranges_after_replot(self):
        try:
            xr = self._preserve_main_xrange
            if xr and self.plot_pane.plot is not None and xr[1] > xr[0]:
                self.plot_pane.set_xrange(float(xr[0]), float(xr[1]))
        except Exception:
            pass
        finally:
            self._preserve_main_xrange = None

        try:
            xr = self._preserve_detached_xrange
            if xr and self.detached_win is not None and self.detached_win.pane.plot is not None and xr[1] > xr[0]:
                self.detached_win.pane.set_xrange(float(xr[0]), float(xr[1]))
        except Exception:
            pass
        finally:
            self._preserve_detached_xrange = None

    def _render_to_all_panes(self, display_series: List[dict], axis_mode: str, x_label: str):
        # Preserve interactive states across re-plot
        mode = getattr(self.plot_pane, "_interaction_mode", "pan")
        reg = getattr(self.plot_pane, "_region_enabled", False)
        clk = getattr(self.plot_pane, "_click_mark_enabled", False)
        yl = getattr(self.plot_pane, "_y_lock_enabled", False)
        y2 = getattr(self.plot_pane, "_right_y_lock_enabled", False)
        leg = getattr(self.plot_pane, "_legend_visible", True)

        self.plot_pane.plot_series(display_series, axis_mode, x_label, style_map=self.style_map)
        self.plot_pane.set_background(self.plot_bg_rgb)

        # Re-apply
        self.plot_pane.set_interaction_mode(mode)
        self.plot_pane.enable_region(reg)
        self.plot_pane.set_click_mark_mode(clk)
        self.plot_pane.set_y_lock(yl)
        self.plot_pane.set_right_y_lock(y2)
        self.plot_pane.set_legend_visible(leg)

        if self.detached_win is not None:
            self.detached_win.pane.plot_series(display_series, axis_mode, x_label, style_map=self.style_map)
            self.detached_win.pane.set_background(self.plot_bg_rgb)

            self.detached_win.pane.set_interaction_mode(mode)
            self.detached_win.pane.enable_region(reg)
            self.detached_win.pane.set_click_mark_mode(clk)
            self.detached_win.pane.set_y_lock(yl)
            self.detached_win.pane.set_right_y_lock(y2)
            self.detached_win.pane.set_legend_visible(leg)

        # Markers
        self.plot_pane.clear_markers()
        if self.detached_win is not None:
            self.detached_win.pane.clear_markers()
        for m in self._markers.values():
            self.plot_pane.add_marker(m["x"], m["y"], m["label"])
            if self.detached_win is not None:
                self.detached_win.pane.add_marker(m["x"], m["y"], m["label"])

        self._restore_ranges_after_replot()

    def update_plot_data_with_filter(self):
        if not self.current_series:
            return

        apply_filter = self.chk_smooth.isChecked() and SCIPY_AVAILABLE
        window_len = int(self.spin_smooth_win.value())

        self._filter_token += 1
        token = self._filter_token
        self.status.showMessage("Filtreleniyor/çiziliyor...")

        series_snapshot = [dict(s) for s in (self.current_series or [])]
        worker = FilterWorker(token=token, series=series_snapshot, apply_filter=apply_filter, window_len=window_len)
        self._start_job(worker, self._on_filter_ready, self._on_worker_failed, tag="filter", cancel_tags=["filter"])

    def _on_filter_ready(self, payload: dict):
        if (payload or {}).get("cancelled"):
            return

        if payload.get("token") != self._filter_token:
            return

        display_series = payload.get("display_series", [])
        axis_mode = payload.get("axis_mode", "numeric")
        x_label = payload.get("x_label", "X")

        self._render_to_all_panes(display_series, axis_mode, x_label)
        self.status.showMessage("Hazır.", 1500)

    def on_clear_plot(self):
        self.plot_pane.clear_all()
        if self.detached_win is not None:
            self.detached_win.pane.clear_all()

        self.current_series = None
        self.cmb_fft_channel.clear()
        self.cmb_style_series.clear()
        if hasattr(self, "shift_series_tree") and self.shift_series_tree is not None:
            self.shift_series_tree.clear()
        self.clear_markers()

    # ---------------- Range ----------------
    def _sync_range_boxes(self, x0, x1):
        self.xmin.blockSignals(True)
        self.xmax.blockSignals(True)
        self.xmin.setValue(x0)
        self.xmax.setValue(x1)
        self.xmin.blockSignals(False)
        self.xmax.blockSignals(False)

    def _on_any_range_changed(self, source: str, x0: float, x1: float):
        if self._syncing_range:
            return
        self._syncing_range = True
        try:
            self._sync_range_boxes(x0, x1)

            if source == "main" and self.detached_win is not None:
                self.detached_win.pane.set_xrange(x0, x1)
            elif source == "detached":
                self.plot_pane.set_xrange(x0, x1)

            # Lazy TDMS view update (debounced)
            self._schedule_lazy_view_update(x0, x1)
        finally:
            self._syncing_range = False

    def apply_range_manual(self):
        x0, x1 = self.xmin.value(), self.xmax.value()
        self.plot_pane.set_xrange(x0, x1)
        if self.detached_win is not None:
            self.detached_win.pane.set_xrange(x0, x1)

    def autofit_y_in_range(self):
        x0, x1 = self.xmin.value(), self.xmax.value()
        self.plot_pane.autofit_y_in_range(x0, x1)
        if self.detached_win is not None:
            self.detached_win.pane.autofit_y_in_range(x0, x1)

    # ---------------- Lazy view (on-demand TDMS reading) ----------------
    def _schedule_lazy_view_update(self, x0: float, x1: float):
        if not LAZY_ENABLE or not self.current_series:
            return
        # sadece lazy kanallar varsa
        has_lazy = any(bool(s.get("lazy_meta")) for s in self.current_series)
        if not has_lazy:
            return

        try:
            x0 = float(x0)
            x1 = float(x1)
            if not (math.isfinite(x0) and math.isfinite(x1)):
                return
        except Exception:
            return

        self._lazy_view_pending = (x0, x1)
        try:
            self._lazy_view_timer.start(int(LAZY_VIEW_DEBOUNCE_MS))
        except Exception:
            pass

    def _run_lazy_view_update(self):
        if not LAZY_ENABLE or not self.current_series or not self._lazy_view_pending:
            return

        x0, x1 = self._lazy_view_pending
        self._lazy_view_pending = None

        reqs: List[dict] = []
        for s in self.current_series:
            meta = s.get("lazy_meta")
            if not isinstance(meta, dict):
                continue

            skey = s.get("style_key") or ""
            if not skey:
                continue

            # x_shift varsa plot koordinatından raw koordinata çevir
            x_shift = 0.0
            try:
                st = self.style_map.get(skey, {})
                x_shift = float(st.get("x_shift", 0.0) or 0.0)
                if not math.isfinite(x_shift):
                    x_shift = 0.0
            except Exception:
                x_shift = 0.0

            reqs.append({
                "style_key": skey,
                "path": meta.get("path"),
                "group": meta.get("group"),
                "channel": meta.get("channel"),
                "n": int(meta.get("n", 0) or 0),
                "x_mode": meta.get("x_mode", "index"),
                "x_base": float(meta.get("x_base", 0.0) or 0.0),
                "x_inc": float(meta.get("x_inc", 1.0) or 1.0),
                "x0": float(x0 - x_shift),
                "x1": float(x1 - x_shift),
            })

        if not reqs:
            return

        self._lazy_view_token += 1
        token = self._lazy_view_token

        worker = LazyViewLoadWorker(reqs, max_points=int(LAZY_VIEW_MAX_POINTS))
        self._start_job(worker, lambda payload, t=token: self._on_lazy_view_ready(t, payload), self._on_worker_failed,
                        tag="lazy_view", cancel_tags=["lazy_view"])

    def _on_lazy_view_ready(self, token: int, payload: dict):
        if (payload or {}).get("cancelled"):
            return

        if token != self._lazy_view_token:
            return

        updates = (payload or {}).get("updates", {}) or {}
        if not isinstance(updates, dict) or not updates:
            return

        changed = False
        for s in self.current_series or []:
            skey = s.get("style_key") or ""
            if skey not in updates:
                continue
            u = updates[skey]
            win = tuple(u.get("win", ())) if isinstance(u, dict) else ()
            if win and s.get("_lazy_last_win") == win:
                continue

            x = u.get("x")
            y = u.get("y")
            if x is None or y is None:
                continue

            s["x"] = x
            s["y"] = y
            if win:
                s["_lazy_last_win"] = win
            changed = True

        if changed:
            self._capture_current_ranges_for_replot()
            self.update_plot_data_with_filter()

    def _load_full_xy_for_series(self, s: dict) -> Tuple[np.ndarray, np.ndarray]:
        """FFT gibi hesaplamalarda gerektiğinde tam veriyi diskten okur (lazy kanallarda)."""
        source_mode = str(s.get("source_x_mode", s.get("x_mode", "index")) or "index")
        manual_fs_hz = self._get_plot_manual_fs_hz()

        if not bool(s.get("lazy_meta")):
            y = np.asarray(s.get("y", []), dtype=np.float64)
            x = np.asarray(s.get("x", []), dtype=np.float64)
            if manual_fs_hz is not None and source_mode == "index":
                x = np.arange(int(y.size), dtype=np.float64) / float(manual_fs_hz)
            elif source_mode == "index":
                x = np.arange(int(y.size), dtype=np.float64)
            return x, y

        meta = s.get("lazy_meta") or {}
        path = meta.get("path")
        group = meta.get("group")
        channel = meta.get("channel")
        n = int(meta.get("n", 0) or 0)
        source_base = float(s.get("source_x_base", meta.get("x_base", 0.0)) or 0.0)
        source_inc = float(s.get("source_x_inc", meta.get("x_inc", 1.0)) or 1.0)

        if not (path and group and channel and n > 0):
            return np.asarray(s.get("x", []), dtype=np.float64), np.asarray(s.get("y", []), dtype=np.float64)

        tdms = None
        try:
            tdms = TdmsFile.open(path)
            ch = tdms[group][channel]
            y = np.asarray(ch[:])
            if y.ndim != 1:
                y = np.ravel(y)
            if not y.flags['C_CONTIGUOUS']:
                y = np.ascontiguousarray(y)
            if not np.issubdtype(y.dtype, np.number):
                y = np.asarray(y, dtype=np.float64)

            nn = int(y.size)
            idx = np.arange(nn, dtype=np.float64)
            if manual_fs_hz is not None and source_mode == "index":
                x = idx / float(manual_fs_hz)
            elif source_mode == "index":
                x = idx
            else:
                x = (float(source_base) + idx * float(source_inc)).astype(np.float64)
            return x, y
        except Exception:
            return np.asarray(s.get("x", []), dtype=np.float64), np.asarray(s.get("y", []), dtype=np.float64)
        finally:
            try:
                if tdms is not None:
                    tdms.close()
            except Exception:
                pass

    # ---------------- Export & Statistics ----------------
    def _gather_plotted_xy(self, full_res: bool = True) -> List[Tuple[str, np.ndarray, np.ndarray]]:
        """Çizili kanalların (isim, x, y) listesini döndürür; X/Y shift uygulanır.

        full_res=True ise lazy kanallar için tüm veri diskten okunur.
        """
        out: List[Tuple[str, np.ndarray, np.ndarray]] = []
        for s in (self.current_series or []):
            skey = s.get("style_key") or ""
            if full_res:
                x, y = self._load_full_xy_for_series(s)
            else:
                x = np.asarray(s.get("x", []), dtype=np.float64)
                y = np.asarray(s.get("y", []), dtype=np.float64)

            st = self.style_map.get(skey, {}) if skey else {}
            x_shift = float(st.get("x_shift", 0.0) or 0.0)
            y_shift = float(st.get("y_shift", 0.0) or 0.0)
            if math.isfinite(x_shift) and x_shift != 0.0:
                x = np.asarray(x, dtype=np.float64) + x_shift
            if math.isfinite(y_shift) and y_shift != 0.0:
                y = np.asarray(y, dtype=np.float64) + y_shift

            fl = (s.get("file_label") or "").strip()
            nm = (s.get("name") or "").strip()
            name = f"{fl} | {nm}" if fl else nm
            out.append((name, np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)))
        return out

    def _active_region_xrange(self) -> Optional[Tuple[float, float]]:
        try:
            if getattr(self.plot_pane, "_region_enabled", False):
                v = self.plot_pane.region_values()
                if v and math.isfinite(v[0]) and math.isfinite(v[1]) and v[1] > v[0]:
                    return float(v[0]), float(v[1])
        except Exception:
            pass
        return None

    def export_plotted_csv(self):
        if not self.current_series:
            QMessageBox.information(self, "Veri Yok", "Önce kanalları çizdirin.")
            return

        last_dir = str(self.settings.value("paths/last_export_dir", "") or "")
        start = os.path.join(last_dir, "veri.csv") if last_dir else "veri.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Veriyi CSV Kaydet", start, "CSV (*.csv)")
        if not path:
            return

        region = self._active_region_xrange()
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            named = self._gather_plotted_xy(full_res=True)
            if region is not None:
                lo, hi = region
                clipped = []
                for name, x, y in named:
                    sel = (x >= lo) & (x <= hi)
                    if np.any(sel):
                        clipped.append((name, x[sel], y[sel]))
                named = clipped or named

            axis_mode, x_label = _axis_mode_and_label_from_series(self.current_series)
            csv_text = named_series_to_csv(named, x_label=x_label or "x")
            with open(path, "w", encoding="utf-8") as f:
                f.write(csv_text)
            self.settings.setValue("paths/last_export_dir", os.path.dirname(path))
            scope = "aralık" if region is not None else "tüm veri"
            self.status.showMessage(f"CSV kaydedildi ({scope}): {os.path.basename(path)}", 4000)
        except Exception as e:
            QMessageBox.critical(self, "Dışa Aktarma Hatası", f"CSV yazılamadı:\n{e}")
        finally:
            QApplication.restoreOverrideCursor()

    def export_plot_image(self):
        if self.plot_pane.plot is None:
            QMessageBox.information(self, "Grafik Yok", "Önce kanalları çizdirin.")
            return

        last_dir = str(self.settings.value("paths/last_export_dir", "") or "")
        start = os.path.join(last_dir, "grafik.png") if last_dir else "grafik.png"
        path, _ = QFileDialog.getSaveFileName(self, "Grafik Görüntüsü Kaydet", start, "PNG (*.png)")
        if not path:
            return

        try:
            from pyqtgraph.exporters import ImageExporter
            exporter = ImageExporter(self.plot_pane.plot.getPlotItem())
            exporter.export(path)
            self.settings.setValue("paths/last_export_dir", os.path.dirname(path))
            self.status.showMessage(f"Grafik kaydedildi: {os.path.basename(path)}", 4000)
        except Exception as e:
            QMessageBox.critical(self, "Dışa Aktarma Hatası", f"Görüntü yazılamadı:\n{e}")

    def show_statistics(self):
        if not self.current_series:
            QMessageBox.information(self, "Veri Yok", "Önce kanalları çizdirin.")
            return

        region = self._active_region_xrange()
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            named = self._gather_plotted_xy(full_res=True)
        finally:
            QApplication.restoreOverrideCursor()

        rows = []
        for name, x, y in named:
            if region is not None:
                stats = compute_channel_stats(x, y, region[0], region[1])
            else:
                stats = compute_channel_stats(x, y)
            if stats is None:
                continue
            rows.append((name, stats))

        if not rows:
            QMessageBox.information(self, "İstatistik", "İstatistik hesaplanacak geçerli veri yok.")
            return

        scope = f"Aralık [{region[0]:.6g}, {region[1]:.6g}]" if region is not None else "Tüm veri"
        header = f"{'Kanal':<28} {'N':>9} {'Min':>13} {'Max':>13} {'Ort':>13} {'Std':>13} {'RMS':>13} {'P2P':>13}"
        lines = [scope, "", header, "-" * len(header)]
        for name, st in rows:
            nm = (name[:27]) if len(name) > 27 else name
            lines.append(
                f"{nm:<28} {st['count']:>9d} {st['min']:>13.6g} {st['max']:>13.6g} "
                f"{st['mean']:>13.6g} {st['std']:>13.6g} {st['rms']:>13.6g} {st['p2p']:>13.6g}"
            )

        dlg = QMessageBox(self)
        dlg.setWindowTitle("İstatistikler")
        dlg.setIcon(QMessageBox.Icon.Information)
        dlg.setText("Çizilen kanal istatistikleri:")
        dlg.setDetailedText("\n".join(lines))
        try:
            dlg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        except Exception:
            pass
        dlg.exec()

    # ---------------- Markers ----------------
    def clear_markers(self):
        self.plot_pane.clear_markers()
        if self.detached_win is not None:
            self.detached_win.pane.clear_markers()
        self.marker_list.clear()
        self._markers.clear()
        self._marker_counter = 0
        self.lbl_marker_calc.setText("Hesap: —")

    def _on_marker_requested_from(self, source: str, x: float, y: float):
        self._marker_counter += 1
        mid = self._marker_counter
        label = f"İşaretçi {mid}"

        self._markers[mid] = {"id": mid, "label": label, "x": float(x), "y": float(y)}

        axis_mode = self.plot_pane.axis_mode
        x_str = _format_x_for_display(axis_mode, float(x))
        y_str = f"{float(y):.12g}"

        it = QTreeWidgetItem([label, x_str, y_str])
        it.setData(0, Qt.ItemDataRole.UserRole, mid)
        self.marker_list.addTopLevelItem(it)

        self.plot_pane.add_marker(float(x), float(y), label)
        if self.detached_win is not None:
            self.detached_win.pane.add_marker(float(x), float(y), label)

    def _on_marker_double_clicked(self, item: QTreeWidgetItem, _col: int):
        mid = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(mid, int) or mid not in self._markers:
            return
        x = float(self._markers[mid]["x"])
        self.plot_pane.jump_to_x(x)
        if self.detached_win is not None:
            self.detached_win.pane.jump_to_x(x)

    def _selected_two_markers(self) -> Optional[Tuple[dict, dict]]:
        items = self.marker_list.selectedItems()
        if len(items) != 2:
            QMessageBox.information(self, "Seçim", "Lütfen CTRL ile tam 2 işaretçi seçin.")
            return None

        ms = []
        for it in items:
            mid = it.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(mid, int) and mid in self._markers:
                ms.append(self._markers[mid])

        if len(ms) != 2:
            QMessageBox.information(self, "Seçim", "Geçerli 2 işaretçi seçilemedi.")
            return None

        ms.sort(key=lambda m: m["id"])
        return ms[0], ms[1]

    def _marker_math_add(self):
        pair = self._selected_two_markers()
        if not pair:
            return
        a, b = pair

        mode = self.cmb_marker_calc_axis.currentText()
        if mode == "Sadece X":
            sx = float(a["x"]) + float(b["x"])
            self.lbl_marker_calc.setText(f"Hesap: {a['label']} + {b['label']}  | X={sx:.12g}")
        elif mode == "Sadece Y":
            sy = float(a["y"]) + float(b["y"])
            self.lbl_marker_calc.setText(f"Hesap: {a['label']} + {b['label']}  |  Y={sy:.12g}")
        else:
            sx = float(a["x"]) + float(b["x"])
            sy = float(a["y"]) + float(b["y"])
            self.lbl_marker_calc.setText(f"Hesap: {a['label']} + {b['label']}  |  X={sx:.12g}  Y={sy:.12g}")

    def _marker_math_sub(self):
        pair = self._selected_two_markers()
        if not pair:
            return
        a, b = pair  # B - A

        mode = self.cmb_marker_calc_axis.currentText()
        if mode == "Sadece X":
            dx = float(b["x"]) - float(a["x"])
            self.lbl_marker_calc.setText(f"Hesap: {b['label']} - {a['label']}  |  ΔX={dx:.12g}")
        elif mode == "Sadece Y":
            dy = float(b["y"]) - float(a["y"])
            self.lbl_marker_calc.setText(f"Hesap: {b['label']} - {a['label']}  |  ΔY={dy:.12g}")
        else:
            dx = float(b["x"]) - float(a["x"])
            dy = float(b["y"]) - float(a["y"])
            self.lbl_marker_calc.setText(f"Hesap: {b['label']} - {a['label']}  | ΔX={dx:.12g}  ΔY={dy:.12g}")

    # ---------------- FFT ----------------
    def _fft_curve_pen(self):
        fg = _bg_to_fg_rgb(self.plot_bg_rgb)
        if fg == (255, 255, 255):
            return pg.mkPen((114, 180, 255), width=1.8)
        return pg.mkPen((0, 92, 175), width=1.8)

    def _fft_peak_pen(self):
        fg = _bg_to_fg_rgb(self.plot_bg_rgb)
        if fg == (255, 255, 255):
            return pg.mkPen((255, 196, 87), width=1.2, style=Qt.PenStyle.DashLine)
        return pg.mkPen((190, 95, 0), width=1.2, style=Qt.PenStyle.DashLine)

    def _fft_peak_brush(self):
        fg = _bg_to_fg_rgb(self.plot_bg_rgb)
        return pg.mkBrush(255, 196, 87, 210) if fg == (255, 255, 255) else pg.mkBrush(200, 110, 20, 210)

    def clear_fft_plot(self):
        self._last_fft_payload = None
        self.fft_plot.clear()
        apply_plotwidget_theme(self.fft_plot, self.plot_bg_rgb)
        self.fft_plot.setLabel("bottom", "Frekans (Hz)")
        self.fft_plot.setLabel("left", "Genlik")
        try:
            self.fft_plot.getPlotItem().setTitle("")
        except Exception:
            pass
        if hasattr(self, "lbl_fft_info"):
            self.lbl_fft_info.setText("FFT için bir kanal seçip hesaplama başlatın.")

    def _rerender_fft_plot(self, *_):
        if getattr(self, "_last_fft_payload", None):
            self._render_fft_payload(self._last_fft_payload)

    def _render_fft_payload(self, p: dict):
        if not p:
            return

        freq = np.asarray(p.get("freq", []), dtype=np.float64)
        use_psd = bool(self.chk_fft_psd.isChecked()) if hasattr(self, "chk_fft_psd") else False
        if use_psd and p.get("psd_linear") is not None:
            mag_linear = np.asarray(p.get("psd_linear", []), dtype=np.float64)
            mag_kind = "psd"
        else:
            mag_linear = np.asarray(p.get("mag_linear", []), dtype=np.float64)
            mag_kind = "amp"
        if freq.size == 0 or mag_linear.size == 0:
            self.clear_fft_plot()
            return

        fmin = float(self.sp_fft_fmin.value()) if hasattr(self, "sp_fft_fmin") else 0.0
        fmax = float(self.sp_fft_fmax.value()) if hasattr(self, "sp_fft_fmax") else 0.0
        hide_dc = bool(self.chk_fft_hide_dc.isChecked()) if hasattr(self, "chk_fft_hide_dc") else False
        use_log = bool(self.chk_fft_log.isChecked()) if hasattr(self, "chk_fft_log") else False

        mask = np.isfinite(freq) & np.isfinite(mag_linear)
        if hide_dc:
            mask &= (freq > 0.0)
        if fmin > 0:
            mask &= (freq >= fmin)
        if fmax > 0:
            mask &= (freq <= fmax)

        freq_p = freq[mask]
        mag_linear_p = mag_linear[mask]

        self.fft_plot.clear()
        apply_plotwidget_theme(self.fft_plot, self.plot_bg_rgb)

        if freq_p.size == 0 or mag_linear_p.size == 0:
            self.fft_plot.setLabel("bottom", "Frekans (Hz)")
            self.fft_plot.setLabel("left", "Genlik")
            try:
                self.fft_plot.getPlotItem().setTitle("<b>FFT</b> — Seçilen frekans aralığında veri yok")
            except Exception:
                pass
            self.lbl_fft_info.setText(
                "Seçilen filtreleme ile gösterilecek spektrum kalmadı. Min/Max f veya 0 Hz gizleme seçeneklerini kontrol edin."
            )
            return

        if mag_kind == "psd":
            if use_log:
                mag_plot = 10.0 * np.log10(np.maximum(mag_linear_p, 1e-20))
                ylab = "PSD (dB/Hz)"
            else:
                mag_plot = mag_linear_p
                ylab = "PSD (birim²/Hz)"
        else:
            if use_log:
                mag_plot = 20.0 * np.log10(np.maximum(mag_linear_p, 1e-20))
                ylab = "Genlik (dB)"
            else:
                mag_plot = mag_linear_p
                ylab = "Genlik"

        curve = self.fft_plot.plot(freq_p, mag_plot, pen=self._fft_curve_pen())
        try:
            curve.setClipToView(True)
        except Exception:
            pass
        try:
            curve.setDownsampling(auto=True, method='peak')
        except Exception:
            pass
        try:
            curve.setSkipFiniteCheck(True)
        except Exception:
            pass

        self.fft_plot.setLabel("bottom", "Frekans (Hz)")
        self.fft_plot.setLabel("left", ylab)
        shown_fmin = float(freq_p[0])
        shown_fmax = float(freq_p[-1])
        title = (
            f"<b>{p['name']}</b>"
            f" &nbsp;&nbsp;|&nbsp;&nbsp; Fs={p['fs']:.6g} Hz"
            f" &nbsp;&nbsp;|&nbsp;&nbsp; Δf={p['df']:.6g} Hz"
            f" &nbsp;&nbsp;|&nbsp;&nbsp; N={p['n']}"
            f" &nbsp;&nbsp;|&nbsp;&nbsp; Aralık: {shown_fmin:.6g} - {shown_fmax:.6g} Hz"
        )
        try:
            self.fft_plot.getPlotItem().setTitle(title)
        except Exception:
            pass

        xr = padded_minmax(freq_p, pad_ratio=0.01)
        yr = padded_minmax(mag_plot, pad_ratio=0.08)
        if xr is not None:
            self.fft_plot.setXRange(xr[0], xr[1], padding=0.0)
        if yr is not None:
            self.fft_plot.setYRange(yr[0], yr[1], padding=0.0)

        peak_text = "Baskın pik kapalı"
        if bool(self.chk_fft_peak.isChecked()) and freq_p.size > 0:
            try:
                peak_idx = int(np.nanargmax(mag_linear_p))
                peak_freq = float(freq_p[peak_idx])
                peak_mag_linear = float(mag_linear_p[peak_idx])
                peak_mag_plot = float(mag_plot[peak_idx])
                vline = pg.InfiniteLine(pos=peak_freq, angle=90, pen=self._fft_peak_pen(), movable=False)
                self.fft_plot.addItem(vline)
                scatter = pg.ScatterPlotItem(
                    [peak_freq], [peak_mag_plot],
                    size=8,
                    pen=pg.mkPen(0, 0, 0, 0),
                    brush=self._fft_peak_brush(),
                )
                self.fft_plot.addItem(scatter)
                peak_kind_lbl = "PSD" if mag_kind == "psd" else "Genlik"
                peak_text = f"Baskın pik: {peak_freq:.6g} Hz | {peak_kind_lbl}: {peak_mag_linear:.6g}"
            except Exception:
                peak_text = "Baskın pik hesaplanamadı"

        preprocess = []
        if p.get("detrend_linear"):
            preprocess.append("lineer detrend")
        elif p.get("remove_mean"):
            preprocess.append("ortalama kaldırıldı")
        if p.get("use_window"):
            preprocess.append(p.get("window_name", "pencere"))
        if not preprocess:
            preprocess.append("ham sinyal")

        info_lines = [
            f"<b>Kanal:</b> {p['name']}",
            f"<b>Örnekleme:</b> {p['fs']:.6g} Hz &nbsp;&nbsp; <b>N:</b> {p['n']} &nbsp;&nbsp; <b>Çözünürlük:</b> {p['df']:.6g} Hz",
            f"<b>Ön işleme:</b> {', '.join(preprocess)}",
            f"<b>Gösterilen aralık:</b> {shown_fmin:.6g} - {shown_fmax:.6g} Hz &nbsp;&nbsp; <b>Ölçek:</b> {ylab}",
            f"<b>{peak_text}</b>",
        ]
        self.lbl_fft_info.setText("<br>".join(info_lines))

    def compute_fft(self):
        if not self.current_series:
            return
        idx = self.cmb_fft_channel.currentIndex()
        if idx < 0:
            return
        skey = self.cmb_fft_channel.itemData(idx, role=Qt.ItemDataRole.UserRole)
        if not isinstance(skey, str):
            return

        chosen = None
        for s in self.current_series:
            if s.get("style_key") == skey:
                chosen = s
                break
        if not chosen:
            return

        x, y = self._load_full_xy_for_series(chosen)
        if y is None or len(y) == 0:
            return

        if self.chk_smooth.isChecked() and SCIPY_AVAILABLE and not bool(chosen.get("is_digital", False)):
            y = apply_savgol_safe(y, window_len=int(self.spin_smooth_win.value()), polyorder_hint=3)

        fs_hint = float(self.fs_fft.value())
        fs_hint = None if fs_hint <= 0 else fs_hint

        name = f"{chosen.get('file_label', '')} | {chosen.get('name', '')}".strip(" |")
        self.lbl_fft_info.setText("FFT hesaplanıyor...")

        worker = FFTWorker(
            name=name,
            x=x,
            y=y,
            fs_hint=fs_hint,
            use_window=bool(self.chk_fft_window.isChecked()),
            remove_mean=bool(self.chk_fft_remove_mean.isChecked()),
            detrend_linear=bool(self.chk_fft_detrend.isChecked())
        )
        self._start_job(worker, self._on_fft_ready, self._on_worker_failed, tag="fft", cancel_tags=["fft"])

    def _on_fft_ready(self, p: dict):
        if (p or {}).get("cancelled"):
            return
        self._last_fft_payload = p or None
        self._render_fft_payload(p)
        self.tabs.setCurrentIndex(1)

    def export_fft_csv(self):
        p = getattr(self, "_last_fft_payload", None)
        if not p:
            QMessageBox.information(self, "FFT Yok", "Önce bir FFT hesaplayın.")
            return

        freq = np.asarray(p.get("freq", []), dtype=np.float64)
        mag = np.asarray(p.get("mag_linear", []), dtype=np.float64)
        psd = np.asarray(p.get("psd_linear", []), dtype=np.float64)
        if freq.size == 0 or mag.size == 0:
            QMessageBox.information(self, "FFT Yok", "Dışa aktarılacak spektrum verisi yok.")
            return

        last_dir = str(self.settings.value("paths/last_export_dir", "") or "")
        start = os.path.join(last_dir, "fft.csv") if last_dir else "fft.csv"
        path, _ = QFileDialog.getSaveFileName(self, "FFT CSV Kaydet", start, "CSV (*.csv)")
        if not path:
            return

        try:
            n = freq.size
            has_psd = psd.size == n
            lines = ["frequency_hz,amplitude" + (",psd" if has_psd else "")]
            for i in range(n):
                if has_psd:
                    lines.append(f"{freq[i]:.12g},{mag[i]:.12g},{psd[i]:.12g}")
                else:
                    lines.append(f"{freq[i]:.12g},{mag[i]:.12g}")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self.settings.setValue("paths/last_export_dir", os.path.dirname(path))
            self.status.showMessage(f"FFT CSV kaydedildi: {os.path.basename(path)}", 4000)
        except Exception as e:
            QMessageBox.critical(self, "Dışa Aktarma Hatası", f"FFT CSV yazılamadı:\n{e}")

    # ---------------- Drag & Drop ----------------
    def dragEnterEvent(self, e):
        try:
            md = e.mimeData()
            if md is not None and md.hasUrls():
                for url in md.urls():
                    try:
                        p = url.toLocalFile()
                        if p and p.lower().endswith(".tdms"):
                            e.acceptProposedAction()
                            return
                    except Exception:
                        pass
        except Exception:
            pass
        e.ignore()

    def dropEvent(self, e):
        paths = []
        try:
            md = e.mimeData()
            if md is not None and md.hasUrls():
                for url in md.urls():
                    try:
                        p = url.toLocalFile()
                        if p and p.lower().endswith(".tdms"):
                            paths.append(p)
                    except Exception:
                        pass
        except Exception:
            pass

        if paths:
            self.open_tdms_paths(paths)
            try:
                e.acceptProposedAction()
            except Exception:
                pass
        else:
            e.ignore()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(LIGHT_QSS)  # default theme: light
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
