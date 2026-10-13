(() => {
  "use strict";
  const root = document.documentElement;
  const languageToggle = document.querySelector("[data-language-toggle]");
  let toastTimer;

  function setLanguage(language, persist = false) {
    const lang = language === "zh" ? "zh" : "en";
    root.dataset.lang = lang;
    root.lang = lang === "zh" ? "zh-CN" : "en";
    const languageLabel = lang === "zh" ? "Switch to English" : "切换到中文";
    languageToggle?.setAttribute("aria-label", languageLabel);
    languageToggle?.setAttribute("title", languageLabel);
    document.querySelectorAll("[data-internal]").forEach((link) => {
      const url = new URL(link.getAttribute("href"), location.href);
      url.searchParams.set("lang", lang);
      link.href = url.pathname + url.search + url.hash;
    });
    const title = document.querySelector(
      'meta[name="site-title-' + lang + '"]',
    );
    if (title) document.title = title.content;
    if (persist) {
      try {
        localStorage.setItem("vb-language", lang);
      } catch (_) {
        /* Storage is optional. */
      }
      const url = new URL(location.href);
      url.searchParams.set("lang", lang);
      history.replaceState(null, "", url);
    }
  }
  languageToggle?.addEventListener("click", () => {
    // Preserve the visible section when translated paragraphs change height.
    const sections = [...document.querySelectorAll("main section[id]")];
    const anchor = sections
      .filter((section) => section.getBoundingClientRect().top <= 120)
      .pop();
    const offset = anchor ? anchor.getBoundingClientRect().top : 0;
    setLanguage(root.dataset.lang === "zh" ? "en" : "zh", true);
    if (anchor)
      window.scrollBy({
        top: anchor.getBoundingClientRect().top - offset,
        behavior: "instant",
      });
  });
  setLanguage(root.dataset.lang || "en");

  const toast = document.getElementById("toast");
  document.querySelectorAll("[data-copy]").forEach((button) =>
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copy);
      const isZh = root.dataset.lang === "zh";
      try {
        if (!navigator.clipboard) throw new Error("Clipboard unavailable");
        await navigator.clipboard.writeText(target.textContent.trim());
        toast.textContent = isZh ? "已复制到剪贴板" : "Copied to clipboard";
      } catch (_) {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(target);
        selection.removeAllRanges();
        selection.addRange(range);
        toast.textContent = isZh
          ? "代码已选中，请按 Ctrl / ⌘ + C 复制"
          : "Code selected. Press Ctrl / ⌘ + C to copy.";
      }
      toast.hidden = false;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {
        toast.hidden = true;
      }, 3500);
    }),
  );
})();
