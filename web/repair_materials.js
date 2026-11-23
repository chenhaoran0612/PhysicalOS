// Issue #30: what a repair session found in the library — the `library` event of brainos/repair_materials.py:
// the device's usable materials given to the assistant, or, with none to install a driver from, that the
// user should upload one first. The shared views are wrapped, not edited (hardware.js wraps the session
// view too): the session view names the tag; the fix chat (investigation.js) shows the event as one of its
// own lines, a warning when there is no usable driver material and a read otherwise.
{
 const rmBaseBody=engineSessionBody,rmBaseChat=invChatLog;
 const rmTag=/<span class="tag">library<\/span>/g;
 engineSessionBody=s=>{const html=rmBaseBody(s);return s?.mode==='repair'?html.replace(rmTag,'<span class="tag">资料库</span>'):html;};
 invChatLog=s=>rmBaseChat({...s,events:(s?.events||[]).map(e=>e?.kind==='library'?{...e,kind:e.missing?'tool_error':'tool'}:e)});
}
