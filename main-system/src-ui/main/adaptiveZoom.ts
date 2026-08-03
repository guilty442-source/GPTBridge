import type { BrowserWindow } from 'electron'

const MIN_VIEWPORT_SCALE = 0.72
const MAX_VIEWPORT_SCALE = 1.5
const MIN_EFFECTIVE_ZOOM = 0.62
const MAX_EFFECTIVE_ZOOM = 1.6
const RESIZE_DEBOUNCE_MS = 60

interface WindowZoomProfile {
  window: BrowserWindow
  referenceWidth: number
  referenceHeight: number
  lastAppliedFactor: number
  resizeTimer: NodeJS.Timeout | null
}

interface AdaptiveZoomInput {
  width: number
  height: number
  referenceWidth: number
  referenceHeight: number
  preferredFactor: number
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value))
}

export function calculateAdaptiveZoomFactor(input: AdaptiveZoomInput): number {
  const width = Math.max(1, input.width)
  const height = Math.max(1, input.height)
  const referenceWidth = Math.max(1, input.referenceWidth)
  const referenceHeight = Math.max(1, input.referenceHeight)
  const preferredFactor =
    Number.isFinite(input.preferredFactor) && input.preferredFactor > 0
      ? input.preferredFactor
      : 1
  const viewportScale = clamp(
    Math.min(width / referenceWidth, height / referenceHeight),
    MIN_VIEWPORT_SCALE,
    MAX_VIEWPORT_SCALE
  )
  const effectiveFactor = clamp(
    viewportScale * preferredFactor,
    MIN_EFFECTIVE_ZOOM,
    MAX_EFFECTIVE_ZOOM
  )
  return Math.round(effectiveFactor * 1000) / 1000
}

export class AdaptiveZoomController {
  private readonly profiles = new Map<number, WindowZoomProfile>()

  constructor(private readonly getPreferredFactor: () => number) {}

  register(window: BrowserWindow): void {
    const [referenceWidth, referenceHeight] = window.getContentSize()
    this.profiles.set(window.id, {
      window,
      referenceWidth: Math.max(1, referenceWidth),
      referenceHeight: Math.max(1, referenceHeight),
      lastAppliedFactor: 0,
      resizeTimer: null,
    })

    const scheduleUpdate = () => this.schedule(window)
    window.on('resize', scheduleUpdate)
    window.on('maximize', scheduleUpdate)
    window.on('unmaximize', scheduleUpdate)
    window.on('enter-full-screen', scheduleUpdate)
    window.on('leave-full-screen', scheduleUpdate)
    window.webContents.on('did-finish-load', scheduleUpdate)
    window.on('closed', () => this.unregister(window.id))

    this.apply(window)
  }

  applyAll(): void {
    for (const profile of this.profiles.values()) {
      this.apply(profile.window)
    }
  }

  private schedule(window: BrowserWindow): void {
    const profile = this.profiles.get(window.id)
    if (!profile) return
    if (profile.resizeTimer) clearTimeout(profile.resizeTimer)
    profile.resizeTimer = setTimeout(() => {
      profile.resizeTimer = null
      this.apply(window)
    }, RESIZE_DEBOUNCE_MS)
  }

  private apply(window: BrowserWindow): void {
    const profile = this.profiles.get(window.id)
    if (!profile || window.isDestroyed() || window.webContents.isDestroyed()) return

    const [width, height] = window.getContentSize()
    const factor = calculateAdaptiveZoomFactor({
      width,
      height,
      referenceWidth: profile.referenceWidth,
      referenceHeight: profile.referenceHeight,
      preferredFactor: this.getPreferredFactor(),
    })
    if (Math.abs(profile.lastAppliedFactor - factor) < 0.005) return

    profile.lastAppliedFactor = factor
    window.webContents.setZoomFactor(factor)
  }

  private unregister(windowId: number): void {
    const profile = this.profiles.get(windowId)
    if (profile?.resizeTimer) clearTimeout(profile.resizeTimer)
    this.profiles.delete(windowId)
  }
}
