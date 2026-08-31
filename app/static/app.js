// Minimal progressive enhancement: POST buttons + key upload. No framework.
(function () {
  "use strict";

  function show(target, data) {
    var el = document.querySelector(target || "#out");
    if (!el) { return; }
    el.hidden = false;
    el.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
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

    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "working…";

    fetch(url, opts)
      .then(function (r) { return r.json().catch(function () { return { status: r.status }; }); })
      .then(function (d) {
        show(out, d);
        // Reload so the tables and status cards reflect the new state.
        if (/backup|restore|delete|binary/.test(url)) {
          setTimeout(function () { window.location.reload(); }, 1200);
        }
      })
      .catch(function (e) { show(out, "Request failed: " + e); })
      .finally(function () {
        btn.disabled = false;
        btn.textContent = label;
      });
  });

  function wireUpload(formId, url, outSel, confirmMsg) {
    var f = document.getElementById(formId);
    if (!f) { return; }
    f.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (confirmMsg && !window.confirm(confirmMsg)) { return; }
      var btn = f.querySelector("button[type=submit]");
      var label = btn ? btn.textContent : "";
      if (btn) { btn.disabled = true; btn.textContent = "uploading…"; }
      fetch(url, { method: "POST", body: new FormData(f) })
        .then(function (r) { return r.json(); })
        .then(function (d) { show(outSel, d); })
        .catch(function (e) { show(outSel, "Upload failed: " + e); })
        .finally(function () {
          if (btn) { btn.disabled = false; btn.textContent = label; }
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
        .then(function (d) { show("#restoreout", d); })
        .catch(function (e) { show("#restoreout", "Upload failed: " + e); })
        .finally(function () { btns.forEach(function (b) { b.disabled = false; }); });
    });
  }
})();
