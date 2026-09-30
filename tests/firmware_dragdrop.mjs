/** Load firmware_dragdrop.html through a local static server, not the IMU app.
 * All API calls are stubbed in-page. No network/device action is possible.
 */
import {mountFirmwareTools} from '../static/firmware.js';
const container=document.querySelector('#firmware-tools'),errors=[],calls=[];
let failNext=false;
const live={source:'live',port:'/synthetic/imu',identity:'synthetic-only',device:{protocol:'v2',version:{app_text:'2.0.3.0'}},recording:{id:null},firmware_busy:false,device_busy:false};
const panel=mountFirmwareTools(container,{
  async api(path,options){
    calls.push({path,options});
    if(path!='/api/v1/firmware/upload')throw new Error('Destructive action forbidden in fixture');
    if(failNext){failNext=false;throw new Error('synthetic upload failed');}
    const data=await options.body.arrayBuffer();
    return {ok:true,data:{id:'a'.repeat(32),filename:options.body.name,current_version:'2.0.3.0',app_version:'2.0.4.0',boot_version:'0.0.0.0',release:'20261001',file_size:data.byteLength,page_count:1,sha256:'b'.repeat(64),upgrade_allowed:true}};
  },onError(error){errors.push(error.message);},
});
panel.update(live);
function file(name,size=50){return new File([new Uint8Array(size)],name,{type:'application/octet-stream'});}
function drop(files){const dataTransfer=new DataTransfer();files.forEach(f=>dataTransfer.items.add(f));container.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer}));}
function $(name){return container.querySelector(`[data-fw="${name}"]`);}
const results=[];
function check(value,label){if(!value)throw new Error(label);results.push(label);}
async function until(condition){const end=Date.now()+1000;while(!condition()){if(Date.now()>end)throw new Error('Fixture wait timed out');await new Promise(r=>setTimeout(r,10));}}
async function run(){
  check($('upgrade').disabled,'initial upgrade is disabled');
  drop([file('synthetic.BIN')]);await until(()=>$('metadata').textContent.includes('2.0.4.0'));
  check(calls.length===1&&calls[0].options.body.name==='synthetic.BIN','uppercase BIN drop uploads one original File');
  check(calls[0].options.headers['Content-Type']==='application/octet-stream','drop uses binary upload');
  check(!$('ack').checked&&$('upgrade').disabled,'drop never grants write confirmation or starts upgrade');
  check($('metadata').querySelector('dd').dataset.noI18n==='true','firmware file labels preserve noI18n');
  $('ack').checked=true;$('ack').dispatchEvent(new Event('change'));check(!$('upgrade').disabled,'explicit confirmation enables a valid future upgrade');
  drop([file('wrong.txt')]);
  check(calls.length===1&&!$('ack').checked&&$('upgrade').disabled,'invalid extension clears earlier confirmation without upload');
  drop([file('a.bin'),file('b.bin')]);check(calls.length===1&&errors.at(-1).includes('每次只能'),'multiple files are rejected without upload');
  drop([file('oversized.bin',4096*255+19)]);check(calls.length===1&&errors.at(-1).includes('255'),'oversized image is rejected before API access');
  drop([file('empty.bin',0)]);check(calls.length===1&&$('upload').disabled,'empty package is rejected before API access');
  panel.update({...live,firmware_busy:true,device_busy:true});drop([file('busy.bin')]);check(calls.length===1&&$('drop').disabled,'active device transaction rejects replacement files');
  panel.update(live);failNext=true;drop([file('failed.bin')]);await until(()=>errors.at(-1)==='synthetic upload failed');
  check($('upgrade').disabled&&!$('ack').checked,'upload failure cannot reuse an earlier image or confirmation');
  // Cover the ordinary file picker after drag/drop with the same validation.
  const dt=new DataTransfer();dt.items.add(file('picked.bin'));$('file').files=dt.files;$('file').dispatchEvent(new Event('change'));
  const before=calls.length;check(!$('upload').disabled,'file picker still selects a valid file');$('upload').click();await until(()=>calls.length>before&&$('metadata').textContent.includes('picked.bin'));
  check(calls.every(call=>call.path==='/api/v1/firmware/upload'),'no upgrade, reboot or parameter API was called');
  return {passed:results.length,checks:results,uploads:calls.length,destructive_calls:0};
}
window.firmwareTestPromise=run().then(result=>{window.firmwareTestResult=result;document.querySelector('#results').textContent=JSON.stringify(result,null,2);return result;}).catch(error=>{window.firmwareTestResult={error:error.message};document.querySelector('#results').textContent=error.stack;throw error;});
