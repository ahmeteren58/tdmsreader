/* Vana CFD Kataloğu - Uygulama Mantığı (sunucusuz, offline) */
(function () {
  "use strict";

  const DATA = Array.isArray(window.VALVE_DATA) ? window.VALVE_DATA : [];

  const state = {
    search: "",
    types: new Set(),
    dns: new Set(),
    fluids: new Set(),
    openingMin: 0,
    openingMax: 100,
    cavOnly: false,
    sort: "name",
    selected: new Set(),
  };

  // --- Yardımcılar ---
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const fmt = (v, d = 2) =>
    v === null || v === undefined || v === ""
      ? "–"
      : typeof v === "number"
      ? (Number.isInteger(v) ? v.toLocaleString("tr-TR") : v.toLocaleString("tr-TR", { maximumFractionDigits: d }))
      : v;
  const sci = (v) => (typeof v === "number" ? v.toExponential(1).replace("+", "") : v);
  const byId = (id) => DATA.find((d) => d.id === id);
  const isCav = (d) => typeof d.results.cavitationIndex === "number" && d.results.cavitationIndex < 1.5;

  // --- Filtre seçeneklerini doldur ---
  function uniqueSorted(getter, numeric) {
    const set = new Set(DATA.map(getter));
    const arr = Array.from(set);
    arr.sort(numeric ? (a, b) => a - b : (a, b) => String(a).localeCompare(String(b), "tr"));
    return arr;
  }

  function buildChips(container, values, stateSet, label) {
    container.innerHTML = "";
    values.forEach((val) => {
      const chip = document.createElement("div");
      chip.className = "chip";
      chip.textContent = label ? label(val) : val;
      chip.addEventListener("click", () => {
        if (stateSet.has(val)) {
          stateSet.delete(val);
          chip.classList.remove("active");
        } else {
          stateSet.add(val);
          chip.classList.add("active");
        }
        render();
      });
      container.appendChild(chip);
    });
  }

  // --- Filtreleme ---
  function applyFilters() {
    let rows = DATA.filter((d) => {
      if (state.types.size && !state.types.has(d.type)) return false;
      if (state.dns.size && !state.dns.has(d.dn)) return false;
      if (state.fluids.size && !state.fluids.has(d.fluid)) return false;
      if (d.opening < state.openingMin || d.opening > state.openingMax) return false;
      if (state.cavOnly && !isCav(d)) return false;
      if (state.search) {
        const q = state.search.toLowerCase();
        const hay = [d.name, d.type, "dn" + d.dn, d.fluid, d.pressureClass, d.notes]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    const s = state.sort;
    rows.sort((a, b) => {
      const av = s === "name" ? a.name : s === "dn" ? a.dn : a.results[s];
      const bv = s === "name" ? b.name : s === "dn" ? b.dn : b.results[s];
      if (typeof av === "string") return av.localeCompare(bv, "tr");
      return (bv ?? 0) - (av ?? 0); // sayısallarda büyükten küçüğe
    });
    return rows;
  }

  // --- Kart oluşturma ---
  function cardHTML(d) {
    const thumb = d.images && d.images[0] ? d.images[0].src : "";
    const cav = isCav(d);
    const checked = state.selected.has(d.id) ? "checked" : "";
    const selClass = state.selected.has(d.id) ? "selected" : "";
    return `
      <article class="card ${selClass}" data-id="${d.id}">
        <div class="card-thumb" data-open="${d.id}">
          ${thumb ? `<img src="${thumb}" alt="${d.name}" loading="lazy" />` : ""}
          <span class="card-type-badge">${d.type}</span>
          <label class="card-select" title="Karşılaştırma/Sunum için seç" onclick="event.stopPropagation()">
            <input type="checkbox" data-select="${d.id}" ${checked} />
          </label>
        </div>
        <div class="card-body">
          <div class="card-title" data-open="${d.id}">${d.name}</div>
          <div class="card-meta">
            <span>DN${d.dn}</span>
            <span>${d.pressureClass}</span>
            <span>Açıklık %${d.opening}</span>
            <span>${d.fluid}</span>
          </div>
          <div class="metrics">
            <div class="metric"><div class="m-label">Cv</div><div class="m-value">${fmt(d.results.cv)}</div></div>
            <div class="metric"><div class="m-label">ΔP (bar)</div><div class="m-value">${fmt(d.results.deltaP)}</div></div>
            <div class="metric"><div class="m-label">K faktörü</div><div class="m-value">${fmt(d.results.kFactor)}</div></div>
            <div class="metric"><div class="m-label">Kavitasyon σ</div>
              <div class="m-value ${cav ? "tag-cav" : "tag-ok"}">${fmt(d.results.cavitationIndex)}</div></div>
          </div>
        </div>
      </article>`;
  }

  function render() {
    const rows = applyFilters();
    const grid = $("#grid");
    grid.innerHTML = rows.map(cardHTML).join("");
    $("#count").textContent = rows.length;
    $("#empty").classList.toggle("hidden", rows.length > 0);

    grid.querySelectorAll("[data-open]").forEach((el) =>
      el.addEventListener("click", () => openDetail(el.getAttribute("data-open")))
    );
    grid.querySelectorAll("[data-select]").forEach((el) =>
      el.addEventListener("change", (e) => {
        const id = el.getAttribute("data-select");
        if (e.target.checked) state.selected.add(id);
        else state.selected.delete(id);
        updateSelectionUI();
        el.closest(".card").classList.toggle("selected", e.target.checked);
      })
    );
    updateSelectionUI();
  }

  function updateSelectionUI() {
    const n = state.selected.size;
    $("#sel-count").textContent = n;
    $("#compare-btn").disabled = n < 2;
    $("#pptx-btn").disabled = n < 1;
  }

  // --- Detay modalı ---
  function specRow(label, value) {
    return `<tr><th>${label}</th><td>${value}</td></tr>`;
  }

  function openDetail(id) {
    const d = byId(id);
    if (!d) return;
    const r = d.results;
    const gallery = (d.images || [])
      .map(
        (img) => `<figure>
          <img src="${img.src}" alt="${img.label}" data-zoom="${img.src}" />
          <figcaption>${img.label}</figcaption>
        </figure>`
      )
      .join("");

    $("#modal-content").innerHTML = `
      <div class="detail-head">
        <div>
          <h2>${d.name}</h2>
          <div class="detail-sub">${d.type} · DN${d.dn} · ${d.pressureClass} · Analiz: ${fmt(d.analysisDate)}</div>
        </div>
        <span class="card-type-badge">${isCav(d) ? "⚠ Kavitasyon riski" : "✓ Güvenli"}</span>
      </div>

      <div class="detail-gallery">${gallery}</div>

      <div class="spec-grid">
        <div>
          <div class="section-title">Çalışma Koşulları</div>
          <table class="spec-table">
            ${specRow("Akışkan", d.fluid)}
            ${specRow("Sıcaklık (°C)", fmt(d.temperature))}
            ${specRow("Açıklık (%)", fmt(d.opening))}
            ${specRow("Giriş Basıncı (bar)", fmt(d.inletPressure))}
            ${specRow("Çıkış Basıncı (bar)", fmt(d.outletPressure))}
            ${specRow("Debi (m³/h)", fmt(d.flowRate))}
            ${specRow("Reynolds", sci(d.reynolds))}
          </table>
        </div>
        <div>
          <div class="section-title">CFD Sonuçları</div>
          <table class="spec-table">
            ${specRow("Cv", fmt(r.cv))}
            ${specRow("Kv", fmt(r.kv))}
            ${specRow("Basınç Düşümü ΔP (bar)", fmt(r.deltaP))}
            ${specRow("Kayıp Katsayısı K (ζ)", fmt(r.kFactor))}
            ${specRow("Kavitasyon İndeksi σ", `<span class="${isCav(d) ? "tag-cav" : "tag-ok"}">${fmt(r.cavitationIndex)}</span>`)}
            ${specRow("Tork (Nm)", fmt(r.torque))}
            ${specRow("Kütle Debisi (kg/s)", fmt(r.massFlow))}
          </table>
        </div>
      </div>

      ${d.notes ? `<div class="section-title">Notlar</div><div class="notes-box">${d.notes}</div>` : ""}
    `;
    $("#modal").classList.remove("hidden");
    $("#modal-content")
      .querySelectorAll("[data-zoom]")
      .forEach((img) => img.addEventListener("click", () => openLightbox(img.getAttribute("data-zoom"))));
  }

  // --- Lightbox ---
  function openLightbox(src) {
    $("#lightbox-img").src = src;
    $("#lightbox").classList.remove("hidden");
  }

  // --- Karşılaştırma ---
  let compareCharts = [];
  function openCompare() {
    const items = Array.from(state.selected).map(byId).filter(Boolean);
    if (items.length < 2) return;

    const rowsDef = [
      ["Tip", (d) => d.type],
      ["DN", (d) => d.dn],
      ["Basınç Sınıfı", (d) => d.pressureClass],
      ["Açıklık (%)", (d) => fmt(d.opening)],
      ["Akışkan", (d) => d.fluid],
      ["Debi (m³/h)", (d) => fmt(d.flowRate)],
      ["Cv", (d) => fmt(d.results.cv)],
      ["Kv", (d) => fmt(d.results.kv)],
      ["ΔP (bar)", (d) => fmt(d.results.deltaP)],
      ["K faktörü", (d) => fmt(d.results.kFactor)],
      ["Kavitasyon σ", (d) => `<span class="${isCav(d) ? "tag-cav" : "tag-ok"}">${fmt(d.results.cavitationIndex)}</span>`],
      ["Tork (Nm)", (d) => fmt(d.results.torque)],
    ];

    const head = `<tr><th>Özellik</th>${items.map((d) => `<th>${d.name}</th>`).join("")}</tr>`;
    const body = rowsDef
      .map(([label, fn]) => `<tr><td>${label}</td>${items.map((d) => `<td>${fn(d)}</td>`).join("")}</tr>`)
      .join("");

    $("#compare-content").innerHTML = `
      <div style="overflow-x:auto">
        <table class="compare-table"><thead>${head}</thead><tbody>${body}</tbody></table>
      </div>
      <div class="chart-row">
        <div class="chart-wrap"><canvas id="chart-cv"></canvas></div>
        <div class="chart-wrap"><canvas id="chart-k"></canvas></div>
      </div>
    `;
    $("#compare-modal").classList.remove("hidden");

    compareCharts.forEach((c) => c.destroy());
    compareCharts = [];
    const labels = items.map((d) => d.name);
    const mk = (canvasId, title, getter, color) =>
      new Chart(document.getElementById(canvasId), {
        type: "bar",
        data: { labels, datasets: [{ label: title, data: items.map(getter), backgroundColor: color }] },
        options: {
          responsive: true,
          plugins: { legend: { labels: { color: "#cbd5e1" } }, title: { display: true, text: title, color: "#e2e8f0" } },
          scales: {
            x: { ticks: { color: "#93a4bd", maxRotation: 35, minRotation: 0 }, grid: { color: "#243048" } },
            y: { ticks: { color: "#93a4bd" }, grid: { color: "#243048" } },
          },
        },
      });
    compareCharts.push(mk("chart-cv", "Cv Karşılaştırması", (d) => d.results.cv, "#2dd4bf"));
    compareCharts.push(mk("chart-k", "K Faktörü Karşılaştırması", (d) => d.results.kFactor, "#f59e0b"));
  }

  // --- PPTX Sunum (tamamen tarayıcıda, offline) ---
  function svgToPng(src) {
    return new Promise((resolve) => {
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => {
        const canvas = document.createElement("canvas");
        canvas.width = img.naturalWidth || 640;
        canvas.height = img.naturalHeight || 400;
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#020617";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        try {
          resolve(canvas.toDataURL("image/png"));
        } catch (e) {
          resolve(null);
        }
      };
      img.onerror = () => resolve(null);
      img.src = src;
    });
  }

  async function exportPPTX() {
    const items = Array.from(state.selected).map(byId).filter(Boolean);
    if (!items.length) return;
    const btn = $("#pptx-btn");
    const oldText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Hazırlanıyor...";

    try {
      const pptx = new PptxGenJS();
      pptx.defineLayout({ name: "WIDE", width: 13.33, height: 7.5 });
      pptx.layout = "WIDE";
      const NAVY = "0B1120";
      const TEAL = "2DD4BF";
      const WHITE = "FFFFFF";
      const MUTED = "93A4BD";

      // Kapak
      const cover = pptx.addSlide();
      cover.background = { color: NAVY };
      cover.addText("Vana CFD Analiz Sonuçları", { x: 0.7, y: 2.4, w: 12, h: 1, fontSize: 40, bold: true, color: WHITE });
      cover.addText(`${items.length} vana · ${new Date().toLocaleDateString("tr-TR")}`, {
        x: 0.7, y: 3.5, w: 12, h: 0.6, fontSize: 18, color: TEAL,
      });

      for (const d of items) {
        const slide = pptx.addSlide();
        slide.background = { color: NAVY };
        slide.addText(d.name, { x: 0.5, y: 0.3, w: 12.3, h: 0.6, fontSize: 26, bold: true, color: WHITE });
        slide.addText(`${d.type} · DN${d.dn} · ${d.pressureClass} · Açıklık %${d.opening} · ${d.fluid}`, {
          x: 0.5, y: 0.95, w: 12.3, h: 0.4, fontSize: 13, color: MUTED,
        });

        // Görsel (ilk görsel - basınç konturu)
        const png = d.images && d.images[0] ? await svgToPng(d.images[0].src) : null;
        if (png) {
          slide.addImage({ data: png, x: 0.5, y: 1.6, w: 6.4, h: 4.0 });
          slide.addText(d.images[0].label, { x: 0.5, y: 5.6, w: 6.4, h: 0.3, fontSize: 11, color: MUTED, align: "center" });
        }

        // Sonuç tablosu
        const r = d.results;
        const rows = [
          [{ text: "Parametre", options: { bold: true, color: WHITE, fill: { color: "1C2740" } } },
           { text: "Değer", options: { bold: true, color: WHITE, fill: { color: "1C2740" } } }],
          ["Cv", fmt(r.cv)],
          ["Kv", fmt(r.kv)],
          ["ΔP (bar)", fmt(r.deltaP)],
          ["K faktörü (ζ)", fmt(r.kFactor)],
          ["Kavitasyon σ", fmt(r.cavitationIndex)],
          ["Tork (Nm)", fmt(r.torque)],
          ["Debi (m³/h)", fmt(d.flowRate)],
        ].map((row) =>
          row.map((c) =>
            typeof c === "object" ? c : { text: String(c), options: { color: "E6EDF6" } }
          )
        );
        slide.addTable(rows, {
          x: 7.2, y: 1.6, w: 5.6, colW: [3.2, 2.4],
          fontSize: 13, border: { type: "solid", color: "243048", pt: 1 },
          fill: { color: "111827" }, valign: "middle", rowH: 0.45,
        });

        if (d.notes) {
          slide.addText("Not: " + d.notes, { x: 7.2, y: 5.7, w: 5.6, h: 1.2, fontSize: 11, color: MUTED, italic: true });
        }
      }

      await pptx.writeFile({ fileName: `Vana_CFD_Sunum_${Date.now()}.pptx` });
    } catch (err) {
      alert("Sunum oluşturulurken hata: " + err.message);
      console.error(err);
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
      updateSelectionUI();
    }
  }

  // --- Olay bağlantıları ---
  function bindEvents() {
    $("#search").addEventListener("input", (e) => {
      state.search = e.target.value.trim();
      render();
    });
    $("#sort").addEventListener("change", (e) => {
      state.sort = e.target.value;
      render();
    });
    const omin = $("#opening-min");
    const omax = $("#opening-max");
    const sync = () => {
      let lo = +omin.value, hi = +omax.value;
      if (lo > hi) { [lo, hi] = [hi, lo]; }
      state.openingMin = lo; state.openingMax = hi;
      $("#opening-min-val").textContent = lo;
      $("#opening-max-val").textContent = hi;
      render();
    };
    omin.addEventListener("input", sync);
    omax.addEventListener("input", sync);
    $("#filter-cav").addEventListener("change", (e) => {
      state.cavOnly = e.target.checked;
      render();
    });
    $("#reset-filters").addEventListener("click", () => {
      state.types.clear(); state.dns.clear(); state.fluids.clear();
      state.openingMin = 0; state.openingMax = 100; state.cavOnly = false; state.search = "";
      omin.value = 0; omax.value = 100;
      $("#opening-min-val").textContent = 0; $("#opening-max-val").textContent = 100;
      $("#search").value = ""; $("#filter-cav").checked = false;
      $$(".chip.active").forEach((c) => c.classList.remove("active"));
      render();
    });
    $("#compare-btn").addEventListener("click", openCompare);
    $("#pptx-btn").addEventListener("click", exportPPTX);

    // Modal kapatma
    document.addEventListener("click", (e) => {
      if (e.target.hasAttribute("data-close")) $("#modal").classList.add("hidden");
      if (e.target.hasAttribute("data-close-compare")) $("#compare-modal").classList.add("hidden");
      if (e.target.hasAttribute("data-close-lightbox") || e.target.id === "lightbox-img")
        $("#lightbox").classList.add("hidden");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        $("#modal").classList.add("hidden");
        $("#compare-modal").classList.add("hidden");
        $("#lightbox").classList.add("hidden");
      }
    });
  }

  // --- Başlangıç ---
  function init() {
    if (!DATA.length) {
      $("#grid").innerHTML =
        '<div class="empty">Veri bulunamadı. <a href="loader.html" style="color:var(--primary)">Veri Yükle</a> sayfasından Excel/CSV yükleyin.</div>';
      return;
    }
    buildChips($("#filter-type"), uniqueSorted((d) => d.type), state.types);
    buildChips($("#filter-dn"), uniqueSorted((d) => d.dn, true), state.dns, (v) => "DN" + v);
    buildChips($("#filter-fluid"), uniqueSorted((d) => d.fluid), state.fluids);
    bindEvents();
    render();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
