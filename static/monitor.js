/* Monitor privado: consume acciones observadas, sin porcentajes inventados. */
(() => {
  'use strict';
  const get = id => document.getElementById(id);
  const panel = get('workMonitor'), dialog = get('workMonitorDialog');
  if (!panel || !dialog) return;
  const active = status => ['queued', 'running'].includes(status);
  const terminal = status => ['complete', 'failed', 'cancelled', 'blocked'].includes(status);
  const states = {queued: 'En cola', running: 'En curso', complete: 'Terminado', failed: 'Con errores', cancelled: 'Cancelado', waiting: 'Sin trabajo registrado', partial: 'Resultado parcial', blocked: 'Bloqueado'};
  const phases = {waiting: 'En espera', connecting: 'Conectando', analyzing: 'Analizando página', downloading: 'Descargando', converting: 'Convirtiendo audio', merging: 'Uniendo vídeo y audio', verifying: 'Comprobando archivo', complete: 'Archivo listo'};
  let conversation = null, request = null, status = 'waiting', stage = '', error = '', errorMessage = '', disconnected = false, downloadsDisconnected = false;
  let workers = new Map(), containers = new Map(), seen = new Set(), events = [], plan = null, jobs = [], files = [];
  let traceJob = null, promptLoadable = false, resultPending = false;
  const traceDialog = get('downloadTraceDialog');

  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  }
  function line(parent, text, className = 'field-help') {parent.append(node('p', text, className));}
  function retryText(job) {
    if (job.status !== 'queued' || !Number.isFinite(job.progress?.retry_at)) return '';
    const remaining = Math.max(0, Math.ceil(job.progress.retry_at - Date.now() / 1000));
    return `Próximo intento: ${new Date(job.progress.retry_at * 1000).toLocaleTimeString()}. ` +
      (remaining ? `Espera restante: ${Math.floor(remaining / 60)} min ${remaining % 60} s.` : 'Esperando confirmación del siguiente intento.');
  }
  function resetRun() {
    request = null; status = 'waiting'; stage = ''; error = ''; errorMessage = ''; disconnected = false;
    promptLoadable = false;
    resultPending = false;
    workers = new Map(); containers = new Map(); seen = new Set(); events = []; plan = null;
  }
  function select(id) {
    if (id === conversation) return;
    clear(); conversation = id; window.agentTerminal?.select(id);
  }
  function clear() {
    conversation = null; resetRun(); jobs = []; files = []; downloadsDisconnected = false;
    window.agentTerminal?.clear();
    panel.hidden = true; dialog.close(); traceDialog?.close(); traceJob = null;
    if (get('recoverPromptChat')) get('recoverPromptChat').hidden = true;
    get('downloadTraceSteps')?.replaceChildren(); get('downloadTraceState')?.replaceChildren();
    for (const id of ['workMonitorState', 'workMonitorHint', 'monitorCurrent', 'monitorGuide', 'monitorWorkers', 'monitorPlan', 'monitorContainers', 'monitorDownloads', 'monitorTimeline']) get(id).replaceChildren();
  }
  function ingest(event) {
    if (seen.has(event.id)) return;
    seen.add(event.id);
    const p = event.payload || {};
    if (event.kind !== 'sandbox_output') {
      stage = event.title;
      events.push(event);
      if (events.length > 10) events.shift();
    }
    if (event.kind === 'error') {error = p.code || ''; errorMessage = event.title || '';}
    if (event.kind === 'completed') resultPending = p.review === 'unresolved';
    if (p.task_graph || p.result?.task_graph) plan = p.task_graph || p.result.task_graph;
    if (event.kind === 'model') workers.set('model', {name: p.model || 'Modelo de conversación', status: 'running'});
    else if (workers.has('model') && ['tool', 'review', 'completed'].includes(event.kind)) workers.set('model', {...workers.get('model'), status: 'complete'});
    if (event.kind === 'worker_started' || event.kind === 'worker_completed' || event.kind === 'team_review') {
      const key = String(p.worker_id || p.worker || (event.kind === 'team_review' ? 'revisor' : 'especialista'));
      workers.set(key, {name: key, status: event.kind === 'worker_completed' ? p.status || 'complete' : 'running'});
    }
    if (['sandbox_started', 'sandbox_output', 'sandbox_finished'].includes(event.kind) && typeof p.container === 'string') {
      containers.set(p.container, {...containers.get(p.container), ...p,
        status: event.kind === 'sandbox_finished' ? p.status : event.kind === 'sandbox_started' ? 'starting' : 'output'});
      if (containers.size > 8) containers.delete(containers.keys().next().value);
    }
  }
  function effectiveState(state) {
    return active(state) && terminal(status) ? 'Cerrado al finalizar el trabajo' : states[state] || state;
  }
  function description() {
    const pending = jobs.filter(job => active(job.status));
    const failed = jobs.filter(job => job.status === 'failed');
    const blocked = jobs.find(job => job.status === 'blocked');
    if (disconnected || (pending.length && downloadsDisconnected)) return ['Actualización interrumpida', 'Se perdió la conexión con el registro. Se volverá a consultar; el estado mostrado puede estar atrasado.'];
    if (active(status)) return [`${states[status]} · ${stage || 'Preparando la petición'}`,
      status === 'queued' ? 'Espera un turno disponible. Puedes seguir escribiendo mientras el trabajo está en cola.' : 'Puedes seguir conversando. Abre el monitor para ver los pasos, los especialistas y la salida de los contenedores.'];
    const waiting = pending.find(job => job.progress?.phase === 'waiting');
    if (waiting && pending.every(job => job.status === 'queued')) return ['Descargas en espera', waiting.message + ' ' + retryText(waiting)];
    if (pending.length) return [`${pending.length} descarga${pending.length === 1 ? '' : 's'} en segundo plano · ${phases[pending[0].progress?.phase] || states[pending[0].status]}`,
      'La descarga sigue en su propio proceso. Cuando termine, abre Archivos y descargas para guardar el archivo.'];
    if (blocked) return ['Objetivo pendiente · requiere una acción', blocked.message || 'Revisa el diagnóstico y resuelve el requisito para reanudar el mismo objetivo.'];
    if (promptRecovered(conversation)) return ['El archivo de instrucciones ya se puede cargar',
      'El error pertenece al intento anterior. Reenvía tu mensaje para iniciar un trabajo nuevo.'];
    if (status === 'failed' && ['prompt_too_long', 'invalid_prompt'].includes(error)) return ['No se pudieron cargar las instrucciones',
      error === 'prompt_too_long' ? errorMessage || 'Este intento superó el antiguo límite de tamaño de instrucciones. Ese límite fijo ya se retiró; vuelve a enviar tu mensaje.'
      : 'No se pudieron cargar las instrucciones del perfil. Consulta la consola y la documentación del proyecto.'];
    if (status === 'failed' && error === 'execution_limit') return ['Se alcanzó el límite de pasos',
      'Este intento terminó antes de completar la tarea. Abre el monitor para revisar las acciones realizadas y los errores; después puedes enviar otra petición.'];
    if (status === 'failed' && error === 'context_limit') return ['La petición supera el contexto del modelo',
      'El tamaño total de las instrucciones, el mensaje y los datos supera el contexto disponible. Revisa ese total y la capacidad del modelo configurado.'];
    if (status === 'failed') return ['El trabajo se detuvo', error === 'run_timeout'
      ? 'Se agotó el tiempo de ejecución. Revisa el último paso y vuelve a pedir una tarea más concreta.'
      : 'Abre el monitor y revisa el último error. Los pasos anteriores quedan registrados en esta conversación.'];
    if (status === 'cancelled') return ['Trabajo cancelado', 'La ejecución del agente se detuvo. Las descargas tienen su propio botón para cancelarlas.'];
    if (failed.length) return ['Hay una descarga con errores', failed.at(-1).message || 'Abre Archivos y descargas para consultar el motivo y las opciones disponibles.'];
    if (status === 'complete' && resultPending) return ['Respuesta parcial: hay trabajo pendiente', 'El chat conserva el avance observado y los pasos que faltan. Puedes continuar con otra petición; no se considera verificada la tarea.'];
    if (status === 'complete') return ['Trabajo terminado', 'Lee el resultado en el chat. Puedes abrir el monitor para consultar los pasos y las comprobaciones realizadas.'];
    return ['Sin trabajo registrado', 'Envía una petición para seguir aquí sus acciones.'];
  }
  function render() {
    if (!conversation) return;
    panel.hidden = status === 'waiting' && !events.length && !jobs.length;
    const [title, hint] = description();
    for (const [id, text] of [['workMonitorState', title], ['workMonitorHint', hint], ['monitorCurrent', title], ['monitorGuide', hint]]) {
      if (get(id).textContent !== text) get(id).textContent = text;
    }
    panel.dataset.state = status;
    if (get('recoverPromptChat')) get('recoverPromptChat').hidden = !(status === 'failed' && !promptLoadable && ['prompt_too_long', 'invalid_prompt'].includes(error)
      && conversation === window.agentChat?.conversationId && window.agentChat?.canUseGeneral);

    const workerList = get('monitorWorkers'); workerList.replaceChildren();
    for (const worker of workers.values()) line(workerList, `${worker.name} · ${effectiveState(worker.status)}`);
    if (!workers.size) line(workerList, 'Este trabajo aún no ha registrado llamadas al modelo o especialistas.');
    if (active(status) && workers.size) line(workerList, 'Una solicitud activa puede estar esperando su turno de modelo.');

    const planView = get('monitorPlan'); planView.replaceChildren();
    if (plan?.nodes?.length) {
      const list = node('ol');
      for (const step of plan.nodes) {
        const state = {pending: 'Pendiente', observed: 'Resultado observado', failed: 'Falló'}[step.status] || step.status;
        list.append(node('li', `${step.title} · ${state}`));
      }
      planView.append(list);
      line(planView, `${plan.nodes.filter(step => step.status === 'observed').length} de ${plan.nodes.length} pasos con resultado observado.`);
    } else line(planView, 'Los pasos aparecerán cuando el agente registre un plan. Las peticiones simples pueden resolverse directamente.');

    const containerList = get('monitorContainers');
    // Conserva la posición de lectura al llegar una nueva muestra de consola.
    const positions = new Map(Array.from(containerList.children).map(child => [child.dataset.container,
      {top: child.lastElementChild?.scrollTop || 0, height: child.lastElementChild?.scrollHeight || 0, client: child.lastElementChild?.clientHeight || 0}]));
    containerList.replaceChildren();
    for (const c of containers.values()) {
      const card = node('article', undefined, 'monitor-container'); card.dataset.container = c.container;
      const state = c.status === 'starting' ? 'Inicio solicitado a Docker' : c.status === 'output' ? 'Salida recibida · esperando cierre' : states[c.status] || c.status;
      line(card, `${c.purpose === 'web_reader' ? 'Lector de páginas' : 'Ejecución de código'} · ${terminal(status) && ['starting', 'output'].includes(c.status) ? 'Sin cierre registrado' : state}`, 'monitor-container-title');
      line(card, c.container, 'monitor-container-name');
      line(card, `Límites: ${c.cpu_limit} CPU · ${c.memory_mb} MB · ${c.timeout_seconds} s. Red: ${c.network === 'none' ? 'desactivada' : 'habilitada para el lector'}.`);
      if (c.exit_code !== null && c.exit_code !== undefined) line(card, `Código de salida: ${c.exit_code}${c.error ? ' · ' + c.error : ''}`);
      const log = node('pre', [c.stdout, c.stderr ? '[stderr]\n' + c.stderr : ''].filter(Boolean).join('\n') || 'Todavía no hay salida de consola.', 'monitor-console');
      log.setAttribute('tabindex', '0'); log.setAttribute('aria-label', `Salida de ${c.container}`);
      card.append(log); containerList.append(card);
      const previous = positions.get(c.container);
      if (!previous || previous.top + previous.client >= previous.height - 12) log.scrollTop = log.scrollHeight;
      else log.scrollTop = previous.top;
    }
    if (!containers.size) line(containerList, 'No se ha solicitado ningún contenedor en este trabajo. Las respuestas de texto pueden usar solo el modelo local.');

    const downloadList = get('monitorDownloads'); downloadList.replaceChildren();
    for (const job of jobs.slice(0, 8)) {
      const row = node('div', undefined, 'monitor-download');
      line(row, `${job.progress?.title || 'Descarga solicitada'} · ${states[job.status] || job.status}`, 'monitor-container-title');
      line(row, job.message || phases[job.progress?.phase] || '');
      if (retryText(job)) line(row, retryText(job));
      const inspect = node('button', 'Ver herramientas y pasos', 'scene-toggle'); inspect.type = 'button';
      inspect.addEventListener('click', () => openDownload(job.id)); row.append(inspect);
      if (job.status === 'running' && job.progress?.phase !== 'waiting') {
        const bar = node('progress'); bar.max = 100;
        bar.setAttribute('aria-label', 'Progreso de descarga');
        // Solo la transferencia comunica un porcentaje medido. Convertir no tiene ETA fiable.
        if (job.progress?.phase === 'downloading' && Number.isFinite(job.progress?.percent)) {
          bar.value = Math.min(100, Math.max(0, job.progress.percent));
          line(row, `${bar.value.toFixed(1)} % transferido`);
        }
        row.append(bar);
      }
      downloadList.append(row);
    }
    if (!jobs.length) line(downloadList, files.length ? `${files.length} archivo(s) disponible(s) en Archivos y descargas.` : 'No hay descargas registradas en esta conversación.');
    line(downloadList, 'Las descargas se ejecutan en un proceso local independiente. Su progreso continúa aunque la respuesta del chat haya terminado.');
    renderTrace();

    const timeline = get('monitorTimeline'); timeline.replaceChildren();
    for (const event of events) {
      let time = '';
      if (event.created_at) {
        const date = new Date(event.created_at.replace(' ', 'T') + (/Z$|[+-]\d\d:\d\d$/.test(event.created_at) ? '' : 'Z'));
        if (!Number.isNaN(date.getTime())) time = date.toLocaleTimeString('es', {hour12: false}) + ' · ';
      }
      timeline.append(node('li', time + event.title));
    }
  }
  function renderTrace() {
    if (!traceJob || !conversation || !traceDialog) return;
    const job = jobs.find(item => item.id === traceJob);
    if (!job) {traceDialog.close(); traceJob = null; get('downloadTraceSteps').replaceChildren(); return;}
    get('downloadTraceState').textContent = `${states[job.status] || job.status} · ${job.message || ''} ${retryText(job)}`;
    const list = get('downloadTraceSteps'); list.replaceChildren();
    if (job.progress?.skill) {
      line(list, `Skill: ${job.progress.skill.id} · versión ${job.progress.skill.version}.`);
      for (const criterion of job.progress.skill.acceptance_criteria || []) line(list, `Criterio: ${criterion}`);
    }
    const process = job.progress?.process;
    if (process?.pid) {
      const processStates = {running: 'en ejecución', complete: 'terminado', failed: 'terminado con error', timeout: 'tiempo agotado', cancelled: 'cancelado', unknown_after_restart: 'sin cierre registrado antes del reinicio'};
      line(list, `Proceso local PID ${process.pid} · ${processStates[process.status] || 'estado desconocido'}.`);
      if (Number.isFinite(process.exit_code)) line(list, `Código de salida: ${process.exit_code}.`);
    }
    const policy = job.progress?.retry_policy;
    if (policy) line(list, `Espera exponencial: base ${policy.base_seconds} s, tope ${policy.cap_seconds} s, respetando Retry-After. Reintentos usados: ${job.progress.rate_limit_retries || 0}${policy.persistent_recovery ? ' · recuperación persistente hasta completar o cancelar' : ' de ' + policy.max_auto_retries}.`);
    for (const [index, step] of (job.steps || []).entries()) {
      const card = node('article', undefined, 'download-trace-step');
      card.dataset.state = step.kind === 'attempt_failed' ? 'failed' : 'observed';
      const labels = {attempt: 'Intento', operation: 'Herramienta', discovery: 'Página analizada', attempt_failed: 'Intento fallido', metadata: 'Archivo identificado', retry: 'Ejecución retomada', rate_limit_wait: 'Espera antes del reintento', recovery_wait: 'Recuperación programada', strategy: 'Estrategia elegida', console: 'Salida de herramienta', verified: 'Archivo comprobado', process_started: 'Proceso iniciado', process_finished: 'Proceso finalizado'};
      card.append(node('h3', `${index + 1}. ${labels[step.kind] || 'Paso'} · ${step.method || 'descargador'}`));
      if (step.message) line(card, step.message);
      if (step.next_action && step.next_action !== step.message) line(card, step.next_action);
      if (Array.isArray(step.tools)) for (const tool of step.tools) line(card, `${tool.name}: ${tool.available ? 'disponible' : 'no disponible'}${tool.version ? ' · ' + tool.version : ''}`);
      if (Number.isInteger(step.pid)) line(card, `PID: ${step.pid}`);
      if (Number.isFinite(step.exit_code)) line(card, `Código de salida: ${step.exit_code} · ${step.elapsed_seconds || 0} s.`);
      if (Number.isFinite(step.retry_at)) line(card, `Reintento programado: ${new Date(step.retry_at * 1000).toLocaleTimeString()}.`);
      if (step.url) line(card, step.url, 'monitor-container-name');
      if (step.operation) line(card, `${step.method || ''}.${step.operation}`, 'monitor-container-name');
      if (Array.isArray(step.command)) card.append(node('pre', step.command.map(arg => /[\s"']/.test(String(arg)) ? JSON.stringify(arg) : String(arg)).join(' '), 'monitor-console'));
      if (Number.isFinite(step.count)) line(card, `${step.count} enlace(s) compatible(s) encontrado(s).`);
      if (Array.isArray(step.links)) for (const url of step.links) line(card, url, 'monitor-container-name');
      list.append(card);
    }
    if (!job.steps?.length) line(list, active(job.status) ? 'Esperando el primer paso del descargador…' : 'Esta descarga anterior no tiene pasos detallados conservados.');
    if (job.download?.id) {
      const link = node('a', `Guardar ${job.download.filename}`, 'scene-toggle');
      link.href = `/api/agent/conversations/${conversation}/downloads/${encodeURIComponent(job.download.id)}`;
      link.setAttribute('download', job.download.filename); list.append(link);
    }
  }
  function openDownload(id) {
    if (!conversation || !jobs.some(job => job.id === id) || !traceDialog) return;
    traceJob = id; renderTrace();
    if (!traceDialog.open) traceDialog.showModal();
  }
  function promptRecovered(id) {
    return id === conversation && status === 'failed' && ['prompt_too_long', 'invalid_prompt'].includes(error) && promptLoadable;
  }
  window.agentMonitor = {
    select,
    start(id, requestId) {select(id); resetRun(); request = requestId; status = 'queued'; window.agentTerminal?.start(id, requestId); render();},
    activity(id, data) {
      if (id !== conversation) return;
      if (data.request_id && request && data.request_id !== request) resetRun();
      request = data.request_id || request;
      status = data.status; error = data.error_code || error; disconnected = false;
      promptLoadable = data.prompt_configuration?.loadable === true;
      window.agentTerminal?.activity(id, data);
      (data.events || []).forEach(ingest); render();
    },
    downloads(id, incoming, completed) {if (id === conversation) {jobs = incoming; files = completed; downloadsDisconnected = false; window.agentTerminal?.downloads(id, incoming); render();}},
    connectionLost(id, source = 'activity') {if (id === conversation) {if (source === 'downloads') downloadsDisconnected = true; else disconnected = true; window.agentTerminal?.connectionLost(id, source); render();}},
    clear, openDownload, promptRecovered
  };
  get('openWorkMonitor').addEventListener('click', () => {render(); dialog.showModal();});
  get('closeWorkMonitor').addEventListener('click', () => dialog.close());
  get('recoverPromptChat')?.addEventListener('click', async event => {
    event.target.disabled = true;
    try {await window.agentChat?.useGeneralProfile();} finally {event.target.disabled = false;}
  });
  get('closeDownloadTrace')?.addEventListener('click', () => traceDialog.close());
  traceDialog?.addEventListener('close', () => {if (!traceDialog.open) traceJob = null;});
  window.addEventListener('project-session-expired', clear);
})();
