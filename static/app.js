import {createPose} from './pose.js';
import {Charts} from './charts.js';

const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let csrf='',local=true,state=null,caps=null,pose=null,lastSeq=0,lastGeneration=-1,lastSource='none',samplesBusy=false,toastTimer,recordingsBusy=false;
const charts=new Charts($('#charts'));
const operations=[];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(v,d=3)=>Number.isFinite(v)?v.toFixed(d):'—';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

function toast(message,error=false){clearTimeout(toastTimer);$('#toast').textContent=message;$('#toast').className='toast'+(error?' error':'');toastTimer=setTimeout(()=>$('#toast').classList.add('hidden'),error?9000:4000);}
async function api(path,options={}){
  const response=await fetch(path,{...options,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
  const data=await response.json();
  if(!data.ok)throw new Error(data.error?.message||'请求失败');
  return data;
}
function renderOperations(){
  const el=$('#operation-list');el.classList.remove('empty');
  el.innerHTML=operations.slice(-20).reverse().map(op=>`<div class="operation-row"><strong>${esc(op.action)}</strong><span class="tag">${esc(({queued:'排队中',running:'执行中',succeeded:'已完成',failed:'失败',uncertain:'结果未知'})[op.state])}</span><p>${esc(op.error?.message||op.result?.message||op.result?.verification||op.id)}</p></div>`).join('');
}
async function act(action,params={},button=null){
  if(button)button.disabled=true;
  const key=crypto.randomUUID?.()||`ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  let op;
  try{
    const response=await api('/api/v1/actions',{method:'POST',headers:{'Idempotency-Key':key},body:JSON.stringify({action,params})});op=response.operation;operations.push(op);renderOperations();
    const deadline=Date.now()+30000;
    while(['queued','running'].includes(op.state)){if(Date.now()>deadline)throw new Error('等待超时；请用操作 ID 查询原操作，不要重复发送');await sleep(180);op=(await api('/api/v1/operations/'+op.id)).operation;const index=operations.findIndex(item=>item.id===op.id);operations[index]=op;renderOperations();}
    if(op.state==='failed')throw new Error(op.error.message);
    if(op.state==='uncertain'){toast(op.result?.message||'设备操作结果未知；不要重复发送',true);return op;}
    toast(({connect:'串口已打开，等待有效数据',disconnect:'已断开连接',demo:'已进入演示模式', 'record.start':'开始录制原始数据','record.stop':'录制已保存','record.export':'CSV 已生成','playback.open':'录制已打开'})[action]||'操作已完成');
    return op;
  }catch(error){if(op&&['queued','running'].includes(op.state)){op={...op,state:'uncertain',error:{message:error.message}};operations[operations.findIndex(item=>item.id===op.id)]=op;renderOperations();}toast(`操作 ${op?.id||key}：`+error.message,true);throw error;}
  finally{if(button)button.disabled=false;renderDeviceAvailability();}
}
function handle(fn){return async e=>{try{await fn(e);}catch(_){/* User-visible errors are reported by API callers. */}};}
function page(name){
  $$('.page').forEach(el=>el.classList.toggle('hidden',el.dataset.workspace!==name));
  $$('.nav').forEach(el=>{el.classList.toggle('active',el.dataset.page===name);if(el.dataset.page===name)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  charts.visible=name==='charts';if(charts.visible)requestAnimationFrame(()=>charts.create());
  if(name==='recordings')loadRecordings().catch(e=>toast(e.message,true));
  if(name==='diagnostics')loadLogs().catch(e=>toast(e.message,true));
  if(name==='device')loadCapabilities().catch(e=>toast(e.message,true));
}
$$('.nav').forEach(el=>el.addEventListener('click',()=>page(el.dataset.page)));

function theme(){
  const value=document.documentElement.dataset.theme;
  $('#theme-label').textContent=value==='dark'?'深色':'亮色';$('#theme-select').value=value;
  $('#accent-select').value=document.documentElement.dataset.accent;
  try{localStorage.setItem('dmimu.theme',value);localStorage.setItem('dmimu.accent',document.documentElement.dataset.accent);}catch(_){}
  pose?.theme();charts.theme();
}
$('#theme-toggle').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';theme();};
$('#theme-select').onchange=e=>{document.documentElement.dataset.theme=e.target.value;theme();};
$('#accent-select').onchange=e=>{document.documentElement.dataset.accent=e.target.value;theme();};

function renderChannel(key,keys,el,factor=1){
  const channel=state.channels[key];
  const labels=key==='euler'?['Roll / 横滚','Pitch / 俯仰','Yaw / 偏航']:keys.map(x=>x.toUpperCase());
  el.innerHTML=keys.map((k,i)=>`<div><span class="${key==='euler'?'':'axis-'+k}">${labels[i]}</span><strong>${channel?fmt(channel.values[k]*factor,key==='euler'?2:3):'—'}</strong></div>`).join('');
  const age=$('#'+key+'-freshness');if(age){age.classList.toggle('stale',!!channel?.stale);age.textContent=channel?(state.source==='playback'&&!state.playback?.playing?'回放已暂停':`${channel.stale?'数据过期 · ':''}${Math.round(channel.age_ms)} ms · ${fmt(channel.rate_hz,0)} Hz`):'尚未收到数据';}
}
function render(s){
  state=s;
  if(s.generation!==lastGeneration){charts.clear();lastGeneration=s.generation;lastSeq=0;}
  if(s.source!==lastSource){pose?.restore();lastSource=s.source;}
  $('#connection-state').textContent=({streaming:'数据接收中',connected:'已连接',disconnected:'未连接','no-data':'等待数据',error:'连接错误',demo:'演示模式',playback:'回放模式'})[s.connection]||s.connection;
  const source=({live:'真实 USB',demo:'演示数据',playback:'录制回放',none:'无数据源'})[s.source];
  $('#data-source').textContent=source;$('#source-badge').textContent=source;
  $('#source-badge').classList.toggle('demo',s.source==='demo');$('#connection-dot').className='dot'+(s.connection==='streaming'?' live':s.source==='demo'?' demo':'');
  $('#connection-message').textContent=s.error||s.recording.error||'';$('#connection-message').classList.toggle('hidden',!s.error&&!s.recording.error);
  $('#record-label').textContent=s.recording.id?'停止录制':'开始录制';$('#record-toggle').classList.toggle('recording',!!s.recording.id);$('#record-toggle').disabled=!s.recording.id&&!['live','demo'].includes(s.source);
  renderChannel('euler',['roll','pitch','yaw'],$('#euler-values'));
  renderChannel('acceleration',['x','y','z'],$('#acceleration-values'));
  renderChannel('angular_velocity',['x','y','z'],$('#angular_velocity-values'),$('#gyro-unit').value==='deg'?180/Math.PI:1);
  const q=s.channels.quaternion;$('#quaternion-values').textContent=q?['w','x','y','z'].map(k=>fmt(q.values[k],4)).join('  '):'—　—　—　—';
  $('#quaternion-freshness').textContent=q?(q.stale?'数据过期':`${Math.round(q.age_ms)} ms · ${fmt(q.rate_hz,0)} Hz`):'尚未收到数据';
  $('#quaternion-freshness').classList.toggle('stale',!!q?.stale);
  const temp=s.channels.temperature;$('#temperature-value').innerHTML=(temp?fmt(temp.values.current,1):'—')+' <small>°C</small>';
  $('#temperature-value').title=temp?.stale?'温度数据已过期':'';
  $('#rate-value').innerHTML=fmt(s.channels.euler?.rate_hz??s.channels.quaternion?.rate_hz,0)+' <small>Hz</small>';
  const orientation=pose?.update(s.channels);$('#orientation-kind').textContent=orientation||'三维渲染不可用';
  $('#pose-empty').classList.toggle('hidden',!!(s.channels.quaternion||s.channels.euler));
  $('#stat-frames').textContent=s.statistics.frames.toLocaleString();$('#stat-crc').textContent=s.statistics.crc_errors.toLocaleString();$('#stat-discarded').textContent=s.statistics.discarded_bytes.toLocaleString();
  const pb=s.playback;$('#playback-toggle').disabled=!pb;$('#playback-position').disabled=!pb;
  $('#playback-toggle').textContent=pb?.playing?'暂停':'播放';$('#playback-name').textContent=pb?'录制 '+pb.id.slice(0,8):'从下面选择一段录制';
  if(pb){$('#playback-position').max=pb.duration;if(document.activeElement!==$('#playback-position'))$('#playback-position').value=pb.position;$('#playback-time').textContent=`${fmt(pb.position,2)} / ${fmt(pb.duration,2)} s`;$('#playback-speed').value=pb.speed;}
  renderDeviceAvailability();
  $('#protocol-probe').disabled=s.source!=='live'||!!s.recording.id;
}
function renderDeviceAvailability(){
  const ready=!!caps?.device_control.supported&&state?.source==='live'&&!state?.recording.id&&!operations.some(op=>['queued','running'].includes(op.state));
  $('#device-fields').disabled=!ready;
  for(const id of ['read-device','calibrate-gyro','calibrate-six','device-zero'])$('#'+id).disabled=!ready;
}
async function loadCapabilities(){
  caps=(await api('/api/v1/capabilities')).data;
  $('#protocol-badge').textContent=caps.device_control.supported?'旧版 1.x 控制协议':'自动接收模式';
  $('#protocol-notice').textContent=caps.device_control.reason||'已选择旧版协议。必须确认设备为 1.x；校准结果缺少可核验回报，发送后将标为结果未知。';
  renderDeviceAvailability();
}
async function loadPorts(){
  const ports=(await api('/api/v1/ports')).data;const selected=$('#port').value||state?.port;
  $('#port').innerHTML='<option value="">'+(ports.length?'选择 USB 串口…':'等待 Type-C 接入…')+'</option>'+ports.map(p=>`<option value="${esc(p.device)}">${esc(p.device)} · ${esc(p.description||'USB 串口')}${p.recognized?' / DM IMU':''}</option>`).join('');
  if(ports.some(p=>p.device===selected))$('#port').value=selected;else if(ports.filter(p=>p.recognized).length===1)$('#port').value=ports.find(p=>p.recognized).device;
}
$('#refresh-ports').onclick=handle(async()=>{await loadPorts();toast('串口列表已刷新');});
$('#connect').onclick=handle(async e=>{if(!$('#port').value){toast('先选择串口',true);return;}await act('connect',{port:$('#port').value},e.currentTarget);});
$('#disconnect').onclick=handle(e=>act('disconnect',{},e.currentTarget));
$('#demo').onclick=handle(e=>act('demo',{},e.currentTarget));
$('#record-toggle').onclick=handle(async e=>{await act(state.recording.id?'record.stop':'record.start',{},e.currentTarget);await loadRecordings();});
$('#view-reset').onclick=()=>pose?.reset();
$('#display-zero').onclick=()=>{if(!state?.channels.euler&&!state?.channels.quaternion){toast('还没有姿态数据',true);return;}pose?.zero();toast('显示参考已归零，设备数据不变');};
$('#display-restore').onclick=()=>{pose?.restore();toast('已恢复设备姿态参考');};
$('#gyro-unit').onchange=()=>{if(state)render(state);};
$('#chart-pause').onclick=e=>{charts.paused=!charts.paused;e.currentTarget.textContent=charts.paused?'继续波形':'暂停波形';if(!charts.paused)charts.draw();};
$('#chart-window').onchange=e=>{charts.seconds=Number(e.target.value);charts.draw();};
$('#chart-reset').onclick=()=>{charts.paused=false;$('#chart-pause').textContent='暂停波形';charts.draw();};

async function loadRecordings(){
  if(recordingsBusy)return;recordingsBusy=true;
  try{
    const recordings=(await api('/api/v1/recordings')).data;
    $('#recording-list').innerHTML=recordings.length?recordings.map(r=>`<article class="panel recording-row"><div><h3>${esc(new Date(r.created_at*1000).toLocaleString())} <span class="tag">${esc(({live:'真实 USB',demo:'演示数据'})[r.source]||r.source)}${r.active?' · 录制中':''}</span></h3><small class="mono">${esc(r.id.slice(0,12))} · ${(r.size/1024).toFixed(1)} KiB</small></div><div class="actions"><button data-replay="${r.id}" ${r.active?'disabled':''}>打开回放</button><button data-csv="${r.id}" ${r.active?'disabled':''}>导出 CSV</button><button data-raw="${r.id}" ${r.active?'disabled':''}>原始数据 ↓</button></div></article>`).join(''):'<div class="empty"><h3>还没有录制</h3>连接模块或进入演示模式，点击顶部“开始录制”。</div>';
    $$('[data-replay]').forEach(b=>b.onclick=handle(()=>act('playback.open',{id:b.dataset.replay},b)));
    $$('[data-csv]').forEach(b=>b.onclick=handle(async()=>{const op=await act('record.export',{id:b.dataset.csv},b);if(op.state==='succeeded')download(b.dataset.csv,'csv');}));
    $$('[data-raw]').forEach(b=>b.onclick=()=>download(b.dataset.raw,'raw'));
  }finally{recordingsBusy=false;}
}
function download(id,kind){const link=document.createElement('a');link.href=`/api/v1/recordings/${id}/${kind}`;link.download='';link.click();}
$('#refresh-recordings').onclick=handle(loadRecordings);
$('#playback-toggle').onclick=handle(e=>act('playback.control',{playing:!state.playback.playing},e.currentTarget));
$('#playback-position').onchange=handle(e=>{charts.clear();return act('playback.control',{position:Number(e.target.value)});});
$('#playback-speed').onchange=handle(e=>act('playback.control',{speed:Number(e.target.value)}));
async function loadLogs(){const logs=(await api('/api/v1/logs')).data;$('#log-list').classList.toggle('empty',!logs.length);$('#log-list').innerHTML=logs.length?logs.slice().reverse().map(l=>`<div class="log-row ${esc(l.level)}"><time>${esc(new Date(l.time*1000).toLocaleTimeString())}</time><span>${esc(l.message)}</span></div>`).join(''):'服务尚无日志';}
$('#refresh-logs').onclick=handle(loadLogs);
$('#protocol-probe').onclick=handle(async e=>{if(!confirm('探测会暂时进入设置模式，最多查询三次并退出。\n会保存原始应答，不修改参数或保存配置。继续？'))return;const op=await act('protocol.probe',{acknowledged:true},e.currentTarget);if(op.result?.id){const a=document.createElement('a');a.href='/api/v1/protocol-probes/'+op.result.id;a.download='';a.click();}await loadLogs();});

$('#device-form').onsubmit=handle(async e=>{e.preventDefault();const f=e.currentTarget;const p={};for(const k of ['acceleration_enabled','gyro_enabled','euler_enabled','quaternion_enabled','heating_enabled'])p[k]=f.elements[k].checked;p.interval_ms=Number(f.elements.interval_ms.value);p.target_temperature=Number(f.elements.target_temperature.value);await act('device.configure',p,f.querySelector('button.primary'));});
$('#read-device').onclick=handle(async e=>{const op=await act('device.read-settings',{},e.currentTarget);const config=op.result?.configuration;if(config){for(const [key,value] of Object.entries(config)){const input=$('#device-form').elements[key];if(input){if(input.type==='checkbox')input.checked=!!value;else input.value=value;}}}});
function calibration(kind,button){if(!confirm('确认模块使用旧版 1.x 固件，并已放置稳固？\n指令发送后请观察设备指示灯；上位机无法确认校准完成。'))return;return act('device.calibrate',{kind,acknowledged:true},button);}
$('#calibrate-gyro').onclick=handle(e=>calibration('gyro',e.currentTarget));
$('#calibrate-six').onclick=handle(e=>calibration('six-face',e.currentTarget));
$('#device-zero').onclick=handle(e=>{if(confirm('确认修改真实设备的角度参考？'))return act('device.angle-zero',{acknowledged:true},e.currentTarget);});

async function saveSettings(p){
  try{const data=(await api('/api/v1/settings',{method:'POST',body:JSON.stringify(p)})).data;$('#restart-notice').classList.toggle('hidden',!data.restart_required);toast(data.restart_required?'已保存，重启工作台后生效':'设置已保存');await loadCapabilities();return data;}
  catch(error){toast(error.message,true);throw error;}
}
$('#host-form').onsubmit=handle(async e=>{e.preventDefault();if($('#protocol').value==='legacy-v1'&&!confirm('只有确认设备为 1.x 固件才能启用旧版指令。\n确认继续？'))return;await saveSettings({auto_connect:$('#auto-connect').checked,baudrate:Number($('#baudrate').value),protocol:$('#protocol').value,legacy_crc:$('#legacy-crc').checked});});
$('#lan-form').onsubmit=handle(async e=>{e.preventDefault();const p={lan_enabled:$('#lan-enabled').checked};if($('#lan-password').value)p.password=$('#lan-password').value;await saveSettings(p);$('#lan-password').value='';});
$('#save-agent').onclick=handle(()=>saveSettings({agent_enabled:$('#agent-enabled').checked}));
$('#regenerate-agent').onclick=handle(()=>{if(confirm('重新生成凭据后，之前的 Agent 凭据会失效。继续？'))return saveSettings({regenerate_agent_token:true});});
async function loadSettings(){const p=(await api('/api/v1/settings')).data;$('#auto-connect').checked=p.auto_connect;$('#baudrate').value=p.baudrate;$('#protocol').value=p.protocol;$('#legacy-crc').checked=p.legacy_crc;$('#lan-enabled').checked=p.lan_enabled;$('#agent-enabled').checked=p.agent_enabled;$('#restart-notice').classList.toggle('hidden',!p.restart_required);for(const id of ['host-fields','lan-fields','agent-fields'])$('#'+id).disabled=!local;}

async function start(){
  const auth=(await api('/api/session')).data;csrf=auth.csrf;local=auth.local;$('#app-version').textContent=`v${auth.version} / PORT ${location.port||'5050'}`;$('#local-badge').textContent=local?'本机访问':'局域网访问';
  if(!auth.authenticated){$('#login').classList.remove('hidden');return;}
  $('#login').classList.add('hidden');$('#shell').classList.remove('hidden');
  try{pose=createPose($('#pose'));}catch(error){$('#pose-error').textContent='三维渲染不可用：'+error.message;$('#pose-error').classList.remove('hidden');}
  theme();await Promise.all([loadPorts(),loadCapabilities(),loadSettings()]);
  render((await api('/api/v1/status')).data);
  const events=new EventSource('/api/v1/events');events.onmessage=e=>render(JSON.parse(e.data));events.onerror=()=>{if(state?.connection!=='disconnected')$('#connection-message').textContent='工作台服务连接中断，正在重新连接…';$('#connection-message').classList.remove('hidden');};
  setInterval(async()=>{if(samplesBusy)return;samplesBusy=true;try{const data=(await api('/api/v1/samples?after='+lastSeq)).data;if(data.generation!==state.generation)return;if(data.truncated)charts.clear();charts.add(data.samples);lastSeq=data.seq;}catch(_){}finally{samplesBusy=false;}},250);
  setInterval(()=>loadPorts().catch(()=>{}),3000);
}
$('#login-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/login',{method:'POST',body:JSON.stringify({password:$('#password').value})});location.reload();}catch(error){$('#login-error').textContent=error.message;}};
start().catch(error=>toast('启动页面失败：'+error.message,true));
