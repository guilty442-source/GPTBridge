from __future__ import annotations


AUTO_SCAN_CONTEXT_SCRIPT = """
(platform) => {
  const ignored = new Set([
    'about', 'accounts', 'direct', 'explore', 'home', 'i', 'intent', 'messages',
    'notifications', 'privacy', 'reel', 'reels', 'search', 'settings', 'share',
    'stories', 'terms', 'tos'
  ]);
  const pathParts = location.pathname.split('/').filter(Boolean);
  const loginVisible = Boolean(document.querySelector(
    'input[name="username"], input[autocomplete="username"], input[autocomplete="current-password"]'
  ));
  const unavailablePattern = /(sorry, this page isn't available|page isn't available|此頁面無法使用|找不到此頁面|頁面無法使用|頁面無法顯示)/i;
  const pageUnavailable = platform === 'instagram'
    && unavailablePattern.test(document.body?.innerText || '');
  const handlePattern = platform === 'x'
    ? /^[A-Za-z0-9_]{1,15}$/
    : /^[A-Za-z0-9._]{1,30}$/;
  const isAllowedHandle = (handle) => {
    const normalized = String(handle || '').replace(/^@/, '').trim();
    return handlePattern.test(normalized) && !ignored.has(normalized.toLowerCase());
  };
  const handleFromHref = (href) => {
    let parsed;
    try { parsed = new URL(href, location.href); } catch { return ''; }
    if (platform === 'instagram' && !parsed.hostname.includes('instagram.com')) return '';
    if (platform === 'x' && !(parsed.hostname === 'x.com' || parsed.hostname.endsWith('.x.com') || parsed.hostname.includes('twitter.com'))) return '';
    const parts = parsed.pathname.split('/').filter(Boolean);
    const handle = parts.length === 1 ? parts[0].replace(/^@/, '') : '';
    return isAllowedHandle(handle) ? handle : '';
  };
  const isVisible = (node) => {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const profileTextPattern = /(profile|個人檔案|個人資料|プロフィール|프로필|perfil|profil|profiel|profilo)/i;
  const nodeSignal = (node) => [
    node?.innerText || '',
    node?.getAttribute?.('aria-label') || '',
    node?.getAttribute?.('title') || '',
    node?.getAttribute?.('alt') || '',
    ...[...(node?.querySelectorAll?.('[aria-label], [title], img[alt]') || [])].map((item) => [
      item.getAttribute('aria-label') || '',
      item.getAttribute('title') || '',
      item.getAttribute('alt') || ''
    ].join(' '))
  ].join(' ');
  const isLikelyInstagramProfileAnchor = (anchor) => {
    const handle = handleFromHref(anchor.href);
    if (!handle) return false;
    const signal = nodeSignal(anchor);
    if (profileTextPattern.test(signal)) return true;
    const navigationScope = anchor.closest('nav, [role="navigation"], header, aside');
    return Boolean(
      navigationScope
      && anchor.querySelector('img')
      && (isVisible(anchor) || anchor.getClientRects().length > 0)
      && profileTextPattern.test(nodeSignal(navigationScope))
    );
  };
  const extractViewerHandleFromScripts = () => {
    const scriptText = [...document.scripts]
      .map((script) => script.textContent || '')
      .filter(Boolean)
      .join('\\n')
      .slice(0, 2000000);
    const patterns = [
      /"viewer"[\\s\\S]{0,900}?"username"\\s*:\\s*"([A-Za-z0-9._]{1,30})"/,
      /"viewer_username"\\s*:\\s*"([A-Za-z0-9._]{1,30})"/,
      /"username"\\s*:\\s*"([A-Za-z0-9._]{1,30})"[\\s\\S]{0,500}?"is_viewer"\\s*:\\s*true/,
      /"username"\\s*:\\s*"([A-Za-z0-9._]{1,30})"[\\s\\S]{0,500}?"isViewer"\\s*:\\s*true/
    ];
    for (const pattern of patterns) {
      const match = scriptText.match(pattern);
      const handle = match?.[1] || '';
      if (isAllowedHandle(handle)) return handle;
    }
    return '';
  };
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
  const followingDialog = platform === 'instagram'
    ? [...document.querySelectorAll('[role="dialog"]')].find(isInstagramFollowingDialog)
    : null;
  const ready = !loginVisible && (platform === 'instagram'
    ? Boolean(followingDialog)
    : pathParts.length === 2 && pathParts[1] === 'following');
  if (ready) {
    return {
      logged_in: true,
      ready: true,
      target_url: location.href,
      following_url: location.href,
      message: ''
    };
  }
  if (pageUnavailable) {
    return {
      logged_in: !loginVisible,
      ready: false,
      target_url: '',
      following_url: '',
      message: 'Instagram 目前在不可用頁面，正在重新解析個人檔案入口'
    };
  }

  let profileAnchor = null;
  if (platform === 'x') {
    profileAnchor = document.querySelector('[data-testid="AppTabBar_Profile_Link"][href]');
  } else {
    const scopes = [...document.querySelectorAll('nav, [role="navigation"], header, aside')];
    const anchors = [...new Set((scopes.length ? scopes : [document]).flatMap((scope) => [...scope.querySelectorAll('a[href]')]))];
    const profileSelector = [
      '[aria-label="Profile"]',
      '[aria-label="個人檔案"]',
      '[aria-label="個人資料"]',
      '[title="Profile"]',
      '[title="個人檔案"]'
    ].join(', ');
    profileAnchor = anchors.find((anchor) => (
      anchor.matches(profileSelector) || Boolean(anchor.querySelector(profileSelector))
    )) || anchors.find(isLikelyInstagramProfileAnchor);
  }
  let handle = profileAnchor ? handleFromHref(profileAnchor.href) : '';
  if (!handle && platform === 'instagram' && pathParts.length === 1 && isAllowedHandle(pathParts[0])) {
    handle = pathParts[0].replace(/^@/, '');
  }
  if (!handle && platform === 'instagram') {
    handle = extractViewerHandleFromScripts();
  }
  if (!handle) {
    return {
      logged_in: !loginVisible && Boolean(document.querySelector('nav')),
      ready: false,
      target_url: '',
      following_url: '',
      message: loginVisible ? '等待登入' : '找不到個人檔案入口'
    };
  }

  const origin = platform === 'x' ? 'https://x.com' : 'https://www.instagram.com';
  return {
    logged_in: true,
    ready: false,
    target_url: platform === 'instagram'
      ? `${origin}/${handle}/`
      : `${origin}/${handle}/following/`,
    following_url: `${origin}/${handle}/following/`,
    message: ''
  };
}
"""


INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT = """
() => {
  const text = document.body?.innerText || '';
  return /(sorry, this page isn't available|page isn't available|此頁面無法使用|找不到此頁面|頁面無法使用|頁面無法顯示)/i.test(text);
}
"""


INSTAGRAM_PROFILE_NAVIGATION_SCRIPT = """
() => {
  const ignored = new Set([
    'about', 'accounts', 'direct', 'explore', 'home', 'i', 'intent', 'messages',
    'notifications', 'privacy', 'reel', 'reels', 'search', 'settings', 'share',
    'stories', 'terms', 'tos'
  ]);
  const profilePattern = /(profile|個人檔案|個人資料|プロフィール|프로필|perfil|profil|profiel|profilo)/i;
  const normalizeHandle = (href) => {
    let parsed;
    try { parsed = new URL(href, location.href); } catch { return ''; }
    if (!parsed.hostname.includes('instagram.com')) return '';
    const parts = parsed.pathname.split('/').filter(Boolean);
    const handle = parts.length === 1 ? parts[0].replace(/^@/, '') : '';
    return /^[A-Za-z0-9._]{1,30}$/.test(handle)
      && !ignored.has(handle.toLowerCase())
      ? handle
      : '';
  };
  const signalFor = (node) => [
    node?.innerText || '',
    node?.textContent || '',
    node?.getAttribute?.('aria-label') || '',
    node?.getAttribute?.('title') || '',
    node?.getAttribute?.('alt') || '',
    ...[...(node?.querySelectorAll?.('[aria-label], [title], img[alt]') || [])].map((child) => [
      child.getAttribute('aria-label') || '',
      child.getAttribute('title') || '',
      child.getAttribute('alt') || ''
    ].join(' '))
  ].join(' ');
  const anchors = [
    ...document.querySelectorAll('nav a[href], [role="navigation"] a[href], header a[href], aside a[href], a[href]')
  ];
  const profileAnchor = anchors.find((anchor) => {
    if (!normalizeHandle(anchor.href)) return false;
    if (profilePattern.test(signalFor(anchor))) return true;
    const scope = anchor.closest('nav, [role="navigation"], header, aside');
    return Boolean(scope && anchor.querySelector('img') && profilePattern.test(signalFor(scope)));
  });
  if (!profileAnchor) return false;
  profileAnchor.scrollIntoView?.({ block: 'center', inline: 'center' });
  for (const eventType of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    let event;
    try {
      event = new (eventType.startsWith('pointer') ? PointerEvent : MouseEvent)(
        eventType,
        { bubbles: true, cancelable: true, view: window }
      );
    } catch {
      event = new MouseEvent(eventType.replace('pointer', 'mouse'), {
        bubbles: true,
        cancelable: true,
        view: window
      });
    }
    profileAnchor.dispatchEvent(event);
  }
  profileAnchor.click?.();
  return true;
}
"""


