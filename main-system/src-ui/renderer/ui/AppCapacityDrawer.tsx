import { Drawer } from '@/ui/drawer/Drawer'
import { formatBytes, formatProjectSize } from '@/shared/utils/format'
import { mainSystemLocale } from '@/locales/main-system'

const t = mainSystemLocale.product

type SystemMetrics = {
  diskUsagePercent?: number | null
  diskTotalBytes?: number | null
  diskFreeBytes?: number | null
  diskRoot?: string
}

function formatFileCount(value: number | undefined): string {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? `${new Intl.NumberFormat('zh-TW').format(value)} ${t.files}`
    : t.pendingCheck
}

function capacityDetail(bytes: number | undefined, fileCount: number | undefined): string {
  return `${formatBytes(bytes, { exactBytes: true, fallback: t.pendingCheck })} · ${formatFileCount(fileCount)}`
}

export type CapacityDrawerProps = {
  open: boolean
  onClose: () => void
  systemMetrics: SystemMetrics
  mainSystemSizeBytes?: number
  mainSystemFileCount?: number
  dependencySizeBytes?: number
  dependencyFileCount?: number
  sharedLayerSizeBytes?: number
  sharedLayerFileCount?: number
  workspaceSizeBytes?: number
  workspaceFileCount?: number
}

export function CapacityDrawer({
  open,
  onClose,
  systemMetrics,
  mainSystemSizeBytes,
  mainSystemFileCount,
  dependencySizeBytes,
  dependencyFileCount,
  sharedLayerSizeBytes,
  sharedLayerFileCount,
  workspaceSizeBytes,
  workspaceFileCount,
}: CapacityDrawerProps) {
  const diskUsedBytes =
    typeof systemMetrics.diskTotalBytes === 'number' &&
    typeof systemMetrics.diskFreeBytes === 'number'
      ? Math.max(0, systemMetrics.diskTotalBytes - systemMetrics.diskFreeBytes)
      : null
  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={t.capacityDetails}
      eyebrow={t.systemOverview}
      icon="D"
    >
      <div className="capacity-drawer">
        <article className="capacity-row" data-testid="system-disk-size">
          <div className="capacity-row__head">
            <strong>{t.systemDisk} {systemMetrics.diskRoot || ''}</strong>
            <span className="capacity-row__big">
              {formatBytes(systemMetrics.diskTotalBytes, { exactBytes: true, fallback: t.pendingCheck })}
            </span>
          </div>
          <p className="capacity-row__detail">
            {t.usageRate}{' '}
            {typeof systemMetrics.diskUsagePercent === 'number'
              ? `${systemMetrics.diskUsagePercent.toFixed(1)}%`
              : t.pendingCheck}
            {' · '}
            {t.used} {formatBytes(diskUsedBytes, { exactBytes: true, fallback: t.pendingCheck })}
            {' · '}
            {t.available} {formatBytes(systemMetrics.diskFreeBytes, { exactBytes: true, fallback: t.pendingCheck })}
          </p>
        </article>

        <article className="capacity-row" data-testid="main-system-folder-size">
          <div className="capacity-row__head">
            <strong>{t.mainSystemSize}</strong>
            <span className="capacity-row__big">
              {formatProjectSize(mainSystemSizeBytes, { fallback: t.pendingCheck })}
            </span>
          </div>
          <p className="capacity-row__detail">
            {t.mainSystemSizeHint} · {capacityDetail(mainSystemSizeBytes, mainSystemFileCount)}
          </p>
        </article>

        <article className="capacity-row" data-testid="dependency-folder-size">
          <div className="capacity-row__head">
            <strong>{t.dependencySize}</strong>
            <span className="capacity-row__big">
              {formatProjectSize(dependencySizeBytes, { fallback: t.pendingCheck })}
            </span>
          </div>
          <p className="capacity-row__detail">
            {t.dependencySizeHint} · {capacityDetail(dependencySizeBytes, dependencyFileCount)}
          </p>
        </article>

        <article className="capacity-row" data-testid="shared-layer-folder-size">
          <div className="capacity-row__head">
            <strong>{t.sharedLayerSize}</strong>
            <span className="capacity-row__big">
              {formatProjectSize(sharedLayerSizeBytes, { fallback: t.pendingCheck })}
            </span>
          </div>
          <p className="capacity-row__detail">
            {t.sharedLayerSizeHint} · {capacityDetail(sharedLayerSizeBytes, sharedLayerFileCount)}
          </p>
        </article>

        <article className="capacity-row" data-testid="workspace-folder-size">
          <div className="capacity-row__head">
            <strong>{t.workspaceSize}</strong>
            <span className="capacity-row__big">
              {formatProjectSize(workspaceSizeBytes, { fallback: t.pendingCheck })}
            </span>
          </div>
          <p className="capacity-row__detail">
            {t.workspaceSizeHint} · {capacityDetail(workspaceSizeBytes, workspaceFileCount)}
          </p>
        </article>
      </div>
    </Drawer>
  )
}
