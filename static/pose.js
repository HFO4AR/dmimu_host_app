import * as THREE from './vendor/three.module.js';

// STEP is prepared in device coordinates: +X toward Type-C, +Y along the
// marked long edge, +Z out of the lid. A single proper rotation changes
// device Z-up into Three.js Y-up: X -> X, Y -> -Z, Z -> Y.
const basis = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2);
const colors = [0xd55c51, 0x369a82, 0x4b8cca];
const axes = [new THREE.Vector3(1,0,0), new THREE.Vector3(0,1,0), new THREE.Vector3(0,0,1)];

export function readOrientation(channels, output) {
  const q = channels.quaternion;
  if (q && !q.stale && ['w','x','y','z'].every(k => Number.isFinite(q.values[k]))) {
    const v = q.values, norm = Math.hypot(v.w,v.x,v.y,v.z);
    if (norm > .1 && norm < 2) {output.set(v.x,v.y,v.z,v.w).normalize(); return '设备四元数';}
  }
  const e = channels.euler;
  if (e && !e.stale && ['roll','pitch','yaw'].every(k => Number.isFinite(e.values[k]))) {
    const v = e.values;
    output.setFromEuler(new THREE.Euler(v.roll*Math.PI/180,v.pitch*Math.PI/180,v.yaw*Math.PI/180,'ZYX'));
    return '欧拉角 / ZYX';
  }
  return null;
}

export function displayQuaternion(latest, reference, output = new THREE.Quaternion()) {
  return output.copy(basis).multiply(reference).multiply(latest);
}

function label(text, color) {
  const canvas = document.createElement('canvas'); canvas.width=128; canvas.height=64;
  const ctx=canvas.getContext('2d'); ctx.font='600 38px system-ui'; ctx.textAlign='center'; ctx.textBaseline='middle';
  ctx.fillStyle='#'+color.toString(16).padStart(6,'0'); ctx.fillText(text,64,32);
  const texture = new THREE.CanvasTexture(canvas); texture.colorSpace=THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({map:texture,depthTest:false,transparent:true}));
  sprite.scale.set(.5,.25,1); return sprite;
}

function coordinateAxes(lengths) {
  const group = new THREE.Group();
  axes.forEach((direction,i) => {
    group.add(new THREE.ArrowHelper(direction,new THREE.Vector3(),lengths[i],colors[i],.16,.09));
    const name=label('XYZ'[i],colors[i]); name.position.copy(direction).multiplyScalar(lengths[i]+.18);group.add(name);
  });
  return group;
}

// Original vector artwork, written from the physical product's labels. No
// photograph is used as a texture and no manufacturer texture is bundled.
function lidMarkings(surfaceZ) {
  const canvas=document.createElement('canvas');canvas.width=1040;canvas.height=1440;
  const ctx=canvas.getContext('2d');ctx.fillStyle='#e8e9e5';ctx.strokeStyle='#e8e9e5';ctx.lineWidth=8;
  // Canvas right = device -X, down = device +Y. Type-C is its left edge.
  function text(value,x,y,size=35,angle=0){ctx.save();ctx.translate(x,y);ctx.rotate(angle);ctx.font=`600 ${size}px Arial`;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(value,0,0);ctx.restore();}
  text('DM-IMU-L1-V1.0',105,720,44,-Math.PI/2);
  ['485-B','485-A','GND','VCC'].forEach((s,i)=>text(s,240+i*75,155,29,-Math.PI/2));
  ['CAN-L','CAN-H','GND','VCC'].forEach((s,i)=>text(s,680+i*75,155,29,-Math.PI/2));
  ['VCC','GND','485-A','485-B'].forEach((s,i)=>text(s,240+i*75,1275,29,-Math.PI/2));
  ['VCC','GND','CAN-H','CAN-L'].forEach((s,i)=>text(s,680+i*75,1275,29,-Math.PI/2));
  text('485 RES  ON',957,460,26,-Math.PI/2);text('OFF',957,590,26,-Math.PI/2);
  text('CAN RES  ON',957,980,26,-Math.PI/2);text('OFF',957,1110,26,-Math.PI/2);
  const ox=810,oy=760;
  function arrow(x,y){ctx.beginPath();ctx.moveTo(ox,oy);ctx.lineTo(x,y);ctx.stroke();const a=Math.atan2(y-oy,x-ox);ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x-28*Math.cos(a-.5),y-28*Math.sin(a-.5));ctx.lineTo(x-28*Math.cos(a+.5),y-28*Math.sin(a+.5));ctx.fill();}
  arrow(645,oy);arrow(ox,915);text('X',605,oy,35);text('Y',ox,955,35);text('Z',ox+45,oy-7,35);
  ctx.beginPath();ctx.arc(ox,oy,14,0,Math.PI*2);ctx.stroke();
  const texture=new THREE.CanvasTexture(canvas);texture.colorSpace=THREE.SRGBColorSpace;
  const mesh=new THREE.Mesh(new THREE.PlaneGeometry(2.6,3.6),new THREE.MeshBasicMaterial({map:texture,transparent:true,depthWrite:false,polygonOffset:true,polygonOffsetFactor:-2}));
  // Turn the artwork 180 degrees in its plane: canvas left is device +X,
  // canvas down is +Y. This changes UVs only, never CAD handedness.
  const uv=mesh.geometry.attributes.uv;for(let i=0;i<uv.count;i++){uv.setX(i,1-uv.getX(i));uv.setY(i,1-uv.getY(i));}
  mesh.position.z=surfaceZ+.002;return mesh;
}

