import { Route, Routes } from "react-router-dom";

import { Layout } from "@/components/Layout";
import AnalyzePage from "@/pages/Analyze";
import DashboardPage from "@/pages/Dashboard";
import DiscoveryPage from "@/pages/Discovery";
import EventRadarPage from "@/pages/EventRadar";
import ForecastHistoryPage from "@/pages/ForecastHistory";
import SettingsPage from "@/pages/Settings";
import StockDetailPage from "@/pages/StockDetail";
import WatchlistPage from "@/pages/Watchlist";
import { EmptyState } from "@/components/common";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="discovery" element={<DiscoveryPage />} />
        <Route path="analyze" element={<AnalyzePage />} />
        <Route path="stocks/:ticker" element={<StockDetailPage />} />
        <Route path="events" element={<EventRadarPage />} />
        <Route path="watchlist" element={<WatchlistPage />} />
        <Route path="forecasts" element={<ForecastHistoryPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<EmptyState title="Page not found" />} />
      </Route>
    </Routes>
  );
}
