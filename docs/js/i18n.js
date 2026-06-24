/* ============================================================
   i18n loader for apple-health-mcp-server LP
   - Supports en / ja
   - Resolves nested keys via dot-path ("hero.headline_html")
   - data-i18n=...     → textContent
   - data-i18n-html=... → innerHTML (only used for sanitized site content)
   - data-i18n-attr-X=key → element.setAttribute(X, value)
   - Default language: localStorage → navigator.language → "en"
   ============================================================ */

(function () {
  "use strict";

  const SUPPORTED = ["en", "ja"];
  const STORAGE_KEY = "ahmcp.lang";

  function detectLang() {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && SUPPORTED.includes(saved)) return saved;
    const browser = (navigator.language || "en").slice(0, 2).toLowerCase();
    return SUPPORTED.includes(browser) ? browser : "en";
  }

  function resolveKey(dict, path) {
    return path
      .split(".")
      .reduce((acc, k) => (acc && acc[k] !== undefined ? acc[k] : undefined), dict);
  }

  async function loadDict(lang) {
    const res = await fetch(`./i18n/${lang}.json`, { cache: "no-cache" });
    if (!res.ok) throw new Error(`i18n load failed: ${lang}`);
    return await res.json();
  }

  function apply(dict, lang) {
    document.documentElement.lang = lang;
    if (dict.title) document.title = dict.title;

    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const v = resolveKey(dict, el.dataset.i18n);
      if (v !== undefined) el.textContent = v;
    });

    document.querySelectorAll("[data-i18n-html]").forEach((el) => {
      const v = resolveKey(dict, el.dataset.i18nHtml);
      if (v !== undefined) el.innerHTML = v;
    });

    // data-i18n-attr-aria-label="hero.something" 形式
    document.querySelectorAll("*").forEach((el) => {
      for (const name of el.getAttributeNames()) {
        if (!name.startsWith("data-i18n-attr-")) continue;
        const attr = name.slice("data-i18n-attr-".length);
        const v = resolveKey(dict, el.getAttribute(name));
        if (v !== undefined) el.setAttribute(attr, v);
      }
    });

    // language toggle ボタンのラベル更新
    document.querySelectorAll("[data-i18n-toggle]").forEach((btn) => {
      btn.textContent = dict.lang_toggle || (lang === "en" ? "日本語" : "English");
      btn.setAttribute("aria-label", "Switch language");
    });
  }

  async function setLang(lang) {
    if (!SUPPORTED.includes(lang)) return;
    try {
      const dict = await loadDict(lang);
      apply(dict, lang);
      localStorage.setItem(STORAGE_KEY, lang);
      // Expose current dict + lang for downstream consumers (e.g. hero rotator).
      window.AHMCPi18n.dict = dict;
      window.AHMCPi18n.lang = lang;
      window.AHMCPi18n.resolveKey = (path) => resolveKey(dict, path);
      document.dispatchEvent(
        new CustomEvent("ahmcp:i18n-applied", { detail: { lang } })
      );
    } catch (e) {
      console.error(e);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    setLang(detectLang());

    document.querySelectorAll("[data-i18n-toggle]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const current = document.documentElement.lang || "en";
        const next = current === "en" ? "ja" : "en";
        setLang(next);
      });
    });
  });

  // 外部から強制切替したい場合の hook
  window.AHMCPi18n = { setLang, detectLang };
})();
