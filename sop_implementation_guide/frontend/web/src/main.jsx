import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/metropolis/400.css'
import '@fontsource/metropolis/500.css'
import '@fontsource/metropolis/600.css'
import '@fontsource/metropolis/700.css'
import 'leaflet/dist/leaflet.css'
import './index.css'
import App from './App.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
