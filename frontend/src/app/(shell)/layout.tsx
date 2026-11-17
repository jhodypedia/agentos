"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { SidebarNav } from "@/components/sidebar-nav";
import { useAuth } from "@/lib/auth";

export default function ShellLayout({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  if (loading) {
    return (
      <div className="grid min-h-screen place-items-center">
        <div className="flex items-center gap-2 text-xs text-[var(--color-faint)]">
          <span className="live-dot h-1.5 w-1.5 rounded-full bg-[var(--color-accent)]" />
          Connecting to control plane…
        </div>
      </div>
    );
  }

  if (!user) return null;

  return (
    <div className="min-h-screen">
      <SidebarNav />
      <main className="md:pl-60">
        <div className="mx-auto max-w-[1400px] px-4 py-6 pt-16 md:px-8 md:py-8">{children}</div>
      </main>
    </div>
  );
}
