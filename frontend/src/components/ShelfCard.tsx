import { forwardRef } from 'react';
import type { Photo, MatchResult } from '../types';
import { HighlightOverlay } from './HighlightOverlay';
import { ShelfMenu } from './ShelfMenu';

interface ShelfCardProps {
  photo: Photo;
  matchesByBinId: Record<string, MatchResult>;
  activeBinId?: string | null;
  isFirst?: boolean;
  isLast?: boolean;
  onMoveUp?: (photo: Photo) => void;
  onMoveDown?: (photo: Photo) => void;
  onRename?: (photo: Photo) => void;
  onReplace?: (photo: Photo) => void;
  onDelete?: (photo: Photo) => void;
}

export const ShelfCard = forwardRef<HTMLDivElement, ShelfCardProps>(
  (
    {
      photo,
      matchesByBinId,
      activeBinId,
      isFirst = false,
      isLast = false,
      onMoveUp,
      onMoveDown,
      onRename,
      onReplace,
      onDelete,
    },
    ref
  ) => {
    const shelfMatches = photo.bins.filter((b) => Boolean(matchesByBinId[b.id]));
    const hasMatches = shelfMatches.length > 0;

    return (
      <section
        ref={ref}
        data-photo-id={photo.id}
        className="relative w-full border-b border-slate-800/80 bg-[#0c111d] flex flex-col items-center select-none"
      >
        {/* Subtle shelf header banner */}
        <div className="w-full max-w-7xl px-4 py-2 flex items-center justify-between text-xs text-slate-400 font-mono">
          <div className="flex items-center space-x-2">
            {onMoveUp && onMoveDown && onRename && onReplace && onDelete ? (
              <ShelfMenu
                photo={photo}
                isFirst={isFirst}
                isLast={isLast}
                onMoveUp={onMoveUp}
                onMoveDown={onMoveDown}
                onRename={onRename}
                onReplace={onReplace}
                onDelete={onDelete}
              />
            ) : (
              <span className="inline-block w-2 h-2 rounded-full bg-slate-600" />
            )}
            <span className="font-semibold text-slate-300">{photo.original_name}</span>
            <span className="text-slate-500">({photo.bins.length} bins indexed)</span>
          </div>

          {hasMatches && (
            <div className="flex items-center space-x-2">
              <span className="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-yellow-950/80 text-yellow-300 border border-yellow-500/50">
                {shelfMatches.length} {shelfMatches.length === 1 ? 'match' : 'matches'}
              </span>
            </div>
          )}
        </div>

        {/* Full-width responsive image wrapper */}
        <div className="relative w-full max-w-7xl overflow-hidden shadow-2xl">
          <img
            src={`/images/${photo.filename}?v=${photo.image_hash?.slice(0, 8) || photo.created_at}`}
            alt={photo.original_name}
            className="w-full h-auto block pointer-events-none"
            loading="eager"
            decoding="async"
          />

          {/* SVG Overlay */}
          <HighlightOverlay
            bins={photo.bins}
            matchesByBinId={matchesByBinId}
            activeBinId={activeBinId}
          />
        </div>
      </section>
    );
  }
);

ShelfCard.displayName = 'ShelfCard';
