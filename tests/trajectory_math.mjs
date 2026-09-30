// Synthetic tests only. Never reads or controls an actual device.
import assert from 'node:assert/strict';
import {TrajectoryEstimator, GRAVITY, rotateVector, eulerQuaternion, normalizeQuaternion} from '../static/trajectory_math.js';
const near=(a,b,tol=1e-8)=>assert.ok(Math.abs(a-b)<tol,`${a} != ${b}`);
const vnear=(a,b,tol=1e-8)=>a.forEach((v,i)=>near(v,b[i],tol));
const identity=[1,0,0,0];
const sample=(channel,values,time,extra={})=>({channel,values,time,slave_id:1,...extra});
const rows=(t,a=[0,0,GRAVITY],q=identity,g=[0,0,0],extra={})=>[sample('acceleration',{x:a[0],y:a[1],z:a[2]},t,extra),sample('angular_velocity',{x:g[0],y:g[1],z:g[2]},t,extra),sample('quaternion',{w:q[0],x:q[1],y:q[2],z:q[3]},t,extra)];
const calibrated=(q=identity,a=[0,0,GRAVITY])=>{const e=new TrajectoryEstimator();e.setSource('live',1);e.beginReference();for(let i=0;i<=210;i++)e.consume(rows(1000+i*.01,a,q));assert.ok(e.reference);assert.equal(e.active,false);return e;};
// Quaternion device -> world rotation and ZYX Euler use the same proper rotation.
for(const angles of [{roll:90,pitch:0,yaw:0},{roll:0,pitch:90,yaw:0},{roll:0,pitch:0,yaw:90},{roll:21,pitch:-45,yaw:72}]){
  const q=eulerQuaternion(angles),conjugate=[q[0],-q[1],-q[2],-q[3]],vector=[1.5,-2.3,.1];vnear(rotateVector(rotateVector(vector,q),conjugate),vector);
  const body=rotateVector([0,0,GRAVITY],conjugate);vnear(rotateVector(body,q),[0,0,GRAVITY]);
}
assert.equal(normalizeQuaternion({w:0,x:0,y:0,z:0}),null);
assert.equal(normalizeQuaternion({w:NaN,x:0,y:0,z:0}),null);
// Static level and a tilted body must not manufacture motion after gravity removal.
for(const e of [{engine:calibrated(),q:identity,a:[0,0,GRAVITY]},(()=>{const q=eulerQuaternion({roll:38,pitch:-22,yaw:64}),conjugate=[q[0],-q[1],-q[2],-q[3]],a=rotateVector([0,0,GRAVITY],conjugate);return {engine:calibrated(q,a),q,a};})()]){
  const engine=e.engine,q=e.q,a=e.a;engine.start();for(let i=0;i<=400;i++)engine.consume(rows(1003+i*.01,a,q));vnear(engine.position,[0,0,0]);vnear(engine.velocity,[0,0,0]);assert.equal(engine.stationary,true);
}
// Constant known world acceleration: after 2 s, v=a*t and p=a*t^2/2.
const moving=calibrated();moving.zupt=false;moving.start();for(let i=0;i<=200;i++)moving.consume(rows(1003+i*.01,[1,0,GRAVITY]));vnear(moving.velocity,[2,0,0]);vnear(moving.position,[2,0,0]);near(moving.distance,2);
// Same acceleration while the device is rotated gives the same world path.
const q=eulerQuaternion({roll:30,pitch:-18,yaw:76}),inv=[q[0],-q[1],-q[2],-q[3]],bodyGravity=rotateVector([0,0,GRAVITY],inv),rotating=calibrated(q,bodyGravity);rotating.zupt=false;rotating.start();
for(let i=0;i<=200;i++)rotating.consume(rows(1003+i*.01,rotateVector([1,0,GRAVITY],inv),q));vnear(rotating.position,[2,0,0]);vnear(rotating.velocity,[2,0,0]);
// A chunk containing 8 duplicate timestamps has one averaged integration step.
const chunks=calibrated();chunks.zupt=false;chunks.start();for(let i=0;i<=200;i++){const batch=rows(1003+i*.01,[1,0,GRAVITY]);for(let j=0;j<7;j++)batch.push(batch[0]);chunks.consume(batch);}vnear(chunks.position,[2,0,0]);assert.equal(chunks.coalesced,201*7);
// Missing attitude must stop integration; do not extrapolate old orientation.
const absent=calibrated();absent.start();absent.consume(rows(1003));absent.consume([sample('acceleration',{x:1,y:0,z:GRAVITY},1003.06)]);assert.equal(absent.active,false);assert.match(absent.message,/姿态/);vnear(absent.velocity,[0,0,0]);
// Read gaps, source changes, and pauses never bridge position or velocity.
const gap=calibrated();gap.start();gap.consume(rows(1003,[1,0,GRAVITY]));gap.consume(rows(1003.02,[1,0,GRAVITY]));const before=gap.position.slice();gap.consume(rows(1004,[1,0,GRAVITY]));assert.equal(gap.active,false);vnear(gap.position,before);vnear(gap.velocity,[0,0,0]);gap.start();gap.consume(rows(1005,[1,0,GRAVITY]));vnear(gap.position,before);assert.ok(gap.segment>=2);gap.setSource('live',2);assert.equal(gap.reference,null);assert.equal(gap.points.length,0);assert.equal(gap.active,false);
// Euler fallback without quaternion is equivalent, and quaternion wins when present.
const fallback=calibrated();fallback.start();const eulerRows=rows(1003).filter(r=>r.channel!=='quaternion');eulerRows.push(sample('euler',{roll:0,pitch:0,yaw:0},1003));fallback.consume(eulerRows);assert.equal(fallback.orientationKind,'euler_zyx');
const preference=calibrated();preference.start();const both=rows(1003);both.push(sample('euler',{roll:70,pitch:30,yaw:10},1003));preference.consume(both);assert.equal(preference.orientationKind,'quaternion');vnear(preference.linear,[0,0,0]);
// No gyro / moving / inverted gravity reference is never accepted.
for(const variant of ['missingGyro','moving','negativeGravity']){const e=new TrajectoryEstimator();e.setSource('live',1);e.beginReference();for(let i=0;i<=250;i++){let r=rows(1000+i*.01,variant==='negativeGravity'?[0,0,-GRAVITY]:[0,0,GRAVITY],identity,variant==='moving'?[0,0,.5]:[0,0,0]);if(variant==='missingGyro')r=r.filter(s=>s.channel!=='angular_velocity');e.consume(r);}assert.equal(e.reference,null);}
// A 4x replay produces exactly the same trajectory using recorded times.
const playback=new TrajectoryEstimator();playback.setSource('playback',5);playback.beginReference();for(let i=0;i<=210;i++)playback.consume(rows(4000+i*.0025,[0,0,GRAVITY],identity,[0,0,0],{measurement_time:1000+i*.01}));assert.ok(playback.reference);playback.zupt=false;playback.start();for(let i=0;i<=200;i++)playback.consume(rows(4001+i*.0025,[1,0,GRAVITY],identity,[0,0,0],{measurement_time:1003+i*.01}));vnear(playback.position,[2,0,0]);assert.equal(playback.timeBasis,'recorded_time');assert.equal(playback.points[0].host_time,4001);
const missingTime=new TrajectoryEstimator();missingTime.setSource('playback',1);missingTime.beginReference();missingTime.consume(rows(1000));assert.equal(missingTime.calibrating,false);assert.match(missingTime.message,/原始采样时间/);
// Bounds pause the estimator rather than growing memory forever.
const bounded=calibrated();bounded.options.maxPoints=5;bounded.options.pointPeriod=0;bounded.start();for(let i=0;i<10;i++)bounded.consume(rows(1003+i*.01,[1,0,GRAVITY]));assert.equal(bounded.points.length,5);assert.equal(bounded.active,false);vnear(bounded.position,bounded.points.at(-1).position);
const exported=moving.exportPayload('csv');assert.equal(exported.estimate,true);assert.equal(exported.source,'live');assert.equal(exported.points[0].orientation_kind,'quaternion');exported.points[0].position[0]=999;assert.notEqual(moving.points[0].position[0],999);
const resetReference=calibrated();resetReference.start();resetReference.consume(rows(1003));assert.ok(resetReference.points.length);resetReference.beginReference();assert.equal(resetReference.points.length,0);assert.equal(resetReference.reference,null);assert.equal(resetReference.origin,null);
const duration=calibrated();duration.start();duration.consume(rows(1003));duration.pause();duration.start();duration.consume(rows(1130));assert.equal(duration.active,false);assert.equal(duration.points.length,1);
console.log('Trajectory synthetic math passed: static, rotated gravity, known acceleration, chunk timestamps, gaps, playback timing, bounds');
