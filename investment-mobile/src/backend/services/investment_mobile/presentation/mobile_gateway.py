from __future__ import annotations

import json
import secrets
import socket
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..domain.platform_contract import mobile_platform_contract

PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULT_PORT = 18765
DEFAULT_PAIRING_TTL_HOURS = 1.0
REQUEST_WINDOW_SECONDS = 60.0
MAX_REQUESTS_PER_WINDOW = 120
SESSION_TTL_HOURS = 12.0
SESSION_IDLE_SECONDS = 30 * 60
SnapshotProvider = Callable[[], dict[str, Any]]
CommandScheduler = Callable[[str], dict[str, Any]]
RemoteUrlProvider = Callable[[], str]


def normalize_remote_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("遠端橋接 URL 必須是 http:// 或 https:// 開頭。")
    hostname = str(parsed.hostname or "").casefold()
    if parsed.scheme != "https" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("非本機的遠端橋接 URL 必須使用 https://。")
    return raw.rstrip("/")


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for item in socket.gethostbyname_ex(hostname)[2]:
            addresses.add(item)
    except OSError:
        pass

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            addresses.add(str(probe.getsockname()[0]))
        finally:
            probe.close()
    except OSError:
        pass

    return sorted(
        item
        for item in addresses
        if item and not item.startswith("127.") and not item.startswith("169.254.")
    )


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _mobile_html() -> bytes:
    html = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI投資管家手機同步</title>
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
      <h1>AI投資管家</h1>
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


