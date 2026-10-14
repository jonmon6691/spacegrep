import React, { useState, useRef, useEffect } from 'react';
import { Settings, ArrowUp, ArrowDown, Edit2, RefreshCw, Trash2 } from 'lucide-react';
import type { Photo } from '../types';

interface ShelfMenuProps {
  photo: Photo;
  isFirst: boolean;
  isLast: boolean;
  onMoveUp: (photo: Photo) => void;
  onMoveDown: (photo: Photo) => void;
  onRename: (photo: Photo) => void;
  onReplace: (photo: Photo) => void;
  onDelete: (photo: Photo) => void;
}

export const ShelfMenu: React.FC<ShelfMenuProps> = ({
  photo,
  isFirst,
  isLast,
  onMoveUp,
  onMoveDown,
  onRename,
  onReplace,
  onDelete,
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;

    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setIsOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen]);

  const handleAction = (action: () => void) => {
    setIsOpen(false);
    action();
  };

  return (
    <div ref={menuRef} className="relative inline-block text-left select-none">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setIsOpen(!isOpen);
        }}
        aria-haspopup="true"
        aria-expanded={isOpen}
        className={`p-1 -ml-1 rounded-md transition-colors ${
          isOpen
            ? 'bg-slate-800 text-cyan-300 ring-1 ring-slate-700'
            : 'text-slate-500 hover:text-slate-200 hover:bg-slate-800/80'
        }`}
        title="Shelf actions (Move, Rename, Replace, Delete)"
      >
        <Settings className="w-3.5 h-3.5" />
      </button>

      {isOpen && (
        <div
          role="menu"
          aria-orientation="vertical"
          className="absolute left-0 mt-1.5 w-48 rounded-xl bg-slate-900/95 backdrop-blur-xl border border-slate-700/80 shadow-2xl py-1.5 z-30 animate-in fade-in zoom-in-95 duration-100 font-sans"
        >
          {/* Move Up */}
          <button
            type="button"
            role="menuitem"
            disabled={isFirst}
            onClick={() => handleAction(() => onMoveUp(photo))}
            className="w-full flex items-center space-x-2.5 px-3 py-1.5 text-xs text-left text-slate-300 hover:text-white hover:bg-slate-800/70 transition-colors disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-slate-300"
          >
            <ArrowUp className="w-3.5 h-3.5 text-cyan-400 flex-shrink-0" />
            <span>Move Up</span>
          </button>

          {/* Move Down */}
          <button
            type="button"
            role="menuitem"
            disabled={isLast}
            onClick={() => handleAction(() => onMoveDown(photo))}
            className="w-full flex items-center space-x-2.5 px-3 py-1.5 text-xs text-left text-slate-300 hover:text-white hover:bg-slate-800/70 transition-colors disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-slate-300"
          >
            <ArrowDown className="w-3.5 h-3.5 text-cyan-400 flex-shrink-0" />
            <span>Move Down</span>
          </button>

          {/* Rename */}
          <button
            type="button"
            role="menuitem"
            onClick={() => handleAction(() => onRename(photo))}
            className="w-full flex items-center space-x-2.5 px-3 py-1.5 text-xs text-left text-slate-300 hover:text-white hover:bg-slate-800/70 transition-colors"
          >
            <Edit2 className="w-3.5 h-3.5 text-cyan-400 flex-shrink-0" />
            <span>Rename Shelf</span>
          </button>

          {/* Replace */}
          <button
            type="button"
            role="menuitem"
            onClick={() => handleAction(() => onReplace(photo))}
            className="w-full flex items-center space-x-2.5 px-3 py-1.5 text-xs text-left text-slate-300 hover:text-white hover:bg-slate-800/70 transition-colors"
          >
            <RefreshCw className="w-3.5 h-3.5 text-cyan-400 flex-shrink-0" />
            <span>Replace Photo</span>
          </button>

          {/* Divider */}
          <div className="my-1 border-t border-slate-800" />

          {/* Delete */}
          <button
            type="button"
            role="menuitem"
            onClick={() => handleAction(() => onDelete(photo))}
            className="w-full flex items-center space-x-2.5 px-3 py-1.5 text-xs text-left text-red-400 hover:text-red-300 hover:bg-red-950/40 transition-colors"
          >
            <Trash2 className="w-3.5 h-3.5 flex-shrink-0" />
            <span>Delete Shelf</span>
          </button>
        </div>
      )}
    </div>
  );
};
