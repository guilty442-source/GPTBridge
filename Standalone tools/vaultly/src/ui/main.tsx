import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { VaultlyWindowApp } from './VaultlyWindowApp'

const root = document.getElementById('root')
if (!root) throw new Error('Tool renderer root is missing')
createRoot(root).render(<StrictMode><VaultlyWindowApp /></StrictMode>)
