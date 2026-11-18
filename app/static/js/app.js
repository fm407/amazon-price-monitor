// Theme toggle and confirmation prompts. No inline scripts are used (strict CSP).
(function () {
  "use strict";
  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-bs-theme", next);
      try { localStorage.setItem("pm-theme", next); } catch (e) { /* storage unavailable */ }
      document.dispatchEvent(new CustomEvent("pm:themechange"));
    });
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-confirm]");
    if (button && !window.confirm(button.getAttribute("data-confirm"))) {
      event.preventDefault();
    }
  });
  var here = window.location.pathname;
  document.querySelectorAll(".navbar .nav-link").forEach(function (link) {
    if (link.getAttribute("href") === here) { link.setAttribute("aria-current", "page"); }
  });
})();
