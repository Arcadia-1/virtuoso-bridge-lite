import assert from 'node:assert/strict';
import { test } from 'vitest';
import { damp, wave, occurrence, visibility, wrap } from '../docs/assets/home/motion.mjs';

test('reversing mid-flight preserves position and velocity and settles', () => {
  const state={value:0,velocity:0};
  for(let i=0;i<12;i++)damp(state,3,1/60,8);
  const before={...state};
  damp(state,-2,0,8);
  assert.ok(Math.abs(state.value-before.value)<1e-12);
  assert.ok(Math.abs(state.velocity-before.velocity)<1e-12);
  for(let i=0;i<240;i++)damp(state,-2,1/60,8);
  assert.ok(Math.abs(state.value+2)<1e-9);
  assert.ok(Math.abs(state.velocity)<1e-8);
});
test('timing is stable at both 30 and 120 frames per second', () => {
  const a={value:0,velocity:0},b={...a};
  for(let i=0;i<30;i++)damp(a,2,1/30,8);
  for(let i=0;i<120;i++)damp(b,2,1/120,8);
  assert.ok(Math.abs(a.value-b.value)<1e-12);
});
test('wave starts and ends without a displacement jump', () => {
  for(const d of [0,1,2,3,4]) {
    assert.equal(wave(d,0),0);assert.equal(wave(d,2.2),0);
    assert.ok(Math.abs(wave(d,.00001))<1e-8);
    assert.ok(Math.abs(wave(d,2.2-.00001))<1e-8);
  }
});
test('loop seam is invisible, including the four-project collection', () => {
  for(const count of [4,10,11,14]) {
    for(let id=0;id<count;id++) {
      const seam=id+count/2;
      const a=occurrence(id,seam-.00001,count),b=occurrence(id,seam+.00001,count);
      assert.equal(Math.abs(a-b),count);
      assert.ok(visibility(a-(seam-.00001),count)<1e-8);
      assert.ok(visibility(b-(seam+.00001),count)<1e-8);
    }
  }
});
test('rapid forward/back targets do not reset the shared track', () => {
  const state={value:0,velocity:0};let target=0;
  for(let i=0;i<90;i++) {
    target+=i%5===0?-1:1;
    const prev=state.value;
    damp(state,target,1/120,8);
    assert.ok(Number.isFinite(state.value)&&Number.isFinite(state.velocity));
    assert.ok(Math.abs(state.value-prev)<1);
  }
  for(let i=0;i<600;i++)damp(state,target,1/60,8);
  assert.ok(Math.abs(state.value-target)<1e-9);
  assert.equal(wrap(-1,14),13);
});
