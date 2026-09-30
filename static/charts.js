const specifications = [
  ['acceleration','加速度','m/s²',['x','y','z']],
  ['angular_velocity','角速度','rad/s',['x','y','z']],
  ['euler','姿态角','deg',['roll','pitch','yaw']],
];
const colors=['#d55c51','#369a82','#4b8cca'];

export class Charts {
  constructor(mount){
    this.mount=mount;this.buffers={};this.plots={};this.paused=false;this.seconds=10;this.visible=false;
    for(const [key,title,unit,axes] of specifications){
      this.buffers[key]=[];
      const panel=document.createElement('section');panel.className='panel chart-panel';
      panel.innerHTML=`<div class="panel-title"><h2>${title}</h2><span class="unit">${unit}</span></div><div class="chart-mount" id="chart-${key}"></div>`;
      mount.append(panel);
      new ResizeObserver(()=>this.resize(key)).observe(panel.lastElementChild);
    }
  }
  create(){
    if(!this.visible)return;
    const ink=getComputedStyle(document.documentElement).getPropertyValue('--muted').trim();
    const grid=getComputedStyle(document.documentElement).getPropertyValue('--line').trim();
    for(const [key,title,unit,axes] of specifications){
      const el=document.querySelector(`#chart-${key}`);if(!el.clientWidth||this.plots[key])continue;
      this.plots[key]=new window.uPlot({width:el.clientWidth,height:el.clientHeight-28,
        cursor:{drag:{x:true,y:false}},legend:{live:true},scales:{x:{time:false}},
        hooks:{setSelect:[u=>{if(u.select.width>0){this.paused=true;document.querySelector('#chart-pause').textContent='继续波形';}}]},
        axes:[{stroke:ink,grid:{stroke:grid},values:(u,vals)=>vals.map(v=>v.toFixed(1)+'s')},{stroke:ink,grid:{stroke:grid},size:65}],
        series:[{},...axes.map((a,i)=>({label:a.toUpperCase(),stroke:colors[i],width:1.3,points:{show:false}}))],
      },[[],[],[],[]],el);
    }
    this.draw();
  }
  resize(key){const el=document.querySelector(`#chart-${key}`);if(el.clientWidth&&this.plots[key])this.plots[key].setSize({width:el.clientWidth,height:el.clientHeight-28});else this.create();}
  add(samples){
    for(const s of samples){const spec=specifications.find(x=>x[0]===s.channel);if(!spec)continue;this.buffers[s.channel].push([s.time,...spec[3].map(k=>s.values[k])]);}
    for(const b of Object.values(this.buffers)){if(b.length){const cutoff=b.at(-1)[0]-35;let n=0;while(n<b.length&&b[n][0]<cutoff)n++;if(n)b.splice(0,n);if(b.length>40000)b.splice(0,b.length-40000);}}
    if(!this.paused)this.draw();
  }
  draw(){
    if(!this.visible)return;
    for(const [key,plot] of Object.entries(this.plots)){
      const b=this.buffers[key];if(!b.length){plot.setData([[],[],[],[]]);continue;}
      const latest=b.at(-1)[0],start=latest-this.seconds;
      const slice=b.filter(row=>row[0]>=start);
      // Keep extrema for each axis in every bucket so short spikes remain visible.
      let rows=slice;
      if(slice.length>1600){const indices=new Set([0,slice.length-1]);const step=Math.ceil(slice.length/240);for(let i=0;i<slice.length;i+=step){for(let a=1;a<4;a++){let lo=i,hi=i;for(let j=i;j<Math.min(i+step,slice.length);j++){if(slice[j][a]<slice[lo][a])lo=j;if(slice[j][a]>slice[hi][a])hi=j;}indices.add(lo);indices.add(hi);}}rows=[...indices].sort((a,b)=>a-b).map(i=>slice[i]);}
      const data=[[],[],[],[]];
      for(const row of rows){data[0].push(row[0]-latest);for(let i=1;i<4;i++)data[i].push(row[i]);}
      plot.setData(data);
    }
  }
  clear(){for(const key in this.buffers)this.buffers[key]=[];this.draw();}
  theme(){for(const p of Object.values(this.plots))p.destroy();this.plots={};this.create();}
}
