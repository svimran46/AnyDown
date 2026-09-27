// CAPTURE — frontend logic with Google Authentication & Quality Gating.
// Talks to the same-origin API: /api/config, /api/auth/*, /api/media/inspect, /api/download, /api/status/:id, /api/file/:id

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
    ladderHint: $("ladder-hint"),
    downloadBtn: $("download-btn"),

    statusPanel: $("status-panel"),
    tallyDot: $("tally-dot"),
    statusText: $("status-text"),
    downloadLink: $("download-link"),

    authBar: $("auth-bar"),
    loginTriggerBtn: $("login-trigger-btn"),
    userBadge: $("user-badge"),
    userAvatar: $("user-avatar"),
    userName: $("user-name"),
    logoutBtn: $("logout-btn"),

    loginModal: $("login-modal"),
    modalCloseBtn: $("modal-close-btn"),
    modalDesc: $("modal-desc"),
    gSigninElement: $("g-signin-element"),

    statusTip: $("status-tip"),
    statusTipText: $("status-tip-text"),

    minimalAuthCard: $("minimal-auth-card"),
    minimalAuthTag: $("minimal-auth-tag"),
    minimalAuthTitle: $("minimal-auth-title"),
    minimalGSigninElement: $("minimal-g-signin-element"),
  };

  const authState = {
    authenticated: false,
    user: null,
    googleClientId: null,
    guestMaxHeight: 720,
    pendingDownload: null, // { url, formatId, isAudioOnly }
    gisInitialized: false,
  };

  const state = {
    formats: [],
    selectedFormatId: null,
    selectedIsAudioOnly: false,
    selectedIsLocked: false,
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

  // ---------- auth UI management ----------

  function setAuthenticatedUser(user) {
    authState.authenticated = true;
    authState.user = user;

    if (els.loginTriggerBtn) els.loginTriggerBtn.hidden = true;
    if (els.userBadge) els.userBadge.hidden = false;
    if (els.userName) els.userName.textContent = user.name || (user.email ? user.email.split("@")[0] : "User");
    if (els.userAvatar) {
      if (user.avatarUrl) {
        els.userAvatar.src = user.avatarUrl;
        els.userAvatar.hidden = false;
      } else {
        els.userAvatar.hidden = true;
      }
    }
    if (els.ladderHint) {
      els.ladderHint.textContent = "All qualities unlocked";
    }
  }

  function setGuestUser() {
    authState.authenticated = false;
    authState.user = null;

    if (els.loginTriggerBtn) els.loginTriggerBtn.hidden = false;
    if (els.userBadge) els.userBadge.hidden = true;
    if (els.userAvatar) els.userAvatar.removeAttribute("src");
    if (els.userName) els.userName.textContent = "";
    if (els.ladderHint) {
      els.ladderHint.textContent = `> ${authState.guestMaxHeight}p requires sign in`;
    }
  }

  function openLoginModal(desc) {
    if (desc && els.modalDesc) {
      els.modalDesc.textContent = desc;
    } else if (els.modalDesc) {
      els.modalDesc.textContent = `Sign in with Google to unlock 1080p, 1440p, 4K, and high-bitrate video downloads.`;
    }
    if (els.loginModal) els.loginModal.hidden = false;
    renderGoogleButton();
  }

  function closeLoginModal() {
    if (els.loginModal) els.loginModal.hidden = true;
  }

  function showMinimalAuthCard(fmt, labelText) {
    if (!els.minimalAuthCard || authState.authenticated) return;
    const res = fmt ? (fmt.resolution || (fmt.height ? `${fmt.height}p` : fmt.note || labelText || "HD")) : (labelText || "HD");
    if (els.minimalAuthTag) els.minimalAuthTag.textContent = `Quality Gate: ${res}`;
    if (els.minimalAuthTitle) els.minimalAuthTitle.textContent = `Sign in with Google to download in ${res}`;
    els.minimalAuthCard.hidden = false;
    renderGoogleButtons();
  }

  function hideMinimalAuthCard() {
    if (els.minimalAuthCard) {
      els.minimalAuthCard.hidden = true;
    }
  }

  function renderGoogleButtons() {
    if (!window.google || !window.google.accounts || !window.google.accounts.id) return;

    if (els.gSigninElement) {
      els.gSigninElement.innerHTML = "";
      try {
        google.accounts.id.renderButton(els.gSigninElement, {
          type: "standard",
          theme: "filled_black",
          size: "large",
          text: "signin_with",
          shape: "rectangular",
          logo_alignment: "left",
          width: 280,
        });
      } catch (e) {
        console.warn("Could not render modal Google button", e);
      }
    }

    if (els.minimalGSigninElement) {
      els.minimalGSigninElement.innerHTML = "";
      try {
        google.accounts.id.renderButton(els.minimalGSigninElement, {
          type: "standard",
          theme: "filled_blue",
          size: "large",
          text: "signin_with",
          shape: "rectangular",
          logo_alignment: "left",
          width: 260,
        });
      } catch (e) {
        console.warn("Could not render inline Google button", e);
      }
    }
  }

  function setupGoogleIdentityServices() {
    if (!window.google || !window.google.accounts || !window.google.accounts.id) {
      setTimeout(setupGoogleIdentityServices, 250);
      return;
    }
    if (authState.gisInitialized || !authState.googleClientId) return;

    google.accounts.id.initialize({
      client_id: authState.googleClientId,
      callback: handleGoogleCredentialResponse,
      auto_select: false,
      cancel_on_tap_outside: true,
    });
    authState.gisInitialized = true;
    renderGoogleButtons();
  }

  async function handleGoogleCredentialResponse(googleResponse) {
    if (!googleResponse || !googleResponse.credential) return;

    try {
      const res = await fetch("/api/auth/google", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ credential: googleResponse.credential }),
      });
      const data = await parseResponse(res, "Sign in failed.");
      if (data.authenticated && data.user) {
        setAuthenticatedUser(data.user);
        hideMinimalAuthCard();
        closeLoginModal();

        // Refresh ladder rungs to unlock qualities
        if (state.formats.length > 0) {
          renderLadder(state.formats);
        }

        // Automatic resume of requested download
        if (authState.pendingDownload) {
          const pending = authState.pendingDownload;
          authState.pendingDownload = null;
          resumePendingDownload(pending);
        }
      }
    } catch (err) {
      showError(err.message);
    }
  }

  async function resumePendingDownload(pending) {
    if (!pending || !pending.url || !pending.formatId) return;

    // Find and select the corresponding rung
    const targetRung = Array.from(els.ladderList.querySelectorAll(".rung")).find(
      (r) => r.dataset.formatId === pending.formatId
    );
    if (targetRung) {
      selectRung(targetRung);
    }

    clearError();
    els.downloadBtn.disabled = true;
    els.statusPanel.hidden = false;
    els.downloadLink.hidden = true;
    setTally("active");
    els.statusText.textContent = "Starting download…";

    try {
      const job = await startDownload(pending.url, pending.formatId, pending.isAudioOnly);
      pollStatus(job.job_id);
    } catch (err) {
      setTally("failed");
      els.statusText.textContent = err.message;
      els.downloadBtn.disabled = false;
    }
  }

  async function initAuth() {
    try {
      const cfgRes = await fetch("/api/config");
      if (cfgRes.ok) {
        const cfg = await cfgRes.json();
        authState.googleClientId = cfg.google_client_id;
        authState.guestMaxHeight = cfg.guest_max_height || 720;
      }
    } catch (e) {
      console.warn("Could not load /api/config", e);
    }

    try {
      const meRes = await fetch("/api/auth/me");
      if (meRes.ok) {
        const meData = await meRes.json();
        if (meData.authenticated && meData.user) {
          setAuthenticatedUser(meData.user);
        } else {
          setGuestUser();
        }
      } else {
        setGuestUser();
      }
    } catch (e) {
      setGuestUser();
    }

    setupGoogleIdentityServices();
  }

  // ---------- ladder rendering ----------

  function isFormatLocked(fmt) {
    if (authState.authenticated) return false;
    if (fmt.locked !== undefined) return fmt.locked;
    return !!(fmt.height && fmt.height > authState.guestMaxHeight);
  }

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
      rung.dataset.audioOnly = fmt.format_id === "audio-only" ? "true" : "false";

      const locked = isFormatLocked(fmt);
      if (locked) {
        rung.classList.add("locked");
        rung.dataset.locked = "true";
      }

      const label = document.createElement("span");
      label.className = "rung-label";

      let textLabel = "Video";
      if (fmt.format_id === "audio-only") {
        textLabel = "Audio only (MP3)";
      } else if (fmt.note) {
        textLabel = fmt.note;
      } else if (!fmt.has_video) {
        textLabel = "Audio";
      }
      label.textContent = textLabel;

      const rightContainer = document.createElement("span");
      rightContainer.className = "rung-right";

      if (locked) {
        const lockBadge = document.createElement("span");
        lockBadge.className = "rung-lock-badge";
        lockBadge.innerHTML = "&#128274; Sign in to unlock";
        rightContainer.appendChild(lockBadge);
      }

      const spec = document.createElement("span");
      spec.className = "rung-spec mono";
      spec.textContent = formatSpec(fmt);
      rightContainer.appendChild(spec);

      rung.append(label, rightContainer);

      rung.addEventListener("click", () => {
        selectRung(rung);

        if (rung.dataset.locked === "true") {
          // Unauthenticated user clicked locked format (>720p):
          // 1. Record pending download
          authState.pendingDownload = {
            url: els.urlInput.value.trim(),
            formatId: fmt.format_id,
            isAudioOnly: fmt.format_id === "audio-only",
          };
          // 2. Open the minimalistic sign-in box directly under the ladder!
          showMinimalAuthCard(fmt, textLabel);
        } else {
          // Unlocked format (<= 720p or audio): hide minimal auth box
          hideMinimalAuthCard();
        }
      });

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
    state.selectedIsLocked = rung.dataset.locked === "true";
    els.downloadBtn.disabled = false;

    const labelSpan = els.downloadBtn.querySelector(".btn-label");
    if (labelSpan) {
      labelSpan.textContent = (state.selectedIsLocked && !authState.authenticated)
        ? "Sign in with Google to Download"
        : "Download";
    }
  }

  function handleRungKeydown(e, rung) {
    const rungs = Array.from(els.ladderList.querySelectorAll(".rung"));
    const idx = rungs.indexOf(rung);

    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      rung.click();
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = e.key === "ArrowDown" ? rungs[(idx + 1) % rungs.length] : rungs[(idx - 1 + rungs.length) % rungs.length];
      selectRung(next);
    }
  }

  // ---------- error explanation & guidance ----------

  function explainError(rawError, context = "download") {
    let msg = "";
    let tip = "";

    if (typeof rawError === "string") {
      msg = rawError;
    } else if (rawError && rawError.message) {
      msg = rawError.message;
      if (rawError.tip) tip = rawError.tip;
    } else {
      msg = "An unexpected error occurred.";
    }

    const lower = msg.toLowerCase();

    // 1. Rate Limiting (429)
    if (lower.includes("too many requests") || rawError?.status === 429 || lower.includes("rate limit") || lower.includes("slow down")) {
      return {
        message: msg.includes("wait") ? msg : "Too many requests in a short period.",
        tip: tip || "Please wait 5 seconds before making another request to protect the server."
      };
    }

    // 2. Quality Gate / Login Required (401)
    if (rawError?.code === "LOGIN_REQUIRED" || lower.includes("sign in with google") || rawError?.status === 401) {
      return {
        message: msg,
        tip: tip || "Sign in with your Google account to unlock 1080p, 1440p, and 4K downloads instantly."
      };
    }

    // 3. Format / Audio / Video stream missing
    if (lower.includes("requested format") || lower.includes("invalid format") || lower.includes("format not available")) {
      return {
        message: "This specific stream or resolution is no longer provided by the host.",
        tip: "Please select another resolution or the audio-only option from the format ladder above."
      };
    }

    // 4. Bot Check / Verification
    if (lower.includes("bot check") || lower.includes("sign in to confirm you're not a bot") || lower.includes("confirm you") || lower.includes("captcha")) {
      return {
        message: "YouTube bot verification triggered on server.",
        tip: "Datacenter IPs are blocked by YouTube. Install our free Tampermonkey Userscript (link below) to download directly on YouTube using your own residential IP!"
      };
    }

    // 5. File size cap exceeded
    if (lower.includes("larger than max-filesize") || lower.includes("size limit")) {
      return {
        message: msg,
        tip: "The selected format exceeds the file size cap. Please choose a lower resolution (e.g. 720p or 480p)."
      };
    }

    // 6. Network timeout / Gateway error (502 / 504)
    if (lower.includes("504") || lower.includes("gateway timeout") || lower.includes("timed out")) {
      return {
        message: "The connection to the video provider timed out.",
        tip: "Click Download again to retry, or choose another resolution."
      };
    }

    if (lower.includes("502") || lower.includes("bad gateway")) {
      return {
        message: "Temporary gateway issue reaching the video provider.",
        tip: "Wait a moment and try clicking Download again."
      };
    }

    // 7. Private / Copyright / Geo-blocked
    if (lower.includes("private video") || lower.includes("copyright") || lower.includes("geo-restricted") || lower.includes("blocked")) {
      return {
        message: "This video is restricted by the content creator or copyright holder.",
        tip: "Please verify that the video is public and accessible in your region."
      };
    }

    // 8. General fallback
    return {
      message: msg,
      tip: tip || (context === "download" ? "Try selecting a different resolution above, or retry in a few moments." : "Check that the URL is public and valid, then try again.")
    };
  }

  function showStatusError(err, context = "download") {
    const explained = explainError(err, context);
    setTally("failed");
    els.statusText.textContent = explained.message;
    if (els.statusTip && els.statusTipText) {
      els.statusTipText.textContent = explained.tip;
      els.statusTip.hidden = false;
    }
    els.downloadBtn.disabled = false;
  }

  function clearStatusTip() {
    if (els.statusTip) {
      els.statusTip.hidden = true;
      if (els.statusTipText) els.statusTipText.textContent = "";
    }
  }

  // ---------- API calls ----------

  async function parseResponse(res, fallbackMessage) {
    let data = null;
    const contentType = res.headers ? (res.headers.get("content-type") || "") : "";

    if (contentType.includes("application/json")) {
      try {
        data = await res.json();
      } catch (e) {
        // Fallback to text
      }
    }

    if (!data) {
      try {
        const text = await res.text();
        if (text && text.trim().length > 0 && text.length < 500) {
          data = { detail: text.trim() };
        }
      } catch (e) {
        // Ignore
      }
    }

    if (!res.ok) {
      if (res.status === 401 && (data?.error === "LOGIN_REQUIRED" || data?.detail === "LOGIN_REQUIRED")) {
        const err = new Error(data?.message || data?.detail || fallbackMessage);
        err.code = "LOGIN_REQUIRED";
        err.status = 401;
        err.requiredHeight = data?.requiredHeight;
        err.tip = data?.tip || "Sign in with Google to download videos above 720p.";
        throw err;
      }

      // Handle FastAPI 422 validation error array
      let detailMsg = "";
      if (Array.isArray(data?.detail)) {
        detailMsg = data.detail.map((d) => (d && d.msg ? d.msg : JSON.stringify(d))).join("; ");
      } else if (typeof data?.detail === "string") {
        detailMsg = data.detail;
      } else if (typeof data?.error === "string") {
        detailMsg = data.error;
      } else if (typeof data?.message === "string") {
        detailMsg = data.message;
      }

      if (!detailMsg) {
        if (res.status === 429) {
          detailMsg = "Too many requests — please wait a few seconds before trying again.";
        } else if (res.status === 504) {
          detailMsg = "The server or proxy timed out while fetching media. (HTTP 504)";
        } else if (res.status === 502) {
          detailMsg = "Bad gateway connecting to media provider. (HTTP 502)";
        } else {
          detailMsg = `${fallbackMessage} (HTTP ${res.status})`;
        }
      }

      const err = new Error(detailMsg);
      err.status = res.status;
      err.tip = data?.tip;
      throw err;
    }

    return data || {};
  }

  async function fetchInfo(url) {
    const res = await fetch("/api/media/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    return parseResponse(res, "Couldn't read that link.");
  }

  async function startDownload(url, formatId, audioOnly) {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        format_id: audioOnly ? null : formatId,
        format_has_audio: audioOnly ? false : !!state.formats.find((f) => f.format_id === formatId)?.has_audio,
        audio_only: audioOnly
      }),
    });
    return parseResponse(res, "Couldn't start the download.");
  }

  async function fetchStatus(jobId) {
    const res = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
    return parseResponse(res, "Lost track of that job.");
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

    let consecutiveErrors = 0;
    const MAX_RETRIES = 5;

    const tick = async () => {
      let data;
      try {
        data = await fetchStatus(jobId);
        consecutiveErrors = 0;
      } catch (err) {
        consecutiveErrors += 1;
        if (consecutiveErrors >= MAX_RETRIES) {
          showStatusError(err, "poll");
          return;
        }
        setTally("active");
        els.statusText.textContent = `Connection issue — retrying (${consecutiveErrors}/${MAX_RETRIES})…`;
        state.pollHandle = setTimeout(tick, 2000);
        return;
      }

      if (data.status === "queued") {
        setTally("active");
        els.statusText.textContent = "Queued…";
        state.pollHandle = setTimeout(tick, 1200);
      } else if (data.status === "downloading") {
        setTally("active");
        const pct = typeof data.progress === "number" ? ` ${data.progress.toFixed(0)}%` : "";
        const speed = data.speed ? ` · ${formatFileSize(data.speed)}/s` : "";
        const eta = data.eta ? ` · ${data.eta}s` : "";
        els.statusText.textContent = `Downloading…${pct}${speed}${eta}`;
        state.pollHandle = setTimeout(tick, 1200);
      } else if (data.status === "completed") {
        setTally("done");
        els.statusText.textContent = "Ready.";
        clearStatusTip();
        els.downloadLink.href = `/api/file/${encodeURIComponent(jobId)}`;
        els.downloadLink.hidden = false;
        els.downloadBtn.disabled = false;
      } else if (data.status === "failed") {
        showStatusError(new Error(data.error || "Download failed on server."), "download");
      }
    };

    tick();
  }

  // ---------- event wiring ----------

  if (els.loginTriggerBtn) {
    els.loginTriggerBtn.addEventListener("click", () => openLoginModal());
  }

  if (els.modalCloseBtn) {
    els.modalCloseBtn.addEventListener("click", () => closeLoginModal());
  }

  if (els.loginModal) {
    els.loginModal.addEventListener("click", (e) => {
      if (e.target === els.loginModal) closeLoginModal();
    });
  }

  if (els.logoutBtn) {
    els.logoutBtn.addEventListener("click", async () => {
      try {
        await fetch("/api/auth/logout", { method: "POST" });
        setGuestUser();
        hideMinimalAuthCard();
        // Re-render ladder to reflect locked high-resolution rungs
        if (state.formats.length > 0) {
          renderLadder(state.formats);
        }
      } catch (err) {
        console.error("Logout failed:", err);
      }
    });
  }

  els.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearError();
    clearStatusTip();
    hideMinimalAuthCard();

    const url = els.urlInput.value.trim();
    if (!url) return;

    els.mediaPanel.hidden = true;
    els.ladderPanel.hidden = true;
    els.statusPanel.hidden = true;
    els.downloadLink.hidden = true;
    stopPolling();

    setFetching(true);
    try {
      const info = await fetchInfo(url);

      els.thumb.onerror = () => {
        els.mediaPanel.classList.add("no-thumb");
        els.thumb.removeAttribute("src");
      };
      els.thumb.onload = () => {
        els.mediaPanel.classList.remove("no-thumb");
      };
      if (info.thumbnail) {
        els.thumb.src = info.thumbnail;
        els.thumb.alt = `Thumbnail for ${info.title}`;
      } else {
        els.thumb.onerror();
        els.thumb.alt = "";
      }
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
        note: "Audio only (MP3)",
        locked: false,
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
    clearStatusTip();

    // If selected format is locked (> 720p) and user is a guest:
    if (state.selectedIsLocked && !authState.authenticated) {
      authState.pendingDownload = {
        url: els.urlInput.value.trim(),
        formatId: state.selectedFormatId,
        isAudioOnly: state.selectedIsAudioOnly,
      };
      if (els.minimalAuthCard) {
        els.minimalAuthCard.hidden = false;
        renderGoogleButtons();
        els.minimalAuthCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
      openLoginModal(`Sign in with Google to download in full HD.`);
      return;
    }

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
      if (err.code === "LOGIN_REQUIRED") {
        setTally(null);
        els.statusPanel.hidden = true;
        els.downloadBtn.disabled = false;
        authState.pendingDownload = {
          url: els.urlInput.value.trim(),
          formatId: state.selectedFormatId,
          isAudioOnly: state.selectedIsAudioOnly,
        };
        if (els.minimalAuthCard) {
          els.minimalAuthCard.hidden = false;
          renderGoogleButtons();
        }
        openLoginModal(err.message);
        return;
      }
      showStatusError(err, "download");
    }
  });

  // Initialize authentication on page load
  initAuth();
})();
