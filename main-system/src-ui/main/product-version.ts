import packageMetadata from '../../package.json'

const bundledVersion = String(packageMetadata.version || '').trim()
const LOCKED_PRODUCT_VERSION = '1.00000'

if (bundledVersion !== LOCKED_PRODUCT_VERSION) {
  throw new Error(
    `GPTBridge product version is locked to ${LOCKED_PRODUCT_VERSION}; found ${bundledVersion || 'missing'}`
  )
}

/** Product version is immutable and never taken from Electron or an update payload. */
export const PRODUCT_VERSION = LOCKED_PRODUCT_VERSION

export const PRODUCT_DISPLAY_VERSION = PRODUCT_VERSION.split('.').slice(0, 2).join('.')
