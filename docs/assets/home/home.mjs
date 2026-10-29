// The SVG is the static, accessible diagram. Only small HTML signal layers move.
const figure = document.querySelector('.hero-flow');
if (figure) {
  const svg = figure.querySelector('svg');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const animations = [];
  const cycle = 4400, entrance = 1800;
  let visible = false, introduced = false, plane;

  function prepareMotion() {
    if (plane) return;
    plane = document.createElement('div');
    plane.className = 'system-motion';
    plane.setAttribute('aria-hidden', 'true');
    const fragment = document.createDocumentFragment();
    const animate = (element, frames, delay = 0) => {
      fragment.append(element);
      const animation = element.animate(frames, {
        duration: cycle, delay: entrance + delay, iterations: Infinity,
        easing: 'linear', fill: 'both',
      });
      animation.pause();
      animations.push(animation);
    };
    for (const path of svg.querySelectorAll('.system-routes path')) {
      const input = path.dataset.geometry === 'input';
      const start = input ? 0 : 1250, duration = input ? 1250 : 2850;
      const length = path.getTotalLength();
      // Sample geometry once, never read layout or update styles on animation frames.
      const frames = Array.from({length: 25}, (_, i) => {
        const point = path.getPointAtLength(length * i / 24);
        return {
          offset: (start + duration * i / 24) / cycle,
          transform: `translate(${point.x - 4.5}px,${point.y - 4.5}px)`,
          opacity: i === 0 || i === 24 ? 0 : 1,
        };
      });
      if (start) frames.unshift({...frames[0], offset: 0});
      frames.push({...frames.at(-1), offset: 1});
      // Overlapping round samples form a short stroke that follows the curve.
      for (let i = 0; i < 5; i++) {
        const packet = document.createElement('i');
        packet.className = 'system-packet';
        animate(packet, frames, i * 38);
      }
    }
    for (const node of svg.querySelectorAll('.node-bridge,.node-tool')) {
      const bridge = node.classList.contains('node-bridge');
      const pulse = document.createElement('i');
      pulse.className = 'system-node-light';
      const radius = bridge ? 8 : 5.5;
      pulse.style.cssText = `left:${node.cx.baseVal.value-radius}px;top:${node.cy.baseVal.value-radius}px;width:${radius*2}px;height:${radius*2}px`;
      const arrival = bridge ? 1250 : 4100;
      animate(pulse, [
        {opacity: 0, offset: 0}, {opacity: 0, offset: (arrival-50)/cycle},
        {opacity: 1, offset: (arrival+50)/cycle},
        {opacity: 0, offset: Math.min(1,(arrival+250)/cycle)}, {opacity: 0, offset: 1},
      ]);
    }
    plane.append(fragment);
    figure.append(plane);
    const resize = () => {plane.style.transform = `scale(${svg.getBoundingClientRect().width/svg.viewBox.baseVal.width})`;};
    new ResizeObserver(resize).observe(svg);
    resize();
  }
  function sync() {
    const running = visible && !document.hidden && !reduced.matches;
    if (running) prepareMotion();
    figure.classList.toggle('system-running', running);
    figure.classList.toggle('system-reduced', reduced.matches);
    if (running && !introduced) {
      introduced = true;
      figure.classList.add('system-enter');
    }
    for (const animation of animations) {
      if (running && animation.playState !== 'running') animation.play();
      else if (!running && animation.playState === 'running') animation.pause();
    }
  }
  new IntersectionObserver(([entry]) => {visible = entry.isIntersecting; sync();}, {threshold: .15}).observe(figure);
  document.addEventListener('visibilitychange', sync);
  reduced.addEventListener('change', sync);
  sync();
}
