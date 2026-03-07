"use strict";

// ── Tab-stable session ID ─────────────────────────────────────────────────
let _tabSessionId = null;

async function getTabSessionId() {
  if (_tabSessionId) return _tabSessionId;
  try {
    const tab  = await getActiveTab();
    const key  = `tab_session_${tab.id}`;
    const data = await chrome.storage.local.get(key);
    if (data[key]) {
      _tabSessionId = data[key];
    } else {
      _tabSessionId = `tab-${tab.id}-${Math.random().toString(36).slice(2, 8)}`;
      await chrome.storage.local.set({ [key]: _tabSessionId });
    }
  } catch (e) {
    if (!_tabSessionId) _tabSessionId = `tab-${Date.now()}-${Math.random().toString(36).slice(2,6)}`;
  }
  return _tabSessionId;
}

const DEFAULT_BACKEND = "http://localhost:8000";
// BACKEND is resolved from chrome.storage.local at startup (see _initBackend).
// All code uses the `BACKEND` variable; it is reassigned before any user action.
let BACKEND     = DEFAULT_BACKEND;
let BACKEND_URL = DEFAULT_BACKEND;
let lastScreenContext = {};

// Read persisted backend URL from storage, update module variables and the
// settings input, then inject updated config into the active tab's
// vision_observer if it is already running.
async function _initBackend() {
  const data = await chrome.storage.local.get("gopilotBackendUrl");
  const stored = (data.gopilotBackendUrl || "").trim().replace(/\/$/, "");
  if (stored) {
    BACKEND     = stored;
    BACKEND_URL = stored;
  }
  const inp = document.getElementById("backend-url-input");
  if (inp) inp.value = BACKEND;
}
_initBackend();

// ── DOM refs ──────────────────────────────────────────────────────────────
const micBtn        = document.getElementById("mic-btn");
const micIcon       = document.getElementById("mic-icon");
const stopIcon      = document.getElementById("stop-icon");
const spinIcon      = document.getElementById("spin-icon");
const micLabel      = document.getElementById("mic-label");
const micRingPulse  = document.getElementById("mic-ring-pulse");
const voiceWaveform = document.getElementById("voice-waveform");
const transcriptRow = document.getElementById("transcript-row");
const transcriptText= document.getElementById("transcript-text");
const responseRow   = document.getElementById("response-row");
const responseText  = document.getElementById("response-text");
const actionChip    = document.getElementById("action-chip");
const replayBtn     = document.getElementById("replay-btn");
const warningRow    = document.getElementById("warning-row");
const warningText   = document.getElementById("warning-text");
const errorRow      = document.getElementById("error-row");
const errorText     = document.getElementById("error-text");
const copilotToggle = document.getElementById("copilot-toggle");
const toggleLabel   = document.getElementById("toggle-label");
// Voice workflow — not in new HTML, use "free" always
const voiceWorkflowSelect = { value: "free" };

// ── Voice state ────────────────────────────────────────────────────────────
let mediaRecorder = null;
let audioChunks   = [];
let voiceStream   = null;
let isRecording   = false;
let lastAudioUrl  = null;

// ── Global audio lock — prevents overlapping TTS from nav events + voice ──
// Only ONE audio source plays at a time. New audio cancels nothing — it queues.
// Navigation announcements are skipped if voice just played within 3 seconds.
let _lastAudioEndTime = 0;
let _audioPlaying     = false;
const _audioQueue     = [];

async function playAudio(url, priority = "normal") {
  // Nav announcements skip if voice response audio is recent (within 4s)
  if (priority === "nav" && (Date.now() - _lastAudioEndTime < 4000 || _audioPlaying)) {
    console.log("[GoPilot Audio] Skipping nav announcement — voice audio recent");
    return;
  }

  _audioQueue.push({ url, priority });
  if (_audioQueue.length > 1) return;  // already draining

  while (_audioQueue.length > 0) {
    const { url: u } = _audioQueue[0];
    _audioPlaying = true;
    try {
      const res = await fetch(u);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buffer  = await res.arrayBuffer();
      const ctx     = new AudioContext();
      const decoded = await ctx.decodeAudioData(buffer);
      const src     = ctx.createBufferSource();
      src.buffer    = decoded;
      src.connect(ctx.destination);
      await new Promise(resolve => {
        src.onended = () => { ctx.close(); resolve(); };
        src.start(0);
      });
    } catch (err) {
      console.warn("[GoPilot Audio] Playback failed:", err.message);
    }
    _lastAudioEndTime = Date.now();
    _audioPlaying     = false;
    _audioQueue.shift();
  }
}

// ── UI state ──────────────────────────────────────────────────────────────
function setVoiceState(state) {
  // states: idle | recording | processing | done | error
  micIcon.classList.toggle("hidden",  state === "recording" || state === "processing");
  stopIcon.classList.toggle("hidden", state !== "recording");
  spinIcon.classList.toggle("hidden", state !== "processing");
  micBtn.classList.toggle("recording",  state === "recording");
  micBtn.classList.toggle("processing", state === "processing");
  micRingPulse.classList.toggle("recording", state === "recording");
  voiceWaveform.classList.toggle("active", state === "recording");
  micLabel.classList.toggle("recording",  state === "recording");
  micLabel.classList.toggle("processing", state === "processing");

  if (state === "idle" || state === "done") micLabel.textContent = "Tap to speak";
  else if (state === "recording")  micLabel.textContent = "Recording…";
  else if (state === "processing") micLabel.textContent = "Processing…";
  else if (state === "error")      micLabel.textContent = "Error — tap to retry";
}

function clearOutput() {
  transcriptRow.classList.remove("visible");
  responseRow.classList.remove("visible");
  warningRow.classList.add("hidden");
  errorRow.classList.add("hidden");
  actionChip.classList.add("hidden");
  replayBtn.classList.add("hidden");
  lastAudioUrl = null;
}

