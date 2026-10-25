/* Archivos privados: texto y ZIP; nunca ejecuta el HTML generado en el navegador. */
(() => {
  'use strict';
  const panel = document.getElementById('projectPanel');
  const list = document.getElementById('projectFiles');
  const status = document.getElementById('projectStatus');
  const download = document.getElementById('downloadProject');
  const form = document.getElementById('mediaDownloadForm');
  const mediaUrl = document.getElementById('mediaDownloadUrl');
  const mediaMode = document.getElementById('mediaDownloadMode');
  const mediaSubmit = document.getElementById('mediaDownloadSubmit');
  const mediaStatus = document.getElementById('mediaDownloadStatus');
  if (!panel || !list || !status || !download) return;
  let conversation = null, version = 0, controller = null, poll = null;
  let submission = null;
  let noticeSignature = '', tracking = false;

  function clear() {
    ++version; controller?.abort(); controller = null; conversation = null;
    clearTimeout(poll); poll = null;
    panel.hidden = true; list.replaceChildren(); status.textContent = '';
    download.hidden = true; download.removeAttribute('href');
    submission = null;
    noticeSignature = ''; tracking = false;
    if (mediaUrl) mediaUrl.value = '';
    if (mediaStatus) mediaStatus.textContent = '';
    if (mediaSubmit) mediaSubmit.disabled = false;
  }
  form?.addEventListener('submit', async event => {
    event.preventDefault();
    if (!conversation || submission) return;
    const id = conversation, token = {};
    submission = token; mediaSubmit.disabled = true; mediaStatus.textContent = 'Preparando descarga…';
    try {
      const response = await window.projectApi(`/api/agent/conversations/${id}/download-jobs`, {
        method: 'POST', body: JSON.stringify({url: mediaUrl.value.trim(), mode: mediaMode.value})
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'No se pudo iniciar la descarga.');
      if (conversation !== id || submission !== token) return;
      mediaStatus.textContent = result.status === 'complete' ? 'El archivo ya está disponible abajo.' : 'Descarga iniciada. Puedes seguir conversando mientras termina.';
      mediaUrl.value = ''; panel.querySelector('details')?.setAttribute('open', '');
      await load();
    } catch (error) {
      if (conversation === id && submission === token) mediaStatus.textContent = error.message;
    } finally {
      if (submission === token) {submission = null; mediaSubmit.disabled = false;}
    }
  });
  async function load() {
    if (!conversation) return;
    clearTimeout(poll); poll = null;
    const ticket = ++version, id = conversation;
    controller?.abort(); controller = new AbortController();
    const active = controller;
    const timer = setTimeout(() => active.abort(), 8000);
    try {
      const response = await window.projectApi(`/api/agent/conversations/${id}/project`, {signal: active.signal});
      const data = await response.json();
      if (version !== ticket || conversation !== id) return;
      if (!response.ok) throw new Error(data.detail || 'No se pudieron cargar los archivos.');
      window.agentMonitor?.downloads(id, data.jobs || [], data.downloads || []);
      const downloads = data.downloads || [];
      const jobs = (data.jobs || []).filter(job => job.status !== 'complete');
      const pending = jobs.filter(job => ['queued', 'running'].includes(job.status));
      tracking = pending.length > 0;
      const notices = data.notices || [];
      const signature = notices.map(item => item.notification_id).join(',');
      if (signature && signature !== noticeSignature) {
        noticeSignature = signature;
        if (mediaStatus) mediaStatus.textContent = notices.at(-1).content;
        panel.querySelector('details')?.setAttribute('open', '');
        void window.agentChat?.refreshDownloadNotices(id);
      }
      list.replaceChildren(); panel.hidden = false;
      status.textContent = `${data.files.length} archivos de proyecto · ${downloads.length} descargas completadas · ${pending.length} en proceso.`;
      if (pending.length || jobs.some(job => ['failed', 'blocked'].includes(job.status))) panel.querySelector('details')?.setAttribute('open', '');
      download.hidden = !data.files.length;
      download.href = `/api/agent/conversations/${id}/project.zip`;
      download.setAttribute('download', `proyecto-${id}.zip`);
      for (const job of jobs) {
        const item = document.createElement('div'); item.className = 'activity-entry download-job';
        const name = document.createElement('p'); name.textContent = job.progress?.title || 'Descarga solicitada';
        const detail = document.createElement('p'); detail.textContent = job.message;
        if (job.status === 'queued' && Number.isFinite(job.progress?.retry_at)) {
          detail.textContent += ` Próximo intento: ${new Date(job.progress.retry_at * 1000).toLocaleTimeString()}.`;
          const seconds = Math.max(0, Math.ceil(job.progress.retry_at - Date.now() / 1000));
          detail.textContent += seconds ? ` Espera restante: ${Math.floor(seconds / 60)} min ${seconds % 60} s.` : ' Esperando confirmación del siguiente intento.';
        }
        item.append(name, detail);
        const inspect = document.createElement('button'); inspect.type = 'button'; inspect.className = 'scene-toggle';
        inspect.textContent = 'Ver herramientas y pasos';
        inspect.addEventListener('click', () => window.agentMonitor?.openDownload(job.id)); item.append(inspect);
        if (job.status === 'running' && job.progress?.phase !== 'waiting') {
          const progress = document.createElement('progress'); progress.className = 'download-progress'; progress.max = 100;
          progress.setAttribute('aria-label', job.message);
          if (Number.isFinite(job.progress?.percent) && job.progress?.phase === 'downloading') {
            progress.value = Math.max(0, Math.min(100, job.progress.percent));
            detail.textContent += ` ${progress.value.toFixed(1)} %`;
          }
          item.append(progress);
        }
        const actions = job.status === 'blocked' ? ['retry', 'cancel'] : job.status === 'failed' ? ['retry'] : ['queued', 'running'].includes(job.status) ? ['cancel'] : [];
        for (const action of actions) {
          const button = document.createElement('button'); button.className = 'scene-toggle'; button.type = 'button';
          button.textContent = action === 'retry' ? (job.status === 'blocked' ? 'Reanudar tras resolver el requisito' : job.source_available ? 'Reintentar con el archivo conservado' : 'Reintentar descarga') : 'Cancelar descarga';
          button.addEventListener('click', async () => {
            button.disabled = true;
            try {
              const response = await window.projectApi(`/api/agent/conversations/${id}/download-jobs/${encodeURIComponent(job.id)}/${action}`, {method: 'POST'});
              const result = await response.json();
              if (!response.ok) throw new Error(result.detail || 'No se pudo actualizar la descarga.');
              if (conversation === id && version === ticket) await load();
            } catch (error) {if (conversation === id) {status.textContent = error.message; button.disabled = false;}}
          });
          item.append(button);
        }
        list.append(item);
      }
      for (const file of downloads) {
        const item = document.createElement('div'); item.className = 'activity-entry';
        const link = document.createElement('a'); link.className = 'scene-toggle';
        link.href = `/api/agent/conversations/${id}/downloads/${encodeURIComponent(file.id)}`;
        link.textContent = `Descargar ${file.filename} · ${file.bytes} bytes`;
        link.setAttribute('download', file.filename);
        item.append(link);
        const job = (data.jobs || []).find(job => job.download?.id === file.id);
        if (job) {
          const inspect = document.createElement('button'); inspect.type = 'button'; inspect.className = 'scene-toggle';
          inspect.textContent = 'Ver herramientas y pasos'; inspect.addEventListener('click', () => window.agentMonitor?.openDownload(job.id)); item.append(inspect);
        }
        list.append(item);
      }
      for (const file of data.files) {
        const item = document.createElement('details'); item.className = 'activity-entry';
        const summary = document.createElement('summary'); summary.textContent = `${file.path} · ${file.bytes} bytes`;
        const text = document.createElement('pre'); text.textContent = file.content;
        item.append(summary, text); list.append(item);
      }
      if (pending.length && conversation === id && version === ticket) poll = setTimeout(() => void load(), 1500);
    } catch (error) {
      if (version === ticket && conversation === id) window.agentMonitor?.connectionLost(id, 'downloads');
      if (version === ticket && error.name !== 'AbortError') {panel.hidden = false; status.textContent = error.message;}
      if (tracking && conversation === id && version === ticket) poll = setTimeout(() => void load(), 3000);
    } finally {clearTimeout(timer);}
  }
  window.agentProjects = {
    show(id) {clear(); conversation = id; return load();},
    refresh() {return load();},
    clear
  };
  window.addEventListener('project-session-expired', clear);
})();
