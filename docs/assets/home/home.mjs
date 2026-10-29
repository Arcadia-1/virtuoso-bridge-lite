if (typeof document !== "undefined") {
// SVG strokes animate; labels and layout never move. Stop when offscreen.
const figure = document.querySelector('.hero-flow');
if (figure) {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let visible = false;
  let introduced = false;
  const sync = () => {
    const running = visible && !document.hidden && !reduced.matches;
    figure.classList.toggle('system-running', running);
    if (running && !introduced) {
      introduced = true;
      figure.classList.add('system-enter');
    }
    figure.classList.toggle('system-reduced', reduced.matches);

  };
  new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    sync();
  }, {threshold: .15}).observe(figure);
  document.addEventListener('visibilitychange', sync);
  reduced.addEventListener('change', sync);
  sync();
}

}