function showResult(result) {
  transcriptText.textContent = result.transcription || "(inaudible)";
  transcriptRow.classList.add("visible");

  responseText.textContent = result.ai_response || "";
  responseRow.classList.add("visible");

  // Action chip
  const action = result.action || {};
  const atype  = action.action || "unknown";
  if (atype && atype !== "unknown") {
    actionChip.textContent = atype.replace("_", " ");
    actionChip.className   = `action-chip action-${atype}`;
    actionChip.classList.remove("hidden");
  }

  // Warning
  if (result.submit_guard_triggered && result.warning) {
    warningText.textContent = result.warning;
    warningRow.classList.remove("hidden");
  }

  // Replay
  if (result.audio_file) {
    const fname  = result.audio_file.replace(/\\/g, "/").split("/").pop();
    lastAudioUrl = `${BACKEND}/voice/audio/${fname}`;
    replayBtn.classList.remove("hidden");
  }

  setVoiceState("done");
}

function showError(msg) {
  errorText.textContent = msg;
  errorRow.classList.remove("hidden");
  setVoiceState("error");
}

// Aliases for functions that call old names
function showVoiceError(msg) { showError(msg); }
function clearVoiceOutput()  { clearOutput();  }


async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) throw new Error("No active tab found.");
  return tab;
}

async function injectScripts(tabId) {
  if (_injectedTabs.has(tabId)) { console.log("[GoPilot ⏱] injectScripts: SKIPPED (cached)"); return; }

  tStart("inject_check");
  const [check] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => window.__copilotExtractVersion >= 4 && window.__copilotExecutorVersion >= 4,
  });
  tEnd("inject_check");

  if (check.result) {
    _injectedTabs.add(tabId);
    console.log("[GoPilot ⏱] injectScripts: already in page, cached");
    return;
  }

  tStart("inject_files");
  await Promise.all([
    chrome.scripting.executeScript({ target: { tabId }, files: ["content/extractor.js"] }),
    chrome.scripting.executeScript({ target: { tabId }, files: ["content/executor.js"] }),
    chrome.scripting.executeScript({ target: { tabId }, files: ["content/result_scanner.js"] }),
  ]);
  tEnd("inject_files");
  _injectedTabs.add(tabId);
}

async function extractContext(tabId) {
  tStart("extractContext_total");
  await injectScripts(tabId);

  tStart("extractContext_exec");
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      if (typeof window.__copilotExtractContext !== "function")
        return { ok: false, error: "Extractor not available on this page." };
      try { return { ok: true, data: window.__copilotExtractContext() }; }
      catch (e) { return { ok: false, error: e.message }; }
    },
  });
  tEnd("extractContext_exec");
  tEnd("extractContext_total");

  if (!res.result.ok) throw new Error(res.result.error);
  if (!res.result.data) throw new Error("Extractor returned empty context.");

  return res.result.data;
}

