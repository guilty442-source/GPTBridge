import { useToolRunner, toolWindowStyles as styles } from './toolWindowRunner'
import { VaultlyDownloadCenter } from './VaultlyDownloadCenter'

export function VaultlyWindowApp() {
  const { sendCommand, socketStatus } = useToolRunner('vaultly')

  return (
    <main style={{ ...styles.app, padding: '20px' }}>
      <section style={{ ...styles.card, maxWidth: '1280px', padding: '24px' }}>
        <header style={styles.header}>
          <div>
            <div style={styles.kicker}>GPTBridge Tool</div>
            <h1 style={styles.title}>影音下載自動化</h1>
            <p style={styles.muted}>Vaultly 專屬下載中心。</p>
          </div>
          <span style={styles.badge}>{socketStatus}</span>
        </header>
        <VaultlyDownloadCenter sendCommand={sendCommand} />
      </section>
    </main>
  )
}
