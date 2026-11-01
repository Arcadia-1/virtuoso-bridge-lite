import { createWaterfall } from './waterfall.mjs?v=20261030-controls';

const archive = document.querySelector('.archive[data-waterfall]');
// The five static columns are usable before scripts load. Only loop copies are deferred.
const approaching = new IntersectionObserver(entries => {
  if (!entries.some(entry => entry.isIntersecting)) return;
  createWaterfall(archive);
  approaching.disconnect();
}, {rootMargin: '300px 0px'});
approaching.observe(archive);
