from __future__ import annotations


DISCOVER_POSTS_SCRIPT = """
(platform) => {
  const result = [];
  const seen = new Set();
  const links = [...document.querySelectorAll('a[href]')];
  for (const link of links) {
    let parsed;
    try { parsed = new URL(link.href, location.href); } catch { continue; }
    const path = parsed.pathname;
    const isPost = platform === 'instagram'
      ? /^\\/(p|reel)\\/[A-Za-z0-9_-]+\\/?$/.test(path)
      : /^\\/[A-Za-z0-9_]+\\/status\\/\\d+/.test(path);
    if (!isPost) continue;
    const postUrl = `${parsed.origin}${parsed.pathname}`;
    if (seen.has(postUrl)) continue;
    seen.add(postUrl);
    const container = link.closest('article') || link.closest('[data-testid="cellInnerDiv"]') || link.parentElement;
    const time = container?.querySelector('time');
    result.push({
      post_url: postUrl,
      text: (container?.innerText || '').trim(),
      published_at: time?.dateTime || time?.getAttribute('datetime') || ''
    });
  }
  return result;
}
"""


INSPECT_POST_SCRIPT = """
(platform) => {
  const root = document.querySelector('article') || document.querySelector('main') || document.body;
  const text = (root?.innerText || '').trim();
  const time = root?.querySelector('time');
  const media = [];
  const seen = new Set();
  for (const image of root?.querySelectorAll('img') || []) {
    let src = image.currentSrc || image.src || '';
    const acceptable = platform === 'x'
      ? src.includes('pbs.twimg.com/media')
      : (src.includes('cdninstagram.com') || src.includes('fbcdn.net')) && image.naturalWidth >= 250;
    if (!acceptable || seen.has(src)) continue;
    if (platform === 'x') {
      try {
        const parsed = new URL(src);
        parsed.searchParams.set('name', 'orig');
        src = parsed.toString();
      } catch {}
    }
    seen.add(src);
    media.push({ media_type: 'photo', source_url: src });
  }
  const elementVideos = [];
  const videoIds = new Set();
  let videoPoster = '';
  for (const video of root?.querySelectorAll('video, video source') || []) {
    const src = video.currentSrc || video.src || video.getAttribute?.('src') || '';
    const poster = video.poster || video.getAttribute?.('poster') || '';
    if (!videoPoster && poster) videoPoster = poster;
    for (const value of [src, poster]) {
      const match = value.match(/\\/(?:amplify_video(?:_thumb)?|ext_tw_video)\\/(\\d+)\\//i);
      if (match) videoIds.add(match[1]);
    }
    const isInitSegment = /\\/(?:aud\\/mp4a|vid\\/avc1)\\/0\\/0\\//i.test(src);
    if (!src || src.startsWith('blob:') || isInitSegment || seen.has(src)) continue;
    seen.add(src);
    elementVideos.push({ source_url: src, delivery: 'direct', observed_size: 0, thumbnail_url: poster });
  }
  const networkVideos = [];
  const networkPlaylists = [];
  for (const resource of performance.getEntriesByType('resource')) {
    const src = resource.name || '';
    const allowedHost = platform === 'x'
      ? src.includes('video.twimg.com')
      : src.includes('cdninstagram.com') || src.includes('fbcdn.net');
    const isPlaylist = /\\.m3u8(?:\\?|$)/i.test(src);
    const isVideoFile = /\\.(?:mp4|m4v|mov|webm)(?:\\?|$)/i.test(src);
    const isInitSegment = /\\/(?:aud\\/mp4a|vid\\/avc1)\\/0\\/0\\//i.test(src);
    const matchesCurrentVideo = platform !== 'x' || videoIds.size === 0 ||
      [...videoIds].some((videoId) => src.includes(`/${videoId}/`));
    if (!allowedHost || (!isPlaylist && !isVideoFile) || isInitSegment || !matchesCurrentVideo || seen.has(src)) continue;
    const candidate = {
      source_url: src,
      delivery: isPlaylist ? 'hls' : 'direct',
      observed_size: Number(resource.decodedBodySize || resource.encodedBodySize || resource.transferSize || 0)
    };
    if (isPlaylist) networkPlaylists.push(candidate);
    else networkVideos.push(candidate);
  }
  const isMasterPlaylist = (candidate) =>
    /\\/pl\\/[^/]+\\.m3u8(?:\\?|$)/i.test(candidate.source_url);
  networkPlaylists.sort((a, b) =>
    Number(isMasterPlaylist(b)) - Number(isMasterPlaylist(a)) ||
    b.observed_size - a.observed_size
  );
  networkVideos.sort((a, b) => b.observed_size - a.observed_size);
  const orderedVideos = platform === 'x'
    ? [...networkPlaylists, ...elementVideos, ...networkVideos]
    : [...elementVideos, ...networkVideos, ...networkPlaylists];
  if (orderedVideos.length > 0) {
    const primary = orderedVideos[0];
    const fallbackUrls = orderedVideos
      .slice(1)
      .map((candidate) => candidate.source_url)
      .filter((src, index, values) => src && values.indexOf(src) === index);
    media.push({
      media_type: 'video',
      source_url: primary.source_url,
      delivery: primary.delivery,
      thumbnail_url: primary.thumbnail_url || videoPoster,
      fallback_urls: fallbackUrls
    });
  }
  const labels = [...root.querySelectorAll('[aria-label]')].map((node) => node.getAttribute('aria-label') || '');
  const findMetric = (tokens) => labels.find((label) => tokens.some((token) => label.toLowerCase().includes(token))) || '';
  return {
    text,
    published_at: time?.dateTime || time?.getAttribute('datetime') || '',
    likes: findMetric(['like', '讚']),
    views: findMetric(['view', '觀看']),
    media
  };
}
"""
