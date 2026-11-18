// Applies the saved theme before first paint. Loaded in <head> without defer on purpose.
(function () {
  var saved = null;
  try { saved = localStorage.getItem("pm-theme"); } catch (e) { /* storage unavailable */ }
  if (saved === "light" || saved === "dark") {
    document.documentElement.setAttribute("data-bs-theme", saved);
  }
})();
