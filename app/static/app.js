/* Kitchen Inventory Kiosk – vanilla JS, no build step. */
(() => {
  "use strict";

  const NEVER = "2999-12-31";
  const state = {
    mode: "consume",
    config: { consume_countdown_seconds: 5, barcode_prefix: "grcy:s:", default_best_before_days: 7, ui_idle_return_seconds: 300 },
    catalog: { categories: [], groups: [], products: [], locations: [] },
    inventory: [],
    search: { major: null, sub: null, location: null, text: "" },
    add: { major: null, sub: null, product: null, expiry: null, qty: 1, labelMode: "individual", printMode: "print", lastResult: null },
    consume: { current: null, timer: null, endsAt: 0, lastShown: null },
    modalScanHandler: null,
  };

  // ------------------------------------------------------------------ utils
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function isoDate(d) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }
  function addDays(days) { const d = new Date(); d.setDate(d.getDate() + days); return isoDate(d); }
  function addMonths(months) {
    const d = new Date(); const day = d.getDate();
    d.setDate(1); d.setMonth(d.getMonth() + months);
    const last = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
    d.setDate(Math.min(day, last));
    return isoDate(d);
  }
  function daysUntil(dateStr) {
    if (!dateStr || dateStr.startsWith(NEVER)) return null;
    const [y, m, d] = dateStr.slice(0, 10).split("-").map(Number);
    const target = new Date(y, m - 1, d);
    const today = new Date(); today.setHours(0, 0, 0, 0);
    return Math.round((target - today) / 86400000);
  }
  function fmtExpiry(dateStr) {
    if (!dateStr || dateStr.startsWith(NEVER)) return "No expiry";
    const n = daysUntil(dateStr);
    const rel = n === 0 ? "today" : n === 1 ? "tomorrow" : n < 0 ? `${-n} day${n === -1 ? "" : "s"} ago` : `in ${n} days`;
    return `${dateStr.slice(0, 10)} (${rel})`;
  }
  function expiryClass(dateStr) {
    const n = daysUntil(dateStr);
    if (n === null) return "exp-ok";
    if (n < 0) return "exp-expired";
    if (n <= 3) return "exp-soon";
    return "exp-ok";
  }
  function fmtAmount(n) { return Number.isInteger(n) ? String(n) : n.toFixed(2).replace(/\.?0+$/, ""); }

  async function api(method, path, body) {
    let res;
    try {
      res = await fetch(path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      const err = new Error("Network error – is the kiosk server running?");
      err.status = 0;
      throw err;
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) {
      let detail = data && data.detail;
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join(", ");
      const err = new Error(detail || `HTTP ${res.status}`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function toast(message, kind = "info", opts = {}) {
    const el = h("div", { class: `toast ${kind}` }, h("span", {}, message));
    if (opts.action) {
      el.append(h("button", { class: "btn", onclick: () => { opts.action.run(); el.remove(); } }, opts.action.label));
    }
    $("#toasts").append(el);
    setTimeout(() => el.remove(), opts.duration || 4500);
  }

  // --------------------------------------------------------- scan listener
  // The Zebra DS2278 acts as a USB HID keyboard: it "types" the code very fast
  // and finishes with Enter. We listen globally so scanning works in every mode
  // without focusing an input first. Scanners configured without an Enter/Tab
  // suffix are handled too: a burst of scanner-speed keystrokes is committed
  // after a short idle timeout. Keystrokes in a real text field (manual date
  // entry, product name, search…) are never treated as scanner input.
  const Scanner = (() => {
    const SCANNER_MAX_GAP_MS = 60;   // keystroke gap that is "too fast for a human"
    const IDLE_RESET_MS = 1000;
    const BURST_COMMIT_MS = 250;     // idle time after which a suffix-less scan is committed
    const MIN_BURST_LENGTH = 4;
    let buffer = "";
    let gaps = [];
    let last = 0;
    let commitTimer = null;
    let handler = () => {};

    const sink = $("#scan-sink");
    const nonTextTypes = new Set(["button", "checkbox", "radio", "submit", "reset", "range", "color", "file"]);
    function isEditable(el) {
      if (!el || el === sink) return false;
      if (el.isContentEditable) return true;
      if (el.tagName === "TEXTAREA" || el.tagName === "SELECT") return true;
      return el.tagName === "INPUT" && !nonTextTypes.has(el.type);
    }

    function reset() {
      buffer = "";
      gaps = [];
      if (commitTimer) { clearTimeout(commitTimer); commitTimer = null; }
    }

    function isScannerBurst() { return gaps.length >= 2 && gaps.every((g) => g < SCANNER_MAX_GAP_MS); }

    // Fallback for scanners that send no Enter/Tab suffix: commit the buffer once
    // the keystrokes stop, but only when they arrived at scanner speed, so a
    // human typing (who may still press Enter) keeps the buffer.
    function commitBurst() {
      commitTimer = null;
      const code = buffer.trim();
      if (code.length < MIN_BURST_LENGTH || !isScannerBurst()) return;
      reset();
      handler(code);
    }

    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.altKey || e.metaKey) return;
      // Typing in a text field is always manual entry (e.g. a date), never a scan.
      if (isEditable(document.activeElement)) { reset(); return; }
      const now = performance.now();
      const gap = now - last;
      last = now;

      if (e.key === "Enter" || e.key === "Tab") {
        const code = buffer.trim();
        reset();
        if (!code) return;
        e.preventDefault();
        handler(code);
        return;
      }
      if (e.key.length !== 1) return;
      if (gap > IDLE_RESET_MS) reset();
      else if (buffer) gaps.push(gap);
      buffer += e.key;
      if (commitTimer) clearTimeout(commitTimer);
      commitTimer = setTimeout(commitBurst, BURST_COMMIT_MS);
      e.preventDefault();
    }, true);

    function refocus() {
      const a = document.activeElement;
      if (!a || a === document.body || a === document.documentElement) sink.focus({ preventScroll: true });
      sink.value = "";
    }
    document.addEventListener("focusout", () => setTimeout(refocus, 0));
    document.addEventListener("pointerup", (e) => { if (!isEditable(e.target)) setTimeout(refocus, 0); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refocus(); });
    window.addEventListener("focus", refocus);
    setInterval(refocus, 1500);
    refocus();

    return { onScan: (fn) => { handler = fn; } };
  })();

  Scanner.onScan((code) => {
    if (!$("#modal").classList.contains("hidden")) {
      if (state.modalScanHandler) state.modalScanHandler(code);
      else toast("Close the dialog before scanning", "info");
      return;
    }
    if (state.mode === "consume") Consume.scan(code);
    else if (state.mode === "search") Search.scan(code);
    else Add.scan(code);
  });

  // ------------------------------------------------------------------ modes
  function setMode(mode) {
    state.mode = mode;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.mode === mode));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${mode}`));
    if (mode === "search") Search.refresh();
    if (mode === "add") Add.render();
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => setMode(t.dataset.mode)));

  // ------------------------------------------------------------ idle return
  // After a few minutes without any interaction the kiosk goes back to the
  // consume screen (the default state someone walking up to it expects).
  const IdleReturn = (() => {
    let timer = null;
    function arm() {
      if (timer) clearTimeout(timer);
      timer = null;
      const secs = state.config.ui_idle_return_seconds;
      if (!secs || secs <= 0) return;
      timer = setTimeout(() => {
        timer = null;
        if (state.mode !== "consume" || !$("#modal").classList.contains("hidden")) {
          closeModal();
          setMode("consume");
        }
      }, secs * 1000);
    }
    ["pointerdown", "keydown", "wheel", "touchstart"].forEach((ev) =>
      document.addEventListener(ev, arm, { passive: true, capture: true }));
    return { arm };
  })();

  // ------------------------------------------------------------- modal
  function openModal(content, scanHandler = null) {
    const box = $("#modal-content");
    box.replaceChildren(content);
    state.modalScanHandler = scanHandler;
    $("#modal").classList.remove("hidden");
  }
  function closeModal() {
    $("#modal").classList.add("hidden");
    $("#modal-content").replaceChildren();
    state.modalScanHandler = null;
  }
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

  // ------------------------------------------------ category drill-down
  function renderCategoryRows(majorsEl, subsEl, sel, onChange, opts = {}) {
    majorsEl.replaceChildren(
      h("button", { class: `chip${sel.major === null ? " active" : ""}`, onclick: () => onChange(null, null) }, "All"),
      ...state.catalog.categories.map((c) =>
        h("button", { class: `chip${sel.major === c.name ? " active" : ""}`, onclick: () => onChange(c.name, null) }, c.name)
      )
    );
    const major = state.catalog.categories.find((c) => c.name === sel.major);
    if (!major || (!major.subcategories.length && !opts.alwaysShowSubs)) { subsEl.replaceChildren(); return; }
    subsEl.replaceChildren(
      h("button", { class: `chip sub-chip${sel.sub === null ? " active" : ""}`, onclick: () => onChange(major.name, null) }, `All ${major.name}`),
      ...major.subcategories.map((s) =>
        h("button", { class: `chip sub-chip${sel.sub === s.name ? " active" : ""}`, onclick: () => onChange(major.name, s.name) }, s.name)
      )
    );
  }

  function matchesCategory(item, sel) {
    if (sel.major === null) return true;
    if (item.major !== sel.major) return false;
    return sel.sub === null || item.sub === sel.sub;
  }

  async function loadCatalog() {
    try {
      state.catalog = await api("GET", "/api/catalog");
    } catch (e) {
      toast(`Could not load products: ${e.message}`, "bad");
    }
  }

  function splitGroup(name) {
    const delim = state.config.category_delimiter || "/";
    const i = (name || "").indexOf(delim);
    if (!name || !name.trim()) return { major: "Uncategorized", sub: null };
    if (i < 0) return { major: name.trim(), sub: null };
    return { major: name.slice(0, i).trim() || "Uncategorized", sub: name.slice(i + delim.length).trim() || null };
  }

  function groupsForMajor(major) {
    return state.catalog.groups
      .map((g) => ({ ...g, ...splitGroup(g.name) }))
      .filter((g) => g.major === major)
      .sort((a, b) => (a.sub || "").localeCompare(b.sub || ""));
  }

  // Majors + subcategory chip picker used by the new-product and change-category dialogs.
  function renderGroupPicker(majorsEl, groupsEl, sel, extraChips = []) {
    function render() {
      const majors = state.catalog.categories.filter((c) => c.group_ids.length);
      majorsEl.replaceChildren(
        h("button", { class: `chip${sel.major === null ? " active" : ""}`, onclick: () => { sel.major = null; sel.groupId = null; render(); } }, "No category"),
        ...majors.map((c) => h("button", {
          class: `chip${sel.major === c.name ? " active" : ""}`,
          onclick: () => {
            sel.major = c.name;
            const groups = groupsForMajor(c.name);
            sel.groupId = groups.length === 1 ? groups[0].id : null;
            render();
          },
        }, c.name)),
        ...extraChips);
      if (sel.major === null) { groupsEl.replaceChildren(); return; }
      groupsEl.replaceChildren(...groupsForMajor(sel.major).map((g) => h("button", {
        class: `chip sub-chip${sel.groupId === g.id ? " active" : ""}`,
        onclick: () => { sel.groupId = g.id; render(); },
      }, g.sub || `${g.major} (general)`)));
    }
    render();
    return render;
  }

  // Inline "type a name + Add" row used for creating categories/locations in dialogs.
  function inlineAdder(placeholder, onCreate) {
    const input = h("input", { type: "text", placeholder, autocomplete: "off" });
    const addBtn = h("button", { class: "btn btn-primary" }, "Add");
    const cancelBtn = h("button", { class: "btn btn-secondary", "aria-label": "Cancel" }, "✕");
    const row = h("div", { class: "inline-add hidden" }, input, addBtn, cancelBtn);
    function hide() { row.classList.add("hidden"); input.value = ""; }
    addBtn.addEventListener("click", async () => {
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      addBtn.disabled = true;
      try { await onCreate(name); hide(); }
      catch (e) { toast(e.message, "bad"); }
      finally { addBtn.disabled = false; }
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addBtn.click(); } });
    cancelBtn.addEventListener("click", hide);
    return { row, show: () => { row.classList.remove("hidden"); input.focus(); } };
  }

  // ================================================================ CONSUME
  const Consume = (() => {
    const card = $("#consume-card");
    const banner = $("#consume-banner");
    const number = $("#countdown-number");
    const fill = $("#countdown-fill");
    const undoBtn = $("#undo-btn");

    function setState(cls, text) {
      card.classList.remove("state-pending", "state-committed", "state-cancelled", "state-failed");
      card.classList.add(`state-${cls}`);
      banner.textContent = text;
      undoBtn.classList.toggle("hidden", cls !== "pending");
    }

    function showItem(item) {
      $("#consume-idle").classList.add("hidden");
      card.classList.remove("hidden");
      $("#ci-name").textContent = item.product_name;
      $("#ci-expiry").textContent = fmtExpiry(item.best_before_date);
      $("#ci-location").textContent = item.location_name || "—";
      $("#ci-stock").textContent = item.stock_id ? item.stock_id + (item.specific_entry ? "" : " (next to expire)") : "—";
    }

    const recorded = new Set();
    function addHistory(p) {
      if (recorded.has(p.id)) return;
      recorded.add(p.id);
      const labels = { committed: "Removed from Grocy", cancelled: "Kept in stock", failed: "Failed" };
      const list = $("#consume-history");
      list.prepend(h("li", {},
        h("span", {}, `${p.item.product_name}`, h("span", { class: "muted" }, `  ${new Date().toLocaleTimeString()}`)),
        h("span", { class: `tag ${p.status}` }, labels[p.status] || p.status, p.error ? ` – ${p.error}` : "")));
      while (list.children.length > 8) list.lastChild.remove();
    }

    function stopTimer() { if (state.consume.timer) { clearInterval(state.consume.timer); state.consume.timer = null; } }

    function finish(p) {
      stopTimer();
      if (state.consume.current && state.consume.current.id === p.id) state.consume.current = null;
      if (state.consume.lastShown === p.id) {
        if (p.status === "committed") setState("committed", "Removed from Grocy");
        else if (p.status === "cancelled") setState("cancelled", "Cancelled — Item kept in stock");
        else if (p.status === "failed") setState("failed", `Not removed: ${p.error || "Grocy error"}`);
      }
      addHistory(p);
      Search.invalidate();
    }

    function tick() {
      const cur = state.consume.current;
      if (!cur) return;
      const total = state.config.consume_countdown_seconds * 1000;
      const remaining = Math.max(0, state.consume.endsAt - performance.now());
      const secs = Math.ceil(remaining / 1000);
      if (number.textContent !== String(secs)) {
        number.textContent = secs > 0 ? `${secs}…` : "0";
        number.classList.remove("tick"); void number.offsetWidth; number.classList.add("tick");
      }
      fill.style.transform = `scaleX(${remaining / total})`;
      if (remaining <= 0) { stopTimer(); commit(cur.id); }
    }

    async function commit(id) {
      try {
        finish(await api("POST", `/api/consume/${id}/commit`));
      } catch (e) {
        toast(`Consume failed: ${e.message}`, "bad");
      }
    }

    async function scan(code) {
      stopTimer();
      try {
        const res = await api("POST", "/api/consume", { code });
        res.committed_previous.forEach((p) => { if (p.status !== "pending") addHistory(p); });
        const p = res.pending;
        state.consume.current = p;
        state.consume.lastShown = p.id;
        state.consume.endsAt = performance.now() + p.remaining_seconds * 1000;
        showItem(p.item);
        setState("pending", "Item consumed");
        number.textContent = "";
        tick();
        state.consume.timer = setInterval(tick, 100);
        Search.invalidate();
      } catch (e) {
        // The server may have committed the previous pending item before this scan failed.
        let resumed = false;
        const cur = state.consume.current;
        if (cur) {
          try {
            const p = await api("GET", `/api/consume/${cur.id}`);
            if (p.status === "pending") { state.consume.timer = setInterval(tick, 100); resumed = true; }
            else finish(p);
          } catch (_) { state.consume.current = null; }
        }
        if (!resumed) {
          state.consume.lastShown = null;
          $("#consume-idle").classList.add("hidden");
          card.classList.remove("hidden");
          ["#ci-name", "#ci-expiry", "#ci-location", "#ci-stock"].forEach((s) => { $(s).textContent = "—"; });
          $("#ci-name").textContent = code;
          setState("failed", e.status === 404 ? "Not found" : e.status === 409 ? "Not in stock" : "Error");
        }
        toast(e.message, "bad");
      }
    }

    undoBtn.addEventListener("click", async () => {
      const cur = state.consume.current;
      if (!cur) return;
      stopTimer();
      try {
        finish(await api("POST", `/api/consume/${cur.id}/cancel`));
        toast("Cancelled — Item kept in stock", "info");
      } catch (e) {
        if (e.status === 409 && e.data && e.data.pending) { finish(e.data.pending); toast(e.message, "bad"); }
        else { toast(`Undo failed: ${e.message}`, "bad"); state.consume.timer = setInterval(tick, 100); }
      }
    });

    return { scan, consumeCode: (code) => { setMode("consume"); scan(code); } };
  })();

  // ================================================================= SEARCH
  const Search = (() => {
    let stale = true;
    let shownProductId = null;
    const input = $("#search-input");
    const resultBox = $("#search-result");

    function invalidate() { stale = true; if (state.mode === "search") refresh(); }

    function refreshResult() {
      // Keep the open product panel in sync (consumed/opened/moved entries).
      if (resultBox.classList.contains("hidden") || !shownProductId) return;
      api("GET", `/api/products/${shownProductId}/stock`)
        .then((res) => renderResult(res, { silent: true }))
        .catch(() => {});
    }

    async function refresh() {
      renderFilters();
      if (!stale) { renderList(); refreshResult(); return; }
      try {
        const [inv] = await Promise.all([api("GET", "/api/inventory"), loadCatalog()]);
        state.inventory = inv.items;
        stale = false;
        renderFilters();
        renderList();
        refreshResult();
      } catch (e) {
        $("#inventory-summary").textContent = `Could not load inventory: ${e.message}`;
      }
    }

    function renderFilters() {
      renderCategoryRows($("#search-majors"), $("#search-subs"), state.search, (major, sub) => {
        state.search.major = major; state.search.sub = sub;
        shownProductId = null;
        resultBox.classList.add("hidden");
        renderFilters(); renderList();
      });
      renderLocationFilter();
    }

    // Where a product's stock actually is (entries may have been moved), falling
    // back to the product's default location.
    function itemLocations(i) {
      if (i.stock_locations && i.stock_locations.length) return i.stock_locations;
      return i.location_name ? [i.location_name] : [];
    }

    function renderLocationFilter() {
      const names = new Set();
      state.inventory.forEach((i) => itemLocations(i).forEach((n) => names.add(n)));
      if (state.search.location) names.add(state.search.location);
      const box = $("#search-locations");
      const pick = (name) => {
        state.search.location = name;
        shownProductId = null;
        resultBox.classList.add("hidden");
        renderLocationFilter(); renderList();
      };
      box.replaceChildren(
        h("button", { class: `chip${state.search.location === null ? " active" : ""}`, onclick: () => pick(null) }, "All locations"),
        ...[...names].sort((a, b) => a.localeCompare(b)).map((n) =>
          h("button", { class: `chip${state.search.location === n ? " active" : ""}`, onclick: () => pick(n) }, `📍 ${n}`))
      );
    }

    function renderList() {
      const text = state.search.text.trim().toLowerCase();
      const items = state.inventory.filter((i) =>
        matchesCategory(i, state.search)
        && (state.search.location === null || itemLocations(i).includes(state.search.location))
        && (!text || i.name.toLowerCase().includes(text)));
      const totalAmount = items.reduce((s, i) => s + i.amount, 0);
      const filtered = state.search.major !== null || state.search.location !== null || text;
      $("#inventory-summary").replaceChildren(
        filtered ? "Showing " : "Total current inventory: ",
        h("strong", {}, `${items.length} product${items.length === 1 ? "" : "s"}`),
        ` · ${fmtAmount(totalAmount)} item${totalAmount === 1 ? "" : "s"}`,
        filtered ? ` (of ${state.inventory.length} products)` : ""
      );
      $("#inventory-list").replaceChildren(...items.map((i) =>
        h("li", { class: `inv-item ${expiryClass(i.best_before_date)}`, onclick: () => showProduct(i.product_id) },
          h("div", { class: "name" }, i.name),
          h("div", { class: "meta" },
            h("span", { class: "exp" }, fmtExpiry(i.best_before_date)),
            h("span", { class: "amount" }, `× ${fmtAmount(i.amount)}`)),
          h("div", { class: "meta" }, h("span", {}, i.sub ? `${i.major} › ${i.sub}` : i.major), h("span", {}, (i.stock_locations && i.stock_locations.length ? i.stock_locations.join(", ") : i.location_name) || "")))
      ));
    }

    function renderResult(res, opts = {}) {
      const p = res.product;
      const entries = res.entries;
      shownProductId = p.id;
      const rows = entries.map((e) => {
        const highlight = res.stock_id && e.stock_id === res.stock_id;
        return h("li", { class: `entry ${expiryClass(e.best_before_date)}${highlight ? " highlight" : ""}` },
          h("div", { class: "row" },
            h("strong", { class: "exp" }, `EXP ${fmtExpiry(e.best_before_date)}`),
            h("span", {}, `Qty ${fmtAmount(e.amount)}`),
            h("span", {}, `📍 ${e.location_name || "—"}`),
            e.open ? h("span", {}, "Opened") : null,
            h("span", { class: "mono muted" }, e.stock_id)),
          h("div", { class: "actions" },
            h("button", { class: "btn btn-danger", onclick: () => Consume.consumeCode(`${state.config.barcode_prefix}${e.stock_id}`) }, "Consume Now"),
            !e.open ? h("button", { class: "btn btn-secondary", onclick: () => openOne(e.stock_id) },
              p.open_location_name ? `Open 1 → ${p.open_location_name}` : "Open 1") : null,
            h("button", { class: "btn btn-secondary", onclick: () => reprint(e.stock_id) }, "⎙ Reprint Label"),
            h("button", { class: "btn btn-secondary", onclick: () => moveDialog(e, p) }, "Move Location")));
      });
      resultBox.replaceChildren(
        h("header", {},
          h("div", {}, h("h2", {}, p.name), h("div", { class: "muted" }, p.sub ? `${p.major} › ${p.sub}` : p.major)),
          h("div", { class: "header-actions" },
            h("button", { class: "btn btn-secondary", onclick: () => changeCategoryDialog(p) }, "✎ Category"),
            h("button", { class: "btn btn-secondary", onclick: () => { shownProductId = null; resultBox.classList.add("hidden"); } }, "✕ Close"))),
        entries.length ? h("ul", { class: "entries" }, rows) : h("p", { class: "big muted" }, "Not in stock")
      );
      resultBox.classList.remove("hidden");
      if (!opts.silent) resultBox.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    async function showProduct(productId) {
      try { renderResult(await api("GET", `/api/products/${productId}/stock`)); }
      catch (e) { toast(e.message, "bad"); }
    }

    function changeCategoryDialog(product) {
      const sel = { major: null, groupId: product.group_id || null };
      const current = state.catalog.groups.find((g) => g.id === sel.groupId);
      if (current) sel.major = splitGroup(current.name).major;
      const majorsEl = h("div", { class: "chip-row" });
      const groupsEl = h("div", { class: "chip-row sub" });
      const saveBtn = h("button", { class: "btn btn-primary" }, "Save");
      renderGroupPicker(majorsEl, groupsEl, sel);
      saveBtn.addEventListener("click", async () => {
        if (sel.major !== null && sel.groupId === null) { toast("Pick a subcategory", "bad"); return; }
        saveBtn.disabled = true;
        try {
          const updated = await api("POST", `/api/products/${product.id}/category`, { product_group_id: sel.groupId });
          closeModal();
          toast(`${updated.name} → ${updated.sub ? `${updated.major} › ${updated.sub}` : updated.major}`, "ok");
          await loadCatalog();
          invalidate();
          Add.render();
          renderResult(await api("GET", `/api/products/${product.id}/stock`));
        } catch (e) {
          toast(e.message, "bad");
          saveBtn.disabled = false;
        }
      });
      openModal(h("div", {},
        h("h2", {}, `Category for ${product.name}`),
        h("div", { class: "field" }, majorsEl, groupsEl),
        h("div", { class: "button-row" }, h("button", { class: "btn btn-secondary", onclick: closeModal }, "Cancel"), saveBtn)));
    }

    async function scan(code) {
      try {
        const res = await api("GET", `/api/lookup?code=${encodeURIComponent(code)}`);
        state.search.major = null; state.search.sub = null; state.search.location = null;
        renderFilters(); renderList();
        renderResult(res);
      } catch (e) {
        toast(e.status === 404 ? `No match for ${code}` : e.message, "bad");
      }
    }

    function moveDialog(entry, product) {
      const buttons = state.catalog.locations.map((loc) =>
        h("button", {
          class: `chip${loc.id === entry.location_id ? " active" : ""}`,
          onclick: async () => {
            try {
              await api("POST", `/api/stock/${encodeURIComponent(entry.stock_id)}/move`, { location_id: loc.id });
              closeModal();
              toast(`${product.name} moved to ${loc.name}`, "ok");
              stale = true;
              renderResult(await api("GET", `/api/products/${product.id}/stock`));
            } catch (e) { toast(e.message, "bad"); }
          },
        }, loc.name));
      openModal(h("div", {},
        h("h2", {}, `Move ${product.name}`),
        h("p", { class: "muted" }, `Currently in ${entry.location_name || "—"}`),
        h("div", { class: "chip-row" }, buttons),
        h("div", { class: "button-row" }, h("button", { class: "btn btn-secondary", onclick: closeModal }, "Cancel"))));
    }

    input.addEventListener("input", () => { state.search.text = input.value; renderList(); });
    $("#search-clear").addEventListener("click", () => { input.value = ""; state.search.text = ""; renderList(); input.blur(); });

    return { refresh, invalidate, scan };
  })();

  async function reprint(stockId) {
    try {
      await api("POST", `/api/print/stock/${encodeURIComponent(stockId)}`);
      toast("Label sent to printer", "ok");
    } catch (e) { toast(`Print failed: ${e.message}`, "bad"); }
  }

  async function openOne(stockId) {
    try {
      const res = await api("POST", `/api/stock/${encodeURIComponent(stockId)}/open`);
      toast(res.moved
        ? `Opened 1 × ${res.product_name} → moved to ${res.location_name}`
        : `Opened 1 × ${res.product_name}${res.location_name ? ` (kept in ${res.location_name})` : ""}`, "ok");
      Search.invalidate();
      return res;
    } catch (e) {
      toast(`Open failed: ${e.message}`, "bad");
      return null;
    }
  }

  // ==================================================================== ADD
  const Add = (() => {
    const dateInput = $("#expiry-date");

    function qtyPrefix(amount) { return amount > 1 ? `${fmtAmount(amount)}× ` : ""; }

    function showStep(step) {
      $("#add-step-1").classList.toggle("hidden", step !== 1);
      $("#add-step-2").classList.toggle("hidden", step !== 2);
      $("#add-result").classList.toggle("hidden", step !== 3);
      $$(".steps li").forEach((li) => {
        const n = Number(li.dataset.step);
        li.classList.toggle("active", n === step);
        li.classList.toggle("done", n < step);
      });
    }

    function render() {
      renderCategoryRows($("#add-majors"), $("#add-subs"), state.add, (major, sub) => {
        state.add.major = major; state.add.sub = sub; render();
      });
      const products = state.catalog.products.filter((p) => matchesCategory(p, state.add));
      $("#add-products").replaceChildren(...products.map((p) =>
        h("button", { class: "product-btn", onclick: () => selectProduct(p) }, p.name,
          h("small", {}, p.sub ? `${p.major} › ${p.sub}` : p.major))));
      if (!products.length) $("#add-products").append(h("p", { class: "muted" }, "No products in this category."));
    }

    function defaultExpiry(product) {
      const days = product.default_best_before_days;
      if (days === -1) return null;
      return addDays(days > 0 ? days : state.config.default_best_before_days);
    }

    function setExpiry(value, activeBtn = null) {
      state.add.expiry = value; // null = no expiry
      $("#expiry-value").textContent = value ? fmtExpiry(value) : "No expiry";
      dateInput.value = value || "";
      $$("#expiry-quick .btn").forEach((b) => b.classList.toggle("active", b === activeBtn));
    }

    function setQty(value) {
      state.add.qty = Math.min(999, Math.max(1, value));
      $("#qty-value").textContent = String(state.add.qty);
      $("#qty-minus").disabled = state.add.qty <= 1;
    }

    function setLabelMode(mode) {
      state.add.labelMode = mode;
      $$("#label-mode-seg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.labelMode === mode));
    }

    function setPrintMode(mode) {
      state.add.printMode = mode;
      $$("#print-mode-seg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.printMode === mode));
      // Label count is irrelevant when no label is printed.
      $$("#label-mode-seg .seg-btn").forEach((b) => { b.disabled = mode === "oem"; });
      $("#add-print-btn").textContent = mode === "oem" ? "ADD TO GROCY" : "ADD & PRINT LABEL";
    }

    // "When opened, move one to <location>" – stored on the Grocy product
    // (move_on_open + default consume location), so it sticks for next time.
    function renderOpenBehavior() {
      const product = state.add.product;
      const box = $("#open-behavior-chips");
      if (!product) { box.replaceChildren(); return; }
      const set = async (locationId) => {
        try {
          const updated = await api("POST", `/api/products/${product.id}/open-behavior`, { location_id: locationId });
          Object.assign(product, updated);
          const cat = state.catalog.products.find((x) => x.id === product.id);
          if (cat) Object.assign(cat, updated);
          renderOpenBehavior();
          toast(updated.open_location_id === null
            ? `${product.name}: opened items stay in place`
            : `${product.name}: opened items move to ${updated.open_location_name}`, "info", { duration: 2500 });
        } catch (e) { toast(e.message, "bad"); }
      };
      box.replaceChildren(
        h("button", { class: `chip${product.open_location_id == null ? " active" : ""}`, onclick: () => set(null) }, "Stays in place"),
        ...state.catalog.locations.map((loc) => h("button", {
          class: `chip${product.open_location_id === loc.id ? " active" : ""}`,
          onclick: () => set(loc.id),
        }, `Move to ${loc.name}`))
      );
    }

    function selectProduct(product) {
      state.add.product = product;
      $("#add-product-name").textContent = product.name;
      $("#add-product-category").textContent = product.sub ? `${product.major} › ${product.sub}` : product.major;
      setExpiry(defaultExpiry(product));
      setQty(1);
      setLabelMode("individual");
      setPrintMode("print");
      renderOpenBehavior();
      showStep(2);
    }

    async function scan(code) {
      try {
        const res = await api("GET", `/api/lookup?code=${encodeURIComponent(code)}`);
        selectProduct(res.product);
        toast(`Selected ${res.product.name}`, "info", { duration: 2000 });
      } catch (e) {
        if (e.status === 404 && !code.toLowerCase().startsWith(state.config.barcode_prefix.toLowerCase())) newProductDialog(code);
        else toast(e.message, "bad");
      }
    }

    function newProductDialog(barcode = null) {
      const sel = { major: null, groupId: null, locationId: null };
      const nameInput = h("input", { type: "text", placeholder: "Product name", autocomplete: "off", enterkeyhint: "done" });
      const barcodeEl = h("div", { class: "big mono" }, barcode || "— (no barcode)");
      const lookupHint = h("div", { class: "muted lookup-hint" }, "");
      const majorsEl = h("div", { class: "chip-row" });
      const groupsEl = h("div", { class: "chip-row sub" });
      const saveBtn = h("button", { class: "btn btn-primary" }, "Create product");
      let prefilled = "";

      async function prefillFromBarcode(code) {
        if (!code) return;
        lookupHint.textContent = "Looking up barcode…";
        try {
          const info = await api("GET", `/api/external-product?barcode=${encodeURIComponent(code)}`);
          if (code !== barcode) return; // a newer scan replaced this barcode
          if (!info.found) { lookupHint.textContent = "Barcode not in OpenFoodFacts – enter the name yourself."; return; }
          const name = info.brand && !info.name.toLowerCase().includes(info.brand.toLowerCase())
            ? `${info.brand} ${info.name}` : info.name;
          // Don't clobber anything the user already typed.
          if (!nameInput.value.trim() || nameInput.value === prefilled) {
            nameInput.value = name;
            prefilled = name;
          }
          lookupHint.textContent = `Found in OpenFoodFacts${info.quantity ? ` · ${info.quantity}` : ""} – check the name, then pick a category.`;
        } catch (_) {
          if (code === barcode) lookupHint.textContent = "Barcode lookup unavailable – enter the name yourself.";
        }
      }

      const renderCats = renderGroupPicker(majorsEl, groupsEl, sel, [
        h("button", { class: "chip chip-add", onclick: () => catAdder.show() }, "＋ New…"),
      ]);
      const catAdder = inlineAdder(
        `New category, e.g. Dairy${state.config.category_delimiter || "/"}Cheese`,
        async (name) => {
          const group = await api("POST", "/api/categories", { name });
          await loadCatalog();
          sel.major = splitGroup(name).major;
          sel.groupId = group.id;
          renderCats();
          toast(`Created category ${name}`, "ok");
        }
      );

      const locationsEl = h("div", { class: "chip-row" });
      const locAdder = inlineAdder("New location name", async (name) => {
        const loc = await api("POST", "/api/locations", { name });
        await loadCatalog();
        sel.locationId = loc.id;
        renderLocs();
        toast(`Created location ${name}`, "ok");
      });
      function renderLocs() {
        locationsEl.replaceChildren(
          h("button", { class: `chip${sel.locationId === null ? " active" : ""}`, onclick: () => { sel.locationId = null; renderLocs(); } }, "Default"),
          ...state.catalog.locations.map((loc) => h("button", {
            class: `chip${sel.locationId === loc.id ? " active" : ""}`,
            onclick: () => { sel.locationId = loc.id; renderLocs(); },
          }, loc.name)),
          h("button", { class: "chip chip-add", onclick: () => locAdder.show() }, "＋ New…"));
      }
      renderLocs();

      saveBtn.addEventListener("click", async () => {
        const name = nameInput.value.trim();
        if (!name) { toast("Enter a product name", "bad"); nameInput.focus(); return; }
        if (sel.major !== null && sel.groupId === null) { toast("Pick a subcategory", "bad"); return; }
        saveBtn.disabled = true;
        try {
          const product = await api("POST", "/api/products", { name, barcode, product_group_id: sel.groupId, location_id: sel.locationId });
          closeModal();
          toast(`Created ${product.name}`, "ok");
          await loadCatalog();
          Search.invalidate();
          selectProduct(product);
        } catch (e) {
          toast(e.message, "bad");
          saveBtn.disabled = false;
        }
      });

      openModal(h("div", {},
        h("h2", {}, barcode ? "Unknown barcode – create product" : "New product"),
        h("div", { class: "field" }, h("div", { class: "label" }, "Barcode"), barcodeEl, lookupHint),
        h("div", { class: "field" }, h("div", { class: "label" }, "Name"), nameInput),
        h("div", { class: "field" }, h("div", { class: "label" }, "Category"), majorsEl, groupsEl, catAdder.row),
        h("div", { class: "field" }, h("div", { class: "label" }, "Location"), locationsEl, locAdder.row),
        h("div", { class: "button-row" }, h("button", { class: "btn btn-secondary", onclick: closeModal }, "Cancel"), saveBtn)
      ), (code) => {
        barcode = code;
        barcodeEl.textContent = code;
        prefillFromBarcode(code);
      });
      prefillFromBarcode(barcode);
      setTimeout(() => nameInput.focus(), 50);
    }

    async function addAndPrint() {
      const product = state.add.product;
      if (!product) return;
      const printing = state.add.printMode !== "oem";
      const btn = $("#add-print-btn");
      btn.disabled = true;
      btn.textContent = "Saving…";
      try {
        const res = await api("POST", "/api/stock/add", {
          product_id: product.id,
          amount: state.add.qty,
          best_before_date: state.add.expiry,
          no_expiry: state.add.expiry === null,
          print_label: printing,
          label_mode: state.add.labelMode,
        });
        state.add.lastResult = res;
        $("#ar-name").textContent = res.product_name;
        $("#ar-qty").textContent = fmtAmount(res.amount || 1);
        $("#ar-expiry").textContent = fmtExpiry(res.best_before_date);
        $("#ar-stock").textContent = res.barcode;
        const pr = res.print;
        const expected = state.add.labelMode === "box" ? 1 : res.amount;
        $("#ar-print").textContent = !printing
          ? "None – using OEM barcode"
          : pr && pr.ok
            ? `Printed${pr.printed > 1 ? ` ×${pr.printed}` : ""} ✔`
            : pr && pr.printed
              ? `Printed ${pr.printed} of ${expected} – ${pr.error}`
              : `Not printed – ${pr ? pr.error : "disabled"}`;
        showStep(3);
        Search.invalidate();
        if (!printing) {
          toast(`Added ${qtyPrefix(res.amount)}${res.product_name} · consume by scanning its barcode`, "ok");
        } else if (pr && pr.ok) {
          toast(`Added ${qtyPrefix(res.amount)}${res.product_name} · ${pr.printed > 1 ? `${pr.printed} labels` : "label"} printed`, "ok", { action: { label: "Reprint", run: () => reprint(res.stock_id) } });
        } else {
          toast(`Added ${qtyPrefix(res.amount)}${res.product_name}, but ${pr && pr.printed ? `only ${pr.printed} of ${expected} labels printed` : "the label did not print"}`, "bad", { action: { label: "Retry print", run: () => reprint(res.stock_id) }, duration: 8000 });
        }
      } catch (e) {
        toast(`Add failed: ${e.message}`, "bad");
      } finally {
        btn.disabled = false;
        btn.textContent = state.add.printMode === "oem" ? "ADD TO GROCY" : "ADD & PRINT LABEL";
      }
    }

    $$("#expiry-quick .btn").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.none) setExpiry(null, b);
      else if (b.dataset.months) setExpiry(addMonths(Number(b.dataset.months)), b);
      else setExpiry(addDays(Number(b.dataset.days)), b);
    }));
    dateInput.addEventListener("change", () => { if (dateInput.value) setExpiry(dateInput.value); });
    $("#qty-minus").addEventListener("click", () => setQty(state.add.qty - 1));
    $("#qty-plus").addEventListener("click", () => setQty(state.add.qty + 1));
    $$("#label-mode-seg .seg-btn").forEach((b) => b.addEventListener("click", () => setLabelMode(b.dataset.labelMode)));
    $$("#print-mode-seg .seg-btn").forEach((b) => b.addEventListener("click", () => setPrintMode(b.dataset.printMode)));
    $("#add-print-btn").addEventListener("click", addAndPrint);
    $("#change-product-btn").addEventListener("click", () => showStep(1));
    $("#new-product-btn").addEventListener("click", () => newProductDialog());
    $("#reprint-btn").addEventListener("click", () => state.add.lastResult && reprint(state.add.lastResult.stock_id));
    $("#open-one-btn").addEventListener("click", () => state.add.lastResult && openOne(state.add.lastResult.stock_id));
    $("#add-same-btn").addEventListener("click", () => state.add.product && selectProduct(state.add.product));
    $("#add-new-btn").addEventListener("click", () => { state.add.product = null; showStep(1); render(); });

    return { scan, render };
  })();

  // ================================================================ status
  async function checkHealth() {
    try {
      const res = await api("GET", "/api/health");
      $("#grocy-dot").className = `dot ${res.grocy.ok ? "ok" : "bad"}`;
      $("#printer-dot").className = `dot ${res.printer.ok ? "ok" : "bad"}`;
      return res;
    } catch (_) {
      $("#grocy-dot").className = "dot bad";
      $("#printer-dot").className = "dot bad";
      return null;
    }
  }

  $("#status-btn").addEventListener("click", async () => {
    const res = await checkHealth();
    const line = (label, ok, detail) => h("li", {}, h("span", { class: "tag " + (ok ? "committed" : "failed") }, ok ? "● OK" : "● DOWN"), ` ${label}: ${detail || ""}`);
    openModal(h("div", {},
      h("h2", {}, "System status"),
      res ? h("ul", { class: "history" },
        line("Grocy", res.grocy.ok, res.grocy.ok ? `version ${res.grocy.version || "?"}` : res.grocy.error),
        line(`Printer ${res.printer.model}`, res.printer.ok, res.printer.detail)) : h("p", {}, "Kiosk server unreachable"),
      h("div", { class: "button-row" },
        h("button", { class: "btn btn-secondary", onclick: async () => {
          try { await api("POST", "/api/print/test"); toast("Test label printed", "ok"); } catch (e) { toast(`Test print failed: ${e.message}`, "bad"); }
        } }, "⎙ Print test label"),
        h("button", { class: "btn btn-secondary", onclick: () => location.reload() }, "↻ Reload app"),
        h("button", { class: "btn btn-primary", onclick: closeModal }, "Close"))));
  });

  // ================================================================== boot
  async function boot() {
    try {
      state.config = await api("GET", "/api/config");
    } catch (_) { /* keep defaults */ }
    $$(".js-countdown-seconds").forEach((el) => { el.textContent = state.config.consume_countdown_seconds; });
    IdleReturn.arm();
    await loadCatalog();
    checkHealth();
    setInterval(checkHealth, 30000);
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  }

  setMode("consume");
  boot();
})();
