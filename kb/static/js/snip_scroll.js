function scrollSnipRange(range) {
  const clips = [];
  for (let host = range.startContainer.parentElement; host; host = host.parentElement) {
    if (host === document.scrollingElement) continue;
    const style = getComputedStyle(host);
    const horizontal = /^(auto|scroll|hidden|clip)$/.test(style.overflowX);
    const vertical = /^(auto|scroll|hidden|clip)$/.test(style.overflowY);
    if (horizontal || vertical) clips.push({ host, horizontal, vertical });
  }
  const viewport = window.visualViewport;
  const view = {
    left: viewport ? viewport.offsetLeft : 0,
    top: viewport ? viewport.offsetTop : 0,
    width: viewport ? viewport.width : document.documentElement.clientWidth,
    height: viewport ? viewport.height : document.documentElement.clientHeight,
  };
  const width = Math.min(view.width, ...clips.filter(clip => clip.horizontal).map(clip => clip.host.clientWidth));
  const height = Math.min(view.height, ...clips.filter(clip => clip.vertical).map(clip => clip.host.clientHeight));
  const bounds = range.getBoundingClientRect();
  const target = range.cloneRange();
  if (bounds.width > width || bounds.height > height) target.collapse(true);
  const shift = (start, end, low, size) => {
    const margin = Math.min(12, Math.max(0, (size - (end - start)) / 2));
    if (start >= low + margin && end <= low + size - margin) return 0;
    return (start + end) / 2 - low - size / 2;
  };
  for (const clip of clips) {
    const rect = target.getBoundingClientRect();
    const port = clip.host.getBoundingClientRect();
    clip.host.scrollBy({
      left: clip.horizontal ? shift(rect.left, rect.right, port.left + clip.host.clientLeft, clip.host.clientWidth) : 0,
      top: clip.vertical ? shift(rect.top, rect.bottom, port.top + clip.host.clientTop, clip.host.clientHeight) : 0,
      behavior: 'instant',
    });
  }
  const rect = target.getBoundingClientRect();
  window.scrollBy({
    left: shift(rect.left, rect.right, view.left, view.width),
    top: shift(rect.top, rect.bottom, view.top, view.height),
    behavior: 'instant',
  });
}
