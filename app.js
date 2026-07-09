// CAPTURE — frontend logic. No build step, no dependencies.
// Talks to the same-origin API: /api/info, /api/download, /api/status/:id, /api/file/:id

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    form: $("fetch-form"),
    urlInput: $("url-input"),
    fetchBtn: $("fetch-btn"),
    errorBox: $("error-box"),

    mediaPanel: $("media-panel"),
    thumb: $("media-thumb"),
    title: $("media-title"),
    uploader: $("media-uploader"),
    uploaderSep: $("uploader-sep"),
    duration: $("media-duration"),

    ladderPanel: $("ladder-panel"),
    ladderList: $("ladder-list"),
    downloadBtn: $("download-btn"),

    statusPanel: $("status-panel"),
    tallyDot: $("tally-dot"),
    statusText: $("status-text"),
    downloadLink: $("download-link"),
  };

  const state = {
    formats: [],
    selectedFormatId: null,
    selectedIsAudioOnly: false,
    pollHandle: null,
  };

  // ---------- formatting helpers ----------

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "";
    seconds = Math.round(seconds);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  function formatFileSize(bytes) {
    if (!bytes || bytes <= 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    let n = bytes;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i += 1;
    }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function formatSpec(fmt) {
    const parts = [];
    if (fmt.resolution) parts.push(fmt.resolution);
    else if (!fmt.has_video) parts.push("audio");
    if (fmt.ext) parts.push(fmt.ext);
    const size = formatFileSize(fmt.filesize);
    if (size) parts.push(size);
    return parts.join(" \u00b7 ");
  }

  // ---------- error / status UI ----------

  function showError(message) {
    els.errorBox.textContent = message;
    els.errorBox.hidden = false;
  }

  function clearError() {
    els.errorBox.hidden = true;
    els.errorBox.textContent = "";
  }

  function setFetching(isFetching) {
    els.fetchBtn.disabled = isFetching;
    els.urlInput.classList.toggle("scanning", isFetching);
    els.fetchBtn.querySelector(".btn-label").textContent = isFetching ? "Fetching…" : "Fetch";
  }

  // ---------- ladder rendering ----------

  function renderLadder(formats) {
    state.formats = formats;
    state.selectedFormatId = null;
    state.selectedIsAudioOnly = false;
    els.downloadBtn.disabled = true;
    els.ladderList.innerHTML = "";

    formats.forEach((fmt, idx) => {
      const rung = document.createElement("div");
      rung.className = "rung";
      rung.setAttribute("role", "radio");
      rung.setAttribute("aria-checked", "false");
      rung.tabIndex = idx === 0 ? 0 : -1;
      rung.dataset.formatId = fmt.format_id;
      rung.dataset.audioOnly = fmt.has_video ? "false" : "true";

      const label = document.createElement("span");
      label.className = "rung-label";
      label.textContent = !fmt.has_video ? "Audio only" : fmt.note || "Video";

      const spec = document.createElement("span");
      spec.className = "rung-spec mono";
      spec.textContent = formatSpec(fmt);

      rung.append(label, spec);
      rung.addEventListener("click", () => selectRung(rung));
      rung.addEventListener("keydown", (e) => handleRungKeydown(e, rung));

      els.ladderList.appendChild(rung);
    });
  }

  function selectRung(rung) {
    els.ladderList.querySelectorAll(".rung").forEach((r) => {
      r.setAttribute("aria-checked", "false");
      r.tabIndex = -1;
    });
    rung.setAttribute("aria-checked", "true");
    rung.tabIndex = 0;
    rung.focus();

    state.selectedFormatId = rung.dataset.formatId;
    state.selectedIsAudioOnly = rung.dataset.audioOnly === "true";
    els.downloadBtn.disabled = false;
  }

  function handleRungKeydown(e, rung) {
    const rungs = Array.from(els.ladderList.querySelectorAll(".rung"));
    const idx = rungs.indexOf(rung);

    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      selectRung(rung);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = e.key === "ArrowDown" ? rungs[(idx + 1) % rungs.length] : rungs[(idx - 1 + rungs.length) % rungs.length];
      selectRung(next);
    }
  }

  // ---------- API calls ----------

  async function fetchInfo(url) {
    const res = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Couldn't read that link.");
    return data;
  }

  async function startDownload(url, formatId, audioOnly) {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, format_id: audioOnly ? null : formatId, audio_only: audioOnly }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Couldn't start the download.");
    return data;
  }

  async function fetchStatus(jobId) {
    const res = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Lost track of that job.");
    return data;
  }

  // ---------- status polling ----------

  function setTally(mode) {
    els.tallyDot.className = "tally-dot" + (mode ? ` ${mode}` : "");
  }

  function stopPolling() {
    if (state.pollHandle) {
      clearTimeout(state.pollHandle);
      state.pollHandle = null;
    }
  }

  function pollStatus(jobId) {
    stopPolling();

    const tick = async () => {
      let data;
      try {
        data = await fetchStatus(jobId);
      } catch (err) {
        setTally("failed");
        els.statusText.textContent = err.message;
        return;
      }

      if (data.status === "queued") {
        setTally("active");
        els.statusText.textContent = "Queued…";
        state.pollHandle = setTimeout(tick, 1200);
      } else if (data.status === "downloading") {
        setTally("active");
        els.statusText.textContent = "Downloading…";
        state.pollHandle = setTimeout(tick, 1200);
      } else if (data.status === "completed") {
        setTally("done");
        els.statusText.textContent = "Ready.";
        els.downloadLink.href = `/api/file/${encodeURIComponent(jobId)}`;
        els.downloadLink.hidden = false;
      } else if (data.status === "failed") {
        setTally("failed");
        els.statusText.textContent = data.error || "Download failed.";
      }
    };

    tick();
  }

  // ---------- event wiring ----------

  els.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearError();

    const url = els.urlInput.value.trim();
    if (!url) return;

    els.mediaPanel.hidden = true;
    els.ladderPanel.hidden = true;
    els.statusPanel.hidden = true;
    stopPolling();

    setFetching(true);
    try {
      const info = await fetchInfo(url);

      els.thumb.src = info.thumbnail || "";
      els.thumb.alt = info.title ? `Thumbnail for ${info.title}` : "";
      els.title.textContent = info.title || "Untitled";
      els.uploader.textContent = info.uploader || "";
      const durationText = formatDuration(info.duration);
      els.duration.textContent = durationText;
      els.uploaderSep.hidden = !(info.uploader && durationText);
      els.mediaPanel.hidden = false;

      const formats = (info.formats || []).slice();
      formats.push({
        format_id: "audio-only",
        ext: "mp3",
        resolution: null,
        has_video: false,
        has_audio: true,
        filesize: null,
        note: "Audio only",
      });
      renderLadder(formats);
      els.ladderPanel.hidden = false;
    } catch (err) {
      showError(err.message);
    } finally {
      setFetching(false);
    }
  });

  els.downloadBtn.addEventListener("click", async () => {
    if (!state.selectedFormatId) return;
    clearError();

    els.downloadBtn.disabled = true;
    els.statusPanel.hidden = false;
    els.downloadLink.hidden = true;
    setTally("active");
    els.statusText.textContent = "Starting…";

    try {
      const url = els.urlInput.value.trim();
      const job = await startDownload(url, state.selectedFormatId, state.selectedIsAudioOnly);
      pollStatus(job.job_id);
    } catch (err) {
      setTally("failed");
      els.statusText.textContent = err.message;
    } finally {
      els.downloadBtn.disabled = false;
    }
  });
})();
