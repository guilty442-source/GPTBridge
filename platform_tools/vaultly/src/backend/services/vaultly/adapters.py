from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from time import monotonic
from typing import Any, Iterable
from urllib.parse import urlencode


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


IGNORED_HANDLES = {
    "about",
    "accounts",
    "direct",
    "explore",
    "home",
    "i",
    "intent",
    "messages",
    "notifications",
    "privacy",
    "reel",
    "reels",
    "search",
    "settings",
    "share",
    "stories",
    "terms",
    "tos",
}

HANDLE_PATTERNS = {
    "instagram": re.compile(r"^[A-Za-z0-9._]{1,30}$"),
    "x": re.compile(r"^[A-Za-z0-9_]{1,15}$"),
}
INSTAGRAM_WEB_APP_ID = "936619743392459"
INSTAGRAM_API_PAGE_SIZE = 50

STAR_ACCOUNT_WORDS = {
    "actor",
    "actress",
    "artist",
    "band",
    "dancer",
    "idol",
    "jpop",
    "kpop",
    "model",
    "performer",
    "singer",
    "trainee",
}

STAR_ACCOUNT_PHRASES = {
    "k pop",
    "j pop",
    "c pop",
    "偶像",
    "女團",
    "女星",
    "明星",
    "歌手",
    "演員",
    "演藝",
    "男團",
    "男星",
    "練習生",
    "舞者",
    "藝人",
    "藝能",
    "韓團",
    "日團",
    "模特",
    "模特兒",
}

NON_STAR_ACCOUNT_WORDS = {
    "agency",
    "cafe",
    "clinic",
    "company",
    "corp",
    "corporation",
    "fanclub",
    "fanpage",
    "hospital",
    "hotel",
    "inc",
    "ltd",
    "magazine",
    "mall",
    "market",
    "media",
    "news",
    "newspaper",
    "outlet",
    "restaurant",
    "shop",
    "store",
    "tourism",
    "travel",
    "wholesale",
}

NON_STAR_ACCOUNT_PHRASES = {
    "不動產",
    "代購",
    "企業",
    "公司",
    "商店",
    "商場",
    "客服",
    "工作機會",
    "批發",
    "折扣",
    "招募",
    "新聞",
    "旅遊",
    "旅行社",
    "日報",
    "活動企劃",
    "物流",
    "房仲",
    "房產",
    "品牌",
    "團購",
    "媒體",
    "官方客服",
    "購物",
    "醫院",
    "診所",
    "餐廳",
    "飯店",
    "電商",
    "雜誌",
    "優惠",
    "零售",
}


