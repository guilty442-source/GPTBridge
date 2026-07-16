import { StatusBar } from 'expo-status-bar'
import { useMemo, useState } from 'react'
import {
  ActivityIndicator,
  Alert,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  RefreshControl,
  SafeAreaView,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native'

import type {
  InvestmentMobileSnapshot,
  MobileHolding,
  RiskNotice,
} from './src/api/contracts'
import { useInvestmentPlatform } from './src/hooks/useInvestmentPlatform'

type TabKey = 'overview' | 'holdings' | 'alerts' | 'assistant'

const TABS: Array<{ key: TabKey; label: string; glyph: string }> = [
  { key: 'overview', label: '總覽', glyph: '⌂' },
  { key: 'holdings', label: '持股', glyph: '▥' },
  { key: 'alerts', label: '警示', glyph: '!' },
  { key: 'assistant', label: '本地 AI', glyph: '✦' },
]

function display(value: unknown, fallback = '-'): string {
  const normalized = String(value ?? '').trim()
  return normalized || fallback
}

function amount(value: unknown): string {
  const parsed = Number(value)
  return Number.isFinite(parsed)
    ? parsed.toLocaleString('zh-TW', { maximumFractionDigits: 2 })
    : '-'
}

function percentage(value: unknown): string {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${amount(parsed)}%` : '-'
}

function ConnectionScreen({
  busy,
  message,
  onPair,
}: {
  busy: boolean
  message: string
  onPair: (baseUrl: string, code: string) => Promise<void>
}) {
  const [baseUrl, setBaseUrl] = useState('')
  const [code, setCode] = useState('')

  const submit = async () => {
    if (!baseUrl.trim() || !code.trim()) {
      Alert.alert('資料不足', '請輸入電腦端顯示的連線網址與配對碼。')
      return
    }
    try {
      await onPair(baseUrl, code)
    } catch (error) {
      Alert.alert('配對失敗', error instanceof Error ? error.message : '請稍後再試。')
    }
  }

  return (
    <KeyboardAvoidingView style={styles.connectionPage}>
      <View style={styles.brandMark}>
        <Text style={styles.brandGlyph}>投</Text>
      </View>
      <Text style={styles.connectionEyebrow}>GPTBRIDGE ANDROID</Text>
      <Text style={styles.connectionTitle}>AI投資管家</Text>
      <Text style={styles.connectionLead}>
        Android 原生程式，與電腦端使用同一份持股、風險與分析資料。
      </Text>

      <View style={styles.connectionCard}>
        <Text style={styles.fieldLabel}>電腦端連線網址</Text>
        <TextInput
          value={baseUrl}
          onChangeText={setBaseUrl}
          placeholder="例如 http://192.168.1.20:18765"
          placeholderTextColor="#89938e"
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          style={styles.input}
          editable={!busy}
        />
        <Text style={styles.fieldLabel}>一次性配對碼</Text>
        <TextInput
          value={code}
          onChangeText={(value) => setCode(value.toUpperCase())}
          placeholder="XXXXXXXX"
          placeholderTextColor="#89938e"
          autoCapitalize="characters"
          autoCorrect={false}
          maxLength={12}
          style={[styles.input, styles.codeInput]}
          editable={!busy}
        />
        <Pressable
          accessibilityRole="button"
          onPress={() => void submit()}
          disabled={busy}
          style={({ pressed }) => [
            styles.primaryButton,
            pressed && styles.buttonPressed,
            busy && styles.buttonDisabled,
          ]}
        >
          {busy ? (
            <ActivityIndicator color="#ffffff" />
          ) : (
            <Text style={styles.primaryButtonText}>安全配對此手機</Text>
          )}
        </Pressable>
      </View>
      <Text style={styles.securityNote}>
        配對憑證只保存在 Android 系統加密儲存區；外部網路僅接受 HTTPS。
      </Text>
      <Text style={styles.connectionStatus}>{message}</Text>
    </KeyboardAvoidingView>
  )
}

function Metric({
  label,
  value,
  emphasis = false,
}: {
  label: string
  value: string
  emphasis?: boolean
}) {
  return (
    <View style={[styles.metric, emphasis && styles.metricEmphasis]}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={[styles.metricValue, emphasis && styles.metricValueEmphasis]}>
        {value}
      </Text>
    </View>
  )
}

function Overview({ snapshot }: { snapshot: InvestmentMobileSnapshot }) {
  const state = snapshot.state
  const analytics = snapshot.analytics || {}
  const diagnostics = snapshot.diagnostics?.local_ai || {}
  const status = state.local_ai_product_status || {}
  return (
    <View style={styles.sectionStack}>
      <View style={styles.scoreCard}>
        <View>
          <Text style={styles.scoreEyebrow}>投資健康分數</Text>
          <Text style={styles.scoreValue}>
            {typeof status.score === 'number' ? status.score : '-'}
          </Text>
        </View>
        <View style={styles.scoreCopy}>
          <Text style={styles.scoreState}>
            {display(status.state_label || diagnostics.state_label, '等待資料')}
          </Text>
          <Text style={styles.scoreDetail}>
            報價 {display(status.quote_health_label || diagnostics.quote_health_label)}
          </Text>
        </View>
      </View>
      <View style={styles.metricGrid}>
        <Metric
          label="組合市值"
          value={amount(analytics.performance?.current_value)}
          emphasis
        />
        <Metric
          label="未實現損益"
          value={amount(analytics.performance?.unrealized_pnl)}
        />
        <Metric
          label="單日 VaR 95%"
          value={percentage(analytics.risk?.var_95_one_day_percent)}
        />
        <Metric
          label="最大回撤"
          value={percentage(analytics.risk?.max_drawdown_percent)}
        />
        <Metric
          label="持股數"
          value={String(
            state.portfolio?.holding_count || state.holdings?.length || 0
          )}
        />
        <Metric
          label="未確認警示"
          value={String(analytics.alerts?.unacknowledged_count || 0)}
        />
      </View>
      <View style={styles.infoCard}>
        <Text style={styles.cardTitle}>共用資料來源</Text>
        <Text style={styles.infoLine}>
          {display(state.portfolio?.file_name, '電腦端尚未載入持股')}
        </Text>
        <Text style={styles.infoMuted}>
          電腦端是唯一資料來源；Android 端即時讀取相同資料，不建立第二份帳本。
        </Text>
      </View>
    </View>
  )
}

function HoldingRow({ holding }: { holding: MobileHolding }) {
  return (
    <View style={styles.listCard}>
      <View style={styles.listCardHead}>
        <View style={styles.symbolBadge}>
          <Text style={styles.symbolBadgeText}>
            {display(holding.symbol).slice(0, 5)}
          </Text>
        </View>
        <View style={styles.listCardTitleBlock}>
          <Text style={styles.listCardTitle}>{display(holding.symbol)}</Text>
          <Text style={styles.listCardSub}>
            {display(holding.name || holding.market)}
          </Text>
        </View>
        <Text style={styles.listCardAmount}>
          {amount(holding.quantity)}
        </Text>
      </View>
      <Text style={styles.listCardDetail}>
        平均成本 {amount(holding.average_cost)} {display(holding.currency, '')} ·{' '}
        {display(holding.asset_type)}
      </Text>
    </View>
  )
}

function Holdings({ holdings }: { holdings: MobileHolding[] }) {
  if (!holdings.length) {
    return <EmptyState text="請先在電腦端匯入持股資料。" />
  }
  return (
    <View style={styles.sectionStack}>
      {holdings.map((holding, index) => (
        <HoldingRow
          key={`${holding.symbol || 'holding'}-${index}`}
          holding={holding}
        />
      ))}
    </View>
  )
}

function NoticeCard({
  item,
  kind,
}: {
  item: RiskNotice
  kind: 'warning' | 'action'
}) {
  return (
    <View
      style={[
        styles.noticeCard,
        kind === 'warning' ? styles.warningCard : styles.actionCard,
      ]}
    >
      <Text style={styles.noticeMeta}>
        {display(item.symbol || item.severity || item.risk_level_label)}
      </Text>
      <Text style={styles.noticeTitle}>
        {display(item.title || item.code, kind === 'warning' ? '風險警示' : '行動')}
      </Text>
      <Text style={styles.noticeBody}>
        {display(item.detail || item.action || item.due)}
      </Text>
    </View>
  )
}

function AlertsView({
  warnings,
  actions,
}: {
  warnings: RiskNotice[]
  actions: RiskNotice[]
}) {
  if (!warnings.length && !actions.length) {
    return <EmptyState text="目前沒有風險警示或待辦行動。" />
  }
  return (
    <View style={styles.sectionStack}>
      <Text style={styles.groupTitle}>風險警示</Text>
      {warnings.map((item, index) => (
        <NoticeCard key={`warning-${index}`} item={item} kind="warning" />
      ))}
      <Text style={styles.groupTitle}>行動計畫</Text>
      {actions.map((item, index) => (
        <NoticeCard key={`action-${index}`} item={item} kind="action" />
      ))}
    </View>
  )
}

function Assistant({
  busy,
  onQueue,
}: {
  busy: boolean
  onQueue: (instruction: string) => Promise<string>
}) {
  const [instruction, setInstruction] = useState('')
  const submit = async () => {
    if (!instruction.trim()) return
    try {
      const message = await onQueue(instruction)
      setInstruction('')
      Alert.alert('已送達電腦端', message)
    } catch (error) {
      Alert.alert('命令失敗', error instanceof Error ? error.message : '請稍後再試。')
    }
  }
  return (
    <View style={styles.sectionStack}>
      <View style={styles.aiCard}>
        <Text style={styles.cardTitle}>電腦端本地 AI</Text>
        <Text style={styles.infoMuted}>
          手機只排入命令；分析仍在電腦端執行並寫回同一份投資資料。
        </Text>
        <TextInput
          value={instruction}
          onChangeText={setInstruction}
          placeholder="例如：更新全部持股評分，列出重大風險與今日行動"
          placeholderTextColor="#89938e"
          multiline
          textAlignVertical="top"
          style={styles.commandInput}
          editable={!busy}
        />
        <Pressable
          accessibilityRole="button"
          onPress={() => void submit()}
          disabled={busy || !instruction.trim()}
          style={({ pressed }) => [
            styles.primaryButton,
            pressed && styles.buttonPressed,
            (busy || !instruction.trim()) && styles.buttonDisabled,
          ]}
        >
          <Text style={styles.primaryButtonText}>
            {busy ? '傳送中…' : '傳送到電腦端'}
          </Text>
        </Pressable>
      </View>
      <View style={styles.boundaryCard}>
        <Text style={styles.boundaryTitle}>手機能力邊界</Text>
        <Text style={styles.boundaryLine}>可用：總覽、持股、警示、行動與 AI 命令</Text>
        <Text style={styles.boundaryLine}>電腦限定：匯入、刪除、交易帳本與安全設定</Text>
      </View>
    </View>
  )
}

function EmptyState({ text }: { text: string }) {
  return (
    <View style={styles.emptyState}>
      <Text style={styles.emptyGlyph}>◇</Text>
      <Text style={styles.emptyText}>{text}</Text>
    </View>
  )
}

export default function App() {
  const platform = useInvestmentPlatform()
  const [tab, setTab] = useState<TabKey>('overview')
  const snapshot = platform.snapshot
  const syncedLabel = useMemo(() => {
    if (!platform.lastSyncedAt) return '等待同步'
    return new Date(platform.lastSyncedAt).toLocaleTimeString('zh-TW', {
      hour12: false,
    })
  }, [platform.lastSyncedAt])

  if (!platform.connection || !snapshot) {
    return (
      <SafeAreaView style={styles.safeArea}>
        <StatusBar style="light" />
        <ConnectionScreen
          busy={platform.busy}
          message={platform.message}
          onPair={platform.pair}
        />
      </SafeAreaView>
    )
  }

  const state = snapshot.state
  return (
    <SafeAreaView style={styles.safeArea}>
      <StatusBar style="light" />
      <View style={styles.app}>
        <View style={styles.topbar}>
          <View>
            <Text style={styles.topbarEyebrow}>ANDROID 即時共用</Text>
            <Text style={styles.topbarTitle}>AI投資管家</Text>
          </View>
          <Pressable
            accessibilityRole="button"
            onPress={() =>
              Alert.alert('解除此手機？', '只會清除此手機的配對憑證。', [
                { text: '取消', style: 'cancel' },
                {
                  text: '解除',
                  style: 'destructive',
                  onPress: () => void platform.disconnect(),
                },
              ])
            }
            style={styles.disconnectButton}
          >
            <Text style={styles.disconnectText}>解除</Text>
          </Pressable>
        </View>
        <View style={styles.liveStrip}>
          <View style={styles.liveDot} />
          <Text style={styles.liveText}>{platform.message}</Text>
          <Text style={styles.liveClock}>{syncedLabel}</Text>
        </View>
        <ScrollView
          style={styles.content}
          contentContainerStyle={styles.contentContainer}
          refreshControl={
            <RefreshControl
              refreshing={platform.busy}
              onRefresh={() => void platform.refresh()}
              tintColor="#2f8068"
            />
          }
        >
          {tab === 'overview' ? <Overview snapshot={snapshot} /> : null}
          {tab === 'holdings' ? (
            <Holdings holdings={state.holdings || []} />
          ) : null}
          {tab === 'alerts' ? (
            <AlertsView
              warnings={state.local_ai_risk_warnings || []}
              actions={state.local_ai_action_plan || []}
            />
          ) : null}
          {tab === 'assistant' ? (
            <Assistant busy={platform.busy} onQueue={platform.queueCommand} />
          ) : null}
        </ScrollView>
        <View style={styles.tabbar}>
          {TABS.map((item) => {
            const active = tab === item.key
            return (
              <Pressable
                key={item.key}
                accessibilityRole="tab"
                accessibilityState={{ selected: active }}
                onPress={() => setTab(item.key)}
                style={[styles.tabButton, active && styles.tabButtonActive]}
              >
                <Text style={[styles.tabGlyph, active && styles.tabTextActive]}>
                  {item.glyph}
                </Text>
                <Text style={[styles.tabLabel, active && styles.tabTextActive]}>
                  {item.label}
                </Text>
              </Pressable>
            )
          })}
        </View>
      </View>
    </SafeAreaView>
  )
}

const colors = {
  ink: '#17241f',
  muted: '#66736d',
  green: '#245e4d',
  greenDark: '#173f36',
  mint: '#d9eee7',
  paper: '#f3f5f1',
  surface: '#ffffff',
  line: '#d8dfda',
  warning: '#8d3c2d',
  warningSoft: '#f9e5df',
  blue: '#315f87',
  blueSoft: '#e4edf5',
}

const styles = StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: colors.greenDark,
    paddingTop: Platform.OS === 'android' ? 18 : 0,
  },
  app: { flex: 1, backgroundColor: colors.paper },
  connectionPage: {
    flex: 1,
    justifyContent: 'center',
    paddingHorizontal: 24,
    paddingBottom: 28,
    backgroundColor: colors.greenDark,
  },
  brandMark: {
    width: 58,
    height: 58,
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: 18,
    marginBottom: 20,
    backgroundColor: '#cce9df',
  },
  brandGlyph: { color: colors.greenDark, fontSize: 27, fontWeight: '900' },
  connectionEyebrow: {
    color: '#8cc7b5',
    fontSize: 11,
    fontWeight: '900',
    letterSpacing: 1.8,
  },
  connectionTitle: {
    marginTop: 5,
    color: '#ffffff',
    fontSize: 34,
    fontWeight: '900',
  },
  connectionLead: {
    marginTop: 8,
    color: '#c4d8d1',
    fontSize: 15,
    lineHeight: 23,
  },
  connectionCard: {
    gap: 9,
    marginTop: 26,
    borderRadius: 22,
    padding: 18,
    backgroundColor: colors.surface,
  },
  fieldLabel: {
    marginTop: 3,
    color: colors.muted,
    fontSize: 12,
    fontWeight: '800',
  },
  input: {
    minHeight: 48,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 12,
    paddingHorizontal: 13,
    color: colors.ink,
    backgroundColor: '#f9faf8',
    fontSize: 15,
  },
  codeInput: { letterSpacing: 3, fontSize: 18, fontWeight: '900' },
  primaryButton: {
    minHeight: 50,
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: 13,
    marginTop: 5,
    paddingHorizontal: 16,
    backgroundColor: colors.green,
  },
  primaryButtonText: { color: '#ffffff', fontSize: 15, fontWeight: '900' },
  buttonPressed: { opacity: 0.82 },
  buttonDisabled: { opacity: 0.5 },
  securityNote: {
    marginTop: 16,
    color: '#a8c6bb',
    fontSize: 12,
    lineHeight: 18,
    textAlign: 'center',
  },
  connectionStatus: {
    marginTop: 8,
    color: '#d4e7e0',
    fontSize: 12,
    textAlign: 'center',
  },
  topbar: {
    minHeight: 72,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 18,
    backgroundColor: colors.greenDark,
  },
  topbarEyebrow: {
    color: '#8cc7b5',
    fontSize: 10,
    fontWeight: '900',
    letterSpacing: 1.4,
  },
  topbarTitle: {
    marginTop: 2,
    color: '#ffffff',
    fontSize: 23,
    fontWeight: '900',
  },
  disconnectButton: {
    borderWidth: 1,
    borderColor: '#52776b',
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 8,
  },
  disconnectText: { color: '#d8e9e3', fontSize: 12, fontWeight: '800' },
  liveStrip: {
    minHeight: 38,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 7,
    paddingHorizontal: 18,
    borderBottomWidth: 1,
    borderBottomColor: colors.line,
    backgroundColor: '#e7f2ed',
  },
  liveDot: { width: 8, height: 8, borderRadius: 4, backgroundColor: '#2b9a70' },
  liveText: { flex: 1, color: colors.green, fontSize: 11, fontWeight: '800' },
  liveClock: { color: colors.muted, fontSize: 11 },
  content: { flex: 1 },
  contentContainer: { padding: 14, paddingBottom: 30 },
  sectionStack: { gap: 11 },
  scoreCard: {
    minHeight: 132,
    flexDirection: 'row',
    alignItems: 'center',
    gap: 18,
    borderRadius: 20,
    padding: 20,
    backgroundColor: colors.greenDark,
  },
  scoreEyebrow: { color: '#9bc9ba', fontSize: 11, fontWeight: '800' },
  scoreValue: { marginTop: 4, color: '#ffffff', fontSize: 52, fontWeight: '900' },
  scoreCopy: { flex: 1, gap: 5 },
  scoreState: { color: '#ffffff', fontSize: 17, fontWeight: '900' },
  scoreDetail: { color: '#bcd6cd', fontSize: 12, lineHeight: 18 },
  metricGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 9 },
  metric: {
    width: '48%',
    minHeight: 86,
    justifyContent: 'space-between',
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 16,
    padding: 13,
    backgroundColor: colors.surface,
  },
  metricEmphasis: { borderColor: '#9bc9ba', backgroundColor: '#edf7f3' },
  metricLabel: { color: colors.muted, fontSize: 11, fontWeight: '800' },
  metricValue: { color: colors.ink, fontSize: 19, fontWeight: '900' },
  metricValueEmphasis: { color: colors.green },
  infoCard: {
    gap: 7,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 16,
    padding: 15,
    backgroundColor: colors.surface,
  },
  cardTitle: { color: colors.ink, fontSize: 16, fontWeight: '900' },
  infoLine: { color: colors.green, fontSize: 14, fontWeight: '800' },
  infoMuted: { color: colors.muted, fontSize: 12, lineHeight: 19 },
  listCard: {
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 16,
    padding: 14,
    backgroundColor: colors.surface,
  },
  listCardHead: { flexDirection: 'row', alignItems: 'center', gap: 11 },
  symbolBadge: {
    width: 46,
    height: 46,
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: 13,
    backgroundColor: colors.mint,
  },
  symbolBadgeText: { color: colors.greenDark, fontSize: 11, fontWeight: '900' },
  listCardTitleBlock: { flex: 1 },
  listCardTitle: { color: colors.ink, fontSize: 16, fontWeight: '900' },
  listCardSub: { marginTop: 2, color: colors.muted, fontSize: 12 },
  listCardAmount: { color: colors.green, fontSize: 17, fontWeight: '900' },
  listCardDetail: { marginTop: 10, color: colors.muted, fontSize: 12 },
  groupTitle: {
    marginTop: 3,
    marginBottom: -2,
    color: colors.ink,
    fontSize: 15,
    fontWeight: '900',
  },
  noticeCard: { gap: 5, borderRadius: 16, padding: 15 },
  warningCard: { backgroundColor: colors.warningSoft },
  actionCard: { backgroundColor: colors.blueSoft },
  noticeMeta: { color: colors.muted, fontSize: 10, fontWeight: '900' },
  noticeTitle: { color: colors.ink, fontSize: 15, fontWeight: '900' },
  noticeBody: { color: colors.muted, fontSize: 12, lineHeight: 19 },
  aiCard: {
    gap: 11,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 18,
    padding: 17,
    backgroundColor: colors.surface,
  },
  commandInput: {
    minHeight: 145,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 14,
    padding: 13,
    color: colors.ink,
    backgroundColor: '#f8faf8',
    fontSize: 15,
    lineHeight: 22,
  },
  boundaryCard: {
    gap: 5,
    borderRadius: 16,
    padding: 15,
    backgroundColor: '#e9ece8',
  },
  boundaryTitle: { color: colors.ink, fontSize: 13, fontWeight: '900' },
  boundaryLine: { color: colors.muted, fontSize: 12, lineHeight: 18 },
  emptyState: {
    minHeight: 220,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 12,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 18,
    backgroundColor: colors.surface,
  },
  emptyGlyph: { color: '#86a399', fontSize: 34 },
  emptyText: { color: colors.muted, fontSize: 14 },
  tabbar: {
    minHeight: 68,
    flexDirection: 'row',
    alignItems: 'stretch',
    borderTopWidth: 1,
    borderTopColor: colors.line,
    paddingHorizontal: 8,
    paddingBottom: 4,
    backgroundColor: colors.surface,
  },
  tabButton: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 2,
    borderRadius: 12,
    marginVertical: 5,
  },
  tabButtonActive: { backgroundColor: '#e5f1ec' },
  tabGlyph: { color: colors.muted, fontSize: 17, fontWeight: '900' },
  tabLabel: { color: colors.muted, fontSize: 10, fontWeight: '800' },
  tabTextActive: { color: colors.greenDark },
})
