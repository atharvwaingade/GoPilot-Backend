"use strict";

/**
 * Service worker for GoPilot Vision extension.
 *
 * Responsibilities:
 *  1. Install lifecycle logging
 *  2. FETCH_AUDIO   — fetches TTS WAV from localhost, returns base64 to content script.
 *  3. BACKEND_POST  — proxies JSON POST requests to localhost on behalf of content scripts.
 *     Content scripts run in the host page context and are subject to that page's CSP.
 *     Pages like inventory.umbrellasales.xyz may block fetch() to localhost entirely.
 *     The service worker runs in the extension context with no such restriction.
 *  4. COPILOT_LOG   — relay log messages from content scripts
 */

chrome.runtime.onInstalled.addListener(({ reason }) => {
  if (reason === "install") {
    console.log("[GoPilot] Extension installed.");
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.type) return false;

  // ── Audio proxy — fetch from localhost, return as base64 ─────────────────
  if (message.type === "FETCH_AUDIO") {
    const url = message.url;
    if (!url) {
      sendResponse({ ok: false, error: "No URL provided" });
      return false;
    }

    fetch(url)
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.arrayBuffer();
      })
      .then(buffer => {
        const bytes  = new Uint8Array(buffer);
        let binary   = "";
        const chunk  = 8192;
        for (let i = 0; i < bytes.length; i += chunk) {
          binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
        }
        sendResponse({ ok: true, base64: btoa(binary) });
      })
      .catch(err => {
        console.warn("[GoPilot] FETCH_AUDIO failed:", url, err.message);
        sendResponse({ ok: false, error: err.message });
      });

    return true; // keep message channel open for async sendResponse
  }

  // ── Backend POST proxy — bypasses host-page CSP for localhost POST calls ──
  // Usage from content script:
  //   chrome.runtime.sendMessage({ type:"BACKEND_POST", url:"http://localhost:8000/...", body:{...} })
  //   → { ok: true, data: {...} }  |  { ok: false, error: "..." }
  if (message.type === "BACKEND_POST") {
    const { url, body } = message;
    if (!url) { sendResponse({ ok: false, error: "No URL provided" }); return false; }

    fetch(url, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body || {}),
    })
      .then(res => res.json().then(data => ({ ok: res.ok, data })))
      .then(result => sendResponse(result))
      .catch(err => {
        console.warn("[GoPilot] BACKEND_POST failed:", url, err.message);
        sendResponse({ ok: false, error: err.message });
      });

    return true; // keep message channel open for async sendResponse
  }

  // ── Log relay ─────────────────────────────────────────────────────────────
  if (message.type === "COPILOT_LOG") {
    console.log(`[GoPilot] ${message.payload}`);
    sendResponse({ ok: true });
    return false;
  }

  return false;
});