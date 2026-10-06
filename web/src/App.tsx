import { Navigate, Route, Routes } from 'react-router-dom'
import { BrandProvider } from './state/BrandContext'
import { ToastProvider } from './state/Toast'
import Overview from './pages/Overview'
import Approvals from './pages/Approvals'
import Planner from './pages/Planner'
import Content from './pages/Content'
import ContentItem from './pages/ContentItem'
import Execution from './pages/Execution'
import Agents from './pages/Agents'
import Integrations from './pages/Integrations'
import Campaigns from './pages/Campaigns'
import Learnings from './pages/Learnings'
import Guidelines from './pages/Guidelines'
import Sources from './pages/Sources'
import Performance from './pages/Performance'
import Feedback from './pages/Feedback'
import Engagement from './pages/Engagement'
import Settings from './pages/Settings'

export default function App() {
  return (
    <ToastProvider>
      <BrandProvider>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/approvals" element={<Approvals />} />
          <Route path="/approvals/:kind/:id" element={<Approvals />} />
          <Route path="/planner" element={<Planner />} />
          <Route path="/content" element={<Content />} />
          <Route path="/content/newsletter/:id" element={<ContentItem />} />
          <Route path="/execution" element={<Execution />} />
          <Route path="/campaigns" element={<Campaigns />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="/guidelines" element={<Guidelines />} />
          <Route path="/performance" element={<Performance />} />
          <Route path="/learnings" element={<Learnings />} />
          <Route path="/feedback" element={<Feedback />} />
          <Route path="/feedback/:id" element={<Feedback />} />
          <Route path="/engagement" element={<Engagement />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/integrations" element={<Integrations />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrandProvider>
    </ToastProvider>
  )
}