export function createPose(container) {
  const renderer=new THREE.WebGLRenderer({antialias:true,alpha:true});renderer.setPixelRatio(Math.min(window.devicePixelRatio,2));
  container.prepend(renderer.domElement);
  const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(38,1,.1,100);
  scene.add(new THREE.AmbientLight(0xffffff,1.7));
  const light=new THREE.DirectionalLight(0xffffff,3.2);light.position.set(-3,6,5);scene.add(light);
  const rim=new THREE.DirectionalLight(0xffffff,1.7);rim.position.set(4,1,-3);scene.add(rim);
  const sensor=new THREE.Group();scene.add(sensor);sensor.quaternion.copy(basis);
  const axisGroup=coordinateAxes([2.0,2.45,1.5]);sensor.add(axisGroup);
  const floor=new THREE.GridHelper(10,20,0xc3c7c8,0xe2e4e3);floor.position.y=-2.25;scene.add(floor);
  const latest=new THREE.Quaternion(),reference=new THREE.Quaternion(),target=new THREE.Quaternion();
  let yaw=2.3,elevation=.8,distance=8.6,drag=null,stopped=false,lastTime=0,hasOrientation=false,modelReady=false,zeroed=false;
  const status=document.createElement('div');status.className='pose-model-status';status.textContent='正在载入官方 CAD 模型…';container.append(status);
  const frame=document.createElement('div');frame.className='pose-frame-status';frame.textContent='设备坐标 · +X → Type-C · +Z → 壳顶';container.append(frame);
  const controls=document.createElement('div');controls.className='pose-view-controls';
  [['透视','perspective'],['俯视','top'],['Type-C侧','usb']].forEach(([name,view])=>{const b=document.createElement('button');b.textContent=name;b.type='button';b.onclick=()=>setView(view);controls.append(b);});
  const toggle=document.createElement('button');toggle.type='button';toggle.textContent='隐藏坐标轴';toggle.setAttribute('aria-pressed','true');toggle.onclick=()=>{axisGroup.visible=!axisGroup.visible;toggle.textContent=axisGroup.visible?'隐藏坐标轴':'显示坐标轴';toggle.setAttribute('aria-pressed',String(axisGroup.visible));};controls.append(toggle);container.append(controls);
  const hudScene=new THREE.Scene(),hudCamera=new THREE.PerspectiveCamera(38,1,.1,20),hud=coordinateAxes([.75,.75,.75]);hud.quaternion.copy(basis);hudScene.add(hud);
  hudScene.traverse(o=>{if(o.isSprite)o.scale.set(.32,.16,1);});
  function setCamera(){camera.up.set(0,1,0);camera.position.set(distance*Math.cos(elevation)*Math.sin(yaw),distance*Math.sin(elevation),distance*Math.cos(elevation)*Math.cos(yaw));camera.lookAt(0,0,0);}
  function setView(view){
    distance=8.6;
    if(view==='perspective'){yaw=2.3;elevation=.8;setCamera();return;}
    // Snap to the physical lid/USB side in its current displayed orientation,
    // rather than assuming its Type-C always points along a world-space axis.
    const shown=displayQuaternion(latest,reference);
    const direction=(view==='top'?new THREE.Vector3(0,0,1):new THREE.Vector3(1,0,.15)).normalize().applyQuaternion(shown);
    camera.position.copy(direction).multiplyScalar(distance);
    camera.up.copy(view==='top'?new THREE.Vector3(0,-1,0):new THREE.Vector3(0,0,1)).applyQuaternion(shown);
    camera.lookAt(0,0,0);yaw=Math.atan2(direction.x,direction.z);elevation=Math.asin(direction.y);
  }
  setCamera();
  container.addEventListener('pointerdown',e=>{if(e.target.closest('button,a'))return;drag=[e.clientX,e.clientY];container.setPointerCapture(e.pointerId);});
  container.addEventListener('pointermove',e=>{if(!drag)return;yaw-=(e.clientX-drag[0])*.008;elevation=Math.max(-1.45,Math.min(1.569,elevation+(e.clientY-drag[1])*.008));drag=[e.clientX,e.clientY];setCamera();});
  container.addEventListener('pointerup',()=>{drag=null;});container.addEventListener('pointercancel',()=>{drag=null;});
  container.addEventListener('wheel',e=>{e.preventDefault();distance=Math.max(5,Math.min(17,distance+e.deltaY*.008));setCamera();},{passive:false});
  let width=0,height=0;
  new ResizeObserver(()=>{width=container.clientWidth;height=container.clientHeight;if(width&&height){renderer.setSize(width,height,false);camera.aspect=width/height;camera.updateProjectionMatrix();}}).observe(container);
  function theme(){const dark=document.documentElement.dataset.theme==='dark';scene.background=new THREE.Color(dark?0x111923:0xf2f3f0);floor.material.color.set(dark?0x334252:0xd3d7d5);}
  theme();
  async function loadModel(){
    try {
      const response=await fetch('/api/model');if(!response.ok)throw Error('模型服务不可用');const result=await response.json(),info=result.data;
      if(!info.ready)throw Error('官方模型未准备；在终端运行 python scripts/prepare_model.py --install，再刷新页面');
      const responseMesh=await fetch(info.mesh_url);if(!responseMesh.ok)throw Error('模型下载失败');const raw=await responseMesh.arrayBuffer();
      const size=new DataView(raw).getUint32(0,true);if(size>65536||size+4>raw.byteLength)throw Error('模型头无效');
      const meta=JSON.parse(new TextDecoder().decode(new Uint8Array(raw,4,size)));if(meta.format!=='dmimu.mesh.v1')throw Error('模型格式不兼容');
      const model=new THREE.Group();
      for(const g of meta.groups){if(!Number.isInteger(g.count)||g.count<0||g.count%3||!Number.isInteger(g.offset)||g.offset<0||4+size+g.offset+g.count*12>raw.byteLength)throw Error('模型网格无效');
        const data=raw.slice(4+size+g.offset,4+size+g.offset+g.count*12),geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(data),3));geometry.computeVertexNormals();
        const material=new THREE.MeshStandardMaterial({color:new THREE.Color().setRGB(...g.rgb,THREE.SRGBColorSpace),roughness:g.name==='usb'?.32:.55,metalness:g.name==='usb'?.85:.25});model.add(new THREE.Mesh(geometry,material));
      }
      model.add(lidMarkings(meta.surface_z));sensor.add(model);modelReady=true;status.textContent='官方 STEP · 26 × 36 × 9 mm';status.classList.add('ready');
      frame.title='模型来自官方 CAD；方向依据模块实物标记。视觉原点为壳体中心，未声明芯片敏感中心。';
    } catch(error){status.textContent=error.message;status.classList.add('error');}
  }
  loadModel();
  renderer.domElement.addEventListener('webglcontextlost',e=>{e.preventDefault();stopped=true;status.textContent='三维渲染上下文丢失，刷新页面恢复；采集仍在继续';status.classList.add('error');});
  function animate(now){requestAnimationFrame(animate);if(stopped||!width||!height)return;const dt=Math.min(.1,(now-lastTime)/1000||.016);lastTime=now;displayQuaternion(latest,reference,target);sensor.quaternion.slerp(target,1-Math.exp(-dt*18));
    renderer.setViewport(0,0,width,height);renderer.autoClear=true;renderer.render(scene,camera);
    renderer.autoClear=false;renderer.clearDepth();renderer.setViewport(12,14,72,72);hudCamera.position.copy(camera.position).normalize().multiplyScalar(3.8);hudCamera.lookAt(0,0,0);renderer.render(hudScene,hudCamera);
  }
  requestAnimationFrame(animate);
  return {
    update(channels,configuration=null){
      const kind=readOrientation(channels,latest);hasOrientation=!!kind;
      const rotation=configuration?.installation_rotation;
      const carrier=Number.isInteger(rotation)&&rotation!==0;
      // A non-default installation changes the firmware's reported frame.
      // Until the manufacturer rotation table is verified, show that limitation
      // instead of applying a guessed second rotation to the physical housing.
      frame.textContent=carrier?`载体坐标 · 安装方向 ${rotation} · 壳体映射待确认`:(zeroed?'显示已归零 · 世界参考已改变':'设备坐标 · +X → Type-C · +Z → 壳顶');
      frame.classList.toggle('carrier',carrier);frame.title=carrier?'当前输出为非默认安装坐标，壳体显示未应用未经确认的安装方向映射。':'视觉原点为壳体中心；红 X 朝 Type-C，蓝 Z 朝壳顶。';
      return kind||'姿态数据已过期 / 等待新数据';
    },
    zero(){if(!hasOrientation)return false;reference.copy(latest).invert();zeroed=true;return true;},
    restore(){reference.identity();zeroed=false;},reset(){setView('perspective');},theme,
    // Allows isolated mathematical/model checks without altering device state.
    inspect(){return {modelReady,hasOrientation,zeroed,quaternion:sensor.quaternion.toArray(),cadToDevice:[[0,0,-1],[-1,0,0],[0,1,0]]};},
  };
}
