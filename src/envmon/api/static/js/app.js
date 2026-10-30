"use strict";

const ROOT = (document.querySelector('meta[name="envmon-root"]')?.content || "").replace(/\/$/, "");
const $ = (id) => document.getElementById(id);

const COLORS = {
  temperature: "#e8590c",
  humidity: "#1c7ed6",
  pm25: "#f08c00",
  pm10: "#c92a2a",
  voc: "#7048e8",
  nox: "#0ca678",
  lux: "#f2b705",
  noise: "#2f9e44",
};

const DEFAULT_CONFIG = {
  station_name: "Environment",
  timezone: "UTC",
  mode: "hardware",
  simulated: false,
  sampling: { enabled: true, interval_seconds: 30 },
  web: {
    refresh_seconds: 30,
    chart_default_hours: 24,
    chart_max_points: 1500,
    allow_export: true,
  },
  thresholds: {
    temperature_c: { min: 18, max: 26 },
    humidity_pct: { min: 30, max: 70 },
    pm2_5_ugm3: { min: null, max: 25 },
    pm10_ugm3: { min: null, max: 50 },
    voc_index: { min: null, max: 250 },
    lux: { min: 200, max: null },
    noise_db_a: { min: null, max: 65 },
  },
  llm: { enabled: false, model: null },
};

const SENSOR_LABELS = {
  sen5x: "SEN5x air quality",
  tsl2561: "TSL2561 light",
  noise: "Grove microphone",
  simulator: "Simulated sensors",
};

const state = {
  config: DEFAULT_CONFIG,
  hours: 24,
  charts: {},
  lastSeries: null,
  lastDaily: null,
  summaryLoaded: false,
  refreshing: false,
  refreshTimer: null,
  toastTimer: null,
};

function api(path) {
  return fetch(`${ROOT}${path}`, { cache: "no-store" }).then(async (response) => {
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch (_) {
        /* not JSON */
      }
      throw new Error(`${response.status} ${detail}`);
    }
    return response.json();
  });
}

function round(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
  const factor = 10 ** digits;
  return Math.round(Number(value) * factor) / factor;
}

