(() => {
  const root = document.documentElement;
  const viewport = window.visualViewport;
  const property = '--shell-viewport-height';
  const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve));
  const shellHeight = () => Number.parseFloat(root.style.getPropertyValue(property));
  let original;
  let height;
  let overridden = false;

  function restore() {
    if (!overridden) return;
    if (original) Object.defineProperty(viewport, 'height', original);
    else delete viewport.height;
    overridden = false;
    viewport.dispatchEvent(new Event('resize'));
  }

  return {
    async setup() {
      if (!viewport || viewport.scale !== 1 || viewport.height <= 120) {
        throw new Error('Expected a native-scale viewport taller than the treatment');
      }
      const deadline = performance.now() + 2000;
      while (!Number.isFinite(shellHeight()) && performance.now() < deadline) await nextFrame();
      if (!Number.isFinite(shellHeight())) throw new Error('OpenClaw did not publish its shell height');
      original = Object.getOwnPropertyDescriptor(viewport, 'height');
      height = viewport.height;
    },
    intervene() {
      Object.defineProperty(viewport, 'height', { value: height - 120, configurable: true });
      overridden = true;
    },
    async observe() {
      viewport.dispatchEvent(new Event('resize'));
      await nextFrame();
      await nextFrame();
      return { visual_viewport_height_px: viewport.height, published_shell_height_px: shellHeight() };
    },
    restore,
    cleanup: restore,
  };
})()
