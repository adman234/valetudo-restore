// Minimal progressive enhancement: POST buttons + key upload. No framework.
(function () {
  "use strict";

  function show(target, data) {
    var el = document.querySelector(target || "#out");
    if (!el) { return; }
    el.hidden = false;
    el.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
  }

  // ---- robot status card -------------------------------------------------
  // The card is rendered from the last recorded verdict, so on its own it goes
  // stale: after a restore it kept saying WIPED until a reload. Refresh it from
  // /api/status after any action that probed the robot, when the tab regains
  // focus, and every 30s in the background.
  function fmtTs(ts) {
    if (!ts) { return "never"; }
    return new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  }

  function applyMonitor(m) {
    var card = document.getElementById("statuscard");
    if (!card || !m || !m.state) { return; }
    card.className = "card status " + String(m.state).toLowerCase();
    function set(key, text) {
      var el = card.querySelector('[data-st="' + key + '"]');
      if (el) { el.textContent = text; }
      return el;
    }
    set("state", m.state);
    set("streak", m.streak ? " · " + m.streak + "x" : "");
    var err = set("error", m.error || "");
    if (err) { err.hidden = !m.error; }
    set("ts", fmtTs(m.ts));
  }

  function pollStatus() {
    fetch("/api/status")
      .then(function (r) { return r.json(); })
      .then(function (d) { applyMonitor(d.monitor); })
      .catch(function () { /* next poll will try again */ });
  }

  if (document.getElementById("statuscard")) {
    setInterval(pollStatus, 30000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { pollStatus(); }
    });
  }

  // A button that swaps its label for "working…" would change width and shove
  // every button after it along. Pin the current width for the duration.
  function busy(btn, text) {
    btn.dataset.label = btn.textContent;
    btn.style.width = btn.getBoundingClientRect().width + "px";
    btn.classList.add("busy");
    btn.disabled = true;
    btn.textContent = text;
  }

  function idle(btn) {
    btn.disabled = false;
    if (btn.dataset.label !== undefined) { btn.textContent = btn.dataset.label; }
    btn.style.width = "";
    btn.classList.remove("busy");
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("button[data-post]");
    if (!btn) { return; }
    ev.preventDefault();

    var confirmMsg = btn.getAttribute("data-confirm");
    if (confirmMsg && !window.confirm(confirmMsg)) { return; }

    var url = btn.getAttribute("data-post");
    var out = btn.getAttribute("data-out") || "#out";
    var field = btn.getAttribute("data-field");
    var opts = { method: "POST" };

    if (field) {
      var body = new URLSearchParams();
      var parts = field.split("=");
      body.append(parts[0], parts.slice(1).join("="));
      opts.body = body;
    }

    busy(btn, "working…");

    fetch(url, opts)
      .then(function (r) { return r.json().catch(function () { return { status: r.status }; }); })
      .then(function (d) {
        show(out, d);
        // Test connection, restores and restart all record a fresh verdict.
        if (d && d.state) { pollStatus(); }
        // Reload so the tables and status cards reflect the new state.
        if (/backup|restore|delete|binary|diagnostics|helpers/.test(url)) {
          setTimeout(function () { window.location.reload(); }, 1200);
        }
      })
      .catch(function (e) { show(out, "Request failed: " + e); })
      .finally(function () { idle(btn); });
  });

  function wireUpload(formId, url, outSel, confirmMsg) {
    var f = document.getElementById(formId);
    if (!f) { return; }
    f.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (confirmMsg && !window.confirm(confirmMsg)) { return; }
      var btn = f.querySelector("button[type=submit]");
      if (btn) { busy(btn, "uploading…"); }
      fetch(url, { method: "POST", body: new FormData(f) })
        .then(function (r) { return r.json(); })
        .then(function (d) { show(outSel, d); })
        .catch(function (e) { show(outSel, "Upload failed: " + e); })
        .finally(function () {
          if (btn) { idle(btn); }
        });
    });
  }

  var showAll = document.getElementById("showall");
  if (showAll) {
    showAll.addEventListener("click", function () {
      var rows = document.querySelectorAll("#backuptable tr.extra");
      var hidden = rows.length && rows[0].hidden;
      rows.forEach(function (r) { r.hidden = !hidden; });
      showAll.textContent = hidden
        ? "Show fewer"
        : "Show all " + showAll.dataset.total + " backups";
    });
  }

  wireUpload("keyform", "/api/upload-key", "#keyout", null);

  // Settings: the Save button is grey and disabled until a field differs from
  // what the server rendered, then green. Compared against the elements'
  // default values rather than a snapshot, so a browser that restores typed
  // values on back-navigation still shows them as unsaved.
  var sf = document.getElementById("settingsform");
  var sb = document.getElementById("savebtn");
  if (sf && sb) {
    var changed = function (el) {
      if (!el.name || el.type === "hidden" || el.type === "file") { return false; }
      if (el.type === "checkbox" || el.type === "radio") { return el.checked !== el.defaultChecked; }
      if (el.tagName === "SELECT") {
        var opts = Array.prototype.slice.call(el.options);
        var def = opts.filter(function (o) { return o.defaultSelected; })[0] || opts[0];
        return !!def && !def.selected;
      }
      return el.value !== el.defaultValue;
    };
    var refresh = function () {
      var dirty = Array.prototype.some.call(sf.elements, changed);
      sb.disabled = !dirty;
      sb.classList.toggle("dirty", dirty);
      sb.title = dirty ? "You have unsaved changes" : "No changes to save";
    };
    sf.addEventListener("input", refresh);
    sf.addEventListener("change", refresh);
    window.addEventListener("pageshow", refresh);
    refresh();
  }

  // The restore form has TWO submit buttons posting to different endpoints
  // (everything vs map-only), so the target comes from the button, not the form.
  var rf = document.getElementById("restoreform");
  if (rf) {
    rf.addEventListener("click", function (ev) {
      var b = ev.target.closest("button[type=submit][data-endpoint]");
      if (b) { rf.dataset.endpoint = b.dataset.endpoint;
               rf.dataset.confirm = b.getAttribute("data-confirm") || ""; }
    });
    rf.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var url = rf.dataset.endpoint || "/api/restore";
      if (rf.dataset.confirm && !window.confirm(rf.dataset.confirm)) { return; }
      var btns = rf.querySelectorAll("button[type=submit]");
      btns.forEach(function (b) { b.disabled = true; });
      show("#restoreout", "working… large archives take a minute.");
      fetch(url, { method: "POST", body: new FormData(rf) })
        .then(function (r) { return r.json(); })
        .then(function (d) { show("#restoreout", d); pollStatus(); })
        .catch(function (e) { show("#restoreout", "Upload failed: " + e); })
        .finally(function () { btns.forEach(function (b) { b.disabled = false; }); });
    });
  }
})();
