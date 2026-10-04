import assert from 'node:assert/strict';
import { normalizePanDeg, clampTiltDeg, gimbalBasis } from '../src/js/core/gimbalMath.js';

const eps=1e-10;
const close=(a,b,label='')=>assert.ok(Math.abs(a-b)<eps,`${label}: ${a} != ${b}`);
const vec=(got,want,label)=>Object.keys(want).forEach(k=>close(got[k],want[k],`${label}.${k}`));
const dot=(a,b)=>a.x*b.x+a.y*b.y+a.z*b.z;
const mag=(a)=>Math.hypot(a.x,a.y,a.z);

for (const [input,expected] of [[0,0],[360,0],[450,90],[-90,270],[720,0]]) {
  assert.equal(normalizePanDeg(input),expected);
}
assert.equal(normalizePanDeg(NaN),0);
for (const [input,expected] of [[-1,0],[0,0],[90,90],[180,180],[181,180]]) {
  assert.equal(clampTiltDeg(input),expected);
}
vec(gimbalBasis(0,0).direction,{x:1,y:0,z:0},'front');
vec(gimbalBasis(90,0).direction,{x:0,y:-1,z:0},'right');
vec(gimbalBasis(180,0).direction,{x:-1,y:0,z:0},'back-pan');
vec(gimbalBasis(270,0).direction,{x:0,y:1,z:0},'left');
vec(gimbalBasis(0,90).direction,{x:0,y:0,z:-1},'down');
vec(gimbalBasis(0,180).direction,{x:-1,y:0,z:0},'back-tilt');
let cases=0;
for (const pan of [0,1,45,90,179,270,359]) for (const tilt of [0,1,30,90,120,179,180]) {
  const {direction,up,right}=gimbalBasis(pan,tilt);
  close(mag(direction),1,'direction norm'); close(mag(up),1,'up norm'); close(mag(right),1,'right norm');
  close(dot(direction,up),0,'d.u'); close(dot(direction,right),0,'d.r'); close(dot(up,right),0,'u.r');
  cases++;
}
console.log(`PASS gimbal math: ${cases} orthonormal basis cases, pan wrap, tilt clamp, 6 directions`);
