import * as THREE from './vendor/three.module.js';
import {TrajectoryEstimator, GRAVITY} from './trajectory_math.js';
const basis=new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1,0,0),-Math.PI/2);
const display=v=>new THREE.Vector3(v[0],v[2],-v[1]);
const n=(v,d=3)=>Number.isFinite(v)?v.toFixed(d):'—';
const sources={live:'真实 USB',demo:'演示数据',playback:'录制回放',none:'无数据源'};
const colors=[0xd55c51,0x369a82,0x4b8cca];
function download(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function axes(size) {
  const group=new THREE.Group();
  [[1,0,0],[0,1,0],[0,0,1]].forEach((v,i)=>{
    group.add(new THREE.ArrowHelper(new THREE.Vector3(...v),new THREE.Vector3(),size,colors[i],size*.15,size*.07));
    const c=document.createElement('canvas');c.width=128;c.height=64;const ctx=c.getContext('2d');ctx.fillStyle='#'+colors[i].toString(16);ctx.font='600 38px system-ui';ctx.textAlign='center';ctx.fillText('XYZ'[i],64,47);const texture=new THREE.CanvasTexture(c);texture.colorSpace=THREE.SRGBColorSpace;
    const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:texture,transparent:true,depthTest:false}));sprite.position.set(...v).multiplyScalar(size*1.2);sprite.scale.set(size*.4,size*.2,1);group.add(sprite);
  });return group;
}
function createView(container) {
  const renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));container.prepend(renderer.domElement);
  const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(43,1,.0001,10000),target=new THREE.Vector3();
  const geometry=new THREE.BufferGeometry(),vertices=new Float32Array(12000*6);geometry.setAttribute('position',new THREE.BufferAttribute(vertices,3));geometry.setDrawRange(0,0);
  const material=new THREE.LineBasicMaterial({color:0xd55c51}),path=new THREE.LineSegments(geometry,material);path.frustumCulled=false;scene.add(path);
  const marker=new THREE.Group(),triad=axes(.1);marker.add(triad);marker.add(new THREE.Mesh(new THREE.SphereGeometry(.006,16,12),new THREE.MeshBasicMaterial({color:0xd55c51})));scene.add(marker);
  const origin=axes(1);origin.quaternion.copy(basis);scene.add(origin);
  const grid=new THREE.GridHelper(2,20,0x8c989f,0xcfd5d5);scene.add(grid);
  let width=0,height=0,extent=.25,yaw=.65,pitch=.65,drag=null,dirty=true,stopped=false,points=[];
  const hud=document.createElement('div');hud.className='trajectory-view-hud';hud.textContent='世界坐标 · X / Y / Z · 单位 m';container.append(hud);
  function cameraUpdate(){camera.up.set(0,1,0);camera.position.set(extent*3*Math.cos(pitch)*Math.sin(yaw),extent*3*Math.sin(pitch),extent*3*Math.cos(pitch)*Math.cos(yaw)).add(target);camera.lookAt(target);origin.scale.setScalar(extent*.65);grid.scale.setScalar(extent);marker.scale.setScalar(extent*1.2);hud.textContent=`世界坐标 · 单位 m · 网格 ${n(extent/10,4)} m`;dirty=true;}
  function fit(){if(!points.length){target.set(0,0,0);extent=.25;}else{const box=new THREE.Box3();box.expandByPoint(new THREE.Vector3());points.forEach(p=>box.expandByPoint(display(p.position)));box.getCenter(target);const size=box.getSize(new THREE.Vector3());extent=Math.max(.02,size.length()*.6);}cameraUpdate();}
  cameraUpdate();
  new ResizeObserver(()=>{width=container.clientWidth;height=container.clientHeight;if(width&&height){renderer.setSize(width,height,false);camera.aspect=width/height;camera.updateProjectionMatrix();dirty=true;}}).observe(container);
  container.addEventListener('pointerdown',e=>{if(e.button!==0||e.target.closest('button'))return;drag=[e.clientX,e.clientY];container.setPointerCapture(e.pointerId);});
  container.addEventListener('pointermove',e=>{if(!drag)return;yaw-=(e.clientX-drag[0])*.008;pitch=Math.max(-1.45,Math.min(1.45,pitch+(e.clientY-drag[1])*.008));drag=[e.clientX,e.clientY];cameraUpdate();});
  container.addEventListener('pointerup',()=>{drag=null;});container.addEventListener('pointercancel',()=>{drag=null;});
  container.addEventListener('wheel',e=>{e.preventDefault();extent=Math.max(.005,Math.min(1000,extent*Math.exp(e.deltaY*.001)));cameraUpdate();},{passive:false});
  renderer.domElement.addEventListener('webglcontextlost',e=>{e.preventDefault();stopped=true;hud.textContent='三维上下文丢失；轨迹估计和导出仍可使用';});
  function theme(){const dark=document.documentElement.dataset.theme==='dark';scene.background=new THREE.Color(dark?0x111923:0xf2f3f0);(Array.isArray(grid.material)?grid.material:[grid.material]).forEach(m=>m.color.set(dark?0x344554:0xcbd3d1));material.color.set(dark?0xf58b79:0xd55c51);dirty=true;}
  theme();
  function animate(){requestAnimationFrame(animate);if(!stopped&&dirty&&width&&height){renderer.render(scene,camera);dirty=false;}}requestAnimationFrame(animate);
  return {theme,fit,view(name){yaw=name==='top'?0:.65;pitch=name==='top'?Math.PI/2-.001:.65;cameraUpdate();},update(data,autoFit){points=data;let index=0;for(let i=1;i<data.length;i++){if(data[i].segment!==data[i-1].segment)continue;for(const p of [data[i-1],data[i]]){const v=display(p.position);vertices[index++]=v.x;vertices[index++]=v.y;vertices[index++]=v.z;}}geometry.attributes.position.needsUpdate=true;geometry.setDrawRange(0,index/3);marker.visible=!!data.length;
    if(data.length){const p=data.at(-1);marker.position.copy(display(p.position));const [w,x,y,z]=p.quaternion;marker.quaternion.copy(basis).multiply(new THREE.Quaternion(x,y,z,w));}if(autoFit)fit();dirty=true;},inspect(){return {renderedSegments:geometry.drawRange.count/2,extent,position:marker.position.toArray()};}};
}

