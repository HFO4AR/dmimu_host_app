// Device state and configuration are populated only from real replies.
import {installationRotations} from './rotations.js';
export function createDeviceControls({act, refreshCapabilities, toast, getState, getCaps, busy}) {
  const $=s=>document.querySelector(s),form=$('#device-form');
  for(const option of form.elements.installation_rotation.options)option.textContent=installationRotations[Number(option.value)];
  const flags=['acceleration_enabled','gyro_enabled','euler_enabled','quaternion_enabled','heating_enabled'];
  const extendedFlags=['can_active'];
  const numbers=['interval_ms','target_temperature'];
  const extended=['slave_id','master_id','can_baudrate','uart_baudrate','communication','installation_rotation','accel_range','gyro_range'];
  const modern=()=>getState()?.device?.protocol==='v2'&&getCaps()?.device_control?.profile!=='legacy-v1';
  const statusNames=['空闲','等待稳定','采样中','完成','失败'];
  function fill(config){if(!config)return;for(const [name,value]of Object.entries(config)){const input=form.elements[name];if(input){if(input.type==='checkbox')input.checked=!!value;else if(value!==null)input.value=value;}}}
  $('#read-device').onclick=async e=>{try{const op=await act(modern()||getCaps()?.device_control.profile==='legacy-v1'?'device.read-settings':'device.inspect',{},e.currentTarget);fill(op.result?.configuration);await refreshCapabilities();}catch(_){} };
  form.onsubmit=async e=>{e.preventDefault();const params={};for(const name of [...flags,...(modern()?extendedFlags:[])])params[name]=form.elements[name].checked;for(const name of [...numbers,...(modern()?extended:[])])if(!form.elements[name].disabled)params[name]=Number(form.elements[name].value);try{const op=await act('device.configure',params,form.querySelector('button.primary'));fill(op.result?.configuration);if(op.result?.recalibration_required)toast('安装方向或量程已改变，请重新执行静止零偏校准');}catch(_){} };
  for(const [id,kind]of [['calibrate-gyro','gyro'],['calibrate-six','six-face']])$('#'+id).onclick=async e=>{const message=modern()?(kind==='gyro'?'确认开始静止零偏校准？校准期间保持模块静止，结果由设备状态应答确认。':'确认开始六面校准？按 +X、−X、+Y、−Y、+Z、−Z 朝上放置，当前面与进度由设备反馈。'):'确认旧版 1.x 模块已放置稳固？指令发送后观察指示灯，上位机无法确认完成。';if(confirm(message)){try{await act('device.calibrate',{kind,acknowledged:true},e.currentTarget);}catch(_){}}};
  $('#abort-six').onclick=async e=>{try{await act('device.calibration-abort',{acknowledged:true},e.currentTarget);}catch(_){} };
  $('#calibration-refresh').onclick=async e=>{try{await act('device.calibration-status',{},e.currentTarget);}catch(_){} };
  $('#device-zero').onclick=async e=>{if(confirm('确认修改真实设备的'+(modern()?'航向':'角度')+'参考？')){try{await act(modern()?'device.yaw-zero':'device.angle-zero',{acknowledged:true},e.currentTarget);}catch(_){}}};
  $('#factory-reset').onclick=async e=>{if(confirm('确认恢复模块出厂参数？这会覆盖已保存的设备配置。')){try{await act('device.factory-reset',{acknowledged:true},e.currentTarget);}catch(_){}}};
  $('#read-build-info').onclick=async e=>{try{await act('device.build-info',{},e.currentTarget);}catch(_){}};
  $('#restart-device').onclick=async e=>{if(confirm('确认重启当前模块？连接将暂时中断，只会发送一次重启指令；重连后核对版本。')){try{await act('device.restart',{acknowledged:true},e.currentTarget,60000);}catch(_){}}};
  function result(el,status,six=false){
    if(!status){el.textContent='尚未读取设备状态';return;}
    const lines=[`${statusNames[status.state]||status.state} · ${status.samples} / ${status.target_samples} 个样本`];
    if(six){lines.push(`已完成 ${status.completed_faces} / 6 面 · 当前：${status.current_face===null?'无':['+X','−X','+Y','−Y','+Z','−Z'][status.current_face]}`);if(status.has_result)lines.push(`零偏 / m/s²：${status.bias_m_s2.map(v=>v.toFixed(4)).join(' / ')}\n比例：${status.scale.map(v=>v.toFixed(4)).join(' / ')}`);}
    else {lines.push(`${status.still?'设备判定静止':'设备判定不稳定'} · 稳定度 ${status.stability_deg_s.toFixed(2)} °/s · 阈值 ${status.threshold_deg_s.toFixed(2)} °/s`);if(status.has_result)lines.push(`零偏 / °/s：${status.bias_deg_s.map(v=>v.toFixed(4)).join(' / ')}\n噪声 / °/s：${status.noise_deg_s.map(v=>v.toFixed(4)).join(' / ')}`);}
    if(status.error)lines.push(`设备错误代码：${status.error}`);
    el.textContent=lines.join('\n');
  }
  return {render(state,caps){
    const live=state?.source==='live'&&!state?.recording.id, v2=modern();
    const calibrationActive=[state?.device?.static_calibration,state?.device?.six_face_calibration].some(s=>s&&[1,2].includes(s.state));
    const ready=live&&!!caps?.device_control.supported&&!busy()&&!calibrationActive;
    $('#device-fields').disabled=!ready;$('#read-device').disabled=!live||busy();
    for(const name of [...extendedFlags,...extended])form.elements[name].disabled=!v2||!ready||(name==='installation_rotation'&&!('installation_rotation'in(state?.device?.configuration||{})))||(['accel_range','gyro_range'].includes(name)&&!('accel_range'in(state?.device?.configuration||{})));
    $('#v2-device-fields').classList.toggle('hidden',!v2);$('#v2-calibration-tools').classList.toggle('hidden',!v2);
    for(const id of ['calibrate-gyro','calibrate-six','device-zero'])$('#'+id).disabled=!ready;
    $('#abort-six').disabled=!live||!v2||busy();$('#factory-reset').disabled=!ready||!v2;
    $('#calibration-refresh').disabled=!live||!v2||busy();
    $('#read-build-info').disabled=!live||!v2||busy()||!!state?.device_busy;
    $('#restart-device').disabled=!ready||!v2||!!state?.device_busy;
    const build=state?.device?.build_info;
    $('#device-build-info').textContent=build?build.text||'RAW BUILD HEX: '+build.raw_hex:'—';
    $('#protocol-badge').textContent=v2?'新版 2.x · 应答已验证':caps?.device_control.profile==='legacy-v1'?'旧版 1.x':'待读取设备版本';
    $('#protocol-notice').textContent=v2?'读取与配置结果由带 CRC 的设备应答确认；校准启动应答与校准完成状态分别显示。':caps?.device_control.reason||'旧版校准没有可核验的完成应答。';
    $('#device-version').textContent=state?.device?.version?`APP ${state.device.version.app_text} · BL ${state.device.version.boot_text}`:'尚未读取版本';
    $('#device-zero').textContent=v2?'设备航向归零':'设备角度归零';
    $('#calibration-protocol').textContent=v2?'V2 DEVICE STATUS':'LEGACY V1';
    result($('#static-calibration-status'),state?.device?.static_calibration);result($('#six-calibration-status'),state?.device?.six_face_calibration,true);
  }};
}
