"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { getToken } from "./api";
import type { DomainEvent } from "./types";

export type StreamStatus = "connecting" | "live" | "reconnecting" | "closed";

interface Options {
  limit?: number;
  enabled?: boolean;
}

/** Subscribe to /dashboard/stream (SSE) and keep a bounded in-memory buffer. */
export function useEventStream({ limit = 100, enabled = true }: Options = {}) {
  const [events, setEvents] = useState<DomainEvent[]>([]);
  const [status, setStatus] = useState<StreamStatus>("closed");
  const sourceRef = useRef<EventSource | null>(null);

  const connect = useCallback(() => {
    const token = getToken();
    if (!token) return;
    const base = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
    const prefix = process.env.NEXT_PUBLIC_API_PREFIX ?? "/api/v1";
    const url = `${base}${prefix}/dashboard/stream?token=${encodeURIComponent(token)}`;

    setStatus((current) => (current === "closed" ? "connecting" : "reconnecting"));
    const source = new EventSource(url);
    sourceRef.current = source;

    source.onopen = () => setStatus("live");
    source.onerror = () => setStatus("reconnecting");

    source.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as DomainEvent;
        setEvents((previous) => [event, ...previous].slice(0, limit));
      } catch {
        /* ignore malformed frames */
      }
    };

    const names = [
      "task.created",
      "task.started",
      "task.completed",
      "task.failed",
      "task.cancelled",
      "task.progress",
      "agent.created",
      "agent.started",
      "agent.stopped",
      "tool.started",
      "tool.completed",
      "tool.failed",
      "finding.created",
      "approval.required",
      "approval.decided",
      "submission.completed",
      "model.usage",
      "system.alert",
    ];
    for (const name of names) {
      source.addEventListener(name, (raw) => {
        try {
          const payload = JSON.parse((raw as MessageEvent<string>).data) as DomainEvent;
          setEvents((previous) => [payload, ...previous].slice(0, limit));
        } catch {
          /* ignore malformed frames */
        }
      });
    }
  }, [limit]);

  useEffect(() => {
    if (!enabled) return;
    connect();
    return () => {
      sourceRef.current?.close();
      sourceRef.current = null;
      setStatus("closed");
    };
  }, [connect, enabled]);

  return { events, status, reconnect: connect, clear: () => setEvents([]) };
}
