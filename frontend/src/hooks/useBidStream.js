import { useState, useRef, useCallback, useEffect } from "react";

/**
 * useBidStream — React hook for streaming proposal drafts via SSE.
 *
 * Opens an EventSource to POST /api/workspaces/{id}/draft and
 * accumulates tokens per section into a reactive state map.
 *
 * Usage:
 *   const { sections, isStreaming, error, startStream, stopStream } = useBidStream(workspaceId);
 *
 * Returns:
 *   sections     — { [sectionRef: string]: { title: string, text: string, done: boolean } }
 *   isStreaming   — boolean
 *   error         — string | null
 *   startStream   — () => void   (call to begin streaming)
 *   stopStream    — () => void   (call to abort early)
 *
 * SSE event format expected from backend:
 *   data: {"section": "4.3", "title": "...", "token": "word", "done": false}
 *   data: [DONE]
 */

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

export default function useBidStream(workspaceId) {
  const [sections, setSections] = useState({});
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState(null);
  // { total, done, remaining, resumed } — sent by the backend at stream start
  const [progress, setProgress] = useState(null);

  // Ref to hold the current abort controller so we can cancel from outside
  const abortRef = useRef(null);

  /**
   * Start the SSE stream by sending a POST fetch and reading the
   * response body as a stream (fetch + ReadableStream, NOT EventSource,
   * because EventSource only supports GET).
   *
   * options.restart — true regenerates everything from scratch; false
   * (default) resumes, drafting only sections not yet saved.
   */
  const startStream = useCallback(async ({ restart = false } = {}) => {
    if (isStreaming) return;
    if (!workspaceId) {
      setError("No workspace ID provided.");
      return;
    }

    // Reset state
    setSections({});
    setError(null);
    setProgress(null);
    setIsStreaming(true);

    const abortController = new AbortController();
    abortRef.current = abortController;

    try {
      const response = await fetch(`${API_BASE}/api/workspaces/${workspaceId}/draft${restart ? "?restart=true" : ""}`, {
        method: "POST",
        signal: abortController.signal,
        headers: {
          Accept: "text/event-stream",
        },
      });

      if (!response.ok) {
        const errBody = await response.text();
        throw new Error(`Server error ${response.status}: ${errBody}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE events are delimited by double newline
        const lines = buffer.split("\n\n");
        // Keep the last incomplete chunk in the buffer
        buffer = lines.pop() || "";

        for (const eventBlock of lines) {
          for (const line of eventBlock.split("\n")) {
            if (!line.startsWith("data: ")) continue;

            const payload = line.slice(6).trim(); // remove "data: "

            // ── Terminal signal ──────────────────────────────────────────
            if (payload === "[DONE]") {
              setIsStreaming(false);
              return;
            }

            // ── Parse JSON event ─────────────────────────────────────────
            try {
              const event = JSON.parse(payload);

              // Progress meta event (sent once at stream start)
              if (event.meta) {
                setProgress({
                  total: event.total,
                  done: event.done,
                  remaining: event.remaining,
                  resumed: event.resumed,
                });
                continue;
              }

              const { section, title, token, done: sectionDone } = event;

              if (!section) continue;

              setSections((prev) => {
                const existing = prev[section] || { title: title || section, text: "", done: false };
                return {
                  ...prev,
                  [section]: {
                    title: title || existing.title,
                    text: sectionDone ? existing.text : existing.text + token,
                    done: !!sectionDone,
                  },
                };
              });
            } catch {
              // Skip malformed JSON lines silently
              console.warn("useBidStream: failed to parse SSE event:", payload);
            }
          }
        }
      }
    } catch (err) {
      if (err.name === "AbortError") {
        console.log("useBidStream: stream aborted by user.");
      } else {
        console.error("useBidStream error:", err);
        setError(err.message || "Stream failed.");
      }
    } finally {
      setIsStreaming(false);
      abortRef.current = null;
    }
  }, [workspaceId, isStreaming]);

  /**
   * Abort the in-progress stream.
   */
  const stopStream = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setIsStreaming(false);
  }, []);

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (abortRef.current) {
        abortRef.current.abort();
      }
    };
  }, []);

  return {
    /** Map of section ref → { title, text, done } */
    sections,
    /** Whether the stream is currently active */
    isStreaming,
    /** Error message if something went wrong, null otherwise */
    error,
    /** { total, done, remaining, resumed } from the stream's meta event */
    progress,
    /** Call to begin streaming; pass { restart: true } to regenerate all */
    startStream,
    /** Call to stop the stream — completed sections stay saved */
    stopStream,
  };
}
