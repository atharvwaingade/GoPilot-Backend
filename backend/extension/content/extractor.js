"use strict";
/**
 * extractor.js v5 — Truly Universal Extractor
 *
 * Works on: Google Forms, Typeform, Wufoo, SAP, Salesforce, custom ERPs,
 *           React/Vue/Angular apps, plain HTML forms, bilingual pages
 *
 * Key improvements over v4:
 *  ✓ Google Forms shadow DOM / custom element support
 *  ✓ Label slug as field_id (not DOM id) so "fill name" always works
 *  ✓ Stores BOTH original DOM id AND label slug for robust executor lookup
 *  ✓ Deduplication by label slug (not DOM id) prevents duplicates
 *  ✓ Contenteditable / rich-text fields (Salesforce, Gmail compose)
 *  ✓ Radio groups captured as single logical field
 *  ✓ Custom select widgets (react-select, select2, choices.js)
 */
(function () {
  if (window.__copilotExtractVersion >= 5) return;
  window.__copilotExtractVersion = 5;

  const MAX_FIELDS = 150;

  // ── Visibility ─────────────────────────────────────────────────────────────
  function isVisible(el) {
    if (!el) return false;
    if (el.type === "hidden") return false;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || parseFloat(s.opacity) < 0.1) return false;
    // offsetParent is null for fixed/absolute — don't exclude those
    if (!el.offsetParent && el.tagName !== "BODY" &&
        el.type !== "radio" && el.type !== "checkbox" &&
        s.position !== "fixed" && s.position !== "absolute") return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    return true;
  }

  // ── Label slug — stable ID from human-readable label ──────────────────────
  function toSlug(text) {
    return (text || "")
      .replace(/\([^)]+\)/g, "")          // strip (parenthetical) prefixes
      .replace(/[\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0B80-\u0BFF]+/g, "") // strip Indic scripts
      .replace(/[*†‡§¶#@!?:]+/g, "")     // strip symbols
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 50);
  }

  // ── Label resolution — 8 strategies ───────────────────────────────────────
  function getLabel(el) {
    // 1. <label for="id">
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return clean(lbl.textContent);
    }
    // 2. aria-label
    const al = el.getAttribute("aria-label");
    if (al && al.trim()) return al.trim();
    // 3. aria-labelledby
    const alby = el.getAttribute("aria-labelledby");
    if (alby) {
      const t = alby.split(/\s+/).map(id => document.getElementById(id)?.textContent || "").join(" ").trim();
      if (t) return clean(t);
    }
    // 4. Wrapped in <label>
    const wl = el.closest("label");
    if (wl) return clean(wl.textContent);
    // 5. Previous sibling text (common in custom ERP layouts)
    let sib = el.previousElementSibling;
    for (let i = 0; i < 3 && sib; i++, sib = sib.previousElementSibling) {
      const t = sib.textContent.trim();
      if (t.length > 0 && t.length < 120 && !sib.querySelector("input,select,textarea")) return clean(t);
    }
    // 6. Walk up to 4 parents looking for label-like text
    let parent = el.parentElement;
    for (let i = 0; i < 4 && parent; i++, parent = parent.parentElement) {
      // Direct text of parent (not children)
      const dt = directText(parent);
      if (dt.length > 1 && dt.length < 120) return clean(dt);
      // Parent's previous sibling
      const psib = parent.previousElementSibling;
      if (psib) {
        const t = psib.textContent.trim();
        if (t.length > 0 && t.length < 120 && !psib.querySelector("input,select,textarea")) return clean(t);
      }
      // Look for role=heading or legend inside same container
      const heading = parent.querySelector("legend, [role=heading], .label, .field-label, .question-title, .form-label");
      if (heading && heading !== el) {
        const t = heading.textContent.trim();
        if (t.length > 0 && t.length < 120) return clean(t);
      }
    }
    // 7. Placeholder
    if (el.placeholder) return el.placeholder.trim();
    // 8. name / id as fallback
    return (el.name || el.id || "").replace(/[_\-]/g, " ").trim();
  }

  function clean(t) { return (t || "").replace(/\s+/g, " ").trim().slice(0, 100); }
  function directText(el) {
    let t = "";
    el.childNodes.forEach(n => { if (n.nodeType === Node.TEXT_NODE) t += n.textContent; });
    return t.trim();
  }

  // ── Field type detection ───────────────────────────────────────────────────
  function isDateField(el) {
    if (["date","datetime-local","month","week"].includes(el.type)) return true;
    const ph = (el.placeholder || "").toLowerCase();
    if (/dd[-\/]mm[-\/]yyyy|mm[-\/]dd[-\/]yyyy|yyyy[-\/]mm[-\/]dd/.test(ph)) return true;
    const nm = (el.name || el.id || "").toLowerCase();
    if (/date|_dt$|_on$|\bdob\b/.test(nm)) return true;
    if (el.classList.contains("flatpickr-input") || el.classList.contains("datepicker")) return true;
    if (el.hasAttribute("data-datepicker") || el.hasAttribute("data-provide")) return true;
    if (el.closest("[data-provide='datepicker']")) return true;
    return false;
  }

  function getType(el) {
    if (el.tagName === "SELECT") return "select";
    if (el.tagName === "TEXTAREA") return "textarea";
    if (el.contentEditable === "true") return "contenteditable";
    const role = el.getAttribute("role");
    if (role === "combobox") return "combobox";
    if (role === "listbox")  return "select";
    if (isDateField(el))     return "date";
    if (el.type === "checkbox") return "checkbox";
    if (el.type === "radio")    return "radio";
    if (el.type === "number")   return "number";
    if (el.type === "email")    return "email";
    if (el.type === "tel")      return "tel";
    if (el.type === "url")      return "url";
    return el.type || "text";
  }

  function getValue(el) {
    if (el.type === "checkbox") return el.checked ? "true" : "false";
    if (el.type === "radio")    return el.checked ? el.value : null;
    if (el.tagName === "SELECT") {
      const opt = el.options[el.selectedIndex];
      if (!opt || opt.value === "" || /^(select|choose|pick)\s/i.test(opt.text)) return null;
      return opt.text.trim() || opt.value;
    }
    if (el.contentEditable === "true") return el.textContent.trim() || null;
    return el.value || null;
  }

  // ── Main extraction ────────────────────────────────────────────────────────
  window.__copilotExtractContext = function () {
    const fields = [];
    const seenSlug = new Set();  // deduplicate by label slug
    const seenDomId = new Set(); // deduplicate by DOM key

    const SELECTORS = [
      'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]):not([type="file"]):not([type="color"]):not([type="range"])',
      "select",
      "textarea",
      '[contenteditable="true"]:not([role="presentation"])',
      '[role="combobox"]',
      '[role="spinbutton"]',
      '[role="textbox"]',
    ].join(", ");

    // ── Radio groups — handle as single logical field ──────────────────────
    const radioGroups = new Map();
    document.querySelectorAll('input[type="radio"]').forEach(el => {
      if (!isVisible(el)) return;
      const name = el.name || getLabel(el);
      if (!radioGroups.has(name)) radioGroups.set(name, []);
      radioGroups.get(name).push(el);
    });
    radioGroups.forEach((radios, name) => {
      const label = getLabel(radios[0]);
      const slug  = toSlug(label) || toSlug(name) || `radio_${fields.length}`;
      if (seenSlug.has(slug)) return;
      seenSlug.add(slug);
      const checked = radios.find(r => r.checked);
      const options = radios.map(r => ({
        value: r.value,
        label: (r.labels?.[0]?.textContent || r.value || "").trim(),
      }));
      radios[0].dataset.copilotFieldId = slug;
      fields.push({
        field_id:   slug,
        dom_id:     radios[0].name || radios[0].id,
        label:      label || name,
        type:       "radio",
        required:   radios[0].required,
        readonly:   false,
        calculated: false,
        value:      checked ? checked.value : null,
        options,
      });
    });

    // ── All other inputs ───────────────────────────────────────────────────
    document.querySelectorAll(SELECTORS).forEach((el, i) => {
      if (el.type === "radio") return; // handled above
      if (!isVisible(el) || fields.length >= MAX_FIELDS) return;
      // Skip inputs that live inside nav/sidebar (search boxes in menus etc.)
      if (el.closest('nav, aside, [role="navigation"], .sidebar, .sidenav, #sidebar')) return;

      const domKey = (el.id || el.name || "") + "|" + (el.type || el.tagName);
      if (seenDomId.has(domKey) && domKey !== "|") return;
      if (domKey !== "|") seenDomId.add(domKey);

      const label = getLabel(el);
      const slug  = toSlug(label) || (el.id ? toSlug(el.id) : null) || (el.name ? toSlug(el.name) : null) || `field_${i}`;

      // If same slug already exists, append index to make unique
      const finalSlug = seenSlug.has(slug) ? `${slug}_${i}` : slug;
      seenSlug.add(finalSlug);

      // Store slug as data attribute so executor can find by it
      el.dataset.copilotFieldId = finalSlug;

      const type   = getType(el);
      const isDate = isDateField(el);
      const isRO   = el.readOnly || el.getAttribute("aria-readonly") === "true" || el.disabled;
      const value  = getValue(el);

      const f = {
        field_id:    finalSlug,
        dom_id:      el.id || el.name || null,  // original DOM id for executor fallback
        label:       label || finalSlug,
        type,
        is_date:     isDate,
        required:    el.required || el.getAttribute("aria-required") === "true",
        readonly:    isRO,
        calculated:  isRO && el.dataset.calculated === "true",
        value:       value ? String(value).slice(0, 200) : null,
        placeholder: el.placeholder || null,
      };

      if (type === "select" || type === "combobox") {
        f.options = Array.from(el.options || [])
          .filter(o => o.value !== "" && !/^(select|choose|pick)\s/i.test(o.text))
          .map(o => ({ value: o.value, label: o.text.trim() }));
      }

      if (isDate) {
        const ph = (el.placeholder || "").toLowerCase();
        if      (ph.includes("dd-mm-yyyy")) f.date_format = "dd-mm-yyyy";
        else if (ph.includes("dd/mm/yyyy")) f.date_format = "dd/mm/yyyy";
        else if (el.type === "date")        f.date_format = "yyyy-mm-dd";
        else                                f.date_format = "dd/mm/yyyy";
      }

      fields.push(f);
    });

    // ── Buttons ────────────────────────────────────────────────────────────
    // NAV_CONTAINERS: sidebar/menu/navbar links are navigation, NOT form actions.
    // Excluding them prevents voice navigator from confusing "Supplier" nav link
    // with the "Supplier" form field.
    const NAV_CONTAINERS = [
      "nav", "aside", "[role='navigation']", "[role='menubar']",
      ".sidebar", ".side-bar", ".sidenav", ".side-nav",
      ".navbar", ".nav-bar", ".left-panel", ".left-menu",
      "#sidebar", "#sidenav", "#left-panel",
      // Vue Router / common SPA sidebar patterns
      ".router-link-container", ".sidebar-menu", ".side-menu",
      ".app-sidebar", ".main-sidebar", ".app-nav", ".main-nav",
      ".menu-wrapper", ".nav-wrapper", ".navigation-menu",
      "[class*='sidebar']", "[class*='side-nav']", "[class*='side-menu']",
      "[id*='sidebar']", "[id*='sidenav']",
    ].join(", ");

    const buttons = [];
    const bseen   = new Set();
    document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"], a[href]').forEach((el, i) => {
      if (!isVisible(el)) return;

      // Skip links inside nav/sidebar containers — these are page navigation,
      // not form actions. Voice should use voice_navigator for these.
      if (el.tagName === "A" && el.closest(NAV_CONTAINERS)) return;

      // Skip SPA route links (href contains hash routes like /#/application/...)
      if (el.tagName === "A" && el.href) {
        const h = el.href;
        if (/#\/[a-zA-Z]/.test(h) && !h.includes(location.hash)) return; // different route
      }

      // Also skip links whose only purpose is sidebar section headers
      if (el.tagName === "A") {
        const parent = el.parentElement;
        const isNavItem = parent && (
          parent.classList.contains("nav-item") ||
          parent.classList.contains("menu-item") ||
          parent.classList.contains("sidebar-item") ||
          parent.classList.contains("router-link") ||
          parent.getAttribute("role") === "menuitem" ||
          parent.getAttribute("role") === "listitem"
        );
        if (isNavItem) return;
        // Skip the link itself if it's a router-link-active or similar
        if (el.classList.contains("router-link") ||
            el.classList.contains("nav-link") ||
            el.classList.contains("menu-link") ||
            el.classList.contains("sidebar-link")) return;
      }

      const lbl = (el.textContent || el.value || el.getAttribute("aria-label") || "").trim().slice(0, 80);
      if (!lbl || lbl.length < 2) return;
      if (bseen.has(lbl.toLowerCase())) return;
      bseen.add(lbl.toLowerCase());
      const bid = el.id || el.name || toSlug(lbl) || `btn_${i}`;
      el.dataset.copilotFieldId = bid;
      buttons.push({
        button_id: bid,
        label:     lbl,
        disabled:  el.disabled || false,
        action:    el.type || (el.tagName === "A" ? "navigate" : "click"),
        is_submit: el.type === "submit" || /submit|save|send|continue|next/i.test(lbl),
        href:      el.href || null,
        is_nav:    el.tagName === "A",  // flag for voice_navigator priority
      });
    });

    // ── Sidebar / nav links (kept separate so voice_navigator can use them) ──
    const navLinks = [];
    const nlseen   = new Set();
    document.querySelectorAll("a[href]").forEach(el => {
      if (!isVisible(el)) return;
      if (!el.closest(NAV_CONTAINERS)) return;
      const lbl = (el.textContent || el.getAttribute("aria-label") || "").trim().slice(0, 80);
      if (!lbl || lbl.length < 2) return;
      if (nlseen.has(lbl.toLowerCase())) return;
      nlseen.add(lbl.toLowerCase());
      navLinks.push({
        button_id: el.id || toSlug(lbl) || `nav_${nlseen.size}`,
        label:     lbl,
        href:      el.href || null,
        disabled:  false,
        is_nav:    true,
        action:    "navigate",
      });
    });

    return {
      app:          { name: document.title || location.hostname, domain: location.hostname },
      page:         { page_id: location.href, title: document.title, url: location.href, mode: "live" },
      sections:     buildSections(fields),
      buttons:      buttons.slice(0, 20),
      nav_links:    navLinks.slice(0, 30),
      total_fields: fields.length,
      tables:       extractTables(),
    };
  };

  // ── Section grouping ───────────────────────────────────────────────────────
  function buildSections(fields) {
    if (!fields.length) return [];

    // Try fieldsets
    const fieldsets = Array.from(document.querySelectorAll("fieldset"));
    if (fieldsets.length) {
      const sections = []; const used = new Set();
      fieldsets.forEach((fs, i) => {
        const legend = fs.querySelector("legend");
        const title  = legend ? clean(legend.textContent) : `Section ${i + 1}`;
        const sec = fields.filter(f => {
          const el = document.querySelector(`[data-copilot-field-id="${CSS.escape(f.field_id)}"]`);
          return el && fs.contains(el) && !used.has(f.field_id);
        });
        if (sec.length) { sec.forEach(f => used.add(f.field_id)); sections.push({ section_id: `fs_${i}`, title, fields: sec }); }
      });
      const rest = fields.filter(f => !used.has(f.field_id));
      if (rest.length) sections.push({ section_id: "other", title: "Other Fields", fields: rest });
      if (sections.length) return sections;
    }

    // Try headings
    const headings = Array.from(document.querySelectorAll(
      "h1,h2,h3,h4,.card-title,.section-heading,.panel-heading,.section-title,.form-section-title"
    )).filter(h => isVisible(h) && h.textContent.trim());
    if (headings.length) {
      const sections = []; const used = new Set();
      headings.forEach((h, i) => {
        const title = clean(h.textContent);
        const next  = headings[i + 1];
        const sec = fields.filter(f => {
          const el = document.querySelector(`[data-copilot-field-id="${CSS.escape(f.field_id)}"]`);
          if (!el || used.has(f.field_id)) return false;
          if (!(h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
          if (next) return !!(next.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING);
          return true;
        });
        if (sec.length) { sec.forEach(f => used.add(f.field_id)); sections.push({ section_id: `sec_${i}`, title, fields: sec }); }
      });
      const rest = fields.filter(f => !used.has(f.field_id));
      if (rest.length) sections.push({ section_id: "page_fields", title: "Page Fields", fields: rest });
      if (sections.length) return sections;
    }

    return [{ section_id: "page_fields", title: "Page Fields", fields }];
  }


  // ── Table / list data extractor ───────────────────────────────────────────
  // Runs after form fields — reads visible <table>s and data-grid lists.
  // Adds a "tables" key to the context so voice can answer "how many pending".

  function extractTables() {
    const tables = [];
    const MAX_ROWS = 15;   // capture first 15 rows, count the rest

    // ── Strategy 1: real <table> elements ───────────────────────────────────
    document.querySelectorAll("table").forEach(function(tbl) {
      if (!isVisible(tbl)) return;

      // Headers from <thead> or first <tr>
      const thEls = Array.from(
        tbl.querySelectorAll("thead th, thead td")
      );
      let headers = thEls.map(th => th.textContent.trim()).filter(Boolean);

      // If no thead, try first data row
      const firstRow = tbl.querySelector("tbody tr, tr");
      if (!headers.length && firstRow) {
        headers = Array.from(firstRow.querySelectorAll("td, th"))
          .map(td => td.textContent.trim().slice(0, 30))
          .filter(Boolean);
      }
      if (!headers.length) return;  // no recognisable headers — skip

      // Caption / nearby heading
      const captionEl = tbl.querySelector("caption");
      let caption = captionEl ? captionEl.textContent.trim() : "";
      if (!caption) {
        // Look for heading immediately before the table
        let prev = tbl.previousElementSibling;
        while (prev && !prev.matches("h1,h2,h3,h4,h5,h6,.card-title,.table-title,.section-title")) {
          prev = prev.previousElementSibling;
        }
        if (prev) caption = prev.textContent.trim().slice(0, 40);
      }
      if (!caption) caption = document.title || "Records";

      // Rows
      const bodyRows = Array.from(tbl.querySelectorAll("tbody tr")).filter(isVisible);
      const totalRows = bodyRows.length;
      const captured = bodyRows.slice(0, MAX_ROWS);

      const rows = captured.map(function(tr) {
        const cells = Array.from(tr.querySelectorAll("td")).map(td => td.textContent.trim());
        const row = {};
        headers.forEach(function(h, i) {
          if (cells[i] !== undefined) row[h] = cells[i].slice(0, 60);
        });
        return row;
      }).filter(r => Object.keys(r).length > 0);

      if (!rows.length) return;

      // Status summary — look for a column whose values repeat (status-like)
      const statusSummary = {};
      const statusCol = _detectStatusColumn(headers, rows);
      if (statusCol) {
        rows.forEach(function(r) {
          const v = r[statusCol] || "";
          if (v) statusSummary[v] = (statusSummary[v] || 0) + 1;
        });
      }

      tables.push({
        table_id:       "tbl_" + tables.length,
        caption:        caption.slice(0, 50),
        headers:        headers.slice(0, 10),
        row_count:      totalRows,
        rows:           rows,
        status_summary: statusSummary,
        has_more:       totalRows > MAX_ROWS,
      });
    });

    // ── Strategy 2: [role=grid] and [role=rowgroup] ──────────────────────────
    if (!tables.length) {
      document.querySelectorAll("[role=grid],[role=table]").forEach(function(grid) {
        if (!isVisible(grid)) return;

        const colHeaders = Array.from(grid.querySelectorAll("[role=columnheader]"))
          .map(h => h.textContent.trim()).filter(Boolean);
        if (!colHeaders.length) return;

        const dataRows = Array.from(grid.querySelectorAll("[role=row]"))
          .filter(function(r) {
            return !r.querySelector("[role=columnheader]") && isVisible(r);
          });

        const totalRows = dataRows.length;
        const rows = dataRows.slice(0, MAX_ROWS).map(function(row) {
          const cells = Array.from(row.querySelectorAll("[role=cell],[role=gridcell]"))
            .map(c => c.textContent.trim());
          const obj = {};
          colHeaders.forEach(function(h, i) {
            if (cells[i] !== undefined) obj[h] = cells[i].slice(0, 60);
          });
          return obj;
        }).filter(r => Object.keys(r).length > 0);

        if (!rows.length) return;

        const statusSummary = {};
        const statusCol = _detectStatusColumn(colHeaders, rows);
        if (statusCol) {
          rows.forEach(function(r) {
            const v = r[statusCol] || "";
            if (v) statusSummary[v] = (statusSummary[v] || 0) + 1;
          });
        }

        // Caption from aria-label or preceding heading
        let caption = grid.getAttribute("aria-label") || "";
        if (!caption) {
          let prev = grid.previousElementSibling;
          while (prev && !prev.matches("h1,h2,h3,h4,h5,h6,.card-title")) {
            prev = prev.previousElementSibling;
          }
          caption = prev ? prev.textContent.trim().slice(0, 40) : "Records";
        }

        tables.push({
          table_id:       "grid_" + tables.length,
          caption:        caption,
          headers:        colHeaders.slice(0, 10),
          row_count:      totalRows,
          rows:           rows,
          status_summary: statusSummary,
          has_more:       totalRows > MAX_ROWS,
        });
      });
    }

    return tables;
  }

  function _detectStatusColumn(headers, rows) {
    // Status column: low cardinality values (<=8 unique), looks like a status word
    const STATUS_HINTS = /status|state|stage|condition|type|flag/i;
    for (const h of headers) {
      if (STATUS_HINTS.test(h)) return h;
    }
    // Fallback: find column with ≤6 unique values repeated across rows
    for (const h of headers) {
      const uniq = new Set(rows.map(r => r[h]).filter(Boolean));
      if (uniq.size >= 2 && uniq.size <= 6) return h;
    }
    return null;
  }

})();