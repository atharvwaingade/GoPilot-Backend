"use strict";
/**
 * executor.js v5 — Universal Field Filler
 *
 * Works on: Google Forms, Typeform, React/Vue/Angular apps,
 *           Salesforce, SAP, plain HTML, any custom ERP
 *
 * findEl uses 10 strategies in order:
 *  1. data-copilot-field-id (stamped by extractor — most reliable)
 *  2. Direct DOM id
 *  3. name attribute
 *  4. Exact aria-label
 *  5. Partial id/name match
 *  6. <label for> matching
 *  7. Question container scan (Google Forms, Typeform, etc.)
 *  8. Any element with matching aria-label (fuzzy)
 *  9. Placeholder match
 * 10. Any visible input on page if only one matches keyword
 */
(function () {
  if (window.__copilotExecutorVersion >= 5) return;
  window.__copilotExecutorVersion = 5;

  const HL_STYLE = "outline:3px solid #7c6af7 !important;outline-offset:2px;box-shadow:0 0 0 5px rgba(124,106,247,0.2) !important;transition:all 0.15s;";
  const HL_MS    = 2500;

  // ── Confirmation modal ─────────────────────────────────────────────────────
  window.__copilotShowConfirmModal = function (action, reason) {
    return new Promise(resolve => {
      document.getElementById("_cp_modal")?.remove();
      const overlay = mk("div", { id: "_cp_modal", style: "position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,0.75);display:flex;align-items:center;justify-content:center;font-family:system-ui,sans-serif" });
      const box = mk("div", { style: "background:#18181b;border:1px solid #3f3f46;border-radius:12px;padding:24px;max-width:420px;width:90%;color:#e4e4e7" });
      box.innerHTML = `
        <p style="font-weight:700;font-size:15px;margin-bottom:8px;color:#f0a030">⚠ Confirm Action</p>
        <p style="font-size:13px;color:#a1a1aa;margin-bottom:8px">${reason}</p>
        <pre style="background:#0d0f12;border:1px solid #3f3f46;border-radius:6px;padding:8px;font-size:10px;color:#3dd68c;max-height:100px;overflow-y:auto;margin-bottom:16px">${JSON.stringify(action, null, 2)}</pre>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button id="_cp_cancel" style="padding:7px 16px;border-radius:6px;border:1px solid #3f3f46;background:#27272a;color:#e4e4e7;font-size:13px;cursor:pointer">Cancel</button>
          <button id="_cp_confirm" style="padding:7px 16px;border-radius:6px;border:none;background:#7c6af7;color:#fff;font-size:13px;font-weight:700;cursor:pointer">Confirm</button>
        </div>`;
      overlay.appendChild(box);
      document.body.appendChild(overlay);
      box.querySelector("#_cp_cancel").onclick  = () => { overlay.remove(); resolve(false); };
      box.querySelector("#_cp_confirm").onclick = () => { overlay.remove(); resolve(true); };
    });
  };

  // ── Main entry point ───────────────────────────────────────────────────────
  window.__copilotExecuteAction = function (action) {
    if (!action?.action) return { ok: false, reason: "No action provided" };
    if (action.action === "tool_call")                          return doFill(action);
    if (action.action === "tab_switch")                         return doTabSwitch(action);
    if (action.action === "navigate" || action.action === "click") return doClick(action);
    return { ok: false, reason: `Unknown action type: ${action.action}` };
  };

  // ── Slug normaliser — same logic as extractor ──────────────────────────────
  function slugOf(text) {
    return (text || "")
      .replace(/\([^)]+\)/g, "")
      .replace(/[\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0B80-\u0BFF]+/g, "")
      .replace(/[*†‡§¶#@!?:]+/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 50);
  }

  function slugMatch(a, b) {
    const sa = slugOf(a), sb = slugOf(b);
    if (!sa || !sb) return false;
    if (sa === sb) return true;
    // All words in needle appear in haystack
    const words = sa.split("_").filter(w => w.length > 1);
    return words.length > 0 && words.every(w => sb.includes(w));
  }

  const INPUT_SEL = "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]):not([type=file]), select, textarea, [contenteditable=true], [role=combobox], [role=textbox]";

  // ── 10-strategy element finder ─────────────────────────────────────────────
  function findEl(fieldId) {
    if (!fieldId) return null;
    const fid = String(fieldId);
    const esc = CSS.escape(fid);

    // 1. data-copilot-field-id stamped by extractor (most reliable)
    let el = document.querySelector(`[data-copilot-field-id="${esc}"]`);
    if (el) return el;

    // 2. Direct DOM id
    el = document.getElementById(fid);
    if (el) return el;

    // 3. name attribute
    el = document.querySelector(`[name="${esc}"]`);
    if (el) return el;

    // 4. Exact aria-label
    el = document.querySelector(`[aria-label="${esc}"]`);
    if (el && el.matches(INPUT_SEL)) return el;

    // 5. Partial id/name match (React, Angular generated IDs)
    el = document.querySelector(`[id*="${esc}"], [name*="${esc}"]`);
    if (el) return el;

    // For strategies 6-10 we work with a normalised needle
    const needle = slugOf(fid);
    if (!needle) return null;

    // 6. <label> element text matching
    for (const lbl of document.querySelectorAll("label")) {
      if (!slugMatch(lbl.textContent, needle)) continue;
      if (lbl.htmlFor) {
        const t = document.getElementById(lbl.htmlFor);
        if (t) return t;
      }
      const inp = lbl.querySelector(INPUT_SEL);
      if (inp) return inp;
      // Look at next siblings
      let sib = lbl.nextElementSibling;
      for (let i = 0; i < 5 && sib; i++, sib = sib.nextElementSibling) {
        const inp2 = sib.matches?.(INPUT_SEL) ? sib : sib.querySelector(INPUT_SEL);
        if (inp2) return inp2;
      }
    }

    // 7. Question container scan (Google Forms, Typeform, Wufoo, custom forms)
    //    Walk every element that looks like a question wrapper
    const containers = document.querySelectorAll(
      "[role=listitem], [role=group], fieldset, " +
      ".freebirdFormviewerComponentsQuestionBaseRoot, " +  // Google Forms
      ".Qr7Oae, .z12JJ, " +                               // Google Forms inner
      ".question, .form-group, .field-wrapper, .form-field, " +
      ".input-group, .field, .form-row, .control-group, " +
      ".MuiFormControl-root, .v-input, " +                // Material UI, Vuetify
      ".sf-field, .fd-question"                           // Salesforce, custom
    );
    for (const c of containers) {
      // Find the label text for this container
      const headingEl = c.querySelector(
        "div[role=heading], legend, label, " +
        ".freebirdFormviewerComponentsQuestionBaseTitle, " +  // Google Forms title
        ".M7eMe, .HoXoMd, " +                                // Google Forms variants
        "h1,h2,h3,h4,strong,.question-title,.field-label,.control-label,.form-label"
      );
      const txt = headingEl ? headingEl.textContent : c.childNodes[0]?.textContent || "";
      if (!txt.trim() || !slugMatch(txt, needle)) continue;

      const inp = c.querySelector(INPUT_SEL);
      if (inp) return inp;
    }

    // 8. Fuzzy aria-label match
    for (const el of document.querySelectorAll("[aria-label]")) {
      if (!el.matches(INPUT_SEL)) continue;
      const al = el.getAttribute("aria-label");
      if (slugMatch(al, needle) || slugMatch(needle, slugOf(al))) return el;
    }

    // 9. Placeholder match — check both directions so that a short needle like
    // "category" matches a placeholder like "Enter Product Category".
    for (const el of document.querySelectorAll("input[placeholder], textarea[placeholder]")) {
      const ph = el.placeholder;
      if (slugMatch(ph, needle) || slugMatch(needle, slugOf(ph))) return el;
    }

    // 10. Last resort — if only one input on the page matches keyword from needle
    const allInputs = Array.from(document.querySelectorAll(INPUT_SEL)).filter(el => {
      const s = getComputedStyle(el);
      return s.display !== "none" && s.visibility !== "hidden";
    });
    if (allInputs.length === 1) return allInputs[0]; // Only one input — must be it
    const words = needle.split("_").filter(w => w.length > 2);
    const wordMatches = allInputs.filter(el => {
      const combo = slugOf([el.id, el.name, el.placeholder, el.getAttribute("aria-label")].join(" "));
      return words.some(w => combo.includes(w));
    });
    if (wordMatches.length === 1) return wordMatches[0];

    return null;
  }

  // ── Helpers ────────────────────────────────────────────────────────────────
  function highlight(el) {
    if (!el) return;
    const prev = el.getAttribute("style") || "";
    el.setAttribute("style", prev + ";" + HL_STYLE);
    setTimeout(() => el.setAttribute("style", prev), HL_MS);
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function nativeSet(el, value) {
    const proto = el.tagName === "SELECT"   ? window.HTMLSelectElement.prototype
                : el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype
                                            : window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc?.set) desc.set.call(el, value);
    else el.value = value;
  }

  function fireEvents(el) {
    ["focus","input","change","blur"].forEach(ev =>
      el.dispatchEvent(new Event(ev, { bubbles: true, cancelable: true }))
    );
    el.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "insertText" }));
    el.dispatchEvent(new KeyboardEvent("keyup",   { bubbles: true, keyCode: 0 }));
    el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, keyCode: 0 }));
  }

  function typeCharByChar(el, str) {
    el.focus(); nativeSet(el, ""); fireEvents(el);
    for (let i = 0; i < str.length; i++) {
      const ch = str[i], code = ch.charCodeAt(0);
      el.dispatchEvent(new KeyboardEvent("keydown",  { key: ch, keyCode: code, bubbles: true }));
      el.dispatchEvent(new KeyboardEvent("keypress", { key: ch, keyCode: code, bubbles: true }));
      nativeSet(el, str.slice(0, i + 1));
      el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
      el.dispatchEvent(new KeyboardEvent("keyup", { key: ch, keyCode: code, bubbles: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur",   { bubbles: true }));
  }

  // ── Date handling ──────────────────────────────────────────────────────────
  function parseDate(val) {
    if (!val) return null;
    val = String(val).trim();
    if (/^today$/i.test(val)) return new Date();
    if (/^yesterday$/i.test(val)) { const y = new Date(); y.setDate(y.getDate()-1); return y; }
    if (/^tomorrow$/i.test(val)) { const t = new Date(); t.setDate(t.getDate()+1); return t; }
    let d = new Date(val);
    if (!isNaN(d.getTime())) return d;
    // DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY
    const dmy = val.match(/^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{4})$/);
    if (dmy) { d = new Date(+dmy[3], +dmy[2]-1, +dmy[1]); if (!isNaN(d)) return d; }
    // MM/DD/YYYY
    const mdy = val.match(/^(\d{1,2})[-\/](\d{1,2})[-\/](\d{4})$/);
    if (mdy) { d = new Date(+mdy[3], +mdy[1]-1, +mdy[2]); if (!isNaN(d)) return d; }
    // "0703 2026" or "07 03 2026" — Whisper transcribes dates with spaces
    // Treat as DD MM YYYY
    const spaced = val.match(/^(\d{2})(\d{2})\s+(\d{4})$/) ||
                   val.match(/^(\d{1,2})\s+(\d{1,2})\s+(\d{4})$/);
    if (spaced) { d = new Date(+spaced[3], +spaced[2]-1, +spaced[1]); if (!isNaN(d)) return d; }
    // "07032026" — 8 digit no separator DDMMYYYY
    const nosep = val.match(/^(\d{2})(\d{2})(\d{4})$/);
    if (nosep) { d = new Date(+nosep[3], +nosep[2]-1, +nosep[1]); if (!isNaN(d)) return d; }
    // "March 7 2026" / "7 March 2026" / "7th March 2026"
    const wordy = val.replace(/\b(\d+)(st|nd|rd|th)\b/gi, "$1");
    d = new Date(wordy);
    if (!isNaN(d.getTime())) return d;
    return null;
  }
  const pad = n => String(n).padStart(2,"0");
  const toYMD    = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
  const toDMY    = d => `${pad(d.getDate())}/${pad(d.getMonth()+1)}/${d.getFullYear()}`;
  const toDMYd   = d => `${pad(d.getDate())}-${pad(d.getMonth()+1)}-${d.getFullYear()}`;

  function isDateField(el) {
    if (["date","datetime-local","month","week"].includes(el.type)) return true;
    const ph = (el.placeholder||"").toLowerCase();
    if (/dd[-\/]mm[-\/]yyyy|mm[-\/]dd[-\/]yyyy/.test(ph)) return true;
    const nm = (el.name||el.id||"").toLowerCase();
    if (/date|_dt$|_on$|\bdob\b/.test(nm)) return true;
    if (el.classList.contains("flatpickr-input")||el.classList.contains("datepicker")) return true;
    if (el.hasAttribute("data-datepicker")) return true;
    return false;
  }

  function fillDate(el, raw) {
    const d = parseDate(raw);
    if (!d) return { ok: false, reason: `Cannot parse date: "${raw}". Use DD/MM/YYYY` };
    highlight(el);
    if (el.type === "date" || el.type === "datetime-local") {
      nativeSet(el, toYMD(d)); fireEvents(el);
      return { ok: true, field_id: el.id||el.name, value: toYMD(d) };
    }
    if (el._flatpickr) { el._flatpickr.setDate(d, true); return { ok: true, value: toDMY(d) }; }
    if (window.jQuery) {
      try {
        const $el = window.jQuery(el);
        if ($el.data("datepicker")) {
          const fmt = (el.placeholder||"").includes("dd-mm") ? toDMYd(d) : toDMY(d);
          $el.val(fmt).trigger("change"); $el.datepicker?.("update", d);
          return { ok: true, value: fmt };
        }
      } catch(_) {}
    }
    const ph = (el.placeholder||"").toLowerCase();
    const fmt = ph.includes("dd-mm") ? toDMYd(d) : ph.includes("yyyy-mm") ? toYMD(d) : toDMY(d);
    nativeSet(el, fmt); fireEvents(el);
    if (el.value !== fmt) typeCharByChar(el, fmt);
    return { ok: true, field_id: el.id||el.name, value: fmt };
  }

  // ── Custom combobox fill (Vue-select, react-select, etc.) ─────────────────
  function fillCombobox(el, raw) {
    const target = String(raw || "").toLowerCase().trim();
    highlight(el); el.focus();

    // Clear and type the value to trigger dropdown
    nativeSet(el, raw); fireEvents(el);
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", keyCode: 40, bubbles: true }));

    // Give the dropdown 300ms to render, then try to click a matching option
    return new Promise(resolve => {
      setTimeout(() => {
        // Look for dropdown options in common structures
        const selectors = [
          "[role=option]", "[role=listitem]",
          ".vs__dropdown-option", ".v-select-option",
          ".dropdown-item", "[class*='option']",
          "li[data-value]", ".select-option",
        ];
        let options = [];
        for (const sel of selectors) {
          options = Array.from(document.querySelectorAll(sel)).filter(o => {
            const r = o.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          });
          if (options.length) break;
        }

        // Try exact then fuzzy match
        let match = options.find(o => o.textContent.trim().toLowerCase() === target);
        if (!match) match = options.find(o => o.textContent.toLowerCase().includes(target));
        if (!match && target.length > 3) {
          match = options.find(o => target.includes(o.textContent.trim().toLowerCase()));
        }

        if (match) {
          match.click();
          resolve({ ok: true, field_id: el.id || el.name, value: match.textContent.trim() });
        } else {
          // Fallback: press Enter to accept typed value
          el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 13, bubbles: true }));
          const opts_txt = options.slice(0,5).map(o=>o.textContent.trim()).join(", ");
          if (options.length) {
            resolve({ ok: false, reason: `No option matching "${raw}". Options: ${opts_txt}` });
          } else {
            // No dropdown appeared — value may have been accepted as-is (some combos allow free text)
            resolve({ ok: true, field_id: el.id || el.name, value: raw });
          }
        }
      }, 350);
    });
  }

  // ── Select fill ────────────────────────────────────────────────────────────
  function fillSelect(el, raw) {
    if (raw === null || raw === undefined) return { ok: false, reason: "No value for select" };
    const target = String(raw).toLowerCase().trim();
    highlight(el); el.focus();
    const opts = Array.from(el.options);
    const try_ = fn => { const o = opts.find(fn); if (o) { nativeSet(el, o.value); fireEvents(el); return { ok: true, value: o.text.trim() }; } return null; };
    return (
      try_(o => o.value.toLowerCase() === target) ||
      try_(o => o.text.toLowerCase().trim() === target) ||
      try_(o => o.text.toLowerCase().includes(target)) ||
      try_(o => target.includes(o.text.toLowerCase().trim()) && o.text.trim().length > 1) ||
      try_(o => target.split(/\s+/).some(w => w.length > 1 && o.text.toLowerCase().includes(w))) ||
      { ok: false, reason: `No option matching "${raw}". Options: ${opts.map(o=>o.text.trim()).filter(Boolean).join(", ").slice(0,200)}` }
    );
  }

  // ── Google Forms radio/checkbox (custom elements) ──────────────────────────
  function fillGoogleFormsChoice(container, value) {
    const target = value.toLowerCase().trim();
    // Google Forms uses div[role=radio] or div[role=checkbox]
    const choices = container.querySelectorAll("[role=radio],[role=checkbox],[data-value]");
    for (const choice of choices) {
      const txt = (choice.textContent || choice.getAttribute("data-value") || "").toLowerCase().trim();
      if (txt === target || txt.includes(target) || target.includes(txt)) {
        highlight(choice);
        choice.click();
        return { ok: true, value: txt };
      }
    }
    return null;
  }

  // ── Main fill dispatcher ───────────────────────────────────────────────────
  function doFill(action) {
    const { field_id, value } = action;
    if (!field_id) return { ok: false, reason: "Missing field_id" };

    let el = findEl(field_id);

    // Google Forms fallback — find question container and click choice
    if (!el) {
      const needle = slugOf(field_id);
      const containers = document.querySelectorAll("[role=listitem],[role=group],.freebirdFormviewerComponentsQuestionBaseRoot");
      for (const c of containers) {
        const headingEl = c.querySelector("[role=heading],.freebirdFormviewerComponentsQuestionBaseTitle,.M7eMe");
        if (headingEl && slugOf(headingEl.textContent) && 
            needle.split("_").every(w => w.length < 2 || slugOf(headingEl.textContent).includes(w))) {
          const gRes = fillGoogleFormsChoice(c, String(value));
          if (gRes) return gRes;
          el = c.querySelector(INPUT_SEL);
          break;
        }
      }
    }

    // Fallback 1: original DOM id / name stamped by the extractor.
    // Reliable even after a React re-render that wipes data-copilot-field-id.
    if (!el && action.dom_id) {
      el = document.getElementById(action.dom_id) ||
           document.querySelector(`[name="${CSS.escape(action.dom_id)}"]`);
    }

    // Fallback 2: human-readable label forwarded by the backend.
    // Lets strategy 6 (label text match) run against the full label string.
    if (!el && action.label) {
      el = findEl(action.label);
    }

    // Fallback 3: placeholder text forwarded by the backend.
    // Useful when the element lost its stamp and has no matching id/label.
    if (!el && action.placeholder) {
      el = findEl(action.placeholder);
    }

    if (!el) return { ok: false, reason: `Field not found: "${field_id}". Try using the exact label text as field_id.` };
    if (el.disabled) return { ok: false, reason: `Field "${field_id}" is disabled` };

    const tag  = el.tagName.toLowerCase();
    const type = (el.type || "").toLowerCase();

    if (type === "checkbox") {
      const checked = !["false","0","no","off"].includes(String(value).toLowerCase());
      highlight(el); el.checked = checked; fireEvents(el);
      return { ok: true, field_id, value: checked };
    }

    if (type === "radio") {
      const name = el.name;
      const radios = document.querySelectorAll(`input[type="radio"]${name ? `[name="${CSS.escape(name)}"]` : ""}`);
      let found = false;
      radios.forEach(r => {
        const match = r.value.toLowerCase() === String(value).toLowerCase() ||
                      (r.labels?.[0]?.textContent||"").toLowerCase().includes(String(value).toLowerCase()) ||
                      slugOf(r.value) === slugOf(String(value));
        if (match) { highlight(r); r.checked = true; fireEvents(r); found = true; }
      });
      return found ? { ok: true, field_id, value } : { ok: false, reason: `Radio option "${value}" not found` };
    }

    if (tag === "select") return fillSelect(el, value);
    if (isDateField(el))  return fillDate(el, value);

    // ── Custom combobox / autocomplete (Vue-select, react-select, etc.) ──────
    // Detected by role=combobox or aria-autocomplete, or if a real <select>
    // was not found but the element has a dropdown trigger behaviour.
    const isCombo = el.getAttribute("role") === "combobox" ||
                    el.getAttribute("aria-autocomplete") ||
                    el.classList.contains("vs__search") ||
                    el.closest(".v-select, .vue-select, [class*='select-container']");
    if (isCombo || (tag === "input" && el.closest("[class*='select'],[class*='dropdown'],[class*='combo']") )) {
      return fillCombobox(el, value);
    }

    if (tag === "button" || ["submit","button","reset"].includes(type)) {
      highlight(el); el.click();
      return { ok: true, field_id, action: "click" };
    }

    if (el.contentEditable === "true") {
      highlight(el); el.focus();
      el.textContent = String(value ?? ""); fireEvents(el);
      return { ok: true, field_id, value };
    }

    // Standard input / textarea
    highlight(el); el.focus();
    const strVal = value !== null && value !== undefined ? String(value) : "";
    nativeSet(el, strVal); fireEvents(el);
    if (el.value !== strVal && strVal.length > 0) typeCharByChar(el, strVal);
    return { ok: true, field_id, value: strVal };
  }

  // ── Tab / nav switch ───────────────────────────────────────────────────────
  function doTabSwitch(action) {
    const target = (action.value || "").toLowerCase();
    if (!target) return { ok: false, reason: "No tab value" };
    const match = [...document.querySelectorAll("a,button,[role=tab],[role=menuitem],.nav-link,.tab,li")]
      .find(el => el.textContent.trim().toLowerCase() === target ||
                  el.textContent.trim().toLowerCase().includes(target));
    if (match) { highlight(match); match.click(); return { ok: true, label: match.textContent.trim() }; }
    return { ok: false, reason: `Tab "${action.value}" not found` };
  }

  // ── Click / navigate ───────────────────────────────────────────────────────
  function doClick(action) {
    const id = action.field_id || action.button_id;
    let el = id ? findEl(id) : null;
    if (!el && action.label) {
      el = [...document.querySelectorAll("button,a,[role=button],input[type=submit]")]
        .find(b => b.textContent.trim().toLowerCase() === (action.label||"").toLowerCase());
    }
    if (!el) return { ok: false, reason: `Button/link not found: ${id || action.label}` };
    highlight(el);
    if (el.href && !el.href.startsWith("javascript")) window.location.href = el.href;
    else el.click();
    return { ok: true, action: "click", field_id: id };
  }

  function mk(tag, props) {
    const el = document.createElement(tag);
    Object.entries(props).forEach(([k, v]) => {
      if (k === "style") el.style.cssText = v; else el[k] = v;
    });
    return el;
  }

})();