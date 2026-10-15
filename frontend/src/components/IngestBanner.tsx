import React, { useEffect, useState, useRef } from 'react';
import { Sparkles, CheckCircle2, ChevronUp, ChevronDown, Layers, X } from 'lucide-react';
import type { IngestJobState, Photo } from '../types';

interface IngestBannerProps {
  onJobCompleted: (photo: Photo) => void;
  onBannerHeightChange?: (height: number) => void;
}

export const IngestBanner: React.FC<IngestBannerProps> = ({ onJobCompleted, onBannerHeightChange }) => {
  const [activeJobs, setActiveJobs] = useState<IngestJobState[]>([]);
  const [isMinimized, setIsMinimized] = useState(false);
  const [recentlyCompleted, setRecentlyCompleted] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) {
      onBannerHeightChange?.(0);
      return;
    }
    const el = containerRef.current;
    onBannerHeightChange?.(el.offsetHeight);

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        onBannerHeightChange?.(entry.target.getBoundingClientRect().height);
      }
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      onBannerHeightChange?.(0);
    };
  }, [activeJobs.length, recentlyCompleted, isMinimized, onBannerHeightChange]);

  const onJobCompletedRef = useRef(onJobCompleted);
  useEffect(() => {
    onJobCompletedRef.current = onJobCompleted;
  }, [onJobCompleted]);

  useEffect(() => {
    let eventSource: EventSource | null = null;
    let pollInterval: ReturnType<typeof setInterval> | null = null;
    let isMounted = true;

    const connectSSE = () => {
      eventSource = new EventSource('/api/ingest/stream');

      eventSource.addEventListener('initial', (e: MessageEvent) => {
        try {
          const jobs: IngestJobState[] = JSON.parse(e.data);
          if (isMounted) {
            setActiveJobs(jobs.filter((j) => j.status === 'queued' || j.status === 'processing'));
          }
        } catch (err) {
          console.error('Failed to parse initial jobs:', err);
        }
      });

      eventSource.onmessage = (e: MessageEvent) => {
        try {
          const updated: IngestJobState = JSON.parse(e.data);
          if (!isMounted) return;
          setActiveJobs((prev) => {
            if (updated.status === 'completed' || updated.status === 'failed') {
              if (updated.status === 'completed' && updated.photo_record) {
                onJobCompletedRef.current(updated.photo_record);
                setRecentlyCompleted(updated.original_name);
                setTimeout(() => {
                  if (isMounted) setRecentlyCompleted(null);
                }, 6000);
              }
              return prev.filter((j) => j.job_id !== updated.job_id);
            }
            const existingIndex = prev.findIndex((j) => j.job_id === updated.job_id);
            if (existingIndex >= 0) {
              const copy = [...prev];
              copy[existingIndex] = updated;
              return copy;
            } else {
              return [...prev, updated];
            }
          });
        } catch (err) {
          console.error('Failed to parse ingest event:', err);
        }
      };

      // Native EventSource automatically attempts to reconnect on transient disconnects.
      // We only fall back to polling if the connection is permanently closed.
      eventSource.onerror = () => {
        if (!isMounted) return;
        if (eventSource && eventSource.readyState === EventSource.CLOSED) {
          eventSource.close();
          eventSource = null;
          if (!pollInterval) {
            pollInterval = setInterval(async () => {
              try {
                const res = await fetch('/api/ingest/active');
                if (res.ok && isMounted) {
                  const jobs: IngestJobState[] = await res.json();
                  setActiveJobs(jobs.filter((j) => j.status === 'queued' || j.status === 'processing'));
                }
              } catch {}
            }, 5000);
          }
        }
      };
    };

    connectSSE();

    return () => {
      isMounted = false;
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
      }
    };
  }, []);

  // If recently completed and no active jobs, show brief celebration banner that shifts the page down
  if (activeJobs.length === 0 && recentlyCompleted) {
    return (
      <div
        ref={containerRef}
        className="w-full bg-emerald-950/95 border-b border-emerald-600/70 px-4 py-2.5 text-xs text-emerald-200 relative z-30 shadow-md animate-in fade-in slide-in-from-top duration-200"
      >
        <div className="flex items-center justify-between max-w-7xl mx-auto w-full">
          <div className="flex items-center space-x-2.5 min-w-0">
            <CheckCircle2 className="w-4 h-4 text-emerald-400 flex-shrink-0" />
            <span className="font-semibold text-emerald-300">Indexed Shelf Ready:</span>
            <span className="font-mono text-emerald-100 truncate">{recentlyCompleted}</span>
            <span className="text-emerald-400 hidden sm:inline">&bull; Added to visual search manifest</span>
          </div>
          <button
            onClick={() => setRecentlyCompleted(null)}
            className="text-emerald-400 hover:text-emerald-200 p-0.5 rounded hover:bg-emerald-900/50 transition-colors ml-2 flex-shrink-0"
            title="Dismiss"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    );
  }

  if (activeJobs.length === 0) return null;

  const currentJob = activeJobs[0];

  return (
    <div
      ref={containerRef}
      className="w-full bg-gradient-to-r from-slate-900 via-cyan-950/95 to-slate-900 border-b border-cyan-800/60 text-xs relative z-30 shadow-md"
    >
      <div className="max-w-7xl mx-auto px-4 py-2.5">
        {/* Top summary row */}
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center space-x-2.5 min-w-0">
            <div className="relative flex items-center justify-center w-6 h-6 rounded-lg bg-cyan-900/60 border border-cyan-700/50 flex-shrink-0">
              <Sparkles className="w-3.5 h-3.5 text-cyan-300 animate-pulse" />
            </div>
            <div className="min-w-0">
              <div className="flex items-center space-x-2">
                <span className="font-semibold text-cyan-200 tracking-wide uppercase text-[11px] font-mono">
                  {currentJob.is_seed ? 'Background Shelf Ingest' : 'Processing Upload'}
                </span>
                <span className="text-slate-400 truncate text-[11px] max-w-[200px] sm:max-w-xs font-mono">
                  ({currentJob.original_name})
                </span>
                {activeJobs.length > 1 && (
                  <span className="bg-cyan-950 text-cyan-400 border border-cyan-800 px-1.5 py-0.2 rounded text-[10px] font-mono">
                    +{activeJobs.length - 1} queued
                  </span>
                )}
              </div>
              <p className="text-cyan-100 font-medium text-xs mt-0.5 truncate">
                {currentJob.encouragement}
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-3 flex-shrink-0">
            {currentJob.bins_count > 0 && (
              <div className="hidden sm:flex items-center space-x-1 bg-slate-900/80 border border-cyan-800/60 px-2 py-0.5 rounded-full text-cyan-300 font-mono text-[11px]">
                <Layers className="w-3 h-3 text-cyan-400" />
                <span>{currentJob.bins_count} bins</span>
              </div>
            )}
            <span className="font-mono font-bold text-cyan-300 text-xs">
              {currentJob.progress}%
            </span>
            <button
              onClick={() => setIsMinimized(!isMinimized)}
              className="text-slate-400 hover:text-slate-200 p-1 rounded hover:bg-slate-800/60 transition-colors"
              title={isMinimized ? 'Expand progress' : 'Minimize'}
            >
              {isMinimized ? <ChevronDown className="w-4 h-4" /> : <ChevronUp className="w-4 h-4" />}
            </button>
          </div>
        </div>

        {/* Progress Bar & Details */}
        {!isMinimized && (
          <div className="mt-2 pt-1 border-t border-cyan-900/40 flex flex-col space-y-1.5">
            <div className="w-full bg-slate-950/80 rounded-full h-1.5 overflow-hidden border border-cyan-900/50">
              <div
                className="bg-gradient-to-r from-cyan-500 via-teal-400 to-emerald-400 h-full rounded-full transition-all duration-300 ease-out"
                style={{ width: `${Math.max(4, currentJob.progress)}%` }}
              />
            </div>
            <div className="flex items-center justify-between text-[11px] text-slate-400 font-mono">
              <span className="truncate">{currentJob.message}</span>
              {currentJob.total_tiles > 1 && (
                <span>
                  Tile {currentJob.completed_tiles} / {currentJob.total_tiles}
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
