const specifications = [
  ['acceleration','加速度','m/s²',['x','y','z']],
  ['angular_velocity','角速度','rad/s',['x','y','z']],
  ['euler','姿态角','deg',['roll','pitch','yaw']],
  ['quaternion','四元数','无量纲',['w','x','y','z']],
];
const colors=['#d55c51','#369a82','#4b8cca','#ac78c1'];
const number=(v,d=4)=>Number.isFinite(v)?v.toFixed(d):'—';
function lowerBound(rows,time){let lo=0,hi=rows.length;while(lo<hi){const m=(lo+hi)>>1;if(rows[m][0]<time)lo=m+1;else hi=m;}return lo;}
function upperBound(rows,time){let lo=0,hi=rows.length;while(lo<hi){const m=(lo+hi)>>1;if(rows[m][0]<=time)lo=m+1;else hi=m;}return lo;}
function nearest(rows,time){if(!rows.length||time<rows[0][0]||time>rows.at(-1)[0])return null;const i=lowerBound(rows,time);if(!i)return rows[0];if(i===rows.length)return rows.at(-1);return time-rows[i-1][0]<=rows[i][0]-time?rows[i-1]:rows[i];}
// Preserve each axis's extrema, including very short spikes. Measurements for
// cursors and statistics always use the original samples, never this rendering set.
function reduceRows(rows,width){const limit=Math.max(160,width*2);if(rows.length<=limit)return rows;const indices=new Set([0,rows.length-1]);const step=Math.ceil(rows.length/Math.max(20,width/5));for(let i=0;i<rows.length;i+=step){for(let axis=1;axis<rows[0].length;axis++){let lo=i,hi=i;for(let j=i;j<Math.min(i+step,rows.length);j++){if(rows[j][axis]<rows[lo][axis])lo=j;if(rows[j][axis]>rows[hi][axis])hi=j;}indices.add(lo);indices.add(hi);}}return [...indices].sort((a,b)=>a-b).map(i=>rows[i]);}

