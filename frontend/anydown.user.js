// ==UserScript==
// @name         AnyDown Assistant — Fast Video Downloader
// @namespace    https://anydown-com.onrender.com/
// @version      1.2.0
// @description  Adds a 1-click Download button directly below YouTube videos for instant MP4 and MP3 downloads.
// @author       AnyDown
// @match        https://www.youtube.com/*
// @match        https://m.youtube.com/*
// @grant        none
// @run-at       document-end
// ==/UserScript==

(() => {
  "use strict";

  const ANYDOWN_HOST = "https://anydown-com.onrender.com";
  let activeMenu = null;

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

  function getPlayerData() {
    try {
      // 1. Check window.ytInitialPlayerResponse
      if (window.ytInitialPlayerResponse && window.ytInitialPlayerResponse.streamingData) {
        return window.ytInitialPlayerResponse;
      }
      // 2. Check movie_player element
      const player = document.getElementById("movie_player");
      if (player && typeof player.getPlayerResponse === "function") {
        const resp = player.getPlayerResponse();
        if (resp && resp.streamingData) return resp;
      }
      // 3. Check ytplayer config args
      if (window.ytplayer && window.ytplayer.config && window.ytplayer.config.args) {
        const raw = window.ytplayer.config.args.player_response;
        if (raw) {
          const parsed = JSON.parse(raw);
          if (parsed && parsed.streamingData) return parsed;
        }
      }
    } catch (e) {
      console.warn("[AnyDown] Could not read player data:", e);
    }
    return null;
  }

  function extractFormats(playerResponse) {
    if (!playerResponse || !playerResponse.streamingData) return [];
    const { formats = [], adaptiveFormats = [] } = playerResponse.streamingData;
    const title = (playerResponse.videoDetails && playerResponse.videoDetails.title) || document.title.replace(" - YouTube", "").trim() || "video";

    const results = [];

    // 1. Combined Progressive streams (Video + Audio ready to play, usually 720p / 360p)
    formats.forEach((f) => {
      if (f.url) {
        results.push({
          label: f.qualityLabel || `${f.height}p`,
          category: "combined",
          ext: "mp4",
          note: "Video + Audio (Ready to play)",
          size: formatBytes(f.contentLength),
          url: f.url,
          title: `${title} - ${f.qualityLabel || f.height + "p"}.mp4`,
        });
      }
    });

    // 2. High-res adaptive video streams (1080p, 1440p, 4K)
    adaptiveFormats.forEach((f) => {
      const mime = f.mimeType || "";
      if (mime.startsWith("video/mp4") && f.url) {
        results.push({
          label: f.qualityLabel || `${f.height}p`,
          category: "video_only",
          ext: "mp4",
          note: "High-Bitrate Video track",
          size: formatBytes(f.contentLength),
          url: f.url,
          title: `${title} - ${f.qualityLabel || f.height + "p"} (video).mp4`,
        });
      }
    });

    // 3. Audio streams (M4A / WebM)
    adaptiveFormats.forEach((f) => {
      const mime = f.mimeType || "";
      if (mime.startsWith("audio/mp4") && f.url) {
        const kbps = Math.round((f.bitrate || 128000) / 1000);
        results.push({
          label: `Audio M4A (${kbps}k)`,
          category: "audio",
          ext: "m4a",
          note: "High Quality Audio",
          size: formatBytes(f.contentLength),
          url: f.url,
          title: `${title} (audio).m4a`,
        });
      }
    });

    return results;
  }

  function triggerDownload(url, filename) {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "video.mp4";
    a.target = "_blank";
    a.rel = "noreferrer noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  function closeMenu() {
    if (activeMenu) {
      activeMenu.remove();
      activeMenu = null;
    }
  }

  function showDownloadMenu(btn) {
    closeMenu();

    const data = getPlayerData();
    if (!data) {
      alert("AnyDown: Could not detect video streams yet. Please wait a second for the video to load, or play the video and try again.");
      return;
    }

    const items = extractFormats(data);
    if (!items.length) {
      alert("AnyDown: No direct streams found on this video. It may be DRM protected, age-restricted, or currently loading.");
      return;
    }

    const currentUrl = window.location.href;

    const menu = document.createElement("div");
    menu.id = "anydown-dropdown-menu";
    menu.style.cssText = `
      position: absolute;
      z-index: 999999;
      background: #141716;
      border: 1px solid #2d3830;
      border-radius: 8px;
      padding: 10px;
      width: 290px;
      box-shadow: 0 12px 32px rgba(0,0,0,0.8);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #e5ede7;
      animation: anydown-fadein 0.15s ease-out;
    `;

    let html = `
      <div style="display:flex;align-items:center;justify-content:space-between;padding-bottom:8px;border-bottom:1px solid #232d26;margin-bottom:8px;">
        <span style="font-weight:700;font-size:12px;letter-spacing:0.04em;color:#00dfa2;">ANYDOWN ASSISTANT</span>
        <span style="font-size:10px;color:#7d8c82;font-family:monospace;">Direct Downloader</span>
      </div>
      <div style="font-size:11px;color:#94a399;margin-bottom:6px;font-weight:600;">Combined Formats (Video + Audio):</div>
    `;

    const combined = items.filter((i) => i.category === "combined");
    if (combined.length) {
      combined.forEach((item, idx) => {
        html += `
          <button class="anydown-item-btn" data-idx="${idx}" style="
            display:flex;align-items:center;justify-content:space-between;width:100%;
            background:#1b211d;border:1px solid #232d26;border-radius:5px;
            padding:7px 10px;margin-bottom:5px;color:#e5ede7;font-size:12px;
            cursor:pointer;text-align:left;transition:all 0.1s ease;
          ">
            <span style="font-weight:600;color:#00dfa2;">${item.label}</span>
            <span style="font-size:11px;color:#7d8c82;font-family:monospace;">${item.size || item.ext}</span>
          </button>
        `;
      });
    } else {
      html += `<div style="font-size:11px;color:#7d8c82;padding:4px 0;">Only separate high-res streams available below.</div>`;
    }

    const audioItems = items.filter((i) => i.category === "audio");
    if (audioItems.length) {
      html += `<div style="font-size:11px;color:#94a399;margin:8px 0 6px;font-weight:600;">Audio Only:</div>`;
      audioItems.forEach((item, idx) => {
        const itemIdx = items.indexOf(item);
        html += `
          <button class="anydown-item-btn" data-idx="${itemIdx}" style="
            display:flex;align-items:center;justify-content:space-between;width:100%;
            background:#1b211d;border:1px solid #232d26;border-radius:5px;
            padding:6px 10px;margin-bottom:5px;color:#e5ede7;font-size:12px;
            cursor:pointer;text-align:left;transition:all 0.1s ease;
          ">
            <span style="font-weight:500;">🎵 ${item.label}</span>
            <span style="font-size:11px;color:#7d8c82;font-family:monospace;">${item.size || "m4a"}</span>
          </button>
        `;
      });
    }

    const highRes = items.filter((i) => i.category === "video_only");
    if (highRes.length) {
      html += `<div style="font-size:11px;color:#94a399;margin:8px 0 6px;font-weight:600;">High-Res Tracks (1080p+):</div>`;
      highRes.slice(0, 3).forEach((item) => {
        const itemIdx = items.indexOf(item);
        html += `
          <button class="anydown-item-btn" data-idx="${itemIdx}" style="
            display:flex;align-items:center;justify-content:space-between;width:100%;
            background:#1b211d;border:1px solid #232d26;border-radius:5px;
            padding:6px 10px;margin-bottom:5px;color:#e5ede7;font-size:12px;
            cursor:pointer;text-align:left;transition:all 0.1s ease;
          ">
            <span style="font-weight:500;">🎬 ${item.label} (Video)</span>
            <span style="font-size:11px;color:#7d8c82;font-family:monospace;">${item.size || ""}</span>
          </button>
        `;
      });
    }

    html += `
      <div style="margin-top:10px;padding-top:8px;border-top:1px solid #232d26;text-align:center;">
        <a href="${ANYDOWN_HOST}/?url=${encodeURIComponent(currentUrl)}" target="_blank" style="
          display:block;font-size:11px;color:#00dfa2;text-decoration:none;font-weight:600;padding:4px 0;
        ">
          Open in AnyDown Web Studio &rarr;
        </a>
      </div>
    `;

    menu.innerHTML = html;

    // Attach click listeners to format buttons
    menu.querySelectorAll(".anydown-item-btn").forEach((itemBtn) => {
      itemBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const idx = Number(itemBtn.dataset.idx);
        const targetItem = items[idx];
        if (targetItem && targetItem.url) {
          triggerDownload(targetItem.url, targetItem.title);
          closeMenu();
        }
      });
      itemBtn.addEventListener("mouseenter", () => {
        itemBtn.style.background = "#232d26";
        itemBtn.style.borderColor = "#00dfa2";
      });
      itemBtn.addEventListener("mouseleave", () => {
        itemBtn.style.background = "#1b211d";
        itemBtn.style.borderColor = "#232d26";
      });
    });

    const rect = btn.getBoundingClientRect();
    menu.style.top = `${rect.bottom + window.scrollY + 6}px`;
    menu.style.left = `${Math.max(10, rect.left + window.scrollX - 100)}px`;

    document.body.appendChild(menu);
    activeMenu = menu;
  }

  function injectButton() {
    if (document.getElementById("anydown-injected-btn")) return;

    // Standard Desktop YouTube action bar
    const actions = document.querySelector(
      "#top-row.ytd-watch-metadata #actions #top-level-buttons-computed"
    );
    if (!actions) return;

    const btn = document.createElement("button");
    btn.id = "anydown-injected-btn";
    btn.setAttribute("type", "button");
    btn.innerHTML = `
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" style="margin-right:6px;">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
      </svg>
      <span>Download</span>
    `;

    btn.style.cssText = `
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 36px;
      padding: 0 16px;
      border-radius: 18px;
      background: #00dfa2;
      color: #0c0f0d;
      font-weight: 700;
      font-size: 13px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      border: none;
      cursor: pointer;
      margin-right: 8px;
      box-shadow: 0 2px 8px rgba(0, 223, 162, 0.3);
      transition: transform 0.15s ease, background 0.15s ease;
    `;

    btn.addEventListener("mouseenter", () => {
      btn.style.background = "#05f5b4";
      btn.style.transform = "translateY(-1px)";
    });
    btn.addEventListener("mouseleave", () => {
      btn.style.background = "#00dfa2";
      btn.style.transform = "translateY(0)";
    });

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      showDownloadMenu(btn);
    });

    actions.prepend(btn);
  }

  // Global close on click outside
  document.addEventListener("click", (e) => {
    if (activeMenu && !activeMenu.contains(e.target)) {
      closeMenu();
    }
  });

  // Handle YouTube Single Page App (SPA) navigation
  window.addEventListener("yt-navigate-finish", () => {
    closeMenu();
    setTimeout(injectButton, 500);
  });

  // Backup interval in case element loads asynchronously
  setInterval(injectButton, 1200);

  // Inject animation styles
  const style = document.createElement("style");
  style.textContent = `
    @keyframes anydown-fadein {
      from { opacity: 0; transform: translateY(-4px); }
      to { opacity: 1; transform: translateY(0); }
    }
  `;
  document.head.appendChild(style);
})();
