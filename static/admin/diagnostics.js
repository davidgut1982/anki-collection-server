/**
 * diagnostics.js — A9 Diagnostics Dashboard
 *
 * All data is fetched via acsInvoke() (token-gated POST /admin/api/invoke).
 * Chart.js 4.4.9 is loaded from static/admin/vendor/chart.min.js (vendored
 * locally; no CDN dependency).
 *
 * Error isolation design:
 *   Each chart is wrapped in an independent try/catch around its acsInvoke
 *   call.  A failure in one stat action sets that chart's error banner and
 *   continues rendering the rest of the page.  No chart failure can block
 *   another chart.
 *
 * Loading state:
 *   Each chart's <canvas> is left visible (empty) while loading.  On success
 *   the canvas is drawn.  On empty data the canvas is hidden and the
 *   .diag-empty element is shown.  On error the canvas is hidden and the
 *   .diag-error element is shown with the message.
 */

/* global acsInvoke, Chart */
(function () {
  "use strict";

  // ── Design tokens (match admin.css CSS vars via JS) ────────────────────────
  const C = {
    blue:    "#0969da",
    green:   "#1a7f37",
    yellow:  "#bf8700",
    red:     "#cf222e",
    purple:  "#8250df",
    gray:    "#57606a",
    teal:    "#0969da",
    orange:  "#bc4c00",
    border:  "#d0d7de",
    text:    "#24292f",
    bg:      "#f4f5f7",
    // per-category for card counts
    stateColors: {
      new:       "#1d4ed8",
      learning:  "#854d0e",
      review:    "#166534",
      suspended: "#6b7280",
      buried:    "#6d28d9",
    },
  };

  // ── Chart.js global defaults ───────────────────────────────────────────────
  Chart.defaults.font.family =
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = C.text;
  Chart.defaults.plugins.legend.labels.boxWidth = 14;
  Chart.defaults.plugins.legend.labels.padding = 12;

  // ── Helpers ────────────────────────────────────────────────────────────────

  /** Destroy an existing Chart.js instance on a canvas before recreating. */
  const _charts = {};
  function getOrCreate(id, type, data, options) {
    if (_charts[id]) {
      _charts[id].destroy();
    }
    const ctx = document.getElementById(id);
    if (!ctx) return null;
    _charts[id] = new Chart(ctx, { type, data, options });
    return _charts[id];
  }

  function hide(el) { if (el) el.hidden = true; }
  function show(el) { if (el) el.hidden = false; }

  /** Show the .diag-error banner for a chart card with the given message. */
  function showChartError(errorElId, canvasId, msg) {
    const errEl = document.getElementById(errorElId);
    const canvas = document.getElementById(canvasId);
    hide(canvas);
    if (errEl) {
      errEl.hidden = false;
      errEl.textContent = "Error: " + msg;
    }
  }

  /** Show the .diag-empty banner and hide the canvas. */
  function showEmpty(emptyElId, canvasId) {
    const emptyEl = document.getElementById(emptyElId);
    const canvas = document.getElementById(canvasId);
    hide(canvas);
    if (emptyEl) emptyEl.hidden = false;
  }

  /** Reset error/empty/canvas visibility before a fresh load. */
  function resetCard(canvasId, emptyElId, errorElId) {
    const canvas = document.getElementById(canvasId);
    if (canvas) canvas.hidden = false;
    const e = document.getElementById(emptyElId);
    if (e) e.hidden = true;
    const r = document.getElementById(errorElId);
    if (r) r.hidden = true;
  }

  /** Format milliseconds as a human-readable string. */
  function fmtMs(ms) {
    if (ms === null || ms === undefined) return "—";
    const sec = Math.round(ms / 1000);
    if (sec < 60) return sec + " s";
    const min = Math.floor(sec / 60);
    const remSec = sec % 60;
    if (min < 60) return min + " m " + remSec + " s";
    const hr = Math.floor(min / 60);
    const remMin = min % 60;
    return hr + " h " + remMin + " m";
  }

  /** Trim a date-string array for display on bar charts (thin out labels). */
  function thinLabels(labels, maxTicks) {
    if (labels.length <= maxTicks) return labels;
    const step = Math.ceil(labels.length / maxTicks);
    return labels.map((l, i) => (i % step === 0 ? l : ""));
  }

  // Shared axis/tooltip defaults for time-series bar charts
  function timeSeriesOpts(labels, unit, formatFn) {
    const displayLabels = thinLabels(labels, 20);
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "top" },
        tooltip: {
          callbacks: {
            title: (items) => labels[items[0].dataIndex] || "",
            label: formatFn
              ? (item) => item.dataset.label + ": " + formatFn(item.raw)
              : undefined,
          },
        },
      },
      scales: {
        x: {
          ticks: {
            autoSkip: false,
            maxRotation: 45,
            callback: (val, idx) => displayLabels[idx],
          },
          grid: { color: C.border },
        },
        y: {
          beginAtZero: true,
          ticks: { precision: 0 },
          grid: { color: C.border },
        },
      },
    };
  }

  // ── 1. Summary strip ───────────────────────────────────────────────────────
  async function loadSummary() {
    const strip = document.getElementById("diag-summary-strip");
    if (!strip) return;

    let cardData = null;
    let healthData = null;

    try {
      cardData = await acsInvoke("statCardCounts", {});
    } catch (_) { /* best-effort */ }

    try {
      const resp = await fetch("/health", { credentials: "same-origin" });
      if (resp.ok) healthData = await resp.json();
    } catch (_) { /* best-effort */ }

    const total = cardData ? cardData.total : "—";
    const notes = healthData && healthData.note_count != null
      ? healthData.note_count.toLocaleString()
      : "—";
    const status = healthData ? healthData.status : "—";

    strip.innerHTML =
      '<div class="diag-summary-items">' +
        '<span class="diag-summary-item">' +
          '<strong>' + (total !== "—" ? Number(total).toLocaleString() : "—") + '</strong>' +
          '<span class="diag-summary-sub">total cards</span>' +
        '</span>' +
        '<span class="diag-summary-sep">·</span>' +
        '<span class="diag-summary-item">' +
          '<strong>' + notes + '</strong>' +
          '<span class="diag-summary-sub">notes</span>' +
        '</span>' +
        (cardData
          ? '<span class="diag-summary-sep">·</span>' +
            '<span class="diag-summary-item">' +
              '<strong class="state-badge state-new">' + Number(cardData.new).toLocaleString() + '</strong>' +
              '<span class="diag-summary-sub">new</span>' +
            '</span>' +
            '<span class="diag-summary-sep">·</span>' +
            '<span class="diag-summary-item">' +
              '<strong class="state-badge state-learn">' + Number(cardData.learning).toLocaleString() + '</strong>' +
              '<span class="diag-summary-sub">learning</span>' +
            '</span>' +
            '<span class="diag-summary-sep">·</span>' +
            '<span class="diag-summary-item">' +
              '<strong class="state-badge state-review">' + Number(cardData.review).toLocaleString() + '</strong>' +
              '<span class="diag-summary-sub">review</span>' +
            '</span>'
          : "") +
        '<span class="diag-summary-sep">·</span>' +
        '<span class="diag-summary-item">' +
          '<strong class="status-' + status + '">' + status + '</strong>' +
          '<span class="diag-summary-sub">collection</span>' +
        '</span>' +
      '</div>';
  }

  // ── 2. Card counts doughnut ─────────────────────────────────────────────
  async function loadCardCounts() {
    const canvasId = "chart-card-counts";
    const emptyId = "chart-card-counts-empty";
    const errId = "chart-card-counts-error";
    const totalEl = document.getElementById("chart-card-counts-total");
    resetCard(canvasId, emptyId, errId);
    if (totalEl) totalEl.textContent = "";

    let data;
    try {
      data = await acsInvoke("statCardCounts", {});
    } catch (e) {
      showChartError(errId, canvasId, String(e.message || e));
      return;
    }

    const cats = ["new", "learning", "review", "suspended", "buried"];
    const vals = cats.map((k) => data[k] || 0);
    if (vals.every((v) => v === 0)) {
      showEmpty(emptyId, canvasId);
      return;
    }

    if (totalEl) {
      totalEl.textContent =
        "Total: " + Number(data.total || 0).toLocaleString() + " cards";
    }

    getOrCreate(
      canvasId,
      "doughnut",
      {
        labels: ["New", "Learning", "Review", "Suspended", "Buried"],
        datasets: [
          {
            data: vals,
            backgroundColor: [
              C.stateColors.new,
              C.stateColors.learning,
              C.stateColors.review,
              C.stateColors.suspended,
              C.stateColors.buried,
            ],
            borderWidth: 2,
            borderColor: "#fff",
            hoverOffset: 4,
          },
        ],
      },
      {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "right" },
          tooltip: {
            callbacks: {
              label: (item) => {
                const val = item.raw;
                const pct = data.total
                  ? ((val / data.total) * 100).toFixed(1) + "%"
                  : "0%";
                return " " + item.label + ": " + Number(val).toLocaleString() + " (" + pct + ")";
              },
            },
          },
        },
      }
    );
  }

  // ── 3. True retention ───────────────────────────────────────────────────
  async function loadTrueRetention(days) {
    const canvasId = "chart-retention";
    const emptyId = "chart-retention-empty";
    const errId = "chart-retention-error";
    const tableEl = document.getElementById("diag-retention-table");
    resetCard(canvasId, emptyId, errId);
    if (tableEl) tableEl.hidden = true;

    let data;
    try {
      data = await acsInvoke("statTrueRetention", { days });
    } catch (e) {
      showChartError(errId, canvasId, String(e.message || e));
      return;
    }

    const cohorts = ["young", "mature", "overall"];
    const totals = cohorts.map((k) => (data[k] || {}).total || 0);
    if (totals.every((t) => t === 0)) {
      showEmpty(emptyId, canvasId);
      return;
    }

    // Populate table
    if (tableEl) {
      const tbody = tableEl.querySelector("tbody");
      tbody.innerHTML = "";
      cohorts.forEach((k) => {
        const r = data[k] || {};
        const tr = document.createElement("tr");
        tr.innerHTML =
          "<td>" + k.charAt(0).toUpperCase() + k.slice(1) + "</td>" +
          "<td>" + (r.pass || 0).toLocaleString() + "</td>" +
          "<td>" + (r.total || 0).toLocaleString() + "</td>" +
          "<td>" + (r.retention !== null && r.retention !== undefined
            ? r.retention.toFixed(1) + "%"
            : "—") + "</td>";
        tbody.appendChild(tr);
      });
      tableEl.hidden = false;
    }

    const retentions = cohorts.map((k) => {
      const r = data[k] || {};
      return r.retention !== null && r.retention !== undefined ? r.retention : 0;
    });

    getOrCreate(
      canvasId,
      "bar",
      {
        labels: ["Young", "Mature", "Overall"],
        datasets: [
          {
            label: "Retention %",
            data: retentions,
            backgroundColor: [C.blue + "cc", C.green + "cc", C.orange + "cc"],
            borderColor: [C.blue, C.green, C.orange],
            borderWidth: 1,
          },
        ],
      },
      {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (item) =>
                " Retention: " + item.raw.toFixed(2) + "%",
            },
          },
        },
        scales: {
          x: { grid: { color: C.border } },
          y: {
            beginAtZero: true,
            max: 100,
            ticks: { callback: (v) => v + "%" },
            grid: { color: C.border },
          },
        },
      }
    );
  }

  // ── 4. Interval distribution ────────────────────────────────────────────
  async function loadIntervalDistribution() {
    const canvasId = "chart-interval";
    const emptyId = "chart-interval-empty";
    const errId = "chart-interval-error";
    resetCard(canvasId, emptyId, errId);

    let data;
    try {
      data = await acsInvoke("statIntervalDistribution", {});
    } catch (e) {
      showChartError(errId, canvasId, String(e.message || e));
      return;
    }

    const buckets = (data && data.buckets) || [];
    const counts = buckets.map((b) => b.count || 0);
    if (counts.every((c) => c === 0)) {
      showEmpty(emptyId, canvasId);
      return;
    }

    getOrCreate(
      canvasId,
      "bar",
      {
        labels: buckets.map((b) => b.label),
        datasets: [
          {
            label: "Cards",
            data: counts,
            backgroundColor: C.blue + "bb",
            borderColor: C.blue,
            borderWidth: 1,
          },
        ],
      },
      {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: C.border } },
          y: {
            beginAtZero: true,
            ticks: { precision: 0 },
            grid: { color: C.border },
          },
        },
      }
    );
  }

  // ── 5. Ease distribution ────────────────────────────────────────────────
  async function loadEaseDistribution() {
    const canvasId = "chart-ease";
    const emptyId = "chart-ease-empty";
    const errId = "chart-ease-error";
    const noteEl = document.getElementById("diag-fsrs-note");
    resetCard(canvasId, emptyId, errId);
    if (noteEl) noteEl.hidden = true;

    let data;
    try {
      data = await acsInvoke("statEaseDistribution", {});
    } catch (e) {
      showChartError(errId, canvasId, String(e.message || e));
      return;
    }

    const buckets = (data && data.sm2) || [];
    const counts = buckets.map((b) => b.count || 0);
    if (counts.every((c) => c === 0)) {
      showEmpty(emptyId, canvasId);
    } else {
      getOrCreate(
        canvasId,
        "bar",
        {
          labels: buckets.map((b) => b.label),
          datasets: [
            {
              label: "Cards",
              data: counts,
              backgroundColor: C.purple + "bb",
              borderColor: C.purple,
              borderWidth: 1,
            },
          ],
        },
        {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: {
              ticks: { maxRotation: 45 },
              grid: { color: C.border },
            },
            y: {
              beginAtZero: true,
              ticks: { precision: 0 },
              grid: { color: C.border },
            },
          },
        }
      );
    }

    if (noteEl && data && data.fsrs_note) {
      noteEl.textContent = data.fsrs_note;
      noteEl.hidden = false;
    }
  }

  // ── 6. Future due ───────────────────────────────────────────────────────
  async function loadFutureDue(days) {
    const canvasId = "chart-future-due";
    const emptyId = "chart-future-due-empty";
    const errId = "chart-future-due-error";
    resetCard(canvasId, emptyId, errId);

    let data;
    try {
      data = await acsInvoke("statFutureDue", { days });
    } catch (e) {
      showChartError(errId, canvasId, String(e.message || e));
      return;
    }

    if (!Array.isArray(data) || data.length === 0) {
      showEmpty(emptyId, canvasId);
      return;
    }

    const counts = data.map((d) => d.count || 0);
    if (counts.every((c) => c === 0)) {
      showEmpty(emptyId, canvasId);
      return;
    }

    const labels = data.map((d) => {
      const offset = d.day_offset;
      if (offset === 0) return "Today";
      if (offset === 1) return "+1";
      return "+" + offset;
    });

    const opts = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => {
              const offset = data[items[0].dataIndex].day_offset;
              return offset === 0 ? "Today" : "Day +" + offset;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            autoSkip: true,
            maxTicksLimit: 20,
            maxRotation: 45,
          },
          grid: { color: C.border },
        },
        y: {
          beginAtZero: true,
          ticks: { precision: 0 },
          grid: { color: C.border },
        },
      },
    };

    getOrCreate(
      canvasId,
      "bar",
      {
        labels,
        datasets: [
          {
            label: "Cards due",
            data: counts,
            backgroundColor: C.teal + "bb",
            borderColor: C.teal,
            borderWidth: 1,
          },
        ],
      },
      opts
    );
  }

  // ── 7. Reviews by day ───────────────────────────────────────────────────
  async function loadReviewsByDay(days) {
    const canvasId = "chart-reviews";
    const emptyId = "chart-reviews-empty";
    const errId = "chart-reviews-error";
    resetCard(canvasId, emptyId, errId);

    let data;
    try {
      data = await acsInvoke("statReviewsByDay", { days });
    } catch (e) {
      showChartError(errId, canvasId, String(e.message || e));
      return;
    }

    if (!Array.isArray(data) || data.length === 0) {
      showEmpty(emptyId, canvasId);
      return;
    }

    const reps = data.map((d) => d.reps || 0);
    const reviews = data.map((d) => d.reviews || 0);
    if (reps.every((v) => v === 0) && reviews.every((v) => v === 0)) {
      showEmpty(emptyId, canvasId);
      return;
    }

    const labels = data.map((d) => d.date);
    const opts = timeSeriesOpts(labels);

    getOrCreate(
      canvasId,
      "bar",
      {
        labels,
        datasets: [
          {
            label: "reps (all)",
            data: reps,
            backgroundColor: C.blue + "99",
            borderColor: C.blue,
            borderWidth: 1,
            order: 2,
          },
          {
            label: "reviews (type=1)",
            data: reviews,
            backgroundColor: C.green + "bb",
            borderColor: C.green,
            borderWidth: 1,
            order: 1,
          },
        ],
      },
      opts
    );
  }

  // ── 8. Added by day ─────────────────────────────────────────────────────
  async function loadAddedByDay(days) {
    const canvasId = "chart-added";
    const emptyId = "chart-added-empty";
    const errId = "chart-added-error";
    resetCard(canvasId, emptyId, errId);

    let data;
    try {
      data = await acsInvoke("statAddedByDay", { days });
    } catch (e) {
      showChartError(errId, canvasId, String(e.message || e));
      return;
    }

    if (!Array.isArray(data) || data.length === 0) {
      showEmpty(emptyId, canvasId);
      return;
    }

    const counts = data.map((d) => d.count || 0);
    if (counts.every((c) => c === 0)) {
      showEmpty(emptyId, canvasId);
      return;
    }

    const labels = data.map((d) => d.date);
    const opts = timeSeriesOpts(labels);

    getOrCreate(
      canvasId,
      "bar",
      {
        labels,
        datasets: [
          {
            label: "Cards added",
            data: counts,
            backgroundColor: C.green + "99",
            borderColor: C.green,
            borderWidth: 1,
          },
        ],
      },
      opts
    );
  }

  // ── 9. Time spent ───────────────────────────────────────────────────────
  async function loadTimeSpent(days) {
    const canvasId = "chart-time";
    const emptyId = "chart-time-empty";
    const errId = "chart-time-error";
    const summaryEl = document.getElementById("diag-time-summary");
    resetCard(canvasId, emptyId, errId);
    if (summaryEl) summaryEl.hidden = true;

    let data;
    try {
      data = await acsInvoke("statTimeSpent", { days });
    } catch (e) {
      showChartError(errId, canvasId, String(e.message || e));
      return;
    }

    if (!data || data.totalMs === 0) {
      showEmpty(emptyId, canvasId);
      return;
    }

    // Summary strip
    if (summaryEl) {
      const avgStr = data.avgMsPerRep != null
        ? fmtMs(data.avgMsPerRep) + " / rep"
        : "—";
      summaryEl.innerHTML =
        '<span class="diag-time-stat">' +
          '<strong>' + fmtMs(data.totalMs) + '</strong>' +
          '<span class="diag-summary-sub">total in period</span>' +
        '</span>' +
        '<span class="diag-summary-sep">·</span>' +
        '<span class="diag-time-stat">' +
          '<strong>' + avgStr + '</strong>' +
          '<span class="diag-summary-sub">avg per rep</span>' +
        '</span>';
      summaryEl.hidden = false;
    }

    const perDay = (data.perDayMs || []);
    if (perDay.length === 0) {
      showEmpty(emptyId, canvasId);
      return;
    }

    const labels = perDay.map((d) => d.date);
    const msVals = perDay.map((d) => d.ms || 0);
    // Convert ms → minutes for the chart (more readable scale)
    const minVals = msVals.map((m) => Math.round(m / 60000 * 10) / 10);

    const opts = timeSeriesOpts(labels, "day", null);
    // Override y-axis label for minutes
    opts.scales.y.title = { display: true, text: "minutes" };
    opts.plugins.tooltip.callbacks.label = (item) =>
      " " + item.dataset.label + ": " + fmtMs(msVals[item.dataIndex]);

    getOrCreate(
      canvasId,
      "bar",
      {
        labels,
        datasets: [
          {
            label: "Study time",
            data: minVals,
            backgroundColor: C.orange + "99",
            borderColor: C.orange,
            borderWidth: 1,
          },
        ],
      },
      opts
    );
  }

  // ── Orchestrator ────────────────────────────────────────────────────────────
  function getDays() {
    const sel = document.getElementById("diag-range");
    return sel ? parseInt(sel.value, 10) || 30 : 30;
  }

  function setRefreshStatus(msg) {
    const el = document.getElementById("diag-refresh-status");
    if (el) el.textContent = msg;
  }

  async function loadAll() {
    const days = getDays();
    setRefreshStatus("Loading…");

    // Run all loaders concurrently; each is independently error-isolated.
    // loadSummary and loadCardCounts do not need a days param.
    // loadIntervalDistribution and loadEaseDistribution are also time-range-free.
    await Promise.allSettled([
      loadSummary(),
      loadCardCounts(),
      loadTrueRetention(days),
      loadIntervalDistribution(),
      loadEaseDistribution(),
      loadFutureDue(days),
      loadReviewsByDay(days),
      loadAddedByDay(days),
      loadTimeSpent(days),
    ]);

    setRefreshStatus("");
  }

  // ── Boot ────────────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    const refreshBtn = document.getElementById("diag-refresh-btn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", loadAll);
    }

    const rangeSelect = document.getElementById("diag-range");
    if (rangeSelect) {
      rangeSelect.addEventListener("change", loadAll);
    }

    loadAll();
  });
})();
