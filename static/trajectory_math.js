// Pure estimation math. No DOM, serial access, or device commands.
export const GRAVITY = 9.80665;
const norm = v => Math.hypot(...v);
const vec = v => v && ['x','y','z'].every(k=>Number.isFinite(v[k])) ? [v.x,v.y,v.z] : null;
export function normalizeQuaternion(v) {
  if(!v || !['w','x','y','z'].every(k=>Number.isFinite(v[k]))) return null;
  const n=Math.hypot(v.w,v.x,v.y,v.z);
  return n>.1&&n<2 ? [v.w/n,v.x/n,v.y/n,v.z/n] : null;
}
export function eulerQuaternion(v) {
  if(!v || !['roll','pitch','yaw'].every(k=>Number.isFinite(v[k]))) return null;
  const [x,y,z]=[v.roll,v.pitch,v.yaw].map(a=>a*Math.PI/360),cx=Math.cos(x),sx=Math.sin(x),cy=Math.cos(y),sy=Math.sin(y),cz=Math.cos(z),sz=Math.sin(z);
  return [cx*cy*cz+sx*sy*sz,sx*cy*cz-cx*sy*sz,cx*sy*cz+sx*cy*sz,cx*cy*sz-sx*sy*cz];
}
export function rotateVector(v,q) {
  const [w,x,y,z]=q,[a,b,c]=v;
  return [(1-2*(y*y+z*z))*a+2*(x*y-z*w)*b+2*(x*z+y*w)*c,2*(x*y+z*w)*a+(1-2*(x*x+z*z))*b+2*(y*z-x*w)*c,2*(x*z-y*w)*a+2*(y*z+x*w)*b+(1-2*(x*x+y*y))*c];
}
const percentile=(values,fraction)=>{const ordered=values.slice().sort((a,b)=>a-b),offset=(ordered.length-1)*fraction,low=Math.floor(offset),high=Math.ceil(offset);return ordered[low]+(ordered[high]-ordered[low])*(offset-low);};
const referenceRanges={referenceGyroMax:[.01,1],referenceAccelerationStd:[.02,3],referenceOutlierFraction:[0,.2],referenceSeconds:[1,10]};
const settings = {maxGap:.1,maxAttitudeAge:.05,referenceSeconds:2,referenceSamples:50,maxReferenceWait:30,referenceGyroMax:.15,referenceAccelerationStd:.35,referenceOutlierFraction:.10,stillGyro:.035,stillAcceleration:.12,stillSeconds:.35,maxSeconds:120,maxPoints:12000,pointPeriod:.01};

