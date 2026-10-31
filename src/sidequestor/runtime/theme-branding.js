/* Optional presentation module: remove its script/link and API route to disable. */
(function (root) {
  'use strict';
  function assetUrl(value) {
    if (typeof value !== 'string' || !value.startsWith('/theme/assets/')) return null;
    try {
      const parts = value.slice('/theme/assets/'.length).split('/').map(decodeURIComponent);
      if (parts.some(x => !x || x.startsWith('.') || /[\\/%?#:\u0000]/.test(x))) return null;
      return /\.(png|jpe?g|gif|webp|avif)$/i.test(parts.at(-1)) ? value : null;
    } catch (_) { return null; }
  }
  function nextCarPosition(cars, index, direction, gap, viewport) {
    const others = cars.filter((_, i) => i !== index);
    return direction === 'left'
      ? Math.max(viewport, ...others.map(c => c.x + c.width)) + gap
      : Math.min(0, ...others.map(c => c.x)) - cars[index].width - gap;
  }
  function chooseImage(images, random = Math.random) {
    return images[Math.min(images.length - 1, Math.floor(random() * images.length))];
  }
  const api = {assetUrl, nextCarPosition, chooseImage, setMoving: () => {}};
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.SidequestorBranding = api;
  if (!root.document) return;
  const document = root.document;
  const stream = document.querySelector('#motion-stream');
  const logo = document.querySelector('.mark img');
  if (!stream || !logo) return;
  const originalLogo = logo.getAttribute('src');
  const originalTrain = stream.innerHTML;
  let moving = stream.classList.contains('active'), cars = [], frame = null, previous = null;
  let config = null, failedImages = new Set(), resizeObserver = null, observedWidth = null;
  const reduced = root.matchMedia('(prefers-reduced-motion: reduce)');
  function restoreTrain() {
    if (frame !== null) root.cancelAnimationFrame(frame);
    frame = null; previous = null; cars = []; config = null;
    resizeObserver?.disconnect(); resizeObserver = null; observedWidth = null;
    stream.classList.remove('sq-activity-train');
    stream.innerHTML = originalTrain;
  }
  function availableImages() { return config.images.filter(url => !failedImages.has(url)); }
  function setImage(car) {
    const images = availableImages();
    if (!images.length) { restoreTrain(); return; }
    car.img.src = chooseImage(images);
  }
  function reposition() {
    if (!config) return;
    let x = 0;
    for (const car of cars) { car.x = x; x += car.width + config.gap; }
    paint();
  }
  function paint() { for (const car of cars) car.img.style.transform = `translateX(${car.x}px)`; }
  function shouldMove() { return config && moving && !document.hidden && !reduced.matches; }
  function sync() {
    if (shouldMove() && frame === null) { previous = null; frame = root.requestAnimationFrame(tick); }
    else if (!shouldMove() && frame !== null) { root.cancelAnimationFrame(frame); frame = null; previous = null; }
  }
  function tick(now) {
    frame = null;
    if (!shouldMove()) return;
    const seconds = previous === null ? 0 : Math.min(.05, (now - previous) / 1000);
    previous = now;
    const width = stream.clientWidth;
    for (let i = 0; i < cars.length; i++) {
      const car = cars[i];
      car.x += config.speed * seconds * (config.direction === 'left' ? -1 : 1);
      if ((config.direction === 'left' && car.x + car.width < 0)
          || (config.direction === 'right' && car.x > width)) {
        setImage(car);
        if (!config) return;
        car.x = nextCarPosition(cars, i, config.direction, config.gap, width);
      }
    }
    paint();
    frame = root.requestAnimationFrame(tick);
  }
  api.setMoving = value => { moving = Boolean(value); sync(); };
  function configure(activity) {
    const images = Array.isArray(activity?.images) ? activity.images.map(assetUrl).filter(Boolean).slice(0, 32) : [];
    if (!images.length) return;
    const number = (value, fallback, min, max) => Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : fallback;
    config = {images, height: number(activity.height, 28, 12, 96),
      gap: number(activity.gap, 16, 0, 96), speed: number(activity.speed, 32, 4, 160),
      direction: activity.direction === 'left' ? 'left' : 'right'};
    stream.replaceChildren();
    stream.classList.add('sq-activity-train');
    stream.style.setProperty('--sq-activity-height', `${config.height}px`);
    resizeCars();
    if (root.ResizeObserver) {
      resizeObserver = new root.ResizeObserver(() => {
        if (config && stream.clientWidth !== observedWidth) resizeCars();
      });
      resizeObserver.observe(stream);
    }
    paint(); sync();
  }
  function addCar() {
    const img = document.createElement('img');
    img.alt = ''; img.setAttribute('aria-hidden', 'true'); img.draggable = false;
    const car = {img, width: config.height, x: 0};
    cars.push(car);
    img.addEventListener('load', () => {
      if (!config || !cars.includes(car)) return;
      const oldWidth = car.width;
      car.width = Math.max(1, img.naturalWidth / Math.max(1, img.naturalHeight) * config.height);
      img.style.width = `${car.width}px`;
      if (!car.loaded) { car.loaded = true; reposition(); }
      else { if (config.direction === 'right') car.x -= car.width - oldWidth; paint(); }
    });
    img.addEventListener('error', () => {
      if (!config || !cars.includes(car)) return;
      failedImages.add(img.getAttribute('src'));
      setImage(car);
    });
    stream.append(img); setImage(car);
  }
  function resizeCars() {
    if (!config) return;
    observedWidth = stream.clientWidth;
    const average = cars.length ? cars.reduce((sum, car) => sum + car.width, 0) / cars.length : config.height;
    // Keep a full strip after narrow/wide layout changes, with a bounded DOM.
    const count = Math.max(4, Math.min(24, Math.ceil(observedWidth / (average + config.gap)) + 3));
    while (cars.length > count) cars.pop().img.remove();
    while (config && cars.length < count) addCar();
    reposition(); sync();
  }
  root.addEventListener('resize', () => {
    if (config && stream.clientWidth !== observedWidth) resizeCars();
  });
  document.addEventListener('visibilitychange', sync);
  reduced.addEventListener('change', sync);
  root.addEventListener('pagehide', () => { if (frame !== null) root.cancelAnimationFrame(frame); frame = null; });
  root.fetch('/api/theme', {cache: 'no-store'}).then(response => {
    if (!response.ok) throw Error('Theme config unavailable');
    return response.json();
  }).then(value => {
    const src = assetUrl(value.logo);
    if (src) {
      logo.addEventListener('error', () => { logo.classList.remove('sq-custom-logo'); logo.src = originalLogo; }, {once: true});
      logo.classList.add('sq-custom-logo');
      logo.src = src;
    }
    configure(value.activity);
  }).catch(() => { /* Missing/bad config keeps the built-in branding. */ });
})(typeof window === 'object' ? window : globalThis);
