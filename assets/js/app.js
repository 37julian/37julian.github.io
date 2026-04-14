"use strict";

// ── Constants ────────────────────────────────────────────────────────────────

const RUN_TYPES = new Set([
  "running", "trail_running", "treadmill_running", "virtual_run",
  "indoor_running", "track_running",
]);

const TYPE_META = {
  running:             { icon: "🏃", label: "Laufen" },
  trail_running:       { icon: "🏔️", label: "Trail Running" },
  treadmill_running:   { icon: "🏃", label: "Laufband" },
  virtual_run:         { icon: "🏃", label: "Virtuell" },
  indoor_running:      { icon: "🏃", label: "Indoor Laufen" },
  track_running:       { icon: "🏟️", label: "Bahn" },
  cycling:             { icon: "🚴", label: "Radfahren" },
  road_biking:         { icon: "🚴", label: "Rennrad" },
  mountain_biking:     { icon: "🚵", label: "MTB" },
  indoor_cycling:      { icon: "🚴", label: "Spinning" },
  swimming:            { icon: "🏊", label: "Schwimmen" },
  open_water_swimming: { icon: "🌊", label: "Freiwasser" },
  hiking:              { icon: "🥾", label: "Wandern" },
  walking:             { icon: "🚶", label: "Gehen" },
  strength_training:   { icon: "💪", label: "Kraft" },
  yoga:                { icon: "🧘", label: "Yoga" },
};

const DEFAULT_META = { icon: "🏅", label: "Aktivität" };

// ── Formatters ───────────────────────────────────────────────────────────────

function fmtPace(minPerKm) {
  if (!minPerKm) return "–";
  const min = Math.floor(minPerKm);
  const sec = Math.round((minPerKm - min) * 60);
  return `${min}:${String(sec).padStart(2, "0")} /km`;
}

function fmtDate(str) {
  if (!str) return "–";
  const [y, m, d] = str.slice(0, 10).split("-");
  return `${d}.${m}.${y}`;
}

function typeMeta(type) {
  return TYPE_META[type] || { ...DEFAULT_META, label: type || "Unbekannt" };
}

// ── Data loading ─────────────────────────────────────────────────────────────

async function loadData() {
  const res = await fetch("data/activities.json");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Stats cards ───────────────────────────────────────────────────────────────

function renderStats(stats) {
  const el = document.getElementById("stats-cards");
  if (!stats) return;

  const items = [
    { icon: "🏃", value: stats.ytd_runs ?? 0,
      label: `Läufe ${stats.current_year}` },
    { icon: "📏", value: `${stats.ytd_distance_km ?? 0} km`,
      label: `Distanz ${stats.current_year}` },
    { icon: "⚡", value: fmtPace(stats.avg_pace_min_per_km),
      label: "Ø Pace" },
    { icon: "❤️", value: stats.avg_heart_rate ? `${stats.avg_heart_rate} bpm` : "–",
      label: "Ø Herzfrequenz" },
  ];

  el.innerHTML = items.map(i => `
    <div class="stat-card">
      <span class="stat-icon">${i.icon}</span>
      <span class="stat-value">${i.value}</span>
      <span class="stat-label">${i.label}</span>
    </div>`).join("");
}

// ── Activities table ──────────────────────────────────────────────────────────

function renderActivities(activities) {
  const el = document.getElementById("activities-list");
  const rows = (activities || []).slice(0, 25);

  if (rows.length === 0) {
    el.innerHTML = '<div class="empty">Noch keine Aktivitäten vorhanden.</div>';
    return;
  }

  el.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Datum</th>
            <th>Name</th>
            <th>Typ</th>
            <th>Distanz</th>
            <th>Zeit</th>
            <th>Pace</th>
            <th>HF Ø</th>
            <th>Höhenmeter</th>
            <th>kcal</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(a => {
            const m = typeMeta(a.type);
            return `
            <tr>
              <td class="td-muted">${fmtDate(a.date)}</td>
              <td class="td-name">${a.name || "–"}</td>
              <td><span class="badge">${m.icon} ${m.label}</span></td>
              <td>${a.distance_km > 0 ? a.distance_km + " km" : "–"}</td>
              <td>${a.duration_formatted || "–"}</td>
              <td>${fmtPace(a.pace_min_per_km)}</td>
              <td>${a.avg_heart_rate ? a.avg_heart_rate + " bpm" : "–"}</td>
              <td>${a.elevation_gain_m ? Math.round(a.elevation_gain_m) + " m" : "–"}</td>
              <td>${a.calories ? Math.round(a.calories) : "–"}</td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>
    </div>`;
}

// ── Charts ────────────────────────────────────────────────────────────────────

/** Build a map of ISO week-label → distance (km) for the last `n` weeks. */
function weeklyDistances(activities, n = 16) {
  // Build week slots
  const slots = {};
  const now = new Date();
  for (let i = 0; i < n; i++) {
    const d = new Date(now);
    d.setDate(d.getDate() - i * 7);
    const key = isoWeekLabel(d);
    slots[key] = 0;
  }

  const runs = (activities || []).filter(a => RUN_TYPES.has(a.type));
  runs.forEach(r => {
    if (!r.date) return;
    const key = isoWeekLabel(new Date(r.date));
    if (key in slots) slots[key] = +(slots[key] + r.distance_km).toFixed(1);
  });

  const keys = Object.keys(slots).reverse();
  return { labels: keys.map(k => k.replace(/\s\d{4}$/, "")), values: keys.map(k => slots[k]) };
}

/** ISO week label like "KW 15 2026". */
function isoWeekLabel(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  d.setUTCDate(d.getUTCDate() + 4 - (d.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
  return `KW ${String(week).padStart(2, "0")} ${d.getUTCFullYear()}`;
}

function renderWeeklyChart(activities) {
  const ctx = document.getElementById("weeklyChart").getContext("2d");
  const { labels, values } = weeklyDistances(activities, 16);

  new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: "rgba(37, 99, 235, 0.75)",
        borderColor: "rgba(37, 99, 235, 1)",
        borderWidth: 1,
        borderRadius: 5,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => `${c.parsed.y} km` } },
      },
      scales: {
        y: {
          beginAtZero: true,
          grid: { color: "#e2e8f0" },
          ticks: { callback: v => v + " km" },
        },
        x: { grid: { display: false } },
      },
    },
  });
}

