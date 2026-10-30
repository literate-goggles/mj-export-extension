// Applies a stored light/dark choice before first paint. Loaded as a classic,
// same-origin script so it runs under the page's CSP without inline code.
(function () {
  try {
    var theme = window.localStorage.getItem('mjv-theme');
    if (theme === 'light' || theme === 'dark') document.documentElement.setAttribute('data-theme', theme);
  } catch (err) {
    // Storage can be unavailable (private window, blocked site data); the system theme applies.
  }
})();
