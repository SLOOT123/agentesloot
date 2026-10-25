/* Solo interfaz. El envío de mensajes se gestiona en app.js. */
(() => {
  'use strict';
  const input = document.getElementById('userInput');
  const sidebar = document.getElementById('chatSidebar');
  const main = document.getElementById('chatMain');
  const openButton = document.getElementById('openSidebar');
  const closeButton = document.getElementById('closeSidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  if (!input || !sidebar || !main || !openButton || !closeButton || !backdrop) return;

  const mobile = window.matchMedia('(max-width: 640px)');
  let isOpen = false;

  function resizeInput() {
    input.style.height = 'auto';
    const maxHeight = parseFloat(getComputedStyle(input).maxHeight) || 200;
    input.style.height = Math.min(input.scrollHeight, maxHeight) + 'px';
  }
  input.addEventListener('input', resizeInput);
  window.addEventListener('resize', resizeInput);
  resizeInput();

  function setMenu(open, restoreFocus = true) {
    isOpen = mobile.matches && open;
    sidebar.classList.toggle('sidebar-open', isOpen);
    openButton.setAttribute('aria-expanded', String(isOpen));
    backdrop.hidden = !isOpen;
    sidebar.inert = mobile.matches && !isOpen;
    main.inert = isOpen;
    if (isOpen) closeButton.focus();
    else if (restoreFocus && mobile.matches) openButton.focus();
  }
  openButton.addEventListener('click', () => setMenu(true));
  closeButton.addEventListener('click', () => setMenu(false));
  backdrop.addEventListener('click', () => setMenu(false));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && isOpen) setMenu(false);
  });
  sidebar.addEventListener('click', event => {
    if (event.target.closest('.history-item, .new-chat-btn') && isOpen) setMenu(false);
  });
  mobile.addEventListener('change', () => setMenu(false, false));
  setMenu(false, false);

  document.getElementById('chatBox').addEventListener('click', event => {
    const suggestion = event.target.closest('[data-prompt]');
    if (!suggestion) return;
    // La sugerencia se prepara para editarla; no se envía automáticamente.
    input.value = suggestion.dataset.prompt;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
  });
})();