OPEN_FOLLOWING_LIST_SCRIPT = """
(platform) => {
  if (platform !== 'instagram') return false;
  const followingPattern = /(following|追蹤中|正在追蹤|關注中|关注中|フォロー中|팔로잉|siguiendo|seguidos|abonnements|gefolgt|seguindo|mengikuti|takip)/i;
  const scope = document.querySelector('main') || document;
  const signalFor = (node) => [
    node.innerText || '',
    node.textContent || '',
    node.getAttribute?.('aria-label') || '',
    node.getAttribute?.('title') || '',
    node.getAttribute?.('href') || '',
    ...[...(node.querySelectorAll?.('[aria-label], [title]') || [])].map((child) => [
      child.getAttribute('aria-label') || '',
      child.getAttribute('title') || ''
    ].join(' '))
  ].join(' ').trim().toLowerCase();
  const isFollowingTarget = (node) => {
    const signal = signalFor(node);
    return followingPattern.test(signal);
  };
  const candidates = [
    ...scope.querySelectorAll('a[href], button, [role="button"], [role="link"]'),
    ...document.querySelectorAll('a[href*="/following/"]')
  ];
  const following = candidates.find((node) => {
    const link = node.matches?.('a[href]') ? node : node.closest?.('a[href]');
    if (link?.href) {
      let parsed;
      try { parsed = new URL(link.href, location.href); } catch { parsed = null; }
      const parts = parsed?.pathname.split('/').filter(Boolean) || [];
      if (parsed?.hostname.includes('instagram.com') && parts.length === 2 && parts[1] === 'following') return true;
    }
    return isFollowingTarget(node);
  });
  if (!following) return false;
  const clickTarget = following.closest?.('a[href], button, [role="button"], [role="link"]') || following;
  clickTarget.scrollIntoView?.({ block: 'center', inline: 'center' });
  for (const eventType of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    let parsed;
    try {
      parsed = new (eventType.startsWith('pointer') ? PointerEvent : MouseEvent)(
        eventType,
        { bubbles: true, cancelable: true, view: window }
      );
    } catch {
      parsed = new MouseEvent(eventType.replace('pointer', 'mouse'), {
        bubbles: true,
        cancelable: true,
        view: window
      });
    }
    clickTarget.dispatchEvent(parsed);
  }
  clickTarget.click?.();
  return true;
}
"""


INSTAGRAM_PROFILE_SETTINGS_SCRIPT = """
() => {
  const ignored = new Set([
    'about', 'accounts', 'direct', 'explore', 'home', 'i', 'intent', 'messages',
    'notifications', 'privacy', 'reel', 'reels', 'search', 'settings', 'share',
    'stories', 'terms', 'tos'
  ]);
  const normalizeHandle = (value) => {
    const handle = String(value || '').trim().replace(/^@/, '');
    return /^[A-Za-z0-9._]{1,30}$/.test(handle)
      && !ignored.has(handle.toLowerCase())
      ? handle
      : '';
  };
  const inputs = [
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[aria-label*="username" i]',
    'input[aria-label*="用戶" i]',
    'input[aria-label*="使用者" i]',
    'input[aria-label*="帳號" i]',
    'input[placeholder*="username" i]',
    'input[placeholder*="用戶" i]',
    'input[placeholder*="使用者" i]',
    'input[placeholder*="帳號" i]'
  ];
  for (const selector of inputs) {
    const input = document.querySelector(selector);
    const handle = normalizeHandle(input?.value || input?.getAttribute?.('value'));
    if (handle) return handle;
  }
  const scriptText = [...document.scripts]
    .map((script) => script.textContent || '')
    .filter(Boolean)
    .join('\\n')
    .slice(0, 2000000);
  const patterns = [
    /"viewer"[\\s\\S]{0,900}?"username"\\s*:\\s*"([A-Za-z0-9._]{1,30})"/,
    /"viewer_username"\\s*:\\s*"([A-Za-z0-9._]{1,30})"/,
    /"username"\\s*:\\s*"([A-Za-z0-9._]{1,30})"[\\s\\S]{0,500}?"is_viewer"\\s*:\\s*true/,
    /"username"\\s*:\\s*"([A-Za-z0-9._]{1,30})"[\\s\\S]{0,500}?"isViewer"\\s*:\\s*true/
  ];
  for (const pattern of patterns) {
    const handle = normalizeHandle(scriptText.match(pattern)?.[1] || '');
    if (handle) return handle;
  }
  return '';
}
"""
