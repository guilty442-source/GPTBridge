import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { SystemRescueWindowApp } from './SystemRescueWindowApp'

createRoot(document.getElementById('root')!).render(
  <StrictMode><SystemRescueWindowApp /></StrictMode>
)