function fmt(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes);
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function age(seconds) {
  if (seconds === null || seconds === undefined) return "no data";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d`;
}

function hoursLabel(hours) {
  if (hours === 6) return "6 h";
  if (hours === 24) return "24 h";
  if (hours === 168) return "7 d";
  if (hours === 720) return "30 d";
  return `${hours} h`;
}

function dailyDays() {
  if (state.hours <= 6) return 7;
  if (state.hours <= 24) return 14;
  if (state.hours <= 168) return 30;
  return 90;
}

function localDate(offsetDays = 0) {
  const date = new Date();
  date.setDate(date.getDate() + offsetDays);
  return date.toISOString().slice(0, 10);
}

function thresholdFor(key) {
  const map = {
    temperature: "temperature_c",
    humidity: "humidity_pct",
    pm25: "pm2_5_ugm3",
    pm10: "pm10_ugm3",
    voc: "voc_index",
    lux: "lux",
    noise: "noise_db_a",
  };
  return state.config.thresholds[map[key]] || {};
}

function metricState(key, value) {
  if (value === null || value === undefined) return "unknown";
  const limits = thresholdFor(key);
  if (limits.max !== null && limits.max !== undefined && value > limits.max) return "high";
  if (limits.min !== null && limits.min !== undefined && value < limits.min) return "low";
  return "ok";
}

function setStateDot(key, status) {
  const dot = document.querySelector(`[data-metric="${key}"] .state-dot`);
  if (!dot) return;
  dot.dataset.state = status;
  const labels = { ok: "Within configured range", high: "Above configured range", low: "Below configured range", unknown: "No data" };
  dot.title = labels[status] || "";
}

function setCard(key, value, digits) {
  const node = $(`value-${key}`);
  if (!node) return;
  node.textContent = fmt(value, digits);
  setStateDot(key, metricState(key, value));
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 4500);
}

function applyConfig() {
  const config = state.config;
  document.title = `${config.station_name} · Environment`;
  $("sim-banner").hidden = !config.simulated;
  if (config.simulated) {
    document.body.dataset.simulated = "true";
  }
  const defaultHours = Number(config.web?.chart_default_hours || 24);
  state.hours = [6, 24, 168, 720].includes(defaultHours) ? defaultHours : 24;
  document.querySelectorAll(".range-btn").forEach((button) => {
    button.classList.toggle("is-active", Number(button.dataset.hours) === state.hours);
  });
  $("export-btn").hidden = !config.web?.allow_export;
  $("footer-tz").textContent = config.timezone || "UTC";
  $("station-subtitle").textContent = `${config.timezone || "UTC"} · indoor monitoring`;
  updateExportLink();
}

function updateExportLink() {
  $("export-btn").href = `${ROOT}/api/export.csv?hours=${state.hours}`;
}

async function loadConfig() {
  try {
    const config = await api("/api/config");
    state.config = { ...DEFAULT_CONFIG, ...config, web: { ...DEFAULT_CONFIG.web, ...(config.web || {}) } };
    applyConfig();
  } catch (error) {
    showToast(`Could not load configuration: ${error.message}`);
  }
}

/* ------------------------------------------------------------------ charts */

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function chartColors() {
  return {
    text: cssVar("--text") || "#17212b",
    muted: cssVar("--muted") || "#5e6b7a",
    faint: cssVar("--faint") || "#8a97a6",
    grid: cssVar("--border") || "#e1e8f0",
    surface: cssVar("--surface") || "#ffffff",
  };
}

function emptyGraphic() {
  const c = chartColors();
  return {
    type: "text",
    left: "center",
    top: "middle",
    style: { text: "No data in this range yet", fill: c.faint, font: "13px sans-serif" },
  };
}

function baseOption() {
  const c = chartColors();
  return {
    animationDuration: 350,
    textStyle: { color: c.text, fontFamily: "inherit" },
    tooltip: {
      trigger: "axis",
      backgroundColor: c.surface,
      borderColor: c.grid,
      textStyle: { color: c.text, fontSize: 12 },
      axisPointer: { type: "line", lineStyle: { color: c.faint, type: "dashed" } },
    },
    legend: {
      top: 0,
      right: 0,
      icon: "roundRect",
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: c.muted, fontSize: 11 },
    },
    grid: { left: 46, right: 48, top: 34, bottom: 26, containLabel: false },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: c.grid } },
      axisTick: { show: false },
      axisLabel: {
        color: c.faint,
        fontSize: 10,
        hideOverlap: true,
        formatter: (value) => {
          const date = new Date(value);
          if (state.hours <= 48) {
            return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          }
          return date.toLocaleDateString([], { month: "short", day: "numeric" });
        },
      },
      splitLine: { show: false },
    },
    dataZoom: [{ type: "inside", throttle: 80 }],
  };
}

function valueAxis(name, formatter) {
  const c = chartColors();
  return {
    type: "value",
    name,
    scale: true,
    nameTextStyle: { color: c.faint, fontSize: 10, padding: [0, 0, 0, 4] },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: c.faint, fontSize: 10, formatter },
    splitLine: { lineStyle: { color: c.grid, type: "dashed" } },
  };
}

function lineSeries(name, color, data, yAxisIndex = 0, width = 2) {
  return {
    name,
    type: "line",
    yAxisIndex,
    data,
    showSymbol: false,
    smooth: 0.22,
    connectNulls: false,
    lineStyle: { width, color },
    itemStyle: { color },
    emphasis: { focus: "series", lineStyle: { width: width + 1 } },
    areaStyle: yAxisIndex === 0 ? { color, opacity: 0.07 } : undefined,
  };
}

function initCharts() {
  if (!window.echarts) return;
  ["climate", "air", "light-noise", "daily"].forEach((name) => {
    const element = $(`chart-${name}`);
    if (!element) return;
    state.charts[name] = window.echarts.init(element, null, { renderer: "canvas" });
  });
  renderCharts();
}

function renderCharts() {
  renderTimeCharts();
  renderDailyChart();
}

function renderTimeCharts() {
  const series = state.lastSeries;
  const points = series?.points || [];

  if (!state.charts.climate) return;

  const timeData = (key) => points.map((point) => [point.ts * 1000, point[key]]);
  if (!points.length) {
    ["climate", "air", "light-noise"].forEach((name) => {
      state.charts[name]?.clear();
      state.charts[name]?.setOption({ graphic: emptyGraphic() });
    });
    return;
  }

  const climate = baseOption();
  climate.legend.data = ["Temperature", "Humidity"];
  climate.yAxis = [
    valueAxis("°C", (value) => `${value}`),
    { ...valueAxis("%", (value) => `${value}%`), min: 0, max: 100, splitLine: { show: false } },
  ];
  climate.series = [
    lineSeries("Temperature", COLORS.temperature, timeData("temperature_c"), 0),
    lineSeries("Humidity", COLORS.humidity, timeData("humidity_pct"), 1),
  ];
  state.charts.climate.setOption(climate, true);

  const air = baseOption();
  air.legend.data = ["PM2.5", "PM10", "VOC index"];
  air.yAxis = [
    valueAxis("µg/m³", (value) => `${value}`),
    { ...valueAxis("VOC", (value) => `${value}`), splitLine: { show: false } },
  ];
  air.series = [
    lineSeries("PM2.5", COLORS.pm25, timeData("pm2_5_ugm3"), 0),
    lineSeries("PM10", COLORS.pm10, timeData("pm10_ugm3"), 0),
    lineSeries("VOC index", COLORS.voc, timeData("voc_index"), 1, 1.5),
  ];
  state.charts.air.setOption(air, true);

  const lightNoise = baseOption();
  lightNoise.legend.data = ["Illuminance", "Sound (approx.)"];
  lightNoise.yAxis = [
    valueAxis("lx", (value) => `${value}`),
    { ...valueAxis("dB(A)", (value) => `${value}`), splitLine: { show: false } },
  ];
  lightNoise.series = [
    lineSeries("Illuminance", COLORS.lux, timeData("lux"), 0),
    lineSeries("Sound (approx.)", COLORS.noise, timeData("noise_db_a"), 1),
  ];
  state.charts["light-noise"].setOption(lightNoise, true);
}

function dailyDate(day) {
  return new Date(`${day}T12:00:00`).getTime();
}

function renderDailyChart() {
  const chart = state.charts.daily;
  if (!chart) return;
  const rows = state.lastDaily?.days || [];
  if (!rows.length) {
    chart.clear();
    chart.setOption({ graphic: emptyGraphic() });
    return;
  }
  const values = (key) => rows.map((row) => [dailyDate(row.day), row[key]]);
  const option = baseOption();
  option.legend.data = ["Temp avg", "Temp min", "Temp max", "PM2.5 avg"];
  option.grid = { left: 46, right: 48, top: 34, bottom: 26 };
  option.xAxis.axisLabel.formatter = (value) =>
    new Date(value).toLocaleDateString([], { month: "short", day: "numeric" });
  option.yAxis = [
    valueAxis("°C", (value) => `${value}`),
    { ...valueAxis("µg/m³", (value) => `${value}`), splitLine: { show: false } },
  ];
  option.series = [
    lineSeries("Temp avg", COLORS.temperature, values("temperature_avg"), 0, 2.2),
    lineSeries("Temp min", "#f4a261", values("temperature_min"), 0, 1),
    lineSeries("Temp max", "#d9480f", values("temperature_max"), 0, 1),
    lineSeries("PM2.5 avg", COLORS.pm25, values("pm2_5_avg"), 1, 1.5),
  ];
  chart.setOption(option, true);
}

/* ------------------------------------------------------------------ updates */

function renderCurrent(sample) {
  if (!sample) {
    ["temperature", "humidity", "pm25", "pm10", "voc", "lux", "noise"].forEach((key) => {
      setCard(key, null, 0);
      $(`sub-${key}`).textContent = "waiting for data";
    });
    return;
  }
  setCard("temperature", sample.temperature_c, 1);
  setCard("humidity", sample.humidity_pct, 0);
  setCard("pm25", sample.pm2_5_ugm3, 1);
  setCard("pm10", sample.pm10_ugm3, 1);
  setCard("voc", sample.voc_index, 0);
  setCard("lux", sample.lux, 0);
  setCard("noise", sample.noise_db_a, 1);
  if (sample.noise_clipping_pct > 5) {
    const sub = $("sub-noise");
    sub.textContent = `ADC clipping ${fmt(sample.noise_clipping_pct, 1)}% — reduce gain`;
  }
}

function summarise(points, avgKey, minKey, maxKey) {
  let weighted = 0;
  let count = 0;
  let min = null;
  let max = null;
  for (const point of points) {
    if (point[avgKey] !== null && point[avgKey] !== undefined && point.count) {
      weighted += Number(point[avgKey]) * Number(point.count);
      count += Number(point.count);
    }
    if (point[minKey] !== null && point[minKey] !== undefined) {
      min = min === null ? Number(point[minKey]) : Math.min(min, Number(point[minKey]));
    }
    if (point[maxKey] !== null && point[maxKey] !== undefined) {
      max = max === null ? Number(point[maxKey]) : Math.max(max, Number(point[maxKey]));
    }
  }
  return { avg: count ? weighted / count : null, min, max };
}

function renderSeries(subs) {
  const points = state.lastSeries?.points || [];
  const label = hoursLabel(state.hours);
  const keys = ["temperature", "humidity", "pm25", "pm10", "voc", "lux", "noise"];
  if (!points.length) {
    keys.forEach((key) => {
      $(`sub-${key}`).textContent = "no data in selected range";
    });
    return;
  }
  if (subs.temperature.avg !== null) {
    $("sub-temperature").textContent =
      `${label} avg ${fmt(subs.temperature.avg, 1)} °C · range ${fmt(subs.temperature.min, 1)}–${fmt(subs.temperature.max, 1)}`;
  }
  if (subs.humidity.avg !== null) {
    $("sub-humidity").textContent =
      `${label} avg ${fmt(subs.humidity.avg, 0)}% · range ${fmt(subs.humidity.min, 0)}–${fmt(subs.humidity.max, 0)}%`;
  }
  if (subs.pm25.avg !== null) {
    $("sub-pm25").textContent = `${label} avg ${fmt(subs.pm25.avg, 1)} · peak ${fmt(subs.pm25.max, 1)} µg/m³`;
  }
  if (subs.pm10.avg !== null) {
    $("sub-pm10").textContent = `${label} avg ${fmt(subs.pm10.avg, 1)} · peak ${fmt(subs.pm10.max, 1)} µg/m³`;
  }
  if (subs.voc.avg !== null) {
    $("sub-voc").textContent = `${label} avg ${fmt(subs.voc.avg, 0)} · peak ${fmt(subs.voc.max, 0)}`;
  }
  if (subs.lux.avg !== null) {
    $("sub-lux").textContent =
      `${label} avg ${fmt(subs.lux.avg, 0)} lx · range ${fmt(subs.lux.min, 0)}–${fmt(subs.lux.max, 0)}`;
  }
  if (subs.noise.avg !== null) {
    $("sub-noise").textContent =
      `${label} avg ${fmt(subs.noise.avg, 1)} dB(A) · peak ${fmt(subs.noise.max, 1)}`;
  }
}

function updateSeriesData() {
  const points = state.lastSeries?.points || [];
  renderSeries({
    temperature: summarise(points, "temperature_c", "temperature_min", "temperature_max"),
    humidity: summarise(points, "humidity_pct", "humidity_min", "humidity_max"),
    pm25: summarise(points, "pm2_5_ugm3", "pm2_5_ugm3", "pm2_5_max"),
    pm10: summarise(points, "pm10_ugm3", "pm10_ugm3", "pm10_max"),
    voc: summarise(points, "voc_index", "voc_index", "voc_max"),
    lux: summarise(points, "lux", "lux_min", "lux_max"),
    noise: summarise(points, "noise_db_a", "noise_db_a", "noise_max"),
  });
}

function renderStatus(status) {
  const pill = $("status-pill");
  const text = $("status-text");
  pill.className = "pill";
  if (status.simulated) {
    pill.classList.add("pill-sim");
    text.textContent = "Simulated";
  } else if (!status.ok) {
    pill.classList.add("pill-stale");
    text.textContent = "No recent data";
  } else if (status.degraded) {
    pill.classList.add("pill-degraded");
    text.textContent = "Degraded";
  } else {
    pill.classList.add("pill-ok");
    text.textContent = "Live";
  }
  $("updated").textContent =
    status.sample_age_seconds !== null && status.sample_age_seconds !== undefined
      ? `updated ${age(status.sample_age_seconds)} ago`
      : "no samples yet";

  const list = $("sensor-list");
  list.replaceChildren();
  const sensors = status.sensors || [];
  if (!sensors.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = "No sensor status reported.";
    list.append(item);
  } else {
    sensors.forEach((sensor) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.className = "sensor-name";
      const dot = document.createElement("span");
      dot.className = "state-dot";
      dot.dataset.state = sensor.ok === true ? "ok" : sensor.ok === false ? "high" : "unknown";
      const label = document.createElement("span");
      label.textContent = SENSOR_LABELS[sensor.name] || sensor.name;
      name.append(dot, label);
      const detail = document.createElement("span");
      detail.className = "sensor-detail";
      if (sensor.ok === false && sensor.last_error) {
        detail.textContent = sensor.last_error;
      } else if (sensor.last_ok_ts) {
        detail.textContent = `ok · ${new Date(sensor.last_ok_ts * 1000).toLocaleTimeString()}`;
      } else {
        detail.textContent = "waiting";
      }
      item.append(name, detail);
      list.append(item);
    });
  }

  const errors = [...new Set((status.recent_errors || []).filter(Boolean))].slice(0, 6);
  const box = $("error-box");
  const errorList = $("error-list");
  errorList.replaceChildren();
  errors.forEach((message) => {
    const item = document.createElement("li");
    item.textContent = message;
    errorList.append(item);
  });
  box.hidden = errors.length === 0;

  const db = status.database || {};
  $("footer-mode").textContent = status.simulated ? "Simulator mode" : "Hardware sensors";
  $("footer-db").textContent = `${fmt(db.samples || 0, 0)} samples · ${fmtBytes(db.db_size_bytes)}`;
  $("footer-version").textContent = `envmon ${status.version || ""}`.trim();
}

function renderDailyTable() {
  const body = $("daily-table-body");
  body.replaceChildren();
  const rows = [...(state.lastDaily?.days || [])].reverse();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.className = "muted";
    td.textContent = "No daily records yet. Yesterday’s aggregate is written after midnight.";
    tr.append(td);
    body.append(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const cells = [
      row.day,
      fmt(row.sample_count, 0),
      `${fmt(row.temperature_avg, 1)} °C`,
      `${fmt(row.temperature_min, 1)}–${fmt(row.temperature_max, 1)}`,
      `${fmt(row.humidity_avg, 0)}%`,
      `${fmt(row.pm2_5_avg, 1)} µg/m³`,
      `${fmt(row.pm10_max, 1)} µg/m³`,
      `${fmt(row.noise_avg, 1)} dB(A)`,
    ];
    cells.forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    });
    body.append(tr);
  });
}

function renderSummary(summary) {
  const text = $("summary-text");
  text.classList.toggle("is-error", Boolean(summary.error));
  text.textContent = summary.summary;
  const parts = [];
  if (summary.source === "ollama") {
    parts.push(`Generated by ${summary.model}`);
  } else {
    parts.push("Generated locally from daily statistics");
  }
  if (summary.cached) parts.push("cached");
  if (summary.created_at) {
    parts.push(new Date(summary.created_at * 1000).toLocaleString());
  }
  if (summary.error) parts.push("LLM unavailable — local fallback");
  $("summary-meta").textContent = parts.join(" · ");
  $("summary-meta").title = summary.error || "";
}

/* ------------------------------------------------------------------- loaders */

async function refreshCurrent() {
  const data = await api("/api/current");
  renderCurrent(data.sample);
}

async function refreshSeries() {
  const config = state.config;
  const data = await api(
    `/api/series?hours=${state.hours}&max_points=${config.web?.chart_max_points || 1500}`,
  );
  state.lastSeries = data;
  updateSeriesData();
  renderTimeCharts();
}

async function refreshDaily() {
  const data = await api(`/api/daily?days=${dailyDays()}`);
  state.lastDaily = data;
  renderDailyChart();
  renderDailyTable();
}

async function refreshStatus() {
  const status = await api("/api/status");
  renderStatus(status);
}

async function refreshSummary(force = false) {
  const button = $("summary-refresh");
  button.disabled = true;
  const oldText = button.textContent;
  button.textContent = "Generating…";
  try {
    const data = await api(`/api/summary?refresh=${force ? "true" : "false"}`);
    renderSummary(data);
    state.summaryLoaded = true;
  } catch (error) {
    $("summary-text").textContent = "Summary is temporarily unavailable.";
    $("summary-meta").textContent = error.message;
    $("summary-text").classList.add("is-error");
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
}

async function refreshLive() {
  if (state.refreshing) return;
  state.refreshing = true;
  const tasks = await Promise.allSettled([
    refreshCurrent(),
    refreshSeries(),
    refreshDaily(),
    refreshStatus(),
  ]);
  state.refreshing = false;
  const labels = ["current readings", "history", "daily records", "status"];
  tasks.forEach((task, index) => {
    if (task.status === "rejected") {
      showToast(`Could not load ${labels[index]}: ${task.reason.message}`);
    }
  });
}

async function refreshAll() {
  await refreshLive();
  if (!state.summaryLoaded) {
    await refreshSummary(false);
  }
}

/* --------------------------------------------------------------- interactions */

function bindControls() {
  document.querySelectorAll(".range-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      const hours = Number(button.dataset.hours);
      if (hours === state.hours) return;
      state.hours = hours;
      document.querySelectorAll(".range-btn").forEach((other) => {
        other.classList.toggle("is-active", other === button);
      });
      updateExportLink();
      await Promise.allSettled([refreshSeries(), refreshDaily()]);
    });
  });

  $("refresh-btn").addEventListener("click", async () => {
    const button = $("refresh-btn");
    button.disabled = true;
    await refreshLive();
    button.disabled = false;
  });

  $("summary-refresh").addEventListener("click", () => refreshSummary(true));

  $("download-daily").addEventListener("click", () => {
    const rows = state.lastDaily?.days || [];
    if (!rows.length) {
      showToast("No daily records to download yet.");
      return;
    }
    const header = [
      "day",
      "sample_count",
      "temperature_avg",
      "temperature_min",
      "temperature_max",
      "humidity_avg",
      "humidity_min",
      "humidity_max",
      "pm2_5_avg",
      "pm2_5_max",
      "pm10_avg",
      "pm10_max",
      "voc_avg",
      "voc_max",
      "nox_avg",
      "nox_max",
      "lux_avg",
      "lux_min",
      "lux_max",
      "noise_avg",
      "noise_max",
    ];
    const escape = (value) => {
      const text = value === null || value === undefined ? "" : String(value);
      return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
    };
    const lines = [header.join(",")];
    rows.forEach((row) => lines.push(header.map((key) => escape(row[key])).join(",")));
    const blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `envmon-daily-${localDate()}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
  });
}

function scheduleAutoRefresh() {
  clearInterval(state.refreshTimer);
  if (document.hidden) return;
  const seconds = Math.max(10, Number(state.config.web?.refresh_seconds || 30));
  state.refreshTimer = setInterval(refreshLive, seconds * 1000);
}

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

async function init() {
  bindControls();
  initCharts();
  await loadConfig();
  await refreshAll();

  const resize = debounce(() => {
    Object.values(state.charts).forEach((chart) => chart?.resize());
  }, 150);
  window.addEventListener("resize", resize);

  document.addEventListener("visibilitychange", () => {
    scheduleAutoRefresh();
    if (!document.hidden) refreshLive();
  });
  scheduleAutoRefresh();

  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  darkQuery.addEventListener?.("change", () => {
    Object.values(state.charts).forEach((chart) => chart?.dispose());
    state.charts = {};
    initCharts();
  });
}

document.addEventListener("DOMContentLoaded", init);