async function checkPermission(toolName) {
  if (_permCache.has(toolName)) { console.log(`[GoPilot ⏱] checkPermission(${toolName}): CACHED`); return _permCache.get(toolName); }

  tStart(`perm_${toolName}`);
  try {
    const res = await fetch(`${BACKEND}/tools/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_name: toolName, payload: {} }),
    });
    if (res.status === 403) {
      const b = await res.json().catch(() => ({}));
      const result = { outcome: "blocked", reason: b.detail || "Blocked" };
      _permCache.set(toolName, result);
      return result;
    }
    const result = await res.json();
    tEnd(`perm_${toolName}`);
    _permCache.set(toolName, result);
    return result;
  } catch {
    tEnd(`perm_${toolName}`);
    // Don't cache network failures — backend may come back up
    return { outcome: "allowed", reason: "" };
  }
}

async function executeAction(tabId, action) {
  const act = action.action;

  // ── Navigation: back / reload / home (special URLs, no DOM needed) ────────
  if (act === "navigate") {
    const url = action.url || "";
    if (url === "back") {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: () => window.history.back(),
      });
      return { ok: true, action: "navigate", url: "back" };
    }
    if (url === "reload") {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: () => window.location.reload(),
      });
      return { ok: true, action: "navigate", url: "reload" };
    }
    if (url === "/" || url === "home" || url === "hash_home") {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
          // SPA apps (hash routing) use /#/ as home — detect and use the right one
          const isHashApp = window.location.hash.length > 1 ||
                            window.location.href.includes("/#/");
          if (isHashApp) {
            window.location.hash = "/";
          } else {
            window.location.href = "/";
          }
        },
      });
      return { ok: true, action: "navigate", url };
    }
    // Absolute / relative URL
    if (url.startsWith("http") || url.startsWith("/")) {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: (u) => { window.location.href = u; },
        args: [url],
      });
      return { ok: true, action: "navigate", url };
    }
  }

  // ── Click (nav link or button found by extractor) ─────────────────────────
  if (act === "click") {
    await injectScripts(tabId);
    const [exec] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (a) => {
        try { return { ok: true, result: window.__copilotExecuteAction(a) }; }
        catch (e) { return { ok: false, error: e.message }; }
      },
      args: [action],
    });
    if (!exec?.result?.ok) {
      // Fallback 1: try matching by label text across ALL anchors (including nav)
      const [labelTry] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (lbl) => {
          const all = [...document.querySelectorAll("a, button, [role=button]")];
          const match = all.find(el => el.textContent.trim().toLowerCase() === lbl.toLowerCase());
          if (match) { match.click(); return true; }
          return false;
        },
        args: [action.label || ""],
      }).catch(() => [false]);
      if (labelTry?.result) return { ok: true, action: "click" };

      // Fallback 2: try matching by href directly
      if (action.href) {
        await chrome.scripting.executeScript({
          target: { tabId },
          func: (href) => { window.location.href = href; },
          args: [action.href],
        });
        return { ok: true, action: "click", via: "href_fallback" };
      }
      throw new Error(`Click failed: ${exec?.result?.error || "element not found"}`);
    }
    return exec.result.result;
  }

  // ── Tool call (form fill) ─────────────────────────────────────────────────
  if (act !== "tool_call") return;

  const toolName = resolveToolName(action.field_id);
  const perm     = await checkPermission(toolName);

  if (perm.outcome === "blocked") {
    throw new Error(`Blocked: ${perm.reason}`);
  }

  if (perm.outcome === "requires_confirmation") {
    const [modal] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (act, rsn) => window.__copilotShowConfirmModal(act, rsn),
      args: [action, perm.reason],
    });
    if (!modal.result) throw new Error("User cancelled high-risk action.");
  }

  await injectScripts(tabId);
  const [exec] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (act) => {
      try { return { ok: true, result: window.__copilotExecuteAction(act) }; }
      catch (e) { return { ok: false, error: e.message }; }
    },
    args: [action],
  });

  if (!exec.result.ok) {
    // Return structured failure so caller can voice a recovery, not throw silently
    return { ok: false, reason: exec.result.error || "Unknown error" };
  }
  return exec.result.result;
}

function _encodeWav(samples, sampleRate) {
  const buf    = new ArrayBuffer(44 + samples.length * 2);
  const view   = new DataView(buf);
  const write  = (o, s) => { for (let i=0;i<s.length;i++) view.setUint8(o+i, s.charCodeAt(i)); };
  const len    = samples.length * 2;
  write(0,  "RIFF"); view.setUint32(4,  36 + len, true);
  write(8,  "WAVE"); write(12, "fmt ");
  view.setUint32(16, 16, true);  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);   view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);  write(36, "data");
  view.setUint32(40, len, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

async function _webmToWav(webmBlob) {
  // Decode WebM/OGG via Web Audio API → re-encode as 16kHz mono WAV
  // This runs entirely in the browser — no ffmpeg needed on the server.
  try {
    const arrayBuf  = await webmBlob.arrayBuffer();
    const audioCtx  = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    const decoded   = await audioCtx.decodeAudioData(arrayBuf);
    audioCtx.close();
    // Mix down to mono (take channel 0)
    const samples   = decoded.getChannelData(0);
    return _encodeWav(samples, decoded.sampleRate);
  } catch (e) {
    console.warn("[GoPilot] WAV encode failed, sending raw:", e.message);
    return null;  // caller falls back to raw blob
  }
}

function _estimateTTSDuration(text) {
  return Math.max(800, (text || "").length * 80);
}

function _scheduleGuidedMicReopen() {
  // Cancel any pending timer (prevents double-open)
  if (_guidedMicTimer) { clearTimeout(_guidedMicTimer); _guidedMicTimer = null; }

  // Get the last spoken text to estimate TTS duration
  const lastSpoken = document.getElementById("voice-result-text")?.textContent || "";
  const delay      = _estimateTTSDuration(lastSpoken) + 300; // +300ms buffer

  _guidedMicTimer = setTimeout(async () => {
    _guidedMicTimer = null;
    // Only reopen if NOT already recording (user may have clicked themselves)
    if (!isRecording) {
      console.log("[GoPilot Guided] Auto-reopening mic for next answer...");
      try {
        startRecording();
      } catch (e) {
        console.warn("[GoPilot Guided] Auto-mic failed:", e.message);
      }
    }
  }, delay);
}

function speakFallback(text) {
  if (!text || !("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.05; u.lang = "en-IN";
  window.speechSynthesis.speak(u);
}

async function kokoroReadback(type, payload) {
  try {
    const res  = await fetch(`${BACKEND_URL}/voice/readback`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ readback_type: type, ...payload }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.audio_file) {
      const fname = data.audio_file.replace(/\\/g, "/").split("/").pop();
      await playAudio(`${BACKEND_URL}/voice/audio/${fname}`);
      console.log(`[GoPilot Readback ${type}]`, data.spoken);
      return data.spoken;
    }
  } catch (e) {
    console.warn("[GoPilot Readback] backend failed, using browser TTS:", e.message);
    return null;
  }
}

async function readbackSubmit(tabId) {
  await new Promise(r => setTimeout(r, 750));
  try {
    const [scan] = await chrome.scripting.executeScript({
      target: { tabId },
      func:   () => window.__copilotReadbackSubmit(),
    });
    const spoken = await kokoroReadback("submit", {
      toast_text:   scan?.result?.raw  || "",
      form_cleared: scan?.result?.sentiment === "success",
    });
    if (!spoken) speakFallback(scan?.result?.spoken || "Submitted.");

    // Clear cross-page memory after successful submit
    if (scan?.result?.sentiment === "success" || scan?.result?.raw) {
      const sid = await getTabSessionId();
      fetch(`${BACKEND_URL}/voice/page_memory/clear`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ tab_session_id: sid }),
      }).catch(() => {});
    }
  } catch (e) { console.warn("[GoPilot Readback submit]", e.message); }
}

async function readbackNav(tabId) {
  await new Promise(r => setTimeout(r, 900));
  try {
    // Re-extract fresh context from the new page
    await injectScripts(tabId);
    const ctx = await extractContext(tabId).catch(() => ({}));
    const url = (await getActiveTab().catch(() => ({url:""}))).url || "";
    const spoken = await kokoroReadback("nav", {
      screen_context: ctx,
      url,
    });
    if (!spoken) speakFallback("Page loaded.");
  } catch (e) { console.warn("[GoPilot Readback nav]", e.message); }
}

async function _triggerNextMultiFill() {
  try {
    const tab = await getActiveTab();
    const screenContext = await extractContext(tab.id);
    const sid = await getTabSessionId();

    // Send a silent trigger — tiny silent WAV
    const silentWav = _makeSilentWav(0.1);
    const formData  = new FormData();
    formData.append("audio",          silentWav, "silent.wav");
    formData.append("workflow",       voiceWorkflowSelect?.value || "purchase");
    formData.append("session_id",     sid);
    formData.append("tab_session_id", sid);
    formData.append("screen_context", JSON.stringify(screenContext));
    formData.append("play_audio",     "false");
    formData.append("multi_fill_trigger", "true");

    const res = await fetch(`${BACKEND_URL}/voice/process`, {
      method: "POST",
      body:   formData,
    });
    if (!res.ok) return;
    const result = await res.json();

    // Execute the fill
    const action = result.action || {};
    if (action.action === "tool_call") {
      const execResult = await executeAction(tab.id, action);
      if (execResult?.ok === false) {
        await voiceExecutorError(execResult.reason, action);
        return;
      }
      // readbackFill speaks the confirmation — do NOT also play result.audio_file
      await readbackFill(tab.id, action);
    }

    // Continue if more fills pending (after readback finishes)
    if (result.multi_fill_active) {
      setTimeout(() => _triggerNextMultiFill(), 400);
    }

  } catch (e) {
    console.warn("[GoPilot MultiFill] trigger error:", e.message);
  }
}

function _makeSilentWav(durationSec) {
  const sampleRate  = 16000;
  const numSamples  = Math.floor(sampleRate * durationSec);
  const buffer      = new ArrayBuffer(44 + numSamples * 2);
  const view        = new DataView(buffer);
  const writeStr    = (offset, str) => { for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i)); };
  writeStr(0, "RIFF"); view.setUint32(4, 36 + numSamples * 2, true);
  writeStr(8, "WAVE"); writeStr(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); writeStr(36, "data");
  view.setUint32(40, numSamples * 2, true);
  // samples stay 0 (silence)
  return new Blob([buffer], { type: "audio/wav" });
}

async function voiceExecutorError(reason, action) {
  console.warn("[GoPilot Recovery] Executor error:", reason);
  try {
    const sid = await getTabSessionId();
    const res = await fetch(`${BACKEND_URL}/voice/recover`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        error_reason:   reason,
        field_id:       action.field_id || "",
        field_label:    action.label    || "",
        original_value: String(action.value ?? ""),
        session_id:     sid,
        screen_context: lastScreenContext || {},
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // Play recovery audio (error description + fix offer)
    if (data.audio_file) {
      const fname = data.audio_file.replace(/\\/g, "/").split("/").pop();
      await playAudio(`${BACKEND_URL}/voice/audio/${fname}`);
    }

    // Don't auto-open mic after errors — user will click when ready
    console.log("[GoPilot Recovery] Spoken:", data.spoken);
  } catch (e) {
    console.warn("[GoPilot Recovery] /voice/recover failed:", e.message);
    // Fallback: browser TTS
    speakFallback(`I couldn't complete that fill. ${reason}`);
  }
}

async function readbackFill(tabId, action) {
  await new Promise(r => setTimeout(r, 200));
  try {
    const [scan] = await chrome.scripting.executeScript({
      target: { tabId },
      func:   (fid, lbl, val) => window.__copilotReadbackFill(fid, lbl, val),
      args:   [action.field_id || "", action.label || "", String(action.value ?? "")],
    });

    const res = scan?.result;

    // DOM shows a validation error — route to recovery
    if (res?.has_error && res?.dom_error) {
      console.warn("[GoPilot Recovery] DOM error after fill:", res.dom_error);
      await voiceDomError(res.dom_error, action);
      return;
    }

    // Normal readback
    const spoken = await kokoroReadback("fill", {
      action:         { ...action, value: res?.actual_value || action.value },
      screen_context: lastScreenContext || {},
    });
    if (!spoken) speakFallback(res?.spoken || `Done — ${action.label || "field"} filled.`);
  } catch (e) { console.warn("[GoPilot Readback fill]", e.message); }
}

async function voiceDomError(domErrorText, action) {
  try {
    const sid = await getTabSessionId();
    const res = await fetch(`${BACKEND_URL}/voice/recover`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        error_reason:   domErrorText,
        error_source:   "dom",
        field_id:       action.field_id || "",
        field_label:    action.label    || "",
        original_value: String(action.value ?? ""),
        session_id:     sid,
        screen_context: lastScreenContext || {},
      }),
    });
    const data = res.ok ? await res.json() : {};
    if (data.audio_file) {
      const fname = data.audio_file.replace(/\\/g, "/").split("/").pop();
      await playAudio(`${BACKEND_URL}/voice/audio/${fname}`);
    }
    if (data.recovery_pending) _scheduleGuidedMicReopen();
  } catch (e) {
    speakFallback(`Validation issue with ${action.label || "that field"}: ${domErrorText}`);
  }
}

