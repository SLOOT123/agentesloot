/* Conexiones con perspectiva 3D, sin librerías ni recursos externos. */
(() => {
  'use strict';
  const canvas = document.querySelector('.connection-canvas');
  const context = canvas?.getContext('2d');
  const video = document.getElementById('login-video');
  const toggle = document.getElementById('scene-toggle');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const isChat = document.body.classList.contains('chat-body');
  let width = 0, height = 0, frameId = 0, lastTime = null, angle = 0;
  let userPaused = false, tiltX = 0, tiltY = 0, targetX = 0, targetY = 0;

  // Puntos distribuidos sobre una esfera; las conexiones conservan su posición.
  const points = Array.from({ length: 44 }, (_, index) => {
    const y = 1 - (index / 43) * 2;
    const radius = Math.sqrt(1 - y * y);
    const longitude = index * Math.PI * (3 - Math.sqrt(5));
    return { x: Math.cos(longitude) * radius, y, z: Math.sin(longitude) * radius };
  });
  const links = [];
  for (let first = 0; first < points.length; first += 1) {
    for (let second = first + 1; second < points.length; second += 1) {
      const a = points[first], b = points[second];
      if (Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z) < .65) links.push([first, second]);
    }
  }

  function paused() {
    return userPaused || reducedMotion.matches || document.hidden;
  }

  function draw() {
    if (!context || !width || !height) return;
    context.clearRect(0, 0, width, height);
    const radius = Math.min(width * .56, height * .5, 480);
    const centerX = width * (isChat && width > 640 ? .63 : .5);
    const centerY = height * .49;
    const yaw = angle + tiltX;
    const pitch = .3 + tiltY;
    const cosine = Math.cos(yaw), sine = Math.sin(yaw);
    const pitchCosine = Math.cos(pitch), pitchSine = Math.sin(pitch);
    const projected = points.map(point => {
      const x = point.x * cosine + point.z * sine;
      const z = -point.x * sine + point.z * cosine;
      const y = point.y * pitchCosine - z * pitchSine;
      const depth = point.y * pitchSine + z * pitchCosine;
      const perspective = 1200 / (1200 + depth * radius);
      return { x: centerX + x * radius * perspective, y: centerY + y * radius * perspective, depth };
    });
    context.lineWidth = .8;
    for (const [first, second] of links) {
      const a = projected[first], b = projected[second];
      const opacity = .1 + (2 - a.depth - b.depth) * .045;
      context.strokeStyle = `rgba(75, 218, 243, ${opacity})`;
      context.beginPath();
      context.moveTo(a.x, a.y);
      context.lineTo(b.x, b.y);
      context.stroke();
    }
    for (const point of projected) {
      const size = 1.5 + (1 - point.depth) * .5;
      const glow = context.createRadialGradient(point.x, point.y, 0, point.x, point.y, size * 5);
      glow.addColorStop(0, '#6aeeff66');
      glow.addColorStop(1, '#6aeeff00');
      context.fillStyle = glow;
      context.beginPath();
      context.arc(point.x, point.y, size * 5, 0, Math.PI * 2);
      context.fill();
      context.fillStyle = `rgba(133, 244, 255, ${.4 + (1 - point.depth) * .2})`;
      context.beginPath();
      context.arc(point.x, point.y, size, 0, Math.PI * 2);
      context.fill();
    }
  }

  function animate(time) {
    frameId = 0;
    if (paused()) return;
    if (lastTime === null || time - lastTime >= 33) {
      const elapsed = lastTime === null ? 0 : Math.min(time - lastTime, 100);
      angle += elapsed * .000045;
      tiltX += (targetX - tiltX) * .035;
      tiltY += (targetY - tiltY) * .035;
      draw();
      lastTime = time;
    }
    frameId = window.requestAnimationFrame(animate);
  }

  function resize() {
    if (!context) return;
    width = window.innerWidth;
    height = window.innerHeight;
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    draw();
  }

  function updateMotion() {
    window.cancelAnimationFrame(frameId);
    frameId = 0;
    lastTime = null;
    const stopped = paused();
    document.documentElement.classList.toggle('scene-paused', stopped);
    if (toggle) {
      toggle.hidden = reducedMotion.matches;
      toggle.textContent = userPaused ? 'Reproducir fondo' : 'Pausar fondo';
      toggle.setAttribute('aria-pressed', String(userPaused));
    }
    if (video) {
      video.muted = true;
      if (stopped) video.pause();
      else video.play().catch(() => { /* El fondo conserva el logo y las conexiones. */ });
    }
    draw();
    if (!stopped && context) frameId = window.requestAnimationFrame(animate);
  }

  toggle?.addEventListener('click', () => {
    userPaused = !userPaused;
    updateMotion();
  });
  window.addEventListener('pointermove', event => {
    if (paused() || event.pointerType === 'touch') return;
    targetX = (event.clientX / window.innerWidth - .5) * .3;
    targetY = (event.clientY / window.innerHeight - .5) * .2;
  }, { passive: true });
  window.addEventListener('resize', resize, { passive: true });
  reducedMotion.addEventListener('change', updateMotion);
  document.addEventListener('visibilitychange', updateMotion);
  window.addEventListener('pagehide', () => {
    window.cancelAnimationFrame(frameId);
    video?.pause();
  });
  window.addEventListener('pageshow', updateMotion);
  resize();
  updateMotion();
})();