function renderPaceChart(activities) {
  const ctx = document.getElementById("paceChart").getContext("2d");
  const runs = (activities || [])
    .filter(a => RUN_TYPES.has(a.type) && a.pace_min_per_km && a.distance_km >= 1)
    .slice(0, 60)
    .reverse();

  if (runs.length === 0) return;

  new Chart(ctx, {
    type: "line",
    data: {
      labels: runs.map(r => fmtDate(r.date)),
      datasets: [{
        data: runs.map(r => r.pace_min_per_km),
        borderColor: "rgba(16, 185, 129, 1)",
        backgroundColor: "rgba(16, 185, 129, 0.08)",
        borderWidth: 2,
        tension: 0.35,
        pointRadius: 3.5,
        pointHoverRadius: 6,
        fill: true,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => fmtPace(c.parsed.y) } },
      },
      scales: {
        y: {
          reverse: true, // schneller = besser = oben
          grid: { color: "#e2e8f0" },
          ticks: { callback: v => fmtPace(v) },
        },
        x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } },
      },
    },
  });
}

// ── Heart rate chart ─────────────────────────────────────────────────────────

function renderHRChart(activities) {
  const ctx = document.getElementById("hrChart").getContext("2d");
  const runs = (activities || [])
    .filter(a => RUN_TYPES.has(a.type) && a.avg_heart_rate && a.distance_km >= 1)
    .slice(0, 60)
    .reverse();

  if (runs.length === 0) return;

  new Chart(ctx, {
    type: "line",
    data: {
      labels: runs.map(r => fmtDate(r.date)),
      datasets: [{
        data: runs.map(r => r.avg_heart_rate),
        borderColor: "rgba(239, 68, 68, 1)",
        backgroundColor: "rgba(239, 68, 68, 0.08)",
        borderWidth: 2,
        tension: 0.35,
        pointRadius: 3.5,
        pointHoverRadius: 6,
        fill: true,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => `${c.parsed.y} bpm` } },
      },
      scales: {
        y: {
          grid: { color: "#e2e8f0" },
          ticks: { callback: v => v + " bpm" },
        },
        x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } },
      },
    },
  });
}

// ── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  let data;
  try {
    data = await loadData();
  } catch (e) {
    document.getElementById("stats-cards").innerHTML =
      '<div style="color:rgba(255,255,255,.6);padding:.5rem">Daten nicht verfügbar</div>';
    return;
  }

  if (data.last_updated) {
    const d = new Date(data.last_updated);
    document.getElementById("last-updated").textContent =
      "Zuletzt aktualisiert: " + d.toLocaleString("de-DE");
  }

  renderStats(data.stats);
  renderActivities(data.activities);

  if (data.activities && data.activities.length > 0) {
    renderWeeklyChart(data.activities);
    renderPaceChart(data.activities);
    renderHRChart(data.activities);
  }
}

init();
