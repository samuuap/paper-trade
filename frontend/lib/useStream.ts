"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "./api";

export interface StreamEvent {
  topic: string;
  ts: string;
  data: Record<string, unknown>;
}

export interface StreamState {
  connected: boolean;
  last: StreamEvent | null;
  events: StreamEvent[];
}

const MAX_EVENTS = 60;

/**
 * Subscribe to the backend's SSE feed.
 *
 * `onEvent` is held in a ref so a caller can pass an inline closure without
 * tearing the connection down on every render — which would otherwise turn a
 * live feed into a reconnect loop.
 */
export function useStream(
  topics: string[] | null,
  onEvent?: (event: StreamEvent) => void,
): StreamState {
  const [connected, setConnected] = useState(false);
  const [last, setLast] = useState<StreamEvent | null>(null);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  const key = topics ? topics.join(",") : "";

  useEffect(() => {
    const query = key ? `?topics=${encodeURIComponent(key)}` : "";
    const source = new EventSource(`${API_BASE}/stream${query}`);

    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    const consume = (raw: MessageEvent) => {
      try {
        const event = JSON.parse(raw.data) as StreamEvent;
        setLast(event);
        setEvents((prev) => [event, ...prev].slice(0, MAX_EVENTS));
        handler.current?.(event);
      } catch {
        /* a malformed frame is not worth tearing the stream down for */
      }
    };

    const named = key
      ? key.split(",")
      : ["phase", "cycle", "decision", "order", "fill", "equity", "queue", "job", "system"];
    for (const topic of named) source.addEventListener(topic, consume);

    return () => {
      for (const topic of named) source.removeEventListener(topic, consume);
      source.close();
    };
  }, [key]);

  return { connected, last, events };
}
