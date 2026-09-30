import {spectrum} from './fft_math.js';
self.onmessage=event=>{
  const {id,axes,sampleRate,windowName}=event.data;
  try{
    const results=axes.map(values=>spectrum(values,sampleRate,windowName));
    self.postMessage({id,results},results.flatMap(result=>[result.frequency.buffer,result.amplitude.buffer]));
  }catch(error){self.postMessage({id,error:error.message});}
};
