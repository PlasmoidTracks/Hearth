// Client-side i18n. Loads the string table for the current user's language and
// applies it to elements marked with data-i18n / data-i18n-title / data-i18n-placeholder.
// Exposes window.I18N.t(key) for dynamically-created strings (approval cards, etc.).
// Fallbacks: missing key -> English -> the key itself (never blank).

const I18N = {
  lang: "en",
  strings: {},
  _en: {},

  async load() {
    try {
      const r = await fetch("/api/i18n", { credentials: "same-origin" });
      const d = await r.json();
      this.lang = d.language || "en";
      this.strings = d.strings || {};
    } catch (e) { /* keep defaults */ }
  },

  t(key) { return this.strings[key] || this._en[key] || key; },

  // Apply translations to any DOM subtree (defaults to whole document).
  apply(root = document) {
    root.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = this.t(el.getAttribute("data-i18n")); });
    root.querySelectorAll("[data-i18n-title]").forEach(el => { el.setAttribute("title", this.t(el.getAttribute("data-i18n-title"))); });
    root.querySelectorAll("[data-i18n-placeholder]").forEach(el => { el.setAttribute("placeholder", this.t(el.getAttribute("data-i18n-placeholder"))); });
    document.documentElement.lang = this.lang;
  },
};
window.I18N = I18N;
