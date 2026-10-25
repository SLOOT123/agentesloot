/* Consola observada: recibe el registro privado existente, sin ejecutar texto del navegador. */
(() => {
  'use strict';
  const get = id => document.getElementById(id);
  const panel = get('executionTerminal'), output = get('terminalOutput');
  if (!panel || !output) return;
  let conversation = null, request = null, runState = 'waiting', follow = true, expanded = true, lastObserved = null, objectivePending = false;
  let rows = [], seen = new Set(), streams = new Map(), downloads = new Map(), connected = {activity: true, downloads: true};
  const live = state => ['queued', 'running'].includes(state);
  const labels = {queued: 'En cola', running: 'En ejecución', complete: 'Finalizado', failed: 'Con errores', cancelled: 'Cancelado', waiting: 'En espera', blocked: 'Requiere una acción', partial: 'Objetivo pendiente'};
  const clean = value => String(value ?? '').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');
  const commandText = command => command.map(arg => /[\s"']/.test(String(arg)) ? JSON.stringify(String(arg)) : String(arg)).join(' ');
  function remember(key) {
    if (seen.has(key)) return false;
    seen.add(key);
    if (seen.size > 3000) seen.delete(seen.values().next().value);
    return true;
  }
  function timeLabel(value) {
    if (!value) return new Date().toLocaleTimeString('es', {hour12: false});
    const date = new Date(value.replace(' ', 'T') + (/Z$|[+-]\d\d:\d\d$/.test(value) ? '' : 'Z'));
    return Number.isNaN(date.getTime()) ? '--:--:--' : date.toLocaleTimeString('es', {hour12: false});
  }
  function append(key, text, level = 'info', at) {
    if (!text || !remember(key)) return;
    const value = clean(text);
    rows.push({text: value.slice(0, 5000) + (value.length > 5000 ? '\n[Salida recortada en esta vista]' : ''), level, time: timeLabel(at)});
    if (rows.length > 300) rows.shift();
  }
  function stream(key, value, label, at) {
    if (typeof value !== 'string') return;
    const previous = streams.get(key) || '';
    if (value !== previous) {
      const delta = value.startsWith(previous) ? value.slice(previous.length) : value;
      if (delta.trim()) append(`stream:${key}:${value.length}:${value.slice(-120)}`, `${label}\n${delta.trimEnd()}`, key.endsWith(':stderr') ? 'warning' : 'info', at);
      streams.set(key, value);
      if (streams.size > 32) streams.delete(streams.keys().next().value);
    }
  }
  function step(jobId, item) {
    const prefix = `[descarga ${jobId.slice(0, 8)}]`;
    const identity = `step:${jobId}:${JSON.stringify(item)}`;
    const at = item.created_at;
    if (Array.isArray(item.command)) append(identity + ':cmd', `${prefix} $ ${commandText(item.command)}`, 'command', at);
    if (item.operation) append(identity + ':call', `${prefix} CALL ${item.method || 'herramienta'}.${item.operation}${item.url ? ' ' + item.url : ''}`, 'command', at);
    if (item.kind === 'process_started') append(identity, `${prefix} Proceso iniciado · PID ${item.pid}`, 'success', at);
    else if (item.kind === 'process_finished') append(identity, `${prefix} Proceso cerrado · PID ${item.pid} · salida ${item.exit_code ?? 'sin código'} · ${item.elapsed_seconds ?? '?'} s`, item.status === 'complete' ? 'success' : 'warning', at);
    else if (item.kind === 'console') append(identity, `${prefix} ${item.method || 'salida'}: ${item.message || ''}`, item.level === 'error' ? 'error' : item.level === 'warning' ? 'warning' : 'info', at);
    else {
      const title = item.message || (item.kind === 'attempt' ? `Intento con ${item.method}${item.url ? ' · ' + item.url : ''}` : item.kind);
      append(identity, `${prefix} ${title}`, item.kind === 'attempt_failed' ? 'error' : item.kind === 'verified' ? 'success' : ['rate_limit_wait', 'recovery_wait', 'objective_blocked', 'recovery_plan'].includes(item.kind) ? 'warning' : 'info', at);
    }
    if (item.detail && item.detail !== item.message) append(identity + ':detail', `${prefix} ${item.detail}`, 'warning', at);
  }
  function render() {
    const top = output.scrollTop;
    output.replaceChildren();
    for (const row of rows) {
      const line = document.createElement('div'); line.className = 'terminal-line'; line.dataset.level = row.level;
      const at = document.createElement('span'); at.className = 'terminal-time'; at.textContent = row.time;
      const text = document.createElement('span'); text.textContent = row.text;
      line.append(at, text); output.append(line);
    }
    if (!rows.length) {
      const empty = document.createElement('p'); empty.className = 'terminal-empty';
      empty.textContent = 'Los comandos, la salida y los resultados aparecerán aquí al iniciar una tarea.'; output.append(empty);
    }
    if (follow) output.scrollTop = output.scrollHeight;
    else output.scrollTop = top;
    const pending = [...downloads.values()].filter(job => live(job.status));
    const running = pending.some(job => job.status === 'running');
    const latestDownload = downloads.values().next().value;
    const state = !connected.activity || pending.length && !connected.downloads ? 'disconnected' : running || runState === 'running' ? 'running' : pending.length ? 'waiting' : runState === 'queued' ? 'queued' : latestDownload?.status || (objectivePending ? 'partial' : runState);
    panel.dataset.state = state;
    get('terminalState').textContent = state === 'disconnected' ? 'Conexión interrumpida' : labels[state] || state;
    get('terminalContext').textContent = conversation ? `sesión / ${conversation.slice(0, 8)}${pending.length ? ' · ' + pending.length + ' descarga(s)' : ''}` : 'Procesos de esta conversación';
    const waiting = pending.find(job => Number.isFinite(job.progress?.retry_at));
    let hint = !connected.activity || pending.length && !connected.downloads ? 'Reconectando · el último estado puede estar atrasado' : state === 'running' ? 'Recibiendo actividad del servidor' : state === 'waiting' ? 'Esperando el siguiente paso' : 'Último estado registrado';
    if (waiting && connected.downloads) {
      const seconds = Math.max(0, Math.ceil(waiting.progress.retry_at - Date.now() / 1000));
      hint = seconds ? `Próximo intento en ${Math.floor(seconds / 60)} min ${seconds % 60} s` : 'Esperando confirmación del siguiente intento';
    }
    get('terminalConnection').textContent = hint + (lastObserved ? ' · actualizado ' + lastObserved.toLocaleTimeString('es', {hour12: false}) : '');
    get('terminalCount').textContent = `${rows.length} entradas · máx. 300`;
    output.hidden = !expanded;
  }
  function clear() {
    conversation = null; request = null; runState = 'waiting'; lastObserved = null; objectivePending = false; rows = []; seen = new Set(); streams = new Map(); downloads = new Map(); connected = {activity: true, downloads: true};
    output.replaceChildren(); get('terminalState').textContent = 'Sin ejecución'; get('terminalContext').textContent = 'Procesos de esta conversación';
    get('terminalConnection').textContent = 'Esperando actividad'; get('terminalCount').textContent = '0 entradas'; panel.dataset.state = 'waiting';
  }
  window.agentTerminal = {
    select(id) {if (id !== conversation) {clear(); conversation = id;}},
    start(id, requestId) {if (id !== conversation) this.select(id); request = requestId; runState = 'queued'; objectivePending = false; connected.activity = true; expanded = true; get('terminalToggle').textContent = 'Ocultar'; get('terminalToggle').setAttribute('aria-expanded', 'true'); append('run:' + requestId, `[interfaz] Preparando petición · ${requestId}`, 'info'); render();},
    activity(id, data) {
      if (id !== conversation) return;
      request = data.request_id || request; runState = data.status; connected.activity = true; lastObserved = new Date();
      for (const event of data.events || []) {
        const p = event.payload || {}, key = 'event:' + event.id, at = event.created_at;
        if (event.kind === 'completed') objectivePending = p.review === 'unresolved' || p.objective_status === 'pending';
        if (event.kind === 'download') continue; // El registro del trabajo evita duplicar su salida.
        if (['sandbox_output', 'sandbox_finished'].includes(event.kind)) {
          stream(p.container + ':stdout', p.stdout, `[${p.container}] stdout`, at);
          stream(p.container + ':stderr', p.stderr, `[${p.container}] stderr`, at);
        }
        if (event.kind === 'observation' && p.result) {
          stream(key + ':stdout', p.result.stdout, '[herramienta] stdout', at);
          stream(key + ':stderr', p.result.stderr, '[herramienta] stderr', at);
          if (p.result.message) append(key + ':message', p.result.message, p.result.ok === false ? 'error' : 'info', at);
        }
        if (event.kind === 'sandbox_started' && Array.isArray(p.command)) append(key + ':cmd', '$ ' + commandText(p.command), 'command', at);
        if (event.kind === 'sandbox_finished') append(key, `[${p.container}] Cierre · salida ${p.exit_code ?? 'sin código'}${p.error ? ' · ' + p.error : ''}`, p.status === 'complete' ? 'success' : 'error', at);
        else if (event.kind === 'script' && typeof p.code === 'string') append(key, `[script ${p.language || ''}]\n${p.code}`, 'command', at);
        else if (event.kind !== 'sandbox_output') {
          let text = `[agente] ${event.title || event.kind}`;
          if (p.container) text += ' · ' + p.container;
          if (typeof p.worker === 'string') text += ' · ' + p.worker;
          if (event.kind === 'team_review' && typeof p.verdict === 'string') text += ' · ' + p.verdict;
          if (event.kind === 'retrieval') text += ` · ${p.method} · ${p.returned ?? 0}/${p.candidates ?? 0} fuentes`;
          if (event.kind === 'tool_discovery' && Array.isArray(p.tools)) text += '\n' + p.tools.map(tool => `${tool.name}: ${tool.available ? 'disponible' : 'no disponible'}`).join(' · ');
          append(key, text, event.kind === 'error' ? 'error' : 'info', at);
        }
      }
      render();
    },
    downloads(id, jobs) {
      if (id !== conversation) return;
      connected.downloads = true; lastObserved = new Date();
      downloads = new Map(jobs.filter(job => job.id).map(job => [job.id, job]));
      for (const job of [...downloads.values()].reverse()) {
        for (const item of job.steps || []) step(job.id, item);
        const p = job.progress || {}, prefix = `[descarga ${job.id.slice(0, 8)}]`;
        append(`job:${job.id}:${job.status}:${p.phase}:${job.message}`, `${prefix} ${labels[job.status] || job.status} · ${job.message || ''}`, job.status === 'failed' ? 'error' : job.status === 'complete' ? 'success' : job.status === 'blocked' || p.phase === 'waiting' ? 'warning' : 'info', job.updated_at);
        if (job.status === 'running' && p.phase === 'downloading' && (Number.isFinite(p.bytes) || Number.isFinite(p.percent))) {
          let text = `${prefix} Transferencia: ${Number.isFinite(p.percent) ? p.percent.toFixed(1) + ' % · ' : ''}${p.bytes ?? '?'} / ${p.total_bytes || '?'} bytes`;
          if (Number.isFinite(p.speed)) text += ` · ${Math.round(p.speed)} bytes/s`;
          if (Number.isFinite(p.eta)) text += ` · ETA ${Math.round(p.eta)} s`;
          append(`progress:${job.id}:${p.bytes}:${p.percent}`, text, 'info', job.updated_at);
        }
        if (job.download?.sha256) append(`verified:${job.id}:${job.download.sha256}`, `${prefix} SHA-256 ${job.download.sha256}\nArchivo: ${job.download.filename} · ${job.download.bytes} bytes`, 'success', job.updated_at);
      }
      render();
    },
    connectionLost(id, source) {if (id === conversation) {connected[source] = false; render();}},
    clear
  };
  get('terminalFollow').addEventListener('click', () => {follow = !follow; get('terminalFollow').setAttribute('aria-pressed', String(follow)); render();});
  output.addEventListener('scroll', () => {
    if (follow && output.scrollHeight - output.scrollTop - output.clientHeight > 24) {
      follow = false;
      get('terminalFollow').setAttribute('aria-pressed', 'false');
    }
  });
  get('terminalToggle').addEventListener('click', () => {expanded = !expanded; get('terminalToggle').textContent = expanded ? 'Ocultar' : 'Mostrar'; get('terminalToggle').setAttribute('aria-expanded', String(expanded)); render();});
  window.addEventListener('project-session-expired', clear);
})();
