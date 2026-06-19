import ReactDOM from 'react-dom/client'
import App from './ui/App'
import { HMRGuard } from './shared/components/HMRGuard'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <HMRGuard>
    <App />
  </HMRGuard>
)
