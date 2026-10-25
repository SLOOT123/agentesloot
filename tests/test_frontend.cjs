/* Prueba simulada de DOM y HTTP: ejecuta los módulos reales, sin navegador. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.events = new Map(); this.attributes = {};
    this.value = ''; this.hidden = false; this.disabled = false; this.style = {};
    this.scrollTop = 0; this.scrollHeight = 200; this.clientHeight = 200;
    this.classes = new Set(); this.dataset = {};
    this.classList = {
      toggle: (name, value) => {if (value) this.classes.add(name); else this.classes.delete(name);},
      contains: name => this.classes.has(name)
    };
  }
  addEventListener(type, callback) {if (!this.events.has(type)) this.events.set(type, []); this.events.get(type).push(callback);}
  dispatchEvent(event) {for (const callback of this.events.get(event.type) || []) callback(event);}
  click() {this.dispatchEvent({type: 'click', target: this, preventDefault() {}});}
  append(...nodes) {for (const node of nodes) this.children.push(...(node.tag === 'fragment' ? node.children : [node]));}
  replaceChildren(...nodes) {this.children = []; this.ownText = ''; this.append(...nodes);}
  set textContent(value) {this.ownText = String(value); this.children = [];}
  get textContent() {return (this.ownText || '') + this.children.map(child => child.textContent).join('');}
  set innerHTML(value) {throw new Error('No se debe insertar HTML no confiable');}
  set href(value) {this.attributes.href = value;}
  get href() {return this.attributes.href || '';}
  get firstElementChild() {return this.children[0] || null;}
  cloneNode() {return this.isWelcome ? makeWelcome() : new Element(this.tag);}
  setAttribute(key, value) {this.attributes[key] = value;}
  removeAttribute(key) {delete this.attributes[key];}
  querySelector(selector) {return this.selectors?.[selector] || null;}
  querySelectorAll(selector) {return selector === 'button' ? this.children.filter(child => child.tag === 'button') : [];}
  focus() {}
  showModal() {this.open = true;}
  close() {this.open = false;}
}
function makeWelcome() {
  const node = new Element(); node.isWelcome = true;
  node.selectors = Object.fromEntries(['.eyebrow', '.welcome-title', '.welcome-description'].map(key => [key, new Element()]));
  node.selectors['.welcome-title'].textContent = 'Bienvenida al canal';
  node.append(...Object.values(node.selectors));
  return node;
}
const ids = [...fs.readFileSync('templates/chat.html', 'utf8').matchAll(/\bid="([^\"]+)"/g)].map(match => match[1]);
const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
elements.chatBox.append(makeWelcome());
const headers = Object.fromEntries(['.header-title h1', '.header-title p', '.header-channel'].map(name => [name, new Element()]));
const documentEvents = new Element();
const document = {
  readyState: 'complete', hidden: false,
  getElementById: id => elements[id],
  querySelector: name => headers[name] || null,
  createElement: tag => new Element(tag),
  createTextNode: text => {const element = new Element('text'); element.textContent = text; return element;},
  createDocumentFragment: () => new Element('fragment'),
  addEventListener: (...args) => documentEvents.addEventListener(...args)
};
let timerId = 0;
const timers = new Map(), calls = [], community = [], conversations = new Map(), agentJobs = new Map();
const downloadNotices = new Map();
let resolveFirstCommunity, resolveJobPoll, resolveAgentAck;
let firstCommunity = true, holdAgent = false, holdJobPoll = false, holdAgentAck = false, failJobPoll = false, loseAgentAck = false;
let holdProject = false, resolveProject, jobComplete = false, loseCommunityAck = true;
let downloadJobsOverride = null;
let showPromptFailure = false, promptLoadable = false;
function completeAgentJob(job, status = 'complete') {
  job.status = status;
  if (status === 'complete' && !job.result) {
    job.result = {model: 'equipo simulado', answer: 'RESPUESTA PRIVADA SIMULADA'};
    conversations.get(job.conversation_id).history.push({role: 'user', content: job.prompt},
      {role: 'assistant', content: job.result.answer, sources: []});
  }
  if (status === 'failed') job.error = 'FALLO PRIVADO DE VERIFICACIÓN';
}
const response = data => ({ok: true, status: 200, json: async () => data});
const window = new Element();
window.setTimeout = (callback, duration) => {timers.set(++timerId, {callback, duration}); return timerId;};
window.clearTimeout = id => timers.delete(id);
window.projectSession = async () => ({username: 'usuario_simulado', csrf_token: 'csrf-simulado'});
window.projectApi = async (path, options = {}) => {
  const method = options.method || 'GET';
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({path, method, body});
  if (path === '/api/messages') {
    if (method === 'POST') {
      if (!community.some(item => item.request_id === body.request_id)) community.push({id: community.length + 1, username: 'usuario_simulado', current_user: 'usuario_simulado', role: 'user', message: body.message, request_id: body.request_id, assistant_status: 'queued'});
      if (loseCommunityAck) {loseCommunityAck = false; throw new TypeError('Conexión interrumpida después de guardar');}
      return response({status: 'saved', assistant_status: 'queued'});
    }
    if (firstCommunity) {firstCommunity = false; return new Promise(resolve => {resolveFirstCommunity = () => resolve(response([{id: 99, username: 'otro', message: 'PUBLICO ATRASADO'}]));});}
    return response(community);
  }
  if (path === '/api/agent/config') return response({run_timeout_seconds: 90, default_backend: 'local', default_profile: 'equipo',
    profiles: [{id: 'general', name: 'Mi agente'}, {id: 'equipo', name: 'Equipo de especialistas'}, {id: 'zyon', name: 'ZYON', display_name: 'ZYON'}], backends: [{id: 'local', label: 'Modelo simulado', configured: true}]});
  if (path.endsWith('/project')) {
    if (holdProject) await new Promise(resolve => {resolveProject = resolve;});
    return response({files: [{path: 'index.html', bytes: 28, content: '<img onerror=alert(1)> PRIVADO'}],
      notices: downloadNotices.get(path.split('/')[4]) || [],
      downloads: [{id: 'a0a0a0a0-0000-4000-8000-000000000000', filename: '<img onerror=alert(1)> cancion.mp3', bytes: 4096}],
      jobs: downloadJobsOverride ?? (jobComplete ? [] : [{id: 'b0b0b0b0-0000-4000-8000-000000000000', status: 'running', message: 'Convirtiendo a MP3.',
        steps: [{kind: 'operation', method: 'http', operation: 'GET', url: 'https://example.org/PRIVATE-PAGE'},
          {kind: 'attempt_failed', method: 'http', message: 'ERROR PRIVADO SIMULADO'},
          {kind: 'operation', method: 'ffmpeg', command: ['ffmpeg', '-i', '<img onerror=alert(1)>.wav', 'out.mp3']}],
        progress: {title: 'AUDIO LARGO PRIVADO', phase: 'converting', percent: 42}}])});
  }
  if (path.endsWith('/download-jobs') && method === 'POST') return response({status: 'queued', job: {id: randomUUID()}});
  if (path.includes('/activity?') && showPromptFailure) return response({status: 'failed', error_code: 'prompt_too_long', prompt_configuration: {loadable: promptLoadable}, events: []});
  if (path.includes('/activity?')) return response({status: holdAgent ? 'running' : 'complete', request_id: null, events: [
    {id: 1, kind: 'script', title: 'Script de prueba', payload: {language: 'python', purpose: 'Prueba de salida', code: "print('<img onerror=alert(1)>')"}},
    {id: 2, kind: 'observation', title: 'Salida del script', payload: {result: {exit_code: 0, ok: true, stdout: 'TRACE PRIVADO SIMULADO'}}},
    {id: 3, kind: 'task_graph', title: 'Estado del plan', payload: {task_graph: {
      complete: false, nodes: [{id: 'guardar', title: '<img onerror=alert(1)> PLAN PRIVADO', tool: 'project_write', status: 'observed', depends_on: []},
        {id: 'probar', title: 'Comprobar archivos', tool: 'sandbox_exec', status: 'pending', depends_on: ['guardar']}],
      edges: [{source: 'guardar', target: 'probar'}], scope: 'Resultados observados, sin garantía semántica'
    }}},
    {id: 4, kind: 'worker_started', title: 'Especialista iniciado', payload: {worker: 'Investigador <img onerror=alert(1)>'}},
    {id: 5, kind: 'source_read', title: 'Lectura de fuente', payload: {title: 'FUENTE PRIVADA', url: 'https://example.org/'}},
    {id: 6, kind: 'worker_completed', title: 'Especialista finalizado', payload: {worker: 'Investigador'}},
    {id: 7, kind: 'team_review', title: 'Revisión final', payload: {verdict: 'COMPROBACIÓN PRIVADA'}},
    {id: 8, kind: 'sandbox_started', title: 'Contenedor solicitado', payload: {container: 'community-lab-prueba', purpose: 'code', cpu_limit: 1, memory_mb: 256, timeout_seconds: 15, network: 'none'}},
    {id: 9, kind: 'sandbox_output', title: 'Salida recibida', payload: {container: 'community-lab-prueba', stdout: 'CONSOLA <img onerror=alert(1)>'}},
    {id: 10, kind: 'sandbox_finished', title: 'Contenedor finalizado', payload: {container: 'community-lab-prueba', status: 'complete', exit_code: 0, stdout: 'CONSOLA FINAL PRIVADA'}}
  ]});
  if (path === '/api/agent/conversations') {
    if (method === 'GET') return response([...conversations.values()]);
    const conversation = {id: randomUUID(), profile: body.profile, backend: body.backend, title: 'Nueva conversación', history: []};
    conversations.set(conversation.id, conversation); return response(conversation);
  }
  const jobMatch = path.match(/^\/api\/agent\/conversations\/([^/]+)\/jobs(?:\/([^/]+))?(\/cancel)?$/);
  if (jobMatch) {
    if (!jobMatch[2]) return response([...agentJobs.values()].filter(job => job.conversation_id === jobMatch[1]).reverse());
    const job = agentJobs.get(jobMatch[2]);
    assert(job && job.conversation_id === jobMatch[1], 'Se consultó un trabajo de otra conversación');
    if (jobMatch[3]) { assert.equal(method, 'POST'); job.status = 'cancelled'; return response({job}); }
    if (holdJobPoll) await new Promise(resolve => {resolveJobPoll = resolve;});
    if (failJobPoll) { failJobPoll = false; throw new TypeError('Error de red simulado'); }
    if (!holdAgent && ['queued', 'running'].includes(job.status)) completeAgentJob(job);
    else if (job.status === 'queued') job.status = 'running';
    return response({...job});
  }
  const match = path.match(/^\/api\/agent\/conversations\/([^/]+)\/messages$/);
  if (match) {
    const conversation = conversations.get(match[1]);
    if (method === 'GET') return response([...conversation.history, ...(downloadNotices.get(conversation.id) || [])]);
    assert.equal(body.background, true, 'Las consultas privadas deben ejecutarse en segundo plano');
    let job = agentJobs.get(body.request_id);
    if (!job) {
      job = {id: randomUUID(), request_id: body.request_id, conversation_id: conversation.id, status: 'queued', prompt: body.message};
      agentJobs.set(job.request_id, job);
    }
    if (loseAgentAck) { loseAgentAck = false; throw new TypeError('ACK privado perdido después de guardar'); }
    if (holdAgentAck) await new Promise(resolve => {resolveAgentAck = resolve;});
    return response({status: job.status, job: {...job}, result: job.result});
  }
  throw new Error('Ruta inesperada en la prueba: ' + path);
};
const context = vm.createContext({window, document, console, AbortController, Event, crypto: {randomUUID},
  setTimeout: window.setTimeout, clearTimeout: window.clearTimeout});
for (const file of ['static/api.js', 'static/app.js', 'static/terminal.js', 'static/monitor.js', 'static/projects.js', 'static/activity.js', 'static/agent.js', 'static/chat.js', 'static/effects.js']) {
  new vm.Script(fs.readFileSync(file, 'utf8'), {filename: file});
}
for (const file of ['static/app.js', 'static/terminal.js', 'static/monitor.js', 'static/projects.js', 'static/activity.js', 'static/agent.js']) vm.runInContext(fs.readFileSync(file, 'utf8'), context, {filename: file});
async function flush() {for (let i = 0; i < 200; i++) await Promise.resolve();}
async function tick(duration) {
  const ready = [...timers.entries()].filter(([, timer]) => timer.duration === duration);
  for (const [id, timer] of ready) {timers.delete(id); timer.callback();}
  await flush();
}

(async () => {
  elements.agentMode.click(); await flush();
  assert.equal(window.agentChat.active, true);
  assert.equal(elements.agentProfile.value, 'equipo', 'Debe seleccionar el equipo configurado por defecto');
  resolveFirstCommunity(); await flush();
  assert(!elements.chatBox.textContent.includes('PUBLICO ATRASADO'), 'Una lectura comunitaria atrasada invadió el agente');
  elements.userInput.value = 'MI MENSAJE PRIVADO';
  elements.userInput.dispatchEvent(new Event('input'));
  await window.sendMessage();
  assert(!elements.chatBox.textContent.includes('RESPUESTA PRIVADA SIMULADA'), 'No debe anunciar una respuesta antes de terminar');
  assert(elements.chatBox.textContent.includes('MI MENSAJE PRIVADO'), 'El trabajo aceptado debe mostrar la petición provisional');
  assert(elements.composerStatus.textContent.includes('segundo plano'));
  assert.equal(elements.userInput.value, '', 'El trabajo aceptado debe liberar el borrador enviado');
  assert.equal(elements.agentProfile.disabled, false, 'Debe permitir cambiar de perfil mientras trabaja');
  assert([...timers.values()].some(timer => timer.duration === 1500), 'El trabajo requiere sondeo breve');
  elements.chatBox.scrollHeight = 1000;
  elements.conversationScroll.clientHeight = 400;
  await tick(1500);
  assert(elements.chatBox.textContent.includes('RESPUESTA PRIVADA SIMULADA'));
  assert.equal(elements.chatMain.dataset.mode, 'private');
  assert.equal(elements.conversationScroll.scrollTop, 600, 'El chat privado debe desplazar la conversación completa hasta el mensaje');
  elements.chatBox.scrollHeight = 200; elements.conversationScroll.clientHeight = 200;
  assert.equal(elements.activityPanel, undefined, 'El registro anterior no debe estar en el chat');
  assert(!fs.readFileSync('templates/chat.html', 'utf8').includes('Registro de actividad'));
  assert(elements.terminalOutput.textContent.includes('TRACE PRIVADO SIMULADO'));
  assert(elements.terminalOutput.textContent.includes('<img onerror=alert(1)>'), 'El código debe mostrarse como texto');
  assert(elements.monitorPlan.textContent.includes('PLAN PRIVADO'), 'El plan debe aparecer como texto seguro');
  assert(elements.monitorPlan.textContent.includes('1 de 2 pasos'), 'El monitor conserva el avance del plan');
  assert(elements.monitorPlan.textContent.includes('Pendiente'), 'El plan incompleto debe indicar los pasos pendientes');
  assert(elements.terminalOutput.textContent.includes('Investigador <img onerror=alert(1)>'));
  assert(elements.terminalOutput.textContent.includes('COMPROBACIÓN PRIVADA'), 'La revisión del equipo debe ser visible');
  assert.equal(elements.workMonitor.hidden, false);
  assert(elements.workMonitorState.textContent.includes('descarga'), 'La descarga continúa aunque termine la respuesta del chat');
  elements.openWorkMonitor.click();
  assert.equal(elements.workMonitorDialog.open, true);
  assert(elements.monitorContainers.textContent.includes('CONSOLA FINAL PRIVADA'));
  assert(elements.monitorContainers.textContent.includes('Código de salida: 0'));
  assert(elements.monitorPlan.textContent.includes('1 de 2 pasos'));
  assert(!elements.monitorDownloads.textContent.includes('42.0 %'), 'La conversión no debe reutilizar el porcentaje de transferencia');
  window.agentMonitor.openDownload('b0b0b0b0-0000-4000-8000-000000000000');
  assert.equal(elements.downloadTraceDialog.open, true);
  assert(elements.downloadTraceSteps.textContent.includes('GET'));
  assert(elements.downloadTraceSteps.textContent.includes('ERROR PRIVADO SIMULADO'));
  assert(elements.downloadTraceSteps.textContent.includes('<img onerror=alert(1)>.wav'), 'Los comandos deben mostrarse como texto seguro');
  elements.closeDownloadTrace.click();
  assert.equal(elements.downloadTraceDialog.open, false);
  elements.closeWorkMonitor.click();
  assert.equal(elements.workMonitorDialog.open, false);
  assert.equal(elements.projectPanel.hidden, false);
  assert(elements.projectFiles.textContent.includes('<img onerror=alert(1)> PRIVADO'), 'Los archivos deben mostrarse como texto');
  assert(elements.downloadProject.href.endsWith('/project.zip'));
  assert(elements.projectFiles.textContent.includes('Descargar <img onerror=alert(1)> cancion.mp3'), 'La descarga debe mostrarse como enlace con texto seguro');
  assert(elements.projectFiles.children.some(item => item.children.some(link => link.href?.includes('/downloads/'))), 'Falta el enlace de descarga real');
  assert(elements.projectFiles.textContent.includes('Convirtiendo a MP3.'));
  assert(!elements.projectFiles.textContent.includes('42.0 %'), 'La conversión no debe reutilizar el porcentaje de transferencia');
  assert(elements.projectFiles.textContent.includes('Cancelar descarga'), 'Falta el control para cancelar una descarga pendiente');
  const progressPoll = [...timers.values()].find(timer => timer.duration === 1500);
  assert(progressPoll, 'Las descargas pendientes deben actualizarse solas');
  jobComplete = true; progressPoll.callback(); await flush();
  assert(!elements.projectFiles.textContent.includes('AUDIO LARGO PRIVADO'), 'Una descarga completada debe dejar de mostrarse pendiente');
  assert(elements.projectFiles.textContent.includes('Descargar'), 'El archivo final debe seguir disponible');
  elements.mediaDownloadUrl.value = 'https://example.org/video'; elements.mediaDownloadMode.value = 'video';
  elements.mediaDownloadForm.dispatchEvent({type: 'submit', preventDefault() {}}); await flush();
  const videoRequest = calls.find(call => call.path.endsWith('/download-jobs') && call.method === 'POST');
  assert.deepEqual(videoRequest.body, {url: 'https://example.org/video', mode: 'video'}, 'El selector debe enviar vídeo completo sin invocar al modelo');
  assert(elements.mediaDownloadStatus.textContent.includes('Descarga iniciada'));
  assert.equal(elements.mediaDownloadUrl.value, '');
  elements.mediaDownloadUrl.value = 'https://example.org/article'; elements.mediaDownloadMode.value = 'pdf';
  elements.mediaDownloadForm.dispatchEvent({type: 'submit', preventDefault() {}}); await flush();
  const pdfRequest = calls.filter(call => call.path.endsWith('/download-jobs') && call.method === 'POST').at(-1);
  assert.deepEqual(pdfRequest.body, {url: 'https://example.org/article', mode: 'pdf'});
  assert(!calls.some(call => call.path === '/api/messages' && call.method === 'POST'), 'El mensaje privado llegó al canal público');

  holdProject = true;
  const pendingProject = window.agentProjects.refresh(); await flush();
  assert(resolveProject);
  elements.communityMode.click(); await flush();
  holdProject = false; resolveProject(); await pendingProject; await flush();
  assert.equal(window.agentChat.active, false);
  assert.equal(elements.workMonitor.hidden, true);
  assert.equal(elements.workMonitorDialog.open, false);
  assert.equal(elements.monitorContainers.textContent, '', 'La consola privada debe borrarse al cambiar al principal');
  assert.equal(elements.downloadTraceDialog.open, false);
  assert.equal(elements.downloadTraceSteps.textContent, '');
  assert.equal(elements.projectPanel.hidden, true);
  assert.equal(elements.projectFiles.children.length, 0);
  assert.equal(elements.mediaDownloadStatus.textContent, '', 'El estado de descarga debe borrarse al salir de la conversación');
  assert(!elements.downloadProject.attributes.href);
  assert(!elements.chatBox.textContent.includes('RESPUESTA PRIVADA SIMULADA'));
  elements.userInput.value = 'MENSAJE PUBLICO';
  elements.userInput.dispatchEvent(new Event('input'));
  await window.sendMessage(); await flush();
  assert.equal(elements.userInput.value, 'MENSAJE PUBLICO', 'Un envío sin confirmación debe conservar el borrador');
  await window.sendMessage(); await flush();
  assert.equal(community[0].message, 'MENSAJE PUBLICO');
  assert.equal(community.length, 1, 'Reintentar no debe publicar dos veces el mismo mensaje');
  const publicSends = calls.filter(call => call.path === '/api/messages' && call.method === 'POST');
  assert.equal(publicSends[0].body.request_id, publicSends[1].body.request_id, 'Los reintentos deben conservar el identificador');
  assert(elements.chatBox.textContent.includes('Respuesta de la IA en cola'), 'El principal debe mostrar que la IA responderá');
  community[0].assistant_status = 'running'; await window.loadMessages();
  assert(elements.chatBox.textContent.includes('La IA está respondiendo'), 'El estado de la IA debe actualizarse al consultar el canal');
  community[0].assistant_status = 'complete';
  community.push({id: 2, username: 'Agente', role: 'assistant', message: 'RESPUESTA PUBLICA SIMULADA <img onerror=alert(1)>', reply_username: 'usuario_simulado', team_status: 'complete',
    sources: [{id: 1, kind: 'web', title: 'FUENTE PÚBLICA <img onerror=alert(1)>', url: 'https://docs.example.org/referencia'},
      {id: 2, kind: 'web', title: 'URL INSEGURA', url: 'javascript:alert(1)'},
      {id: 3, kind: 'memory', title: 'MEMORIA NO PUBLICABLE', url: 'https://example.org/privado'},
      {id: 4, kind: 'web', title: 'SEGUNDA FUENTE PÚBLICA', url: 'https://reference.example.net/'}]});
  await window.loadMessages();
  assert(elements.chatBox.textContent.includes('RESPUESTA PUBLICA SIMULADA <img onerror=alert(1)>'), 'La respuesta pública debe mostrarse como texto seguro');
  assert(elements.chatBox.textContent.includes('Agente · para usuario_simulado'), 'La respuesta debe identificar al agente y a su destinatario');
  assert(!elements.chatBox.textContent.includes('La IA está respondiendo'), 'El estado pendiente debe desaparecer al terminar');
  assert(elements.chatBox.textContent.includes('FUENTE PÚBLICA <img onerror=alert(1)>'), 'Las fuentes públicas deben mostrarse como texto seguro');
  assert(elements.chatBox.textContent.includes('SEGUNDA FUENTE PÚBLICA'));
  assert(!elements.chatBox.textContent.includes('URL INSEGURA'));
  assert(!elements.chatBox.textContent.includes('MEMORIA NO PUBLICABLE'));
  function descendants(element) {return [element, ...element.children.flatMap(descendants)];}
  const sourceLinks = descendants(elements.chatBox).filter(element => element.tag === 'a');
  assert.equal(sourceLinks.length, 2);
  assert(sourceLinks.every(link => /^https:\/\//.test(link.href) && link.rel === 'noopener noreferrer'));
  community[1].sources[0].title = 'FUENTE PÚBLICA ACTUALIZADA'; await window.loadMessages();
  assert(elements.chatBox.textContent.includes('FUENTE PÚBLICA ACTUALIZADA'), 'Actualizar evidencias debe volver a dibujar el mensaje existente');

  elements.agentMode.click(); await flush();
  holdAgent = true;
  elements.userInput.value = 'SEGUNDO PRIVADO';
  elements.userInput.dispatchEvent(new Event('input'));
  await window.sendMessage(); await flush();
  const runningJob = [...agentJobs.values()].at(-1), originalConversation = conversations.get(runningJob.conversation_id);
  assert.equal(runningJob.status, 'queued');
  elements.userInput.value = 'BORRADOR PRIVADO ORIGINAL';
  await window.agentChat.newConversation(); await flush();
  assert.equal(conversations.size, 2, 'Debe permitir otra conversación mientras el equipo trabaja');
  elements.userInput.value = 'BORRADOR DE OTRA CONVERSACIÓN';
  elements.agentConversations.children[0].click(); await flush();
  assert.equal(elements.userInput.value, 'BORRADOR PRIVADO ORIGINAL', 'Cada conversación debe conservar su borrador');
  assert(elements.chatBox.textContent.includes('SEGUNDO PRIVADO'), 'Recuperar el trabajo debe recuperar su petición');
  assert(calls.some(call => call.path === `/api/agent/conversations/${originalConversation.id}/jobs`), 'Debe recuperar trabajos al abrir una conversación');
  failJobPoll = true;
  await tick(1500);
  assert(elements.composerStatus.textContent.includes('Reintentando automáticamente'), 'Un error de sondeo debe permitir recuperarse');
  assert([...timers.values()].some(timer => timer.duration === 1500));
  await tick(1500);
  assert.equal(runningJob.status, 'running');
  elements.communityMode.click(); await flush();
  elements.userInput.value = 'BORRADOR PUBLICO';
  completeAgentJob(runningJob); await tick(1500);
  assert.equal(elements.userInput.value, 'BORRADOR PUBLICO', 'La respuesta privada borró un borrador público');
  assert(!elements.chatBox.textContent.includes('SEGUNDO PRIVADO'), 'La respuesta privada apareció en el canal público');
  assert.equal(elements.projectPanel.hidden, true, 'La respuesta privada reabrió los archivos en el canal público');

  elements.agentMode.click(); await flush();
  assert.equal(elements.userInput.value, 'BORRADOR PRIVADO ORIGINAL');
  assert.equal(elements.chatBox.textContent.split('RESPUESTA PRIVADA SIMULADA').length - 1, 2, 'Volver debe mostrar el resultado completado en el historial');
  elements.agentConversations.children[1].click(); await flush();
  assert.equal(elements.userInput.value, 'BORRADOR DE OTRA CONVERSACIÓN');

  // Un ACK perdido conserva el request_id y el texto, sin crear otro trabajo.
  loseAgentAck = true;
  const countBeforeRetry = agentJobs.size;
  await window.sendMessage();
  assert.equal(elements.userInput.value, 'BORRADOR DE OTRA CONVERSACIÓN');
  await window.sendMessage();
  assert.equal(agentJobs.size, countBeforeRetry + 1);
  const privateSends = calls.filter(call => call.path.endsWith('/messages') && call.path.includes('/agent/') && call.method === 'POST');
  assert.equal(privateSends.at(-1).body.request_id, privateSends.at(-2).body.request_id);
  const failedJob = [...agentJobs.values()].at(-1);
  completeAgentJob(failedJob, 'failed'); await tick(1500);
  assert(elements.composerStatus.textContent.includes('FALLO PRIVADO DE VERIFICACIÓN'), 'Debe mostrar el fallo sin anunciar una respuesta');
  elements.userInput.value = 'OTRO TRABAJO PRIVADO'; elements.userInput.dispatchEvent(new Event('input'));
  await window.sendMessage();
  const cancelButton = elements.cancelAgentJob;
  assert(cancelButton, 'Falta el botón de cancelar trabajo');
  assert.equal(cancelButton.hidden, false, 'La consola debe permitir cancelar el trabajo activo');
  cancelButton.click(); await flush();
  assert.equal([...agentJobs.values()].at(-1).status, 'cancelled');
  assert(elements.composerStatus.textContent.includes('cancelado'));

  elements.userInput.value = 'PETICIÓN PRIVADA CON ACK ATRASADO'; elements.userInput.dispatchEvent(new Event('input'));
  holdAgentAck = true;
  const lateAck = window.sendMessage(); await flush();
  assert(resolveAgentAck);
  elements.communityMode.click(); await flush();
  elements.userInput.value = 'BORRADOR PÚBLICO DURANTE ACK PRIVADO';
  holdAgentAck = false; resolveAgentAck(); await lateAck; await flush();
  assert.equal(elements.userInput.value, 'BORRADOR PÚBLICO DURANTE ACK PRIVADO');
  assert.equal(elements.projectPanel.hidden, true);
  assert(!elements.chatBox.textContent.includes('PETICIÓN PRIVADA CON ACK ATRASADO'));
  completeAgentJob([...agentJobs.values()].at(-1));
  elements.agentMode.click(); await flush();

  const notifiedConversation = window.agentChat.conversationId;
  elements.userInput.value = 'BORRADOR DURANTE AVISO DE DESCARGA';
  downloadNotices.set(notifiedConversation, [{id: 'download-notice:1', notification_id: 1, role: 'assistant',
    content: 'DESCARGA LISTA PRIVADA <img onerror=alert(1)>', sources: [],
    download: {id: 'a0a0a0a0-0000-4000-8000-000000000000', filename: 'video listo.mp4'}}]);
  await window.agentProjects.refresh(); await flush();
  assert(elements.chatBox.textContent.includes('DESCARGA LISTA PRIVADA <img onerror=alert(1)>'), 'El aviso debe aparecer automáticamente como texto seguro en el chat');
  assert(elements.chatBox.textContent.includes('Descargar video listo.mp4'));
  assert.equal(elements.userInput.value, 'BORRADOR DURANTE AVISO DE DESCARGA');
  await window.agentProjects.refresh(); await flush();
  assert.equal(elements.chatBox.textContent.split('DESCARGA LISTA PRIVADA').length - 1, 1, 'El sondeo no debe duplicar el aviso');
  elements.communityMode.click(); await flush();
  assert(!elements.chatBox.textContent.includes('DESCARGA LISTA PRIVADA'), 'El aviso privado no debe aparecer en el principal');
  elements.agentMode.click(); await flush();
  assert(elements.chatBox.textContent.includes('DESCARGA LISTA PRIVADA'), 'El aviso debe recuperarse al volver');

  elements.agentProfile.value = 'zyon'; elements.agentProfile.dispatchEvent(new Event('change')); await flush();
  assert.equal(conversations.get(window.agentChat.conversationId).profile, 'zyon');
  assert.equal(headers['.header-title h1'].textContent, 'ZYON');
  assert.equal(elements.openBehavior, undefined, 'La documentación técnica no debe tener un botón en el chat');
  assert.equal(elements.behaviorDialog, undefined);
  assert.equal(elements.agentProfile.children.find(item => item.value === 'zyon').textContent, 'ZYON');
  holdAgent = false;
  elements.userInput.value = 'SALUDO CON ZYON'; elements.userInput.dispatchEvent(new Event('input'));
  await window.sendMessage(); await tick(1500);
  assert(descendants(elements.chatBox).some(element => element.className === 'message-author' && element.textContent === 'ZYON'));
  holdAgent = true;
  elements.communityMode.click(); await flush();
  elements.agentMode.click(); await flush();
  assert(!calls.some(call => /\/profiles\/[^/]+\/behavior$/.test(call.path)), 'Navegar o conversar no debe pedir instrucciones para mostrarlas');

  const failedPromptConversation = window.agentChat.conversationId;
  const postsBeforeRecovery = calls.filter(call => call.method === 'POST' && call.path.endsWith('/messages')).length;
  const failedPromptJob = [...agentJobs.values()].filter(job => job.conversation_id === failedPromptConversation).at(-1);
  Object.assign(failedPromptJob, {status: 'failed', error_code: 'prompt_too_long', error: 'Las instrucciones superan 20.000 caracteres.'});
  showPromptFailure = true;
  elements.communityMode.click(); await flush();
  elements.agentMode.click(); await flush();
  assert.equal(elements.recoverPromptChat.hidden, false, 'Debe ofrecer otro chat cuando no se pueden cargar las instrucciones');
  promptLoadable = true;
  elements.communityMode.click(); await flush();
  elements.agentMode.click(); await flush();
  assert(elements.workMonitorState.textContent.includes('ya se puede cargar'));
  assert(elements.composerStatus.textContent.includes('ya se puede cargar'), 'El compositor debe indicar que el error anterior ya está reparado');
  assert.equal(elements.recoverPromptChat.hidden, true, 'El perfil reparado no requiere cambiar a otro chat');
  assert.equal(window.agentChat.conversationId, failedPromptConversation);
  assert.equal(elements.workMonitor.dataset.state, 'failed', 'El intento anterior debe seguir registrado como fallido');
  promptLoadable = false;
  elements.communityMode.click(); await flush();
  elements.agentMode.click(); await flush();
  showPromptFailure = false;
  elements.userInput.value = 'BORRADOR PARA RECUPERAR EL CHAT';
  elements.recoverPromptChat.click(); await flush();
  assert.notEqual(window.agentChat.conversationId, failedPromptConversation);
  assert.equal(conversations.get(window.agentChat.conversationId).profile, 'general');
  assert(conversations.has(failedPromptConversation), 'La recuperación debe conservar la conversación anterior');
  assert.equal(elements.userInput.value, 'BORRADOR PARA RECUPERAR EL CHAT');
  assert.equal(calls.filter(call => call.method === 'POST' && call.path.endsWith('/messages')).length, postsBeforeRecovery, 'El botón no debe enviar texto sin una nueva acción del usuario');
  assert.equal(elements.recoverPromptChat.hidden, true);

  elements.userInput.value = 'TRABAJO ANTES DE CADUCAR'; elements.userInput.dispatchEvent(new Event('input'));
  await window.sendMessage();
  holdJobPoll = true; await tick(1500);
  assert(resolveJobPoll, 'La consulta de trabajo debe quedar pendiente para probar la caducidad');

  window.dispatchEvent(new Event('project-session-expired'));
  holdJobPoll = false; resolveJobPoll(); await flush();
  assert.equal(elements.chatBox.children.length, 0);
  assert.equal(elements.userInput.disabled, true);
  assert.equal(elements.agentConversations.children.length, 0);
  assert.equal(elements.terminalOutput.children.length, 0);
  assert.equal(elements.projectFiles.children.length, 0);
  assert.equal(elements.workMonitor.hidden, true);
  assert.equal(elements.monitorContainers.textContent, '');
  // Estados del monitor con procesos concurrentes, muestras de consola y respuestas atrasadas.
  const monitor = window.agentMonitor;
  monitor.start('monitor-private', 'request-private');
  const liveEvents = [
    {id: 1, kind: 'worker_started', title: 'Investigación A', payload: {worker_id: 'investigador_a'}},
    {id: 2, kind: 'worker_started', title: 'Investigación B', payload: {worker_id: 'investigador_b'}},
    {id: 3, kind: 'sandbox_started', title: 'Contenedor solicitado', payload: {container: 'community-lab-live', purpose: 'code', cpu_limit: 1, memory_mb: 256, timeout_seconds: 15, network: 'none'}},
    {id: 4, kind: 'sandbox_output', title: 'Salida recibida', payload: {container: 'community-lab-live', stdout: '<script>salida privada</script>'}}
  ];
  monitor.activity('monitor-private', {status: 'running', request_id: 'request-private', events: liveEvents});
  assert(elements.monitorWorkers.textContent.includes('investigador_a · En curso'));
  assert(elements.monitorWorkers.textContent.includes('investigador_b · En curso'));
  assert(elements.monitorContainers.textContent.includes('<script>salida privada</script>'));
  assert(elements.monitorContainers.textContent.includes('esperando cierre'));
  monitor.connectionLost('monitor-private');
  assert(elements.workMonitorState.textContent.includes('interrumpida'));
  monitor.activity('monitor-private', {status: 'complete', request_id: 'request-private', events: liveEvents});
  assert(elements.workMonitorState.textContent.includes('terminado'));
  assert(elements.monitorContainers.textContent.includes('Sin cierre registrado'), 'No se debe fingir un cierre que no llegó');
  monitor.activity('monitor-private', {status: 'complete', events: [{id: 50, kind: 'completed', title: 'Respuesta disponible', payload: {review: 'unresolved'}}]});
  assert(elements.workMonitorState.textContent.includes('parcial'), 'Una respuesta con pasos pendientes no debe presentarse como tarea verificada');
  assert.equal(elements.terminalState.textContent, 'Objetivo pendiente');
  assert(elements.workMonitorHint.textContent.includes('no se considera verificada'));
  monitor.activity('monitor-private', {status: 'complete', events: [{id: 51, kind: 'completed', title: 'Respuesta disponible', payload: {review: 'model_reviewed'}}]});
  assert(elements.workMonitorState.textContent.includes('terminado'));
  monitor.downloads('monitor-private', [{status: 'running', progress: {phase: 'downloading', percent: 37}}], []);
  assert(elements.workMonitorState.textContent.includes('descarga'));
  assert(elements.monitorDownloads.textContent.includes('37.0 % transferido'));
  monitor.downloads('monitor-private', [{id: 'paused', status: 'queued', message: 'Esperando para reintentar automáticamente.',
    progress: {phase: 'waiting', retry_at: Date.now() / 1000 + 300}}], []);
  assert.equal(elements.workMonitorState.textContent, 'Descargas en espera');
  assert(elements.workMonitorHint.textContent.includes('Próximo intento:'));
  assert(elements.monitorDownloads.textContent.includes('Próximo intento:'));
  assert(elements.monitorDownloads.textContent.includes('Espera restante:'));
  assert(!elements.monitorDownloads.children.some(row => row.children.some(child => child.tag === 'progress')), 'Una espera no debe animar una transferencia');
  monitor.downloads('monitor-private', [{id: 'live', status: 'running', message: 'Descargando', steps: [{kind: 'attempt', method: 'http'}]}], []);
  monitor.openDownload('live');
  assert.equal(elements.downloadTraceDialog.open, true);
  monitor.downloads('monitor-private', [{id: 'live', status: 'failed', message: 'Acceso requerido', steps: [{kind: 'attempt_failed', method: 'http', message: 'ERROR FINAL EN VIVO'}]}], []);
  assert(elements.downloadTraceSteps.textContent.includes('ERROR FINAL EN VIVO'), 'La ventana abierta debe recibir nuevos pasos');
  monitor.downloads('monitor-private', [], []);
  monitor.activity('monitor-private', {status: 'failed', error_code: 'run_timeout', events: []});
  assert(elements.workMonitorHint.textContent.includes('tiempo de ejecución'));
  monitor.activity('monitor-private', {status: 'failed', error_code: 'execution_limit', events: []});
  assert.equal(elements.workMonitorState.textContent, 'Se alcanzó el límite de pasos');
  assert(elements.workMonitorHint.textContent.includes('antes de completar'));
  monitor.activity('monitor-private', {status: 'failed', error_code: 'context_limit', events: []});
  assert.equal(elements.workMonitorState.textContent, 'La petición supera el contexto del modelo');
  assert(elements.workMonitorHint.textContent.includes('tamaño total'));
  monitor.activity('monitor-private', {status: 'failed', error_code: 'prompt_too_long', events: [
    {id: 5, kind: 'error', title: 'Las instrucciones activas suman 35.928 caracteres y superan el límite de 20.000.', payload: {code: 'prompt_too_long'}}
  ]});
  assert(elements.workMonitorState.textContent.includes('instrucciones'));
  assert(elements.workMonitorHint.textContent.includes('35.928'));
  monitor.activity('monitor-private', {status: 'failed', error_code: 'prompt_too_long', prompt_configuration: {loadable: true}, events: []});
  assert(elements.workMonitorState.textContent.includes('ya se puede cargar'));
  assert(elements.workMonitorHint.textContent.includes('intento anterior'));
  assert.equal(monitor.promptRecovered('monitor-private'), true);
  assert.equal(monitor.promptRecovered('otra-conversacion'), false);
  assert.equal(elements.workMonitor.dataset.state, 'failed');
  monitor.start('monitor-private', 'legacy-request');
  assert.equal(monitor.promptRecovered('monitor-private'), false, 'Una nueva ejecución debe descartar la comprobación anterior');
  monitor.activity('monitor-private', {status: 'failed', error_code: 'invalid_prompt', events: []});
  assert(elements.workMonitorHint.textContent.includes('documentación del proyecto'), 'Los errores deben dirigir al registro y documentación sin ofrecer una ventana eliminada');
  elements.openWorkMonitor.click();
  monitor.select('otra-conversacion');
  assert.equal(elements.workMonitorDialog.open, false);
  monitor.activity('monitor-private', {status: 'running', events: liveEvents});
  monitor.downloads('monitor-private', [{status: 'running', message: 'SECRETO ATRASADO'}], []);
  assert.equal(elements.monitorContainers.textContent, '');
  assert.equal(elements.monitorDownloads.textContent, '');
  assert.equal(elements.downloadTraceSteps.textContent, '');
  assert.equal(elements.terminalOutput.textContent, '', 'La consola privada debe borrarse al cambiar de conversación');
  monitor.start('terminal-private', 'terminal-request');
  monitor.activity('terminal-private', {request_id: 'terminal-request', status: 'running', events: [
    {id: 1001, kind: 'sandbox_started', title: 'Contenedor solicitado', payload: {container: 'real-container', command: ['docker', 'run', '--network=none', 'python']}},
    {id: 1002, kind: 'sandbox_output', payload: {container: 'real-container', stdout: 'PRIMERA SALIDA\n', stderr: ''}},
    {id: 1003, kind: 'sandbox_output', payload: {container: 'real-container', stdout: 'PRIMERA SALIDA\nSEGUNDA SALIDA\n', stderr: ''}}
  ]});
  assert(elements.terminalOutput.textContent.includes('$ docker run --network=none python'));
  assert.equal(elements.terminalOutput.textContent.split('PRIMERA SALIDA').length - 1, 1, 'Las muestras acumuladas deben anexar solo los bytes nuevos');
  assert(elements.terminalOutput.textContent.includes('SEGUNDA SALIDA'));
  monitor.activity('terminal-private', {request_id: 'terminal-request', status: 'complete', events: [
    {id: 1004, kind: 'sandbox_finished', payload: {container: 'real-container', status: 'complete', exit_code: 0, stdout: 'PRIMERA SALIDA\nSEGUNDA SALIDA\n', stderr: ''}}
  ]});
  const terminalJob = {id: 'terminal-download', status: 'running', message: 'Descargando', updated_at: '2026-10-19 18:00:00',
    progress: {phase: 'downloading', bytes: 512, total_bytes: 1024, percent: 50}, steps: [
      {kind: 'process_started', pid: 1234, command: ['python', 'worker.py']},
      {kind: 'operation', method: 'http', operation: 'GET', url: 'https://example.org/file'},
      {kind: 'console', method: 'yt-dlp', level: 'warning', message: '\x1b[31m<img onerror=alert(1)>\x1b[0m'}]};
  monitor.downloads('terminal-private', [terminalJob], []);
  assert(elements.terminalOutput.textContent.includes('PID 1234'));
  assert(elements.terminalOutput.textContent.includes('CALL http.GET'));
  assert(elements.terminalOutput.textContent.includes('<img onerror=alert(1)>'));
  assert(!elements.terminalOutput.textContent.includes('\x1b['), 'Los controles ANSI deben eliminarse');
  assert(elements.terminalOutput.textContent.includes('50.0 %'));
  const terminalEntryCount = elements.terminalOutput.children.length;
  monitor.downloads('terminal-private', [terminalJob], []);
  assert.equal(elements.terminalOutput.children.length, terminalEntryCount, 'Las consultas repetidas no deben duplicar entradas');
  assert.equal(elements.executionTerminal.dataset.state, 'running');
  elements.terminalToggle.click(); assert.equal(elements.terminalOutput.hidden, true);
  elements.terminalToggle.click(); assert.equal(elements.terminalOutput.hidden, false);
  elements.terminalFollow.click(); assert.equal(elements.terminalFollow.attributes['aria-pressed'], 'false');
  elements.terminalFollow.click();
  elements.terminalOutput.scrollHeight = 1200; elements.terminalOutput.clientHeight = 200;
  elements.terminalOutput.scrollTop = 300;
  elements.terminalOutput.dispatchEvent(new Event('scroll'));
  assert.equal(elements.terminalFollow.attributes['aria-pressed'], 'false', 'Leer líneas anteriores debe detener el seguimiento automático');
  monitor.downloads('terminal-private', [terminalJob], []);
  assert.equal(elements.terminalOutput.scrollTop, 300, 'La nueva actividad no debe mover la consola mientras se lee');
  elements.terminalOutput.scrollHeight = 200;
  monitor.connectionLost('terminal-private', 'downloads');
  assert(elements.terminalState.textContent.includes('interrumpida'));
  monitor.downloads('terminal-private', [{...terminalJob, status: 'queued', progress: {phase: 'waiting', retry_at: Date.now() / 1000 + 60}}], []);
  assert.equal(elements.executionTerminal.dataset.state, 'waiting');
  assert(elements.terminalConnection.textContent.includes('Próximo intento'));
  monitor.downloads('terminal-private', [{...terminalJob, status: 'failed', message: 'Acceso requerido'}], []);
  assert.equal(elements.executionTerminal.dataset.state, 'failed');
  monitor.downloads('terminal-private', [{...terminalJob, status: 'blocked', message: 'Objetivo pendiente: acceso autorizado requerido'}], []);
  assert.equal(elements.executionTerminal.dataset.state, 'blocked');
  assert(elements.workMonitorState.textContent.includes('Objetivo pendiente'));
  assert(elements.terminalState.textContent.includes('Requiere'));
  monitor.activity('terminal-private', {request_id: 'terminal-request', status: 'complete', events: Array.from({length: 350}, (_, i) => ({id: 2000 + i, kind: 'tool', title: 'Entrada acotada ' + i}))});
  assert.equal(elements.terminalOutput.children.length, 300, 'La consola debe limitar el historial en memoria');
  monitor.select('terminal-other');
  monitor.downloads('terminal-private', [terminalJob], []);
  assert.equal(elements.terminalOutput.textContent, '', 'Las respuestas atrasadas no deben restaurar registros de otra conversación');
  monitor.clear();
  downloadJobsOverride = [{id: 'no-source', status: 'failed', message: 'Límite del sitio', source_available: false}];
  await window.agentProjects.show('labels-private');
  assert(elements.projectFiles.textContent.includes('Reintentar descarga'));
  assert(!elements.projectFiles.textContent.includes('archivo conservado'));
  downloadJobsOverride[0].source_available = true;
  await window.agentProjects.refresh();
  assert(elements.projectFiles.textContent.includes('Reintentar con el archivo conservado'));
  downloadJobsOverride = [{id: 'paused', status: 'queued', message: 'Esperando para reintentar.', progress: {phase: 'waiting', retry_at: Date.now() / 1000 + 300}}];
  await window.agentProjects.refresh();
  assert(elements.projectFiles.textContent.includes('Próximo intento:'));
  assert(elements.projectFiles.textContent.includes('Espera restante:'));
  assert(!elements.projectFiles.children.some(row => row.children.some(child => child.tag === 'progress')), 'La cola no debe simular transferencia');
  assert(elements.projectFiles.textContent.includes('Cancelar descarga'));
  downloadJobsOverride = [{id: 'blocked-goal', status: 'blocked', message: 'Acceso autorizado requerido', objective: {status: 'blocked'}}];
  await window.agentProjects.refresh();
  assert(elements.projectFiles.textContent.includes('Reanudar tras resolver el requisito'));
  assert(elements.projectFiles.textContent.includes('Cancelar descarga'));
  window.agentProjects.clear();
  downloadJobsOverride = null;
  console.log('OK: JS real; equipo en segundo plano, recuperación, navegación, borradores, reintentos, cancelación, privacidad, descargas y caducidad.');
})().catch(error => {console.error(error); process.exitCode = 1;});
