from __future__ import annotations


FOLLOWING_SCAN_SCRIPT = """
(platform) => {
  const ignored = new Set([
    'about', 'accounts', 'direct', 'explore', 'home', 'i', 'intent', 'messages',
    'notifications', 'privacy', 'reel', 'reels', 'search', 'settings', 'share',
    'stories', 'terms', 'tos'
  ]);
  const accounts = [];
  const seen = new Set();
  const emptyScroll = { moved: false, position: 0, maximum: 0, at_end: true };
  if (platform === 'x' && !/\\/following\\/?$/.test(location.pathname)) {
    return { accounts, scroll: emptyScroll };
  }
  const isInstagramFollowingDialog = (dialog) => {
    const signal = [
      dialog.getAttribute('aria-label') || '',
      ...[...dialog.querySelectorAll('h1, h2, header')].map((node) => node.innerText || ''),
      ...[...dialog.querySelectorAll('button')].map((node) => node.innerText || '')
    ].join(' ').toLowerCase();
    const profileLinks = [...dialog.querySelectorAll('a[href]')].filter((anchor) => {
      try {
        const parsed = new URL(anchor.href, location.href);
        const parts = parsed.pathname.split('/').filter(Boolean);
        return parsed.hostname.includes('instagram.com')
          && parts.length === 1
          && /^[A-Za-z0-9._]{1,30}$/.test(parts[0]);
      } catch {
        return false;
      }
    });
    return profileLinks.length > 0
      && /(following|追蹤中|正在追蹤|フォロー中|팔로잉|siguiendo|seguidos|abonnements|gefolgt|seguindo|mengikuti|takip)/i.test(signal);
  };
  const scope = platform === 'instagram'
    ? [...document.querySelectorAll('[role="dialog"]')].find(isInstagramFollowingDialog)
    : document;
  if (!scope) return { accounts, scroll: emptyScroll };
  const selector = platform === 'x'
    ? '[data-testid="UserCell"] a[href]'
    : 'a[href]';
  const anchors = [...scope.querySelectorAll(selector)];
  let firstAccountAnchor = null;
  const isVisible = (node) => {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const handlePattern = platform === 'x'
    ? /^[A-Za-z0-9_]{1,15}$/
    : /^[A-Za-z0-9._]{1,30}$/;
  for (const anchor of anchors) {
    if (!isVisible(anchor)) continue;
    let parsed;
    try { parsed = new URL(anchor.href, location.href); } catch { continue; }
    const parts = parsed.pathname.split('/').filter(Boolean);
    if (parts.length !== 1) continue;
    const handle = parts[0].replace(/^@/, '');
    if (!handlePattern.test(handle)) continue;
    if (ignored.has(handle.toLowerCase())) continue;
    if (platform === 'instagram' && !parsed.hostname.includes('instagram.com')) continue;
    if (platform === 'x' && !(parsed.hostname === 'x.com' || parsed.hostname.endsWith('.x.com') || parsed.hostname.includes('twitter.com'))) continue;
    if (!firstAccountAnchor) firstAccountAnchor = anchor;
    const marker = handle.toLowerCase();
    if (seen.has(marker)) continue;
    seen.add(marker);
    let container = platform === 'x' ? anchor.closest('[data-testid="UserCell"]') : null;
    if (!container) {
      let candidate = anchor;
      for (let depth = 0; depth < 8 && candidate; depth += 1) {
        if (candidate.querySelector?.('img') && (candidate.innerText || '').toLowerCase().includes(handle.toLowerCase())) {
          container = candidate;
          break;
        }
        candidate = candidate.parentElement;
      }
    }
    const image = container?.querySelector('img') || anchor.querySelector('img');
    const text = (container?.innerText || anchor.innerText || '')
      .trim()
      .split('\\n')
      .map((item) => item.trim())
      .filter(Boolean);
    const ignoredLabels = new Set([
      handle.toLowerCase(), `@${handle.toLowerCase()}`, 'follow', 'following',
      '追蹤', '追蹤中', '已追蹤', 'follows you'
    ]);
    const displayName = text.find((item) => {
      const normalized = item.toLowerCase();
      return item.length <= 120 && !ignoredLabels.has(normalized);
    }) || '';
    const verified = Boolean(
      container?.querySelector(
        '[data-testid="icon-verified"], [aria-label*="Verified"], [aria-label*="已驗證"], [title*="Verified"], [title*="已驗證"]'
      )
    );
    accounts.push({
      handle,
      display_name: displayName,
      profile_url: `${parsed.origin}/${handle}${platform === 'instagram' ? '/' : ''}`,
      avatar_url: image?.currentSrc || image?.src || image?.getAttribute('src') || '',
      context_text: text.join(' ').slice(0, 500),
      verified
    });
  }
  let target = document.scrollingElement;
  if (platform === 'instagram') {
    const candidates = [];
    let node = firstAccountAnchor?.parentElement || scope;
    while (node && node !== scope.parentElement) {
      const style = getComputedStyle(node);
      if (
        node.scrollHeight > node.clientHeight + 40
        && ['auto', 'scroll'].includes(style.overflowY)
      ) {
        candidates.push(node);
      }
      node = node.parentElement;
    }
    target = candidates.sort(
      (a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
    )[0] || scope;
  }
  if (!target) return { accounts, scroll: emptyScroll };
  const before = target.scrollTop;
  const maximum = Math.max(0, target.scrollHeight - target.clientHeight);
  target.scrollTop = Math.min(maximum, before + Math.max(target.clientHeight * 0.9, 480));
  const position = target.scrollTop;
  return {
    accounts,
    scroll: {
      moved: position > before,
      position,
      maximum,
      at_end: position >= maximum - 2
    }
  };
}
"""


