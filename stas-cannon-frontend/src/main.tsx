import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import ModeSelect from './ModeSelect.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<ModeSelect />} />
        <Route path="/localtest" element={<App mode="localtest" />} />
        <Route path="/bsvtestnet" element={<App mode="bsvtestnet" />} />
        <Route path="/bsvmainnet" element={<App mode="bsvmainnet" />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
