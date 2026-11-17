"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { useAuth } from "@/lib/auth";
import { cx } from "@/lib/format";

const NAV = [
  { href: "/", label: "Dashboard", icon: "M3 3h7v7H3zM14 3h7v4h-7zM14 11h7v10h-7zM3 14h7v7H3z" },
  {
    href: "/agents",
    label: "Agents",
    icon: "M12 2a5 5 0 015 5v2a4 4 0 01-4 4h-2a4 4 0 01-4-4V7a5 5 0 015-5zM4 21c0-3.3 3.6-6 8-6s8 2.7 8 6",
  },
  {
    href: "/tasks",
    label: "Tasks",
    icon: "M4 5h16M4 12h16M4 19h10",
  },
  {
    href: "/bounties",
    label: "Bounties",
    icon: "M12 3l2.7 5.7 6.3.9-4.5 4.4 1 6.3-5.5-2.9-5.5 2.9 1-6.3L3 9.6l6.3-.9z",
  },
  {
    href: "/research",
    label: "Security",
    icon: "M12 3l8 4v5c0 4.5-3.4 8.4-8 9-4.6-.6-8-4.5-8-9V7z",
  },
  {
    href: "/skills",
    label: "Skills",
    icon: "M12 3l3 3-3 3-3-3zM5 10l3 3-3 3-3-3zM19 10l3 3-3 3-3-3zM12 15l3 3-3 3-3-3z",
  },
  {
    href: "/models",
    label: "Models",
    icon: "M5 5h14v5H5zM5 14h14v5H5z",
  },
  {
    href: "/terminal",
    label: "Terminal",
    icon: "M4 5h16v14H4zM8 10l3 2-3 2",
  },
  {
    href: "/approvals",
    label: "Approvals",
    icon: "M9 12l2 2 4-4M12 3l8 4v5c0 4.5-3.4 8.4-8 9-4.6-.6-8-4.5-8-9V7z",
  },
  {
    href: "/logs",
    label: "Logs",
    icon: "M6 3h9l4 4v14H6zM15 3v4h4M9 12h6M9 16h4",
  },
  {
    href: "/settings",
    label: "Settings",
    icon: "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-2.9 1.2 2 2 0 11-4 0 1.7 1.7 0 00-2.9-1.2l-.1.1a2 2 0 11-2.8-2.8l.1-.1A1.7 1.7 0 004.6 15a2 2 0 010-4 1.7 1.7 0 001.2-2.9l-.1-.1a2 2 0 112.8-2.8l.1.1A1.7 1.7 0 0011.5 4a2 2 0 014 0 1.7 1.7 0 002.9 1.2l.1-.1a2 2 0 112.8 2.8l-.1.1A1.7 1.7 0 0019.4 11a2 2 0 010 4z",
  },
];

export function SidebarNav() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const { user, signOut, can } = useAuth();

  const items = NAV.filter((item) => item.href !== "/approvals" || can("operator"));

  return (
    <>
      <button
        type="button"
        className="btn fixed left-3 top-3 z-40 md:hidden"
        onClick={() => setOpen((value) => !value)}
        aria-label="Toggle navigation"
      >
        Menu
      </button>
      <aside
        className={cx(
          "fixed inset-y-0 left-0 z-30 flex w-60 flex-col border-r border-[var(--color-line)] bg-[var(--color-surface)] transition-transform duration-200 md:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center gap-2.5 px-5 py-5">
          <div className="grid h-7 w-7 place-items-center rounded-md bg-[var(--color-accent)] text-[13px] font-bold text-[#08121f]">
            A
          </div>
          <div>
            <div className="text-sm font-semibold leading-none tracking-tight">AgentOS</div>
            <div className="mt-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-faint)]">
              Control plane
            </div>
          </div>
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2.5 pb-4">
          {items.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                data-active={active}
                className="nav-link"
                onClick={() => setOpen(false)}
              >
                <svg
                  viewBox="0 0 24 24"
                  className="h-4 w-4 shrink-0"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d={item.icon} />
                </svg>
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="border-t border-[var(--color-line)] px-3 py-3">
          {user ? (
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate text-xs font-medium">{user.display_name}</div>
                <div className="truncate text-[11px] capitalize text-[var(--color-faint)]">
                  {user.role}
                </div>
              </div>
              <button type="button" className="btn px-2 py-1 text-[11px]" onClick={signOut}>
                Sign out
              </button>
            </div>
          ) : (
            <div className="text-xs text-[var(--color-faint)]">Not signed in</div>
          )}
        </div>
      </aside>
      {open ? (
        <button
          type="button"
          aria-label="Close navigation"
          className="fixed inset-0 z-20 bg-black/50 md:hidden"
          onClick={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}