class MobileSyncGateway:
    def __init__(
        self,
        tool_root: Path,
        snapshot_provider: SnapshotProvider,
        command_scheduler: CommandScheduler,
        remote_url_provider: RemoteUrlProvider,
    ) -> None:
        self.tool_root = tool_root
        self.snapshot_provider = snapshot_provider
        self.command_scheduler = command_scheduler
        self.remote_url_provider = remote_url_provider
        self.pairing_ttl_hours = DEFAULT_PAIRING_TTL_HOURS
        self.pairing_code = self._new_pairing_code()
        self.pairing_created_at = datetime.now(timezone.utc)
        self.pairing_expires_at = self.pairing_created_at + timedelta(
            hours=self.pairing_ttl_hours
        )
        self.pairing_revoked_at: datetime | None = None
        self._request_log: dict[str, list[float]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._security_lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.bind_host = "127.0.0.1"

    def start(self, port: int | None = None, *, allow_lan: bool = False) -> None:
        requested_host = "0.0.0.0" if allow_lan else "127.0.0.1"
        if self.httpd is not None and self.bind_host == requested_host:
            return
        if self.httpd is not None:
            self.shutdown()
        self.bind_host = requested_host
        selected_port = DEFAULT_PORT if port is None else max(0, int(port))
        candidates = [selected_port] if selected_port == 0 else list(range(selected_port, selected_port + 20))
        last_error: OSError | None = None
        handler = self._handler_class()
        for candidate in candidates:
            try:
                self.httpd = ThreadingHTTPServer((self.bind_host, candidate), handler)
                self.port = int(self.httpd.server_address[1])
                break
            except OSError as exc:
                last_error = exc
        if self.httpd is None:
            raise OSError(f"手機同步服務啟動失敗：{last_error}")
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="AIInvestmentMobileSync",
            daemon=True,
        )
        self.thread.start()

    def shutdown(self) -> None:
        if self.httpd is None:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.thread = None

    def rotate_pairing_code(self) -> str:
        self.pairing_code = self._new_pairing_code()
        self.pairing_created_at = datetime.now(timezone.utc)
        self.pairing_expires_at = self.pairing_created_at + timedelta(
            hours=self.pairing_ttl_hours
        )
        self.pairing_revoked_at = None
        return self.pairing_code

    def revoke_pairing_code(self) -> None:
        self.pairing_revoked_at = datetime.now(timezone.utc)
        with self._security_lock:
            self._sessions.clear()

    def set_pairing_ttl_hours(self, hours: float) -> None:
        self.pairing_ttl_hours = max(0.25, min(24.0, float(hours)))
        self.pairing_expires_at = self.pairing_created_at + timedelta(
            hours=self.pairing_ttl_hours
        )

    def pairing_active(self) -> bool:
        return self.pairing_revoked_at is None and datetime.now(timezone.utc) < self.pairing_expires_at

    def _rate_limit_allows(self, remote_address: str) -> bool:
        now = time.monotonic()
        address = remote_address or "unknown"
        with self._security_lock:
            attempts = [
                stamp
                for stamp in self._request_log.get(address, [])
                if now - stamp < REQUEST_WINDOW_SECONDS
            ]
            if len(attempts) >= MAX_REQUESTS_PER_WINDOW:
                self._request_log[address] = attempts
                return False
            attempts.append(now)
            self._request_log[address] = attempts
            if len(self._request_log) > 512:
                self._request_log = {
                    key: value
                    for key, value in self._request_log.items()
                    if value and now - value[-1] < REQUEST_WINDOW_SECONDS
                }
        return True

    def pair(
        self,
        supplied: str,
        remote_address: str,
        *,
        client_platform: str = "",
        device_name: str = "",
    ) -> str:
        if not self._rate_limit_allows(remote_address):
            return ""
        if not (
            self.pairing_active()
            and bool(supplied)
            and secrets.compare_digest(supplied, self.pairing_code)
        ):
            return ""
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._security_lock:
            self._sessions[token] = {
                "created_at": now,
                "last_used_at": now,
                "expires_at": now + SESSION_TTL_HOURS * 3600,
                "remote_address": remote_address or "unknown",
                "client_platform": client_platform[:32],
                "device_name": device_name[:80],
            }
        # A desktop pairing code is one-time. Existing device sessions continue
        # until explicitly revoked or their idle/absolute expiry is reached.
        self.pairing_revoked_at = datetime.now(timezone.utc)
        return token

    def authorize_session(self, supplied: str, remote_address: str) -> bool:
        if not supplied or not self._rate_limit_allows(remote_address):
            return False
        now = time.monotonic()
        with self._security_lock:
            session = self._sessions.get(supplied)
            if not session:
                return False
            expired = now >= float(session.get("expires_at") or 0)
            idle = now - float(session.get("last_used_at") or 0) >= SESSION_IDLE_SECONDS
            wrong_device = str(session.get("remote_address") or "") != (
                remote_address or "unknown"
            )
            if expired or idle or wrong_device:
                self._sessions.pop(supplied, None)
                return False
            session["last_used_at"] = now
        return True

    def status(self, *, expose_pairing_code: bool = True) -> dict[str, Any]:
        running = self.httpd is not None and self.port > 0
        pairing_active = self.pairing_active()
        remote_base_url = ""
        remote_url = ""
        try:
            remote_base_url = normalize_remote_url(self.remote_url_provider())
        except ValueError:
            remote_base_url = ""
        if running and remote_base_url:
            remote_url = remote_base_url
        local_urls = (
            [
                f"http://{ip}:{self.port}"
                for ip in local_ipv4_addresses()
            ]
            if running and self.bind_host == "0.0.0.0"
            else []
        )
        loopback_url = (
            f"http://127.0.0.1:{self.port}"
            if running
            else ""
        )
        status = {
            "enabled": running,
            "running": running,
            "mode": (
                "remote_bridge"
                if remote_url
                else "local_network"
                if self.bind_host == "0.0.0.0"
                else "loopback"
            ),
            "mode_label": (
                "不同網路橋接"
                if remote_url
                else "本機/區網"
                if self.bind_host == "0.0.0.0"
                else "僅限本機"
            ),
            "remote_ready": bool(remote_url),
            "remote_status_label": "不同網路已啟用" if remote_url else "不同網路未設定",
            "relay_required": not bool(remote_url),
            "port": self.port,
            "bind_host": self.bind_host,
            "loopback_url": loopback_url,
            "local_urls": local_urls,
            "remote_base_url": remote_base_url,
            "remote_url": remote_url,
            "access_scope": "read_state_and_queue_xingcheng_command",
            "platform": mobile_platform_contract(),
            "session_count": len(self._sessions),
            "session_idle_minutes": int(SESSION_IDLE_SECONDS / 60),
            "pairing_active": pairing_active,
            "pairing_expired": datetime.now(timezone.utc) >= self.pairing_expires_at,
            "pairing_revoked": self.pairing_revoked_at is not None,
            "pairing_created_at": self.pairing_created_at.isoformat(),
            "pairing_expires_at": self.pairing_expires_at.isoformat(),
            "pairing_revoked_at": self.pairing_revoked_at.isoformat() if self.pairing_revoked_at else "",
            "pairing_ttl_hours": self.pairing_ttl_hours,
            "rate_limit": f"{MAX_REQUESTS_PER_WINDOW}/{int(REQUEST_WINDOW_SECONDS)}s per address",
        }
        if expose_pairing_code:
            status["pairing_code"] = self.pairing_code if pairing_active else ""
        return status

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_OPTIONS(self) -> None:
                self._send_empty(204)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path in {"", "/"}:
                    self._send_bytes(200, _mobile_html(), "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/platform":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "platform": mobile_platform_contract(),
                        },
                    )
                    return
                if parsed.path == "/api/state":
                    if not self._authorized():
                        self._send_json(403, {"ok": False, "message": "工作階段無效或已過期"})
                        return
                    try:
                        payload = gateway.snapshot_provider()
                        self._send_json(200, payload)
                    except Exception as exc:
                        self._send_json(500, {"ok": False, "message": str(exc)})
                    return
                self._send_json(404, {"ok": False, "message": "not found"})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/api/pair":
                    payload = self._read_json()
                    session_token = gateway.pair(
                        str(payload.get("code") or "").strip().upper(),
                        str(self.client_address[0]),
                        client_platform=str(
                            payload.get("client_platform") or ""
                        ).strip(),
                        device_name=str(payload.get("device_name") or "").strip(),
                    )
                    if not session_token:
                        self._send_json(403, {"ok": False, "message": "配對碼不正確或已使用"})
                        return
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "session_token": session_token,
                            "expires_in_hours": SESSION_TTL_HOURS,
                            "idle_timeout_minutes": int(SESSION_IDLE_SECONDS / 60),
                            "platform": mobile_platform_contract(),
                        },
                    )
                    return
                if parsed.path != "/api/xingcheng-command":
                    self._send_json(404, {"ok": False, "message": "not found"})
                    return
                payload = self._read_json()
                if not self._authorized():
                    self._send_json(403, {"ok": False, "message": "工作階段無效或已過期"})
                    return
                instruction = str(payload.get("instruction") or "").strip()
                if not instruction:
                    self._send_json(400, {"ok": False, "message": "請輸入本地 AI 命令"})
                    return
                try:
                    result = gateway.command_scheduler(instruction)
                    self._send_json(200, result)
                except Exception as exc:
                    self._send_json(500, {"ok": False, "message": str(exc)})

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _authorized(self) -> bool:
                authorization = str(self.headers.get("Authorization") or "")
                supplied = (
                    authorization[7:].strip()
                    if authorization.casefold().startswith("bearer ")
                    else str(self.headers.get("X-GPTBridge-Mobile-Session") or "")
                )
                return gateway.authorize_session(
                    supplied,
                    str(self.client_address[0]),
                )

            def _read_json(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    length = 0
                if length <= 0 or length > 32768:
                    return {}
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return {}
                return payload if isinstance(payload, dict) else {}

            def _send_empty(self, status: int) -> None:
                self.send_response(status)
                self._headers("text/plain; charset=utf-8")
                self.end_headers()

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                self._send_bytes(status, _json_bytes(payload), "application/json; charset=utf-8")

            def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self._headers(content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _headers(self, content_type: str) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                )

        return Handler

    @staticmethod
    def _new_pairing_code() -> str:
        return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