export class TrajectoryEstimator {
  constructor(options={}) {this.options={...settings,...options};this.zupt=true;this.source='none';this.generation=-1;this.reset();}
  reset() {
    this.active=false;this.calibrating=false;this.reference=null;this.referenceWindow=[];this.referenceStarted=null;this.referenceProgress=0;this.referenceQuality=null;this.referenceEvaluated=null;
    this.position=[0,0,0];this.velocity=[0,0,0];this.linear=[0,0,0];this.distance=0;this.points=[];this.last=null;this.origin=null;this.segment=0;this.stationary=false;this.stillSince=null;
    this.q=null;this.euler=null;this.gyro=null;this.orientation=null;this.orientationKind='none';this.lastSeen=null;this.lastPointTime=null;this.coalesced=0;this.skipped=0;this.accepted=0;this.gaps=0;
    this.message='先保持模块静止，建立参考';this.timeBasis=this.source==='playback'?'recorded_time':'host_receive_time';
  }
  setSource(source,generation) {
    if(this.source===source&&this.generation===generation)return false;
    this.source=source;this.generation=generation;this.reset();this.message='数据源已变化；轨迹已清空，请重新建立静止参考';return true;
  }
  setOptions(options){
    if(!options||typeof options!=='object'||Array.isArray(options)||!Object.keys(options).length||Object.keys(options).some(k=>k!=='zupt'&&!(k in referenceRanges)))throw Error('无效的轨迹选项');
    if('zupt' in options&&typeof options.zupt!=='boolean')throw Error('zupt 必须为布尔值');
    for(const [name,[low,high]] of Object.entries(referenceRanges))if(name in options&&(typeof options[name]!=='number'||!Number.isFinite(options[name])||options[name]<low||options[name]>high))throw Error(name+' 数值超出范围');
    const changed=Object.keys(referenceRanges).some(k=>k in options&&options[k]!==this.options[k]);
    for(const k of Object.keys(referenceRanges))if(k in options)this.options[k]=options[k];
    if('zupt' in options){this.zupt=options.zupt;this.stillSince=null;this.stationary=false;}
    if(changed){this.reset();this.message='静止参考选项已变化；轨迹已清空，请重新建立参考';}
    return {zupt:this.zupt,...Object.fromEntries(Object.keys(referenceRanges).map(k=>[k,this.options[k]]))};
  }
  beginReference() {
    this.reset();this.calibrating=true;this.message=`保持静止至少 ${this.options.referenceSeconds} 秒；这是上位机估计，不修改模块校准`;
  }
  start() {
    if(!this.reference || this.source==='none'){this.message='先选择数据源并建立静止参考';return false;}
    if(this.points.length>=this.options.maxPoints || (this.points.length && this.points.at(-1).elapsed>=this.options.maxSeconds)){this.message='已达到本次追踪上限，请清空后重新建立参考';return false;}
    this.active=true;this.calibrating=false;this.velocity=[0,0,0];this.last=null;this.stillSince=null;this.stationary=false;this.segment++;this.message='追踪中 · 六轴积分估算，会随时间漂移';return true;
  }
  pause(reason='已暂停；恢复时速度从零开始') {this.active=false;this.calibrating=false;this.last=null;this.velocity=[0,0,0];this.stillSince=null;this.stationary=false;this.message=reason;}
  gap(reason='数据有缺口，已暂停；不会跨缺口积分') {this.gaps++;this.pause(reason);}
  consume(samples) {
    // The service timestamp belongs to a USB receive chunk, not each frame.
    // Average multiple acceleration frames with identical times once instead
    // of manufacturing a device clock or treating zero dt as another step.
    const groups=new Map();
    for(const s of samples) {
      if(!s || !['acceleration','angular_velocity','quaternion','euler'].includes(s.channel))continue;
      const t=this.source==='playback'?s.measurement_time:s.time;
      if(!Number.isFinite(t)){if(this.source==='playback'&&(this.active||this.calibrating))this.gap('回放缺少原始采样时间，已暂停；不能用回放发送时间积分');continue;}
      let g=groups.get(t);if(!g){g={time:t,hostTime:s.time,acc:[],id:s.slave_id};groups.set(t,g);}
      if(g.id!==s.slave_id){g.mixed=true;continue;}
      if(s.channel==='acceleration'){const a=vec(s.values);if(a)g.acc.push(a);}
      if(s.channel==='angular_velocity'){const v=vec(s.values);if(v)g.gyro=v;}
      if(s.channel==='quaternion'){const q=normalizeQuaternion(s.values);if(q)g.q=q;}
      if(s.channel==='euler'){const q=eulerQuaternion(s.values);if(q)g.euler=q;}
    }
    for(const g of [...groups.values()].sort((a,b)=>a.time-b.time))this.consumeGroup(g);
  }
  consumeGroup(g) {
    const t=g.time;
    if(g.q)this.q={time:t,value:g.q,id:g.id};if(g.euler)this.euler={time:t,value:g.euler,id:g.id};if(g.gyro)this.gyro={time:t,value:g.gyro,id:g.id};
    if(!g.acc.length)return;
    if(this.lastSeen!==null&&t<=this.lastSeen){this.skipped++;return;}
    this.lastSeen=t;this.coalesced+=Math.max(0,g.acc.length-1);
    if(g.mixed){if(this.active||this.calibrating)this.gap('同时间出现不同从机 ID，已暂停');return;}
    const age=this.options.maxAttitudeAge,valid=r=>r&&r.id===g.id&&t-r.time>=-1e-9&&t-r.time<=age;
    let attitude,kind;
    if(valid(this.q)){attitude=this.q.value;kind='quaternion';}
    else if(valid(this.euler)){attitude=this.euler.value;kind='euler_zyx';}
    else {this.skipped++;if(this.active||this.calibrating)this.gap('姿态数据缺失或相差超过 50 ms，已暂停');return;}
    this.orientation=attitude;this.orientationKind=kind;
    const accel=g.acc[0].map((_,i)=>g.acc.reduce((sum,a)=>sum+a[i],0)/g.acc.length),world=rotateVector(accel,attitude),gyro=valid(this.gyro)?norm(this.gyro.value):null;
    if(world.some(v=>!Number.isFinite(v))||norm(world)>500){if(this.active||this.calibrating)this.gap('加速度异常，已暂停');return;}
    if(this.calibrating){this.captureReference(t,world,gyro);return;}
    if(!this.active)return;
    if(this.points.length>=this.options.maxPoints){this.pause('轨迹点数达到 12000 上限，请先导出，再清空重试');return;}
    const linear=world.map((v,i)=>v-this.reference[i]);this.linear=linear;
    if(gyro!==null&&gyro<this.options.stillGyro&&norm(linear)<this.options.stillAcceleration){if(this.stillSince===null)this.stillSince=t;this.stationary=this.zupt&&t-this.stillSince>=this.options.stillSeconds;}
    else {this.stillSince=null;this.stationary=false;}
    if(this.origin!==null&&t-this.origin>=this.options.maxSeconds){this.pause('本次追踪达到 120 秒上限；长时间积分漂移明显，请清空后重试');return;}
    if(this.last===null){if(this.origin===null)this.origin=t;this.last={time:t,acc:linear};this.appendPoint(t,g.hostTime);return;}
    const dt=t-this.last.time;
    if(dt>this.options.maxGap){this.gap('采样间隔超过 100 ms，已暂停；恢复时重新从零速度积分');return;}
    const old=this.position.slice();
    if(this.stationary)this.velocity=[0,0,0];
    else for(let i=0;i<3;i++){const average=(this.last.acc[i]+linear[i])/2;this.position[i]+=this.velocity[i]*dt+.5*average*dt*dt;this.velocity[i]+=average*dt;}
    this.distance+=norm(this.position.map((v,i)=>v-old[i]));this.last={time:t,acc:linear};this.accepted++;
    this.appendPoint(t,g.hostTime);
  }
  captureReference(t,world,gyro) {
    if(this.referenceStarted===null)this.referenceStarted=t;
    if(t-this.referenceStarted>this.options.maxReferenceWait){this.pause('30 秒内未获得稳定参考；检查加速度、角速度和姿态通道');return;}
    if(gyro===null){this.referenceWindow=[];this.referenceProgress=0;this.referenceQuality={ready:false,reason:'missing_gyro',samples:0,elapsed:0};this.message='等待同一从机的角速度数据';return;}
    if(this.referenceWindow.length&&t-this.referenceWindow.at(-1).time>this.options.maxGap)this.referenceWindow=[];
    this.referenceWindow.push({time:t,world,gyro});
    if(this.referenceEvaluated!==null&&t-this.referenceEvaluated<.1-1e-9)return;
    this.referenceEvaluated=t;
    const cutoff=t-this.options.referenceSeconds;let first=0;
    while(first+1<this.referenceWindow.length&&this.referenceWindow[first+1].time<=cutoff)first++;
    this.referenceWindow=this.referenceWindow.slice(first).slice(-30000);
    const window=this.referenceWindow,elapsed=t-window[0].time;this.referenceProgress=Math.min(1,elapsed/this.options.referenceSeconds);
    const center=[0,1,2].map(i=>percentile(window.map(r=>r.world[i]),.5)),residual=window.map(r=>norm(r.world.map((v,i)=>v-center[i])));
    const threshold=Math.max(3*this.options.referenceAccelerationStd,6*percentile(residual,.5),.05);
    const inliers=window.filter((r,i)=>residual[i]<=threshold&&r.gyro<=this.options.referenceGyroMax),fraction=1-inliers.length/window.length;
    const mean=inliers.length?[0,1,2].map(i=>inliers.reduce((a,r)=>a+r.world[i],0)/inliers.length):center;
    const rms=inliers.length?Math.sqrt(inliers.reduce((a,r)=>a+r.world.reduce((b,v,i)=>b+(v-mean[i])**2,0),0)/inliers.length):null;
    const gyroP95=inliers.length?percentile(inliers.map(r=>r.gyro),.95):null,half=(window[0].time+t)/2;
    const halves=[true,false].map(before=>inliers.filter(r=>(r.time<half)===before));
    const means=halves.filter(part=>part.length).map(part=>[0,1,2].map(i=>part.reduce((a,r)=>a+r.world[i],0)/part.length));
    const shift=means.length===2?norm(means[1].map((v,i)=>v-means[0][i])):null,magnitude=norm(mean);
    const aligned=magnitude>0&&mean[2]>0&&mean[2]/magnitude>Math.cos(15*Math.PI/180);
    const reason=elapsed+1e-9<this.options.referenceSeconds?'window':fraction>this.options.referenceOutlierFraction+1e-12?'outliers':inliers.length<this.options.referenceSamples?'window':!aligned?'gravity_direction':Math.abs(magnitude-GRAVITY)>.6?'gravity_magnitude':rms===null||rms>this.options.referenceAccelerationStd?'acceleration_noise':shift===null||shift>this.options.referenceAccelerationStd?'motion_trend':'ready';
    this.referenceQuality={ready:reason==='ready',reason,samples:window.length,inliers:inliers.length,elapsed,accelerationRms:rms,gyroP95,outlierFraction:fraction,meanShift:shift,gravityMagnitude:magnitude};
    const messages={window:'正在累计静止窗口；少量尖峰不会清零进度',outliers:'窗口中运动或尖峰过多，请保持模块静止',gravity_direction:'世界重力方向不符合 +Z 约定；请核对姿态和安装方向',gravity_magnitude:'平均加速度偏离重力，请保持静止并核对量纲',acceleration_noise:'窗口加速度噪声过大；保持静止或调整参考噪声阈值',motion_trend:'窗口前后加速度变化明显，请保持模块静止'};
    if(reason!=='ready'){this.message=messages[reason];return;}
    this.reference=mean;this.referenceMagnitude=norm(mean);this.referenceWindow=[];this.calibrating=false;this.referenceProgress=1;this.message='静止参考已建立；点击开始追踪。参考只适用于本次短时估计';
  }
  appendPoint(time,hostTime) {
    if(this.lastPointTime!==null&&time-this.lastPointTime<this.options.pointPeriod&&!this.stationary)return;
    if(this.lastPointTime!==null&&time-this.lastPointTime<this.options.pointPeriod)return;
    this.lastPointTime=time;
    this.points.push({time,host_time:hostTime,elapsed:time-this.origin,segment:this.segment,position:this.position.slice(),velocity:this.velocity.slice(),acceleration:this.linear.slice(),distance:this.distance,stationary:this.stationary,orientation_kind:this.orientationKind,quaternion:this.orientation.slice()});
  }
  snapshot() {return {active:this.active,calibrating:this.calibrating,reference:this.reference?.slice()||null,referenceProgress:this.referenceProgress,position:this.position.slice(),velocity:this.velocity.slice(),acceleration:this.linear.slice(),distance:this.distance,elapsed:this.points.at(-1)?.elapsed||0,stationary:this.stationary,source:this.source,generation:this.generation,points:this.points.length,coalesced:this.coalesced,skipped:this.skipped,gaps:this.gaps,message:this.message,timeBasis:this.timeBasis,orientationKind:this.orientationKind,zupt:this.zupt,referenceOptions:Object.fromEntries(Object.keys(referenceRanges).map(k=>[k,this.options[k]])),referenceQuality:this.referenceQuality?structuredClone(this.referenceQuality):null};}
  exportPayload(format) {return {format,source:this.source,generation:this.generation,gravity_reference:this.reference?.slice()||[0,0,GRAVITY],timing:this.timeBasis,estimate:true,points:this.points.map(p=>({...p,position:p.position.slice(),velocity:p.velocity.slice(),acceleration:p.acceleration.slice(),quaternion:p.quaternion.slice()}))};}
}
