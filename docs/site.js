(() => {
  "use strict";
  const root = document.documentElement;
  const languageButtons = [...document.querySelectorAll("[data-language]")];
  let toastTimer;

  function setLanguage(language, persist = false) {
    const lang = language === "zh" ? "zh" : "en";
    root.dataset.lang = lang;
    root.lang = lang === "zh" ? "zh-CN" : "en";
    languageButtons.forEach((button) =>
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.language === lang),
      ),
    );
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
  languageButtons.forEach((button) =>
    button.addEventListener("click", () => {
      // Preserve the visible section when translated paragraphs change height.
      const sections = [...document.querySelectorAll("main section[id]")];
      const anchor = sections
        .filter((section) => section.getBoundingClientRect().top <= 120)
        .pop();
      const offset = anchor ? anchor.getBoundingClientRect().top : 0;
      setLanguage(button.dataset.language, true);
      if (anchor)
        window.scrollBy({
          top: anchor.getBoundingClientRect().top - offset,
          behavior: "instant",
        });
    }),
  );
  setLanguage(root.dataset.lang || "en");

  document.querySelectorAll('[role="tablist"]').forEach((tablist) => {
    const tabs = [...tablist.querySelectorAll('[role="tab"]')];
    function activate(tab, focus) {
      tabs.forEach((item) => {
        const selected = item === tab;
        item.setAttribute("aria-selected", String(selected));
        item.tabIndex = selected ? 0 : -1;
        document.getElementById(item.getAttribute("aria-controls")).hidden =
          !selected;
      });
      if (focus) tab.focus();
    }
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => activate(tab, false));
      tab.addEventListener("keydown", (event) => {
        let index = tabs.indexOf(tab);
        if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
        else if (event.key === "ArrowLeft")
          index = (index + tabs.length - 1) % tabs.length;
        else if (event.key === "Home") index = 0;
        else if (event.key === "End") index = tabs.length - 1;
        else return;
        event.preventDefault();
        activate(tabs[index], true);
      });
    });
    activate(
      tabs.find((tab) => tab.getAttribute("aria-selected") === "true") ||
        tabs[0],
      false,
    );
  });

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
