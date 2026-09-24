import React, {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useReducer,
} from 'react'

/** §19 InvestmentUIState — display/context state only. This is NEVER
 * the account/holdings/trade authority: after restart everything real
 * is re-fetched from the backend (PostgreSQL authority). */
export type PageId =
  | 'overview'
  | 'tw-equity'
  | 'us-equity'
  | 'fund'
  | 'recommendations'
  | 'autotrade'
  | 'strategies'
  | 'laboratory'
  | 'allocation'
  | 'risk'
  | 'reports'
  | 'import'
  | 'brokers'
  | 'settings'

export type UIState = {
  page: PageId
  market: 'tw' | 'us' | 'fund'
  instrumentId: string
  strategyId: string
  mode: string
  theme: 'light' | 'dark'
  fontScale: number
  sidebarCollapsed: boolean
  aiPanelOpen: boolean
  notices: { id: number; text: string; severity: string; at: number }[]
}

const initial: UIState = {
  page: 'overview',
  market: 'tw',
  instrumentId: '',
  strategyId: '',
  mode: 'ANALYSIS',
  theme: 'light',
  fontScale: 1,
  sidebarCollapsed: false,
  aiPanelOpen: true,
  notices: [],
}

type Action =
  | { type: 'page'; page: PageId }
  | { type: 'market'; market: UIState['market'] }
  | { type: 'instrument'; instrumentId: string }
  | { type: 'strategy'; strategyId: string }
  | { type: 'mode'; mode: string }
  | { type: 'theme'; theme: UIState['theme'] }
  | { type: 'fontScale'; fontScale: number }
  | { type: 'sidebar'; collapsed: boolean }
  | { type: 'aiPanel'; open: boolean }
  | { type: 'notify'; text: string; severity: string }

const PREF_KEY = 'investment-ui-prefs'
const PREF_KEYS = ['theme', 'fontScale', 'sidebarCollapsed', 'aiPanelOpen'] as const

function loadPrefs(): Partial<UIState> {
  try {
    const raw = localStorage.getItem(PREF_KEY)
    return raw ? (JSON.parse(raw) as Partial<UIState>) : {}
  } catch {
    return {}
  }
}

function reducer(s: UIState, a: Action): UIState {
  switch (a.type) {
    case 'page': return { ...s, page: a.page }
    case 'market': return { ...s, market: a.market }
    case 'instrument': return { ...s, instrumentId: a.instrumentId }
    case 'strategy': return { ...s, strategyId: a.strategyId }
    case 'mode': return { ...s, mode: a.mode }
    case 'theme': return { ...s, theme: a.theme }
    case 'fontScale':
      return { ...s, fontScale: Math.min(1.5, Math.max(0.8, a.fontScale)) }
    case 'sidebar': return { ...s, sidebarCollapsed: a.collapsed }
    case 'aiPanel': return { ...s, aiPanelOpen: a.open }
    case 'notify':
      return {
        ...s,
        notices: [
          { id: Date.now(), text: a.text, severity: a.severity, at: Date.now() },
          ...s.notices,
        ].slice(0, 50),
      }
  }
}

const Ctx = createContext<{
  state: UIState
  dispatch: React.Dispatch<Action>
} | null>(null)

export function InvestmentUIProvider(props: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(reducer, { ...initial, ...loadPrefs() })
  // persist display prefs only — never account/portfolio state
  React.useEffect(() => {
    const prefs: Record<string, unknown> = {}
    for (const k of PREF_KEYS) prefs[k] = state[k]
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify(prefs))
    } catch { /* storage unavailable — prefs are non-authoritative */ }
  }, [state.theme, state.fontScale, state.sidebarCollapsed, state.aiPanelOpen])
  const value = useMemo(() => ({ state, dispatch }), [state])
  return <Ctx.Provider value={value}>{props.children}</Ctx.Provider>
}

export function useUIState() {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useUIState outside provider')
  return ctx
}

export function useNotify() {
  const { dispatch } = useUIState()
  return useCallback(
    (text: string, severity = 'INFO') =>
      dispatch({ type: 'notify', text, severity }),
    [dispatch],
  )
}