// ── MediaRecorder ─────────────────────────────────────────────────────────
async function startRecording() {
  if (isRecording) return;
  clearOutput();

  try {
    voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  } catch (err) {
    showError("Microphone access denied. Allow microphone in browser settings.");
    return;
  }

  audioChunks = [];
  const mimeType = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus"
    : MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";

  try {
    mediaRecorder = new MediaRecorder(voiceStream, mimeType ? { mimeType } : {});
  } catch { mediaRecorder = new MediaRecorder(voiceStream); }

  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) audioChunks.push(e.data);
  };
  mediaRecorder.onstop = async () => {
    voiceStream.getTracks().forEach(t => t.stop());
    voiceStream = null;
    if (audioChunks.length === 0) { showError("No audio captured. Please try again."); return; }
    await processVoiceAudio();
  };

  mediaRecorder.start(100);
  isRecording = true;
  setVoiceState("recording");
}

function stopRecording() {
  if (!isRecording || !mediaRecorder) return;
  isRecording = false;
  mediaRecorder.stop();
  setVoiceState("processing");
}

async function processVoiceAudio() {
  setVoiceState("processing");
  const mimeType = audioChunks[0]?.type || "audio/webm";
  const rawBlob  = new Blob(audioChunks, { type: mimeType });
  audioChunks    = [];

  let audioBlob = rawBlob, ext = ".wav";
  if (mimeType.includes("webm") || mimeType.includes("ogg") || mimeType.includes("mp4")) {
    const wavBlob = await _webmToWav(rawBlob);
    if (wavBlob) { audioBlob = wavBlob; ext = ".wav"; }
    else { ext = mimeType.includes("webm") ? ".webm" : mimeType.includes("ogg") ? ".ogg" : ".wav"; }
  }

  let screenContext = {};
  try {
    const tab = await getActiveTab();
    screenContext = await extractContext(tab.id);
    lastScreenContext = screenContext;
  } catch { /* proceed without context */ }

  const workflow  = voiceWorkflowSelect.value;
  const sessionId = await getTabSessionId();

  const formData = new FormData();
  formData.append("audio",          audioBlob, `recording${ext}`);
  formData.append("workflow",       workflow);
  formData.append("session_id",     sessionId);
  formData.append("tab_session_id", sessionId);
  formData.append("screen_context", JSON.stringify(screenContext));
  formData.append("play_audio",     "false");

  let result;
  try {
    const res = await fetch(`${BACKEND}/voice/process`, { method: "POST", body: formData });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Backend error ${res.status}`);
    }
    result = await res.json();
  } catch (err) {
    showError(err.message || "Voice pipeline failed.");
    return;
  }

  showResult(result);

  const action    = result.action || {};
  const isNavAct  = result.nav_action === true;
  const isFillAct = action.action === "tool_call" && !result.submit_guard_triggered;

  // For fills: skip pre-execution audio — readbackFill speaks the confirmed value
  // For everything else (explain, error, nav): play the backend TTS
  if (lastAudioUrl && !isFillAct) {
    await playAudio(lastAudioUrl, "voice");
    // Stamp time so vision_observer suppresses nav announcements for 4s
    try {
      const tab = await getActiveTab();
      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => { window.__gopilotLastVoiceAudioTime = Date.now(); },
      }).catch(() => {});
    } catch (_) {}
  }

  if ((isFillAct || isNavAct) && action.action) {
    try {
      const tab = await getActiveTab();
      const execResult = await executeAction(tab.id, action);

      if (execResult && execResult.ok === false && action.action === "tool_call") {
        await voiceExecutorError(execResult.reason || "Unknown error", action);
        return;
      }

      if (action.action === "tool_call") {
        const isSubmit = action.field_id === "submit"
          || /^(submit|save)$/i.test(action.label || "")
          || action.is_submit === true;
        if (isSubmit) readbackSubmit(tab.id);
        else          await readbackFill(tab.id, action);
      } else if (isNavAct) {
        readbackNav(tab.id);
      }
    } catch (err) {
      console.warn("[GoPilot] Execution failed:", err.message);
    }
  }

  if (result.multi_fill_active) {
    setTimeout(() => _triggerNextMultiFill(), 400);
  }

  const guidedIsAsking = result.guided_fill_active &&
    (action.action === "explain" || action.action === "confirmation" || !action.action);
  if (guidedIsAsking) _scheduleGuidedMicReopen();
}

// ── Mic button ────────────────────────────────────────────────────────────
micBtn.addEventListener("click", () => {
  if (isRecording) stopRecording();
  else             startRecording();
});

replayBtn.addEventListener("click", () => {
  if (lastAudioUrl) playAudio(lastAudioUrl, "voice");
});

// ── CoPilot toggle ────────────────────────────────────────────────────────
chrome.storage.local.get(["copilotEnabled"], (data) => {
  const on = data.copilotEnabled === true;
  copilotToggle.checked  = on;
  toggleLabel.textContent = on ? "ON" : "OFF";
  toggleLabel.classList.toggle("on", on);
});

copilotToggle.addEventListener("change", async () => {
  const enabled = copilotToggle.checked;
  toggleLabel.textContent = enabled ? "ON" : "OFF";
  toggleLabel.classList.toggle("on", enabled);
  chrome.storage.local.set({ copilotEnabled: enabled });

  try {
    const tab = await getActiveTab();
    await injectScripts(tab.id);
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func:   (en) => window.__copilotToggle && window.__copilotToggle(en),
      args:   [enabled],
    });
  } catch (e) { console.warn("[GoPilot] Toggle failed:", e.message); }
});
// ── Settings panel ────────────────────────────────────────────────────────
const settingsBtn   = document.getElementById("settings-btn");
const settingsPanel = document.getElementById("settings-panel");
const backendInput  = document.getElementById("backend-url-input");
const saveBackendBtn= document.getElementById("save-backend-btn");

if (settingsBtn && settingsPanel) {
  settingsBtn.addEventListener("click", () => {
    settingsPanel.classList.toggle("open");
    // Refresh input in case storage changed externally
    if (backendInput) backendInput.value = BACKEND;
  });
}

if (saveBackendBtn && backendInput) {
  saveBackendBtn.addEventListener("click", async () => {
    const val = backendInput.value.trim().replace(/\/$/, "");
    if (!val) return;
    // Validate it looks like a URL
    if (!/^https?:\/\/.+/.test(val)) {
      backendInput.style.borderColor = "var(--danger)";
      setTimeout(() => backendInput.style.borderColor = "", 1500);
      return;
    }
    BACKEND     = val;
    BACKEND_URL = val;
    await chrome.storage.local.set({ gopilotBackendUrl: val });
    // Tell vision_observer running in the active tab about the new URL
    try {
      const tab = await getActiveTab();
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func:   (url) => { if (window.__copilotSetBackend) window.__copilotSetBackend(url); },
        args:   [val],
      });
    } catch (_) {}
    settingsPanel.classList.remove("open");
    console.log("[GoPilot] Backend URL updated to:", val);
  });
}

// ════════════════════════════════════════════════════════════════════════════
// PRODUCTION COPILOT FEATURES — Tab switching, Chat history, Text commands,
//   Explain tab, Context tab, Navigate tab, Quick commands
// ════════════════════════════════════════════════════════════════════════════

// ── Tab switching ────────────────────────────────────────────────────────────
document.querySelectorAll(".tab[data-tab]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab[data-tab]").forEach(t => t.classList.remove("active"));
    btn.classList.add("active");
    const id = btn.dataset.tab;
    document.querySelectorAll(".tab-panel[id^='tab-']").forEach(p => {
      p.classList.toggle("hidden", p.id !== `tab-${id}`);
    });
    if (id === "context")  _autoScanContext();
    if (id === "navigate") _populateNavTiles();
  });
});

// ── Workflow selector — wire real <select> to voiceWorkflowSelect stub ────────
const _workflowSelectEl = document.getElementById("workflow-select");
if (_workflowSelectEl) {
  Object.defineProperty(voiceWorkflowSelect, "value", {
    get: () => _workflowSelectEl.value || "free",
    configurable: true,
  });
}

// ── Chat history ──────────────────────────────────────────────────────────────
const _chatEl      = document.getElementById("chat-history");
const _chatHistory = [];
const MAX_CHAT_TURNS = 12;

function _appendChat(role, text, { actionType = null, audioUrl = null } = {}) {
  if (!text || !_chatEl) return;
  _chatHistory.push({ role, text, actionType, audioUrl });
  if (_chatHistory.length > MAX_CHAT_TURNS) _chatHistory.shift();

  const bubble  = document.createElement("div");
  bubble.className = `chat-bubble chat-bubble--${role}`;

  const labelRow = document.createElement("div");
  labelRow.className = "chat-bubble-label";
  labelRow.textContent = role === "you" ? "You" : "GoPilot";

  if (role === "ai" && audioUrl) {
    const rep = document.createElement("button");
    rep.className = "chat-replay-inline";
    rep.title = "Replay";
    rep.textContent = "▶";
    rep.addEventListener("click", () => playAudio(audioUrl, "voice"));
    labelRow.appendChild(rep);
  }

  const textEl = document.createElement("div");
  textEl.textContent = text;
  bubble.appendChild(labelRow);
  bubble.appendChild(textEl);

  if (role === "ai" && actionType && actionType !== "unknown") {
    const badge = document.createElement("span");
    badge.className = `badge badge--${actionType}`;
    badge.style.cssText = "margin-top:5px;display:inline-block;";
    badge.textContent = actionType.replace("_", " ").toUpperCase();
    bubble.appendChild(badge);
  }

  _chatEl.appendChild(bubble);
  _chatEl.scrollTop = _chatEl.scrollHeight;
}

// ── Override showResult to write to chat history ──────────────────────────────
const _origShowResult = showResult;
showResult = function(result) {          // reassign global
  _origShowResult(result);              // keep legacy elements updated (hidden)

  const text    = result.transcription || "";
  const aiText  = result.ai_response || result.spoken_response || "";
  const action  = result.action || {};
  const atype   = action.action || null;

  let audioUrl = null;
  if (result.audio_file) {
    const fname = result.audio_file.replace(/\\/g, "/").split("/").pop();
    audioUrl = `${BACKEND}/voice/audio/${fname}`;
  }

  if (text && text !== "(inaudible)") _appendChat("you", text);
  if (aiText)                          _appendChat("ai", aiText, { actionType: atype, audioUrl });

  // Show inline warning
  if (result.submit_guard_triggered && result.warning) {
    const wb = document.getElementById("voice-warning-box");
    if (wb) { wb.textContent = "⚠ " + result.warning; wb.classList.remove("hidden"); }
  }

  const vs = document.getElementById("voice-status");
  if (vs) vs.textContent = "TAP TO SPEAK";
};

// ── Override showError to write to chat history ───────────────────────────────
const _origShowError = showError;
showError = function(msg) {
  _origShowError(msg);
  const eb = document.getElementById("voice-error-box");
  const em = document.getElementById("voice-error-msg");
  if (eb && em) { em.textContent = msg; eb.classList.remove("hidden"); }
  _appendChat("ai", "⚠ " + msg, { actionType: "error" });
};

// ── Override setVoiceState to update new UI elements ─────────────────────────
const _origSetVoiceState = setVoiceState;
setVoiceState = function(state) {
  _origSetVoiceState(state);

  const vs   = document.getElementById("voice-status");
  const wf   = document.getElementById("voice-waveform");
  const ring = document.getElementById("voice-ring");
  const mb   = document.getElementById("mic-btn");
  const dot  = document.getElementById("status-dot");

  const labels = { idle:"TAP TO SPEAK", recording:"Recording…",
                   processing:"Processing…", done:"TAP TO SPEAK", error:"Error — tap to retry" };
  if (vs) {
    vs.textContent = labels[state] || "TAP TO SPEAK";
    vs.className   = "voice-status-text" +
      (state === "recording" ? " recording" : state === "processing" ? " processing" :
       state === "done"      ? " ok"        : "");
  }
  if (wf)   wf.classList.toggle("hidden", state !== "recording");
  if (ring) { ring.classList.remove("recording","processing"); ring.classList.toggle("recording",  state === "recording");  ring.classList.toggle("processing", state === "processing"); }
  if (mb)   { mb.classList.remove("recording","processing");   mb.classList.toggle("recording",    state === "recording");  mb.classList.toggle("processing",   state === "processing"); }
  if (dot)  { dot.className = "dot " + (state === "recording" ? "dot--error" : state === "processing" ? "dot--busy" : "dot--ok"); }
};

// ── Override clearOutput to reset new warning/error boxes ────────────────────
const _origClearOutput = clearOutput;
clearOutput = function() {
  _origClearOutput();
  const wb = document.getElementById("voice-warning-box");
  const eb = document.getElementById("voice-error-box");
  if (wb) wb.classList.add("hidden");
  if (eb) eb.classList.add("hidden");
};

// ── Text-input command ────────────────────────────────────────────────────────
async function processTextCommand(rawText) {
  const text = (rawText || "").trim();
  if (!text) return;

  // Switch to voice tab so the user sees the result
  document.querySelector('.tab[data-tab="voice"]')?.click();
  clearOutput();
  setVoiceState("processing");
  _appendChat("you", text);

  try {
    const tab = await getActiveTab();
    let ctx = lastScreenContext || {};
    try { ctx = await extractContext(tab.id); lastScreenContext = ctx; } catch (_) {}

    const sessionId = await getTabSessionId();

    // Call /voice/text (text-only pipeline, no STT)
    const res = await fetch(`${BACKEND}/voice/text`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text:           text,
        workflow:       voiceWorkflowSelect?.value || "free",
        session_id:     sessionId,
        screen_context: ctx,
      }),
    });

    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const result = await res.json();

    showResult(result);   // writes to chat history via override above

    const action    = result.action || {};
    const isNavAct  = result.nav_action === true;
    const isFillAct = action.action === "tool_call" && !result.submit_guard_triggered;

    if (result.audio_file && !isFillAct) {
      const fname = result.audio_file.replace(/\\/g, "/").split("/").pop();
      await playAudio(`${BACKEND}/voice/audio/${fname}`, "voice");
    }

    if ((isFillAct || isNavAct) && action.action) {
      const execResult = await executeAction(tab.id, action);
      if (execResult?.ok === false && action.action === "tool_call") {
        await voiceExecutorError(execResult.reason || "Unknown error", action);
        return;
      }
      if (action.action === "tool_call") {
        const isSubmit = action.field_id === "submit" || /^(submit|save)$/i.test(action.label || "");
        if (isSubmit) readbackSubmit(tab.id); else await readbackFill(tab.id, action);
      } else if (isNavAct) {
        readbackNav(tab.id);
      }
    }

    if (result.multi_fill_active) setTimeout(() => _triggerNextMultiFill(), 400);

  } catch (e) {
    showError(e.message || "Command failed.");
  } finally {
    setVoiceState("done");
  }
}

const _textInput   = document.getElementById("text-input");
const _textSendBtn = document.getElementById("text-send-btn");
if (_textInput && _textSendBtn) {
  _textSendBtn.addEventListener("click", () => {
    const t = _textInput.value.trim();
    if (t) { _textInput.value = ""; processTextCommand(t); }
  });
  _textInput.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const t = _textInput.value.trim();
      if (t) { _textInput.value = ""; processTextCommand(t); }
    }
  });
}

// ── Quick command chips ────────────────────────────────────────────────────────
document.querySelectorAll(".chip[data-cmd]").forEach(chip => {
  chip.addEventListener("click", async () => {
    const cmd = chip.dataset.cmd;
    if (!cmd) return;
    if (cmd.toLowerCase().includes("explain")) {
      document.querySelector('.tab[data-tab="explain"]')?.click();
      setTimeout(() => document.getElementById("explain-btn")?.click(), 80);
      return;
    }
    await processTextCommand(cmd);
  });
});

// ── EXPLAIN TAB ────────────────────────────────────────────────────────────────
async function _explainPage(instruction) {
  const btn    = document.getElementById("explain-btn");
  const result = document.getElementById("explain-result");
  const text   = document.getElementById("explain-text");
  const title  = document.getElementById("explain-page-title");
  const errBox = document.getElementById("explain-error-box");
  const errMsg = document.getElementById("explain-error-msg");

  if (btn) btn.disabled = true;
  if (errBox) errBox.classList.add("hidden");

  try {
    const tab = await getActiveTab();
    let ctx = lastScreenContext || {};
    try { ctx = await extractContext(tab.id); lastScreenContext = ctx; } catch (_) {}

    const res = await fetch(`${BACKEND}/vision/explain`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        screen_context:   ctx,
        user_instruction: instruction || "Explain this page",
        session_id:       "",
      }),
    });

    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const data = await res.json();

    const explanation = data.explanation || data.page_purpose || "I can see this page is ready to use.";
    if (result) result.classList.remove("hidden");
    if (text)   text.textContent  = explanation;
    if (title)  title.textContent = data.page_title || "";

    // Speak the explanation via backend TTS
    const speakRes = await fetch(`${BACKEND}/voice/announce`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ screen_context: ctx, session_id: "" }),
    }).catch(() => null);
    if (speakRes?.ok) {
      const sd = await speakRes.json();
      if (sd.audio_file) {
        const fname = sd.audio_file.replace(/\\/g, "/").split("/").pop();
        playAudio(`${BACKEND}/voice/audio/${fname}`, "voice");
      }
    }

  } catch (e) {
    if (errBox) errBox.classList.remove("hidden");
    if (errMsg) errMsg.textContent = e.message || "Failed to explain page.";
  } finally {
    if (btn) btn.disabled = false;
  }
}

document.getElementById("explain-btn")?.addEventListener("click", () => _explainPage(""));
document.getElementById("explain-ask-btn")?.addEventListener("click", () => {
  const q = document.getElementById("explain-question")?.value?.trim();
  if (!q) return;
  document.getElementById("explain-question").value = "";
  _explainPage(q);
});
document.getElementById("explain-question")?.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); document.getElementById("explain-ask-btn")?.click(); }
});

// ── CONTEXT TAB ────────────────────────────────────────────────────────────────
async function _autoScanContext() {
  const fl = document.getElementById("field-list");
  if (!fl || fl.children.length > 0) return;  // already populated
  await _scanContextFields();
}

async function _scanContextFields() {
  const scanBtn  = document.getElementById("scan-btn");
  const fieldList = document.getElementById("field-list");
  if (!fieldList) return;
  if (scanBtn) { scanBtn.disabled = true; scanBtn.innerHTML = '<span class="btn-icon">🔄</span> Scanning…'; }

  try {
    const tab = await getActiveTab();
    const ctx = await extractContext(tab.id);
    lastScreenContext = ctx;

    const allFields = (ctx.sections || []).flatMap(s => s.fields || []);
    const fillable  = allFields.filter(f => !f.readonly && !f.calculated);
    const filled    = fillable.filter(f => f.value && String(f.value).trim());
    const req       = allFields.filter(f => f.required && !f.readonly);
    const missing   = req.filter(f => !(f.value && String(f.value).trim()));

    document.getElementById("stat-total").textContent    = fillable.length;
    document.getElementById("stat-filled").textContent   = filled.length;
    document.getElementById("stat-required").textContent = req.length;
    document.getElementById("stat-missing").textContent  = missing.length;

    fieldList.innerHTML = "";
    const display = allFields.filter(f => !f.calculated).slice(0, 35);

    if (!display.length) {
      fieldList.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center;padding:12px 0;">No form fields found on this page.</div>';
    } else {
      display.forEach(f => {
        const item  = document.createElement("div");
        item.className = "field-item";
        item.title     = f.readonly ? "Read-only field" : `Click to fill "${f.label || f.field_id}"`;

        const left  = document.createElement("div");
        left.className = "field-item-left";
        const idEl  = document.createElement("div");
        idEl.className = "field-item-id";
        idEl.textContent = f.field_id || "—";
        const lblEl = document.createElement("div");
        lblEl.className = "field-item-label";
        lblEl.textContent = f.label || f.field_id;
        left.append(idEl, lblEl);

        let statusText, statusClass;
        if (f.readonly || f.calculated) {
          statusText = "read-only"; statusClass = "field-status--readonly";
        } else if (f.value && String(f.value).trim()) {
          statusText = "✓ " + String(f.value).slice(0, 12); statusClass = "field-status--filled";
        } else if (f.required) {
          statusText = "⚠ missing"; statusClass = "field-status--missing";
        } else {
          statusText = "empty"; statusClass = "field-status--readonly";
        }

        const st = document.createElement("span");
        st.className = `field-status ${statusClass}`;
        st.textContent = statusText;

        item.append(left, st);

        // Click → switch to Voice tab, pre-fill the text input
        if (!f.readonly && !f.calculated) {
          item.addEventListener("click", () => {
            document.querySelector('.tab[data-tab="voice"]')?.click();
            const ti = document.getElementById("text-input");
            if (ti) { ti.value = `Fill ${f.label || f.field_id} `; ti.focus(); }
          });
        }
        fieldList.appendChild(item);
      });
    }
  } catch (e) {
    if (fieldList) fieldList.innerHTML = `<div style="color:var(--red);font-size:11px;padding:8px 0;">Scan failed: ${e.message}</div>`;
  } finally {
    if (scanBtn) { scanBtn.disabled = false; scanBtn.innerHTML = '<span class="btn-icon">🔍</span> Scan Page Fields'; }
  }
}

document.getElementById("scan-btn")?.addEventListener("click", _scanContextFields);

// ── NAVIGATE TAB ────────────────────────────────────────────────────────────────
async function _populateNavTiles() {
  const area = document.getElementById("nav-dynamic");
  if (!area || area.dataset.done === "1") return;
  try {
    const tab = await getActiveTab();
    const ctx = await extractContext(tab.id).catch(() => lastScreenContext || {});
    lastScreenContext = ctx;

    const navLinks = [
      ...(ctx.nav_links || []),
      ...(ctx.buttons  || []).filter(b => b.is_nav && !b.disabled),
    ].slice(0, 9);

    if (!navLinks.length) return;

    const sep = document.createElement("div");
    sep.style.cssText = "font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px;";
    sep.textContent = "Page links";
    area.appendChild(sep);

    navLinks.forEach(link => {
      const lbl = (link.label || "")
        .replace(/\([^)]*\)/g, "")
        .replace(/[\u0900-\u0D7F]+/g, "")
        .trim();
      if (!lbl || lbl.length < 2) return;

      const chip = document.createElement("button");
      chip.className = "chip";
      chip.textContent = lbl;
      chip.addEventListener("click", () => _processNavAction(`go to ${lbl}`));
      area.appendChild(chip);
    });

    area.dataset.done = "1";
  } catch (_) {}
}

async function _processNavAction(instruction) {
  document.querySelector('.tab[data-tab="voice"]')?.click();
  clearOutput();
  setVoiceState("processing");
  _appendChat("you", instruction);

  try {
    const tab = await getActiveTab();
    let ctx = lastScreenContext || {};
    try { ctx = await extractContext(tab.id); lastScreenContext = ctx; } catch (_) {}

    // Delegate to /voice/text so the full nav resolution pipeline runs
    const sessionId = await getTabSessionId();
    const res = await fetch(`${BACKEND}/voice/text`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text:           instruction,
        workflow:       "free",
        session_id:     sessionId,
        screen_context: ctx,
      }),
    });

    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const result = await res.json();
    const action = result.action || {};

    // Announce
    const spoken = result.ai_response || "";
    if (spoken) _appendChat("ai", spoken, { actionType: action.action || "navigate" });

    if (result.audio_file) {
      const fname = result.audio_file.replace(/\\/g, "/").split("/").pop();
      await playAudio(`${BACKEND}/voice/audio/${fname}`, "voice");
    }

    if (action.action === "navigate" || action.action === "click") {
      const execResult = await executeAction(tab.id, action);
      if (execResult?.ok !== false) readbackNav(tab.id);
    }

  } catch (e) {
    _appendChat("ai", "Navigation failed: " + e.message, { actionType: "error" });
  } finally {
    setVoiceState("done");
  }
}

// Nav grid tiles
document.querySelectorAll(".nav-tile[data-nav]").forEach(tile => {
  tile.addEventListener("click", () => _processNavAction(tile.dataset.nav));
});

// Nav text input
const _navInput = document.getElementById("nav-input");
const _navGoBtn = document.getElementById("nav-go-btn");
if (_navGoBtn && _navInput) {
  _navGoBtn.addEventListener("click", () => {
    const cmd = _navInput.value.trim();
    if (!cmd) return;
    _navInput.value = "";
    _processNavAction(cmd);
  });
  _navInput.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); _navGoBtn.click(); }
  });
}

// ── Status dot check on startup ───────────────────────────────────────────────
(async () => {
  try {
    const res = await fetch(`${BACKEND}/plugins`, { signal: AbortSignal.timeout(3000) }).catch(() => null);
    const dot = document.getElementById("status-dot");
    const lbl = document.getElementById("status-label");
    if (res?.ok) {
      if (dot) dot.className = "dot dot--ok";
      if (lbl) lbl.textContent = "READY";
    } else {
      if (dot) dot.className = "dot dot--error";
      if (lbl) lbl.textContent = "OFFLINE";
    }
  } catch (_) {}
})();
