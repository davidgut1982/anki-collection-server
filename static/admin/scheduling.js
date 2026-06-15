/**
 * scheduling.js -- Scheduling admin page logic (/admin/scheduling, A7).
 *
 * All server calls go through acsInvoke() from admin.js (token-gated proxy).
 * No external JS dependencies -- vanilla ES2020.
 *
 * Read-modify-write pattern for deck options:
 *   1. getDeckConfigs -> populate preset selector dropdown.
 *   2. On preset select: find the matching full config dict from the already-
 *      fetched list (no extra round-trip needed).
 *   3. Bind form fields to the nested camelCase keys inside that config dict.
 *   4. On save: copy the stored config dict, mutate only the form-bound fields,
 *      and POST the WHOLE dict back via updateDeckConfig.  No fields are lost.
 *
 * FSRS panel:
 *   - isFsrsEnabled -> show/hide enable button.
 *   - getFsrsParams (deck=first preset) -> display retention + param vector.
 *   - Enable FSRS button -> confirm -> enableFsrs(optimize=true).
 *   - Save retention -> confirm -> setDesiredRetention.
 *   - Compute optimal -> computeOptimalRetention -> display suggestion.
 */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  /** @type {Array<Object>} All raw config dicts from getDeckConfigs */
  let allPresets = [];

  /** @type {Object|null} The currently selected raw config dict */
  let currentConfig = null;

  // -------------------------------------------------------------------------
  // DOM refs
  // -------------------------------------------------------------------------

  const presetSelect        = document.getElementById("preset-select");
  const presetIdLabel       = document.getElementById("preset-id-label");
  const presetHint          = document.getElementById("preset-hint");
  const deckOptionsCard     = document.getElementById("deck-options-card");
  const formPresetName      = document.getElementById("form-preset-name");
  const deckOptionsForm     = document.getElementById("deck-options-form");
  const saveBtn             = document.getElementById("save-deck-options-btn");
  const deckOptionsStatus   = document.getElementById("deck-options-status");

  // New cards
  const newPerDay           = document.getElementById("new-per-day");
  const newDelays           = document.getElementById("new-delays");
  const graduatingInterval  = document.getElementById("graduating-interval");
  const easyInterval        = document.getElementById("easy-interval");
  const buryNew             = document.getElementById("bury-new");

  // Reviews
  const revPerDay           = document.getElementById("rev-per-day");
  const revMaxIvl           = document.getElementById("rev-max-ivl");
  const revIvlFct           = document.getElementById("rev-ivl-fct");
  const buryRev             = document.getElementById("bury-rev");

  // Lapses
  const lapseDelays         = document.getElementById("lapse-delays");
  const leechFails          = document.getElementById("leech-fails");
  const leechAction         = document.getElementById("leech-action");
  const lapseMinInt         = document.getElementById("lapse-min-int");
  const lapseMult           = document.getElementById("lapse-mult");

  // FSRS panel
  const fsrsCard            = document.getElementById("fsrs-card");
  const fsrsStatusBadge     = document.getElementById("fsrs-status-badge");
  const fsrsEnableRow       = document.getElementById("fsrs-enable-row");
  const fsrsEnabledNote     = document.getElementById("fsrs-enabled-note");
  const enableFsrsBtn       = document.getElementById("enable-fsrs-btn");
  const fsrsRetentionSection= document.getElementById("fsrs-retention-section");
  const desiredRetention    = document.getElementById("desired-retention");
  const saveRetentionBtn    = document.getElementById("save-retention-btn");
  const retentionStatus     = document.getElementById("retention-status");
  const fsrsParamsSection   = document.getElementById("fsrs-params-section");
  const fsrsParamsDisplay   = document.getElementById("fsrs-params-display");
  const fsrsOptimalSection  = document.getElementById("fsrs-optimal-section");
  const computeRetentionBtn = document.getElementById("compute-retention-btn");
  const optimalRetentionResult = document.getElementById("optimal-retention-result");

  // Confirm dialog
  const confirmOverlay      = document.getElementById("confirm-overlay");
  const confirmTitle        = document.getElementById("confirm-title");
  const confirmBody         = document.getElementById("confirm-body");
  const confirmOk           = document.getElementById("confirm-ok");
  const confirmCancel       = document.getElementById("confirm-cancel");

  // -------------------------------------------------------------------------
  // Confirm dialog (promise-based, mirrors browse.js pattern)
  // -------------------------------------------------------------------------

  /**
   * Show a modal confirm dialog.
   * @param {string} title
   * @param {string} body - plain text; set via textContent (no XSS risk)
   * @param {string} [okLabel]
   * @param {string} [okClass]
   * @returns {Promise<boolean>}
   */
  function confirm(title, body, okLabel = "Confirm", okClass = "btn btn-danger") {
    return new Promise((resolve) => {
      confirmTitle.textContent = title;
      confirmBody.textContent = body;
      confirmOk.textContent = okLabel;
      confirmOk.className = okClass;
      confirmOverlay.hidden = false;
      const cleanup = () => {
        confirmOverlay.hidden = true;
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
  // Notify helper (inline status messages, auto-clear)
  // -------------------------------------------------------------------------

  /**
   * Display a transient status message on an element.
   * @param {HTMLElement} el
   * @param {string} msg
   * @param {'ok'|'err'|'info'} [type]
   * @param {number} [ms]  - auto-clear after ms (0 = no auto-clear)
   */
  function notify(el, msg, type = "ok", ms = 4000) {
    el.textContent = msg;
    el.className = "sched-status sched-status-" + type;
    if (ms > 0) {
      setTimeout(() => {
        if (el.textContent === msg) {
          el.textContent = "";
          el.className = "sched-status";
        }
      }, ms);
    }
  }

  // -------------------------------------------------------------------------
  // Parsing helpers
  // -------------------------------------------------------------------------

  /**
   * Parse a space-separated minutes string into a list of numbers.
   * Returns null if any token is not a valid positive number.
   * @param {string} raw
   * @returns {number[]|null}
   */
  function parseDelays(raw) {
    const tokens = raw.trim().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) return [];
    const nums = tokens.map(Number);
    if (nums.some((n) => isNaN(n) || n <= 0)) return null;
    return nums;
  }

  /**
   * Format a delays array as a space-separated string.
   * @param {number[]} delays
   * @returns {string}
   */
  function formatDelays(delays) {
    if (!Array.isArray(delays)) return "";
    return delays.join(" ");
  }

  // -------------------------------------------------------------------------
  // Form population
  // -------------------------------------------------------------------------

  /**
   * Populate all form fields from a raw config dict.
   * @param {Object} cfg - raw DeckConfigDict from getDeckConfigs
   */
  function populateForm(cfg) {
    const nw = cfg.new || {};
    const rv = cfg.rev || {};
    const lp = cfg.lapse || {};

    newPerDay.value         = nw.perDay != null ? nw.perDay : "";
    newDelays.value         = formatDelays(nw.delays || []);
    // ints: [easyInterval, graduatingInterval, ...]
    const ints = nw.ints || [1, 4, 0];
    easyInterval.value      = ints[0] != null ? ints[0] : "";
    graduatingInterval.value= ints[1] != null ? ints[1] : "";
    buryNew.checked         = Boolean(nw.bury);

    revPerDay.value         = rv.perDay != null ? rv.perDay : "";
    revMaxIvl.value         = rv.maxIvl != null ? rv.maxIvl : "";
    // ivlFct stored as fraction (e.g. 1.0 = 100%); display as percentage integer
    revIvlFct.value         = rv.ivlFct != null ? Math.round(rv.ivlFct * 100) : 100;
    buryRev.checked         = Boolean(rv.bury);

    lapseDelays.value       = formatDelays(lp.delays || []);
    leechFails.value        = lp.leechFails != null ? lp.leechFails : "";
    leechAction.value       = lp.leechAction != null ? String(lp.leechAction) : "0";
    lapseMinInt.value       = lp.minInt != null ? lp.minInt : "";
    // mult stored as fraction (e.g. 0.0 = 0%); display as percentage integer
    lapseMult.value         = lp.mult != null ? Math.round(lp.mult * 100) : 0;
  }

  // -------------------------------------------------------------------------
  // Form validation
  // -------------------------------------------------------------------------

  /**
   * Validate all form inputs.  Returns an error string or null if valid.
   * @returns {string|null}
   */
  function validateForm() {
    const errors = [];

    const npd = parseInt(newPerDay.value, 10);
    if (isNaN(npd) || npd < 0) errors.push("New cards per day must be >= 0.");

    const ndVal = parseDelays(newDelays.value);
    if (ndVal === null) errors.push("Learning steps must be positive numbers separated by spaces.");

    const grad = parseInt(graduatingInterval.value, 10);
    if (isNaN(grad) || grad < 1) errors.push("Graduating interval must be >= 1.");

    const easy = parseInt(easyInterval.value, 10);
    if (isNaN(easy) || easy < 1) errors.push("Easy interval must be >= 1.");

    const rpd = parseInt(revPerDay.value, 10);
    if (isNaN(rpd) || rpd < 0) errors.push("Reviews per day must be >= 0.");

    const maxIvl = parseInt(revMaxIvl.value, 10);
    if (isNaN(maxIvl) || maxIvl < 1) errors.push("Maximum interval must be >= 1.");

    const ivlPct = parseInt(revIvlFct.value, 10);
    if (isNaN(ivlPct) || ivlPct < 50 || ivlPct > 500) errors.push("Interval modifier must be 50–500%.");

    const ldVal = parseDelays(lapseDelays.value);
    if (ldVal === null) errors.push("Relearn steps must be positive numbers separated by spaces.");

    const lf = parseInt(leechFails.value, 10);
    if (isNaN(lf) || lf < 1) errors.push("Leech threshold must be >= 1.");

    const la = parseInt(leechAction.value, 10);
    if (la !== 0 && la !== 1) errors.push("Leech action must be 0 or 1.");

    const mi = parseInt(lapseMinInt.value, 10);
    if (isNaN(mi) || mi < 1) errors.push("Min interval after lapse must be >= 1.");

    const multPct = parseInt(lapseMult.value, 10);
    if (isNaN(multPct) || multPct < 0 || multPct > 100) errors.push("New interval % must be 0–100.");

    return errors.length ? errors.join("\n") : null;
  }

  // -------------------------------------------------------------------------
  // Build mutated config dict from form (read-modify-write)
  // -------------------------------------------------------------------------

  /**
   * Produce a mutated copy of the stored config dict with form values applied.
   * All other fields (FSRS params, metadata, rarely-edited keys) are preserved.
   * @returns {Object}
   */
  function buildMutatedConfig() {
    // Deep-copy only the sub-dicts we modify; leave all other top-level keys.
    const cfg = Object.assign({}, currentConfig);
    cfg.new = Object.assign({}, currentConfig.new || {});
    cfg.rev = Object.assign({}, currentConfig.rev || {});
    cfg.lapse = Object.assign({}, currentConfig.lapse || {});

    // New cards
    cfg.new.perDay  = parseInt(newPerDay.value, 10);
    cfg.new.delays  = parseDelays(newDelays.value) || [];
    cfg.new.bury    = buryNew.checked;

    // ints[0] = easyInterval, ints[1] = graduatingInterval, ints[2] preserved
    const origInts  = (currentConfig.new || {}).ints || [1, 4, 0];
    cfg.new.ints    = [
      parseInt(easyInterval.value, 10),
      parseInt(graduatingInterval.value, 10),
      origInts[2] != null ? origInts[2] : 0,
    ];

    // Reviews
    cfg.rev.perDay  = parseInt(revPerDay.value, 10);
    cfg.rev.maxIvl  = parseInt(revMaxIvl.value, 10);
    cfg.rev.ivlFct  = parseInt(revIvlFct.value, 10) / 100;  // % -> fraction
    cfg.rev.bury    = buryRev.checked;

    // Lapses
    cfg.lapse.delays      = parseDelays(lapseDelays.value) || [];
    cfg.lapse.leechFails  = parseInt(leechFails.value, 10);
    cfg.lapse.leechAction = parseInt(leechAction.value, 10);
    cfg.lapse.minInt      = parseInt(lapseMinInt.value, 10);
    cfg.lapse.mult        = parseInt(lapseMult.value, 10) / 100;  // % -> fraction

    return cfg;
  }

  // -------------------------------------------------------------------------
  // Preset selection
  // -------------------------------------------------------------------------

  function onPresetChange() {
    const idx = parseInt(presetSelect.value, 10);
    if (isNaN(idx) || idx < 0 || idx >= allPresets.length) {
      deckOptionsCard.hidden = true;
      fsrsCard.hidden = true;
      return;
    }
    // Snapshot the selected preset before any async work so that a fast
    // preset-switch while loadFsrsPanel() is in-flight cannot cause a race
    // where the FSRS panel shows data for a different preset than the form.
    currentConfig = { ...allPresets[idx] };

    // Update preset id label
    presetIdLabel.textContent = "(id " + currentConfig.id + ")";
    presetHint.hidden = false;

    // Show and populate deck options form
    formPresetName.textContent = currentConfig.name || "(unnamed)";
    populateForm(currentConfig);
    deckOptionsCard.hidden = false;
    deckOptionsStatus.textContent = "";

    // Show and populate FSRS panel
    fsrsCard.hidden = false;
    loadFsrsPanel();
  }

  // -------------------------------------------------------------------------
  // FSRS panel
  // -------------------------------------------------------------------------

  async function loadFsrsPanel() {
    fsrsStatusBadge.textContent = "checking…";
    fsrsStatusBadge.className = "badge";
    fsrsRetentionSection.hidden = true;
    fsrsParamsSection.hidden = true;
    fsrsOptimalSection.hidden = true;

    try {
      const enabled = await acsInvoke("isFsrsEnabled", {});

      if (enabled) {
        fsrsStatusBadge.textContent = "enabled";
        fsrsStatusBadge.className = "badge badge-ok";
        fsrsEnableRow.hidden = true;
        fsrsEnabledNote.hidden = false;
      } else {
        fsrsStatusBadge.textContent = "disabled (SM-2)";
        fsrsStatusBadge.className = "badge badge-muted";
        fsrsEnableRow.hidden = false;
        fsrsEnabledNote.hidden = true;
      }

      // Load FSRS params for the current preset's deck id
      // We use the preset's id as a proxy: find first deck using this preset
      // by using the preset name as a lookup. Fallback to "Default".
      const deckName = currentConfig.name || "Default";
      let fsrsParams = null;
      try {
        fsrsParams = await acsInvoke("getFsrsParams", { deck: deckName });
      } catch (_) {
        // preset name may not match a deck name; fall back to default
        try {
          fsrsParams = await acsInvoke("getFsrsParams", { deck: "Default" });
        } catch (e2) {
          // best effort
          fsrsParams = null;
        }
      }

      if (fsrsParams) {
        // Desired retention
        desiredRetention.value = (fsrsParams.desiredRetention || 0.9).toFixed(2);
        fsrsRetentionSection.hidden = false;
        retentionStatus.textContent = "";

        // FSRS param vector
        renderFsrsParams(fsrsParams.params || []);
        fsrsParamsSection.hidden = false;

        // Compute optimal retention section
        fsrsOptimalSection.hidden = false;
      }
    } catch (err) {
      fsrsStatusBadge.textContent = "error: " + err.message;
      fsrsStatusBadge.className = "badge badge-err";
    }
  }

  /**
   * Render the 21-float FSRS parameter vector as a read-only grid.
   * @param {number[]} params
   */
  function renderFsrsParams(params) {
    fsrsParamsDisplay.innerHTML = "";
    if (!params || params.length === 0) {
      fsrsParamsDisplay.textContent = "(no parameters stored; Anki built-in defaults will be used)";
      return;
    }
    const grid = document.createElement("div");
    grid.className = "fsrs-params-grid";
    params.forEach((val, idx) => {
      const cell = document.createElement("span");
      cell.className = "fsrs-param-cell";
      cell.title = "w" + idx;
      cell.textContent = (typeof val === "number") ? val.toFixed(4) : String(val);
      grid.appendChild(cell);
    });
    fsrsParamsDisplay.appendChild(grid);
    const note = document.createElement("p");
    note.className = "sched-hint";
    note.textContent = params.length + " parameters (FSRS-" + (params.length === 21 ? "6" : params.length === 17 ? "5" : params.length) + ")";
    fsrsParamsDisplay.appendChild(note);
  }

  // -------------------------------------------------------------------------
  // Enable FSRS
  // -------------------------------------------------------------------------

  enableFsrsBtn.addEventListener("click", async () => {
    const ok = await confirm(
      "Enable FSRS",
      "This will enable FSRS and optimize its parameters from your full review history. " +
      "Scheduling changes will affect all devices on their next sync. " +
      "The optimizer requires sufficient review history to succeed. Proceed?",
      "Enable FSRS + Optimize",
      "btn btn-warn"
    );
    if (!ok) return;

    enableFsrsBtn.disabled = true;
    enableFsrsBtn.textContent = "Enabling…";

    try {
      const result = await acsInvoke("enableFsrs", { optimize: true });
      if (result && result.enabled) {
        notify(deckOptionsStatus, "", "ok", 0);  // clear any stale message
        // Reload FSRS panel to reflect new state
        await loadFsrsPanel();
      } else {
        const errMsg = (result && result.error) ? result.error : "Unknown error";
        notify(retentionStatus, "Enable FSRS failed: " + errMsg, "err", 0);
      }
    } catch (err) {
      notify(retentionStatus, "Enable FSRS error: " + err.message, "err", 0);
    } finally {
      enableFsrsBtn.disabled = false;
      enableFsrsBtn.textContent = "Enable FSRS";
    }
  });

  // -------------------------------------------------------------------------
  // Save desired retention
  // -------------------------------------------------------------------------

  saveRetentionBtn.addEventListener("click", async () => {
    const val = parseFloat(desiredRetention.value);
    if (isNaN(val) || val < 0.70 || val > 0.97) {
      notify(retentionStatus, "Retention must be between 0.70 and 0.97.", "err");
      return;
    }

    const presetName = (currentConfig && currentConfig.name) ? currentConfig.name : "selected preset";
    const ok = await confirm(
      "Save Desired Retention",
      "Set desired retention to " + val.toFixed(2) + " for preset \"" + presetName + "\"? " +
      "This affects all decks using this preset on their next sync.",
      "Save",
      "btn btn-primary"
    );
    if (!ok) return;

    saveRetentionBtn.disabled = true;
    try {
      // setDesiredRetention takes a deck name; find the matching deck or use Default
      const deckName = currentConfig.name || "Default";
      let invoked = false;
      try {
        await acsInvoke("setDesiredRetention", { deck: deckName, retention: val });
        invoked = true;
      } catch (_) {
        // preset name != deck name; try Default
        await acsInvoke("setDesiredRetention", { deck: "Default", retention: val });
        invoked = true;
      }
      if (invoked) {
        notify(retentionStatus, "Retention saved: " + val.toFixed(2), "ok");
        // Also update the stored config's desiredRetention so save-config round-trips stay consistent
        if (currentConfig) currentConfig.desiredRetention = val;
      }
    } catch (err) {
      notify(retentionStatus, "Save failed: " + err.message, "err");
    } finally {
      saveRetentionBtn.disabled = false;
    }
  });

  // -------------------------------------------------------------------------
  // Compute optimal retention
  // -------------------------------------------------------------------------

  computeRetentionBtn.addEventListener("click", async () => {
    computeRetentionBtn.disabled = true;
    computeRetentionBtn.textContent = "Computing…";
    optimalRetentionResult.hidden = true;

    try {
      const deckName = currentConfig ? (currentConfig.name || "Default") : "Default";
      let result = null;
      try {
        result = await acsInvoke("computeOptimalRetention", { deck: deckName });
      } catch (_) {
        result = await acsInvoke("computeOptimalRetention", { deck: "Default" });
      }

      optimalRetentionResult.hidden = false;
      if (result && result.optimalRetention != null) {
        const pct = (result.optimalRetention * 100).toFixed(1);
        optimalRetentionResult.className = "sched-optimal-result sched-optimal-ok";
        optimalRetentionResult.textContent =
          "Suggested retention: " + result.optimalRetention.toFixed(4) +
          " (" + pct + "%). " +
          "You can manually enter this value in the Desired Retention field above and save it.";
      } else {
        const errMsg = (result && result.error) ? result.error : "Unknown error";
        optimalRetentionResult.className = "sched-optimal-result sched-optimal-err";
        optimalRetentionResult.textContent =
          "Could not compute optimal retention: " + errMsg + ". " +
          "This typically means insufficient review history. " +
          "Note: this is a best-effort feature; the headless server uses the internal FSRS simulator API.";
      }
    } catch (err) {
      optimalRetentionResult.hidden = false;
      optimalRetentionResult.className = "sched-optimal-result sched-optimal-err";
      optimalRetentionResult.textContent = "Error: " + err.message;
    } finally {
      computeRetentionBtn.disabled = false;
      computeRetentionBtn.textContent = "Compute Optimal Retention";
    }
  });

  // -------------------------------------------------------------------------
  // Deck options form save
  // -------------------------------------------------------------------------

  deckOptionsForm.addEventListener("submit", async (e) => {
    e.preventDefault();

    const validationError = validateForm();
    if (validationError) {
      notify(deckOptionsStatus, validationError, "err", 0);
      return;
    }

    const presetName = (currentConfig && currentConfig.name) ? currentConfig.name : "this preset";
    const ok = await confirm(
      "Save Deck Options",
      "Save changes to preset \"" + presetName + "\"? " +
      "This changes scheduling for ALL decks using this preset.",
      "Save",
      "btn btn-primary"
    );
    if (!ok) return;

    saveBtn.disabled = true;
    notify(deckOptionsStatus, "Saving…", "info", 0);

    try {
      const mutatedCfg = buildMutatedConfig();
      await acsInvoke("updateDeckConfig", { config: mutatedCfg });

      // Update our stored reference with the mutated version so subsequent
      // saves don't revert fields we just changed.
      currentConfig = mutatedCfg;
      // Also update allPresets in-place so re-selecting the preset is consistent.
      const idx = parseInt(presetSelect.value, 10);
      if (!isNaN(idx) && idx >= 0 && idx < allPresets.length) {
        allPresets[idx] = mutatedCfg;
      }

      notify(deckOptionsStatus, "Saved successfully.", "ok");
    } catch (err) {
      notify(deckOptionsStatus, "Save failed: " + err.message, "err", 0);
    } finally {
      saveBtn.disabled = false;
    }
  });

  // -------------------------------------------------------------------------
  // Initialisation: load presets
  // -------------------------------------------------------------------------

  async function init() {
    try {
      const presets = await acsInvoke("getDeckConfigs", {});
      allPresets = Array.isArray(presets) ? presets : [];

      presetSelect.innerHTML = "";

      if (allPresets.length === 0) {
        const opt = document.createElement("option");
        opt.textContent = "No presets found";
        opt.value = "";
        opt.disabled = true;
        presetSelect.appendChild(opt);
        return;
      }

      // Placeholder
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Select a preset…";
      placeholder.disabled = true;
      placeholder.selected = true;
      presetSelect.appendChild(placeholder);

      allPresets.forEach((preset, idx) => {
        const opt = document.createElement("option");
        opt.value = String(idx);
        opt.textContent = (preset.name || "(unnamed)") + "  (id " + preset.id + ")";
        presetSelect.appendChild(opt);
      });

      presetSelect.addEventListener("change", onPresetChange);

      // Auto-select first preset if there is exactly one (common case)
      if (allPresets.length === 1) {
        presetSelect.value = "0";
        onPresetChange();
      }
    } catch (err) {
      presetSelect.innerHTML = "";
      const opt = document.createElement("option");
      opt.textContent = "Error loading presets: " + err.message;
      opt.value = "";
      opt.disabled = true;
      presetSelect.appendChild(opt);
    }
  }

  init();

}());
