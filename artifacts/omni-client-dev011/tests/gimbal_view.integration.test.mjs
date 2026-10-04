import assert from 'node:assert/strict';

class ClassList {
  constructor(){this.s=new Set();}
  toggle(k,v){if(v===undefined)v=!this.s.has(k); v?this.s.add(k):this.s.delete(k);return v;}
  contains(k){return this.s.has(k);}
}
class El {
  constructor(id){this.id=id;this.classList=new ClassList();this.style={};this.textContent='';this.value='';this.tagName='DIV';this.handlers={};}
  addEventListener(k,fn){(this.handlers[k]??=[]).push(fn);}
  dispatch(k,e={}){for(const fn of this.handlers[k]||[])fn(e);}
}
const ids=new Map();
for(const id of ['gimbalPanel','gimbalAngles','gimbalPan','gimbalTilt','gimbalForwardBtn','gimbalDownBtn','gimbalBackBtn']) ids.set(id,new El(id));
ids.get('gimbalPan').tagName='INPUT'; ids.get('gimbalTilt').tagName='INPUT';
const docHandlers={};
globalThis.document={
  getElementById:(id)=>ids.get(id)||new El(id),
  querySelectorAll:()=>[],
  addEventListener:(k,fn)=>(docHandlers[k]??=[]).push(fn),
  dispatch:(k,e)=>{for(const fn of docHandlers[k]||[])fn(e);}
};
globalThis.window={addEventListener:()=>{}};
globalThis.localStorage={getItem:()=>null,setItem:()=>{}};
class Cartesian3 {constructor(x=0,y=0,z=0){this.x=x;this.y=y;this.z=z;}clone(){return new Cartesian3(this.x,this.y,this.z);}}
Cartesian3.UNIT_X=new Cartesian3(1,0,0);Cartesian3.UNIT_Z=new Cartesian3(0,0,1);
class Matrix4 {constructor(name='m'){this.name=name;}}
Matrix4.IDENTITY=new Matrix4('identity');
Matrix4.equals=(a,b)=>a===b || (a&&b&&a.name===b.name);
Matrix4.inverseTransformation=(_m,out)=>out;
Matrix4.multiplyByPoint=(_m,p,out)=>Object.assign(out,p);
class HeadingPitchRoll {constructor(heading=0,pitch=0,roll=0){Object.assign(this,{heading,pitch,roll});}}
class HeadingPitchRange {constructor(heading=0,pitch=0,range=0){Object.assign(this,{heading,pitch,range});}}
class Cartographic {}
Cartographic.fromDegrees=()=>new Cartographic();
Cartographic.fromCartesian=()=>({latitude:0,longitude:0});
globalThis.Cesium={
 Cartesian3,Matrix4,HeadingPitchRoll,HeadingPitchRange,Cartographic,
 Cartesian2:class {constructor(x,y){this.x=x;this.y=y;}},
 Color:{YELLOW:{},BLACK:{},WHITE:{}}, LabelStyle:{FILL_AND_OUTLINE:1},HeightReference:{NONE:0},
 Ellipsoid:{WGS84:{}},
 Math:{toRadians:d=>d*Math.PI/180,toDegrees:r=>r*180/Math.PI},
 Transforms:{
  headingPitchRollToFixedFrame:(_p,_h,_e,_u,out)=>out||new Matrix4('body'),
  eastNorthUpToFixedFrame:(_p,_e,out)=>out||new Matrix4('enu')
 }
};
const {state}=await import('../src/js/state.js');
const {VIEWS,setTrainingView,updateViewCamera,setGimbalAngles,getGimbalAngles,resetGimbal,renderSlavedCamera}=await import('../src/js/core/views.js');
const cam={
 transform:Matrix4.IDENTITY,position:new Cartesian3(),direction:new Cartesian3(1,0,0),
 up:new Cartesian3(0,0,1),right:new Cartesian3(0,-1,0),positionWC:new Cartesian3(100,200,300),
 directionWC:new Cartesian3(1,0,0),upWC:new Cartesian3(0,0,1),rightWC:new Cartesian3(0,-1,0),
 frustum:{fov:1},lookAtTransform(m){this.transform=m;},setView(){}
};
state.viewer={camera:cam,trackedEntity:null,scene:{screenSpaceCameraController:{enableInputs:true},canvas:{addEventListener(){}},globe:{getHeight(){return 0;}}},entities:{add(){return {};}}};
setTrainingView(VIEWS.GIMBAL,true);
assert.equal(ids.get('gimbalPanel').classList.contains('active'),true);
assert.equal(state.viewer.scene.screenSpaceCameraController.enableInputs,false);
ids.get('gimbalPan').value='270';ids.get('gimbalPan').dispatch('input');assert.equal(getGimbalAngles().panDeg,270);
ids.get('gimbalTilt').value='180';ids.get('gimbalTilt').dispatch('input');assert.equal(getGimbalAngles().tiltDeg,180);
ids.get('gimbalDownBtn').dispatch('click');assert.deepEqual(getGimbalAngles(),{panDeg:0,tiltDeg:90});
document.dispatch('keydown',{key:'d',shiftKey:false,target:{tagName:'BODY'},preventDefault(){}});
assert.equal(getGimbalAngles().panDeg,2);
document.dispatch('keydown',{key:'w',shiftKey:true,target:{tagName:'BODY'},preventDefault(){}});
assert.equal(getGimbalAngles().tiltDeg,80);
resetGimbal('forward');assert.deepEqual(getGimbalAngles(),{panDeg:0,tiltDeg:0});
const pos=new Cartesian3(10,20,30),pose={headingDeg:0,pitchDeg:0,rollDeg:0};
updateViewCamera(pos,pose);
const approx=(a,b)=>Math.abs(a-b)<1e-10;
assert.ok(approx(cam.position.x,0.85)&&approx(cam.position.y,0)&&approx(cam.position.z,-0.25));
assert.ok(approx(cam.direction.x,1)&&approx(cam.direction.y,0)&&approx(cam.direction.z,0));
setGimbalAngles(90,0);updateViewCamera(pos,pose);
assert.ok(approx(cam.direction.x,0)&&approx(cam.direction.y,-1));
setGimbalAngles(0,90);updateViewCamera(pos,pose);
assert.ok(approx(cam.direction.x,0)&&approx(cam.direction.z,-1));
resetGimbal('back');updateViewCamera(pos,pose);
assert.ok(approx(cam.direction.x,-1)&&approx(cam.direction.y,0));
setGimbalAngles(450,999);assert.deepEqual(getGimbalAngles(),{panDeg:90,tiltDeg:180});
state.lastCameraPose={position:pos,headingDeg:12,pitchDeg:3,rollDeg:-4};
renderSlavedCamera();
setTrainingView(VIEWS.FREE,true);
assert.equal(ids.get('gimbalPanel').classList.contains('active'),false);
assert.equal(state.viewer.scene.screenSpaceCameraController.enableInputs,true);
console.log('PASS gimbal view integration (mock Cesium): controls, mount, directions, frame and switching');
