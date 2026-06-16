/**
 * admin.js -- shared helpers for the Anki admin console.
 *
 * acsInvoke(action, params)
 * -------------------------
 * All admin UI pages call AnkiConnect actions through the token-gated proxy at
 * POST <ADMIN_BASE>/api/invoke rather than hitting the raw unauthenticated POST /.
 *
 * The base path is read from window.ADMIN_BASE (injected by base.html / login.html
 * via the Jinja2 context processor) so that the client works correctly regardless
 * of the URL prefix the server is mounted under.
 *
 * Usage:
 *   const decks = await acsInvoke('deckNames', {});
 *   const info  = await acsInvoke('cardsInfo', { cards: [1234] });
 *
 * Returns the ``result`` value on success.
 * Throws an Error with the ``error`` string on action-level failure.
 * Throws an Error with HTTP status info on network/auth failure.
 */

/* eslint-disable no-console */
(function (global) {
  "use strict";

  /**
   * Resolve the admin base path from the injected global, falling back to
   * "/admin" if the variable is absent (e.g. in unit test environments).
   * @returns {string}
   */
  function _adminBase() {
    return (global.ADMIN_BASE || "/admin").replace(/\/$/, "");
  }

  /**
   * Call a single AnkiConnect action through the token-gated admin proxy.
   *
   * @param {string} action  - AnkiConnect action name (e.g. "deckNames")
   * @param {Object} params  - action parameters (may be {})
   * @returns {Promise<*>}   - resolves with result value, rejects on error
   */
  async function acsInvoke(action, params) {
    const base = _adminBase();
    const resp = await fetch(base + "/api/invoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ action, params: params || {} }),
    });

    if (!resp.ok) {
      if (resp.status === 401 || resp.status === 302) {
        // Session expired -- redirect to login
        window.location.href = base + "/login";
        throw new Error("Session expired. Redirecting to login.");
      }
      throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
    }

    const envelope = await resp.json();
    if (envelope.error) {
      throw new Error(envelope.error);
    }
    return envelope.result;
  }

  // Expose globally so page scripts can use it without bundling.
  global.acsInvoke = acsInvoke;

})(typeof window !== "undefined" ? window : globalThis);
