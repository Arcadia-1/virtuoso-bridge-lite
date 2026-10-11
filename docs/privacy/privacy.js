(() => {
  const button = document.getElementById("privacy-toggle");
  const status = document.getElementById("privacy-status");
  let optedOut = false;
  let ready = false;
  let failed = false;
  const requested =
    navigator.doNotTrack === "1" || navigator.globalPrivacyControl === true;
  function render() {
    const zh = document.documentElement.dataset.lang === "zh";
    button.textContent = optedOut
      ? zh
        ? "允许统计我的访问"
        : "Allow counting"
      : zh
        ? "停止统计我的访问"
        : "Stop counting me";
    status.textContent = failed
      ? zh
        ? "暂时无法读取或保存偏好，请稍后重试。"
        : "Could not read or save your preference. Please try again."
      : !ready
        ? zh
          ? "正在读取你的偏好……"
          : "Loading your preference…"
        : requested
          ? zh
            ? "你的浏览器已请求不被追踪，我们不会统计你的访问。"
            : "Your browser requests no tracking. Your visits are not counted."
          : optedOut
            ? zh
              ? "已停止统计此浏览器的访问。"
              : "Visits from this browser are not counted."
            : zh
              ? "当前允许第一方汇总统计。"
              : "First-party aggregate counting is currently allowed.";
    button.disabled = !ready || requested;
  }
  fetch("/api/track/opt-out", { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) throw new Error("Unavailable");
      const data = await response.json();
      optedOut = data.optedOut;
      ready = true;
      render();
    })
    .catch(() => {
      failed = true;
      render();
    });
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const response = await fetch("/api/track/opt-out", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ optOut: !optedOut }),
      });
      if (!response.ok) throw new Error("Unavailable");
      optedOut = (await response.json()).optedOut;
      failed = false;
    } catch (_) {
      failed = true;
    }
    render();
  });
  new MutationObserver(render).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-lang"],
  });
})();
