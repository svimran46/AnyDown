// ==UserScript==
// @name         AnyDown Assistant — Video Downloader
// @namespace    https://anydown-com.onrender.com/
// @version      2.0.0
// @description  Fetches real format data from your own AnyDown server and downloads MP4/MP3 straight to your device. No YouTube page scraping.
// @author       AnyDown
// @match        https://www.youtube.com/*
// @match        https://m.youtube.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_download
// @grant        GM_addStyle
// @connect      anydown-com.onrender.com
// @connect      localhost
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

// All media data comes from the AnyDown API, not from the YouTube page. That
// means it works for every format the server can actually serve (including
// 1080p/1440p/4K that the page hides) and stays correct when YouTube changes
// its player internals.
//
// Requests use GM_xmlhttpRequest rather than fetch(): a userscript running on
// youtube.com calling a different origin would be blocked by CORS, and
// GM_xmlhttpRequest is exempt. Downloads use GM_download so the browser saves
// the file with the server's filename instead of navigating to it.

(() => {
  "use strict";

  const ANYDOWN_HOST = "https://anydown-com.onrender.com";
  const API = `${ANYDOWN_HOST}/api`;
  const POLL_MS = 1000;
  const MAX_POLLS = 1800;

  let overlay = null;
  let busy = false;

  // ---------- helpers ----------

  function api(method, path, body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url: API + path,
        headers: body ? { "Content-Type": "application/json" } : {},
        data: body ? JSON.stringify(body) : undefined,
        timeout: 600000,
        onload: (res) => {
          let parsed = null;
          try {
            parsed = JSON.parse(res.responseText);
          } catch (e) {
            /* non-JSON body */
          }
          if (res.status >= 200 && res.status < 300) {
            resolve(parsed);
            return;
          }
          const detail =
            (parsed && (parsed.message || parsed.detail)) ||
            `Server returned ${res.status}.`;
          reject(new Error(String(detail)));
        },
        onerror: () =>
          reject(new Error(`Could not reach ${ANYDOWN_HOST}. Is the server up?`)),
        ontimeout: () => reject(new Error("Request timed out.")),
      });
    });
  }

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    let n = Number(bytes);
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i += 1;
    }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function formatEta(seconds) {
    if (!seconds || seconds <= 0) return "";
    if (seconds < 60) return `${Math.ceil(seconds)}s left`;
    const m = Math.floor(seconds / 60);
    return `${m}m left`;
  }

  function isAudio(fmt) {
    return fmt.format_id === "audio-only" || !fmt.has_video;
  }

  function currentVideoUrl() {
    const url = new URL(window.location.href);
    if (url.pathname !== "/watch") return null;
    const v = url.searchParams.get("v");
    return v ? `https://www.youtube.com/watch?v=${v}` : null;
  }

  function el(tag, style, html) {
    const node = document.createElement(tag);
    if (style) node.style.cssText = style;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }

  // ---------- overlay UI ----------

  function closeOverlay() {
    if (overlay) {
      overlay.remove();
      overlay = null;
    }
    document.removeEventListener("keydown", onKeydown);
  }

  function onKeydown(e) {
    if (e.key === "Escape") closeOverlay();
  }

  function openOverlay() {
    if (overlay) return;
    overlay = el("div", `
      position: fixed; inset: 0; z-index: 2147483647;
      background: rgba(8,10,9,0.86); backdrop-filter: blur(3px);
      display: flex; align-items: center; justify-content: center;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #e5ede7;
    `);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeOverlay();
    });
    document.addEventListener("keydown", onKeydown);
    document.body.appendChild(overlay);
  }

  function setBody(html) {
    const box = overlay.querySelector(".anydown-box");
    if (box) box.innerHTML = html;
  }

  function renderShell() {
    openOverlay();
    overlay.innerHTML = "";
    const box = el("div", `
      background: #141716; border: 1px solid #2d3830; border-radius: 12px;
      width: 560px; max-width: 94vw; max-height: 86vh; overflow-y: auto;
      padding: 18px 20px; box-shadow: 0 20px 60px rgba(0,0,0,0.7);
      animation: anydown-fadein 0.16s ease-out;
    `, "");
    box.className = "anydown-box";
    box.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;
                  padding-bottom:10px;border-bottom:1px solid #232d26;margin-bottom:14px;">
        <span style="font-weight:800;font-size:13px;letter-spacing:0.05em;color:#00dfa2;">
          ANYDOWN ASSISTANT
        </span>
        <button class="anydown-close" style="background:transparent;border:none;color:#7d8c82;
                font-size:20px;cursor:pointer;line-height:1;padding:0 4px;">&times;</button>
      </div>
      <div class="anydown-body" style="font-size:13px;color:#94a399;">Loading&hellip;</div>
    `;
    box.querySelector(".anydown-close").addEventListener("click", closeOverlay);
    overlay.appendChild(box);
  }

  // ---------- render states ----------

  function renderLoading() {
    setBody(`
      <div class="anydown-body" style="font-size:13px;color:#94a399;padding:26px 0;text-align:center;">
        Reading available formats from the AnyDown server&hellip;
      </div>
    `);
  }

  function renderError(message) {
    setBody(`
      <div class="anydown-body">
        <div style="color:#ff7b72;font-size:13px;margin-bottom:12px;">${escapeHtml(message)}</div>
        <button class="anydown-retry" style="background:#00dfa2;color:#0c0f0d;border:none;
          border-radius:6px;padding:8px 16px;font-weight:700;cursor:pointer;">Retry</button>
      </div>
    `);
    const retry = overlay.querySelector(".anydown-retry");
    if (retry) retry.addEventListener("click", () => start(currentVideoUrl()));
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function renderFormats(info, videoUrl) {
    const formats = info.formats || [];
    const audio = [
      { format_id: "audio-only", has_video: false, note: "Audio only (MP3)", filesize: null },
      ...formats.filter(isAudio),
    ];
    const video = formats.filter((f) => !isAudio(f));

    // De-duplicate by height, best first.
    const seen = new Set();
    const videoRows = video
      .filter((f) => {
        const key = f.height || f.format_id;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .sort((a, b) => (b.height || 0) - (a.height || 0));

    const row = (item, label, meta, idx) => `
      <button class="anydown-fmt" data-idx="${idx}" style="
        display:flex;align-items:center;justify-content:space-between;width:100%;
        background:#1b211d;border:1px solid #232d26;border-radius:6px;
        padding:9px 12px;margin-bottom:6px;color:#e5ede7;font-size:13px;
        cursor:pointer;text-align:left;transition:all 0.1s ease;">
        <span style="font-weight:600;">${label}</span>
        <span style="font-size:11px;color:#7d8c82;font-family:monospace;">${meta}</span>
      </button>`;

    let html = `
      <div class="anydown-body">
        <div style="margin-bottom:14px;">
          ${info.thumbnail
            ? `<img src="${escapeHtml(info.thumbnail)}" alt=""
                 style="width:100%;max-height:170px;object-fit:contain;border-radius:6px;
                        background:#0c0f0d;margin-bottom:10px;">`
            : ""}
          <div style="font-weight:700;font-size:14px;line-height:1.3;">
            ${escapeHtml(info.title || "Untitled")}
          </div>
          <div style="font-size:11px;color:#7d8c82;margin-top:3px;font-family:monospace;">
            ${escapeHtml(info.uploader || "")}
            ${info.duration ? " &middot; " + escapeHtml(fmtDuration(info.duration)) : ""}
          </div>
        </div>
    `;

    if (videoRows.length) {
      html += `<div style="font-size:11px;color:#94a399;margin-bottom:6px;font-weight:700;
                        text-transform:uppercase;letter-spacing:0.05em;">Video</div>`;
      const all = [...videoRows, ...audio];
      videoRows.forEach((f) => {
        html += row(
          f,
          escapeHtml(f.note || `${f.height}p`),
          `${f.height ? f.height + "p &middot; " : ""}${escapeHtml(formatBytes(f.filesize) || f.ext || "")}`,
          all.indexOf(f),
        );
      });
      html += `<div style="font-size:11px;color:#94a399;margin:10px 0 6px;font-weight:700;
                        text-transform:uppercase;letter-spacing:0.05em;">Audio</div>`;
      audio.forEach((f, i) => {
        html += row(
          f,
          `${isAudio(f) ? "&#9835; " : ""}${escapeHtml(f.note || "Audio (MP3)")}`,
          escapeHtml(formatBytes(f.filesize) || "mp3"),
          videoRows.length + i,
        );
      });
      setBody(html.replace(/__ALL__/g, ""));
      bindRows(all, videoUrl);
      return;
    }

    html += `<div style="font-size:12px;color:#7d8c82;">No downloadable formats found.</div>`;
    setBody(html);
  }

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
  }

  function bindRows(all, videoUrl) {
    overlay.querySelectorAll(".anydown-fmt").forEach((btn) => {
      btn.addEventListener("mouseenter", () => {
        btn.style.background = "#232d26";
        btn.style.borderColor = "#00dfa2";
      });
      btn.addEventListener("mouseleave", () => {
        btn.style.background = "#1b211d";
        btn.style.borderColor = "#232d26";
      });
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const item = all[Number(btn.dataset.idx)];
        if (item && !busy) download(item, videoUrl);
      });
    });
  }

  // ---------- download flow ----------

  function renderProgress(jobId, pct, speed, eta, label) {
    const pctText = pct == null ? "" : ` ${Math.round(pct)}%`;
    setBody(`
      <div class="anydown-body">
        <div style="font-size:13px;margin-bottom:12px;">${escapeHtml(label)}</div>
        <div style="height:8px;background:#1b211d;border-radius:4px;overflow:hidden;">
          <div style="height:100%;width:${pct == null ? 8 : Math.max(2, Math.min(100, pct))}%;
                      background:#00dfa2;border-radius:4px;transition:width 0.3s ease;"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:8px;
                    font-size:11px;color:#7d8c82;font-family:monospace;">
          <span>${speed ? formatBytes(speed) + "/s" : "starting"}${pctText}</span>
          <span>${escapeHtml(formatEta(eta) || "")}</span>
        </div>
        <div style="margin-top:14px;text-align:center;">
          <button class="anydown-cancel" style="background:transparent;border:1px solid #2d3830;
            color:#94a399;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px;">
            Close
          </button>
        </div>
      </div>
    `);
    const cancel = overlay.querySelector(".anydown-cancel");
    if (cancel) cancel.addEventListener("click", () => { busy = false; closeOverlay(); });
  }

  async function download(item, videoUrl) {
    busy = true;
    renderProgress(null, null, null, null, "Requesting download from server\u2026");

    let job;
    try {
      job = await api("POST", "/download", {
        url: videoUrl,
        format_id: item.format_id,
        audio_only: isAudio(item),
      });
    } catch (err) {
      busy = false;
      renderError(err.message);
      return;
    }

    const jobId = job.job_id;
    const startedAt = Date.now();
    let done = false;

    for (let i = 0; i < MAX_POLLS; i += 1) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      if (!busy) return;

      let status;
      try {
        status = await api("GET", `/status/${jobId}`);
      } catch (err) {
        busy = false;
        renderError(err.message);
        return;
      }

      if (status.status === "completed") {
        done = true;
        renderProgress(100, null, null, null, "Saved. Check your downloads.");
        const filename = status.filename || "video";
        saveFile(jobId, filename);
        setTimeout(() => { if (busy) { busy = false; closeOverlay(); } }, 2500);
        return;
      }

      if (status.status === "failed") {
        busy = false;
        renderError(status.error || "The server could not complete this download.");
        return;
      }

      if ((Date.now() - startedAt) % 15000 < POLL_MS) {
        renderProgress(
          status.progress, status.speed, status.eta,
          `Downloading ${escapeHtml(item.note || item.format_id)}\u2026`,
        );
      }
    }

    if (!done) {
      busy = false;
      renderError("Download timed out before it finished.");
    }
  }

  function saveFile(jobId, filename) {
    const url = `${API}/file/${jobId}`;
    try {
      if (typeof GM_download === "function") {
        GM_download({ url, name: filename, onerror: () => fallbackSave(url) });
        return;
      }
    } catch (e) {
      /* fall through */
    }
    fallbackSave(url);
  }

  function fallbackSave(url) {
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    a.rel = "noreferrer";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ---------- entry point ----------

  async function start(videoUrl) {
    if (busy) return;
    if (!videoUrl) {
      renderShell();
      renderError("This page is not a YouTube watch page.");
      return;
    }
    busy = true;
    renderShell();
    renderLoading();
    try {
      const info = await api("POST", "/media/inspect", { url: videoUrl });
      busy = false;
      renderFormats(info, videoUrl);
    } catch (err) {
      busy = false;
      renderError(err.message);
    }
  }

  function injectButton() {
    if (document.getElementById("anydown-injected-btn")) return;
    const actions = document.querySelector(
      "#top-row.ytd-watch-metadata #actions #top-level-buttons-computed",
    );
    if (!actions) return;

    const btn = el("button", `
      display: inline-flex; align-items: center; justify-content: center;
      height: 36px; padding: 0 16px; border-radius: 18px; background: #00dfa2;
      color: #0c0f0d; font-weight: 700; font-size: 13px; border: none;
      cursor: pointer; margin-right: 8px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      box-shadow: 0 2px 8px rgba(0,223,162,0.3);
    `);
    btn.id = "anydown-injected-btn";
    btn.setAttribute("type", "button");
    btn.innerHTML = `
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
           stroke-width="2.5" style="margin-right:6px;">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
      </svg>
      <span>Download</span>`;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      start(currentVideoUrl());
    });
    actions.prepend(btn);
  }

  GM_addStyle(`
    @keyframes anydown-fadein {
      from { opacity: 0; transform: translateY(-4px); }
      to { opacity: 1; transform: translateY(0); }
    }
  `);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeOverlay();
  });

  window.addEventListener("yt-navigate-finish", () => setTimeout(injectButton, 500));
  setInterval(injectButton, 1200);
})();
