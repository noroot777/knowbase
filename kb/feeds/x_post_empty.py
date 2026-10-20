"""Positive empty-authorship evidence from the rendered target post's own data."""

EMPTY_JS = r'''
  const postPayload = (art, identity) => {
    const key = Object.keys(art).find(key => key.startsWith('__reactFiber$'));
    for (let fiber = art[key], depth = 0; fiber && depth < 40; fiber = fiber.return, depth++) {
      const tweet = fiber.memoizedProps?.tweet;
      if (tweet?.id_str === identity) return tweet;
    }
    return null;
  };
  const payloadEmptyKind = tweet => {
    if (!tweet || typeof tweet.full_text !== 'string' || tweet.note_tweet || tweet.article) return '';
    const range = tweet.display_text_range;
    if (!Array.isArray(range) || range.length !== 2 ||
        !range.every(Number.isInteger) || range[0] < 0 || range[1] < range[0] ||
        range[1] > tweet.full_text.length) return '';
    const media = Array.isArray(tweet.entities?.media) ? tweet.entities.media : [];
    const urls = Array.isArray(tweet.entities?.urls) ? tweet.entities.urls : [];
    const mentions = Array.isArray(tweet.entities?.user_mentions) ? tweet.entities.user_mentions : [];
    if ([...media, ...urls, ...mentions].some(entity => !entity || typeof entity !== 'object')) return '';
    const cardUrl = tweet.card?.url;
    const quoteUrl = tweet.quoted_status_permalink?.url;
    const attachments = [...media, ...urls.filter(entity =>
      entity.url === cardUrl || (tweet.quoted_status_id_str && entity.url === quoteUrl))];
    const reply = ['in_reply_to_status_id_str', 'in_reply_to_user_id_str'].every(key =>
      typeof tweet[key] === 'string' && /^\d+$/.test(tweet[key]));
    const hiddenMentions = reply ? mentions.filter(mention =>
      typeof mention.screen_name === 'string' && /^\w{1,15}$/.test(mention.screen_name) &&
      Array.isArray(mention.indices) && mention.indices[1] <= range[0])
      .map(mention => ({url:'@' + mention.screen_name, indices:mention.indices})) : [];
    const tokens = [...attachments.filter(entity => typeof entity.url === 'string' &&
      /^https:\/\//.test(entity.url)), ...hiddenMentions];
    const spans = tokens.filter(entity => Array.isArray(entity.indices) &&
      entity.indices.length === 2 && entity.indices.every(Number.isInteger) &&
      entity.indices[0] >= 0 && entity.indices[1] > entity.indices[0] &&
      entity.indices[1] <= tweet.full_text.length &&
      tweet.full_text.slice(...entity.indices) === entity.url)
      .map(entity => entity.indices).sort((left, right) => right[0] - left[0]);
    let text = tweet.full_text, boundary = text.length;
    for (const [start, end] of spans) {
      if (end > boundary) return '';
      text = text.slice(0, start) + text.slice(end);
      boundary = start;
    }
    if (text.trim()) return '';
    if (media.some(entity => ['video', 'animated_gif'].includes(entity.type))) return 'video';
    if (media.some(entity => entity.type === 'photo')) return 'photo';
    if (tweet.quoted_status_id_str) return 'quote';
    if (cardUrl) return 'link';
    return 'unspecified';
  };
  const emptyPostReady = art => document.readyState === 'complete' &&
    !postText(ownNode(art, '[data-testid="tweetText"]')) && !postMore(art) &&
    !art.querySelector('[role="progressbar"], [aria-busy="true"]') &&
    !!ownNode(art, '[data-testid="reply"]') &&
    !!ownNode(art, '[data-testid="like"], [data-testid="unlike"]') &&
    !ownNode(art, 'a[href*="/article/"]');
  const emptyPostKind = (art, identity) => emptyPostReady(art) ?
    payloadEmptyKind(postPayload(art, identity)) : '';
'''
