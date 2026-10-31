/* SIMPLE admin panel behaviour (no inline scripts, CSP-safe). */
(function () {
  "use strict";

  const meta = document.querySelector('meta[name="csrf-token"]');
  const csrf = meta ? meta.getAttribute("content") : "";
  const body = document.body;
  const statusUrl = body ? body.getAttribute("data-status-url") : null;

  function toast(message, kind) {
    const stack = document.getElementById("toast-stack");
    if (!stack) return;
    const item = document.createElement("div");
    item.className = "toast" + (kind === "error" ? " error" : "");
    item.textContent = message;
    stack.appendChild(item);
    window.setTimeout(function () {
      item.remove();
    }, 4200);
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": csrf
      },
      body: JSON.stringify(Object.assign({ csrf_token: csrf }, payload || {}))
    });
    if (response.status === 401) {
      window.location.reload();
      throw new Error("Сессия истекла");
    }
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    if (!response.ok || !data || data.ok === false) {
      throw new Error((data && (data.error || data.message)) || "Ошибка запроса");
    }
    return data;
  }

  /* Navigation ------------------------------------------------------ */
  const sidebar = document.getElementById("sidebar");
  document.querySelectorAll("[data-sidebar-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (sidebar) sidebar.classList.toggle("open");
    });
  });
  document.addEventListener("click", function (event) {
    if (!sidebar || !sidebar.classList.contains("open")) return;
    if (sidebar.contains(event.target) || event.target.closest("[data-sidebar-toggle]")) return;
    sidebar.classList.remove("open");
  });

  document.querySelectorAll("[data-dismiss]").forEach(function (button) {
    button.addEventListener("click", function () {
      const flash = button.closest(".flash");
      if (flash) flash.remove();
    });
  });

  document.querySelectorAll("[data-toggle-password]").forEach(function (button) {
    button.addEventListener("click", function () {
      const input = button.parentElement ? button.parentElement.querySelector("input") : null;
      if (!input) return;
      input.type = input.type === "password" ? "text" : "password";
    });
  });

  /* Confirmation dialogs ------------------------------------------- */
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      const message = form.getAttribute("data-confirm") || "Подтвердить действие?";
      if (!window.confirm(message)) event.preventDefault();
    });
  });

  /* Collapsible panels / table rows -------------------------------- */
  document.querySelectorAll("[data-toggle-panel]").forEach(function (button) {
    button.addEventListener("click", function () {
      const panel = document.getElementById(button.getAttribute("data-toggle-panel"));
      if (panel) panel.classList.toggle("hidden");
    });
  });
  document.querySelectorAll("[data-toggle-row]").forEach(function (button) {
    button.addEventListener("click", function () {
      const row = document.getElementById(button.getAttribute("data-toggle-row"));
      if (row) row.classList.toggle("hidden");
    });
  });

  /* Director controls ---------------------------------------------- */
  document.querySelectorAll("[data-director]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const url = button.getAttribute("data-director");
      if (!url) return;
      button.disabled = true;
      try {
        const data = await postJSON(url, {});
        toast(data.message || "Готово");
        refreshStatus();
      } catch (error) {
        toast(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-play-item]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const url = button.getAttribute("data-play-item");
      const select = document.querySelector("[data-next-item-select]");
      if (!url || !select || !select.value) {
        toast("Выберите ролик", "error");
        return;
      }
      button.disabled = true;
      try {
        const data = await postJSON(url, { item_id: Number(select.value) });
        toast(data.message || "Ролик поставлен в эфир");
        refreshStatus();
      } catch (error) {
        toast(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  });

  /* Media preview dialog ------------------------------------------- */
  const dialog = document.getElementById("preview-dialog");
  const previewVideo = document.getElementById("preview-video");
  let hlsInstance = null;

  function stopPreview() {
    if (hlsInstance) {
      hlsInstance.destroy();
      hlsInstance = null;
    }
    if (previewVideo) {
      previewVideo.pause();
      previewVideo.removeAttribute("src");
      previewVideo.load();
    }
  }

  function openPreview(button) {
    if (!dialog || !previewVideo) return;
    const url = button.getAttribute("data-preview-url");
    const title = button.getAttribute("data-preview-title") || "Предпросмотр";
    const kind = button.getAttribute("data-preview-kind") || "";
    const titleNode = dialog.querySelector("[data-preview-title]");
    if (titleNode) titleNode.textContent = title;
    stopPreview();
    if (kind === "hls" && window.Hls && window.Hls.isSupported()) {
      hlsInstance = new window.Hls({ enableWorker: true });
      hlsInstance.loadSource(url);
      hlsInstance.attachMedia(previewVideo);
    } else {
      previewVideo.src = url;
    }
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "open");
    previewVideo.play().catch(function () {});
  }

  document.querySelectorAll("[data-preview-url]").forEach(function (button) {
    button.addEventListener("click", function () {
      openPreview(button);
    });
  });
  document.querySelectorAll("[data-close-dialog]").forEach(function (button) {
    button.addEventListener("click", function () {
      stopPreview();
      if (dialog && typeof dialog.close === "function") dialog.close();
    });
  });
  if (dialog) {
    dialog.addEventListener("close", stopPreview);
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) {
        stopPreview();
        dialog.close();
      }
    });
  }

  /* Sortable program queue ---------------------------------------- */
  document.querySelectorAll("[data-sortable]").forEach(function (list) {
    let dragged = null;

    function saveOrder() {
      const url = list.getAttribute("data-reorder-url");
      if (!url) return;
      const order = Array.from(list.querySelectorAll("[data-item-id]")).map(function (row) {
        return Number(row.getAttribute("data-item-id"));
      });
      postJSON(url, { order: order })
        .then(function (data) { toast(data.message || "Порядок сохранён"); })
        .catch(function (error) { toast(error.message, "error"); });
    }

    list.addEventListener("dragstart", function (event) {
      const row = event.target.closest("[data-item-id]");
      if (!row) return;
      dragged = row;
      row.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      try { event.dataTransfer.setData("text/plain", row.getAttribute("data-item-id")); } catch (error) {}
    });
    list.addEventListener("dragend", function () {
      if (dragged) dragged.classList.remove("dragging");
      list.querySelectorAll(".drag-over").forEach(function (row) { row.classList.remove("drag-over"); });
      dragged = null;
    });
    list.addEventListener("dragover", function (event) {
      event.preventDefault();
      const row = event.target.closest("[data-item-id]");
      if (!row || row === dragged) return;
      list.querySelectorAll(".drag-over").forEach(function (item) { item.classList.remove("drag-over"); });
      row.classList.add("drag-over");
    });
    list.addEventListener("drop", function (event) {
      event.preventDefault();
      const row = event.target.closest("[data-item-id]");
      list.querySelectorAll(".drag-over").forEach(function (item) { item.classList.remove("drag-over"); });
      if (!row || !dragged || row === dragged) return;
      const rows = Array.from(list.querySelectorAll("[data-item-id]"));
      const from = rows.indexOf(dragged);
      const to = rows.indexOf(row);
      if (from < 0 || to < 0) return;
      if (from < to) row.after(dragged);
      else row.before(dragged);
      Array.from(list.querySelectorAll("[data-item-id]")).forEach(function (item, index) {
        const badge = item.querySelector(".item-index");
        if (badge) badge.textContent = String(index + 1).padStart(2, "0");
      });
      saveOrder();
    });
  });

  document.querySelectorAll("[data-save-order]").forEach(function (button) {
    button.addEventListener("click", function () {
      const list = document.querySelector("[data-sortable]");
      if (!list) return;
      const url = list.getAttribute("data-reorder-url");
      const order = Array.from(list.querySelectorAll("[data-item-id]")).map(function (row) {
        return Number(row.getAttribute("data-item-id"));
      });
      postJSON(url, { order: order })
        .then(function (data) { toast(data.message || "Порядок сохранён"); })
        .catch(function (error) { toast(error.message, "error"); });
    });
  });

  /* Live status ---------------------------------------------------- */
  function timeLabel(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  function setLive(name, value) {
    document.querySelectorAll('[data-live="' + name + '"]').forEach(function (node) {
      node.textContent = value;
    });
  }

  function setBar(name, value) {
    const width = Math.max(0, Math.min(100, Number(value) || 0));
    document.querySelectorAll('[data-live-bar="' + name + '"]').forEach(function (node) {
      node.style.width = width + "%";
    });
  }

  let refreshing = false;
  async function refreshStatus() {
    if (!statusUrl || refreshing || document.hidden) return;
    refreshing = true;
    try {
      const response = await fetch(statusUrl, { credentials: "same-origin", headers: { "Accept": "application/json" } });
      if (!response.ok) return;
      const data = await response.json();
      if (!data || data.ok === false) return;
      const engine = data.engine || {};
      const metrics = data.metrics || {};
      const next = data.next || null;
      const labels = { on: "В эфире", off: "Выключен", maintenance: "Профилактика" };

      setLive("mode", labels[data.mode] || data.mode || "—");
      setLive("engine-state", engine.state || "—");
      setLive("engine-sub", engine.ffmpeg_running ? "FFmpeg работает" : "FFmpeg ожидает");
      setLive("director-state", engine.state || "—");
      setLive("viewers", engine.viewers != null ? engine.viewers : (data.viewers || 0));
      setLive("cpu", (metrics.cpu != null ? metrics.cpu : "—") + "%");
      setLive("memory", (metrics.memory_percent != null ? metrics.memory_percent : "—") + "%");
      setLive("disk", (metrics.disk_percent != null ? metrics.disk_percent : "—") + "%");
      setLive("ffmpeg", metrics.ffmpeg_processes != null ? metrics.ffmpeg_processes : "—");
      setLive("segments", metrics.hls_segments != null ? metrics.hls_segments : "—");
      setLive("ffmpeg-state", engine.ffmpeg_running ? "работает" : "ожидает");
      setLive("ffmpeg-pid", engine.ffmpeg_pid || "—");
      setLive("hls", engine.hls_ready ? "готов" : "нет");
      setLive("program-title", engine.program_title || "Ожидание эфира");
      setLive("program-description", engine.program_description || "Описание отсутствует.");
      setLive("media-title", engine.media_title || "—");
      setLive("item-index", ((engine.item_index || 0) + 1) + " / " + (engine.items_total || 0));
      setLive("progress-label", (engine.progress || 0) + "%");
      setLive("program-start", timeLabel(engine.program_started_at));
      setLive("program-end", timeLabel(engine.program_ends_at));
      setLive("program-time", timeLabel(engine.program_started_at) + " — " + timeLabel(engine.program_ends_at));
      setLive("remaining", engine.remaining != null ? formatDuration(engine.remaining) : "—");
      if (next) {
        setLive("next-title", next.title || "—");
        setLive("next-description", next.description || "Следующая передача появится здесь.");
        setLive("next-start", timeLabel(next.start));
        setLive("next-end", timeLabel(next.end));
      }
      setBar("cpu", metrics.cpu);
      setBar("memory", metrics.memory_percent);
      setBar("disk", metrics.disk_percent);
      setBar("program", engine.program_progress != null ? engine.program_progress : engine.progress);
    } catch (error) {
      /* Polling is best-effort; keep the UI stable. */
    } finally {
      refreshing = false;
    }
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours) return hours + ":" + String(minutes).padStart(2, "0") + ":" + String(secs).padStart(2, "0");
    return minutes + ":" + String(secs).padStart(2, "0");
  }

  if (statusUrl) {
    refreshStatus();
    window.setInterval(refreshStatus, 5000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) refreshStatus();
    });
  }
})();