SCROLL_SCRIPT = """
(platform) => {
  const dialog = platform === 'instagram'
    ? [...document.querySelectorAll('[role="dialog"]')].find((candidate) => {
        const signal = [
          candidate.getAttribute('aria-label') || '',
          ...[...candidate.querySelectorAll('h1, h2, header, button')].map((node) => node.innerText || '')
        ].join(' ').toLowerCase();
        return /(following|追蹤中|正在追蹤|フォロー中|팔로잉|siguiendo|seguidos|abonnements|gefolgt|seguindo|mengikuti|takip)/i.test(signal);
      })
    : null;
  const candidates = dialog
    ? [...dialog.querySelectorAll('*')].filter((node) => {
        const style = getComputedStyle(node);
        return node.scrollHeight > node.clientHeight + 80 && ['auto', 'scroll'].includes(style.overflowY);
      })
    : [];
  const target = candidates.sort((a, b) => b.scrollHeight - a.scrollHeight)[0] || document.scrollingElement;
  if (!target) return { moved: false, position: 0, maximum: 0 };
  const before = target.scrollTop;
  const maximum = Math.max(0, target.scrollHeight - target.clientHeight);
  target.scrollTop = Math.min(maximum, before + Math.max(target.clientHeight * 0.9, 480));
  return {
    moved: target.scrollTop > before,
    position: target.scrollTop,
    maximum
  };
}
"""


RESET_FOLLOWING_SCROLL_SCRIPT = """
(platform) => {
  const dialog = platform === 'instagram'
    ? [...document.querySelectorAll('[role="dialog"]')].find((candidate) => {
        const signal = [
          candidate.getAttribute('aria-label') || '',
          ...[...candidate.querySelectorAll('h1, h2, header, button')].map((node) => node.innerText || '')
        ].join(' ').toLowerCase();
        return /(following|追蹤中|正在追蹤|フォロー中|팔로잉|siguiendo|seguidos|abonnements|gefolgt|seguindo|mengikuti|takip)/i.test(signal);
      })
    : null;
  const candidates = dialog
    ? [...dialog.querySelectorAll('*')].filter((node) => {
        const style = getComputedStyle(node);
        return node.scrollHeight > node.clientHeight + 40 && ['auto', 'scroll'].includes(style.overflowY);
      })
    : [];
  const target = candidates.sort(
    (a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
  )[0] || document.scrollingElement;
  if (!target) return false;
  const moved = target.scrollTop > 0;
  target.scrollTop = 0;
  return moved;
}
"""
