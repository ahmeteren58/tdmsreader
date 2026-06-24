/*
 * Vana CFD Kataloğu - Veri Dosyası
 * --------------------------------
 * Uygulama veriyi BU dosyadan okur (window.VALVE_DATA).
 * Bu dosya, "Veri Yükle" sayfası (loader.html) ile Excel/CSV'den otomatik
 * üretilebilir; ya da elle düzenlenebilir.
 *
 * Alan açıklamaları için README.md ve loader.html'deki şablona bakınız.
 */
window.VALVE_DATA = [
  {
    id: "btf-dn100",
    name: "Kelebek Vana DN100 - Tam Açık",
    type: "Kelebek Vana",
    dn: 100,
    pressureClass: "PN16",
    opening: 100,
    fluid: "Su",
    temperature: 20,
    inletPressure: 6.0,
    outletPressure: 5.82,
    flowRate: 320,
    reynolds: 1.1e6,
    results: { cv: 1180, kv: 1021, deltaP: 0.18, kFactor: 0.35, cavitationIndex: 2.9, torque: 145, massFlow: 88.9 },
    images: [
      { label: "Basınç Konturu", src: "images/btf_dn100_pressure.svg" },
      { label: "Hız Konturu", src: "images/btf_dn100_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/btf_dn100_stream.svg" }
    ],
    analysisDate: "2026-03-12",
    notes: "Tam açık konumda düşük kayıp. Disk arkasında hafif resirkülasyon."
  },
  {
    id: "btf-dn200",
    name: "Kelebek Vana DN200 - Tam Açık",
    type: "Kelebek Vana",
    dn: 200,
    pressureClass: "PN16",
    opening: 100,
    fluid: "Su",
    temperature: 20,
    inletPressure: 6.0,
    outletPressure: 5.78,
    flowRate: 1250,
    reynolds: 2.2e6,
    results: { cv: 4650, kv: 4023, deltaP: 0.22, kFactor: 0.31, cavitationIndex: 3.1, torque: 410, massFlow: 347.2 },
    images: [
      { label: "Basınç Konturu", src: "images/btf_dn200_pressure.svg" },
      { label: "Hız Konturu", src: "images/btf_dn200_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/btf_dn200_stream.svg" }
    ],
    analysisDate: "2026-03-14",
    notes: "Ölçek büyüdükçe K faktörü hafif düşüyor."
  },
  {
    id: "btf-dn300",
    name: "Kelebek Vana DN300 - %60 Açık",
    type: "Kelebek Vana",
    dn: 300,
    pressureClass: "PN10",
    opening: 60,
    fluid: "Su",
    temperature: 25,
    inletPressure: 5.0,
    outletPressure: 3.95,
    flowRate: 1800,
    reynolds: 2.6e6,
    results: { cv: 2980, kv: 2578, deltaP: 1.05, kFactor: 1.85, cavitationIndex: 1.4, torque: 980, massFlow: 499.5 },
    images: [
      { label: "Basınç Konturu", src: "images/btf_dn300_pressure.svg" },
      { label: "Hız Konturu", src: "images/btf_dn300_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/btf_dn300_stream.svg" }
    ],
    analysisDate: "2026-04-02",
    notes: "Kısmi açıklıkta kavitasyon riski sınırda (sigma=1.4)."
  },
  {
    id: "ball-dn050",
    name: "Küresel Vana DN50 - Tam Açık",
    type: "Küresel Vana",
    dn: 50,
    pressureClass: "PN40",
    opening: 100,
    fluid: "Su",
    temperature: 20,
    inletPressure: 10.0,
    outletPressure: 9.96,
    flowRate: 95,
    reynolds: 6.0e5,
    results: { cv: 290, kv: 251, deltaP: 0.04, kFactor: 0.08, cavitationIndex: 6.2, torque: 38, massFlow: 26.4 },
    images: [
      { label: "Basınç Konturu", src: "images/ball_dn050_pressure.svg" },
      { label: "Hız Konturu", src: "images/ball_dn050_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/ball_dn050_stream.svg" }
    ],
    analysisDate: "2026-02-20",
    notes: "Tam gözenekli küre; çok düşük basınç kaybı."
  },
  {
    id: "ball-dn100",
    name: "Küresel Vana DN100 - Tam Açık",
    type: "Küresel Vana",
    dn: 100,
    pressureClass: "PN40",
    opening: 100,
    fluid: "Su",
    temperature: 20,
    inletPressure: 10.0,
    outletPressure: 9.94,
    flowRate: 410,
    reynolds: 1.4e6,
    results: { cv: 1520, kv: 1316, deltaP: 0.06, kFactor: 0.09, cavitationIndex: 5.8, torque: 120, massFlow: 113.9 },
    images: [
      { label: "Basınç Konturu", src: "images/ball_dn100_pressure.svg" },
      { label: "Hız Konturu", src: "images/ball_dn100_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/ball_dn100_stream.svg" }
    ],
    analysisDate: "2026-02-22",
    notes: "Tam açık küresel vana en düşük K faktörlü grup."
  },
  {
    id: "globe-dn080",
    name: "Glob Vana DN80 - Tam Açık",
    type: "Glob Vana",
    dn: 80,
    pressureClass: "PN25",
    opening: 100,
    fluid: "Su",
    temperature: 30,
    inletPressure: 8.0,
    outletPressure: 6.9,
    flowRate: 160,
    reynolds: 9.0e5,
    results: { cv: 240, kv: 208, deltaP: 1.10, kFactor: 6.5, cavitationIndex: 1.9, torque: 0, massFlow: 44.4 },
    images: [
      { label: "Basınç Konturu", src: "images/globe_dn080_pressure.svg" },
      { label: "Hız Konturu", src: "images/globe_dn080_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/globe_dn080_stream.svg" }
    ],
    analysisDate: "2026-03-05",
    notes: "S-tipi akış yolu nedeniyle yüksek K faktörü."
  },
  {
    id: "globe-dn150",
    name: "Glob Vana DN150 - Tam Açık",
    type: "Glob Vana",
    dn: 150,
    pressureClass: "PN25",
    opening: 100,
    fluid: "Su",
    temperature: 30,
    inletPressure: 8.0,
    outletPressure: 7.05,
    flowRate: 620,
    reynolds: 1.7e6,
    results: { cv: 980, kv: 849, deltaP: 0.95, kFactor: 6.1, cavitationIndex: 2.1, torque: 0, massFlow: 172.1 },
    images: [
      { label: "Basınç Konturu", src: "images/globe_dn150_pressure.svg" },
      { label: "Hız Konturu", src: "images/globe_dn150_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/globe_dn150_stream.svg" }
    ],
    analysisDate: "2026-03-07",
    notes: "Glob ailesinde tutarlı yüksek kayıp davranışı."
  },
  {
    id: "ctrl-dn100-25",
    name: "Kontrol Vanası DN100 - %25 Açık",
    type: "Kontrol Vanası",
    dn: 100,
    pressureClass: "PN16",
    opening: 25,
    fluid: "Su",
    temperature: 40,
    inletPressure: 7.0,
    outletPressure: 2.1,
    flowRate: 95,
    reynolds: 3.2e5,
    results: { cv: 78, kv: 67, deltaP: 4.90, kFactor: 92.0, cavitationIndex: 0.9, torque: 0, massFlow: 26.4 },
    images: [
      { label: "Basınç Konturu", src: "images/ctrl_dn100_25_pressure.svg" },
      { label: "Hız Konturu", src: "images/ctrl_dn100_25_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/ctrl_dn100_25_stream.svg" }
    ],
    analysisDate: "2026-05-10",
    notes: "Düşük açıklıkta ciddi kavitasyon (sigma=0.9). Dikkat!"
  },
  {
    id: "ctrl-dn100-50",
    name: "Kontrol Vanası DN100 - %50 Açık",
    type: "Kontrol Vanası",
    dn: 100,
    pressureClass: "PN16",
    opening: 50,
    fluid: "Su",
    temperature: 40,
    inletPressure: 7.0,
    outletPressure: 4.6,
    flowRate: 210,
    reynolds: 7.1e5,
    results: { cv: 310, kv: 268, deltaP: 2.40, kFactor: 18.5, cavitationIndex: 1.6, torque: 0, massFlow: 58.3 },
    images: [
      { label: "Basınç Konturu", src: "images/ctrl_dn100_50_pressure.svg" },
      { label: "Hız Konturu", src: "images/ctrl_dn100_50_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/ctrl_dn100_50_stream.svg" }
    ],
    analysisDate: "2026-05-11",
    notes: "Orta açıklık; kavitasyon güvenli aralığa yaklaşıyor."
  },
  {
    id: "ctrl-dn100-75",
    name: "Kontrol Vanası DN100 - %75 Açık",
    type: "Kontrol Vanası",
    dn: 100,
    pressureClass: "PN16",
    opening: 75,
    fluid: "Su",
    temperature: 40,
    inletPressure: 7.0,
    outletPressure: 5.9,
    flowRate: 340,
    reynolds: 1.1e6,
    results: { cv: 720, kv: 623, deltaP: 1.10, kFactor: 6.8, cavitationIndex: 2.6, torque: 0, massFlow: 94.4 },
    images: [
      { label: "Basınç Konturu", src: "images/ctrl_dn100_75_pressure.svg" },
      { label: "Hız Konturu", src: "images/ctrl_dn100_75_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/ctrl_dn100_75_stream.svg" }
    ],
    analysisDate: "2026-05-12",
    notes: "Kavitasyon güvenli. Verimli çalışma bölgesi."
  },
  {
    id: "ctrl-dn100-100",
    name: "Kontrol Vanası DN100 - %100 Açık",
    type: "Kontrol Vanası",
    dn: 100,
    pressureClass: "PN16",
    opening: 100,
    fluid: "Su",
    temperature: 40,
    inletPressure: 7.0,
    outletPressure: 6.6,
    flowRate: 430,
    reynolds: 1.5e6,
    results: { cv: 1240, kv: 1073, deltaP: 0.40, kFactor: 2.3, cavitationIndex: 4.2, torque: 0, massFlow: 119.4 },
    images: [
      { label: "Basınç Konturu", src: "images/ctrl_dn100_100_pressure.svg" },
      { label: "Hız Konturu", src: "images/ctrl_dn100_100_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/ctrl_dn100_100_stream.svg" }
    ],
    analysisDate: "2026-05-13",
    notes: "Tam açık; en yüksek Cv, en düşük kayıp."
  },
  {
    id: "check-dn200",
    name: "Çek Vana DN200 - Tam Açık",
    type: "Çek Vana",
    dn: 200,
    pressureClass: "PN16",
    opening: 100,
    fluid: "Su",
    temperature: 20,
    inletPressure: 6.0,
    outletPressure: 5.55,
    flowRate: 1100,
    reynolds: 1.9e6,
    results: { cv: 3100, kv: 2682, deltaP: 0.45, kFactor: 1.2, cavitationIndex: 3.4, torque: 0, massFlow: 305.5 },
    images: [
      { label: "Basınç Konturu", src: "images/check_dn200_pressure.svg" },
      { label: "Hız Konturu", src: "images/check_dn200_velocity.svg" },
      { label: "Akım Çizgileri", src: "images/check_dn200_stream.svg" }
    ],
    analysisDate: "2026-04-18",
    notes: "Klape açık konumda akışa karşı orta seviye direnç."
  }
];
