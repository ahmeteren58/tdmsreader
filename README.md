# tdmsreader

A fast desktop viewer/analyzer for National Instruments **TDMS** files, built with
PyQt6 + pyqtgraph. It aims to provide an NI TDMS Reader–like experience with
interactive plotting, signal smoothing, FFT/PSD analysis, markers and data export.

## Features

- **Multi-file loading** with per-file channel trees, search and drag-and-drop.
- **Lazy loading** for very large channels: only an overview is loaded initially,
  and the visible window is re-read from disk on zoom/pan (debounced, off-thread).
- **Interactive plot** (pan/zoom, autofit, region selector, crosshair cursor),
  with a separate right axis for digital channels and optional Y / Y2 axis locks.
- **Detached plot window** kept in sync with the main view.
- **Savitzky–Golay smoothing**, per-channel color/width styling and X/Y shifts.
- **Markers** with two-marker add/subtract math (X, Y, or both).
- **Frequency analysis**: single-sided amplitude spectrum and **PSD** (power
  spectral density), Hanning window, mean removal, linear detrend, dominant-peak
  marking and frequency-range limiting.
- **Export**: plotted data to **CSV** (full resolution, wide or long format),
  the spectrum to **CSV**, and the plot to a **PNG** image.
- **Statistics**: count / min / max / mean / std / RMS / peak-to-peak per channel,
  computed over the full record or the active region.
- **Recent files**, light/dark themes, custom plot background and persisted UI state.

## Install

```bash
pip install -r requirements.txt
```

On headless Linux you may also need Qt runtime libraries (e.g. `libegl1`, `libgl1`,
`libxkbcommon0`, the `libxcb*` packages and `libfontconfig1`).

## Run

```bash
python tdms_reader.py
```

Then use **Dosya → TDMS Aç...** (Ctrl+O) or drag a `.tdms` file onto the window,
check the channels you want, and click **İşaretli Kanalları Çizdir**.

### Useful shortcuts

| Shortcut | Action |
| --- | --- |
| Ctrl+O | Open a TDMS file |
| Ctrl+W | Close the current file tab |
| Ctrl+E | Export plotted data to CSV |
| Ctrl+I | Show channel statistics |

### Environment variables

- `TDMSREADER_LOGLEVEL` — logging level (e.g. `DEBUG`, `INFO`).
- `TDMSREADER_ICON_DIR` — optional folder with quickbar icons.
