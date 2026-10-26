function renderSourceBody(content, { plain = false } = {}) {
  const body = $('source-reader-body'), outline = $('source-reader-outline');
  body.className = 'reader-prose';
  outline.hidden = true;
  outline.open = false;
  outline.querySelector('nav').replaceChildren();
  const renderer = plain ? null : snipDocRenderer();
  if (!renderer || !window.DOMPurify) {
    body.classList.add('is-plain');
    body.textContent = content;
    return;
  }
  body.innerHTML = DOMPurify.sanitize(renderer.parse(content));
  prepareMarkdownLinks(body);
  body.querySelectorAll('img').forEach(image => {
    image.loading = 'lazy';
    image.referrerPolicy = 'no-referrer';
  });
  const headings = [...body.querySelectorAll('h1,h2,h3,h4,h5,h6')], anchors = new Map();
  headings.forEach((heading, index) => {
    const slug = heading.textContent.trim().toLowerCase().replace(/[^\p{L}\p{N}_\s-]/gu, '').replace(/\s/g, '-');
    const count = anchors.get(slug) || 0;
    anchors.set(slug, count + 1);
    heading.dataset.readerAnchor = slug + (count ? '-' + count : '');
    heading.id = 'source-section-' + index;
  });
  if (headings.length < 3) return;
  outline.hidden = false;
  outline.querySelector('summary span').textContent = `${headings.length} 个章节`;
  const topLevel = Math.min(...headings.map(heading => Number(heading.tagName.slice(1))));
  headings.forEach(heading => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = heading.textContent;
    button.dataset.readerHeading = heading.id;
    button.style.paddingLeft = `${12 + (Number(heading.tagName.slice(1)) - topLevel) * 14}px`;
    outline.querySelector('nav').append(button);
  });
}

function scrollSourceHeading(heading) {
  if (!heading) return;
  const scroll = $('source-reader-scroll');
  scroll.scrollTop += heading.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 20;
  heading.tabIndex = -1;
  heading.focus({ preventScroll:true });
}

$('source-reader-outline').addEventListener('click', event => {
  const button = event.target.closest('[data-reader-heading]');
  if (button) scrollSourceHeading($(button.dataset.readerHeading));
});
$('source-reader-body').addEventListener('click', event => {
  const link = event.target.closest('a[href^="#"]');
  if (!link) return;
  event.preventDefault();
  let anchor;
  try { anchor = decodeURIComponent(link.getAttribute('href').slice(1)); } catch { return; }
  const heading = [...$('source-reader-body').querySelectorAll('[data-reader-anchor]')]
    .find(node => node.dataset.readerAnchor === anchor);
  scrollSourceHeading(heading);
});
