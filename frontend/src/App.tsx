import { useEffect, useState, useRef, useCallback, useMemo } from 'react';
import type { Photo, MatchResult, WorkerOutgoingMessage } from './types';
import { FloatingOmnibar } from './components/FloatingOmnibar';
import { ShelfStream } from './components/ShelfStream';
import { RadarIndicators } from './components/RadarIndicators';
import { UploadModal } from './components/UploadModal';
import { IngestBanner } from './components/IngestBanner';
import { AdminAuthModal, type AdminActionType } from './components/AdminAuthModal';
import { RenameModal } from './components/RenameModal';
import { ReplaceModal } from './components/ReplaceModal';
import { DeleteConfirmModal } from './components/DeleteConfirmModal';
import { getStoredToken, clearStoredToken } from './utils/auth';

const KIOSK_TIMEOUT_SECONDS = 60;

export function App() {
  const initialQuery = useMemo(() => {
    if (typeof window === 'undefined') return '';
    const params = new URLSearchParams(window.location.search);
    return params.get('q') || params.get('search') || '';
  }, []);
  const hasUrlQuery = Boolean(initialQuery);

  const [photos, setPhotos] = useState<Photo[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState(initialQuery);
  const [matchesByBinId, setMatchesByBinId] = useState<Record<string, MatchResult>>({});
  const [highCount, setHighCount] = useState(0);
  const [moderateCount, setModerateCount] = useState(0);
  const [totalMatches, setTotalMatches] = useState(0);
  const [activeMatchIndex, setActiveMatchIndex] = useState<number | null>(null);

  // Kiosk mode state
  const [kioskEnabled, setKioskEnabled] = useState(true);
  const [kioskSecondsRemaining, setKioskSecondsRemaining] = useState(KIOSK_TIMEOUT_SECONDS);

  // UI modals & state
  const [isUploadOpen, setIsUploadOpen] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [bannerHeight, setBannerHeight] = useState(0);

  // Admin auth & shelf action modal states
  const [authModalState, setAuthModalState] = useState<{
    isOpen: boolean;
    actionType: AdminActionType;
    photo: Photo | null;
    initialError?: string;
  } | null>(null);
  const [renameModalPhoto, setRenameModalPhoto] = useState<Photo | null>(null);
  const [replaceModalPhoto, setReplaceModalPhoto] = useState<Photo | null>(null);
  const [deleteConfirmPhoto, setDeleteConfirmPhoto] = useState<Photo | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  // References
  const workerRef = useRef<Worker | null>(null);
  const queryCounter = useRef(0);
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const hasAutoCenteredRef = useRef(false);

  // Ordered matches in visual top-to-bottom reading order
  const orderedMatches = useMemo(() => {
    const list: Array<{ binId: string; photoId: string }> = [];
    for (const photo of photos) {
      const matchedBins = photo.bins
        .filter((b) => Boolean(matchesByBinId[b.id]))
        .sort((a, b) => {
          if (Math.abs(a.bbox[1] - b.bbox[1]) > 0.03) {
            return a.bbox[1] - b.bbox[1];
          }
          return a.bbox[0] - b.bbox[0];
        });

      for (const bin of matchedBins) {
        list.push({ binId: bin.id, photoId: photo.id });
      }
    }
    return list;
  }, [photos, matchesByBinId]);

  const activeBinId =
    activeMatchIndex !== null && orderedMatches[activeMatchIndex]
      ? orderedMatches[activeMatchIndex].binId
      : null;

  // Fetch manifest on mount
  const loadManifest = useCallback(async () => {
    try {
      setIsLoading(true);
      const res = await fetch('/api/manifest');
      if (!res.ok) throw new Error('Failed to load inventory manifest');
      const data: Photo[] = await res.json();
      setPhotos(data);

      // Send to worker
      if (workerRef.current) {
        workerRef.current.postMessage({
          type: 'INIT_MANIFEST',
          manifest: data,
        });
      }
    } catch (err) {
      console.error('Error loading manifest:', err);
    } finally {
      setIsLoading(false);
    }
  }, []);

  // Initialize Search Web Worker
  useEffect(() => {
    const worker = new Worker(new URL('./workers/searchWorker.ts', import.meta.url), {
      type: 'module',
    });
    workerRef.current = worker;

    worker.onmessage = (e: MessageEvent<WorkerOutgoingMessage>) => {
      const msg = e.data;
      if (msg.type === 'INIT_READY') {
        if (initialQuery) {
          queryCounter.current += 1;
          worker.postMessage({
            type: 'SEARCH',
            query: initialQuery,
            queryId: queryCounter.current,
          });
        }
      } else if (msg.type === 'SEARCH_RESULTS') {
        // Only accept if matches latest query counter
        if (msg.queryId === queryCounter.current) {
          setMatchesByBinId(msg.matches);
          setHighCount(msg.highCount);
          setModerateCount(msg.moderateCount);
          setTotalMatches(msg.totalMatches);
        }
      }
    };

    loadManifest();

    return () => {
      worker.terminate();
      workerRef.current = null;
    };
  }, [loadManifest, initialQuery]);

  // Scroll a specific bin smoothly to the vertical center of the viewport
  const scrollToBin = useCallback(
    (photoId: string, binId: string) => {
      const cardEl = cardRefs.current.get(photoId);
      if (!cardEl) return;

      const photo = photos.find((p) => p.id === photoId);
      const bin = photo?.bins.find((b) => b.id === binId);
      if (!bin) return;

      const imgEl = cardEl.querySelector('img');
      if (!imgEl) return;

      const imgRect = imgEl.getBoundingClientRect();
      const vh = window.innerHeight;
      const [, by, , bh] = bin.bbox;

      // Center of the bin in image pixel coordinates
      const binCenterPixelY = (by + bh / 2) * imgRect.height;
      // Absolute Y of the bin center in document coordinates
      const binAbsoluteY = window.scrollY + imgRect.top + binCenterPixelY;
      // Desired scroll position so that bin is in the middle of viewport
      const targetScrollY = binAbsoluteY - vh / 2;

      window.scrollTo({
        top: Math.max(0, targetScrollY),
        behavior: 'smooth',
      });
    },
    [photos]
  );

  // Auto-center the first match if user opened spacegrep with a pre-loaded URL query (?q=...)
  useEffect(() => {
    if (initialQuery && !hasAutoCenteredRef.current && orderedMatches.length > 0) {
      const first = orderedMatches[0];
      scrollToBin(first.photoId, first.binId);
      hasAutoCenteredRef.current = true;
    }
  }, [initialQuery, orderedMatches, scrollToBin]);

  // Cycle matches forward ('next') or backward ('prev')
  const cycleMatch = useCallback(
    (direction: 'next' | 'prev') => {
      if (orderedMatches.length === 0) return;

      let nextIndex: number;
      if (activeMatchIndex === null) {
        nextIndex = direction === 'next' ? 0 : orderedMatches.length - 1;
      } else {
        if (direction === 'next') {
          nextIndex = (activeMatchIndex + 1) % orderedMatches.length;
        } else {
          nextIndex = (activeMatchIndex - 1 + orderedMatches.length) % orderedMatches.length;
        }
      }

      setActiveMatchIndex(nextIndex);
      const target = orderedMatches[nextIndex];
      scrollToBin(target.photoId, target.binId);
    },
    [orderedMatches, activeMatchIndex, scrollToBin]
  );

  // Enter handler in searchbox: defocus and center the active/first match
  const handleSearchEnter = useCallback(() => {
    if (orderedMatches.length === 0) return;
    const targetIndex = activeMatchIndex !== null ? activeMatchIndex : 0;
    setActiveMatchIndex(targetIndex);
    const target = orderedMatches[targetIndex];
    scrollToBin(target.photoId, target.binId);
  }, [orderedMatches, activeMatchIndex, scrollToBin]);

  // Selection handler when an indicator or card element is clicked
  const handleSelectBin = useCallback(
    (binId: string) => {
      const idx = orderedMatches.findIndex((m) => m.binId === binId);
      if (idx !== -1) {
        setActiveMatchIndex(idx);
      }
    },
    [orderedMatches]
  );

  // Execute search when query changes
  const handleSearchChange = useCallback((newQuery: string) => {
    setSearchQuery(newQuery);
    setActiveMatchIndex(null);
    queryCounter.current += 1;
    const queryId = queryCounter.current;

    // Sync URL query parameter without full page reload
    if (typeof window !== 'undefined') {
      const url = new URL(window.location.href);
      if (newQuery.trim()) {
        url.searchParams.set('q', newQuery.trim());
      } else {
        url.searchParams.delete('q');
        url.searchParams.delete('search');
      }
      window.history.replaceState({}, '', url.toString());
    }

    if (!newQuery.trim()) {
      setMatchesByBinId({});
      setHighCount(0);
      setModerateCount(0);
      setTotalMatches(0);
      return;
    }

    if (workerRef.current) {
      workerRef.current.postMessage({
        type: 'SEARCH',
        query: newQuery,
        queryId,
      });
    }
  }, []);

  // Reset app state as if the kiosk timer expired ('q' shortcut or timer expiration)
  const resetKiosk = useCallback(() => {
    handleSearchChange('');
    window.scrollTo({ top: 0, behavior: 'smooth' });
    setKioskSecondsRemaining(KIOSK_TIMEOUT_SECONDS);
    setActiveMatchIndex(null);
    const activeEl = document.activeElement as HTMLElement | null;
    if (activeEl && activeEl.blur) {
      activeEl.blur();
    }
  }, [handleSearchChange]);

  // Scroll to the next or previous shelf image boundary, offset below the floating omnibar
  const scrollToShelfBoundary = useCallback(
    (direction: 'next' | 'prev') => {
      if (photos.length === 0) return;

      const TOP_NAV_OFFSET = 80;
      const SCROLL_TOLERANCE = 10;

      // Collect target scroll positions for each shelf card in visual sequence
      const targets: number[] = [];
      for (const photo of photos) {
        const cardEl = cardRefs.current.get(photo.id);
        if (cardEl) {
          const rect = cardEl.getBoundingClientRect();
          const cardAbsoluteTop = window.scrollY + rect.top;
          targets.push(Math.max(0, cardAbsoluteTop - TOP_NAV_OFFSET));
        }
      }

      if (targets.length === 0) return;

      const currentY = window.scrollY;

      if (direction === 'next') {
        const nextTarget = targets.find((t) => t > currentY + SCROLL_TOLERANCE);
        window.scrollTo({
          top: nextTarget !== undefined ? nextTarget : targets[0],
          behavior: 'smooth',
        });
      } else {
        // Snaps to the top of current shelf if scrolled partway down,
        // otherwise jumps to the previous shelf, or wraps to the last shelf.
        const prevTargets = targets.filter((t) => t < currentY - SCROLL_TOLERANCE);
        const prevTarget =
          prevTargets.length > 0 ? prevTargets[prevTargets.length - 1] : targets[targets.length - 1];
        window.scrollTo({
          top: prevTarget,
          behavior: 'smooth',
        });
      }
    },
    [photos]
  );

  // Global hotkeys for 'q' (reset), 'n' (next match), 'N' (prev match), and 'Space'/'Shift+Space' (image boundaries)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const isAnyModalOpen =
        isUploadOpen ||
        Boolean(authModalState?.isOpen) ||
        Boolean(renameModalPhoto) ||
        Boolean(replaceModalPhoto) ||
        Boolean(deleteConfirmPhoto);

      if (isAnyModalOpen) return;

      // Ignore if focused on an input, textarea, or contentEditable
      const activeEl = document.activeElement as HTMLElement | null;
      const tagName = activeEl?.tagName;
      if (tagName === 'INPUT' || tagName === 'TEXTAREA' || activeEl?.isContentEditable) {
        return;
      }

      if ((e.key === 'q' || e.key === 'Q') && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        resetKiosk();
      } else if (e.key === 'n' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        cycleMatch('next');
      } else if (
        (e.key === 'N' || (e.key === 'n' && e.shiftKey)) &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        e.preventDefault();
        cycleMatch('prev');
      } else if ((e.key === ' ' || e.code === 'Space') && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        scrollToShelfBoundary(e.shiftKey ? 'prev' : 'next');
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [
    isUploadOpen,
    authModalState,
    renameModalPhoto,
    replaceModalPhoto,
    deleteConfirmPhoto,
    resetKiosk,
    cycleMatch,
    scrollToShelfBoundary,
  ]);

  // Keep activeMatchIndex in bounds if results update
  useEffect(() => {
    if (orderedMatches.length === 0) {
      if (activeMatchIndex !== null) setActiveMatchIndex(null);
    } else if (activeMatchIndex !== null && activeMatchIndex >= orderedMatches.length) {
      setActiveMatchIndex(0);
    }
  }, [orderedMatches.length, activeMatchIndex]);

  // Kiosk inactivity timer: disabled if user arrived via pre-loaded URL search query (?q= or ?search=)
  useEffect(() => {
    if (!kioskEnabled || hasUrlQuery) return;

    const resetTimer = () => {
      setKioskSecondsRemaining(KIOSK_TIMEOUT_SECONDS);
    };

    const interval = setInterval(() => {
      if (!searchQuery && window.scrollY < 20) {
        setKioskSecondsRemaining(KIOSK_TIMEOUT_SECONDS);
        return;
      }

      setKioskSecondsRemaining((prev) => {
        if (prev <= 1) {
          resetKiosk();
          return KIOSK_TIMEOUT_SECONDS;
        }
        return prev - 1;
      });
    }, 1000);

    const userActivityEvents = ['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll'];
    userActivityEvents.forEach((evt) => window.addEventListener(evt, resetTimer, { passive: true }));

    return () => {
      clearInterval(interval);
      userActivityEvents.forEach((evt) => window.removeEventListener(evt, resetTimer));
    };
  }, [kioskEnabled, hasUrlQuery, searchQuery, resetKiosk]);

  // Fullscreen toggle handler
  const handleToggleFullscreen = () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
      setIsFullscreen(true);
    } else {
      document.exitFullscreen().catch(() => {});
      setIsFullscreen(false);
    }
  };

  useEffect(() => {
    const onFsChange = () => {
      setIsFullscreen(Boolean(document.fullscreenElement));
    };
    document.addEventListener('fullscreenchange', onFsChange);
    return () => document.removeEventListener('fullscreenchange', onFsChange);
  }, []);

  // Handlers for upload and photo management
  const refreshSearchIfActive = useCallback(() => {
    if (searchQuery.trim() && workerRef.current) {
      queryCounter.current += 1;
      workerRef.current.postMessage({
        type: 'SEARCH',
        query: searchQuery,
        queryId: queryCounter.current,
      });
    }
  }, [searchQuery]);

  const handleUploadSuccess = (newPhoto: Photo) => {
    setPhotos((prev) => {
      const exists = prev.some((p) => p.id === newPhoto.id);
      const updated = exists
        ? prev.map((p) => (p.id === newPhoto.id ? newPhoto : p))
        : [...prev, newPhoto];
      if (workerRef.current) {
        workerRef.current.postMessage({
          type: 'INIT_MANIFEST',
          manifest: updated,
        });
      }
      return updated;
    });
    refreshSearchIfActive();
  };

  const handleRenameSuccess = (updatedPhoto: Photo) => {
    setPhotos((prev) => {
      const updated = prev.map((p) => (p.id === updatedPhoto.id ? updatedPhoto : p));
      if (workerRef.current) {
        workerRef.current.postMessage({
          type: 'INIT_MANIFEST',
          manifest: updated,
        });
      }
      return updated;
    });
    refreshSearchIfActive();
  };

  const handleDeleteSuccess = (photoId: string) => {
    setPhotos((prev) => {
      const updated = prev.filter((p) => p.id !== photoId);
      if (workerRef.current) {
        workerRef.current.postMessage({
          type: 'INIT_MANIFEST',
          manifest: updated,
        });
      }
      return updated;
    });
    refreshSearchIfActive();
  };

  const executeMove = async (photoId: string, direction: 'up' | 'down', token: string) => {
    // Optimistically update photo order in local state for instantaneous feedback
    setPhotos((prev) => {
      const idx = prev.findIndex((p) => p.id === photoId);
      if (idx === -1) return prev;
      if (direction === 'up' && idx === 0) return prev;
      if (direction === 'down' && idx === prev.length - 1) return prev;
      const targetIdx = direction === 'up' ? idx - 1 : idx + 1;
      const copy = [...prev];
      const temp = copy[idx];
      copy[idx] = copy[targetIdx];
      copy[targetIdx] = temp;
      return copy;
    });

    try {
      const res = await fetch(`/api/photos/${photoId}/move`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ direction }),
      });

      if (!res.ok) {
        if (res.status === 401) {
          clearStoredToken();
          loadManifest();
          const targetPhoto = photos.find((p) => p.id === photoId) || null;
          setAuthModalState({
            isOpen: true,
            actionType: direction === 'up' ? 'move_up' : 'move_down',
            photo: targetPhoto,
            initialError: 'Admin session expired. Please enter password again.',
          });
          return;
        }
        console.error('Failed to move photo');
        loadManifest();
        return;
      }

      const data = await res.json();
      if (data.photos) {
        setPhotos(data.photos);
        if (workerRef.current) {
          workerRef.current.postMessage({
            type: 'INIT_MANIFEST',
            manifest: data.photos,
          });
        }
      }
    } catch (err) {
      console.error('Error moving photo:', err);
      loadManifest();
    }
  };

  const executeDeletePhoto = async (photo: Photo, token: string) => {
    setIsDeleting(true);
    try {
      const res = await fetch(`/api/photos/${photo.id}`, {
        method: 'DELETE',
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      if (!res.ok) {
        if (res.status === 401) {
          clearStoredToken();
          setIsDeleting(false);
          setDeleteConfirmPhoto(null);
          setAuthModalState({
            isOpen: true,
            actionType: 'delete',
            photo,
            initialError: 'Admin session expired. Please enter password again.',
          });
          return;
        }
        throw new Error('Failed to delete photo');
      }

      handleDeleteSuccess(photo.id);
      setDeleteConfirmPhoto(null);
    } catch (err) {
      console.error('Delete error:', err);
    } finally {
      setIsDeleting(false);
    }
  };

  const handleRequestMove = (photo: Photo, direction: 'up' | 'down') => {
    const token = getStoredToken();
    if (!token) {
      setAuthModalState({
        isOpen: true,
        actionType: direction === 'up' ? 'move_up' : 'move_down',
        photo,
      });
      return;
    }
    executeMove(photo.id, direction, token);
  };

  const handleRequestRename = (photo: Photo) => {
    const token = getStoredToken();
    if (!token) {
      setAuthModalState({
        isOpen: true,
        actionType: 'rename',
        photo,
      });
      return;
    }
    setRenameModalPhoto(photo);
  };

  const handleRequestReplace = (photo: Photo) => {
    const token = getStoredToken();
    if (!token) {
      setAuthModalState({
        isOpen: true,
        actionType: 'replace',
        photo,
      });
      return;
    }
    setReplaceModalPhoto(photo);
  };

  const handleRequestDelete = (photo: Photo) => {
    const token = getStoredToken();
    if (!token) {
      setAuthModalState({
        isOpen: true,
        actionType: 'delete',
        photo,
      });
      return;
    }
    setDeleteConfirmPhoto(photo);
  };

  const handleOpenUpload = () => {
    const token = getStoredToken();
    if (!token) {
      setAuthModalState({
        isOpen: true,
        actionType: 'upload',
        photo: null,
      });
      return;
    }
    setIsUploadOpen(true);
  };

  const handleAuthSuccess = (token: string) => {
    if (!authModalState) return;
    const { actionType, photo } = authModalState;
    setAuthModalState(null);

    if (actionType === 'upload') {
      setIsUploadOpen(true);
      return;
    }

    if (!photo) return;

    if (actionType === 'move_up') {
      executeMove(photo.id, 'up', token);
    } else if (actionType === 'move_down') {
      executeMove(photo.id, 'down', token);
    } else if (actionType === 'rename') {
      setRenameModalPhoto(photo);
    } else if (actionType === 'replace') {
      setReplaceModalPhoto(photo);
    } else if (actionType === 'delete') {
      executeDeletePhoto(photo, token);
    }
  };

  return (
    <div className="min-h-screen bg-[#0b0f17] text-slate-100 flex flex-col relative selection:bg-cyan-500/30 selection:text-cyan-200">
      {/* Background Ingest / Seed Progress Banner (shifts page down and scrolls away) */}
      <IngestBanner
        onJobCompleted={handleUploadSuccess}
        onBannerHeightChange={setBannerHeight}
      />

      {/* Pinned Top Omnibar */}
      <FloatingOmnibar
        bannerHeight={bannerHeight}
        searchQuery={searchQuery}
        onSearchChange={handleSearchChange}
        totalMatches={totalMatches}
        highCount={highCount}
        moderateCount={moderateCount}
        activeMatchIndex={activeMatchIndex}
        onEnter={handleSearchEnter}
        kioskSecondsRemaining={kioskSecondsRemaining}
        kioskEnabled={kioskEnabled && !hasUrlQuery}
        onToggleKiosk={() => setKioskEnabled(!kioskEnabled)}
        onOpenUpload={handleOpenUpload}
        isFullscreen={isFullscreen}
        onToggleFullscreen={handleToggleFullscreen}
      />

      {/* Main Continuous Stack of Shelf Images */}
      {isLoading ? (
        <div className="flex-1 flex flex-col items-center justify-center min-h-[70vh]">
          <div className="w-10 h-10 border-4 border-cyan-500/20 border-t-cyan-400 rounded-full animate-spin mb-4" />
          <p className="text-sm font-mono text-slate-400">Loading workshop inventory manifest...</p>
        </div>
      ) : (
        <ShelfStream
          photos={photos}
          matchesByBinId={matchesByBinId}
          cardRefs={cardRefs}
          activeBinId={activeBinId}
          onMoveUp={(p) => handleRequestMove(p, 'up')}
          onMoveDown={(p) => handleRequestMove(p, 'down')}
          onRename={handleRequestRename}
          onReplace={handleRequestReplace}
          onDeletePhoto={handleRequestDelete}
        />
      )}

      {/* Peripheral Radar / Off-screen Indicators */}
      <RadarIndicators
        photos={photos}
        matchesByBinId={matchesByBinId}
        cardRefs={cardRefs}
        activeBinId={activeBinId}
        onSelectBin={handleSelectBin}
      />

      {/* Add Shelf Photo Modal */}
      <UploadModal
        isOpen={isUploadOpen}
        onClose={() => setIsUploadOpen(false)}
        onUploadSuccess={handleUploadSuccess}
        onAuthExpired={() => {
          setIsUploadOpen(false);
          setAuthModalState({
            isOpen: true,
            actionType: 'upload',
            photo: null,
            initialError: 'Admin session expired. Please enter password again.',
          });
        }}
      />

      {/* Dropdown Action Admin Auth Modal */}
      {authModalState && (
        <AdminAuthModal
          isOpen={authModalState.isOpen}
          actionType={authModalState.actionType}
          photoName={authModalState.photo?.original_name}
          binsCount={authModalState.photo?.bins.length}
          initialError={authModalState.initialError}
          onSuccess={handleAuthSuccess}
          onClose={() => setAuthModalState(null)}
        />
      )}

      {/* Rename Modal */}
      <RenameModal
        isOpen={Boolean(renameModalPhoto)}
        photo={renameModalPhoto}
        onClose={() => setRenameModalPhoto(null)}
        onSuccess={handleRenameSuccess}
        onAuthExpired={(p) => {
          setRenameModalPhoto(null);
          setAuthModalState({
            isOpen: true,
            actionType: 'rename',
            photo: p,
            initialError: 'Admin session expired. Please enter password again.',
          });
        }}
      />

      {/* Replace Modal */}
      <ReplaceModal
        isOpen={Boolean(replaceModalPhoto)}
        photo={replaceModalPhoto}
        onClose={() => setReplaceModalPhoto(null)}
        onSuccess={handleUploadSuccess}
        onAuthExpired={(p) => {
          setReplaceModalPhoto(null);
          setAuthModalState({
            isOpen: true,
            actionType: 'replace',
            photo: p,
            initialError: 'Admin session expired. Please enter password again.',
          });
        }}
      />

      {/* Delete Confirmation Modal (when user was already authenticated) */}
      <DeleteConfirmModal
        isOpen={Boolean(deleteConfirmPhoto)}
        photo={deleteConfirmPhoto}
        isDeleting={isDeleting}
        onClose={() => setDeleteConfirmPhoto(null)}
        onConfirm={() => {
          const token = getStoredToken();
          if (token && deleteConfirmPhoto) {
            executeDeletePhoto(deleteConfirmPhoto, token);
          } else if (deleteConfirmPhoto) {
            const p = deleteConfirmPhoto;
            setDeleteConfirmPhoto(null);
            setAuthModalState({
              isOpen: true,
              actionType: 'delete',
              photo: p,
            });
          }
        }}
      />
    </div>
  );
}

export default App;
