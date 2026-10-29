// The index remains the canonical content source and the no-JavaScript fallback.
export function createWaterfall(root, groups, language) {
  const viewport = root.querySelector('.wf-window');
  const panorama = root.querySelector('.wf-panorama');
  const nav = root.querySelector('#categories');
  const steps = [...root.querySelectorAll('[data-wf-step]')];
  const motion = root.querySelector('[data-wf-motion]');
  const detail = root.querySelector('.wf-detail');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const types = ['registry', 'tool', 'project', 'manual', 'citation'];
  const labels = {en:['REGISTRY','TOOL','PROJECT','REFERENCE','SOURCE'], zh:['项目档案','工具','设计项目','参考资料','收录来源']};
  const escape = value => value.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const lanes = groups.map((group, i) => ({group, columns:[], phase:[25,115,0,75,155][i], period:1}));
  let selected = 2, paused = false, hovered = null, focused = null;
  let visible = false, frame = 0, last = 0, panning = false, panTimer;
  let detailGroup = 2, detailItem = 0, detailTime = 0, leaveTimer;
  let detailHovered = false, userSelected = false, hoverTimer;
  let hoverX = 0, hoverY = 0;
  const readingDetails = () => detailHovered || detail.contains(document.activeElement);

  // Repeat only the presentation, keeping the five real collection counts intact.
  for (let set = 0; set < 3; set++) {
    lanes.forEach((lane, i) => {
      const column = document.createElement('div');
      column.className = 'wf-column';
      column.dataset.group = i;
      column.dataset.set = set;
      column.innerHTML = `<div class="wf-track">${[0,1].map(copy => `<div class="wf-copy">${lane.group.items.map((item, j) => {
        const [owner, ...parts] = item.name.split('/');
        const name = parts.join('/');
        return `<a class="wf-record" href="${escape(item.url)}" target="_blank" rel="noopener noreferrer" data-item="${j}" ${set !== 1 || copy ? 'tabindex="-1" aria-hidden="true"' : ''} style="--card-height:${[180,207,190,218][(j+i)%4]}px"><span class="wf-card wf-${types[i]}"><span class="wf-meta"><span class="wf-kind"></span><span>0${i+1}.${String(j+1).padStart(2,'0')}</span></span><span class="wf-title">${escape(i === 0 ? owner : name).replaceAll('_','_<wbr>')}</span><span class="wf-owner">${escape(i === 0 ? name : owner)}</span><span class="wf-description"></span><span class="wf-link" aria-hidden="true">↗</span></span></a>`;
      }).join('')}</div>`).join('')}</div>`;
      panorama.append(column);
      lane.columns.push(column);
    });
  }
  const cards = [...panorama.querySelectorAll('.wf-record')];
  groups.forEach((group, i) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.addEventListener('click', () => choose(i));
    nav.append(button);
  });
  steps.forEach(button => button.addEventListener('click', () => {
    choose(Math.max(0, Math.min(groups.length-1, selected+Number(button.dataset.wfStep))));
  }));
  // Explicit pause is independent of temporary hover/focus and category changes.
  motion.addEventListener('click', () => {
    paused = !paused;
    updateMotion();
    sync();
  });
  function updateMotion() {
    const zh = language() === 'zh';
    motion.disabled = reduced.matches;
    motion.dataset.paused = String(paused || reduced.matches);
    motion.querySelector('span').textContent = reduced.matches
      ? (zh ? '动态已关闭' : 'Motion off')
      : paused ? (zh ? '继续' : 'Resume') : (zh ? '暂停' : 'Pause');
    motion.setAttribute('aria-label', reduced.matches
      ? (zh ? '已按减少动态效果偏好关闭项目滚动' : 'Project scrolling is off: reduced motion enabled')
      : paused ? (zh ? '继续项目滚动' : 'Resume project scrolling') : (zh ? '暂停项目滚动' : 'Pause project scrolling'));
  }
  function updateSteps() {
    steps.forEach(button => {
      const previous = Number(button.dataset.wfStep) < 0;
      button.disabled = previous ? selected === 0 : selected === groups.length-1;
      button.setAttribute('aria-label', language() === 'zh'
        ? (previous ? '上一个分类' : '下一个分类')
        : (previous ? 'Previous collection' : 'Next collection'));
    });
  }
  function paint(lane, normalize = true) {
    if (normalize) lane.phase = ((lane.phase % lane.period) + lane.period) % lane.period;
    for (const column of lane.columns) column.firstElementChild.style.transform = `translateY(${-lane.period + lane.phase}px)`;
  }
  function showDetails(group, item, force = false) {
    if (!force && group === detailGroup && item === detailItem) return;
    detailGroup = group; detailItem = item;
    const record = groups[group].items[item], slash = record.name.indexOf('/');
    detail.querySelector('.wf-detail-id').textContent = `VB-${group+1}.${String(item+1).padStart(2,'0')}`;
    detail.querySelector('.wf-detail-owner').textContent = record.name.slice(0, slash);
    detail.querySelector('.wf-detail-title').textContent = record.name.slice(slash+1);
    detail.querySelector('.wf-detail-description').textContent = record.description[language()];
    detail.querySelector('.wf-detail-category').textContent = groups[group].title[language()];
    detail.querySelector('.wf-detail-position').textContent = `${String(item+1).padStart(2,'0')} / ${String(groups[group].items.length).padStart(2,'0')}`;
    cards.forEach(card => card.classList.toggle('is-current', Number(card.closest('.wf-column').dataset.group) === group && Number(card.dataset.item) === item));
  }
  function nearest() {
    // Automatic centering is only a preview, never a replacement for user selection.
    if (userSelected || readingDetails() || hovered || focused) return;
    const bounds = viewport.getBoundingClientRect(), center = bounds.top + bounds.height / 2;
    const records = [...lanes[selected].columns[1].querySelectorAll('.wf-record')];
    let best = null, distance = Infinity;
    records.forEach(card => {
      const rect = card.getBoundingClientRect(), delta = Math.abs(rect.top + rect.height/2 - center);
      if (delta < distance) {best = card; distance = delta;}
    });
    if (best) showDetails(selected, Number(best.dataset.item));
  }
  const running = () => !paused && !reduced.matches && !hovered && !focused && visible && !document.hidden;
  function tick(time) {
    frame = 0;
    if (!running()) return;
    const distance = Math.min((time-last)/1000, .05) * 16;
    last = time;
    lanes.forEach(lane => {lane.phase += distance; paint(lane);});
    if (time-detailTime > 250 && !panning) {detailTime = time; nearest();}
    frame = requestAnimationFrame(tick);
  }
  function sync() {
    if (running() && !frame) {last = performance.now(); frame = requestAnimationFrame(tick);}
    else if (!running() && frame) {cancelAnimationFrame(frame); frame = 0;}
  }
  function position(animate = true) {
    const column = lanes[selected].columns[1];
    panorama.style.transition = animate ? '' : 'none';
    panorama.style.transform = `translateX(${viewport.clientWidth/2-column.offsetLeft-column.offsetWidth/2}px)`;
  }
  function clearHover() {
    clearTimeout(hoverTimer);
    hovered?.classList.remove('is-hovered');
    hovered = null;
  }
  function selectCard(card) {
    userSelected = true;
    showDetails(Number(card.closest('.wf-column').dataset.group), Number(card.dataset.item));
  }
  function settleHover(card) {
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => {
      if (hovered !== card) return;
      selectCard(card);
      card.classList.add('is-hovered');
    }, 240);
  }
  function choose(i) {
    clearTimeout(leaveTimer);
    clearHover();
    userSelected = false;
    selected = i;
    updateSteps();
    lanes.forEach((lane, j) => lane.columns.forEach(column => column.classList.toggle('is-center', j === i)));
    nav.querySelectorAll('button').forEach((button, j) => button.setAttribute('aria-pressed', String(j === i)));
    clearTimeout(panTimer);
    panning = !reduced.matches;
    position();
    nearest();
    panTimer = setTimeout(() => {panning = false; nearest();}, panning ? 700 : 0);
    sync();
  }
  function measure() {
    viewport.style.setProperty('--wf-column-width', `${Math.max(174,(viewport.clientWidth-44)/3)}px`);
    lanes.forEach(lane => {lane.period = lane.columns[1].querySelector('.wf-copy').offsetHeight; paint(lane);});
    position(false);
  }
  function translate() {
    const lang = language();
    updateSteps();
    updateMotion();
    nav.querySelectorAll('button').forEach((button, i) => {
      button.textContent = groups[i].title[lang];
      const count = document.createElement('small');
      count.textContent = groups[i].items.length;
      button.append(count);
      button.setAttribute('aria-pressed', String(i === selected));
    });
    cards.forEach(card => {
      const i = Number(card.closest('.wf-column').dataset.group);
      const item = groups[i].items[Number(card.dataset.item)];
      card.querySelector('.wf-kind').textContent = labels[lang][i];
      card.querySelector('.wf-description').textContent = item.description[lang];
      card.setAttribute('aria-label', `${item.name} — ${item.description[lang]} — GitHub`);
    });
    measure(); showDetails(detailGroup, detailItem, true); sync();
  }
  // A stable hit area prevents the lifted paper from repeatedly entering/leaving.
  // Lock only the details: reading on the right must not stop the left-hand flow.
  detail.addEventListener('pointerenter', event => {
    if (event.pointerType !== 'touch') detailHovered = true;
  });
  detail.addEventListener('pointerleave', () => {detailHovered = false;});
  detail.addEventListener('pointerdown', () => {userSelected = true;});
  cards.forEach(card => {
    card.addEventListener('pointerenter', event => {
      if (event.pointerType !== 'mouse' || panning) return;
      clearTimeout(leaveTimer);
      clearHover(); hovered = card;
      hoverX = event.clientX; hoverY = event.clientY;
      settleHover(card); sync();
    });
    // Wait for a stationary pointer instead of selecting on brief crossings.
    card.addEventListener('pointermove', event => {
      if (hovered !== card || card.classList.contains('is-hovered')) return;
      if (Math.hypot(event.clientX-hoverX, event.clientY-hoverY) < 4) return;
      hoverX = event.clientX; hoverY = event.clientY;
      settleHover(card);
    });
    card.addEventListener('pointerleave', () => {
      if (hovered === card) {
        clearTimeout(hoverTimer);
        leaveTimer = setTimeout(() => {clearHover(); sync();}, 180);
      }
    });
    card.addEventListener('pointerdown', () => {clearTimeout(hoverTimer); selectCard(card);});
    card.addEventListener('focus', () => {
      if (!card.matches(':focus-visible')) return;
      focused = card;
      selected = Number(card.closest('.wf-column').dataset.group);
      choose(selected);
      // Bring the stable keyboard copy into view, independently of the loop phase.
      viewport.scrollTop = viewport.scrollLeft = 0;
      panorama.scrollLeft = 0;
      position(false);
      const lane = lanes[selected];
      lane.phase += viewport.getBoundingClientRect().top + 65 - card.getBoundingClientRect().top;
      paint(lane, false); selectCard(card); sync();
    });
    card.addEventListener('blur', () => {focused = null; lanes.forEach(lane => paint(lane)); sync();});
  });
  root.addEventListener('keydown', event => {
    if (event.key === 'Escape') {clearHover(); focused?.blur(); sync();}
  });
  document.addEventListener('visibilitychange', () => {if (document.hidden) clearHover(); sync();});
  addEventListener('blur', () => {clearHover(); sync();});
  reduced.addEventListener('change', () => {updateMotion(); sync();});
  new IntersectionObserver(([entry]) => {visible = entry.isIntersecting; if (!visible) clearHover(); sync();}, {threshold:.05}).observe(viewport);
  new ResizeObserver(() => {measure(); sync();}).observe(viewport);
  root.hidden = false;
  translate();
  choose(selected);
  document.fonts.ready.then(() => {measure(); sync();});
  return {translate};
}
