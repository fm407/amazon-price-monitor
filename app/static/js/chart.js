// Price history chart. Plots stored observations only; baseline and target are reference lines.
(function () {
  "use strict";
  var canvas = document.getElementById("price-chart");
  if (!canvas || !window.Chart) { return; }
  var chart = null;
  var range = "30d";
  var last = null;

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function money(value, currency) {
    try {
      return new Intl.NumberFormat("en-US", { style: "currency", currency: currency || "USD" }).format(value);
    } catch (e) {
      return String(value);
    }
  }

  function referenceLine(label, value, from, to, color) {
    return {
      label: label, data: [{ x: from, y: value }, { x: to, y: value }], borderColor: color,
      borderDash: [6, 5], borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 0, fill: false
    };
  }

  function draw(payload) {
    last = payload;
    var data = payload.data;
    var points = data.observations.map(function (o) {
      return { x: new Date(o.observed_at).getTime(), y: Number(o.price), seller: o.seller, basis: o.price_basis };
    });
    var note = document.getElementById("chart-note");
    if (chart) { chart.destroy(); chart = null; }
    if (!points.length) {
      note.textContent = "No observations in this range yet.";
      return;
    }
    note.textContent = points.length + " stored observations. Lines only connect neighbouring points; they are not confirmed prices.";
    var from = points[0].x;
    var to = points[points.length - 1].x;
    if (from === to) { from -= 3600000; to += 3600000; }
    var accent = token("--pm-accent");
    var datasets = [{
      label: "Observed price", data: points, borderColor: accent, backgroundColor: accent,
      borderWidth: 2, pointRadius: points.length > 120 ? 1.5 : 3.5, pointHoverRadius: 6, tension: 0, fill: false
    }];
    if (data.baseline_price !== null) { datasets.push(referenceLine("Baseline", Number(data.baseline_price), from, to, token("--pm-muted"))); }
    if (data.target_price !== null) { datasets.push(referenceLine("Target", Number(data.target_price), from, to, token("--pm-down"))); }
    var grid = token("--pm-line");
    var text = token("--pm-muted");
    var span = to - from;
    chart = new window.Chart(canvas, {
      type: "line",
      data: { datasets: datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
        interaction: { mode: "nearest", intersect: false },
        scales: {
          x: {
            type: "linear", min: from, max: to, grid: { color: grid }, border: { color: grid },
            ticks: {
              color: text, maxTicksLimit: 7, maxRotation: 0,
              callback: function (value) {
                var d = new Date(value);
                return span <= 2 * 86400000
                  ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
                  : d.toLocaleDateString([], { month: "short", day: "numeric" });
              }
            }
          },
          y: {
            grid: { color: grid }, border: { color: grid },
            ticks: { color: text, callback: function (value) { return money(value, data.currency); } }
          }
        },
        plugins: {
          legend: { labels: { color: text, usePointStyle: true, boxHeight: 6 } },
          tooltip: {
            callbacks: {
              title: function (items) { return new Date(items[0].parsed.x).toLocaleString(); },
              label: function (item) {
                var line = item.dataset.label + ": " + money(item.parsed.y, data.currency);
                var raw = item.raw || {};
                if (raw.seller) { line += " (" + raw.seller + ")"; }
                if (raw.basis === "item_only") { line += ", shipping unknown"; }
                return line;
              }
            }
          }
        }
      }
    });
  }

  function load() {
    fetch(canvas.dataset.url + "?range=" + encodeURIComponent(range) + "&limit=5000", { credentials: "same-origin" })
      .then(function (response) { if (!response.ok) { throw new Error("HTTP " + response.status); } return response.json(); })
      .then(draw)
      .catch(function () { document.getElementById("chart-note").textContent = "The price history could not be loaded."; });
  }

  document.querySelectorAll("#chart-ranges [data-range]").forEach(function (button) {
    button.addEventListener("click", function () {
      range = button.dataset.range;
      document.querySelectorAll("#chart-ranges [data-range]").forEach(function (b) { b.classList.toggle("active", b === button); });
      load();
    });
  });
  document.addEventListener("pm:themechange", function () { if (last) { draw(last); } });
  load();
})();
