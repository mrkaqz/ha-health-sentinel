/* Health Sentinel dashboard logic.
 *
 * Everything is fetched with relative URLs so it works unchanged behind the
 * ingress path prefix. Journal lines and event messages are inserted with
 * textContent, never innerHTML — kernel logs are untrusted text.
 */
(function () {
  'use strict';

  var REFRESH_MS = 5000;
  var state = { view: 'now', timer: null, incident: null };

  // ------------------------------------------------------------- helpers

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function get(path) {
    return fetch(path, { headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    var d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2, '0') + ':' +
           String(d.getMinutes()).padStart(2, '0') + ':' +
           String(d.getSeconds()).padStart(2, '0');
  }

  function fmtDateTime(ts) {
    if (!ts) return '—';
    var d = new Date(ts * 1000);
    return d.toLocaleString();
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    seconds = Math.max(0, Math.round(seconds));
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's';
    if (seconds < 86400) {
      return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
    }
    return Math.floor(seconds / 86400) + 'd ' + Math.floor((seconds % 86400) / 3600) + 'h';
  }

  function fmtBytes(bytes) {
    if (bytes === null || bytes === undefined) return '—';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return bytes.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
  }

  function num(value, digits, suffix) {
    if (value === null || value === undefined || isNaN(value)) return '—';
    return Number(value).toFixed(digits === undefined ? 1 : digits) + (suffix || '');
  }

  function tile(label, value, sub, level) {
    var node = el('div', 'tile' + (level ? ' ' + level : ''));
    node.appendChild(el('div', 'label', label));
    node.appendChild(el('div', 'value', value));
    if (sub) node.appendChild(el('div', 'sub', sub));
    return node;
  }

  function eventRow(event) {
    var row = el('div', 'event ' + (event.severity || 'info'));
    row.appendChild(el('time', null, fmtTime(event.ts)));
    row.appendChild(el('span', 'kind', event.kind || ''));
    row.appendChild(el('span', 'msg', event.message || ''));
    return row;
  }

  function renderEvents(container, events, emptyText) {
    clear(container);
    if (!events || !events.length) {
      container.appendChild(el('p', 'muted', emptyText || 'Nothing recorded yet.'));
      return;
    }
    events.forEach(function (event) { container.appendChild(eventRow(event)); });
  }

  function table(node, columns, rows, renderRow) {
    clear(node);
    var thead = el('thead');
    var headRow = el('tr');
    columns.forEach(function (col) {
      var th = el('th', col.numeric ? 'num' : null, col.label);
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    node.appendChild(thead);

    var tbody = el('tbody');
    if (!rows.length) {
      var empty = el('tr');
      var td = el('td', null, 'Nothing to show yet.');
      td.colSpan = columns.length;
      td.className = 'muted';
      empty.appendChild(td);
      tbody.appendChild(empty);
    } else {
      rows.forEach(function (row) { tbody.appendChild(renderRow(row)); });
    }
    node.appendChild(tbody);
  }

  // --------------------------------------------------------------- views

  function switchView(view) {
    state.view = view;
    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.classList.toggle('active', tab.dataset.view === view);
    });
    document.querySelectorAll('.view').forEach(function (section) {
      section.classList.toggle('active', section.id === 'view-' + view);
    });
    refresh();
  }

  // ----------------------------------------------------------------- now

  function renderStatus(data) {
    var core = data.core || {};
    var metrics = data.metrics || {};
    var detector = data.detector || {};

    var pulse = $('pulse');
    pulse.className = 'pulse ' + (core.reachable ? 'ok' : 'bad');
    $('uptime').textContent = 'sentinel up ' + (data.sentinel_uptime || '—');

    var streams = data.streams || {};
    $('stream-host').className = 'stream ' + (streams.host_journal ? 'live' : 'dead');
    $('stream-bus').className = 'stream ' + (streams.event_bus ? 'live' : 'dead');

    // Banner: an open incident outranks the boot verdict.
    var banner = $('banner');
    var verdict = data.boot_verdict || {};
    if (detector.incident_open) {
      banner.className = 'banner critical';
      banner.textContent = 'Home Assistant Core is not responding — down since ' +
        fmtTime(detector.down_since) + ' (' + (detector.last_error || 'no response') + ')';
    } else if (verdict.classification &&
               ['host_power_loss', 'core_only', 'host_reboot', 'addon_restart']
                 .indexOf(verdict.classification) !== -1) {
      banner.className = 'banner warning';
      banner.textContent = 'Last restart: ' + (verdict.summary || verdict.classification);
    } else {
      banner.className = 'banner hidden';
    }

    var tiles = $('tiles');
    clear(tiles);

    tiles.appendChild(tile(
      'Core',
      core.reachable ? 'Online' : 'DOWN',
      core.reachable ? num(core.latency_ms, 0, ' ms') : (core.error || ''),
      core.reachable ? 'ok' : 'err'
    ));

    var memPct = metrics['host.mem.used_pct'];
    tiles.appendChild(tile('Memory', num(memPct, 1, '%'),
      fmtBytes(metrics['host.mem.available_bytes']) + ' available',
      memPct >= 90 ? 'err' : memPct >= 80 ? 'warn' : null));

    var psi = metrics['host.psi.memory.some.avg60'];
    tiles.appendChild(tile('Memory pressure', psi === undefined ? 'n/a' : num(psi, 1, '%'),
      'PSI some, 60s', psi >= 10 ? 'err' : psi >= 1 ? 'warn' : null));

    var cpuPsi = metrics['host.psi.cpu.some.avg60'];
    tiles.appendChild(tile('CPU pressure', cpuPsi === undefined ? 'n/a' : num(cpuPsi, 1, '%'),
      'PSI some, 60s', cpuPsi >= 20 ? 'warn' : null));

    tiles.appendChild(tile('Load', num(metrics['host.load.1'], 2),
      '5m ' + num(metrics['host.load.5'], 2)));

    var freePct = metrics['host.disk.free_pct'];
    tiles.appendChild(tile('Disk free', num(freePct, 1, '%'),
      num(metrics['host.disk.free_gb'], 0, ' GB free'),
      freePct <= 5 ? 'err' : freePct <= 10 ? 'warn' : null));

    if (metrics['host.disk.life_time_pct'] !== undefined) {
      tiles.appendChild(tile('Disk wear', num(metrics['host.disk.life_time_pct'], 0, '%'),
        'SSD lifetime used',
        metrics['host.disk.life_time_pct'] >= 80 ? 'err' : null));
    }

    if (metrics['host.temp.max'] !== undefined) {
      tiles.appendChild(tile('Temperature', num(metrics['host.temp.max'], 1, '°C'),
        'hottest zone', metrics['host.temp.max'] >= 80 ? 'warn' : null));
    }

    tiles.appendChild(tile('Add-ons', num(metrics['addons.running'], 0),
      num(metrics['addons.error'], 0) + ' in error',
      metrics['addons.error'] > 0 ? 'warn' : null));

    tiles.appendChild(tile('Unavailable entities',
      num(metrics['core.entities.unavailable'], 0),
      num(metrics['core.entities.unavailable_pct'], 1, '% of all')));

    renderEvents($('recent-events'), data.recent_events);
  }

  function renderNowCharts() {
    var metrics = [
      'core.latency_ms',
      'host.psi.memory.some.avg60',
      'host.psi.memory.full.avg60',
      'host.psi.cpu.some.avg60',
      'host.mem.used_pct',
      'host.load.1'
    ].join(',');

    get('api/series?hours=6&metric=' + encodeURIComponent(metrics)).then(function (data) {
      var s = data.series || {};

      Chart.draw($('chart-latency'), {
        series: [{ name: 'Core latency (ms)', points: s['core.latency_ms'] || [] }],
        threshold: 2500
      });

      Chart.draw($('chart-psi'), {
        series: [
          { name: 'memory some', points: s['host.psi.memory.some.avg60'] || [] },
          { name: 'memory full', points: s['host.psi.memory.full.avg60'] || [], color: '#f87171' },
          { name: 'cpu some', points: s['host.psi.cpu.some.avg60'] || [], color: '#4ade80' }
        ],
        format: function (v) { return v.toFixed(0) + '%'; }
      });

      Chart.draw($('chart-mem'), {
        series: [
          { name: 'memory used %', points: s['host.mem.used_pct'] || [] },
          { name: 'load 1m', points: s['host.load.1'] || [], color: '#f59e0b' }
        ]
      });
    });
  }

  // ------------------------------------------------------------ timeline

  function renderIncidents() {
    get('api/incidents').then(function (data) {
      var list = $('incident-list');
      clear(list);
      var incidents = data.incidents || [];

      if (!incidents.length) {
        list.appendChild(el('p', 'muted',
          'No incidents recorded. That is the good outcome — the sentinel is ' +
          'watching and has not seen a gap yet.'));
        return;
      }

      incidents.forEach(function (incident) {
        var open = !incident.ended_ts;
        var node = el('div', 'incident ' + (incident.classification || '') + (open ? ' open' : ''));

        var when = el('div', 'when');
        when.appendChild(el('div', null, fmtDateTime(incident.started_ts)));
        when.appendChild(el('div', 'muted',
          open ? 'ongoing' : fmtDuration(incident.duration_seconds)));

        var what = el('div', 'what');
        what.appendChild(el('span', 'verdict',
          (incident.classification || 'unknown').replace(/_/g, ' ')));
        what.appendChild(el('div', null, incident.summary || ''));

        node.appendChild(when);
        node.appendChild(what);
        node.addEventListener('click', function () { showIncident(incident.id); });
        list.appendChild(node);
      });
    });
  }

  function showIncident(id) {
    get('api/incidents/' + id).then(function (incident) {
      var panel = $('incident-detail');
      panel.classList.remove('hidden');
      clear(panel);

      panel.appendChild(el('h2', null,
        'Incident #' + incident.id + ' — ' +
        (incident.classification || '').replace(/_/g, ' ')));
      panel.appendChild(el('p', null, incident.summary || ''));

      var meta = el('table', 'kv');
      [
        ['Started', fmtDateTime(incident.started_ts)],
        ['Ended', incident.ended_ts ? fmtDateTime(incident.ended_ts) : 'still open'],
        ['Duration', incident.ended_ts
          ? fmtDuration(incident.ended_ts - incident.started_ts) : 'ongoing']
      ].forEach(function (pair) {
        var tr = el('tr');
        tr.appendChild(el('td', null, pair[0]));
        tr.appendChild(el('td', null, pair[1]));
        meta.appendChild(tr);
      });
      panel.appendChild(meta);

      var detail = incident.detail || {};
      var evidence = detail.evidence || [];
      if (evidence.length) {
        panel.appendChild(el('h2', null, 'Evidence from the logs'));
        evidence.slice(0, 20).forEach(function (item) {
          var box = el('div', 'evidence');
          box.appendChild(el('div', null, item.explanation || item.kind));
          box.appendChild(el('div', 'line', item.line || ''));
          panel.appendChild(box);
        });
      }

      if (incident.bundle_path) {
        var link = el('a', 'btn', 'Download evidence bundle');
        link.href = 'api/incidents/' + incident.id + '/bundle';
        link.style.display = 'inline-block';
        link.style.marginTop = '12px';
        link.style.textDecoration = 'none';
        panel.appendChild(link);
      }

      if (incident.events && incident.events.length) {
        panel.appendChild(el('h2', null, 'What else happened around then'));
        var events = el('div', 'events');
        renderEvents(events, incident.events);
        panel.appendChild(events);
      }

      panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  // -------------------------------------------------------- integrations

  function renderIntegrations() {
    get('api/integrations').then(function (data) {
      var note = $('mapping-note');
      if (data.mapped_entities) {
        note.textContent = 'Mapped ' + data.mapped_entities +
          ' entities to integrations via ' +
          (data.mapping_source === 'entity_registry'
            ? 'the entity registry.'
            : 'template fallback (the entity registry was not available).');
      } else {
        note.textContent = 'No entity mapping yet — this populates shortly ' +
          'after Home Assistant connects.';
      }

      // Clusters
      var list = $('cluster-list');
      clear(list);
      if (!data.clusters || !data.clusters.length) {
        list.appendChild(el('p', 'muted',
          'No multi-integration outages detected. That is the good outcome.'));
      } else {
        data.clusters.forEach(function (row) {
          list.appendChild(eventRow(row));
        });
      }

      // Per-integration health
      table($('integration-table'), [
        { label: 'Integration' },
        { label: 'Entities', numeric: true },
        { label: 'Unavailable', numeric: true },
        { label: 'Chronic', numeric: true },
        { label: 'Dead %', numeric: true },
        { label: 'Last drop' }
      ], data.integrations || [], function (row) {
        var tr = el('tr');
        tr.appendChild(el('td', null, row.integration));
        tr.appendChild(el('td', 'num', row.total));

        var dead = el('td', 'num', row.unavailable);
        if (row.unavailable > 0) dead.className = 'num leak';
        tr.appendChild(dead);

        tr.appendChild(el('td', 'num', row.chronic));

        var pct = el('td', 'num', num(row.unavailable_pct, 1));
        if (row.unavailable_pct >= 50) pct.className = 'num bad';
        else if (row.unavailable_pct >= 20) pct.className = 'num leak';
        tr.appendChild(pct);

        tr.appendChild(el('td', 'muted', row.last_drop ? fmtTime(row.last_drop) : '—'));
        return tr;
      });

      // Chronic entities
      table($('chronic-table'), [
        { label: 'Entity' },
        { label: 'Integration' },
        { label: 'State' },
        { label: 'Broken for', numeric: true }
      ], data.chronic || [], function (row) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', row.entity_id));
        tr.appendChild(el('td', null, row.platform));
        tr.appendChild(el('td', null, row.state));
        tr.appendChild(el('td', 'num', fmtDuration(row.dead_seconds)));
        return tr;
      });
    });
  }

  // ---------------------------------------------------------------- host

  function renderHost() {
    get('api/host').then(function (data) {
      var host = data.host || {};
      var os = data.os || {};

      var rows = [
        ['Hostname', host.hostname],
        ['Operating system', host.operating_system],
        ['HAOS version', os.version],
        ['Boot slot', os.boot],
        ['Kernel', host.kernel],
        ['Board', os.board],
        ['Disk', host.disk_used + ' / ' + host.disk_total + ' GB used'],
        ['Disk lifetime used', host.disk_life_time !== undefined ? host.disk_life_time + '%' : null],
        ['PSI available', data.psi_available ? 'yes' : 'no — kernel lacks CONFIG_PSI']
      ];

      var kv = $('host-table');
      clear(kv);
      rows.forEach(function (pair) {
        if (pair[1] === undefined || pair[1] === null || pair[1] === '') return;
        var tr = el('tr');
        tr.appendChild(el('td', null, pair[0]));
        tr.appendChild(el('td', null, pair[1]));
        kv.appendChild(tr);
      });

      table($('serial-table'),
        [{ label: 'Device' }, { label: 'Path' }],
        data.serial_devices || [],
        function (device) {
          var tr = el('tr');
          tr.appendChild(el('td', null, device.name || '—'));
          tr.appendChild(el('td', 'mono', device.by_id || device.dev_path || ''));
          return tr;
        });

      var net = data.network || {};
      table($('network-table'), [
        { label: 'Interface' },
        { label: 'Type' },
        { label: 'Connected' },
        { label: 'Address' },
        { label: 'Gateway' }
      ], net.interfaces || [], function (row) {
        var tr = el('tr');
        tr.appendChild(el('td', null,
          row.interface + (row.primary ? ' (primary)' : '')));
        tr.appendChild(el('td', null, row.type || '—'));
        var conn = el('td', null, row.connected ? 'yes' : 'NO');
        if (!row.connected) conn.className = 'bad';
        tr.appendChild(conn);
        tr.appendChild(el('td', 'mono', row.address || '—'));
        tr.appendChild(el('td', 'mono', row.gateway || '—'));
        return tr;
      });

      renderEvents($('kernel-events'), data.kernel_events,
        'No kernel or hardware events recorded. Nothing has gone wrong at the ' +
        'host layer since the sentinel started watching.');
    });
  }

  // ---------------------------------------------------------- containers

  function renderContainers() {
    get('api/containers').then(function (data) {
      table($('container-table'), [
        { label: 'Add-on' },
        { label: 'CPU %', numeric: true },
        { label: 'Memory', numeric: true },
        { label: 'Mem %', numeric: true },
        { label: 'Growth MB/h', numeric: true },
        { label: 'Restarts', numeric: true }
      ], data.containers || [], function (row) {
        var tr = el('tr');
        tr.appendChild(el('td', null, row.name || row.slug));
        tr.appendChild(el('td', 'num', num(row.cpu, 2)));
        tr.appendChild(el('td', 'num', fmtBytes(row.mem_bytes)));
        tr.appendChild(el('td', 'num', num(row.mem_percent, 1)));

        var slope = row.memory_slope_mb_per_hour;
        var slopeCell = el('td', 'num', slope === null || slope === undefined
          ? '—' : (slope > 0 ? '+' : '') + slope.toFixed(1));
        if (slope !== null && slope !== undefined && slope > 5) {
          slopeCell.className = 'num leak';
          slopeCell.title = 'Memory is trending upward — possible leak';
        }
        tr.appendChild(slopeCell);

        var restartCell = el('td', 'num', row.restarts || 0);
        if (row.restarts > 2) restartCell.className = 'num bad';
        tr.appendChild(restartCell);
        return tr;
      });
    });
  }

  // ------------------------------------------------------------ recorder

  function renderRecorder() {
    get('api/recorder').then(function (data) {
      var tiles = $('recorder-tiles');
      clear(tiles);

      var size = data.db_size_bytes;
      tiles.appendChild(tile('Database size', fmtBytes(size), 'recorder',
        size && size > 20 * 1024 * 1024 * 1024 ? 'err'
          : size && size > 5 * 1024 * 1024 * 1024 ? 'warn' : null));
      tiles.appendChild(tile('Event bus', data.connected ? 'Connected' : 'Disconnected',
        'state_changed stream', data.connected ? 'ok' : 'err'));

      Chart.draw($('chart-db'), {
        series: [{ name: 'Database size', points: data.size_history || [] }],
        format: function (v) { return (v / (1024 * 1024 * 1024)).toFixed(1) + 'G'; }
      });

      table($('writer-table'), [
        { label: 'Entity' },
        { label: 'Changes', numeric: true },
        { label: 'Per hour', numeric: true }
      ], data.top_writers || [], function (row) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', row.entity_id));
        tr.appendChild(el('td', 'num', row.changes));
        tr.appendChild(el('td', 'num', num(row.per_hour, 1)));
        return tr;
      });
    });
  }

  // ---------------------------------------------------------------- logs

  function renderLogs() {
    var source = $('log-source').value;
    var search = $('log-search').value;
    var url = 'api/logs?lines=600&source=' + encodeURIComponent(source);
    if (search) url += '&search=' + encodeURIComponent(search);

    updateDownloadLinks(source, search);

    $('log-output').textContent = 'Loading…';
    get(url).then(function (data) {
      $('log-output').textContent = data.text || '(empty)';
    }).catch(function (err) {
      $('log-output').textContent = 'Could not load logs: ' + err.message;
    });
  }

  function stamp() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
           '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }

  // Plain anchors rather than a fetch-and-Blob download: the dashboard runs
  // inside the Home Assistant ingress iframe, where script-initiated blob
  // downloads can be blocked but a normal link with Content-Disposition is not.
  function updateDownloadLinks(source, search) {
    var single = 'api/logs/export?lines=5000&source=' + encodeURIComponent(source);
    if (search) single += '&search=' + encodeURIComponent(search);

    var one = $('log-download');
    one.href = single;
    one.setAttribute('download', 'ha-' + source + '-' + stamp() + '.log');

    var all = $('log-download-full');
    all.href = 'api/logs/export?full=1&lines=3000';
    all.setAttribute('download', 'ha-diagnostic-' + stamp() + '.txt');
  }

  // -------------------------------------------------------------- driver

  function refresh() {
    get('api/status').then(function (data) {
      renderStatus(data);
      var note = $('footer-note');
      var caps = data.notifications || {};
      note.textContent = caps.telegram
        ? 'Telegram alerts enabled.'
        : 'No outbound alerting configured — set a Telegram token in the add-on options.';
    }).catch(function (err) {
      $('banner').className = 'banner critical';
      $('banner').textContent = 'Dashboard cannot reach the sentinel: ' + err.message;
    });

    if (state.view === 'now') renderNowCharts();
    if (state.view === 'timeline') renderIncidents();
    if (state.view === 'integrations') renderIntegrations();
    if (state.view === 'host') renderHost();
    if (state.view === 'containers') renderContainers();
    if (state.view === 'recorder') renderRecorder();
  }

  function init() {
    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.addEventListener('click', function () { switchView(tab.dataset.view); });
    });

    $('log-refresh').addEventListener('click', renderLogs);
    $('log-source').addEventListener('change', renderLogs);
    $('log-search').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') renderLogs();
    });

    $('test-alert').addEventListener('click', function () {
      var button = $('test-alert');
      button.disabled = true;
      button.textContent = 'Sending…';
      fetch('api/test-alert', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (result) {
          button.textContent = result.ok ? 'Test sent' : (result.reason || 'Not configured');
        })
        .catch(function () { button.textContent = 'Failed'; })
        .finally(function () {
          setTimeout(function () {
            button.disabled = false;
            button.textContent = 'Send test alert';
          }, 4000);
        });
    });

    // Debounced: resize fires continuously while dragging a window edge, and
    // each call refetches six metric series.
    var resizeTimer = null;
    window.addEventListener('resize', function () {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        resizeTimer = null;
        if (state.view === 'now') renderNowCharts();
      }, 250);
    });

    refresh();
    renderLogs();
    state.timer = setInterval(refresh, REFRESH_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
