// Native scroll containers retain wheel, touch, keyboard and no-script access to every project.
export function createWaterfall(root) {
  const browser = root.querySelector('.wf-browser');
  const panorama = root.querySelector('.wf-panorama');
  const categories = [...root.querySelectorAll('#categories a')];
  const motion = root.querySelector('[data-wf-motion]');
  const steps = [...root.querySelectorAll('[data-wf-step]')];
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const lanes = [...root.querySelectorAll('.wf-column')].map((column, i) => {
    const viewport = column.querySelector('.wf-window');
    const original = column.querySelector('[data-original]');
    for (const before of [true, false]) {
      const copy = original.cloneNode(true);
      copy.removeAttribute('data-original');
      copy.setAttribute('aria-hidden', 'true');
      copy.querySelectorAll('a').forEach(link => link.tabIndex = -1);
      if (before) original.before(copy); else original.after(copy);
    }
    return {column, viewport, windows: [viewport], original, offset: [25,115,0,75,155][i], period: 0, phase: 0, written: 0};
  });
  // Adjacent sets keep all five collections in view when an end collection is centered.
  for (const before of [true, false]) {
    const set = document.createDocumentFragment();
    lanes.forEach(lane => {
      const copy = lane.column.cloneNode(true);
      copy.removeAttribute('id');
      copy.setAttribute('aria-hidden', 'true');
      copy.querySelectorAll('[data-original]').forEach(el => el.removeAttribute('data-original'));
      copy.querySelectorAll('a').forEach(link => link.tabIndex = -1);
      lane.windows.push(copy.querySelector('.wf-window'));
      set.append(copy);
    });
    if (before) panorama.prepend(set); else panorama.append(set);
  }
  let selected = 2;
  let paused = false, focused = false, visible = false, frame = 0, last = 0;
  const running = () => !paused && !focused && visible && !document.hidden && !reduced.matches;
  function write(lane) {
    lane.windows.forEach(viewport => {viewport.scrollTop = lane.phase;});
    lane.written = lane.viewport.scrollTop;
  }
  function measure() {
    browser.style.setProperty('--wf-column-width', `${Math.max(210, (browser.clientWidth - 80) / 5)}px`);
    const heights = lanes.map(lane => lane.original.offsetHeight);
    lanes.forEach((lane, i) => {
      const offset = lane.period ? lane.phase / lane.period : 1 - lane.offset / heights[i];
      lane.period = heights[i];
      lane.phase = offset * lane.period;
      write(lane);
    });
    position(false);
  }
  function position(animate = true) {
    const column = lanes[selected].column;
    panorama.style.transition = animate && !reduced.matches ? '' : 'none';
    browser.scrollLeft = 0;
    panorama.style.transform = `translateX(${browser.clientWidth / 2 - column.offsetLeft - column.offsetWidth / 2}px)`;
  }
  function choose(index, animate = true) {
    selected = index;
    categories.forEach((link, i) => {
      if (i === selected) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    });
    updateSteps();
    position(animate);
  }
  function updateSteps() {
    const zh = document.documentElement.dataset.lang === 'zh';
    steps.forEach(button => {
      const previous = Number(button.dataset.wfStep) < 0;
      button.disabled = previous ? selected === 0 : selected === lanes.length - 1;
      button.setAttribute('aria-label', zh ? (previous ? '上一个分类' : '下一个分类') : (previous ? 'Previous collection' : 'Next collection'));
    });
  }
  steps.forEach(button => button.addEventListener('click', () => {
    const index = Math.max(0, Math.min(lanes.length - 1, selected + Number(button.dataset.wfStep)));
    history.replaceState(null, '', categories[index].hash);
    choose(index);
  }));
  categories.forEach((link, i) => link.addEventListener('click', event => {
    event.preventDefault();
    history.replaceState(null, '', link.hash);
    choose(i);
  }));
  function followHash() {
    const index = categories.findIndex(link => link.hash === location.hash);
    if (index >= 0) choose(index, false);
  }
  addEventListener('hashchange', followHash);
  function tick(time) {
    frame = 0;
    if (!running()) return;
    const distance = Math.min((time - last) / 1000, .05) * 20;
    last = time;
    lanes.forEach(lane => {
      lane.phase -= distance;
      // Identical neighbouring copies make both seams visually continuous.
      if (lane.phase <= 0) lane.phase += lane.period;
      if (lane.phase >= 2 * lane.period) lane.phase -= lane.period;
      write(lane);
    });
    frame = requestAnimationFrame(tick);
  }
  function sync() {
    if (running() && !frame) {last = performance.now(); frame = requestAnimationFrame(tick);}
    else if (!running() && frame) {cancelAnimationFrame(frame); frame = 0;}
  }
  function updateMotion() {
    updateSteps();
    const zh = document.documentElement.dataset.lang === 'zh';
    motion.disabled = reduced.matches;
    motion.dataset.paused = String(paused || reduced.matches);
    motion.querySelector('span').textContent = reduced.matches ? (zh ? '动态已关闭' : 'Motion off') : paused ? (zh ? '继续' : 'Resume') : (zh ? '暂停' : 'Pause');
    motion.setAttribute('aria-label', reduced.matches ? (zh ? '已关闭自动滚动，可手动浏览项目' : 'Automatic scrolling is off; scroll to browse projects') : paused ? (zh ? '继续项目滚动' : 'Resume project scrolling') : (zh ? '暂停项目滚动' : 'Pause project scrolling'));
  }
  motion.addEventListener('click', () => {paused = !paused; updateMotion(); sync();});
  browser.addEventListener('pointerdown', event => {
    if (event.pointerType === 'touch' && event.target.closest('.wf-window')) {paused = true; updateMotion(); sync();}
  });
  const keyboardFocus = () => browser.contains(document.activeElement) && !!document.activeElement.closest('.wf-column') && document.activeElement.matches(':focus-visible');
  browser.addEventListener('focusin', event => {
    focused = keyboardFocus();
    if (focused) choose(Number(event.target.closest('.wf-column').dataset.group), false);
    sync();
  });
  browser.addEventListener('focusout', () => queueMicrotask(() => {focused = keyboardFocus(); sync();}));
  lanes.forEach(lane => lane.windows.forEach(viewport => viewport.addEventListener('scroll', () => {
    if (Math.abs(viewport.scrollTop - lane.written) > 1) {
      lane.phase = viewport.scrollTop;
      write(lane);
    }
  }, {passive: true})));
  document.addEventListener('visibilitychange', sync);
  new IntersectionObserver(([entry]) => {visible = entry.isIntersecting; sync();}, {threshold: .05}).observe(root);
  const resize = new ResizeObserver(measure);
  lanes.forEach(lane => resize.observe(lane.original));
  new ResizeObserver(measure).observe(browser);
  new MutationObserver(updateMotion).observe(document.documentElement, {attributes: true, attributeFilter: ['data-lang']});
  reduced.addEventListener('change', () => {updateMotion(); sync();});
  root.dataset.ready = '';
  motion.hidden = false;
  steps.forEach(button => {button.hidden = false;});
  measure(); choose(selected, false); followHash(); updateMotion(); sync();
}