export class Charts {
  constructor(mount){
    this.mount=mount;this.buffers={};this.plots={};this.panels={};this.enabled={};this.yLocks={};this.paused=false;this.seconds=10;this.visible=false;this.origin=null;this.range=null;this.frozen=null;this.syncing=false;this.mode='zoom';this.cursors=[null,null];this.nextCursor=0;this.gyroFactor=1;this.scheduled=false;this.refreshHz=30;this.lastDrawTime=-Infinity;
    this.toolbar=document.createElement('div');this.toolbar.className='panel wave-toolbar';
    this.toolbar.innerHTML=`<div class="wave-toolbar-row"><span id="wave-state" class="wave-state" role="status">等待数据</span><label>窗口 <select id="chart-window"><option value="1">1 秒</option><option value="5">5 秒</option><option value="10" selected>10 秒</option><option value="30">30 秒</option><option value="60">60 秒</option></select></label><button id="chart-pause">暂停波形</button><button id="chart-reset">返回实时</button><button id="wave-export" disabled>导出 PNG</button><button id="wave-mat" disabled>MATLAB .mat</button><button id="wave-xlsx" disabled>Excel .xlsx</button><button id="wave-csv" disabled>CSV</button></div><div class="wave-toolbar-row"><label>操作 <select id="wave-mode"><option value="zoom">框选缩放</option><option value="pan">拖动平移</option><option value="cursor">双游标</option></select></label><div class="wave-navigation"><button id="wave-left" aria-label="向前平移半个窗口" title="向前平移半个窗口">←</button><button id="wave-right" aria-label="向后平移半个窗口" title="向后平移半个窗口">→</button><button id="wave-out" title="扩大时间范围">缩小</button><button id="wave-fit">全部缓存</button></div><label>角速度 <select id="wave-gyro-unit"><option value="rad">rad/s</option><option value="deg">deg/s</option></select></label><span id="wave-cache" class="muted"></span></div><div id="wave-gap" class="notice hidden" role="status">部分采样读取落后，缓存可能存在缺口；完整数据请使用原始录制。</div><div class="wave-range"><label>起点 / s <input id="wave-start" type="number" step="0.001"></label><label>终点 / s <input id="wave-end" type="number" step="0.001"></label><button id="wave-apply">应用范围</button><span id="wave-range-error" class="error" role="alert"></span></div><div class="wave-help">所有波形共享时间轴 · Ctrl + 滚轮缩放 · 双击返回实时 · 暂停后新数据继续缓存 · 数据导出为当前窗口所有通道原始点</div><div class="wave-cursor-controls hidden"><label>游标 A / s <input id="wave-a" type="number" step="0.001"></label><label>游标 B / s <input id="wave-b" type="number" step="0.001"></label><button id="wave-clear">清除游标</button><span id="wave-delta" class="mono">点击波形放置 A，再点击放置 B</span></div>`;
    mount.append(this.toolbar);
    for(const [key,title,unit,axes] of specifications){
      this.buffers[key]=[];this.enabled[key]=axes.map(()=>true);
      const panel=document.createElement('section');panel.className='panel chart-panel';panel.dataset.channel=key;
      panel.innerHTML=`<div class="panel-title"><h2>${title}</h2><div class="wave-channel-controls">${axes.map((a,i)=>`<label style="--channel-color:${colors[i]}"><input type="checkbox" checked data-axis="${i}" aria-label="${title} ${a.toUpperCase()} 通道"><span>${a.toUpperCase()}</span></label>`).join('')}<span class="unit">${unit}</span><button class="wave-y-lock" title="锁定当前纵轴范围，便于平移后比较幅度">固定 Y</button></div></div><div class="chart-mount" id="chart-${key}" tabindex="0" aria-label="${title}波形。左右箭头平移，空格暂停，Home 返回实时"><div class="wave-empty">等待${title}数据</div></div><div class="wave-hover mono muted">悬停波形查看原始样本</div><div class="wave-readout hidden"></div><div class="wave-summary muted">尚未收到数据</div>`;
      mount.append(panel);this.panels[key]=panel;
      panel.querySelectorAll('[data-axis]').forEach(input=>input.onchange=()=>{const i=Number(input.dataset.axis);this.enabled[key][i]=input.checked;this.plots[key]?.setSeries(i+1,{show:input.checked});this.updateReadouts();});
      panel.querySelector('.wave-y-lock').onclick=e=>{const plot=this.plots[key];if(this.yLocks[key]){delete this.yLocks[key];e.currentTarget.textContent='固定 Y';}else if(plot&&Number.isFinite(plot.scales.y.min)){this.yLocks[key]={min:plot.scales.y.min,max:plot.scales.y.max};e.currentTarget.textContent='自动 Y';}this.draw();};
      const el=panel.querySelector('.chart-mount');new ResizeObserver(()=>this.resize(key)).observe(el);
      el.addEventListener('keydown',e=>{if(e.target.closest('input,select,textarea,button,[contenteditable]'))return;if(['ArrowLeft','ArrowRight',' ','Home','Escape'].includes(e.key)){e.preventDefault();if(e.key==='ArrowLeft')this.pan(-.5);if(e.key==='ArrowRight')this.pan(.5);if(e.key===' ')this.togglePause();if(e.key==='Home')this.live();if(e.key==='Escape'){this.cursors=[null,null];this.updateReadouts();}}});
    }
    const find=id=>this.toolbar.querySelector('#'+id);this.find=find;
    find('chart-pause').onclick=()=>this.togglePause();find('chart-reset').onclick=()=>this.live();
    find('chart-window').onchange=e=>{this.seconds=Number(e.target.value);if(this.paused&&this.range)this.setRange(this.range[1]-this.seconds,this.range[1]);else this.draw();};
    find('wave-mode').onchange=e=>{this.mode=e.target.value;for(const p of Object.values(this.plots))p.cursor.drag.x=this.mode==='zoom';if(this.mode==='cursor'){this.freeze();if(this.range)this.cursors=[this.range[0]+(this.range[1]-this.range[0])/3,this.range[0]+2*(this.range[1]-this.range[0])/3];}this.draw();};
    find('wave-left').onclick=()=>this.pan(-.5);find('wave-right').onclick=()=>this.pan(.5);find('wave-out').onclick=()=>{if(this.range)this.setRange(this.range[0]-(this.range[1]-this.range[0])/2,this.range[1]+(this.range[1]-this.range[0])/2);};find('wave-fit').onclick=()=>{this.freeze();const b=this.bounds();if(b)this.setRange(...b);};
    find('wave-apply').onclick=()=>{const a=Number(find('wave-start').value),b=Number(find('wave-end').value);if(!find('wave-start').value||!find('wave-end').value||!Number.isFinite(a)||!Number.isFinite(b)||b-a<.001){find('wave-range-error').textContent='终点需要大于起点至少 0.001 秒';return;}find('wave-range-error').textContent='';this.setRange(a,b);};
    for(let i=0;i<2;i++)find(i?'wave-b':'wave-a').onchange=e=>{const value=Number(e.target.value);if(e.target.value&&Number.isFinite(value)){this.freeze();this.cursors[i]=value;this.updateReadouts();}};
    find('wave-clear').onclick=()=>{this.cursors=[null,null];this.updateReadouts();};
    find('wave-gyro-unit').onchange=e=>{const previous=this.gyroFactor;this.gyroFactor=e.target.value==='deg'?180/Math.PI:1;const locked=this.yLocks.angular_velocity;if(locked){locked.min*=this.gyroFactor/previous;locked.max*=this.gyroFactor/previous;}this.panels.angular_velocity.querySelector('.unit').textContent=this.gyroFactor===1?'rad/s':'deg/s';this.draw();};
    find('wave-export').onclick=()=>this.exportPNG();
    for(const format of ['mat','xlsx','csv'])find('wave-'+format).onclick=()=>this.exportData(format);
  }
  source(){return this.frozen||this.buffers;}
  bounds(source=this.source()){const rows=Object.values(source).filter(b=>b.length);return rows.length?[Math.min(...rows.map(b=>b[0][0])),Math.max(...rows.map(b=>b.at(-1)[0]))]:null;}
  freeze(){if(!this.bounds(this.buffers))return;if(!this.paused){this.frozen=Object.fromEntries(Object.entries(this.buffers).map(([k,b])=>[k,b.slice()]));this.paused=true;}this.updateToolbar();}
  togglePause(){if(this.paused)this.live();else{this.freeze();this.draw();}}
  live(){this.paused=false;this.frozen=null;this.cursors=[null,null];this.find('wave-range-error').textContent='';this.draw();}
  setRange(a,b){if(!Number.isFinite(a)||!Number.isFinite(b))return;this.freeze();const bounds=this.bounds();if(!bounds)return;let span=Math.max(.001,b-a);span=Math.min(span,Math.max(.001,bounds[1]-bounds[0]));a=Math.max(bounds[0],Math.min(a,bounds[1]-span));this.range=[a,a+span];this.draw();}
  pan(factor){if(this.range){const d=(this.range[1]-this.range[0])*factor;this.setRange(this.range[0]+d,this.range[1]+d);}}
  create(){
    if(!this.visible)return;
    const ink=getComputedStyle(document.documentElement).getPropertyValue('--muted').trim(),grid=getComputedStyle(document.documentElement).getPropertyValue('--line').trim();
    for(const [key,title,unit,axes] of specifications){const el=this.panels[key].querySelector('.chart-mount');if(!el.clientWidth||this.plots[key])continue;
      this.syncing=true;
      const plot=new window.uPlot({width:el.clientWidth,height:el.clientHeight,legend:{show:false},cursor:{sync:{key:'dmimu-wave-time'},drag:{x:this.mode==='zoom',y:false}},scales:{x:{time:false},y:{range:(u,min,max)=>{if(!Number.isFinite(min)||!Number.isFinite(max))return [0,1];const pad=(max-min||Math.max(Math.abs(min),1))*.08;return [min-pad,max+pad];}}},axes:[{stroke:ink,grid:{stroke:grid},values:(u,v)=>v.map(t=>number(t,Math.abs(u.scales.x.max-u.scales.x.min)<1?3:1)+' s')},{stroke:ink,grid:{stroke:grid},size:64,values:(u,v)=>v.map(x=>number(x,Math.abs(x)<.01?4:Math.abs(x)<10?2:1))}],series:[{},...axes.map((a,i)=>({label:a.toUpperCase(),stroke:colors[i],width:1.25,show:this.enabled[key][i],points:{show:false},spanGaps:false}))],hooks:{setCursor:[u=>this.hover(key,u)],setScale:[(u,k)=>{if(k==='x'&&!this.syncing&&this.range&&Number.isFinite(u.scales.x.min)&&u.scales.x.max>u.scales.x.min&&(Math.abs(u.scales.x.min-this.range[0])>1e-7||Math.abs(u.scales.x.max-this.range[1])>1e-7)){this.setRange(u.scales.x.min,u.scales.x.max);}}]}},[[],...axes.map(()=>[])],el);
      this.plots[key]=plot;this.syncing=false;
      const lines=[0,1].map(i=>{const line=document.createElement('div');line.className='wave-cursor-line hidden';line.dataset.cursor=i?'B':'A';plot.over.append(line);return line;});plot.waveLines=lines;
      let drag=null;
      lines.forEach((line,i)=>{line.title='拖动游标 '+(i?'B':'A');line.addEventListener('pointerdown',e=>{if(e.button!==0)return;e.preventDefault();e.stopPropagation();drag={cursor:i};plot.over.setPointerCapture(e.pointerId);});});
      plot.over.addEventListener('pointerdown',e=>{if(e.button!==0||!this.range||e.target.closest('.wave-cursor-line'))return;if(this.mode==='pan'){this.freeze();drag={x:e.clientX,range:this.range.slice()};plot.over.setPointerCapture(e.pointerId);e.preventDefault();}else if(this.mode==='cursor'){this.freeze();this.cursors[this.nextCursor]=plot.posToVal(e.clientX-plot.over.getBoundingClientRect().left,'x');this.nextCursor=1-this.nextCursor;this.updateReadouts();}});
      plot.over.addEventListener('pointermove',e=>{if(drag?.cursor!==undefined){this.cursors[drag.cursor]=plot.posToVal(Math.max(0,Math.min(plot.over.clientWidth,e.clientX-plot.over.getBoundingClientRect().left)),'x');this.updateReadouts();}else if(drag){const d=(drag.x-e.clientX)/plot.over.clientWidth*(drag.range[1]-drag.range[0]);this.setRange(drag.range[0]+d,drag.range[1]+d);}});
      plot.over.addEventListener('pointerup',()=>{drag=null;});plot.over.addEventListener('pointercancel',()=>{drag=null;});
      plot.over.addEventListener('wheel',e=>{if(!e.ctrlKey||!this.range)return;e.preventDefault();const t=plot.posToVal(e.clientX-plot.over.getBoundingClientRect().left,'x'),f=e.deltaY>0?1.3:1/1.3;this.setRange(t+(this.range[0]-t)*f,t+(this.range[1]-t)*f);},{passive:false});
      plot.over.addEventListener('dblclick',e=>{e.preventDefault();this.live();});
    }this.draw();
  }
  resize(key){const el=this.panels[key].querySelector('.chart-mount');if(el.clientWidth&&this.plots[key]){this.syncing=true;try{this.plots[key].setSize({width:el.clientWidth,height:el.clientHeight});}finally{this.syncing=false;}this.draw();}else this.create();}
  add(samples){
    for(const s of samples){const spec=specifications.find(x=>x[0]===s.channel);const time=s.measurement_time??s.time;if(!spec||!Number.isFinite(time))continue;if(this.origin===null)this.origin=time;const b=this.buffers[s.channel],row=[time-this.origin,...spec[3].map(k=>Number.isFinite(s.values[k])?s.values[k]:null)];if(b.length&&row[0]<b.at(-1)[0])continue;b.push(row);}
    const bounds=this.bounds(this.buffers);if(bounds)for(const b of Object.values(this.buffers)){let n=lowerBound(b,bounds[1]-90);n=Math.max(n,b.length-90000);if(n>0)b.splice(0,n);}
    this.scheduleSampleDraw();
  }
  setRefreshRate(hz){this.refreshHz=[10,20,30,60].includes(Number(hz))?Number(hz):30;}
  scheduleSampleDraw(){
    if(this.scheduled)return;this.scheduled=true;
    const delay=Math.max(0,1000/this.refreshHz-(performance.now()-this.lastDrawTime));
    setTimeout(()=>requestAnimationFrame(()=>{this.scheduled=false;if(!this.paused)this.draw();else this.updateToolbar();}),delay);
  }
  draw(){
    if(!this.visible)return;this.lastDrawTime=performance.now();const bounds=this.bounds();if(!this.paused&&bounds)this.range=[Math.min(Math.max(bounds[0],bounds[1]-this.seconds),bounds[1]-.001),bounds[1]];
    this.syncing=true;
    try{for(const [key,plot] of Object.entries(this.plots)){const b=this.source()[key],axes=specifications.find(s=>s[0]===key)[3],rows=this.range?b.slice(Math.max(0,lowerBound(b,this.range[0])-1),Math.min(b.length,lowerBound(b,this.range[1])+1)):[];const selected=reduceRows(rows,plot.width),deltas=rows.slice(1,65).map((r,i)=>r[0]-rows[i][0]).filter(d=>d>0).sort((a,b)=>a-b),gapThreshold=Math.max(.05,(deltas[Math.floor(deltas.length/2)]||1)*6),gaps=[];for(let i=1;i<rows.length;i++)if(rows[i][0]-rows[i-1][0]>gapThreshold)gaps.push((rows[i][0]+rows[i-1][0])/2);let gapIndex=0;const factor=key==='angular_velocity'?this.gyroFactor:1;const data=[[],...axes.map(()=>[])];for(const row of selected){while(gapIndex<gaps.length&&gaps[gapIndex]<row[0]){data[0].push(gaps[gapIndex++]);for(let a=1;a<data.length;a++)data[a].push(null);}data[0].push(row[0]);for(let a=1;a<row.length;a++)data[a].push(row[a]===null?null:row[a]*factor);}plot.setData(data,false);if(this.range)plot.setScale('x',{min:this.range[0],max:this.range[1]});plot.setScale('y',this.yLocks[key]||{min:null,max:null});this.panels[key].querySelector('.wave-empty').classList.toggle('hidden',rows.length>0);}}finally{this.syncing=false;}
    this.updateToolbar();this.updateReadouts();
  }
  updateToolbar(){const b=this.bounds(this.buffers),f=this.find;f('wave-state').textContent=!b?'等待数据':this.paused?'已冻结 · 采集继续':'实时跟随';f('wave-state').classList.toggle('frozen',this.paused);f('chart-pause').textContent=this.paused?'继续实时':'暂停波形';f('wave-cache').textContent=b?`缓存 ${number(b[1]-b[0],1)} s · 最多 90 s / 每通道 90,000 点`: '接入设备后显示波形';f('wave-export').disabled=!this.bounds();for(const format of ['mat','xlsx','csv'])f('wave-'+format).disabled=!this.bounds()||this.exporting;for(const id of ['chart-pause','wave-left','wave-right','wave-out','wave-fit','wave-apply'])f(id).disabled=!b;if(this.range){for(const [id,v] of [['wave-start',this.range[0]],['wave-end',this.range[1]]])if(document.activeElement!==f(id))f(id).value=number(v,3);}this.toolbar.querySelector('.wave-cursor-controls').classList.toggle('hidden',this.mode!=='cursor');}
  hover(key,plot){
    const el=this.panels[key].querySelector('.wave-hover');if(!this.range||plot.cursor.left<0){el.textContent='悬停波形查看原始样本';return;}
    const row=nearest(this.source()[key],plot.posToVal(plot.cursor.left,'x')),axes=specifications.find(s=>s[0]===key)[3],factor=key==='angular_velocity'?this.gyroFactor:1;
    el.textContent=row?`${number(row[0],6)} s · ${axes.map((axis,i)=>this.enabled[key][i]?axis.toUpperCase()+' '+number(row[i+1]===null?null:row[i+1]*factor):'').filter(Boolean).join(' · ')}`:'此位置没有原始样本';
  }
  updateReadouts(){
    const has=this.cursors.every(Number.isFinite);this.find('wave-delta').textContent=has?`Δt ${number(this.cursors[1]-this.cursors[0],6)} s · 读数取各通道最近原始样本`:'点击波形放置 A，再点击放置 B';
    for(let i=0;i<2;i++){const input=this.find(i?'wave-b':'wave-a');if(document.activeElement!==input)input.value=Number.isFinite(this.cursors[i])?number(this.cursors[i],6):'';}
    for(const [key,title,unit,axes] of specifications){const b=this.source()[key],factor=key==='angular_velocity'?this.gyroFactor:1,displayUnit=key==='angular_velocity'&&factor!==1?'deg/s':unit,panel=this.panels[key],readout=panel.querySelector('.wave-readout'),rows=this.range?b.slice(lowerBound(b,this.range[0]),upperBound(b,this.range[1])):[];
      const a=Number.isFinite(this.cursors[0])?nearest(b,this.cursors[0]):null,c=Number.isFinite(this.cursors[1])?nearest(b,this.cursors[1]):null;
      readout.classList.toggle('hidden',this.mode!=='cursor');readout.innerHTML=`<table><thead><tr><th>${displayUnit}</th><th>A ${a?number(a[0],4)+' s':'—'}</th><th>B ${c?number(c[0],4)+' s':'—'}</th><th>B − A</th></tr></thead><tbody>${axes.map((axis,i)=>this.enabled[key][i]?`<tr><th style="color:${colors[i]}">${axis.toUpperCase()}</th><td>${number(a?.[i+1]===null?null:a?.[i+1]*factor)}</td><td>${number(c?.[i+1]===null?null:c?.[i+1]*factor)}</td><td>${number(a&&c&&a[i+1]!==null&&c[i+1]!==null?(c[i+1]-a[i+1])*factor:null)}</td></tr>`:'').join('')}</tbody></table>`;
      const summary=axes.map((axis,i)=>{if(!this.enabled[key][i])return '';let min=Infinity,max=-Infinity,sq=0,count=0;for(const row of rows){const v=row[i+1];if(v===null)continue;min=Math.min(min,v);max=Math.max(max,v);sq+=v*v;count++;}return count?`${axis.toUpperCase()} 峰峰 ${number((max-min)*factor,3)} · RMS ${number(Math.sqrt(sq/count)*factor,3)}`:'';}).filter(Boolean).join('　');panel.querySelector('.wave-summary').textContent=rows.length?`${rows.length.toLocaleString()} 原始点 · ${summary||'所有通道已隐藏'}`:'当前范围无数据';
      const plot=this.plots[key];if(plot?.waveLines)plot.waveLines.forEach((line,i)=>{const time=this.cursors[i],show=this.mode==='cursor'&&Number.isFinite(time)&&this.range&&time>=this.range[0]&&time<=this.range[1];line.classList.toggle('hidden',!show);if(show)line.style.left=plot.valToPos(time,'x')+'px';});
    }
  }
  markGap(){this.find('wave-gap').classList.remove('hidden');}
  setExportHandler(handler){this.exportHandler=handler;}
  async exportData(format){
    if(!this.range||!this.exportHandler||this.exporting)return;
    const channels={};for(const [key,rows] of Object.entries(this.source())){const selected=rows.slice(lowerBound(rows,this.range[0]),upperBound(rows,this.range[1]));if(selected.length)channels[key]=selected.map(row=>[row[0]+this.origin,...row.slice(1)]);}
    if(!Object.keys(channels).length){this.find('wave-range-error').textContent='当前窗口没有可导出的数据';return;}
    this.exporting=true;this.find('wave-range-error').textContent='正在生成数据文件…';this.updateToolbar();
    try{await this.exportHandler({format,channels,source:this.sourceName||'unknown'});this.find('wave-range-error').textContent='';}catch(error){this.find('wave-range-error').textContent=error.message||'数据导出失败';}finally{this.exporting=false;this.updateToolbar();}
  }
  exportPNG(){
    const entries=Object.entries(this.plots).filter(([key])=>this.source()[key].length);if(!entries.length)return;
    const width=Math.max(900,...entries.map(([,p])=>p.width)),height=entries.reduce((s,[,p])=>s+p.height+115,80),canvas=document.createElement('canvas');canvas.width=width*2;canvas.height=height*2;const ctx=canvas.getContext('2d');ctx.scale(2,2);const style=getComputedStyle(document.documentElement);ctx.fillStyle=style.getPropertyValue('--surface').trim();ctx.fillRect(0,0,width,height);ctx.fillStyle=style.getPropertyValue('--ink').trim();ctx.font='bold 20px sans-serif';ctx.fillText('DM IMU · 波形分析',24,32);ctx.font='12px sans-serif';ctx.fillText(`时间 ${number(this.range?.[0],3)} – ${number(this.range?.[1],3)} s · ${this.paused?'冻结视图':'实时视图'} · ${new Date().toLocaleString()}`,24,56);
    let y=80;for(const [key,p] of entries){const spec=specifications.find(s=>s[0]===key),unit=this.panels[key].querySelector('.unit').textContent;ctx.font='bold 15px sans-serif';ctx.fillText(`${spec[1]} / ${unit}`,24,y+20);ctx.font='12px sans-serif';spec[3].forEach((axis,i)=>{if(this.enabled[key][i]){ctx.fillStyle=colors[i];ctx.fillText(axis.toUpperCase(),width-180+i*40,y+20);}});ctx.drawImage(p.ctx.canvas,0,y+32,p.width,p.height);if(this.mode==='cursor')this.cursors.forEach((t,i)=>{if(Number.isFinite(t)&&t>=this.range[0]&&t<=this.range[1]){const x=p.bbox.left/devicePixelRatio+p.valToPos(t,'x');ctx.strokeStyle=i?'#369a82':'#d55c51';ctx.beginPath();ctx.moveTo(x,y+32+p.bbox.top/devicePixelRatio);ctx.lineTo(x,y+32+(p.bbox.top+p.bbox.height)/devicePixelRatio);ctx.stroke();ctx.fillText(i?'B':'A',x+4,y+45);}});ctx.fillStyle=style.getPropertyValue('--ink').trim();ctx.font='11px sans-serif';ctx.fillText(this.panels[key].querySelector('.wave-summary').textContent.slice(0,160),24,y+p.height+54);if(this.mode==='cursor')ctx.fillText(this.panels[key].querySelector('.wave-readout').innerText.replace(/\s+/g,' ').slice(0,160),24,y+p.height+77);y+=p.height+115;}
    canvas.toBlob(blob=>{if(!blob)return;const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='dmimu-waveform-'+new Date().toISOString().replace(/[:.]/g,'-')+'.png';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  }
  clear(){for(const key in this.buffers)this.buffers[key]=[];this.origin=null;this.frozen=null;this.range=null;this.paused=false;this.yLocks={};this.find('wave-gap').classList.add('hidden');for(const panel of Object.values(this.panels))panel.querySelector('.wave-y-lock').textContent='固定 Y';this.cursors=[null,null];this.draw();}
  theme(){for(const p of Object.values(this.plots))p.destroy();this.plots={};this.create();}
}
