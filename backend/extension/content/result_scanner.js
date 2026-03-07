/**
 * result_scanner.js — Post-action DOM readback for GoPilot CoPilot
 *
 * Runs AFTER executeAction() completes. Reads back what actually happened
 * so the TTS confirmation is grounded in DOM reality, not just the planned action.
 *
 * Three scan types:
 *   1. fill_readback  — reads the actual value now in the field
 *   2. submit_result  — scans for toast/alert/success messages after submit
 *   3. nav_readback   — reads the new page title + field count after navigation
 *
 * Called from popup.js after executeAction() resolves.
 * Returns a plain object: { type, spoken, detail }
 */

(function () {

  // ── Toast / success message selectors ─────────────────────────────────────
  // Covers Bootstrap toasts, SweetAlert, custom alert divs, Django messages,
  // and the specific patterns seen in the Umbrella ERP screenshots
  const TOAST_SELECTORS = [
    // Bootstrap 4/5
    ".toast:not(.hide)",
    ".toast-body",
    // SweetAlert / SweetAlert2
    ".swal2-popup:not(.swal2-hidden)",
    ".swal2-title",
    ".swal2-content",
    ".swal2-html-container",
    // Generic success/error divs (very common in Django/Vue/React ERPs)
    "[class*='success']:not(button):not(input):not(a)",
    "[class*='alert-success']",
    "[class*='alert-danger']",
    "[class*='alert-warning']",
    "[class*='notification']",
    "[role='alert']",
    "[role='status']",
    // Snackbars (Material UI, Vuetify)
    ".snackbar",
    ".v-snack__content",
    ".MuiSnackbar-root",
    // Custom ERP patterns
    "[class*='toast']",
    "[class*='message'][class*='success']",
    "[class*='message'][class*='error']",
    ".flash-message",
    ".flash",
    // Modal result dialogs
    ".modal.show .modal-body",
    ".modal.show .modal-title",
  ].join(", ");

  // Words that indicate a SUCCESS message
  const SUCCESS_WORDS = /\b(success|created|saved|added|submitted|done|complete|recorded|updated|inserted|invoice|order|generated)\b/i;

  // Words that indicate an ERROR message
  const ERROR_WORDS = /\b(error|failed|invalid|required|cannot|could not|duplicate|already exists|warning|please|must)\b/i;

  /**
   * Scan DOM for toast / alert messages.
   * Returns { text, sentiment } or null if nothing visible found.
   */
  function scanToasts() {
    const candidates = [];
    try {
      document.querySelectorAll(TOAST_SELECTORS).forEach(el => {
        if (!el.offsetParent && el.style.display === "none") return;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) return;
        const text = el.textContent.trim().replace(/\s+/g, " ").slice(0, 300);
        if (text.length < 4) return;
        candidates.push({ el, text, rect });
      });
    } catch (e) {}

    if (!candidates.length) return null;

    // Prefer smaller, newer elements (toasts) over large body divs
    candidates.sort((a, b) => {
      const aScore = (a.rect.width * a.rect.height);
      const bScore = (b.rect.width * b.rect.height);
      return aScore - bScore;
    });

    const best = candidates[0];
    const text = best.text;
    const sentiment = SUCCESS_WORDS.test(text) ? "success"
                    : ERROR_WORDS.test(text)   ? "error"
                    : "neutral";
    return { text, sentiment };
  }

  /**
   * Read the actual current value of a field after it was filled.
   * Returns the value string or null.
   */
  function readFieldValue(fieldId) {
    if (!fieldId) return null;
    try {
      // Try by data attribute (set by extractor)
      let el = document.querySelector(`[data-copilot-field-id="${fieldId}"]`);
      if (!el) {
        // Try by id / name
        el = document.getElementById(fieldId)
          || document.querySelector(`[name="${fieldId}"]`);
      }
      if (!el) return null;

      if (el.tagName === "SELECT") {
        const opt = el.options[el.selectedIndex];
        return opt ? opt.text.trim() : el.value;
      }
      if (el.type === "checkbox") return el.checked ? "checked" : "unchecked";
      if (el.type === "radio") {
        const name = el.name;
        const checked = document.querySelector(`input[name="${name}"]:checked`);
        return checked ? (checked.labels?.[0]?.textContent || checked.value) : null;
      }
      return (el.value || el.textContent || "").trim().slice(0, 120) || null;
    } catch (e) {
      return null;
    }
  }

  /**
   * Get a human-readable label for a field by stripping Marathi/Devanagari.
   */
  function cleanLabel(rawLabel) {
    if (!rawLabel) return "";
    return rawLabel
      .replace(/\([^)]*\)/g, "")       // strip (मराठी)
      .replace(/[\u0900-\u0D7F]+/g, "") // strip Devanagari
      .replace(/[*†:]+/g, "")
      .trim();
  }

  /**
   * Count fillable fields on the current page.
   */
  function countFillableFields() {
    const sel = [
      'input:not([type="hidden"]):not([type="submit"]):not([type="button"])',
      'select', 'textarea', '[contenteditable="true"]',
    ].join(", ");
    let total = 0, filled = 0;
    document.querySelectorAll(sel).forEach(el => {
      if (!el.offsetParent) return;
      total++;
      const v = (el.value || el.textContent || "").trim();
      if (v && v !== "0" && !/^(select|choose)/i.test(v)) filled++;
    });
    return { total, filled };
  }

  // ── DOM validation error scanner ──────────────────────────────────────────

  // Selectors for inline validation error messages (framework-agnostic)
  const INLINE_ERROR_SELECTORS = [
    // Bootstrap 4/5
    ".invalid-feedback",
    ".invalid-tooltip",
    // Material UI / MUI
    ".MuiFormHelperText-root.Mui-error",
    // Vuetify
    ".v-messages__message",
    ".error--text .v-messages__message",
    // Generic patterns used in ERPs
    "[class*='error-message']",
    "[class*='field-error']",
    "[class*='form-error']",
    "[class*='validation-error']",
    "[class*='help-block']",
    // aria-describedby targets
    "[id*='error']",
    "[id*='feedback']",
    "[id*='help']",
    // Django / generic
    ".errorlist li",
    ".field-error",
    ".form-text.text-danger",
    ".text-danger:not(button)",
  ].join(", ");

  /**
   * Scan for inline validation errors near a specific field.
   * Looks at: the field's parent container, aria-describedby, nearby siblings.
   */
  function scanFieldError(fieldId) {
    if (!fieldId) return null;

    try {
      // Find the field element
      let el = document.querySelector(`[data-copilot-field-id="${fieldId}"]`)
            || document.getElementById(fieldId)
            || document.querySelector(`[name="${fieldId}"]`);

      if (!el) return null;

      // Check aria-invalid first — most reliable
      const isInvalid = el.getAttribute("aria-invalid") === "true"
                     || el.classList.contains("is-invalid")
                     || el.classList.contains("error")
                     || el.classList.contains("ng-invalid");

      // Look for error message in:
      // 1. aria-describedby (most accessible)
      const describedBy = el.getAttribute("aria-describedby");
      if (describedBy) {
        for (const id of describedBy.split(/\s+/)) {
          const desc = document.getElementById(id);
          if (desc) {
            const text = desc.textContent.trim();
            if (text && text.length > 2) {
              return { text, source: "aria-describedby", is_error: isInvalid };
            }
          }
        }
      }

      // 2. Parent container's .invalid-feedback / error children
      const container = el.closest(
        ".form-group, .form-field, .field-wrapper, .input-group, " +
        ".form-control-wrapper, [class*='field'], [class*='input-wrap']"
      ) || el.parentElement;

      if (container) {
        const errEl = container.querySelector(INLINE_ERROR_SELECTORS);
        if (errEl) {
          const text = errEl.textContent.trim();
          if (text && text.length > 2) {
            return { text, source: "container", is_error: true };
          }
        }
      }

      // 3. Next sibling error element
      let sib = el.nextElementSibling;
      let hops = 0;
      while (sib && hops < 3) {
        if (sib.matches(INLINE_ERROR_SELECTORS)) {
          const text = sib.textContent.trim();
          if (text && text.length > 2) {
            return { text, source: "sibling", is_error: true };
          }
        }
        sib = sib.nextElementSibling;
        hops++;
      }

      // 4. If field is marked invalid but no message found — generic
      if (isInvalid) {
        return { text: "This field has an error.", source: "aria-invalid", is_error: true };
      }

    } catch (e) {}
    return null;
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Readback after a field fill action.
   * @param {string} fieldId   - The field that was just filled
   * @param {string} rawLabel  - The field's label from the action
   * @param {string} planned   - The value we planned to set
   * @returns {{ spoken: string, actual_value: string|null }}
   */
  window.__copilotReadbackFill = function(fieldId, rawLabel, planned) {
    const actual   = readFieldValue(fieldId);
    const label    = cleanLabel(rawLabel) || fieldId.replace(/_/g, " ");
    const domError = scanFieldError(fieldId);

    // DOM validation error detected after fill
    if (domError && domError.is_error) {
      return {
        spoken:       `There's a problem with ${label}: ${domError.text}`,
        actual_value: actual || planned,
        dom_error:    domError.text,
        has_error:    true,
      };
    }

    if (!actual || actual === planned) {
      return {
        spoken:       `Done — ${label} is set to ${planned || actual || "the value"}.`,
        actual_value: actual || planned,
        has_error:    false,
      };
    }

    // Actual value differs from planned (dropdown snapped to closest match)
    return {
      spoken:       `Set — ${label} is now ${actual}.`,
      actual_value: actual,
      has_error:    false,
    };
  };

  /**
   * Readback after a submit action — scans for toast/success messages.
   * Call this 600ms after submit to allow server response to render.
   * @returns {{ spoken: string, sentiment: string }}
   */
  window.__copilotReadbackSubmit = function() {
    const toast = scanToasts();

    if (toast) {
      if (toast.sentiment === "success") {
        // Try to extract key info (invoice numbers, IDs) from the toast
        const numMatch = toast.text.match(/\b([A-Z]{2,}\d{4,}|\d{5,})\b/);
        const numStr   = numMatch ? ` — reference ${numMatch[1]}` : "";
        return {
          spoken:    `Submitted successfully${numStr}. ${toast.text.slice(0, 120)}`,
          sentiment: "success",
          raw:       toast.text,
        };
      }
      if (toast.sentiment === "error") {
        return {
          spoken:    `There was a problem: ${toast.text.slice(0, 150)} Please check and try again.`,
          sentiment: "error",
          raw:       toast.text,
        };
      }
      return {
        spoken:    toast.text.slice(0, 150),
        sentiment: "neutral",
        raw:       toast.text,
      };
    }

    // No toast found — check if form was cleared (fields reset = successful submit)
    const { total, filled } = countFillableFields();
    if (total > 3 && filled <= 1) {
      return {
        spoken:    "Submitted — the form has been cleared, which usually means it went through.",
        sentiment: "success",
        raw:       null,
      };
    }

    return {
      spoken:    "Submit sent. I didn't see a confirmation message — check the page to verify.",
      sentiment: "unknown",
      raw:       null,
    };
  };

  /**
   * Readback after navigation — page title + field summary.
   * @returns {{ spoken: string }}
   */
  window.__copilotReadbackNav = function() {
    const title = document.title || "";
    const { total, filled } = countFillableFields();

    // Clean up common ERP title suffixes
    const cleanTitle = title
      .replace(/[-|–]\s*(umbrella|inventory|stock|app|system).*/i, "")
      .trim();

    if (total === 0) {
      return { spoken: `Opened ${cleanTitle}. No fillable fields here.` };
    }

    const remaining = total - filled;
    if (remaining > 0) {
      return {
        spoken: `Opened ${cleanTitle}. There are ${total} fields, ${remaining} still empty. Say 'fill required fields' to get started.`,
      };
    }
    return {
      spoken: `Opened ${cleanTitle}. ${total} fields are already filled.`,
    };
  };

})();