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

  var kf = document.getElementById("keyform");
  if (kf) {
    kf.addEventListener("submit", function (ev) {
      ev.preventDefault();
      fetch("/api/upload-key", { method: "POST", body: new FormData(kf) })
        .then(function (r) { return r.json(); })
        .then(function (d) { show("#keyout", d); })
        .catch(function (e) { show("#keyout", "Upload failed: " + e); });
    });
  }
})();
