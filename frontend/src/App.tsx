import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from '@/components/layout/AppShell';
import { AnalyticsPage } from '@/pages/AnalyticsPage';
import { ArchitecturePage } from '@/pages/ArchitecturePage';
import { DashboardPage } from '@/pages/DashboardPage';
import { EventsPage } from '@/pages/EventsPage';
import { NodeDetailPage } from '@/pages/NodeDetailPage';
import { NodesPage } from '@/pages/NodesPage';
import { NotFoundPage } from '@/pages/NotFoundPage';

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/nodes" element={<NodesPage />} />
        <Route path="/nodes/:nodeId" element={<NodeDetailPage />} />
        <Route path="/events" element={<EventsPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/architecture" element={<ArchitecturePage />} />
      </Route>
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
