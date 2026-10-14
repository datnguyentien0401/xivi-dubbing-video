import { Outlet } from "react-router-dom";
import { Sidebar } from "@/components/Sidebar";

/** Shared shell for every protected page — left Sidebar + scrollable content
 * area. Nested under ProtectedRoute in App.tsx, so Outlet only ever renders
 * once auth is confirmed. */
export default function AppLayout() {
  return (
    <div className="flex h-screen bg-background">
      <Sidebar />
      <main className="flex-1 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  );
}
