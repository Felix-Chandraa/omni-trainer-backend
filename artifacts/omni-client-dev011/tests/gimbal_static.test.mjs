import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const rd=f=>readFileSync(new URL('../'+f, import.meta.url),'utf8');
const html=rd('map.html'), views=rd('src/js/core/views.js'),main=rd('src/js/main.js');
const registry=rd('src/js/widgets/registry.js');
const profile=JSON.parse(rd('vehicle_profiles/fixed_wing.json'));
assert.match(html, /data-view="gimbal">Gimbal<\/button>/);
assert.doesNotMatch(html, /data-view="gimbal"\s+data-widget=/);
assert.match(html, /id="gimbalPanel"\s+data-widget="gimbal_control"/);
assert.match(registry, /key:\s*"gimbal_control"/);
assert.ok(profile.dashboard.includes('gimbal_control'));
for (const [k,v] of [['1','FPV'],['2','TAIL'],['3','FOLLOW'],['4','GROUND'],['5','FREE'],['6','GIMBAL']]) {
  assert.ok(main.includes(`"${k}": VIEWS.${v}`),`${k} -> ${v}`);
}
for (const v of ['fpv','tail','follow','ground','free','gimbal']) {
  assert.ok(views.includes(`"${v}"`),`${v} is present`);
}
assert.match(views,/gimbalCamera\(position, pose\)/);
assert.match(views,/renderSlavedCamera\(\)/);
console.log('PASS gimbal static: legacy views 1-5, view 6, UI and widget registry');
