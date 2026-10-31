/* Monitoring page live refresh. */
(function () {
  "use strict";

  const root = document.querySelector("[data-monitoring-url]");
  if (!root) return;
  const url = root.getAttribute("data-monitoring-url");
  let busy = false;

  function setMetric(name, value) {
    document.querySelectorAll('[data-metric="' + name + '"]').forEach(function (node) {
      node.textContent = value == null || value === "" ? "—" : value;
    });
  }

  function setBar(name, value) {
    const width = Math.max(0, Math.min(100, Number(value) || 0));
    document.querySelectorAll('[data-metric-bar="' + name + '"]').forEach(function (node) {
      node.style.width = width + "%";
    });
  }

  async function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const response = await fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } });
      if (!response.ok) return;
      const data = await response.json();
      if (!data || data.ok === false) return;
      const m = data.metrics || {};
      const e = data.engine || {};

      setMetric("cpu", (m.cpu != null ? m.cpu : "—") + "%");
      setMetric("memory", (m.memory_percent != null ? m.memory_percent : "—") + "%");
      setMetric("disk", (m.disk_percent != null ? m.disk_percent : "—") + "%");
      setMetric("net", "↓ " + (m.net_recv_label || "—"));
      setMetric("net-out", "↑ " + (m.net_sent_label || "—"));
      setMetric("ffmpeg", m.ffmpeg_processes);
      setMetric("segments", m.hls_segments);
      setBar("cpu", m.cpu);
      setBar("memory", m.memory_percent);
      setBar("disk", m.disk_percent);

      setMetric("engine-state", e.state || "—");
      setMetric("engine-phase", e.phase || "—");
      setMetric("ffmpeg-state", e.ffmpeg_running ? "работает" : "ожидает");
      setMetric("ffmpeg-pid", e.ffmpeg_pid || "—");
      setMetric("program", e.program_title || "—");
      setMetric("media", e.media_title || "—");
      setMetric("progress", (e.progress != null ? e.progress : 0) + "%");
      setMetric("viewers", e.viewers != null ? e.viewers : data.viewers);
      setMetric("hls", e.hls_ready ? "готов" : "нет");
      setMetric("hls-age", e.hls_playlist_age != null ? e.hls_playlist_age : "—");
    } catch (error) {
      /* Ignore transient errors; the next tick retries. */
    } finally {
      busy = false;
    }
  }

  refresh();
  window.setInterval(refresh, 3000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });
})();
