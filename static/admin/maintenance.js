/**
 * maintenance.js -- Database & Media health panel logic (/admin/maintenance, A8).
 *
 * All server calls go through acsInvoke() from admin.js (token-gated proxy),
 * except long-running ops which use a local acsInvokeTimeout() wrapper with a
 * 5-minute AbortController timeout.
 *
 * No external JS dependencies -- vanilla ES2020 IIFE.
 */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // DOM refs
  // -------------------------------------------------------------------------

  // Database buttons
  const checkDbBtn        = document.getElementById("check-db-btn");
  const checkDbSpinner    = document.getElementById("check-db-spinner");
  const findEmptyBtn      = document.getElementById("find-empty-btn");
  const findEmptySpinner  = document.getElementById("find-empty-spinner");
  const optimizeBtn       = document.getElementById("optimize-btn");
  const optimizeSpinner   = document.getElementById("optimize-spinner");
  const fixIntegrityBtn   = document.getElementById("fix-integrity-btn");
  const fixIntegritySpinner = document.getElementById("fix-integrity-spinner");
  const removeEmptyBtn    = document.getElementById("remove-empty-btn");
  const removeEmptySpinner = document.getElementById("remove-empty-spinner");

  // Media buttons
  const mediaCheckBtn     = document.getElementById("media-check-btn");
  const mediaCheckSpinner = document.getElementById("media-check-spinner");
  const mediaSizeBtn      = document.getElementById("media-size-btn");
  const mediaSizeSpinner  = document.getElementById("media-size-spinner");
  const deleteUnusedBtn   = document.getElementById("delete-unused-btn");
  const deleteUnusedSpinner = document.getElementById("delete-unused-spinner");

  // Result areas
  const dbResult          = document.getElementById("db-result");
  const mediaResult       = document.getElementById("media-result");

  // Confirm dialog
  const confirmOverlay    = document.getElementById("confirm-overlay");
  const confirmTitle      = document.getElementById("confirm-title");
  const confirmBody       = document.getElementById("confirm-body");
  const confirmOk         = document.getElementById("confirm-ok");
  const confirmCancel     = document.getElementById("confirm-cancel");

  // -------------------------------------------------------------------------
  // Confirm dialog (promise-based, same pattern as scheduling.js)
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
  // Result rendering helpers
  // -------------------------------------------------------------------------

  /** Format bytes as human-readable string */
  function fmtBytes(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  /** Escape HTML for safe insertion */
  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /** Build a monospace path box for backup path display */
  function backupBox(path) {
    return '<div class="maint-backup-box"><strong>Backup:</strong> <code>' + escHtml(path) + '</code></div>';
  }

  /** Show result in a div */
  function showResult(el, html, isErr) {
    el.innerHTML = html;
    el.className = 'maint-result' + (isErr ? ' maint-result-err' : ' maint-result-ok');
    el.hidden = false;
  }

  /** Show error in result div */
  function showErr(el, msg) {
    showResult(el, '<strong>Error:</strong> ' + escHtml(msg), true);
  }

  // -------------------------------------------------------------------------
  // Spinner helpers
  // -------------------------------------------------------------------------

  function startOp(btn, spinner) {
    btn.disabled = true;
    spinner.hidden = false;
  }

  function endOp(btn, spinner) {
    btn.disabled = false;
    spinner.hidden = true;
  }

  // -------------------------------------------------------------------------
  // Long-running fetch with 5-minute timeout
  // -------------------------------------------------------------------------

  /**
   * Like acsInvoke but with a configurable AbortController timeout.
   * @param {string} action
   * @param {Object} params
   * @param {number} timeoutMs
   * @returns {Promise<any>}
   */
  async function acsInvokeTimeout(action, params, timeoutMs) {
    const controller = new AbortController();
    const tid = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const resp = await fetch('/admin/api/invoke', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ action, params: params || {} }),
        signal: controller.signal,
      });
      clearTimeout(tid);
      if (!resp.ok) {
        if (resp.status === 401 || resp.status === 302) {
          window.location.href = '/admin/login';
          throw new Error('Session expired.');
        }
        throw new Error('HTTP ' + resp.status + ': ' + resp.statusText);
      }
      const envelope = await resp.json();
      if (envelope.error) throw new Error(envelope.error);
      return envelope.result;
    } catch (e) {
      clearTimeout(tid);
      if (e.name === 'AbortError') throw new Error('Operation timed out (5 minutes exceeded).');
      throw e;
    }
  }

  // -------------------------------------------------------------------------
  // Button handlers — Database section
  // -------------------------------------------------------------------------

  checkDbBtn.addEventListener('click', async () => {
    startOp(checkDbBtn, checkDbSpinner);
    dbResult.hidden = true;
    try {
      const r = await acsInvoke('checkDatabase', {});
      let html = r.ok
        ? '<span class="maint-ok">Database OK &mdash; no problems found.</span>'
        : '<span class="maint-warn">Problems found:</span>';
      if (r.problems && r.problems.length > 0) {
        html += '<ul class="maint-list">' + r.problems.map(p => '<li>' + escHtml(p) + '</li>').join('') + '</ul>';
      }
      showResult(dbResult, html, !r.ok);
    } catch (e) {
      showErr(dbResult, e.message);
    } finally {
      endOp(checkDbBtn, checkDbSpinner);
    }
  });

  findEmptyBtn.addEventListener('click', async () => {
    startOp(findEmptyBtn, findEmptySpinner);
    dbResult.hidden = true;
    try {
      const r = await acsInvoke('getEmptyCards', {});
      let html = '<strong>Empty cards:</strong> ' + r.emptyCardCount + ' across ' + r.noteCount + ' note(s).';
      if (r.report) html += '<pre class="maint-report">' + escHtml(r.report) + '</pre>';
      showResult(dbResult, html, r.emptyCardCount > 0);
    } catch (e) {
      showErr(dbResult, e.message);
    } finally {
      endOp(findEmptyBtn, findEmptySpinner);
    }
  });

  optimizeBtn.addEventListener('click', async () => {
    const ok = await confirm(
      'Optimize Collection (VACUUM)',
      'This will run VACUUM on the SQLite database to reclaim disk space. A backup will be made first. Proceed?',
      'Optimize',
      'btn btn-warn'
    );
    if (!ok) return;
    startOp(optimizeBtn, optimizeSpinner);
    dbResult.hidden = true;
    try {
      const r = await acsInvokeTimeout('optimizeCollection', {}, 5 * 60 * 1000);
      let html = '<span class="maint-ok">Optimize complete.</span>' + backupBox(r.backup);
      showResult(dbResult, html, false);
    } catch (e) {
      showErr(dbResult, e.message);
    } finally {
      endOp(optimizeBtn, optimizeSpinner);
    }
  });

  fixIntegrityBtn.addEventListener('click', async () => {
    const ok = await confirm(
      'Fix Integrity',
      'WARNING: this may delete orphaned cards and notes. A backup will be made immediately before the operation. This cannot be undone. Proceed?',
      'Fix Integrity',
      'btn btn-danger'
    );
    if (!ok) return;
    startOp(fixIntegrityBtn, fixIntegritySpinner);
    dbResult.hidden = true;
    try {
      const r = await acsInvokeTimeout('fixIntegrity', {}, 5 * 60 * 1000);
      const statusClass = r.ok ? 'maint-ok' : 'maint-warn';
      let html = '<span class="' + statusClass + '">' + escHtml(r.message) + '</span>' + backupBox(r.backup);
      showResult(dbResult, html, !r.ok);
    } catch (e) {
      showErr(dbResult, e.message);
    } finally {
      endOp(fixIntegrityBtn, fixIntegritySpinner);
    }
  });

  removeEmptyBtn.addEventListener('click', async () => {
    // Pre-fetch empty card count for confirm message
    startOp(removeEmptyBtn, removeEmptySpinner);
    dbResult.hidden = true;
    let emptyCount = 0;
    try {
      const r = await acsInvoke('getEmptyCards', {});
      emptyCount = r.emptyCardCount || 0;
    } catch (e) {
      showErr(dbResult, 'Could not fetch empty card count: ' + e.message);
      endOp(removeEmptyBtn, removeEmptySpinner);
      return;
    } finally {
      // Re-enable for confirm dialog (button must be clickable visually)
      endOp(removeEmptyBtn, removeEmptySpinner);
    }

    const ok = await confirm(
      'Remove Empty Cards',
      'Permanently delete ' + emptyCount + ' empty card(s). A backup will be made first. This cannot be undone. Proceed?',
      'Remove ' + emptyCount + ' Empty Card(s)',
      'btn btn-danger'
    );
    if (!ok) return;

    startOp(removeEmptyBtn, removeEmptySpinner);
    try {
      const r = await acsInvoke('removeEmptyCards', {});
      let html = '<span class="maint-ok">Removed ' + r.removed + ' empty card(s).</span>' + backupBox(r.backup);
      showResult(dbResult, html, false);
    } catch (e) {
      showErr(dbResult, e.message);
    } finally {
      endOp(removeEmptyBtn, removeEmptySpinner);
    }
  });

  // -------------------------------------------------------------------------
  // Button handlers — Media section
  // -------------------------------------------------------------------------

  mediaCheckBtn.addEventListener('click', async () => {
    startOp(mediaCheckBtn, mediaCheckSpinner);
    mediaResult.hidden = true;
    try {
      const r = await acsInvokeTimeout('mediaCheck', {}, 5 * 60 * 1000);
      const MAX_ITEMS = 50;
      let html = '<strong>Unused:</strong> ' + r.unused.length + ' file(s) &nbsp; <strong>Missing:</strong> ' + r.missing.length + ' file(s)';

      if (r.unused.length > 0) {
        const shown = r.unused.slice(0, MAX_ITEMS);
        const extra = r.unused.length - shown.length;
        html += '<details class="maint-details"><summary>Unused files (' + r.unused.length + ')</summary><ul class="maint-list">' +
          shown.map(f => '<li><code>' + escHtml(f) + '</code></li>').join('') +
          (extra > 0 ? '<li class="maint-more">&hellip;and ' + extra + ' more</li>' : '') +
          '</ul></details>';
      }
      if (r.missing.length > 0) {
        const shown = r.missing.slice(0, MAX_ITEMS);
        const extra = r.missing.length - shown.length;
        html += '<details class="maint-details"><summary>Missing files (' + r.missing.length + ')</summary><ul class="maint-list">' +
          shown.map(f => '<li><code>' + escHtml(f) + '</code></li>').join('') +
          (extra > 0 ? '<li class="maint-more">&hellip;and ' + extra + ' more</li>' : '') +
          '</ul></details>';
      }
      if (r.report) html += '<pre class="maint-report">' + escHtml(r.report) + '</pre>';
      showResult(mediaResult, html, r.missing.length > 0);
    } catch (e) {
      showErr(mediaResult, e.message);
    } finally {
      endOp(mediaCheckBtn, mediaCheckSpinner);
    }
  });

  mediaSizeBtn.addEventListener('click', async () => {
    startOp(mediaSizeBtn, mediaSizeSpinner);
    mediaResult.hidden = true;
    try {
      const r = await acsInvoke('mediaDirSize', {});
      let html = '<strong>Size:</strong> ' + fmtBytes(r.bytes) + ' &nbsp; <strong>Files:</strong> ' + r.fileCount + '<br><span class="maint-dir"><code>' + escHtml(r.dir) + '</code></span>';
      showResult(mediaResult, html, false);
    } catch (e) {
      showErr(mediaResult, e.message);
    } finally {
      endOp(mediaSizeBtn, mediaSizeSpinner);
    }
  });

  deleteUnusedBtn.addEventListener('click', async () => {
    // Pre-fetch unused media count
    startOp(deleteUnusedBtn, deleteUnusedSpinner);
    mediaResult.hidden = true;
    let unusedCount = 0;
    try {
      const r = await acsInvokeTimeout('mediaCheck', {}, 5 * 60 * 1000);
      unusedCount = r.unused ? r.unused.length : 0;
    } catch (e) {
      showErr(mediaResult, 'Could not fetch unused media count: ' + e.message);
      endOp(deleteUnusedBtn, deleteUnusedSpinner);
      return;
    } finally {
      endOp(deleteUnusedBtn, deleteUnusedSpinner);
    }

    const ok = await confirm(
      'Delete Unused Media',
      'Permanently delete ' + unusedCount + ' unused media file(s). This is NOT recoverable from the collection backup (media files are not included in the .anki2 backup). Proceed?',
      'Delete ' + unusedCount + ' File(s)',
      'btn btn-danger'
    );
    if (!ok) return;

    startOp(deleteUnusedBtn, deleteUnusedSpinner);
    try {
      const r = await acsInvoke('deleteUnusedMedia', {});
      let html = '<span class="maint-ok">Deleted ' + r.deletedCount + ' unused media file(s).</span>' + backupBox(r.backup);
      showResult(mediaResult, html, false);
    } catch (e) {
      showErr(mediaResult, e.message);
    } finally {
      endOp(deleteUnusedBtn, deleteUnusedSpinner);
    }
  });

}());