def is_star_candidate_account(
    account: dict[str, Any],
    filter_terms: Iterable[str] = (),
) -> tuple[bool, str]:
    if account.get("verified") is True:
        return True, "已認證帳號"

    text = " ".join(
        [
            str(account.get("handle", "")),
            str(account.get("display_name", "")),
            str(account.get("context_text", "")),
        ]
    ).casefold()
    normalized = re.sub(r"[_\-.]+", " ", text)
    words = set(re.findall(r"[a-z0-9]+", normalized))
    handle = str(account.get("handle", "")).strip().lstrip("@").casefold()

    for raw_term in filter_terms:
        term = str(raw_term).strip().casefold()
        if not term:
            continue
        if term.startswith("@"):
            if handle == term[1:].lstrip("@"):
                return False, f"自訂篩選帳號：{raw_term}"
            continue
        if term in text or term in normalized:
            return False, f"自訂篩選關鍵字：{raw_term}"

    matched_word = sorted(words.intersection(NON_STAR_ACCOUNT_WORDS))
    if matched_word:
        return False, f"明顯非明星帳號關鍵字：{matched_word[0]}"

    for phrase in NON_STAR_ACCOUNT_PHRASES:
        if phrase in normalized:
            return False, f"明顯非明星帳號關鍵字：{phrase}"

    matched_star_word = sorted(words.intersection(STAR_ACCOUNT_WORDS))
    if matched_star_word:
        return True, "偶像明星線索"

    for phrase in STAR_ACCOUNT_PHRASES:
        if phrase in normalized:
            return True, "偶像明星線索"
    return True, ""


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
  for (const video of root?.querySelectorAll('video, video source') || []) {
    const src = video.currentSrc || video.src || video.getAttribute?.('src') || '';
    const poster = video.poster || video.getAttribute?.('poster') || '';
    for (const value of [src, poster]) {
      const match = value.match(/\\/(?:amplify_video(?:_thumb)?|ext_tw_video)\\/(\\d+)\\//i);
      if (match) videoIds.add(match[1]);
    }
    const isInitSegment = /\\/(?:aud\\/mp4a|vid\\/avc1)\\/0\\/0\\//i.test(src);
    if (!src || src.startsWith('blob:') || isInitSegment || seen.has(src)) continue;
    seen.add(src);
    elementVideos.push({ source_url: src, delivery: 'direct', observed_size: 0 });
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


@dataclass(frozen=True)
class PlatformDefinition:
    id: str
    name: str
    home_url: str
    match_hosts: tuple[str, ...]
    media_hosts: tuple[str, ...]


PLATFORMS: dict[str, PlatformDefinition] = {
    "instagram": PlatformDefinition(
        id="instagram",
        name="Instagram",
        home_url="https://www.instagram.com/",
        match_hosts=("instagram.com",),
        media_hosts=("cdninstagram.com", "fbcdn.net"),
    ),
    "x": PlatformDefinition(
        id="x",
        name="X",
        home_url="https://x.com/home",
        match_hosts=("x.com", "twitter.com"),
        media_hosts=("twimg.com",),
    ),
}


class PlatformAdapter:
    def __init__(self, definition: PlatformDefinition) -> None:
        self.definition = definition
        self.last_scan_stats: dict[str, Any] = {
            "observed": 0,
            "accepted": 0,
            "filtered": 0,
            "filtered_account_ids": [],
            "filtered_accounts": [],
            "rounds": 0,
            "completed": False,
            "duration_ms": 0,
            "reset_to_start": False,
        }
        self.last_prepare_status: dict[str, str] = {
            "status": "waiting_login",
            "message": "等待登入後自動掃描",
        }

    def _normalize_scanned_account(self, account: Any) -> dict[str, Any] | None:
        if not isinstance(account, dict):
            return None
        handle = str(account.get("handle", "")).strip().lstrip("@")
        pattern = HANDLE_PATTERNS[self.definition.id]
        if not pattern.fullmatch(handle) or handle.casefold() in IGNORED_HANDLES:
            return None

        if self.definition.id == "instagram":
            profile_url = f"https://www.instagram.com/{handle}/"
        else:
            profile_url = f"https://x.com/{handle}"

        return {
            "account_id": f"{self.definition.id}:{handle.casefold()}",
            "platform": self.definition.id,
            "handle": handle,
            "display_name": str(account.get("display_name", "")).strip()[:120],
            "profile_url": profile_url,
            "avatar_url": str(account.get("avatar_url", "")).strip(),
            "context_text": str(account.get("context_text", "")).strip()[:500],
            "verified": bool(account.get("verified", False)),
        }

    @staticmethod
    def _merge_account(
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> None:
        for key in ("display_name", "avatar_url"):
            if not existing.get(key) and incoming.get(key):
                existing[key] = incoming[key]
        if incoming.get("verified") is True:
            existing["verified"] = True

    async def hydrate_avatar_urls(
        self,
        page: Any,
        accounts: list[dict[str, Any]],
    ) -> None:
        if self.definition.id != "instagram":
            return

        semaphore = asyncio.Semaphore(8)

        async def hydrate(account: dict[str, Any]) -> None:
            avatar_url = str(account.get("avatar_url", "")).strip()
            if not avatar_url.startswith("https://"):
                return
            async with semaphore:
                try:
                    response = await page.context.request.get(
                        avatar_url,
                        headers={
                            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                            "Referer": "https://www.instagram.com/",
                        },
                        timeout=15_000,
                    )
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if not response.ok or not content_type.startswith("image/"):
                        return
                    content = await response.body()
                    if not content or len(content) > 512 * 1024:
                        return
                    encoded = base64.b64encode(content).decode("ascii")
                    account["avatar_url"] = f"data:{content_type};base64,{encoded}"
                except Exception:
                    return

        try:
            await asyncio.wait_for(
                asyncio.gather(*(hydrate(account) for account in accounts)),
                timeout=25,
            )
        except asyncio.TimeoutError:
            return

    def _set_prepare_status(self, status: str, message: str) -> None:
        self.last_prepare_status = {"status": status, "message": message}

    @staticmethod
    async def _browser_cookies(page: Any, url: str) -> list[dict[str, Any]]:
        context = getattr(page, "context", None)
        cookies_method = getattr(context, "cookies", None)
        if not callable(cookies_method):
            return []

        try:
            cookies = await cookies_method([url])
        except Exception:
            return []
        return [cookie for cookie in cookies if isinstance(cookie, dict)]

    @classmethod
    async def _browser_cookie_map(cls, page: Any, url: str) -> dict[str, str]:
        cookies = await cls._browser_cookies(page, url)
        return {
            str(cookie.get("name", "")): str(cookie.get("value", ""))
            for cookie in cookies
            if cookie.get("name") and cookie.get("value")
        }

    @staticmethod
    def _cookie_header(cookies: dict[str, str]) -> str:
        return "; ".join(
            f"{name}={value}"
            for name, value in cookies.items()
            if name and value
        )

    async def _instagram_cookie_session_state(self, page: Any) -> dict[str, Any]:
        if self.definition.id != "instagram":
            return {}
        cookies = await self._browser_cookie_map(
            page,
            "https://www.instagram.com/",
        )
        if not cookies.get("sessionid") or not cookies.get("ds_user_id"):
            return {}
        return {
            "logged_in": True,
            "ready": True,
            "user_id": cookies["ds_user_id"],
            "message": "Instagram Cookie 登入可用",
        }

    async def _instagram_request_json(
        self,
        page: Any,
        url: str,
        *,
        referer: str = "https://www.instagram.com/",
    ) -> dict[str, Any] | None:
        context = getattr(page, "context", None)
        request = getattr(context, "request", None)
        get_method = getattr(request, "get", None)
        if not callable(get_method):
            return None

        cookies = await self._browser_cookie_map(page, "https://www.instagram.com/")
        if not cookies.get("sessionid"):
            return None
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "X-ASBD-ID": "129477",
            "X-IG-App-ID": INSTAGRAM_WEB_APP_ID,
            "X-Requested-With": "XMLHttpRequest",
        }
        csrf_token = cookies.get("csrftoken")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        cookie_header = self._cookie_header(cookies)
        if cookie_header:
            headers["Cookie"] = cookie_header

        response = await get_method(url, headers=headers, timeout=45_000)
        if not getattr(response, "ok", False):
            return None

        json_method = getattr(response, "json", None)
        if callable(json_method):
            parsed = await json_method()
            return parsed if isinstance(parsed, dict) else None

        body_method = getattr(response, "body", None)
        if callable(body_method):
            raw_body = await body_method()
            parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
            return parsed if isinstance(parsed, dict) else None
        return None

    def _normalize_instagram_api_user(self, user: Any) -> dict[str, Any] | None:
        if not isinstance(user, dict):
            return None
        handle = str(user.get("username", "")).strip()
        if not handle:
            return None
        return self._normalize_scanned_account(
            {
                "handle": handle,
                "display_name": user.get("full_name", ""),
                "avatar_url": user.get("profile_pic_url", ""),
                "context_text": " ".join(
                    str(value)
                    for value in (
                        user.get("full_name", ""),
                        user.get("username", ""),
                    )
                    if value
                ),
                "verified": bool(user.get("is_verified", False)),
            }
        )

    async def _is_instagram_unavailable_page(self, page: Any) -> bool:
        if self.definition.id != "instagram":
            return False
        try:
            return bool(await page.evaluate(INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT))
        except Exception:
            return False

    async def _open_instagram_profile_from_nav(self, page: Any) -> bool:
        if self.definition.id != "instagram":
            return False
        try:
            opened = bool(await page.evaluate(INSTAGRAM_PROFILE_NAVIGATION_SCRIPT))
            if opened:
                await page.wait_for_timeout(1_500)
            return opened
        except Exception:
            return False

    async def _wait_for_following_ready(
        self,
        page: Any,
        attempts: int = 10,
        delay_ms: int = 750,
    ) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        for attempt in range(max(1, attempts)):
            state = await page.evaluate(AUTO_SCAN_CONTEXT_SCRIPT, self.definition.id)
            latest = state if isinstance(state, dict) else {}
            if latest.get("ready") is True:
                self._set_prepare_status("ready", "追蹤名單已開啟")
                return latest
            if attempt + 1 < attempts:
                await page.wait_for_timeout(delay_ms)
        return latest

    async def _open_instagram_following(self, page: Any) -> bool:
        for _attempt in range(4):
            opened = await page.evaluate(OPEN_FOLLOWING_LIST_SCRIPT, self.definition.id)
            if opened:
                state = await self._wait_for_following_ready(
                    page,
                    attempts=12,
                    delay_ms=500,
                )
                if state.get("ready") is True:
                    return True
            await page.wait_for_timeout(1_000)
        return False

    async def _resolve_instagram_profile_from_settings(self, page: Any) -> dict[str, Any]:
        if self.definition.id != "instagram":
            return {}
        try:
            await page.goto(
                "https://www.instagram.com/accounts/edit/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await page.wait_for_timeout(1_500)
            raw_handle = await page.evaluate(INSTAGRAM_PROFILE_SETTINGS_SCRIPT)
        except Exception:
            return {}

        handle = str(raw_handle or "").strip().lstrip("@")
        if not HANDLE_PATTERNS["instagram"].fullmatch(handle):
            return {}
        if handle.casefold() in IGNORED_HANDLES:
            return {}
        origin = "https://www.instagram.com"
        return {
            "logged_in": True,
            "ready": False,
            "target_url": f"{origin}/{handle}/",
            "following_url": f"{origin}/{handle}/following/",
            "message": "",
        }

    async def _prepare_following_scan_with_dom(self, page: Any) -> bool:
        state = await self._wait_for_following_ready(page, attempts=3, delay_ms=1_000)
        if state.get("ready") is True and self.definition.id != "instagram":
            return True

        target_url = str(state.get("target_url", "")).strip()
        following_url = str(state.get("following_url", "")).strip()
        if self.definition.id == "instagram" and state.get("logged_in") is True:
            settings_state = await self._resolve_instagram_profile_from_settings(page)
            settings_target_url = str(settings_state.get("target_url", "")).strip()
            settings_following_url = str(
                settings_state.get("following_url", "")
            ).strip()
            if settings_target_url:
                target_url = settings_target_url
                following_url = settings_following_url
                state = settings_state
            elif await self._open_instagram_profile_from_nav(page):
                if await self._open_instagram_following(page):
                    return True
        if not target_url:
            if self.definition.id == "instagram" and state.get("logged_in") is True:
                fallback_state = await self._resolve_instagram_profile_from_settings(page)
                target_url = str(fallback_state.get("target_url", "")).strip()
                following_url = str(fallback_state.get("following_url", "")).strip()
                if target_url:
                    state = fallback_state
            if not target_url:
                message = str(state.get("message", "")).strip() or "等待登入後自動掃描"
                self._set_prepare_status(
                    "waiting_login" if state.get("logged_in") is not True else "error",
                    message,
                )
                return False

        if self.definition.id == "instagram":
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1_500)
            if await self._is_instagram_unavailable_page(page):
                fallback_state = await self._resolve_instagram_profile_from_settings(page)
                fallback_target_url = str(fallback_state.get("target_url", "")).strip()
                fallback_following_url = str(
                    fallback_state.get("following_url", "")
                ).strip()
                if fallback_target_url and fallback_target_url != target_url:
                    target_url = fallback_target_url
                    following_url = fallback_following_url
                    await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    await page.wait_for_timeout(1_500)
                else:
                    if await self._open_instagram_profile_from_nav(page):
                        if await self._open_instagram_following(page):
                            return True
                    await page.goto(self.definition.home_url, wait_until="domcontentloaded", timeout=60_000)
                    self._set_prepare_status(
                        "error",
                        "Instagram 個人頁無法使用，已回首頁重新等待自動解析。",
                    )
                    return False
            if await self._open_instagram_following(page):
                return True

            if following_url:
                await page.wait_for_timeout(1_500)
                if await self._open_instagram_following(page):
                    return True

            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1_500)
            if await self._open_instagram_following(page):
                return True

            self._set_prepare_status(
                "error",
                "Instagram 已登入，但無法自動開啟「追蹤中」名單；稍後會自動重試。",
            )
            return False

        await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        state = await self._wait_for_following_ready(page)
        if state.get("ready") is True:
            return True
        self._set_prepare_status(
            "error",
            "X 已登入，但無法自動開啟追蹤名單；稍後會自動重試。",
        )
        return False

    async def prepare_following_scan(self, page: Any) -> bool:
        if self.definition.id == "instagram":
            cookie_state = await self._instagram_cookie_session_state(page)
            if cookie_state.get("logged_in") is True:
                self._set_prepare_status(
                    "ready",
                    "Instagram Cookie 登入可用，使用 Cookie 掃描追蹤名單",
                )
                return True

        return await self._prepare_following_scan_with_dom(page)

    async def _scan_instagram_following_with_cookies(
        self,
        page: Any,
        max_pages: int,
        filter_terms: Iterable[str],
        retained_account_ids: Iterable[str],
    ) -> list[dict[str, Any]] | None:
        session_state = await self._instagram_cookie_session_state(page)
        user_id = str(session_state.get("user_id", "")).strip()
        if not user_id:
            return None

        started_at = monotonic()
        discovered: dict[str, dict[str, Any]] = {}
        observed_ids: set[str] = set()
        filtered_accounts: dict[str, dict[str, Any]] = {}
        retained_ids = {
            str(account_id).strip()
            for account_id in retained_account_ids
            if str(account_id).strip()
        }
        rounds = 0
        completed_scan = False
        next_max_id = ""
        page_limit = max(1, min(120, max_pages))

        for _ in range(page_limit):
            rounds += 1
            query = {"count": INSTAGRAM_API_PAGE_SIZE}
            if next_max_id:
                query["max_id"] = next_max_id
            url = (
                f"https://www.instagram.com/api/v1/friendships/{user_id}/following/"
                f"?{urlencode(query)}"
            )
            payload = await self._instagram_request_json(page, url)
            if payload is None:
                return None
            users = payload.get("users", [])
            if not isinstance(users, list):
                return None
            for user in users:
                normalized = self._normalize_instagram_api_user(user)
                if normalized is None:
                    continue
                account_id = str(normalized["account_id"])
                observed_ids.add(account_id)
                accepted, reason = (
                    (True, "手動還原保留")
                    if account_id in retained_ids
                    else is_star_candidate_account(normalized, filter_terms)
                )
                if not accepted:
                    filtered_accounts[account_id] = {
                        **normalized,
                        "filter_reason": reason,
                        "filter_source": (
                            "manual" if reason.startswith("自訂篩選") else "automatic"
                        ),
                    }
                    discovered.pop(account_id, None)
                    continue
                if normalized.get("verified") is True:
                    filtered_accounts.pop(account_id, None)
                elif account_id in filtered_accounts:
                    continue
                existing = discovered.get(account_id)
                if existing is None:
                    discovered[account_id] = normalized
                else:
                    self._merge_account(existing, normalized)

            next_max_id = str(payload.get("next_max_id", "") or "").strip()
            if not next_max_id:
                completed_scan = True
                break

        self.last_scan_stats = {
            "observed": len(observed_ids),
            "accepted": len(discovered),
            "filtered": len(filtered_accounts),
            "filtered_account_ids": sorted(filtered_accounts),
            "filtered_accounts": sorted(
                filtered_accounts.values(),
                key=lambda account: str(account["handle"]).casefold(),
            ),
            "rounds": rounds,
            "completed": completed_scan,
            "duration_ms": round((monotonic() - started_at) * 1000),
            "reset_to_start": False,
            "method": "cookie_api",
        }
        return sorted(
            discovered.values(),
            key=lambda account: str(account["handle"]).casefold(),
        )

    async def scan_following(
        self,
        page: Any,
        max_scrolls: int = 160,
        filter_terms: Iterable[str] = (),
        retained_account_ids: Iterable[str] = (),
        reset_to_start: bool = True,
    ) -> list[dict[str, Any]]:
        if self.definition.id == "instagram":
            cookie_accounts = await self._scan_instagram_following_with_cookies(
                page,
                max_scrolls,
                filter_terms,
                retained_account_ids,
            )
            if cookie_accounts is not None:
                return cookie_accounts
            if not await self._prepare_following_scan_with_dom(page):
                return []

        started_at = monotonic()
        did_reset_to_start = False
        if reset_to_start:
            did_reset_to_start = bool(
                await page.evaluate(RESET_FOLLOWING_SCROLL_SCRIPT, self.definition.id)
            )
        if did_reset_to_start:
            await page.wait_for_timeout(350)
        discovered: dict[str, dict[str, Any]] = {}
        observed_ids: set[str] = set()
        filtered_accounts: dict[str, dict[str, Any]] = {}
        retained_ids = {
            str(account_id).strip()
            for account_id in retained_account_ids
            if str(account_id).strip()
        }
        stalled_rounds = 0
        end_rounds = 0
        last_position = -1
        rounds = 0
        completed_scan = False
        for _ in range(max(1, min(300, max_scrolls))):
            rounds += 1
            before_observed_count = len(observed_ids)
            scan_result = await page.evaluate(FOLLOWING_SCAN_SCRIPT, self.definition.id)
            if isinstance(scan_result, dict):
                accounts = scan_result.get("accounts", [])
                scroll_state = scan_result.get("scroll", {})
            else:
                accounts = scan_result
                scroll_state = await page.evaluate(SCROLL_SCRIPT, self.definition.id)
            for account in accounts if isinstance(accounts, list) else []:
                normalized = self._normalize_scanned_account(account)
                if normalized is None:
                    continue
                account_id = str(normalized["account_id"])
                observed_ids.add(account_id)
                accepted, reason = (
                    (True, "手動還原保留")
                    if account_id in retained_ids
                    else is_star_candidate_account(normalized, filter_terms)
                )
                if not accepted:
                    filtered_accounts[account_id] = {
                        **normalized,
                        "filter_reason": reason,
                        "filter_source": (
                            "manual" if reason.startswith("自訂篩選") else "automatic"
                        ),
                    }
                    discovered.pop(account_id, None)
                    continue
                if normalized.get("verified") is True:
                    filtered_accounts.pop(account_id, None)
                elif account_id in filtered_accounts:
                    continue
                existing = discovered.get(account_id)
                if existing is None:
                    discovered[account_id] = normalized
                else:
                    self._merge_account(existing, normalized)

            moved = bool(
                scroll_state.get("moved", False)
                if isinstance(scroll_state, dict)
                else scroll_state
            )
            position = int(
                scroll_state.get("position", last_position)
                if isinstance(scroll_state, dict)
                else last_position
            )
            maximum = int(
                scroll_state.get("maximum", position)
                if isinstance(scroll_state, dict)
                else position
            )
            at_end = bool(
                scroll_state.get("at_end", position >= maximum - 2)
                if isinstance(scroll_state, dict)
                else position >= maximum - 2
            )
            added_observed = len(observed_ids) - before_observed_count
            stalled_rounds = (
                stalled_rounds + 1
                if added_observed == 0 and (not moved or position == last_position)
                else 0
            )
            end_rounds = end_rounds + 1 if at_end and added_observed == 0 else 0
            last_position = position

            if end_rounds >= 3:
                completed_scan = True
                break
            if stalled_rounds >= 5:
                break
            await page.wait_for_timeout(
                550 if not moved else 400 if added_observed == 0 else 250
            )
        self.last_scan_stats = {
            "observed": len(observed_ids),
            "accepted": len(discovered),
            "filtered": len(filtered_accounts),
            "filtered_account_ids": sorted(filtered_accounts),
            "filtered_accounts": sorted(
                filtered_accounts.values(),
                key=lambda account: str(account["handle"]).casefold(),
            ),
            "rounds": rounds,
            "completed": completed_scan,
            "duration_ms": round((monotonic() - started_at) * 1000),
            "reset_to_start": did_reset_to_start,
        }
        return sorted(
            discovered.values(),
            key=lambda account: str(account["handle"]).casefold(),
        )

    async def discover_posts(self, page: Any, profile_url: str, limit: int) -> list[dict[str, Any]]:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(1_500)
        discovered: dict[str, dict[str, Any]] = {}
        for _ in range(12):
            posts = await page.evaluate(DISCOVER_POSTS_SCRIPT, self.definition.id)
            for post in posts if isinstance(posts, list) else []:
                post_url = str(post.get("post_url", "")).strip()
                if post_url:
                    discovered[post_url] = {
                        "post_url": post_url,
                        "text": str(post.get("text", "")).strip(),
                        "published_at": str(post.get("published_at", "")).strip(),
                    }
            if len(discovered) >= limit:
                break
            await page.evaluate("() => window.scrollBy(0, Math.max(window.innerHeight * 0.85, 600))")
            await page.wait_for_timeout(850)
        return list(discovered.values())[:limit]

    async def inspect_post(self, page: Any, post: dict[str, Any]) -> dict[str, Any]:
        await page.goto(str(post["post_url"]), wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(1_300)
        inspected = await page.evaluate(INSPECT_POST_SCRIPT, self.definition.id)
        output = dict(post)
        if isinstance(inspected, dict):
            output.update(inspected)
        output["post_url"] = str(post["post_url"])
        return output


def get_adapter(platform: str) -> PlatformAdapter:
    definition = PLATFORMS.get(platform)
    if definition is None:
        raise ValueError(f"不支援的平台：{platform}")
    return PlatformAdapter(definition)
