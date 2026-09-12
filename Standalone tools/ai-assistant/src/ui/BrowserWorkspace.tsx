import type { BrowserController } from './browserController'

type BrowserWorkspaceProps = {
  browser: BrowserController
  busyAction: string
}

export function BrowserWorkspace({ browser, busyAction }: BrowserWorkspaceProps) {
  const busy = Boolean(busyAction)
  return (
    <aside className="nexus-column nexus-column--right">
      <section className="nexus-surface">
        <div className="nexus-section-head">
          <span>內建瀏覽器</span>
          <strong>{browser.browserSession ? '已開啟' : '待命'}</strong>
        </div>
        <p>輸入網址後使用下方按鈕開啟或導航。瀏覽器會以嵌入視窗顯示。</p>
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <input
            type="text"
            value={browser.browserUrl}
            onChange={(event) => browser.setBrowserUrl(event.target.value)}
            placeholder="https://www.google.com"
            style={{ flex: 1 }}
            disabled={busy}
          />
          <button
            type="button"
            onClick={() => void browser.openBrowser()}
            disabled={busy}
          >
            開啟
          </button>
          <button
            type="button"
            onClick={() => void browser.navigateBrowser()}
            disabled={busy || !browser.browserSession}
          >
            導航
          </button>
          <button
            type="button"
            className="nexus-danger"
            onClick={() => void browser.closeBrowser()}
            disabled={!browser.browserSession}
          >
            關閉
          </button>
        </div>
      </section>
    </aside>
  )
}
