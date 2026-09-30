// Radix-2 real-input FFT with coherent-gain corrected, one-sided amplitude.
export function spectrum(values, sampleRate, windowName='hann') {
  const n=values.length;
  if(n<16||n>65536||(n&(n-1))||!Number.isFinite(sampleRate)||sampleRate<=0)throw Error('FFT点数或采样率无效');
  if(!['hann','hamming','rectangular'].includes(windowName))throw Error('未知窗函数');
  const real=new Float64Array(n),imag=new Float64Array(n);
  let mean=0;for(const value of values){if(!Number.isFinite(value))throw Error('FFT采样值无效');mean+=value/n;}
  let gain=0;
  for(let i=0;i<n;i++){
    const w=windowName==='hann'?.5-.5*Math.cos(2*Math.PI*i/n):windowName==='hamming'?.54-.46*Math.cos(2*Math.PI*i/n):1;
    real[i]=(values[i]-mean)*w;gain+=w;
  }
  for(let i=1,j=0;i<n;i++){
    let bit=n>>1;while(j&bit){j^=bit;bit>>=1;}j^=bit;
    if(i<j){const value=real[i];real[i]=real[j];real[j]=value;}
  }
  for(let length=2;length<=n;length<<=1){
    const half=length>>1,angle=-2*Math.PI/length,wrStep=Math.cos(angle),wiStep=Math.sin(angle);
    for(let start=0;start<n;start+=length){
      let wr=1,wi=0;
      for(let j=0;j<half;j++){
        const a=start+j,b=a+half,tr=wr*real[b]-wi*imag[b],ti=wr*imag[b]+wi*real[b];
        real[b]=real[a]-tr;imag[b]=imag[a]-ti;real[a]+=tr;imag[a]+=ti;
        const next=wr*wrStep-wi*wiStep;wi=wr*wiStep+wi*wrStep;wr=next;
      }
    }
  }
  const frequency=new Float64Array(n/2+1),amplitude=new Float64Array(n/2+1);let peak=1;
  for(let i=0;i<frequency.length;i++){frequency[i]=i*sampleRate/n;amplitude[i]=Math.hypot(real[i],imag[i])/gain*(i===0||i===n/2?1:2);if(i&&amplitude[i]>amplitude[peak])peak=i;}
  return {frequency,amplitude,peak:{frequency:frequency[peak],amplitude:amplitude[peak]},resolution:sampleRate/n,sampleRate,n,window:windowName,mean};
}
