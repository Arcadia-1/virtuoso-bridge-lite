// One continuous track; retargeting preserves both position and velocity.
export const wrap = (n, length) => ((n % length) + length) % length;
export const occurrence = (index, center, length) => index + Math.round((center - index) / length) * length;
export function damp(state, target, dt, rate = 10) {
  const offset = state.value - target;
  const impulse = state.velocity + rate * offset;
  const decay = Math.exp(-rate * dt);
  state.value = target + (offset + impulse * dt) * decay;
  state.velocity = (state.velocity - rate * impulse * dt) * decay;
}
export function wave(distance, age) {
  if (age <= 0 || age >= 2.2) return 0;
  const envelope = Math.sin(Math.PI * age / 2.2) ** 2;
  const crest = Math.abs(distance) - age * 3.8;
  return envelope * (Math.exp(-((crest / 1.35) ** 2)) - .22 * Math.exp(-(((crest + 1.9) / 1.5) ** 2)));
}
export function visibility(distance, count) {
  const edge = Math.min(4, count / 2);
  const fade = Math.max(0, Math.min(1, (edge - Math.abs(distance)) / .65));
  return fade * fade * (3 - 2 * fade);
}
