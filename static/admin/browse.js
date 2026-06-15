/**
 * browse.js -- Card/Note browser logic for /admin/browse (A6).
 *
 * All server calls go through acsInvoke() from admin.js (token-gated proxy).
 * No external JS dependencies -- vanilla ES2020.
 *
 * Architecture:
 *   State object holds: currentQuery, currentOffset, pageSize, totalCards,
 *   allCardIds (current result set), cardsData (fetched card details),
 *   selectedIds (Set of card ids), sortCol, sortDir.
 *
 *   Search  -> findCardsPaginated -> cardsInfo -> render table
 *   Row click -> notesInfo -> open editor panel
 *   Bulk action -> confirm dialog -> acsInvoke -> refresh
 */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------
  const S = {
    query: "",
    offset: 0,
    pageSize: 50,
    total: 0,
    cardIds: [],       // full result from findCardsPaginated (current page ids)
    cardsData: [],     // cardsInfo results for current page
    selected: new Set(), // Set<number> of selected cardId
    sortCol: null,
    sortDir: 1,        // 1 = asc, -1 = desc
    // note editor
    editorNoteId: null,
    editorOrigFields: {},
    editorOrigTags: [],
  };

  // Server data embedded by Jinja
  const browseData = JSON.parse(
    document.getElementById("browse-data").textContent
  );
  const DECK_NAMES = browseData.deckNames || [];
  const ALL_TAGS = browseData.allTags || [];

  // -------------------------------------------------------------------------
  // DOM refs
  // -------------------------------------------------------------------------
  const searchForm     = document.getElementById("search-form");
  const searchInput    = document.getElementById("search-input");
  const pageSizeSelect = document.getElementById("page-size-select");
  const statusBar      = document.getElementById("status-bar");
  const statusText     = document.getElementById("status-text");
  const selectionText  = document.getElementById("selection-text");
  const bulkToolbar    = document.getElementById("bulk-toolbar");
  const resultsSection = document.getElementById("results-section");
  const resultsTbody   = document.getElementById("results-tbody");
  const browseEmpty    = document.getElementById("browse-empty");
  const browseLoading  = document.getElementById("browse-loading");
  const browseTools    = document.getElementById("browse-tools");
  const selectAll      = document.getElementById("select-all");
  const prevBtn        = document.getElementById("prev-btn");
  const nextBtn        = document.getElementById("next-btn");
  const pageInfo       = document.getElementById("page-info");
  // Find & Replace
  const fnrSection     = document.getElementById("fnr-section");
  const fnrScope       = document.getElementById("fnr-scope");
  const fnrField       = document.getElementById("fnr-field");
  const fnrSearch      = document.getElementById("fnr-search");
  const fnrReplace     = document.getElementById("fnr-replace");
  const fnrRegex       = document.getElementById("fnr-regex");
  const fnrCase        = document.getElementById("fnr-case");
  const fnrApplyBtn    = document.getElementById("fnr-apply-btn");
  const toggleFnrBtn   = document.getElementById("toggle-fnr-btn");
  // Duplicates
  const dupesSection   = document.getElementById("dupes-section");
  const dupesField     = document.getElementById("dupes-field");
  const dupesRunBtn    = document.getElementById("dupes-run-btn");
  const dupesResults   = document.getElementById("dupes-results");
  const toggleDupesBtn = document.getElementById("toggle-dupes-btn");
  // Editor
  const editorOverlay  = document.getElementById("editor-overlay");
  const editorClose    = document.getElementById("editor-close");
  const editorBody     = document.getElementById("editor-body");
  const editorMeta     = document.getElementById("editor-meta");
  const editorFields   = document.getElementById("editor-fields");
  const editorTagsInput= document.getElementById("editor-tags-input");
  const editorSaveBtn  = document.getElementById("editor-save-btn");
  const editorDeleteBtn= document.getElementById("editor-delete-btn");
  const editorStatus   = document.getElementById("editor-status");
  // Confirm dialog
  const confirmOverlay = document.getElementById("confirm-overlay");
  const confirmTitle   = document.getElementById("confirm-title");
  const confirmBody    = document.getElementById("confirm-body");
  const confirmOk      = document.getElementById("confirm-ok");
  const confirmCancel  = document.getElementById("confirm-cancel");
  // Prompt dialog
  const promptOverlay  = document.getElementById("prompt-overlay");
  const promptTitle    = document.getElementById("prompt-title");
  const promptFields   = document.getElementById("prompt-fields");
  const promptOk       = document.getElementById("prompt-ok");
  const promptCancel   = document.getElementById("prompt-cancel");

  // -------------------------------------------------------------------------
  // Utilities
  // -------------------------------------------------------------------------

  function show(el) { el.hidden = false; }
  function hide(el) { el.hidden = true; }

  function esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
  }

  function cardState(queue, type) {
    if (queue === -1) return "suspended";
    if (queue === -2 || queue === -3) return "buried";
    if (type === 0) return "new";
    if (type === 1 || type === 3) return "learn";
    return "review";
  }

  const FLAG_COLORS = {
    0: "",
    1: "#ef4444",  // red
    2: "#f97316",  // orange
    3: "#22c55e",  // green
    4: "#3b82f6",  // blue
    5: "#a855f7",  // purple
    6: "#ec4899",  // pink
    7: "#78716c",  // gray
  };

  function flagSwatch(n) {
    const c = FLAG_COLORS[n] || "";
    if (!c) return "";
    return `<span class="flag-dot" style="background:${c}" title="Flag ${n}"></span>`;
  }

  /** Extract the sort field value from a card data object. */
  function sortVal(card, col) {
    switch (col) {
      case "firstField": return Object.values(card.fields || {})[0]?.value || "";
      case "deckName":   return card.deckName || "";
      case "modelName":  return card.modelName || "";
      case "due":        return card.due || 0;
      case "interval":   return card.interval || 0;
      case "reps":       return card.reps || 0;
      case "lapses":     return card.lapses || 0;
      case "flags":      return card.flags || 0;
      case "state":      return cardState(card.queue, card.type);
      default:           return 0;
    }
  }

  // -------------------------------------------------------------------------
  // Confirm dialog (promise-based)
  // -------------------------------------------------------------------------

  /**
   * Show a confirmation dialog.
   *
   * @param {string} title      - Plain-text dialog title.
   * @param {string} body       - Plain-text message body.  Displayed via
   *                              textContent so no HTML interpretation occurs.
   *                              If rich markup is genuinely needed, build a
   *                              DocumentFragment from escaped parts and pass
   *                              it as the `bodyNode` option instead.
   * @param {string} okLabel    - Label for the confirm button.
   * @param {string} okClass    - CSS class(es) for the confirm button.
   * @param {{bodyNode?: Node}} [opts] - Advanced: pass a pre-built DOM node as
   *                              the dialog body.  The node MUST be constructed
   *                              from server-controlled or already-escaped data.
   */
  function confirm(title, body, okLabel = "Confirm", okClass = "btn btn-danger", opts = {}) {
    return new Promise((resolve) => {
      confirmTitle.textContent = title;
      // Use textContent to prevent XSS: never put raw user data into innerHTML.
      // For the handful of callers that need bold counts/names we accept a
      // pre-built DOM node via opts.bodyNode (must be constructed safely by the
      // caller).
      confirmBody.innerHTML = "";
      if (opts.bodyNode) {
        confirmBody.appendChild(opts.bodyNode);
      } else {
        confirmBody.textContent = body;
      }
      confirmOk.textContent = okLabel;
      confirmOk.className = okClass;
      show(confirmOverlay);
      const cleanup = () => {
        hide(confirmOverlay);
        confirmOk.removeEventListener("click", onOk);
        confirmCancel.removeEventListener("click", onCancel);
      };
      const onOk = () => { cleanup(); resolve(true); };
      const onCancel = () => { cleanup(); resolve(false); };
      confirmOk.addEventListener("click", onOk);
      confirmCancel.addEventListener("click", onCancel);
    });
  }

  // -------------------------------------------------------------------------
  // Prompt dialog (promise-based)
  // -------------------------------------------------------------------------

  /**
   * Show a prompt dialog with one or more named fields.
   *
   * @param {string} title
   * @param {Array<{name, label, type, options?, value?}>} fields
   * @returns {Promise<Object|null>}  field values or null on cancel
   */
  function prompt(title, fields) {
    return new Promise((resolve) => {
      promptTitle.textContent = title;
      promptFields.innerHTML = "";

      fields.forEach(({ name, label, type, options, value }) => {
        const row = document.createElement("div");
        row.className = "prompt-row";
        const lbl = document.createElement("label");
        lbl.textContent = label;
        lbl.className = "fnr-label";
        row.appendChild(lbl);

        let input;
        if (type === "select" && options) {
          input = document.createElement("select");
          input.className = "fnr-input";
          options.forEach((opt) => {
            const o = document.createElement("option");
            o.value = opt;
            o.textContent = opt;
            input.appendChild(o);
          });
        } else if (type === "textarea") {
          input = document.createElement("textarea");
          input.className = "fnr-input";
          input.rows = 3;
        } else {
          input = document.createElement("input");
          input.type = type || "text";
          input.className = "fnr-input";
          if (value !== undefined) input.value = value;
        }
        input.name = name;
        row.appendChild(input);
        promptFields.appendChild(row);
      });

      show(promptOverlay);

      const cleanup = () => {
        hide(promptOverlay);
        promptOk.removeEventListener("click", onOk);
        promptCancel.removeEventListener("click", onCancel);
      };
      const onOk = () => {
        const result = {};
        fields.forEach(({ name }) => {
          const el = promptFields.querySelector(`[name="${name}"]`);
          result[name] = el ? el.value : "";
        });
        cleanup();
        resolve(result);
      };
      const onCancel = () => { cleanup(); resolve(null); };
      promptOk.addEventListener("click", onOk);
      promptCancel.addEventListener("click", onCancel);
    });
  }

  // -------------------------------------------------------------------------
  // Notification helper
  // -------------------------------------------------------------------------

  function notify(msg, isError = false) {
    // Reuse the status bar for brief notifications
    statusText.textContent = msg;
    statusText.style.color = isError ? "var(--danger)" : "var(--ok)";
    setTimeout(() => {
      statusText.style.color = "";
      updateStatusBar();
    }, 3000);
  }

  // -------------------------------------------------------------------------
  // Search helpers
  // -------------------------------------------------------------------------

  async function search() {
    const q = searchInput.value.trim();
    S.query = q;
    S.offset = 0;
    S.selected.clear();
    await fetchPage();
  }

  async function fetchPage() {
    show(browseLoading);
    hide(browseEmpty);
    hide(resultsSection);
    hide(statusBar);
    hide(bulkToolbar);
    hide(browseTools);

    let pageResult;
    try {
      pageResult = await acsInvoke("findCardsPaginated", {
        query: S.query,
        offset: S.offset,
        limit: S.pageSize,
      });
    } catch (e) {
      hide(browseLoading);
      notify("Search failed: " + e.message, true);
      return;
    }

    S.total   = pageResult.total || 0;
    S.cardIds = pageResult.cards || [];

    hide(browseLoading);

    if (S.total === 0) {
      show(browseEmpty);
      show(statusBar);
      statusText.textContent = "No cards matched";
      return;
    }

    // Fetch card details for this page
    let cards = [];
    try {
      cards = await acsInvoke("cardsInfo", { cards: S.cardIds });
    } catch (e) {
      notify("Failed to load card details: " + e.message, true);
      return;
    }

    S.cardsData = cards;
    S.selected.clear();

    renderTable();
    show(resultsSection);
    show(statusBar);
    show(browseTools);
    // Show FnR if already open
    if (!fnrSection.hidden) {
      updateFnrScope();
    }
    updateStatusBar();
    updatePagination();
  }

  // -------------------------------------------------------------------------
  // Table rendering
  // -------------------------------------------------------------------------

  function renderTable() {
    let rows = [...S.cardsData];

    // Client-side sort
    if (S.sortCol) {
      rows.sort((a, b) => {
        const va = sortVal(a, S.sortCol);
        const vb = sortVal(b, S.sortCol);
        if (va < vb) return -S.sortDir;
        if (va > vb) return S.sortDir;
        return 0;
      });
    }

    // Update sort indicators in headers
    document.querySelectorAll(".col-sort").forEach((th) => {
      th.classList.remove("sort-asc", "sort-desc");
      if (th.dataset.col === S.sortCol) {
        th.classList.add(S.sortDir > 0 ? "sort-asc" : "sort-desc");
      }
    });

    resultsTbody.innerHTML = rows.map((card) => {
      const state   = cardState(card.queue, card.type);
      const firstFv = Object.values(card.fields || {})[0];
      const firstF  = firstFv ? firstFv.value : "";
      // Strip HTML tags for table display
      const firstFText = firstF.replace(/<[^>]+>/g, "");
      const checked  = S.selected.has(card.cardId) ? "checked" : "";

      return `<tr class="result-row${S.selected.has(card.cardId) ? " row-selected" : ""}"
                  data-cid="${card.cardId}" data-nid="${card.note}">
        <td class="col-check">
          <input type="checkbox" class="row-check" data-cid="${card.cardId}" ${checked} />
        </td>
        <td class="col-first-field" title="${esc(firstF)}">${esc(firstFText.slice(0, 80))}${firstFText.length > 80 ? "…" : ""}</td>
        <td>${esc(card.deckName)}</td>
        <td>${esc(card.modelName)}</td>
        <td>${card.due}</td>
        <td>${card.interval}</td>
        <td>${card.reps}</td>
        <td>${card.lapses}</td>
        <td>${flagSwatch(card.flags)}${card.flags ? card.flags : ""}</td>
        <td><span class="state-badge state-${esc(state)}">${esc(state)}</span></td>
      </tr>`;
    }).join("");

    selectAll.checked = false;

    // Attach row events
    resultsTbody.querySelectorAll(".result-row").forEach((row) => {
      const cid = Number(row.dataset.cid);
      const nid = Number(row.dataset.nid);

      // Click on row (not checkbox) opens editor
      row.addEventListener("click", (e) => {
        if (e.target.classList.contains("row-check")) return;
        openEditor(nid);
      });

      // Checkbox changes selection
      const chk = row.querySelector(".row-check");
      chk.addEventListener("change", () => {
        if (chk.checked) {
          S.selected.add(cid);
          row.classList.add("row-selected");
        } else {
          S.selected.delete(cid);
          row.classList.remove("row-selected");
        }
        updateStatusBar();
      });
    });
  }

  // -------------------------------------------------------------------------
  // Status bar + pagination
  // -------------------------------------------------------------------------

  function updateStatusBar() {
    const page   = Math.floor(S.offset / S.pageSize) + 1;
    const pages  = Math.ceil(S.total / S.pageSize) || 1;
    statusText.textContent = `${S.total} cards total — page ${page} of ${pages}`;
    selectionText.textContent = `${S.selected.size} selected`;
    bulkToolbar.hidden = S.selected.size === 0;
  }

  function updatePagination() {
    const page  = Math.floor(S.offset / S.pageSize) + 1;
    const pages = Math.ceil(S.total / S.pageSize) || 1;
    pageInfo.textContent = `Page ${page} of ${pages}`;
    prevBtn.disabled = S.offset === 0;
    nextBtn.disabled = S.offset + S.pageSize >= S.total;
  }

  // -------------------------------------------------------------------------
  // Select all on page
  // -------------------------------------------------------------------------

  selectAll.addEventListener("change", () => {
    const checked = selectAll.checked;
    resultsTbody.querySelectorAll(".row-check").forEach((chk) => {
      const cid = Number(chk.dataset.cid);
      chk.checked = checked;
      const row = chk.closest(".result-row");
      if (checked) {
        S.selected.add(cid);
        row.classList.add("row-selected");
      } else {
        S.selected.delete(cid);
        row.classList.remove("row-selected");
      }
    });
    updateStatusBar();
  });

  // -------------------------------------------------------------------------
  // Pagination controls
  // -------------------------------------------------------------------------

  prevBtn.addEventListener("click", () => {
    if (S.offset > 0) {
      S.offset = Math.max(0, S.offset - S.pageSize);
      fetchPage();
    }
  });

  nextBtn.addEventListener("click", () => {
    if (S.offset + S.pageSize < S.total) {
      S.offset += S.pageSize;
      fetchPage();
    }
  });

  // -------------------------------------------------------------------------
  // Sort columns
  // -------------------------------------------------------------------------

  document.querySelectorAll(".col-sort").forEach((th) => {
    th.style.cursor = "pointer";
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      if (S.sortCol === col) {
        S.sortDir *= -1;
      } else {
        S.sortCol = col;
        S.sortDir = 1;
      }
      renderTable();
    });
  });

  // -------------------------------------------------------------------------
  // Search form
  // -------------------------------------------------------------------------

  searchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    S.pageSize = Number(pageSizeSelect.value) || 50;
    search();
  });

  pageSizeSelect.addEventListener("change", () => {
    S.pageSize = Number(pageSizeSelect.value) || 50;
    S.offset = 0;
    if (S.query) fetchPage();
  });

  // Help toggle
  const helpToggle = document.getElementById("help-toggle");
  const helpBody   = document.getElementById("help-body");
  helpToggle.addEventListener("click", () => {
    helpBody.hidden = !helpBody.hidden;
    helpToggle.setAttribute("aria-expanded", String(!helpBody.hidden));
  });
  helpToggle.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      helpToggle.click();
    }
  });

  // -------------------------------------------------------------------------
  // Bulk actions
  // -------------------------------------------------------------------------

  /** Return the card ids for selected cards. */
  function selectedCardIds() {
    return [...S.selected];
  }

  /** Return note ids corresponding to selected cards. */
  function selectedNoteIds() {
    const nids = new Set();
    S.cardsData.forEach((c) => {
      if (S.selected.has(c.cardId)) nids.add(c.note);
    });
    return [...nids];
  }

  bulkToolbar.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-bulk]");
    if (!btn) return;
    await handleBulkAction(btn.dataset.bulk);
  });

  async function handleBulkAction(action) {
    const cardIds = selectedCardIds();
    const noteIds = selectedNoteIds();
    const n = cardIds.length;
    if (n === 0) return;

    let ok, input;

    switch (action) {

      case "suspend":
        ok = await confirm(
          "Suspend cards",
          `Suspend ${n} card(s)? They will not appear in reviews until unsuspended.`,
          "Suspend"
        );
        if (!ok) return;
        await acsInvoke("suspend", { cards: cardIds });
        notify(`Suspended ${n} card(s)`);
        break;

      case "unsuspend":
        ok = await confirm(
          "Unsuspend cards",
          `Unsuspend ${n} card(s)?`,
          "Unsuspend", "btn btn-primary"
        );
        if (!ok) return;
        await acsInvoke("unsuspend", { cards: cardIds });
        notify(`Unsuspended ${n} card(s)`);
        break;

      case "bury":
        ok = await confirm(
          "Bury cards",
          `Bury ${n} card(s) until tomorrow?`,
          "Bury"
        );
        if (!ok) return;
        await acsInvoke("bury", { cards: cardIds });
        notify(`Buried ${n} card(s)`);
        break;

      case "unbury":
        ok = await confirm(
          "Unbury cards",
          `Unbury ${n} card(s)?`,
          "Unbury", "btn btn-primary"
        );
        if (!ok) return;
        await acsInvoke("unbury", { cards: cardIds });
        notify(`Unburied ${n} card(s)`);
        break;

      case "setFlag":
        input = await prompt("Set flag", [
          {
            name: "flag",
            label: "Flag (0 = none, 1 = red, 2 = orange, 3 = green, 4 = blue, 5 = purple, 6 = pink, 7 = gray)",
            type: "select",
            options: ["0","1","2","3","4","5","6","7"],
          },
        ]);
        if (!input) return;
        ok = await confirm(
          "Set flag",
          `Set flag ${input.flag} on ${n} card(s)?`,
          "Set flag", "btn btn-primary"
        );
        if (!ok) return;
        await Promise.all(
          cardIds.map((cid) =>
            acsInvoke("setSpecificValueOfCard", {
              card: cid,
              keys: ["flags"],
              newValues: [Number(input.flag)],
            })
          )
        );
        notify(`Flag set on ${n} card(s)`);
        break;

      case "changeDeck":
        input = await prompt("Change deck", [
          {
            name: "deck",
            label: "Destination deck",
            type: "select",
            options: DECK_NAMES.length ? DECK_NAMES : ["Default"],
          },
        ]);
        if (!input) return;
        ok = await confirm(
          "Change deck",
          `Move ${n} card(s) to deck "${input.deck}"?`,
          "Move", "btn btn-primary"
        );
        if (!ok) return;
        await acsInvoke("changeDeck", { cards: cardIds, deck: input.deck });
        notify(`Moved ${n} card(s) to "${input.deck}"`);
        break;

      case "setDueDate":
        input = await prompt("Set due date", [
          {
            name: "days",
            label: 'Day spec (e.g. "1", "3-7", "0" = today)',
            type: "text",
            value: "1",
          },
        ]);
        if (!input) return;
        ok = await confirm(
          "Set due date",
          `Set due date to "${input.days}" for ${n} card(s)?`,
          "Set due", "btn btn-primary"
        );
        if (!ok) return;
        await acsInvoke("setDueDate", { cards: cardIds, days: input.days });
        notify(`Due date set for ${n} card(s)`);
        break;

      case "forgetCards":
        ok = await confirm(
          "Forget cards (RESET)",
          `Reset ${n} card(s) to new status? This removes all review history ` +
          "and resets intervals to zero. This action cannot be undone without a backup.",
          "Reset cards"
        );
        if (!ok) return;
        await acsInvoke("forgetCards", {
          cards: cardIds,
          restorePosition: false,
          resetCounts: true,
        });
        notify(`Reset ${n} card(s) to new`);
        break;

      case "reposition":
        input = await prompt("Reposition new cards", [
          { name: "start", label: "Starting position", type: "number", value: "1" },
          { name: "step",  label: "Step",              type: "number", value: "1" },
        ]);
        if (!input) return;
        ok = await confirm(
          "Reposition new cards",
          `Reposition ${n} new card(s) starting at ${input.start}?`,
          "Reposition", "btn btn-primary"
        );
        if (!ok) return;
        await acsInvoke("reposition", {
          cards: cardIds,
          start: Number(input.start),
          step: Number(input.step),
          randomize: false,
          shiftExisting: true,
        });
        notify(`Repositioned ${n} card(s)`);
        break;

      case "addTags":
        input = await prompt("Add tags", [
          { name: "tags", label: "Tags (space-separated)", type: "text" },
        ]);
        if (!input || !input.tags.trim()) return;
        ok = await confirm(
          "Add tags",
          `Add tags "${input.tags}" to ${noteIds.length} note(s)?`,
          "Add", "btn btn-primary"
        );
        if (!ok) return;
        await acsInvoke("addTags", { notes: noteIds, tags: input.tags });
        notify(`Tags added to ${noteIds.length} note(s)`);
        break;

      case "removeTags":
        input = await prompt("Remove tags", [
          { name: "tags", label: "Tags to remove (space-separated)", type: "text" },
        ]);
        if (!input || !input.tags.trim()) return;
        ok = await confirm(
          "Remove tags",
          `Remove tags "${input.tags}" from ${noteIds.length} note(s)?`,
          "Remove", "btn btn-danger"
        );
        if (!ok) return;
        await acsInvoke("removeTags", { notes: noteIds, tags: input.tags });
        notify(`Tags removed from ${noteIds.length} note(s)`);
        break;

      case "deleteNotes": {
        const nCount = noteIds.length;
        // Double-confirm for destructive delete
        const first = await confirm(
          "Delete notes",
          `Permanently delete ${nCount} note(s) and all their cards? This cannot be undone.`,
          "Delete permanently"
        );
        if (!first) return;
        const second = await confirm(
          "CONFIRM DELETE",
          `Are you absolutely sure you want to permanently delete ${nCount} note(s)? ` +
          "There is no undo. All cards for these notes will also be deleted.",
          "Yes, delete permanently"
        );
        if (!second) return;
        await acsInvoke("deleteNotes", { notes: noteIds });
        notify(`Deleted ${nCount} note(s)`);
        S.selected.clear();
        await fetchPage();
        return;
      }

      default:
        notify("Unknown action: " + action, true);
        return;
    }

    // Refresh the current page after any successful action
    try {
      await fetchPage();
    } catch (_) {
      // Non-fatal -- table may be stale but user was notified
    }
  }

  // -------------------------------------------------------------------------
  // Note editor
  // -------------------------------------------------------------------------

  async function openEditor(noteId) {
    editorStatus.textContent = "";
    editorMeta.innerHTML = "";
    editorFields.innerHTML = "<p class='editor-loading'>Loading&hellip;</p>";
    editorTagsInput.value = "";
    S.editorNoteId = noteId;
    show(editorOverlay);

    let notes;
    try {
      notes = await acsInvoke("notesInfo", { notes: [noteId] });
    } catch (e) {
      editorFields.innerHTML = `<p class="editor-error">Failed to load note: ${esc(e.message)}</p>`;
      return;
    }

    if (!notes || notes.length === 0) {
      editorFields.innerHTML = "<p class='editor-error'>Note not found.</p>";
      return;
    }

    const note = notes[0];
    S.editorOrigFields = {};
    S.editorOrigTags   = [...(note.tags || [])];

    editorMeta.innerHTML =
      `<div class="editor-meta-row"><span class="meta-label">Note ID:</span> ${esc(String(note.noteId))}</div>` +
      `<div class="editor-meta-row"><span class="meta-label">Type:</span> ${esc(note.modelName)}</div>` +
      `<div class="editor-meta-row"><span class="meta-label">Cards:</span> ${(note.cards || []).length}</div>`;

    // Render fields sorted by their order
    const fieldEntries = Object.entries(note.fields || {})
      .sort(([, a], [, b]) => a.order - b.order);

    editorFields.innerHTML = fieldEntries.map(([fname, fdata]) => {
      S.editorOrigFields[fname] = fdata.value;
      return `<div class="editor-field">
        <label class="editor-field-label">${esc(fname)}</label>
        <textarea class="editor-field-input" data-field="${esc(fname)}" rows="3">${esc(fdata.value)}</textarea>
      </div>`;
    }).join("");

    editorTagsInput.value = (note.tags || []).join(" ");
  }

  editorClose.addEventListener("click", () => {
    hide(editorOverlay);
    S.editorNoteId = null;
  });

  editorOverlay.addEventListener("click", (e) => {
    if (e.target === editorOverlay) {
      hide(editorOverlay);
    }
  });

  editorSaveBtn.addEventListener("click", async () => {
    if (!S.editorNoteId) return;
    editorStatus.textContent = "Saving...";
    editorStatus.style.color = "";

    // Collect changed fields
    const newFields = {};
    editorFields.querySelectorAll(".editor-field-input").forEach((ta) => {
      const fname = ta.dataset.field;
      const val   = ta.value;
      if (val !== S.editorOrigFields[fname]) {
        newFields[fname] = val;
      }
    });

    // Tag diff
    const newTagStr  = editorTagsInput.value.trim();
    const newTagsArr = newTagStr ? newTagStr.split(/\s+/) : [];
    const origSet    = new Set(S.editorOrigTags);
    const newSet     = new Set(newTagsArr);
    const toAdd      = newTagsArr.filter((t) => !origSet.has(t));
    const toRemove   = S.editorOrigTags.filter((t) => !newSet.has(t));

    try {
      if (Object.keys(newFields).length > 0) {
        await acsInvoke("updateNoteFields", {
          note: { id: S.editorNoteId, fields: newFields },
        });
      }
      if (toAdd.length > 0) {
        await acsInvoke("addTags", {
          notes: [S.editorNoteId],
          tags: toAdd.join(" "),
        });
      }
      if (toRemove.length > 0) {
        await acsInvoke("removeTags", {
          notes: [S.editorNoteId],
          tags: toRemove.join(" "),
        });
      }
      editorStatus.textContent = "Saved.";
      editorStatus.style.color = "var(--ok)";
      // Update orig state
      S.editorOrigFields = { ...S.editorOrigFields, ...newFields };
      S.editorOrigTags   = newTagsArr;
      // Silently refresh table
      fetchPage();
    } catch (e) {
      editorStatus.textContent = "Save failed: " + e.message;
      editorStatus.style.color = "var(--danger)";
    }
  });

  editorDeleteBtn.addEventListener("click", async () => {
    if (!S.editorNoteId) return;
    const ok = await confirm(
      "Delete note",
      `Permanently delete note ${S.editorNoteId} and all its cards?`,
      "Delete"
    );
    if (!ok) return;
    const second = await confirm(
      "CONFIRM DELETE",
      "Are you sure you want to permanently delete this note? This cannot be undone.",
      "Yes, delete"
    );
    if (!second) return;
    try {
      await acsInvoke("deleteNotes", { notes: [S.editorNoteId] });
      hide(editorOverlay);
      S.editorNoteId = null;
      await fetchPage();
    } catch (e) {
      editorStatus.textContent = "Delete failed: " + e.message;
      editorStatus.style.color = "var(--danger)";
    }
  });

  // -------------------------------------------------------------------------
  // Find & Replace
  // -------------------------------------------------------------------------

  toggleFnrBtn.addEventListener("click", () => {
    if (fnrSection.hidden) {
      show(fnrSection);
      updateFnrScope();
      toggleFnrBtn.textContent = "Hide Find & Replace";
    } else {
      hide(fnrSection);
      toggleFnrBtn.textContent = "Find & Replace";
    }
  });

  function updateFnrScope() {
    fnrScope.textContent = `(${S.total} cards in current search)`;
  }

  fnrApplyBtn.addEventListener("click", async () => {
    const searchVal  = fnrSearch.value;
    const replaceVal = fnrReplace.value;
    const field      = fnrField.value || null;
    const useRegex   = fnrRegex.checked;
    const matchCase  = fnrCase.checked;

    if (!searchVal) {
      notify("Enter text to search for", true);
      return;
    }

    // Get note ids from current result set (all pages? -- scope to current page for safety)
    // Using current page card ids -> note ids
    const noteIds = [...new Set(S.cardsData.map((c) => c.note))];

    const ok = await confirm(
      "Find & Replace",
      `Replace "${searchVal}" with "${replaceVal}" ` +
      `in ${field ? `field "${field}"` : "all fields"} ` +
      `across ${noteIds.length} note(s) on the current page?`,
      "Apply", "btn btn-primary"
    );
    if (!ok) return;

    try {
      const count = await acsInvoke("findAndReplace", {
        notes: noteIds,
        search: searchVal,
        replacement: replaceVal,
        regex: useRegex,
        field: field,
        matchCase: matchCase,
      });
      notify(`Find & Replace: ${count} note(s) modified`);
      await fetchPage();
    } catch (e) {
      notify("Find & Replace failed: " + e.message, true);
    }
  });

  // -------------------------------------------------------------------------
  // Find Duplicates
  // -------------------------------------------------------------------------

  toggleDupesBtn.addEventListener("click", () => {
    if (dupesSection.hidden) {
      show(dupesSection);
      dupesResults.hidden = true;
      toggleDupesBtn.textContent = "Hide Find Duplicates";
    } else {
      hide(dupesSection);
      toggleDupesBtn.textContent = "Find Duplicates";
    }
  });

  dupesRunBtn.addEventListener("click", async () => {
    const field = dupesField.value;
    if (!field) {
      notify("Select a field first", true);
      return;
    }
    dupesResults.hidden = true;
    dupesRunBtn.disabled = true;
    dupesRunBtn.textContent = "Searching...";

    try {
      const dupes = await acsInvoke("findDuplicates", { field, search: S.query });

      if (!dupes || dupes.length === 0) {
        dupesResults.innerHTML = "<p>No duplicates found.</p>";
      } else {
        dupesResults.innerHTML = `<p><strong>${dupes.length}</strong> duplicate group(s) found in field "<strong>${esc(field)}</strong>":</p>` +
          '<ul class="dupes-list">' +
          dupes.map(({ value, notes }) =>
            `<li>
              <span class="dupe-value">${esc(value.slice(0, 60))}${value.length > 60 ? "..." : ""}</span>
              &mdash; ${notes.length} notes
              <a href="#" class="dupe-search-link" data-query="dupe:${field}">filter</a>
            </li>`
          ).join("") +
          "</ul>";

        // Wire filter links
        dupesResults.querySelectorAll(".dupe-search-link").forEach((a) => {
          a.addEventListener("click", (e) => {
            e.preventDefault();
            searchInput.value = a.dataset.query;
            search();
          });
        });
      }
      show(dupesResults);
    } catch (e) {
      dupesResults.innerHTML = `<p class="editor-error">Error: ${esc(e.message)}</p>`;
      show(dupesResults);
    } finally {
      dupesRunBtn.disabled = false;
      dupesRunBtn.textContent = "Find Duplicates";
    }
  });

  // -------------------------------------------------------------------------
  // Init: run default search on page load (show all due cards)
  // -------------------------------------------------------------------------

  searchInput.value = "is:due or is:new";
  search();

})();
