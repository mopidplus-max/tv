/* Shared HLS player for the public channel and admin preview. */
(function () {
  "use strict";

  const root = document.querySelector("[data-player]");
  if (!root) return;

  const video = root.querySelector("video");
  if (!video) return;

  const streamUrl = root.getAttribute("data-stream");
  const nowUrl = root.getAttribute("data-now");
  const heartbeatUrl = root.getAttribute("data-heartbeat");
  const loading = root.querySelector("[data-player-loading]");
  const errorBox = root.querySelector("[data-player-error]");
  const errorText = root.querySelector("[data-player-error-text]");
  const retryButton = root.querySelector("[data-player-retry]");
  const playButton = root.querySelector("[data-player-play]");
  const muteButton = root.querySelector("[data-player-mute]");
  const volumeInput = root.querySelector("[data-player-volume]");
  const fullscreenButton = root.querySelector("[data-player-fullscreen]");
  const progressBar = root.querySelector("[data-player-progress]");
  const timeLabel = root.querySelector("[data-player-time]");
  const clockLabel = root.querySelector("[data-player-clock]");
  const titleLabel = root.querySelector("[data-player-title]");
  const nextLabel = root.querySelector("[data-player-next]");

  let hls = null;
  let retries = 0;
  let retryTimer = null;
  let heartbeatTimer = null;
  let nowTimer = null;
  let lastProgress = 0;

  function setIcon(button, name) {
    if (!button) return;
    const use = button.querySelector("use");
    if (use) use.setAttribute("href", "#i-" + name);
  }

  function showLoading(show) {
    if (loading) loading.hidden = !show;
  }

  function showError(message) {
    if (errorText) errorText.textContent = message || "Поток недоступен";
    if (errorBox) errorBox.hidden = false;
    showLoading(false);
  }

  function clearError() {
    if (errorBox) errorBox.hidden = true;
  }

  function startPlayback() {
    const playPromise = video.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch(function () {
        root.classList.add("is-paused");
        setIcon(playButton, "play");
      });
    }
  }

  function attachNative() {
    video.src = streamUrl;
    video.addEventListener("loadedmetadata", function () {
      showLoading(false);
      clearError();
      startPlayback();
    }, { once: true });
    video.addEventListener("error", function () {
      showError("Браузер не смог воспроизвести поток");
    });
    startPlayback();
  }

  function destroyHls() {
    if (hls) {
      try { hls.destroy(); } catch (error) {}
      hls = null;
    }
  }

  function scheduleRetry() {
    if (retries >= 6) {
      showError("Не удалось подключиться к эфиру. Повторите позже.");
      return;
    }
    retries += 1;
    showLoading(true);
    showError("");
    if (errorBox) errorBox.hidden = true;
    window.clearTimeout(retryTimer);
    retryTimer = window.setTimeout(function () {
      loadStream(true);
    }, Math.min(8000, 1500 * retries));
  }

  function loadStream(isRetry) {
    destroyHls();
    clearError();
    showLoading(true);
    if (!isRetry) retries = 0;

    if (!streamUrl) {
      showError("Поток ещё не сформирован");
      return;
    }

    if (window.Hls && window.Hls.isSupported()) {
      hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        liveSyncDurationCount: 3,
        maxBufferLength: 20,
        backBufferLength: 10
      });
      hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
        showLoading(false);
        clearError();
        retries = 0;
        startPlayback();
      });
      hls.on(window.Hls.Events.ERROR, function (event, data) {
        if (!data || !data.fatal) return;
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          scheduleRetry();
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          try { hls.recoverMediaError(); } catch (error) { scheduleRetry(); }
        } else {
          showError("Ошибка HLS-потока");
          scheduleRetry();
        }
      });
      hls.loadSource(streamUrl);
      hls.attachMedia(video);
      return;
    }

    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      attachNative();
      return;
    }

    showError("HLS не поддерживается этим браузером");
  }

  /* Playback controls ---------------------------------------------- */
  playButton && playButton.addEventListener("click", function () {
    if (video.paused) {
      video.play().catch(function () {});
    } else {
      video.pause();
    }
  });

  video.addEventListener("play", function () {
    root.classList.remove("is-paused");
    setIcon(playButton, "pause");
    clearError();
    showLoading(false);
  });
  video.addEventListener("pause", function () {
    root.classList.add("is-paused");
    setIcon(playButton, "play");
  });
  video.addEventListener("waiting", function () {
    if (!video.paused) showLoading(true);
  });
  video.addEventListener("playing", function () {
    showLoading(false);
    clearError();
  });
  video.addEventListener("volumechange", function () {
    if (volumeInput) volumeInput.value = String(video.muted ? 0 : video.volume);
    setIcon(muteButton, video.muted || video.volume === 0 ? "mute" : "volume");
  });

  muteButton && muteButton.addEventListener("click", function () {
    video.muted = !video.muted;
    if (!video.muted && video.volume === 0) video.volume = 0.8;
  });

  volumeInput && volumeInput.addEventListener("input", function () {
    const value = Number(volumeInput.value);
    video.volume = value;
    video.muted = value === 0;
  });

  fullscreenButton && fullscreenButton.addEventListener("click", function () {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(function () {});
    } else if (root.requestFullscreen) {
      root.requestFullscreen().catch(function () {});
    }
  });

  root.addEventListener("click", function (event) {
    if (event.target.closest("button, input, a")) return;
    if (video.paused) video.play().catch(function () {});
  });

  document.addEventListener("keydown", function (event) {
    if (event.target && /input|textarea|select/i.test(event.target.tagName)) return;
    if (event.code === "Space") {
      event.preventDefault();
      if (video.paused) video.play().catch(function () {}); else video.pause();
    } else if (event.key === "m" || event.key === "M") {
      video.muted = !video.muted;
    } else if (event.key === "f" || event.key === "F") {
      if (fullscreenButton) fullscreenButton.click();
    }
  });

  retryButton && retryButton.addEventListener("click", function () {
    retries = 0;
    loadStream(true);
  });

  if (window.matchMedia && window.matchMedia("(hover: none)").matches) {
    root.classList.add("is-touch");
  }

  /* Clock and live programme metadata ------------------------------ */
  function tickClock() {
    if (!clockLabel) return;
    const now = new Date();
    clockLabel.textContent = now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }
  tickClock();
  window.setInterval(tickClock, 15000);

  function updateProgress(percent) {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    lastProgress = value;
    if (progressBar) progressBar.style.width = value + "%";
  }

  async function refreshNow() {
    if (!nowUrl || document.hidden) return;
    try {
      const response = await fetch(nowUrl, { credentials: "same-origin", headers: { "Accept": "application/json" } });
      if (!response.ok) return;
      const data = await response.json();
      if (!data || !data.ok) return;
      if (data.current) {
        if (titleLabel) titleLabel.textContent = data.current.title || "Прямой эфир";
      }
      if (nextLabel) {
        nextLabel.textContent = data.next && data.next.title ? "Далее: " + data.next.title : "";
      }
      if (data.progress) {
        updateProgress(data.progress.percent);
        if (timeLabel && data.progress.remaining != null) {
          timeLabel.textContent = "LIVE";
        }
      }
    } catch (error) {
      /* Keep the current overlay if the API is momentarily unavailable. */
    }
  }

  /* Viewer heartbeat ----------------------------------------------- */
  function viewerId() {
    try {
      let id = window.localStorage.getItem("simple_viewer_id");
      if (!id || !/^[A-Za-z0-9_-]{8,80}$/.test(id)) {
        id = (window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9_-]/g, "");
        window.localStorage.setItem("simple_viewer_id", id);
      }
      return id;
    } catch (error) {
      return String(Date.now()) + Math.random().toString(16).slice(2, 12);
    }
  }

  function sendHeartbeat() {
    if (!heartbeatUrl) return;
    const payload = JSON.stringify({ id: viewerId() });
    if (navigator.sendBeacon) {
      try {
        const blob = new Blob([payload], { type: "application/json" });
        if (navigator.sendBeacon(heartbeatUrl, blob)) return;
      } catch (error) {}
    }
    fetch(heartbeatUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: payload,
      keepalive: true
    }).catch(function () {});
  }

  loadStream(false);
  if (nowUrl) {
    refreshNow();
    nowTimer = window.setInterval(refreshNow, 15000);
  }
  if (heartbeatUrl) {
    sendHeartbeat();
    heartbeatTimer = window.setInterval(sendHeartbeat, 30000);
  }
  window.addEventListener("beforeunload", function () {
    window.clearInterval(nowTimer);
    window.clearInterval(heartbeatTimer);
    window.clearTimeout(retryTimer);
    destroyHls();
  });
})();
