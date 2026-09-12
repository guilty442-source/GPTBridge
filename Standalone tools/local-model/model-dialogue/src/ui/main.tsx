import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { StarChatWindowApp } from './StarChatWindowApp'

const root = document.getElementById('root')
if (!root) throw new Error('Star Chat renderer root is missing')
createRoot(root).render(<StrictMode><StarChatWindowApp /></StrictMode>)
