import {createLocalizer} from './locale.js';
export const DISPLAY_RATES=[10,20,30,60];
export const ACCENTS=['blue','cyan','green','orange','purple','red'];
const aliases={coral:'orange',leaf:'green',ocean:'blue',violet:'purple'};
const read=(key,fallback)=>{try{return localStorage.getItem(key)||fallback;}catch(_){return fallback;}};
const write=(key,value)=>{try{localStorage.setItem(key,String(value));}catch(_){/* Private mode may disable storage. */}};
export function normalizePreferences(value={}) {
  const raw=value.accent||'orange',accent=aliases[raw]||raw;
  return {language:value.language==='en'?'en':'zh',refreshHz:DISPLAY_RATES.includes(Number(value.refreshHz))?Number(value.refreshHz):30,accent:ACCENTS.includes(accent)?accent:'orange'};
}
let preferences=normalizePreferences({language:read('dmimu.language','zh'),refreshHz:read('dmimu.waveformRefresh','30'),accent:read('dmimu.accent',document.documentElement.dataset.accent)});
export function getPreferences(){return {...preferences};}
export function createPreferences({onChange=()=>{}}={}) {
  if(!document.querySelector('link[data-dmimu-preferences]')){const css=document.createElement('link');css.rel='stylesheet';css.href='/static/preferences.css';css.dataset.dmimuPreferences='true';document.head.append(css);}
  const grid=document.querySelector('[data-workspace="settings"] .settings-grid');if(!grid)return null;
  const panel=document.createElement('section');panel.className='panel preferences-panel';panel.innerHTML='<h2>曲线刷新与语言</h2><label>界面语言<select id="language-select"><option value="zh">中文</option><option value="en">English</option></select></label><label>波形刷新率<select id="waveform-refresh-select">'+DISPLAY_RATES.map(rate=>`<option value="${rate}">${rate} Hz</option>`).join('')+'</select></label><p class="muted small">只控制曲线重绘；USB 采集和原始录制不受影响。</p><p class="muted small">显示偏好自动保存在当前浏览器。设备原始日志、文件名与未翻译的固件消息保留原文。</p>';grid.prepend(panel);
  const accent=document.querySelector('#accent-select');if(accent){const names=['蓝','青','绿','橙','紫','红'];accent.replaceChildren(...ACCENTS.map((value,i)=>new Option(names[i],value)));accent.value=preferences.accent;}
  document.documentElement.dataset.accent=preferences.accent;
  const language=panel.querySelector('#language-select'),rate=panel.querySelector('#waveform-refresh-select');language.value=preferences.language;rate.value=String(preferences.refreshHz);
  const localizer=createLocalizer(preferences.language);
  function emit(){write('dmimu.language',preferences.language);write('dmimu.waveformRefresh',preferences.refreshHz);write('dmimu.accent',preferences.accent);document.documentElement.dataset.accent=preferences.accent;onChange(getPreferences());document.dispatchEvent(new CustomEvent('dmimu:preferences',{detail:getPreferences()}));}
  language.addEventListener('change',()=>{preferences.language=language.value==='en'?'en':'zh';localizer.setLanguage(preferences.language);emit();});rate.addEventListener('change',()=>{preferences=normalizePreferences({...preferences,refreshHz:rate.value});emit();});
  accent?.addEventListener('change',()=>{preferences=normalizePreferences({...preferences,accent:accent.value});emit();});
  localizer.setLanguage(preferences.language);emit();return {get:getPreferences,localizer,destroy(){localizer.disconnect();panel.remove();}};
}
