/* Sincroniza los eventos de ejecuci?n con la consola del chat privado. */
(() => {
  'use strict';
  const cancel = document.getElementById('cancelAgentJob');
  if (!cancel) return;
  cancel.hidden = true;
  let ticket = 0, timer = null, controller = null, conversation = null, request = null, cursor = 0;
  let cancelAction = null;
  cancel.addEventListener('click', async () => {
    if (!cancelAction) return;
    cancel.disabled = true;
    try { await cancelAction(); } finally { cancel.disabled = false; }
  });

  function stop() {
    ticket += 1; clearTimeout(timer); controller?.abort(); controller = null;
  }
  function pre(text, parent) {
    const block = document.createElement('pre'); block.textContent = text; parent.append(block);
  }
  async function poll(version, follow) {
    if (version !== ticket || !conversation) return;
    const identifier = request ? `&request_id=${encodeURIComponent(request)}` : '';
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await window.projectApi(`/api/agent/conversations/${conversation}/activity?after=${cursor}${identifier}`, {signal: controller.signal});
      const data = await response.json();
      if (version !== ticket) return;
      if (!response.ok) throw new Error(data.detail || 'No se pudo consultar la ejecución.');
      request = data.request_id || request;
      window.agentMonitor?.activity(conversation, data);
      const events = data.events || [];
      for (const event of events) cursor = Math.max(cursor, event.id);
      if (events.some(event => event.payload?.name === 'project_write' || event.kind === 'completed')) void window.agentProjects?.refresh();
      if (['queued', 'running'].includes(data.status) || (follow && data.status === 'waiting')) timer = setTimeout(() => {void poll(version, true);}, 850);
    } catch (error) {
      if (version === ticket) {
        window.agentMonitor?.connectionLost(conversation);
        if (follow) timer = setTimeout(() => {void poll(version, true);}, 1500);
      }
    } finally { clearTimeout(timeout); }
  }
  window.agentActivity = {
    setJob(job, onCancel) {
      cancel.hidden = !job || !['queued', 'running'].includes(job.status);
      cancelAction = cancel.hidden ? null : onCancel;
      cancel.disabled = false;
    },
    start(id, requestId) {
      stop(); conversation = id; request = requestId; cursor = 0;
      window.agentMonitor?.start(id, requestId); void poll(ticket, true);
    },
    showLatest(id) {
      stop(); conversation = id; request = null; cursor = 0;
      window.agentMonitor?.select(id);
      return poll(ticket, false);
    },
    finish(id, requestId) {
      if (id !== conversation || requestId !== request) return;
      stop(); return poll(ticket, false);
    },
    hide() {stop(); window.agentActivity.setJob(null); window.agentMonitor?.clear();},
    clear() {stop(); conversation = null; request = null; cursor = 0; window.agentActivity.setJob(null); window.agentMonitor?.clear();}
  };

  async function loadLessons() {
    const response = await window.projectApi('/api/agent/lessons');
    const lessons = await response.json();
    if (!response.ok) throw new Error(lessons.detail || 'No se pudieron cargar las correcciones.');
    const list = document.getElementById('lessonsList'); list.replaceChildren();
    document.getElementById('lessonsStatus').textContent = lessons.length
      ? `${lessons.filter(item => item.status === 'observed').length} observadas · ${lessons.filter(item => item.status === 'confirmed').length} útiles para ti · ${lessons.filter(item => item.status === 'rejected').length} descartadas`
      : 'Todavía no hay correcciones observadas.';
    for (const lesson of lessons) {
      const item = document.createElement('details'); item.className = 'activity-entry';
      const summary = document.createElement('summary');
      const status = {observed: 'Observada', confirmed: 'Te resultó útil', rejected: 'Descartada'};
      summary.textContent = `${lesson.purpose} · ${status[lesson.status]}`; item.append(summary);
      pre('Error inicial:\n' + lesson.error, item);
      pre(lesson.code, item); pre('Salida posterior:\n' + lesson.output, item);
      const actions = document.createElement('div'); actions.className = 'lesson-actions';
      for (const [verdict, text] of [['helpful', 'Me sirvió'], ['incorrect', 'No es correcto']]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'scene-toggle'; button.textContent = text;
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            const result = await window.projectApi(`/api/agent/lessons/${lesson.id}/feedback`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({verdict})});
            if (!result.ok) throw new Error('No se pudo guardar tu valoración.');
            await loadLessons();
          } catch (error) {document.getElementById('lessonsStatus').textContent = error.message; button.disabled = false;}
        });
        actions.append(button);
      }
      item.append(actions); list.append(item);
    }
  }
  document.getElementById('openLessons')?.addEventListener('click', async () => {
    document.getElementById('lessonsDialog').showModal();
    try {await loadLessons();} catch (error) {document.getElementById('lessonsStatus').textContent = error.message;}
  });
  document.getElementById('closeLessons')?.addEventListener('click', () => document.getElementById('lessonsDialog').close());
  window.addEventListener('project-session-expired', () => {
    window.agentActivity.clear(); document.getElementById('lessonsList')?.replaceChildren(); document.getElementById('lessonsDialog')?.close();
  });
})();
