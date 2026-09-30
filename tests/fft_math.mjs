import assert from 'node:assert/strict';
import {spectrum} from '../static/fft_math.js';

const n=4096,fs=1024,frequency=37,amplitude=2.5;
const values=Float64Array.from({length:n},(_,i)=>7+amplitude*Math.sin(2*Math.PI*frequency*i/fs));
for(const window of ['hann','hamming','rectangular']){
  const result=spectrum(values,fs,window);
  assert.equal(result.peak.frequency,frequency);
  assert.ok(Math.abs(result.peak.amplitude-amplitude)<1e-10);
  assert.ok(Math.abs(result.mean-7)<1e-10);
  assert.equal(result.resolution,.25);
  assert.equal(result.frequency.at(-1),fs/2);
}
const constant=spectrum(new Float64Array(1024).fill(9.80665),1000);
assert.ok(Math.max(...constant.amplitude)<1e-12);
const nyquist=spectrum(Float64Array.from({length:1024},(_,i)=>i%2?2:-2),1000,'rectangular');
assert.equal(nyquist.peak.frequency,500);
assert.ok(Math.abs(nyquist.peak.amplitude-2)<1e-12);
assert.throws(()=>spectrum([1,2,3],1000));
assert.throws(()=>spectrum(new Float64Array(1024),0));
const invalid=new Float64Array(1024);invalid[5]=NaN;assert.throws(()=>spectrum(invalid,1000));
console.log('FFT frequency, amplitude, coherent window gain, DC and Nyquist checks passed');
