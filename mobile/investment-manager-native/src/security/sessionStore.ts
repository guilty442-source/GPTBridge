import * as SecureStore from 'expo-secure-store'

import type { SavedConnection } from '../api/contracts'

const CONNECTION_KEY = 'gptbridge.investment.native.connection.v1'

export async function loadConnection(): Promise<SavedConnection | null> {
  const value = await SecureStore.getItemAsync(CONNECTION_KEY)
  if (!value) return null
  try {
    const parsed = JSON.parse(value) as SavedConnection
    return parsed.baseUrl && parsed.sessionToken ? parsed : null
  } catch {
    await SecureStore.deleteItemAsync(CONNECTION_KEY)
    return null
  }
}

export function saveConnection(connection: SavedConnection): Promise<void> {
  return SecureStore.setItemAsync(CONNECTION_KEY, JSON.stringify(connection), {
    keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
  })
}

export function clearConnection(): Promise<void> {
  return SecureStore.deleteItemAsync(CONNECTION_KEY)
}
