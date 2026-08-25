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

  wireUpload("keyform", "/api/upload-key", "#keyout", null);
  wireUpload("mapform", "/api/restore-map", "#mapout",
    "Restore map data onto the robot from this file? The current map is copied aside first.");
})();
