/* Minimal canvas time-series renderer.
 *
 * Deliberately hand-rolled rather than pulling in a charting library: the
 * dashboard has to work during the outages it exists to diagnose, so every byte
 * must be local, and the add-on image should not carry a megabyte of JavaScript
 * to draw a handful of line charts.
 *
 * Chart.draw(canvas, { series: [{ name, color, points: [[unixSeconds, value]] }] })
 */
(function (global) {
  'use strict';

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (value && value.trim()) || fallback;
  }

  var PALETTE = ['#38bdf8', '#f59e0b', '#4ade80', '#f87171', '#a78bfa', '#22d3ee'];

  // Hard ceiling on the backing store, in device pixels. Purely a safety net:
  // nothing should ever approach it, but an unbounded canvas allocation takes
  // the whole tab down, so it must not be reachable by arithmetic error.
  var MAX_BACKING = 8192;

  function niceStep(range, targetTicks) {
    if (range <= 0) return 1;
    var rough = range / targetTicks;
    var magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
    var normalised = rough / magnitude;
    var step = normalised >= 5 ? 10 : normalised >= 2 ? 5 : normalised >= 1 ? 2 : 1;
    return step * magnitude;
  }

  function formatTime(unixSeconds) {
    var d = new Date(unixSeconds * 1000);
    return String(d.getHours()).padStart(2, '0') + ':' +
           String(d.getMinutes()).padStart(2, '0');
  }

  function defaultFormat(value) {
    var abs = Math.abs(value);
    if (abs >= 1e9) return (value / 1e9).toFixed(1) + 'G';
    if (abs >= 1e6) return (value / 1e6).toFixed(1) + 'M';
    if (abs >= 1e3) return (value / 1e3).toFixed(1) + 'k';
    if (abs >= 10) return value.toFixed(0);
    if (abs >= 1) return value.toFixed(1);
    return value.toFixed(2);
  }

  function draw(canvas, options) {
    if (!canvas) return;
    options = options || {};

    var series = (options.series || []).filter(function (s) {
      return s.points && s.points.length;
    });

    var ratio = global.devicePixelRatio || 1;
    var cssWidth = Math.max(
      canvas.clientWidth || canvas.parentNode.clientWidth || 600, 10);

    // The intended CSS height has to be remembered separately, because
    // assigning canvas.height ALSO rewrites the height attribute. Reading the
    // attribute back on the next draw would feed in the already-scaled value as
    // if it were CSS pixels, multiplying the canvas by devicePixelRatio on every
    // refresh until the tab runs out of memory. Width is safe because it comes
    // from clientWidth, which CSS governs.
    if (!canvas.dataset.cssHeight) {
      canvas.dataset.cssHeight =
        String(parseInt(canvas.getAttribute('height'), 10) || 160);
    }
    var cssHeight = parseInt(canvas.dataset.cssHeight, 10) || 160;

    var backingWidth = Math.min(Math.round(cssWidth * ratio), MAX_BACKING);
    var backingHeight = Math.min(Math.round(cssHeight * ratio), MAX_BACKING);

    // Assigning width or height clears and reallocates the canvas, so only do
    // it when the size genuinely changed.
    if (canvas.width !== backingWidth) canvas.width = backingWidth;
    if (canvas.height !== backingHeight) canvas.height = backingHeight;
    if (canvas.style.height !== cssHeight + 'px') {
      canvas.style.height = cssHeight + 'px';
    }

    var ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    var text = cssVar('--text', '#e6e8eb');
    var muted = cssVar('--muted', '#9099a6');
    var border = cssVar('--border', '#2e343d');
    var format = options.format || defaultFormat;

    if (!series.length) {
      ctx.fillStyle = muted;
      ctx.font = '12px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('No data yet', cssWidth / 2, cssHeight / 2);
      return;
    }

    var padLeft = 46, padRight = 10, padTop = 14;
    var padBottom = series.length > 1 ? 34 : 20;
    var plotW = Math.max(cssWidth - padLeft - padRight, 10);
    var plotH = Math.max(cssHeight - padTop - padBottom, 10);

    var xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
    series.forEach(function (s) {
      s.points.forEach(function (p) {
        if (p[0] < xMin) xMin = p[0];
        if (p[0] > xMax) xMax = p[0];
        if (p[1] < yMin) yMin = p[1];
        if (p[1] > yMax) yMax = p[1];
      });
    });

    if (options.yMin !== undefined) yMin = Math.min(yMin, options.yMin);
    if (options.yMax !== undefined) yMax = Math.max(yMax, options.yMax);
    if (xMax === xMin) xMax = xMin + 1;
    if (yMax === yMin) { yMax = yMin + 1; yMin = Math.max(0, yMin - 1); }

    // Give the top a little headroom so peaks don't touch the frame.
    var step = niceStep(yMax - yMin, 4);
    yMin = Math.floor(yMin / step) * step;
    yMax = Math.ceil((yMax + step * 0.2) / step) * step;

    function px(t) { return padLeft + ((t - xMin) / (xMax - xMin)) * plotW; }
    function py(v) { return padTop + plotH - ((v - yMin) / (yMax - yMin)) * plotH; }

    // Horizontal gridlines and y labels.
    ctx.strokeStyle = border;
    ctx.fillStyle = muted;
    ctx.lineWidth = 1;
    ctx.font = '10px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var v = yMin; v <= yMax + 1e-9; v += step) {
      var y = Math.round(py(v)) + 0.5;
      ctx.globalAlpha = 0.5;
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(padLeft + plotW, y);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillText(format(v), padLeft - 6, y);
    }

    // X labels at both ends plus the middle — enough to orient, no clutter.
    ctx.textBaseline = 'top';
    var xTicks = [xMin, (xMin + xMax) / 2, xMax];
    xTicks.forEach(function (t, i) {
      ctx.textAlign = i === 0 ? 'left' : i === xTicks.length - 1 ? 'right' : 'center';
      ctx.fillText(formatTime(t), px(t), padTop + plotH + 6);
    });

    // Threshold line, when one is meaningful for the metric.
    if (options.threshold !== undefined &&
        options.threshold >= yMin && options.threshold <= yMax) {
      ctx.save();
      ctx.strokeStyle = cssVar('--warn', '#fbbf24');
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(padLeft, py(options.threshold));
      ctx.lineTo(padLeft + plotW, py(options.threshold));
      ctx.stroke();
      ctx.restore();
    }

    // Series.
    series.forEach(function (s, index) {
      var colour = s.color || PALETTE[index % PALETTE.length];
      var points = s.points.slice().sort(function (a, b) { return a[0] - b[0]; });

      if (s.fill !== false) {
        var gradient = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
        gradient.addColorStop(0, colour + '33');
        gradient.addColorStop(1, colour + '00');
        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.moveTo(px(points[0][0]), padTop + plotH);
        points.forEach(function (p) { ctx.lineTo(px(p[0]), py(p[1])); });
        ctx.lineTo(px(points[points.length - 1][0]), padTop + plotH);
        ctx.closePath();
        ctx.fill();
      }

      ctx.strokeStyle = colour;
      ctx.lineWidth = 1.6;
      ctx.lineJoin = 'round';
      // Explicit every time, not just when a series wants one: setLineDash is
      // canvas-context state, not per-stroke — leaving it set after a dashed
      // series would silently dash every gridline and series drawn after it.
      ctx.setLineDash(s.dash || []);
      ctx.beginPath();
      points.forEach(function (p, i) {
        var x = px(p[0]), y = py(p[1]);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.setLineDash([]);

      // Mark the most recent value so "now" is unambiguous.
      var last = points[points.length - 1];
      ctx.fillStyle = colour;
      ctx.beginPath();
      ctx.arc(px(last[0]), py(last[1]), 2.5, 0, Math.PI * 2);
      ctx.fill();
    });

    // Legend.
    if (series.length > 1) {
      var lx = padLeft;
      var ly = padTop + plotH + 20;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.font = '10px system-ui, sans-serif';
      series.forEach(function (s, index) {
        var colour = s.color || PALETTE[index % PALETTE.length];
        ctx.fillStyle = colour;
        ctx.fillRect(lx, ly + 3, 8, 3);
        ctx.fillStyle = text;
        var label = s.name || ('series ' + (index + 1));
        ctx.fillText(label, lx + 12, ly);
        lx += 12 + ctx.measureText(label).width + 16;
      });
    }
  }

  global.Chart = { draw: draw, palette: PALETTE };
})(window);
