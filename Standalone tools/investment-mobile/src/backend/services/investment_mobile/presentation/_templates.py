from __future__ import annotations


def _mobile_html() -> bytes:
    html = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>投資管家手機同步</title>
  <style>
    :root {
      color-scheme: light;
      font-family: "Noto Sans TC", "Segoe UI", system-ui, sans-serif;
      background: #f4f6f8;
      color: #172026;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f4f6f8; }
    main { min-height: 100vh; padding: 12px; }
    header, section {
      border: 1px solid #d8dee5;
      border-radius: 8px;
      background: #fff;
    }
    header { display: grid; gap: 8px; padding: 14px; margin-bottom: 10px; }
    h1 { margin: 0; font-size: 22px; }
    .meta, .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .pill, .tile {
      min-width: 0;
      border: 1px solid #e2e7ec;
      border-radius: 6px;
      padding: 8px;
      background: #fbfcfd;
    }
    .pill span, .tile span, section > span {
      display: block;
      color: #64717d;
      font-size: 12px;
      font-weight: 800;
    }
    .pill strong, .tile strong {
      display: block;
      margin-top: 3px;
      overflow-wrap: anywhere;
      font-size: 15px;
    }
    section { display: grid; gap: 8px; padding: 12px; margin-bottom: 10px; }
    .score {
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr);
      gap: 10px;
      align-items: center;
      border: 1px solid #cfd8e3;
      border-radius: 8px;
      padding: 10px;
      background: #f7f9fb;
    }
    .score strong { font-size: 30px; line-height: 1; }
    .score span { color: #52606c; font-weight: 900; overflow-wrap: anywhere; }
    .list { display: grid; gap: 6px; }
    article {
      border: 1px solid #e2e7ec;
      border-radius: 6px;
      padding: 8px;
      background: #fbfcfd;
    }
    article strong, article span, article p { display: block; margin: 0; overflow-wrap: anywhere; }
    article strong { font-size: 14px; }
    article span, article p { margin-top: 3px; color: #52606c; font-size: 12px; line-height: 1.45; }
    form { display: grid; gap: 8px; }
    input, textarea {
      width: 100%;
      border: 1px solid #c5cdd5;
      border-radius: 6px;
      padding: 10px;
      font: inherit;
    }
    input { min-height: 42px; text-transform: uppercase; letter-spacing: .12em; }
    textarea { min-height: 72px; resize: vertical; }
    button {
      min-height: 38px;
      border: 1px solid #256f5b;
      border-radius: 6px;
      color: #fff;
      background: #256f5b;
      font: inherit;
      font-weight: 900;
    }
    .empty { color: #6b7782; font-size: 13px; }
    .error { color: #8b2525; }
    @media (max-width: 420px) {
      .meta, .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>投資管家</h1>
      <div class="meta">
        <div class="pill"><span>同步狀態</span><strong id="syncState">連線中</strong></div>
        <div class="pill"><span>更新時間</span><strong id="updatedAt">-</strong></div>
      </div>
    </header>
    <section id="pairingPanel">
      <span>安全配對</span>
      <form id="pairingForm">
        <input id="pairingCode" inputmode="text" autocomplete="one-time-code"
          maxlength="12" placeholder="輸入桌面版顯示的配對碼" />
        <button type="submit">建立此裝置工作階段</button>
      </form>
      <p class="empty">配對碼不會放在網址；完成後只在此裝置保存短期工作階段。</p>
    </section>
    <section>
      <div class="score"><strong id="score">-</strong><span id="scoreLabel">等待資料</span></div>
      <div class="grid">
        <div class="tile"><span>持股</span><strong id="holdingCount">0</strong></div>
        <div class="tile"><span>報價</span><strong id="quoteHealth">-</strong></div>
        <div class="tile"><span>風險</span><strong id="riskCount">0 / 0</strong></div>
        <div class="tile"><span>檔案</span><strong id="fileName">-</strong></div>
      </div>
    </section>
    <section>
      <span>組合分析</span>
      <div class="grid">
        <div class="tile"><span>組合市值</span><strong id="portfolioValue">-</strong></div>
        <div class="tile"><span>未實現損益</span><strong id="unrealizedPnl">-</strong></div>
        <div class="tile"><span>單日 VaR 95%</span><strong id="portfolioVar">-</strong></div>
        <div class="tile"><span>最大回撤</span><strong id="maxDrawdown">-</strong></div>
        <div class="tile"><span>未確認警示</span><strong id="alertCount">0</strong></div>
        <div class="tile"><span>配對到期</span><strong id="pairingExpiry">-</strong></div>
      </div>
    </section>
    <section>
      <span>本地 AI 命令</span>
      <form id="commandForm">
        <textarea id="instruction" placeholder="例如：連網 評分 全部持股，列出重大風險與操作策略"></textarea>
        <button type="submit">送出命令</button>
      </form>
    </section>
    <section>
      <span>持倉風險預告</span>
      <div id="warnings" class="list"><p class="empty">尚無資料</p></div>
    </section>
    <section>
      <span>行動計畫</span>
      <div id="actions" class="list"><p class="empty">尚無資料</p></div>
    </section>
    <section>
      <span>持股</span>
      <div id="holdings" class="list"><p class="empty">尚無資料</p></div>
    </section>
  </main>
  <script>
    const sessionToken = () => localStorage.getItem('gptbridgeMobileSyncSession') || '';
    const authHeaders = () => sessionToken()
      ? { 'Authorization': 'Bearer ' + sessionToken() }
      : {};
    const text = (id, value) => { document.getElementById(id).textContent = value || '-'; };
    const list = (id, items, render) => {
      const node = document.getElementById(id);
      node.innerHTML = '';
      if (!items || items.length === 0) {
        const empty = document.createElement('p');
        empty.className = 'empty';
        empty.textContent = '尚無資料';
        node.appendChild(empty);
        return;
      }
      items.forEach((item) => node.appendChild(render(item)));
    };
    const card = (title, sub, body) => {
      const item = document.createElement('article');
      const strong = document.createElement('strong');
      const span = document.createElement('span');
      strong.textContent = title || '-';
      span.textContent = sub || '';
      item.appendChild(strong);
      item.appendChild(span);
      if (body) {
        const p = document.createElement('p');
        p.textContent = body;
        item.appendChild(p);
      }
      return item;
    };
    async function refresh() {
      try {
        const response = await fetch('/api/state', {
          cache: 'no-store',
          headers: authHeaders()
        });
        if (!response.ok) throw new Error(response.status === 403 ? '配對碼不正確' : '同步失敗');
        const payload = await response.json();
        const state = payload.state || {};
        const diagnostics = payload.diagnostics || {};
        const localAi = diagnostics.xingcheng || {};
        const portfolio = state.portfolio || {};
        const status = state.xingcheng_product_status || {};
        const analytics = payload.analytics || {};
        const performance = analytics.performance || {};
        const portfolioRisk = analytics.risk || {};
        const alerts = analytics.alerts || {};
        const formatNumber = (value) => Number.isFinite(Number(value))
          ? Number(value).toLocaleString('zh-TW', { maximumFractionDigits: 2 })
          : '-';
        const formatPercent = (value) => Number.isFinite(Number(value))
          ? `${formatNumber(value)}%`
          : '-';
        text('syncState', payload.sync?.remote_status_label || '已同步');
        text('updatedAt', new Date(payload.generated_at || Date.now()).toLocaleString('zh-TW', { hour12: false }));
        text('score', typeof status.score === 'number' ? String(status.score) : '-');
        text('scoreLabel', status.state_label || localAi.state_label || '等待資料');
        text('holdingCount', String(portfolio.holding_count || (state.holdings || []).length || 0));
        text('quoteHealth', localAi.quote_health_label || status.quote_health_label || '-');
        text('riskCount', String((localAi.warning_count || 0) + ' / ' + (localAi.critical_count || 0)));
        text('fileName', portfolio.file_name || '-');
        text('portfolioValue', formatNumber(performance.current_value));
        text('unrealizedPnl', formatNumber(performance.unrealized_pnl));
        text('portfolioVar', formatPercent(portfolioRisk.var_95_one_day_percent));
        text('maxDrawdown', formatPercent(portfolioRisk.max_drawdown_percent));
        text('alertCount', String(alerts.unacknowledged_count || 0));
        text('pairingExpiry', payload.sync?.pairing_expires_at
          ? new Date(payload.sync.pairing_expires_at).toLocaleString('zh-TW', { hour12: false })
          : '-');
        list('warnings', state.xingcheng_risk_warnings || [], (item) => card(item.title || item.code, item.symbol || item.severity, item.detail || item.action));
        list('actions', state.xingcheng_action_plan || [], (item) => card(item.title || item.symbol, item.due || item.risk_level_label, item.action));
        list('holdings', state.holdings || [], (item) => card(item.symbol, item.name || item.market, `${item.quantity || 0} · ${item.average_cost || '-'} ${item.currency || ''}`));
      } catch (error) {
        text('syncState', error instanceof Error ? error.message : '同步失敗');
        document.getElementById('syncState').className = 'error';
      }
    }
    document.getElementById('pairingForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      const pairingCode = document.getElementById('pairingCode').value.trim().toUpperCase();
      if (!pairingCode) return;
      const response = await fetch('/api/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: pairingCode })
      });
      if (!response.ok) {
        text('syncState', response.status === 403 ? '配對碼不正確或已使用' : '配對失敗');
        return;
      }
      const payload = await response.json();
      localStorage.setItem('gptbridgeMobileSyncSession', payload.session_token || '');
      document.getElementById('pairingCode').value = '';
      await refresh();
    });
    document.getElementById('commandForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      const instruction = document.getElementById('instruction').value.trim();
      if (!instruction) return;
      const response = await fetch('/api/xingcheng-command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ instruction })
      });
      if (response.ok) {
        document.getElementById('instruction').value = '';
        await refresh();
      }
    });
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""
    return html.encode("utf-8")
