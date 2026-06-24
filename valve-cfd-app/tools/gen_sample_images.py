#!/usr/bin/env python3
"""Örnek CFD görselleri (SVG) üretir.

Bu betik yalnızca prototip için temsili (placeholder) kontur/akım-çizgisi
görselleri oluşturur. Gerçek kullanımda bu görsellerin yerine CFD
yazılımından alınan PNG/JPG dosyaları konur.
"""
import math
import os
import random

OUT = os.path.join(os.path.dirname(__file__), "..", "images")
os.makedirs(OUT, exist_ok=True)

W, H = 640, 400

# Renk skalaları (düşük -> yüksek)
SCALES = {
    "pressure": ["#0b1d51", "#1f6feb", "#2dd4bf", "#fde047", "#f97316", "#dc2626"],
    "velocity": ["#0f172a", "#312e81", "#7c3aed", "#db2777", "#f59e0b", "#fef08a"],
}


def lerp(a, b, t):
    return a + (b - a) * t


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, c))) for c in rgb)


def sample_scale(scale, t):
    stops = SCALES[scale]
    t = max(0.0, min(1.0, t))
    pos = t * (len(stops) - 1)
    i = int(pos)
    f = pos - i
    if i >= len(stops) - 1:
        return stops[-1]
    a = hex_to_rgb(stops[i])
    b = hex_to_rgb(stops[i + 1])
    return rgb_to_hex(tuple(lerp(a[k], b[k], f) for k in range(3)))


def contour_svg(seed, scale, title):
    rnd = random.Random(seed)
    # Birkaç "kaynak" nokta etrafında alan değeri üret -> bantlı kontur görünümü
    sources = [(rnd.uniform(0.15, 0.85) * W, rnd.uniform(0.15, 0.85) * H,
                rnd.uniform(0.4, 1.0)) for _ in range(rnd.randint(3, 5))]
    cell = 16
    rects = []
    for gy in range(0, H, cell):
        for gx in range(0, W, cell):
            cx, cy = gx + cell / 2, gy + cell / 2
            val = 0.0
            for (sx, sy, w) in sources:
                d = math.hypot(cx - sx, cy - sy)
                val += w * math.exp(-(d * d) / (2 * (90 ** 2)))
            t = max(0.0, min(1.0, val))
            # banding -> kontur hissi
            t = round(t * 8) / 8
            rects.append(f'<rect x="{gx}" y="{gy}" width="{cell}" height="{cell}" '
                         f'fill="{sample_scale(scale, t)}"/>')
    legend = "".join(
        f'<rect x="{600}" y="{40 + i*40}" width="22" height="40" '
        f'fill="{SCALES[scale][len(SCALES[scale])-1-i]}"/>'
        for i in range(len(SCALES[scale]))
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<rect width="{W}" height="{H}" fill="#020617"/>
{''.join(rects)}
<rect x="0" y="0" width="{W}" height="{H}" fill="none" stroke="#1e293b" stroke-width="2"/>
{legend}
<text x="16" y="28" font-family="Segoe UI, Arial" font-size="18" fill="#e2e8f0" font-weight="700">{title}</text>
</svg>'''


def streamlines_svg(seed, title):
    rnd = random.Random(seed)
    paths = []
    for i in range(26):
        y0 = rnd.uniform(20, H - 20)
        amp = rnd.uniform(10, 60)
        ph = rnd.uniform(0, 6.28)
        pts = []
        for x in range(0, W + 1, 20):
            y = y0 + amp * math.sin(x / 70 + ph) * (x / W)
            pts.append(f"{x},{y:.1f}")
        col = sample_scale("velocity", i / 26)
        paths.append(f'<polyline points="{" ".join(pts)}" fill="none" '
                     f'stroke="{col}" stroke-width="1.6" opacity="0.85"/>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<rect width="{W}" height="{H}" fill="#020617"/>
{''.join(paths)}
<rect x="0" y="0" width="{W}" height="{H}" fill="none" stroke="#1e293b" stroke-width="2"/>
<text x="16" y="28" font-family="Segoe UI, Arial" font-size="18" fill="#e2e8f0" font-weight="700">{title}</text>
</svg>'''


def write(name, content):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(content)
    print("wrote", name)


# Her vana kaydı icin 3 gorsel: basinc, hiz, akim cizgileri
CASES = [
    "btf_dn100", "btf_dn200", "btf_dn300",
    "ball_dn050", "ball_dn100",
    "globe_dn080", "globe_dn150",
    "ctrl_dn100_25", "ctrl_dn100_50", "ctrl_dn100_75", "ctrl_dn100_100",
    "check_dn200",
]

for idx, c in enumerate(CASES):
    write(f"{c}_pressure.svg", contour_svg(idx * 3 + 1, "pressure", "Basınç [Pa]"))
    write(f"{c}_velocity.svg", contour_svg(idx * 3 + 2, "velocity", "Hız [m/s]"))
    write(f"{c}_stream.svg", streamlines_svg(idx * 3 + 3, "Akım Çizgileri"))

print("done")
