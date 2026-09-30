import * as THREE from './vendor/three.module.js';

export function createPose(container) {
  const renderer = new THREE.WebGLRenderer({antialias: true, alpha: true});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, .1, 100);
  scene.add(new THREE.AmbientLight(0xffffff, 2));
  const light = new THREE.DirectionalLight(0xffffff, 3); light.position.set(3, 6, 4); scene.add(light);
  const sensor = new THREE.Group();
  const material = new THREE.MeshStandardMaterial({color: 0x26473d, roughness: .5, metalness: .2});
  const board = new THREE.Mesh(new THREE.BoxGeometry(2.1, 1.4, .11), material); sensor.add(board);
  const chip = new THREE.Mesh(new THREE.BoxGeometry(.65, .6, .19), new THREE.MeshStandardMaterial({color: 0x242629, roughness: .6})); chip.position.z = .13; sensor.add(chip);
  const connector = new THREE.Mesh(new THREE.BoxGeometry(.45, .33, .23), new THREE.MeshStandardMaterial({color: 0xc2c6c9, metalness: .9, roughness: .25})); connector.position.set(0, -.65, .13); sensor.add(connector);
  for (const x of [-.87, .87]) for (const y of [-.49, .49]) {
    const mount = new THREE.Mesh(new THREE.TorusGeometry(.08, .018, 8, 20), new THREE.MeshStandardMaterial({color: 0xd8bd76, metalness: .7})); mount.position.set(x, y, .067); sensor.add(mount);
  }
  const led = new THREE.Mesh(new THREE.BoxGeometry(.06, .07, .04), new THREE.MeshBasicMaterial({color: 0x61d2a0})); led.position.set(.64, -.4, .09); sensor.add(led);
  const deviceAxes = new THREE.AxesHelper(1.6); sensor.add(deviceAxes); scene.add(sensor);
  const floor = new THREE.GridHelper(7, 14, 0xd6d5d1, 0xeae9e5); floor.position.y = -1.2; scene.add(floor);
  const world = new THREE.Group(); world.position.set(-2.2, -1.18, .9); scene.add(world);
  for (const [axis, color] of [[new THREE.Vector3(1,0,0), 0xd55c51], [new THREE.Vector3(0,0,-1), 0x369a82], [new THREE.Vector3(0,1,0), 0x4b8cca]]) world.add(new THREE.ArrowHelper(axis, new THREE.Vector3(), .55, color, .12, .065));
  const basis = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1,0,0), -Math.PI/2);
  const latest = new THREE.Quaternion(); const reference = new THREE.Quaternion();
  const target = new THREE.Quaternion(); sensor.quaternion.copy(basis);
  let yaw = .65, elevation = .65, distance = 6.8, drag = null, stopped = false;
  function setCamera() {camera.position.set(distance*Math.cos(elevation)*Math.sin(yaw), distance*Math.sin(elevation), distance*Math.cos(elevation)*Math.cos(yaw)); camera.lookAt(0,0,0);}
  setCamera();
  container.addEventListener('pointerdown', e => {drag = [e.clientX, e.clientY]; container.setPointerCapture(e.pointerId);});
  container.addEventListener('pointermove', e => {if (!drag) return; yaw -= (e.clientX-drag[0])*.008; elevation = Math.max(-.15, Math.min(1.45, elevation+(e.clientY-drag[1])*.008)); drag = [e.clientX,e.clientY]; setCamera();});
  container.addEventListener('pointerup', () => {drag=null;});
  container.addEventListener('pointercancel', () => {drag=null;});
  container.addEventListener('wheel', e => {e.preventDefault(); distance = Math.max(3.3, Math.min(12, distance + e.deltaY*.006)); setCamera();}, {passive:false});
  new ResizeObserver(() => {const w=container.clientWidth, h=container.clientHeight; if (w && h) {renderer.setSize(w,h,false); camera.aspect=w/h; camera.updateProjectionMatrix();}}).observe(container);
  function theme() {const dark=document.documentElement.dataset.theme==='dark'; scene.background=new THREE.Color(dark?0x10151e:0xf4f3ef); floor.material.color.set(dark?0x344253:0xdcdcd7);}
  theme();
  renderer.domElement.addEventListener('webglcontextlost', e => {e.preventDefault(); stopped=true; document.querySelector('#pose-error').textContent='三维渲染上下文丢失，刷新页面恢复；采集仍在继续'; document.querySelector('#pose-error').classList.remove('hidden');});
  function animate() {requestAnimationFrame(animate); if(stopped || !container.clientWidth) return; target.copy(basis).multiply(reference).multiply(latest); sensor.quaternion.slerp(target,.18); renderer.render(scene,camera);}
  animate();
  return {
    update(channels) {
      const quat=channels.quaternion, e=channels.euler;
      if(quat && !quat.stale){const v=quat.values; latest.set(v.x,v.y,v.z,v.w).normalize(); return '设备四元数';}
      if(e && !e.stale){const v=e.values; latest.setFromEuler(new THREE.Euler(v.roll*Math.PI/180,v.pitch*Math.PI/180,v.yaw*Math.PI/180,'ZYX')); return '欧拉角 / ZYX';}
      return '等待有效姿态数据';
    },
    zero(){reference.copy(latest).invert();}, restore(){reference.identity();},
    reset(){yaw=.65;elevation=.65;distance=6.8;setCamera();}, theme,
  };
}