export class Trajectory {
  constructor(mount,{onMessage=()=>{}}={}) {
    this.mount=mount;this.estimator=new TrajectoryEstimator();this.onMessage=onMessage;this.visible=false;this.lastState=null;this.lastDataAt=0;this.exportHandler=null;this.view=null;this.installation=null;this.remote=null;this.remoteState=null;this.remotePoints=[];this.remoteEpoch=null;this.remoteGeneration=null;this.remoteVersion=0;this.syncBusy=false;this.syncTimer=null;this.lastSync=0;this.syncDelay=100;this.actionBusy=false;this.syncError=null;
    mount.innerHTML=`<section class="panel trajectory-controls"><div class="trajectory-actions"><button data-action="reference">建立静止参考</button><button data-action="start" class="primary" disabled>开始追踪</button><button data-action="pause" disabled>暂停</button><button data-action="reset">清空轨迹</button><span class="tag" data-value="source">无数据源</span></div><div class="trajectory-options"><label class="check"><input data-option="zupt" type="checkbox" checked>静止零速更新</label><label class="check"><input data-option="fit" type="checkbox" checked>自动适配视野</label><span class="muted">先静止 2 秒 · 最长 120 秒 · 最多 12000 点</span></div><p class="muted small">零速更新假设模块确实停止，匀速平移可能被误判；追踪长距离运动时可关闭。只改变上位机估计，不修改设备参数；重新建立参考会清空旧轨迹，请先导出。</p><div class="trajectory-progress" aria-label="静止参考进度"><div></div></div><div class="trajectory-status" data-value="status" role="status">先保持模块静止，建立参考</div></section><div class="trajectory-grid"><section class="panel trajectory-scene-panel"><div class="panel-title"><h2>三维位移估计</h2><span class="tag">六轴惯性积分</span></div><div class="trajectory-scene" tabindex="0" aria-label="三维轨迹。鼠标拖动旋转，滚轮缩放；使用下方按钮恢复视角"></div><div class="trajectory-view-tools"><button data-action="fit">适配轨迹</button><button data-action="perspective">透视</button><button data-action="top">俯视 XY</button><span class="muted small">拖动旋转 · 滚轮缩放 · 彩色标记为当前姿态</span></div></section><section class="panel trajectory-readouts"><h2>追踪读数</h2><div class="trajectory-vector"><span>位移 / m</span><div data-value="position">—</div></div><div class="trajectory-vector"><span>速度 / m/s</span><div data-value="velocity">—</div></div><div class="trajectory-vector"><span>去重力加速度 / m/s²</span><div data-value="acceleration">—</div></div><dl><div><dt>轨迹长度估计</dt><dd data-value="distance">0.000 m</dd></div><div><dt>追踪跨度</dt><dd data-value="elapsed">0.00 s</dd></div><div><dt>静止 / 零速状态</dt><dd data-value="stationary">未启用</dd></div><div><dt>静止参考模长</dt><dd data-value="gravity">—</dd></div><div><dt>积分时间</dt><dd data-value="timing">主机接收时间</dd></div><div><dt>轨迹点 / 数据缺口</dt><dd data-value="counts">0 / 0</dd></div></dl><p class="trajectory-drift" data-value="drift">六轴 IMU 没有绝对位置来源。微小零偏和姿态误差经两次积分会产生明显漂移。</p></section></div><section class="panel trajectory-export"><h2>导出本次轨迹</h2><div class="actions"><button data-export="csv" disabled>CSV</button><button data-export="xlsx" disabled>Excel .xlsx</button><button data-export="mat" disabled>MATLAB .mat</button></div><p class="muted small">包含原始积分时间、相对时间、分段 ID、位移、速度、去重力加速度和数据源；单位为 m、s。导出的是估算轨迹，不是绝对位置真值。</p><p class="error" data-value="export-error" role="alert"></p></section>`;
    this.find=s=>mount.querySelector(s);const action=name=>this.find(`[data-action="${name}"]`);
    try{this.view=createView(this.find('.trajectory-scene'));}catch(error){this.find('.trajectory-scene').textContent='三维渲染不可用：'+error.message+'；估计与导出仍可使用。';}
    for(const name of ['reference','start','pause','reset'])action(name).onclick=()=>this.control(name,{},action(name));
    action('fit').onclick=()=>this.view?.fit();action('perspective').onclick=()=>this.view?.view('perspective');action('top').onclick=()=>this.view?.view('top');
    this.find('[data-option="zupt"]').onchange=e=>this.control('options',{zupt:e.target.checked},e.target);
    this.find('[data-option="fit"]').onchange=e=>{if(e.target.checked)this.view?.fit();};
    mount.querySelectorAll('[data-export]').forEach(b=>b.onclick=()=>this.export(b.dataset.export,b));
    document.addEventListener('visibilitychange',()=>{if(this.remote){if(document.hidden){clearTimeout(this.syncTimer);this.syncTimer=null;}else this.scheduleSync();return;}if(document.hidden&&(this.estimator.active||this.estimator.calibrating)){this.estimator.gap('浏览器进入后台，已暂停；回到页面后手动恢复');this.render();}});
    this.render();
  }
  setExportHandler(fn){this.exportHandler=fn;this.render();}
  setRemoteHandlers(handlers){
    if(!handlers||typeof handlers.action!=='function'||typeof handlers.read!=='function'||typeof handlers.download!=='function')throw Error('轨迹服务接口不完整');
    this.remote=handlers;this.remoteVersion++;this.remoteState=null;this.remotePoints=[];this.view?.update([],true);this.render();this.scheduleSync();
  }
  acceptStatus(status){
    if(!status||!Number.isInteger(status.epoch)||!Number.isInteger(status.generation))return false;
    const g=status.generation,e=status.epoch;
    if(this.remoteState&&(g<this.remoteGeneration||(g===this.remoteGeneration&&e<this.remoteEpoch)))return false;
    if(g!==this.remoteGeneration||e!==this.remoteEpoch){this.remoteGeneration=g;this.remoteEpoch=e;this.remoteVersion++;this.remotePoints=[];this.view?.update([],true);}
    // A status event can overtake an earlier point fetch in the same epoch.
    if(!this.remoteState||g!==this.remoteState.generation||e!==this.remoteState.epoch||status.points>=this.remoteState.points)this.remoteState=status;
    this.render();return true;
  }
  scheduleSync(){
    if(!this.remote||!this.visible||document.hidden||this.syncBusy||this.syncTimer!==null)return;
    const delay=Math.max(0,this.syncDelay-(performance.now()-this.lastSync));
    this.syncTimer=setTimeout(()=>{this.syncTimer=null;void this.syncRemote();},delay);
  }
  async syncRemote(){
    if(!this.remote||this.syncBusy||!this.visible||document.hidden)return;
    this.syncBusy=true;this.lastSync=performance.now();const version=this.remoteVersion,after=this.remotePoints.length,epoch=this.remoteEpoch;
    try{
      const data=await this.remote.read({after,epoch});
      if(version!==this.remoteVersion)return;
      if(!data||!Number.isInteger(data.epoch)||!Number.isInteger(data.generation)||!Number.isInteger(data.offset)||!Number.isInteger(data.total)||data.total<0||data.total>12000||!Array.isArray(data.points)||data.points.length>12000||data.next!==data.total||data.offset<0||data.offset+data.points.length!==data.next||data.status?.epoch!==data.epoch||data.status?.generation!==data.generation)throw Error('轨迹服务返回的数据结构无效');
      if(data.points.some(p=>!p||!Number.isFinite(p.time)||!Number.isInteger(p.segment)||['position','velocity','acceleration'].some(k=>!Array.isArray(p[k])||p[k].length!==3||!p[k].every(Number.isFinite))||!Array.isArray(p.quaternion)||p.quaternion.length!==4||!p.quaternion.every(Number.isFinite)))throw Error('轨迹点包含无效坐标');
      if(!this.acceptStatus(data.status))return;
      if(data.offset===0)this.remotePoints=data.points;
      else if(data.offset===this.remotePoints.length)this.remotePoints.push(...data.points);
      else {this.remotePoints=[];this.view?.update([],true);throw Error('轨迹缓存已重置，正在重新同步');}
      this.syncError=null;this.syncDelay=100;
      if(this.visible)this.view?.update(this.remotePoints,this.find('[data-option="fit"]').checked);
      this.render();
    }catch(error){if(version===this.remoteVersion){this.syncError='轨迹同步失败：'+error.message+'；服务中的追踪任务继续运行';this.syncDelay=Math.min(3000,Math.max(250,this.syncDelay*2));this.render();}}
    finally{this.syncBusy=false;this.scheduleSync();}
  }
  async control(name,params={},button=null){
    if(this.remote){
      if(this.actionBusy)return;this.remoteVersion++;this.actionBusy=true;this.render();
      try{const op=await this.remote.action(name,params,button);this.remoteVersion++;if(op?.result?.trajectory)this.acceptStatus(op.result.trajectory);this.syncError=null;}
      catch(error){this.onMessage(error.message,true);}
      finally{this.actionBusy=false;this.render();this.scheduleSync();}return;
    }
    if(name==='reference'){this.estimator.beginReference();this.view?.update([],true);this.lastDataAt=performance.now();}
    else if(name==='start'){this.estimator.start();this.lastDataAt=performance.now();}
    else if(name==='pause')this.estimator.pause();
    else if(name==='reset'){this.estimator.reset();this.view?.update([],true);}
    else if(name==='options'){this.estimator.zupt=params.zupt;this.estimator.stillSince=null;this.estimator.stationary=false;}
    this.render();
  }
  update(state) {
    this.lastState=state;
    if(this.remote){this.acceptStatus(state.trajectory);this.scheduleSync();return;}
    const changed=this.estimator.setSource(state.source,state.generation);
    const rotation=state.device?.configuration?.installation_rotation??null;
    if(!changed&&this.installation!==null&&rotation!==null&&rotation!==this.installation){this.estimator.reset();this.estimator.message='安装方向已变化；请重新建立静止参考';this.view?.update([],true);}
    this.installation=rotation;
    if(changed)this.view?.update([],true);
    if(this.estimator.active||this.estimator.calibrating){
      if(state.source==='none'||state.connection==='error'||state.connection==='disconnected')this.estimator.gap('数据源已断开，已暂停');
      else if(state.source==='playback'&&!state.playback?.playing)this.estimator.pause('回放已暂停；恢复回放后手动继续追踪');
      else if(this.lastDataAt&&performance.now()-this.lastDataAt>1200)this.estimator.gap('超过 1.2 秒没有收到采样，已暂停');
    }this.render();
  }
  add(samples){if(this.remote){this.scheduleSync();return;}if(!samples?.length)return;if(samples.some(s=>s.channel==='acceleration'))this.lastDataAt=performance.now();this.estimator.consume(samples);if(this.visible)this.view?.update(this.estimator.points,this.find('[data-option="fit"]').checked);this.render();}
  markGap(){if(this.remote){this.scheduleSync();return;}if(this.estimator.active||this.estimator.calibrating){this.estimator.gap('样本缓存读取落后，已暂停；轨迹不会连接缺失数据');this.render();}}
  show(visible){this.visible=visible;if(visible){this.view?.update(this.remote?this.remotePoints:this.estimator.points,this.find('[data-option="fit"]').checked);this.render();this.scheduleSync();}else{clearTimeout(this.syncTimer);this.syncTimer=null;}}
  theme(){this.view?.theme();}
  render() {
    const s=this.remote?(this.remoteState||{...this.estimator.snapshot(),source:'none',message:'正在读取服务中的轨迹状态'}):this.estimator.snapshot(),zupt=this.remote?!!s.zupt:this.estimator.zupt,busy=this.actionBusy||(this.remote&&!this.remoteState),playbackPaused=this.remote&&s.source==='playback'&&!this.lastState?.playback?.playing,value=(name,text)=>{this.find(`[data-value="${name}"]`).textContent=text;},vector=(name,v)=>{const el=this.find(`[data-value="${name}"]`);el.replaceChildren(...v.map((x,i)=>{const item=document.createElement('span');item.className='axis-'+('xyz'[i]);item.textContent='XYZ'[i]+' '+n(x,4);return item;}));};
    value('source',sources[s.source]||s.source);value('status',this.syncError||(playbackPaused&&!s.active&&!s.calibrating?'先开始回放，再建立参考或继续追踪':s.message));vector('position',s.position);vector('velocity',s.velocity);vector('acceleration',s.acceleration);value('distance',n(s.distance)+' m');value('elapsed',n(s.elapsed,2)+' s');value('stationary',s.stationary?'静止假设成立 · 已置零速度':zupt?'尚未满足静止条件':'关闭自动零速');value('gravity',s.reference?n(Math.hypot(...s.reference),5)+' m/s²':'尚未建立');value('timing',s.timeBasis==='recorded_time'?'录制原始接收时间':'主机接收时间 · 非设备时钟');value('counts',`${s.points} / ${s.gaps}`);
    value('drift',s.elapsed>20?'追踪超过 20 秒，漂移风险增加。距离和位移都是估算值，请用外部位置参考核对。':'六轴 IMU 没有绝对位置来源。微小零偏和姿态误差经两次积分会产生明显漂移。');
    this.find('.trajectory-progress div').style.width=(s.referenceProgress*100)+'%';this.find('.trajectory-progress').hidden=!s.calibrating;
    this.find('[data-action="start"]').disabled=busy||playbackPaused||!s.reference||s.active||s.calibrating||s.source==='none';this.find('[data-action="start"]').textContent=s.active?'追踪中':s.points?'继续追踪':'开始追踪';this.find('[data-action="pause"]').disabled=busy||(!s.active&&!s.calibrating);this.find('[data-action="reference"]').disabled=busy||playbackPaused||s.active||s.calibrating||s.source==='none';
    this.find('[data-action="reset"]').disabled=busy;this.find('[data-option="zupt"]').disabled=busy;if(this.remote)this.find('[data-option="zupt"]').checked=zupt;
    this.mount.querySelectorAll('[data-export]').forEach(b=>b.disabled=!s.points||(b.dataset.export!=='csv'&&!this.exportHandler&&!this.remote));
  }
  async export(format,button) {
    this.find('[data-value="export-error"]').textContent='';button.disabled=true;
    try {
      if(this.remote){if(!this.remoteState?.points)throw Error('当前轨迹没有数据');await this.remote.download(format);return;}
      const data=this.estimator.exportPayload(format);if(!data.points.length)throw Error('当前轨迹没有数据');
      if(this.exportHandler)await this.exportHandler(data);
      else {
        const header=['elapsed_s','measurement_time_unix_s','host_time_unix_s','source','generation','segment','position_x_m','position_y_m','position_z_m','velocity_x_m_s','velocity_y_m_s','velocity_z_m_s','acceleration_x_m_s2','acceleration_y_m_s2','acceleration_z_m_s2','distance_m','stationary','orientation_kind','quaternion_w','quaternion_x','quaternion_y','quaternion_z','gravity_reference_x_m_s2','gravity_reference_y_m_s2','gravity_reference_z_m_s2','timing','estimate'];
        const lines=[header.join(','),...data.points.map(p=>[p.elapsed,p.time,p.host_time,data.source,data.generation,p.segment,...p.position,...p.velocity,...p.acceleration,p.distance,Number(p.stationary),p.orientation_kind,...p.quaternion,...data.gravity_reference,data.timing,1].join(','))];
        download(new Blob(['\ufeff'+lines.join('\r\n')+'\r\n'],{type:'text/csv;charset=utf-8'}),'dmimu-trajectory-estimate-'+Date.now()+'.csv');
      }
    }catch(error){this.find('[data-value="export-error"]').textContent=error.message;this.onMessage(error.message,true);}finally{this.render();}
  }
  inspect(){return {remote:!!this.remote,estimator:this.remote?this.remoteState:this.estimator.snapshot(),epoch:this.remoteEpoch,cachedPoints:this.remotePoints.length,view:this.view?.inspect()};}
}
