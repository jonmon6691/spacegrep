import React from 'react';
import type { Photo, MatchResult } from '../types';
import { ShelfCard } from './ShelfCard';

interface ShelfStreamProps {
  photos: Photo[];
  matchesByBinId: Record<string, MatchResult>;
  cardRefs: React.MutableRefObject<Map<string, HTMLDivElement>>;
  activeBinId?: string | null;
  onMoveUp?: (photo: Photo) => void;
  onMoveDown?: (photo: Photo) => void;
  onRename?: (photo: Photo) => void;
  onReplace?: (photo: Photo) => void;
  onDeletePhoto?: (photo: Photo) => void;
}

export const ShelfStream: React.FC<ShelfStreamProps> = ({
  photos,
  matchesByBinId,
  cardRefs,
  activeBinId,
  onMoveUp,
  onMoveDown,
  onRename,
  onReplace,
  onDeletePhoto,
}) => {
  if (photos.length === 0) {
    return (
      <div className="w-full min-h-[60vh] flex flex-col items-center justify-center text-center p-8">
        <div className="w-16 h-16 rounded-2xl bg-slate-800/80 border border-slate-700 flex items-center justify-center mb-4 text-slate-400">
          <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
        </div>
        <h3 className="text-xl font-bold text-slate-200 mb-1">No Workshop Shelves Loaded</h3>
        <p className="text-slate-400 max-w-md text-sm">
          Drop shelf photos into <code className="text-cyan-400 font-mono">./data/seed_photos/</code> or click <span className="text-cyan-400 font-semibold">Upload Photo</span> in the top bar.
        </p>
      </div>
    );
  }

  return (
    <main className="w-full flex flex-col items-center pt-28 sm:pt-24 pb-32">
      {photos.map((photo, index) => (
        <ShelfCard
          key={photo.id}
          ref={(el) => {
            if (el) {
              cardRefs.current.set(photo.id, el);
            } else {
              cardRefs.current.delete(photo.id);
            }
          }}
          photo={photo}
          matchesByBinId={matchesByBinId}
          activeBinId={activeBinId}
          isFirst={index === 0}
          isLast={index === photos.length - 1}
          onMoveUp={onMoveUp}
          onMoveDown={onMoveDown}
          onRename={onRename}
          onReplace={onReplace}
          onDelete={onDeletePhoto}
        />
      ))}
    </main>
  );
};
