/**
 * 路由表（03 §1.2）：admin 组 6 页 + /health（调试态）；lite 组挂 /lite 前缀（05 文档 D2）。
 * 不建 replay 路由（03 §4.3 W5+ backlog）。
 */
import { lazy, Suspense } from 'react';
import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import AppShell from './components/common/AppShell';

// 页面懒加载（首屏预算 03 §6.3：ECharts 等重库按需 chunk）
const MapPage = lazy(() => import('./pages/MapPage'));
const TimelinePage = lazy(() => import('./pages/TimelinePage'));
const AgentPage = lazy(() => import('./pages/AgentPage'));
const LocationPage = lazy(() => import('./pages/LocationPage'));
const RipplePage = lazy(() => import('./pages/RipplePage'));
const RelationsPage = lazy(() => import('./pages/RelationsPage'));
const HealthPage = lazy(() => import('./pages/HealthPage'));
const LiteHomePage = lazy(() => import('./lite/LiteHomePage'));
const LiteAgentPage = lazy(() => import('./lite/LiteAgentPage'));
const LiteStoryPage = lazy(() => import('./lite/LiteStoryPage'));

const wrap = (node: React.ReactNode) => (
  <Suspense fallback={<div className="p-4 text-text-1">加载中…</div>}>{node}</Suspense>
);

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { path: '/', element: wrap(<MapPage />) },
      { path: '/map', element: wrap(<MapPage />) },
      { path: '/timeline', element: wrap(<TimelinePage />) },
      { path: '/agent/:id', element: wrap(<AgentPage />) },
      { path: '/location/:id', element: wrap(<LocationPage />) },
      { path: '/ripple', element: wrap(<RipplePage />) },
      { path: '/ripple/:eventId', element: wrap(<RipplePage />) },
      { path: '/relations', element: wrap(<RelationsPage />) },
      { path: '/health', element: wrap(<HealthPage />) },
      { path: '/lite/home', element: wrap(<LiteHomePage />) },
      { path: '/lite/agent/:id', element: wrap(<LiteAgentPage />) },
      { path: '/lite/story', element: wrap(<LiteStoryPage />) },
    ],
  },
]);

export default function Router() {
  return <RouterProvider router={router} />;
}
