import { Link, useLocation } from "react-router-dom";
import { FilePlus, LayoutDashboard, LogOut } from "lucide-react";
import { useAuth } from "@/hooks/useAuth";

const NAV_ITEMS = [
  { to: "/studio", label: "Dashboard", icon: LayoutDashboard, exact: true },
  { to: "/projects/new", label: "New Project", icon: FilePlus, exact: false },
];

/** Left navigation sidebar — persistent across every protected page (see
 * AppLayout.tsx). Per-project links (Manage Voices, Track, Review, ...) stay
 * inline on their own pages since they depend on a :projectId/:jobId param
 * the sidebar doesn't have. */
export function Sidebar() {
  const location = useLocation();
  const { user, signOutUser } = useAuth();

  return (
    <aside className="flex h-screen w-60 shrink-0 flex-col border-r bg-card">
      <div className="border-b px-4 py-4">
        <span className="font-display text-lg font-semibold text-primary">
          XIVI
        </span>
        <span className="ml-1 text-xs text-muted-foreground">Studio</span>
      </div>

      <nav className="flex-1 space-y-1 p-3">
        {NAV_ITEMS.map((item) => {
          const isActive = item.exact
            ? location.pathname === item.to
            : location.pathname.startsWith(item.to);
          const Icon = item.icon;
          return (
            <Link
              key={item.to}
              to={item.to}
              className={`flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                isActive
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
              }`}
            >
              <Icon size={18} />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="border-t p-3">
        {user && (
          <div className="mb-2 flex items-center gap-2 px-1">
            {user.photoURL ? (
              <img
                src={user.photoURL}
                alt=""
                className="h-8 w-8 rounded-full"
              />
            ) : (
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-secondary text-xs font-semibold text-secondary-foreground">
                {(user.displayName ?? user.email ?? "?")
                  .charAt(0)
                  .toUpperCase()}
              </div>
            )}
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-foreground">
                {user.displayName ?? "User"}
              </p>
              <p className="truncate text-xs text-muted-foreground">
                {user.email}
              </p>
            </div>
          </div>
        )}
        <button
          type="button"
          onClick={() => signOutUser()}
          className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground"
        >
          <LogOut size={18} />
          Sign out
        </button>
      </div>
    </aside>
  );
}
