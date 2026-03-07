/**
 * vision_observer.js — Stage 2
 *
 * Adds on top of Stage 1:
 *   - Proactive page announcement on navigation (calls /voice/announce)
 *   - Toggle ON/OFF state tracked (reads from chrome.storage.local)
 *   - Plays TTS audio from backend directly on navigation
 *   - Exposes window.__copilotToggle(enabled) for popup to call
 *   - Exposes window.__copilotSetBackend(url) so popup can change backend URL
 */

(function () {
  "use strict";

  if (window.__copilotVisionObserverLoaded) return;
  window.__copilotVisionObserverLoaded = true;

  // ── Config ────────────────────────────────────────────────────────────────
  const DEFAULT_BACKEND = "http://localhost:8000";
  // BACKEND and WS_URL are mutable so popup can update them at runtime via
  // window.__copilotSetBackend(url).
  let BACKEND       = DEFAULT_BACKEND;
  let WS_URL        = `${DEFAULT_BACKEND.replace(/^http/, "ws")}/ws/vision`;
  const POLL_INTERVAL = 800;
  const DEBOUNCE_MS   = 200;
  const RECONNECT_MS  = 3000;
  const MAX_FIELDS    = 15;
  const SESSION_ID    = `vision-${Date.now().toString(36)}`;
  let _lastInstruction  = "";    // last voice command — saved with page snapshot
  let _guidedFillActive = false; // is guided fill running? — saved with snapshot

  // ── State ─────────────────────────────────────────────────────────────────
  let ws             = null;
  let wsReady        = false;
  let pendingSend    = false;
  let debounceTimer  = null;
  let pollTimer      = null;
  let lastContextStr = "";
  let currentUrl     = location.href;
  let copilotEnabled = false;   // tracks toggle state

  // ── Load toggle state + backend URL from storage ──────────────────────────
  chrome.storage.local.get(["copilotEnabled", "lastAnnouncedUrl", "gopilotBackendUrl"], (result) => {
    // Apply persisted backend URL (allows non-default host/port without editing source)
    const stored = (result.gopilotBackendUrl || "").trim().replace(/\/$/, "");
    if (stored) {
      BACKEND = stored;
      WS_URL  = `${stored.replace(/^http/, "ws")}/ws/vision`;
    }
    copilotEnabled = result.copilotEnabled === true;
    if (copilotEnabled) {
      // Only re-announce if this is a genuinely new page (not a popup re-open)
      const prevUrl = result.lastAnnouncedUrl || "";
      if (prevUrl !== location.href) {
        setTimeout(() => {
          _lastAnnouncedUrl = location.href;
          chrome.storage.local.set({ lastAnnouncedUrl: location.href });
          announceCurrentPage(false);
        }, 1200);
      }
    }
    // Connect WebSocket after URL is resolved
    connectWS();
  });

  // Allow popup to update backend URL at runtime (applied to future fetch/WS calls).
  // The WebSocket reconnect loop will pick up WS_URL on its next retry.
  window.__copilotSetBackend = function(url) {
    if (!url) return;
    const clean = url.trim().replace(/\/$/, "");
    BACKEND = clean;
    WS_URL  = `${clean.replace(/^http/, "ws")}/ws/vision`;
    // Force WebSocket reconnect so it uses the new URL immediately
    if (ws) { try { ws.close(); } catch(_) {} }
    console.log("[GoPilot] Backend URL updated to:", clean);
  };

  // ── WebSocket ─────────────────────────────────────────────────────────────

  function connectWS() {
    try {
      ws = new WebSocket(WS_URL);
      ws.onopen  = () => { wsReady = true;  sendSnapshot("navigation"); };
      ws.onclose = () => { wsReady = false; setTimeout(connectWS, RECONNECT_MS); };
      ws.onerror = ()  => { wsReady = false; };
      ws.onmessage = (event) => {
        try { handleBackendMessage(JSON.parse(event.data)); } catch(e) {}
      };
    } catch(e) {
      setTimeout(connectWS, RECONNECT_MS);
    }
  }

  function sendWS(data) {
    if (!ws || !wsReady || ws.readyState !== WebSocket.OPEN) return false;
    try { ws.send(JSON.stringify(data)); return true; } catch(e) { return false; }
  }

  // ── Context extraction ────────────────────────────────────────────────────

  function extractContext() {
    if (typeof window.__copilotExtractContext === "function") {
      try { return window.__copilotExtractContext(); } catch(e) {}
    }
    return extractMinimal();
  }

  function extractMinimal() {
    const fields = [], seen = new Set();
    document.querySelectorAll(
      'input:not([type="hidden"]):not([type="file"]):not([type="submit"]),select,textarea'
    ).forEach((el, i) => {
      if (!isVisible(el) || fields.length >= MAX_FIELDS) return;
      const id = el.id || el.name || `f${i}`;
      if (seen.has(id)) return;
      seen.add(id);
      const label = getLabel(el) || el.placeholder || id;
      fields.push({
        field_id: id, label: label.slice(0,40),
        type: el.tagName.toLowerCase()==="select" ? "select" : (el.type||"text"),
        required: el.required || el.getAttribute("aria-required")==="true",
        readonly: el.readOnly || el.disabled, calculated: false,
        value: el.value ? String(el.value).slice(0,80) : null,
      });
      el.dataset.copilotFieldId = id;
    });
    const buttons = [];
    document.querySelectorAll('button,input[type="submit"]').forEach((el,i) => {
      if (!el.disabled && isVisible(el))
        buttons.push({ button_id: el.id||el.name||`btn_${i}`,
                       label: (el.textContent||el.value||"").trim().slice(0,30),
                       disabled: el.disabled, action: el.type||"click" });
    });
    return {
      app:      { name: document.title||"Unknown" },
      page:     { page_id: location.href, title: document.title||"", mode:"live" },
      sections: fields.length ? [{ section_id:"page", title:"Page Fields", fields }] : [],
      buttons,
    };
  }

  function getLabel(el) {
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.textContent.trim();
    }
    const parent = el.closest("label");
    if (parent) return parent.textContent.replace(el.value||"","").trim();
    return el.getAttribute("aria-label")||el.placeholder||el.name||"";
  }

  function isVisible(el) {
    if (!el.offsetParent && el.tagName !== "BODY") return false;
    const s = getComputedStyle(el);
    return s.display!=="none" && s.visibility!=="hidden" && s.opacity!=="0";
  }

  // ── Proactive page announcement ───────────────────────────────────────────

  async function announceCurrentPage(onToggle) {
    if (!copilotEnabled && !onToggle) return;
    const context = extractContext();
    try {
      const res = await fetch(`${BACKEND}/voice/${onToggle ? "toggle" : "announce"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled:        onToggle ? true : undefined,
          screen_context: context,
          session_id:     SESSION_ID,
          tab_session_id: SESSION_ID,   // tab-stable — used for cross-page memory
          url:            location.href,
        }),
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data.audio_file && !onToggle) {
        // Skip nav announcement audio if voice response just played
        const timeSinceVoice = Date.now() - (window.__gopilotLastVoiceAudioTime || 0);
        if (timeSinceVoice < 4000) {
          console.log("[GoPilot] Nav announce suppressed — voice audio recent");
        } else {
          const fname = data.audio_file.replace(/\\/g,"/").split("/").pop();
          playAudio(`${BACKEND}/voice/audio/${fname}`);
        }
      } else if (data.audio_file && onToggle) {
        // Toggle audio always plays
        const fname = data.audio_file.replace(/\\/g,"/").split("/").pop();
        playAudio(`${BACKEND}/voice/audio/${fname}`);
      }
    } catch(e) {
      console.warn("[GoPilot] Announce failed:", e.message);
    }
  }

  // ── Toggle ON/OFF (called by popup) ──────────────────────────────────────

  window.__copilotToggle = async function(enabled) {
    copilotEnabled = enabled;
    chrome.storage.local.set({ copilotEnabled: enabled });
    const context = extractContext();
    try {
      const res = await fetch(`${BACKEND}/voice/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled,
          screen_context: context,
          session_id:     SESSION_ID,
          url:            location.href,
        }),
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data.audio_file) {
        const fname = data.audio_file.replace(/\\/g,"/").split("/").pop();
        playAudio(`${BACKEND}/voice/audio/${fname}`);
      }
    } catch(e) {
      console.warn("[GoPilot] Toggle announce failed:", e.message);
    }
  };

  // ── Snapshot sender ───────────────────────────────────────────────────────

  function sendSnapshot(type = "dom_snapshot") {
    if (pendingSend && type === "dom_snapshot") return;
    pendingSend = true;
    const context    = extractContext();
    const contextStr = JSON.stringify(context);
    if (type === "dom_snapshot" && contextStr === lastContextStr) {
      pendingSend = false; return;
    }
    lastContextStr = contextStr;
    sendWS({ type, session_id: SESSION_ID, context, url: location.href });
    pendingSend = false;
  }

  // ── MutationObserver ──────────────────────────────────────────────────────

  const observer = new MutationObserver(() => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => sendSnapshot("dom_snapshot"), DEBOUNCE_MS);
  });
  observer.observe(document.body, {
    childList:true, subtree:true, attributes:true,
    attributeFilter:["value","disabled","readonly","aria-hidden"],
  });

  // ── Navigation detection ──────────────────────────────────────────────────

  const _origPush    = history.pushState.bind(history);
  const _origReplace = history.replaceState.bind(history);
  history.pushState    = (...a) => { _origPush(...a);    onNavigate(); };
  history.replaceState = (...a) => { _origReplace(...a); onNavigate(); };
  window.addEventListener("popstate",   onNavigate);
  window.addEventListener("hashchange", onNavigate);

  let _navAnnounceTimer = null;
  let _lastAnnouncedUrl = "";

  function onNavigate() {
    if (location.href !== currentUrl) {
      if (copilotEnabled) _savePageSnapshot(currentUrl);
      currentUrl = location.href;

      // Debounce: hashchange + popstate can both fire for same navigation
      clearTimeout(_navAnnounceTimer);
      _navAnnounceTimer = setTimeout(() => {
        _navAnnounceTimer = null;
        sendSnapshot("navigation");
        // Skip if we already announced this URL (prevent double-fire)
        if (copilotEnabled && currentUrl !== _lastAnnouncedUrl) {
          _lastAnnouncedUrl = currentUrl;
          announceCurrentPage(false);
        }
      }, 800);  // 800ms — enough for DOM and to debounce double events
    }
  }

  // Save a snapshot of the current page to backend page_memory
  async function _savePageSnapshot(urlBeforeNav) {
    try {
      const context = extractContext();
      // Only worth saving if there are fillable fields with values
      const hasWork = (context.sections || []).some(s =>
        (s.fields || []).some(f => !f.readonly && f.value && String(f.value).trim())
      );
      if (!hasWork) return;
      await fetch(`${BACKEND}/voice/page_memory/save`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tab_session_id: SESSION_ID,
          screen_context: context,
          url:            urlBeforeNav,
          last_instruction: _lastInstruction || "",
          was_in_guided_fill: _guidedFillActive || false,
        }),
      });
      console.log("[GoPilot Memory] Saved snapshot for:", urlBeforeNav.slice(-40));
    } catch (e) {
      // Silent fail — memory is enhancement, not critical path
    }
  }

  // ── Input listeners ───────────────────────────────────────────────────────

  document.addEventListener("input", (e) => {
    if (e.target.matches("input,select,textarea")) {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => sendSnapshot("dom_snapshot"), DEBOUNCE_MS*2);
    }
  }, true);
  document.addEventListener("change", (e) => {
    if (e.target.matches("select")) sendSnapshot("dom_snapshot");
  }, true);

  // ── Polling fallback ──────────────────────────────────────────────────────
  pollTimer = setInterval(() => sendSnapshot("dom_snapshot"), POLL_INTERVAL);

  // ── Public API ────────────────────────────────────────────────────────────

  window.__copilotSendVoiceCommand = function(text, workflow) {
    _lastInstruction = text;  // track for page memory
    sendWS({ type:"voice_command", session_id:SESSION_ID,
             context:extractContext(), url:location.href,
             text:text||"", workflow:workflow||"free" });
  };

  window.__copilotSendUserText = function(text, workflow) {
    sendWS({ type:"user_text", session_id:SESSION_ID,
             context:extractContext(), url:location.href,
             text:text||"", workflow:workflow||"free" });
  };

  window.__copilotConfirm = function(confirmed) {
    sendWS({ type:"confirmation", session_id:SESSION_ID,
             context:extractContext(), confirmed });
  };

  window.__copilotGetSessionId = function() { return SESSION_ID; };

  // ── Handle messages FROM backend ──────────────────────────────────────────

  // Streaming TTS chunk queue — plays sentences in order as they arrive
  const _ttsChunkQueue = {};   // { total, chunks: {idx: url}, played: Set }

  function handleBackendMessage(msg) {
    if (msg.type === "action" && msg.execute && msg.action) {
      executeAction(msg.action, msg.spoken);
    }
    // Single-shot TTS (non-streaming)
    if (msg.type === "tts_ready" && msg.audio_url) {
      playAudio(msg.audio_url.startsWith("http")
        ? msg.audio_url : `${BACKEND}${msg.audio_url}`);
    }
    // Streaming TTS — play each chunk as it arrives, in order
    if (msg.type === "tts_chunk" && msg.audio_url !== undefined) {
      const total = msg.total || 1;
      const idx   = msg.chunk_idx || 0;
      const key   = `${total}`;  // group by total (one stream at a time)

      if (!_ttsChunkQueue[key]) {
        _ttsChunkQueue[key] = { total, chunks: {}, played: 0 };
      }
      const q = _ttsChunkQueue[key];
      q.chunks[idx] = msg.audio_url.startsWith("http")
        ? msg.audio_url : `${BACKEND}${msg.audio_url}`;

      // Play chunks in order: if this chunk's index equals next expected
      (function _flushQueue() {
        while (q.chunks[q.played] !== undefined) {
          playAudio(q.chunks[q.played]);
          q.played++;
        }
        if (q.played >= q.total) delete _ttsChunkQueue[key];
      })();
    }
  }

  function executeAction(action, spoken) {
    if (typeof window.__copilotExecuteAction === "function") {
      try {
        window.__copilotExecuteAction(action);
        if (spoken && !window.__copilotTTSPending && "speechSynthesis" in window) {
          const u = new SpeechSynthesisUtterance(spoken);
          u.rate = 1.1; u.lang = "en-IN";
          window.speechSynthesis.speak(u);
        }
      } catch(e) { console.error("[GoPilot] Execute failed:", e); }
    }
  }

  // ── Audio context + playback ──────────────────────────────────────────────

  let _audioCtx = null;

  function _getAudioCtx() {
    if (!_audioCtx || _audioCtx.state === "closed")
      _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (_audioCtx.state === "suspended") _audioCtx.resume().catch(()=>{});
    return _audioCtx;
  }

  function _primeAudio() {
    try {
      const ctx = _getAudioCtx();
      const buf = ctx.createBuffer(1,1,22050);
      const src = ctx.createBufferSource();
      src.buffer = buf; src.connect(ctx.destination); src.start(0);
    } catch(_) {}
  }

  document.addEventListener("click",   _primeAudio, {capture:true});
  document.addEventListener("keydown",  _primeAudio, {capture:true});
  document.addEventListener("touchend", _primeAudio, {capture:true});

  function playAudio(url) {
    window.__copilotTTSPending = true;
    chrome.runtime.sendMessage({ type:"FETCH_AUDIO", url }, (response) => {
      if (chrome.runtime.lastError || !response?.ok) {
        window.__copilotTTSPending = false;
        return;
      }
      const binary = atob(response.base64);
      const bytes  = new Uint8Array(binary.length);
      for (let i=0; i<binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const ctx = _getAudioCtx();
      if (ctx.state === "suspended") { window.__copilotTTSPending = false; return; }
      ctx.decodeAudioData(bytes.buffer)
        .then(decoded => {
          const src = ctx.createBufferSource();
          src.buffer = decoded; src.connect(ctx.destination);
          src.onended = () => { window.__copilotTTSPending = false; };
          src.start(0);
        })
        .catch(() => { window.__copilotTTSPending = false; });
    });
  }

  // ── Start ─────────────────────────────────────────────────────────────────
  // connectWS() is called inside the chrome.storage.local.get callback above
  // so it uses the resolved (possibly user-configured) WS_URL.
  console.log("[GoPilot Vision] Observer v2 active — session:", SESSION_ID);

})();