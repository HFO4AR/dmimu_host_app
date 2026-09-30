const specs={acceleration:['加速度','m/s²',['X','Y','Z']],angular_velocity:['角速度','rad/s',['X','Y','Z']],euler:['欧拉角','deg',['Roll','Pitch','Yaw']],quaternion:['四元数','无量纲',['W','X','Y','Z']]};
const colors=['#d55c51','#369a82','#4b8cca','#9b71c4'];

export class RealtimeFFT {
  constructor(container,charts){
    this.container=container;this.charts=charts;this.visible=false;this.busy=false;this.counter=0;this.lastKey='';this.lastTick=0;this.result=null;
    container.className='panel fft-panel';
    container.innerHTML=`<div class="panel-title"><h2>实时 FFT · 频谱</h2><span class="unit" data-fft="kind">原始采样</span></div><div class="fft-controls"><label>通道<select data-fft="channel">${Object.entries(specs).map(([key,s])=>`<option value="${key}">${s[0]}</option>`).join('')}</select></label><label>点数<select data-fft="size">${[256,512,1024,2048,4096,8192,16384].map(n=>`<option ${n===4096?'selected':''}>${n}</option>`).join('')}</select></label><label>采样率 / Hz<input data-fft="rate" type="number" min="1" max="100000" value="1000" step="any"></label><label>窗函数<select data-fft="window"><option value="hann">Hann</option><option value="hamming">Hamming</option><option value="rectangular">矩形窗</option></select></label><label>幅值轴<select data-fft="scale"><option value="linear">线性</option><option value="log">对数</option></select></label><button data-fft="rate-device">使用设备周期</button><button data-fft="csv" disabled>频谱 CSV</button><button data-fft="mat" disabled>MATLAB .mat</button><button data-fft="xlsx" disabled>Excel .xlsx</button></div><p class="muted small">对当前波形窗口末尾的原始连续样本计算；去除均值，校正窗函数增益。采样率须与模块输出一致，USB 批量接收时间不代表设备采样时钟。</p><div data-fft="plot" class="fft-plot"></div><div data-fft="peaks" class="fft-peaks"></div><p data-fft="status" class="muted small" aria-live="polite">等待足够的原始样本</p>`;
    this.find=name=>container.querySelector(`[data-fft="${name}"]`);
    for(const name of ['channel','size','rate','window','scale'])this.find(name).onchange=()=>{this.invalidate();if(name==='channel'||name==='scale')this.rebuild();this.update(true);};
    this.find('rate-device').onclick=()=>{if(this.interval){this.find('rate').value=1000/this.interval;this.invalidate();this.update(true);}else this.message('先在设备参数页读取模块上报周期，或手动填写已知采样率。');};
    this.find('csv').onclick=()=>this.exportFile('csv');
    for(const kind of ['mat','xlsx'])this.find(kind).onclick=()=>this.exportFile(kind);
    this.worker=new Worker('/static/fft_worker.js',{type:'module'});
    this.worker.onmessage=e=>{this.busy=false;if(e.data.id!==this.counter)return;if(e.data.error){this.message(e.data.error);return;}this.windowMeta=this.pendingMeta;this.result=e.data.results;this.paint();};
    this.worker.onerror=()=>{this.busy=false;this.message('FFT计算线程不可用，请刷新页面');};
    new ResizeObserver(()=>{const width=this.find('plot').clientWidth;if(width&&this.plot)this.plot.setSize({width,height:260});}).observe(this.find('plot'));
  }
  message(text){this.find('status').textContent=text;}
  setVisible(value){this.visible=value;if(value){this.create();this.update(true);}}
  render(state){if(state?.generation!==this.generation){this.generation=state?.generation;this.invalidate();this.message('数据源已变化，等待当前区间样本');}this.interval=state?.device?.configuration?.interval_ms;this.source=state?.source||'none';this.find('rate-device').disabled=!this.interval;}
  invalidate(){this.counter++;this.lastKey='';this.result=null;this.paint();}
  create(){
    const mount=this.find('plot');if(this.plot||!this.visible||!mount.clientWidth)return;
    const style=getComputedStyle(document.documentElement),ink=style.getPropertyValue('--muted').trim(),grid=style.getPropertyValue('--line').trim();
    this.plot=new window.uPlot({width:mount.clientWidth,height:260,scales:{x:{time:false},y:{distr:this.find('scale').value==='log'?3:1}},axes:[{stroke:ink,grid:{stroke:grid},values:(u,vals)=>vals.map(v=>`${v.toPrecision(3)} Hz`)},{stroke:ink,grid:{stroke:grid},size:75,filter:(u,vals)=>this.find('scale').value==='log'?vals.map(v=>v>0&&Math.abs(Math.log10(v)-Math.round(Math.log10(v)))<1e-7?v:null):vals,values:(u,vals)=>vals.map(v=>v===null?null:this.find('scale').value==='log'?(v>0?'1e'+Math.round(Math.log10(v)):null):(v===0?'0':Math.abs(v)<.001||Math.abs(v)>=10000?v.toExponential(2):v.toPrecision(3)))}],legend:{live:true},cursor:{drag:{x:true,y:false}},series:[{},...specs[this.find('channel').value][2].map((label,i)=>({label,stroke:colors[i],width:1.3,points:{show:false}}))]},[[],...specs[this.find('channel').value][2].map(()=>[])],mount);
  }
  rebuild(){if(this.plot)this.plot.destroy();this.plot=null;this.create();}
  theme(){this.rebuild();if(this.result)this.paint();}
  update(force=false){
    if(!this.visible||this.busy)return;const now=performance.now();if(!force&&now-this.lastTick<400)return;this.lastTick=now;
    const channel=this.find('channel').value,n=Number(this.find('size').value),fs=Number(this.find('rate').value),windowName=this.find('window').value;
    if(!Number.isFinite(fs)||fs<1||fs>100000){this.message('采样率必须为 1–100000 Hz');this.result=null;this.paint();return;}
    const range=this.charts.range;if(!range||!Number.isFinite(this.charts.origin)){this.message('等待波形窗口初始化');return;}const rows=this.charts.source()[channel]?.filter(row=>row[0]>=range[0]&&row[0]<=range[1])||[];
    if(rows.length<n){this.message(`当前区间 ${rows.length} / ${n} 点；扩大时间窗口或减少 FFT 点数。`);if(this.result){this.result=null;this.paint();}return;}
    const window=rows.slice(-n),last=window.at(-1),key=[channel,n,fs,windowName,window[0][0],...last].join(':');
    if(key===this.lastKey){if(this.result&&this.windowMeta.paused!==this.charts.paused){this.windowMeta.paused=this.charts.paused;this.paint();}return;}
    const maxGap=Math.max(.1,10/fs);if(window.some((row,i)=>i&&row[0]-window[i-1][0]>maxGap)){this.message('所选 FFT 窗口有采样缺口；移至连续区间后再计算。');this.result=null;this.paint();return;}
    const observed=last[0]-window[0][0],expected=(n-1)/fs;
    if(observed>0&&Math.abs(observed-expected)>Math.max(.06,expected*.2)){this.message(`采样率与该区间时长不一致：${n} 点 / ${observed.toFixed(3)} s，请核对采样率。`);this.result=null;this.paint();return;}
    this.lastKey=key;this.pendingMeta={start:window[0][0]+this.charts.origin,end:last[0]+this.charts.origin,source:this.source,paused:this.charts.paused,channel,unit:specs[channel][1]};
    this.busy=true;for(const kind of ['csv','mat','xlsx'])this.find(kind).disabled=true;const axes=specs[channel][2].map((_,i)=>Float64Array.from(window,row=>row[i+1]));this.worker.postMessage({id:++this.counter,axes,sampleRate:fs,windowName},axes.map(axis=>axis.buffer));
  }
  paint(){
    this.create();for(const kind of ['csv','mat','xlsx'])this.find(kind).disabled=!this.result||this.busy;
    if(!this.result){this.plot?.setData([[],...specs[this.find('channel').value][2].map(()=>[])]);this.find('peaks').replaceChildren();return;}
    const names=specs[this.windowMeta.channel][2],first=this.result[0],log=this.find('scale').value==='log',floor=Math.max(1e-15,...this.result.map(r=>r.peak.amplitude*1e-8));
    this.plot?.setData([Array.from(first.frequency).slice(1),...this.result.map(r=>Array.from(r.amplitude).slice(1).map(v=>log?Math.max(floor,v):v))]);
    this.find('kind').textContent=this.windowMeta.paused?'冻结窗口频谱':'实时窗口频谱';
    this.find('peaks').innerHTML=this.result.map((r,i)=>`<span style="color:${colors[i]}"><strong>${names[i]}</strong> 主峰 ${r.peak.frequency.toFixed(3)} Hz · ${r.peak.amplitude.toPrecision(4)} ${this.windowMeta.unit}</span>`).join('');
    this.message(`${first.n} 点 · Δf ${first.resolution.toPrecision(4)} Hz · 观察窗 ${(first.n/first.sampleRate).toFixed(3)} s · Fs ${first.sampleRate} Hz · 单边峰值幅值（非功率谱密度）`);
  }
  setExportHandler(handler){this.exportHandler=handler;}
  async exportFile(format){
    if(!this.result||this.busy)return;
    const first=this.result[0],payload={format,channel:this.windowMeta.channel,source:this.windowMeta.source,sample_rate_hz:first.sampleRate,window:first.window,samples:first.n,start_time_unix_s:this.windowMeta.start,end_time_unix_s:this.windowMeta.end,frequency_hz:Array.from(first.frequency),amplitude:this.result.map(r=>Array.from(r.amplitude))};
    if(!this.exportHandler){this.message('导出接口未就绪');return;}
    try{await this.exportHandler(payload);}catch(error){this.message(error.message);}
  }
}
