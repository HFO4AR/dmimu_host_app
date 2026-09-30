/** Independent firmware panel. api returns the authenticated JSON envelope. */
export function mountFirmwareTools(container,{api,onOperation=()=>{},onError=()=>{}}){
  let state=null,image=null,operation=null,uploading=false,polling=false,submitting=false,selectedFile=null;
  container.innerHTML=`<h3>固件升级</h3><p class="hint">选择达妙官方 .bin 文件。仅允许升级到更高版本，升级期间保持供电和 Type-C 连接。</p><div class="field-row"><label>官方固件文件<input type="file" accept=".bin,application/octet-stream" data-fw="file"></label><button data-fw="upload">上传并检查</button></div><button type="button" data-fw="drop" style="width:100%;border:1px dashed var(--muted);padding:14px;background:transparent;color:var(--muted)">拖放一个 .bin 文件到这里，松开后上传并检查</button><p class="hint" data-fw="selection" data-no-i18n="true">尚未选择文件</p><dl class="device-fields" data-fw="metadata"></dl><p class="hint" data-fw="guard">先读取设备版本，再上传固件。</p><label class="field-row"><input type="checkbox" data-fw="ack">我已确认设备与版本；允许写入固件并在完成后重启</label><div class="field-row"><button data-fw="upgrade" disabled>升级固件</button><button data-fw="cancel" disabled>停止继续发送</button></div><progress data-fw="progress" max="1" value="0" style="width:100%"></progress><p role="status" data-fw="status">尚未开始升级</p><p class="hint">停止发送不能撤回已经写入的数据。完成分页后将重启一次，读取到文件目标版本才显示成功。</p>`;
  const $=name=>container.querySelector(`[data-fw="${name}"]`);
  const labels={ready:'准备完成',entering:'进入升级模式',announcing:'发送文件信息',erasing:'等待擦除确认',writing:'写入分页',waiting_completion:'等待设备完成信息',waiting_reconnect:'等待重启后的版本核实',succeeded:'目标版本已核实',failed:'未确认完成',cancelled:'已停止发送'};
  function isBusy(){return !!state?.device_busy||!!state?.firmware_busy||!!operation&&['queued','running'].includes(operation.state)||uploading||submitting;}
  function render(){
    const busy=isBusy();
    const current=state?.device?.version?.app_text;
    let blocked=!state||state.source!=='live'||!current?'先连接设备并读取版本。':!image?'先选择并上传固件。':image.current_version!==current?'设备版本已变化，请重新上传并检查。':image.upgrade_allowed!==true?(image.blocker?.message||'文件版本必须高于设备当前版本。'):state.recording?.id?'先停止录制。':state.device?.protocol!=='v2'?'升级需要确认 2.x 设备协议。':null;
    $('guard').textContent=blocked||`将 ${current} 升级到 ${image.app_version}，设备 ${state.port}。`;
    $('upgrade').disabled=busy||!!blocked||!$('ack').checked;
    $('upload').disabled=busy||!selectedFile;
    $('drop').disabled=busy;
    $('drop').setAttribute('aria-busy',String(uploading));
    $('file').disabled=busy;
    $('ack').disabled=busy;
    const fw=state?.firmware;
    const id=operation&&['queued','running'].includes(operation.state)?operation.id:fw?.operation_id;
    $('cancel').disabled=!(state?.firmware_busy||operation&&['queued','running'].includes(operation.state))||!id;
    const terminal=operation&&!['queued','running'].includes(operation.state);
    const p=terminal?operation.result?.firmware:(operation?.progress||fw);
    $('progress').value=p?.progress||0;
    if(p)$('status').textContent=`${labels[p.state]||p.state} · ${p.acked_pages}/${p.total_pages} 页${p.retries?' · 重试 '+p.retries+' 次':''}${p.error?' · '+p.error.message:''}`;
    else if(operation)$('status').textContent=operation.error?.message||operation.result?.message||({'queued':'升级正在排队','running':'读取当前设备状态'})[operation.state]||operation.state;
  }
  function showImage(data){
    image=data;$('metadata').replaceChildren();
    for(const [name,value] of [['文件',data.filename],['目标应用版本',data.app_version],['Boot 版本',data.boot_version],['发行标记',data.release],['文件字节数',data.file_size],['分页',data.page_count],['SHA256',data.sha256]]){
      const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=name;dd.textContent=String(value);dd.dataset.noI18n='true';dd.style.overflowWrap='anywhere';$('metadata').append(dt,dd);
    }
    $('ack').checked=false;render();
  }
  async function submit(action,params){
    const response=await api('/api/v1/actions',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID?.()||`fw-${Date.now()}-${Math.random()}`},body:JSON.stringify({action,params})});
    onOperation(response.operation);return response.operation;
  }
  async function follow(initial){
    if(polling)return;polling=true;operation=initial;
    try{
      try{try{sessionStorage.setItem('dmimu.firmware.operation',operation.id);}catch(_){}}catch(_){}
      while(['queued','running'].includes(operation.state)){
        render();await new Promise(resolve=>setTimeout(resolve,500));
        try{operation=(await api('/api/v1/operations/'+operation.id)).operation;onOperation(operation);}
        catch(error){if([401,403,404].includes(error.status))throw error;$('status').textContent='正在重新连接服务；保留操作 '+operation.id+'，不会重复提交';await new Promise(resolve=>setTimeout(resolve,1200));}
      }
      try{try{sessionStorage.removeItem('dmimu.firmware.operation');}catch(_){}}catch(_){}render();
      if(operation.state!=='succeeded')onError(new Error(operation.error?.message||operation.result?.message||'固件操作未确认完成'));
    }catch(error){onError(error);}finally{polling=false;render();}
  }
  function clearSelection(){
    selectedFile=null;image=null;$('file').value='';$('metadata').replaceChildren();$('ack').checked=false;$('selection').textContent='尚未选择文件';render();
  }
  function selectFile(file){
    image=null;selectedFile=null;$('metadata').replaceChildren();$('ack').checked=false;
    if(!file||!/\.bin$/i.test(file.name)){
      clearSelection();onError(new Error('请选择一个达妙 .bin 固件文件'));return false;
    }
    if(file.size<=18||file.size>4096*255+18){
      clearSelection();onError(new Error(file.size<=18?'固件文件为空或缺少元数据':'固件最多支持 255 个 4096 字节分页'));return false;
    }
    selectedFile=file;$('selection').textContent=file.name+' · '+file.size+' 字节';render();return true;
  }
  async function uploadSelected(){
    const file=selectedFile;if(!file||isBusy())return;
    uploading=true;render();
    try{showImage((await api('/api/v1/firmware/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Firmware-Name':file.name.replace(/[^\x20-\x7e]/g,'_')},body:file})).data);}
    catch(error){image=null;onError(error);}finally{uploading=false;render();}
  }
  $('file').addEventListener('change',()=>{if(!isBusy())selectFile($('file').files[0]);});
  $('ack').addEventListener('change',render);
  $('upload').addEventListener('click',uploadSelected);
  $('drop').addEventListener('click',()=>{if(!isBusy())$('file').click();});
  for(const type of ['dragenter','dragover'])container.addEventListener(type,event=>{
    if(!Array.from(event.dataTransfer?.types||[]).includes('Files'))return;
    event.preventDefault();event.stopPropagation();
    event.dataTransfer.dropEffect=isBusy()?'none':'copy';
    if(!isBusy())$('drop').style.borderColor='var(--accent)';
  });
  container.addEventListener('dragleave',event=>{
    if(!container.contains(event.relatedTarget))$('drop').style.borderColor='var(--muted)';
  });
  container.addEventListener('drop',async event=>{
    if(!Array.from(event.dataTransfer?.types||[]).includes('Files'))return;
    event.preventDefault();event.stopPropagation();$('drop').style.borderColor='var(--muted)';
    if(isBusy())return;
    const files=Array.from(event.dataTransfer.files||[]);
    if(files.length!==1){clearSelection();onError(new Error('每次只能拖放一个 .bin 固件文件'));return;}
    if(selectFile(files[0])){
      // Browser FileList assignment is optional; upload uses selectedFile so
      // drag/drop also works where DataTransfer construction is unavailable.
      try{$('file').files=event.dataTransfer.files;}catch(_){}
      await uploadSelected();
    }
  });
  $('upgrade').addEventListener('click',async()=>{
    if($('upgrade').disabled)return;
    submitting=true;render();
    try{const op=await submit('firmware.upgrade',{id:image.id,acknowledged:true,expected_version:state.device.version.app_text,expected_identity:state.identity});follow(op);}
    catch(error){onError(error);}
    finally{submitting=false,selectedFile=null;render();}
  });
  $('cancel').addEventListener('click',async()=>{
    const id=operation&&['queued','running'].includes(operation.state)?operation.id:state?.firmware?.operation_id;if(!id)return;
    try{await submit('firmware.cancel',{operation_id:id});$('status').textContent='已请求停止继续发送；已写入的数据不能撤回';}
    catch(error){onError(error);}
  });
  async function restore(){
    let id;try{id=sessionStorage.getItem('dmimu.firmware.operation');}catch(_){return;}
    if(!/^[a-f0-9]{32}$/.test(id||''))return;
    try{const op=(await api('/api/v1/operations/'+id)).operation;if(op.action==='firmware.upgrade')follow(op);}
    catch(error){onError(error);}
  }
  render();
  return {update(next){state=next;render();},restore};
}
