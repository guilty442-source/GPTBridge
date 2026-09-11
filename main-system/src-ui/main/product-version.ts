import packageMetadata from '../../package.json'

const bundledVersion = String(packageMetadata.version || '').trim()
const LOCKED_PACKAGE_VERSION = '1.0.0'

if (bundledVersion !== LOCKED_PACKAGE_VERSION) {
  throw new Error(
    `GPTBridge package version is locked to ${LOCKED_PACKAGE_VERSION}; found ${bundledVersion || 'missing'}`
  )
}

/** Product version is immutable and never taken from Electron or an update payload. */
export const PRODUCT_VERSION = bundledVersion

export const PRODUCT_DISPLAY_VERSION = PRODUCT_VERSION.split('.').slice(0, 2).join('.')
