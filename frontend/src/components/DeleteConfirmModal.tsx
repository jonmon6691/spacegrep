import React from 'react';
import { Trash2, Loader2, X } from 'lucide-react';
import type { Photo } from '../types';

interface DeleteConfirmModalProps {
  isOpen: boolean;
  photo: Photo | null;
  isDeleting: boolean;
  onClose: () => void;
  onConfirm: () => void;
}

export const DeleteConfirmModal: React.FC<DeleteConfirmModalProps> = ({
  isOpen,
  photo,
  isDeleting,
  onClose,
  onConfirm,
}) => {
  if (!isOpen || !photo) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-in fade-in duration-150">
      <div className="relative w-full max-w-md bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl p-6 space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="p-2.5 rounded-xl border border-red-800/60 bg-red-950/70 text-red-400">
              <Trash2 className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-base font-semibold text-slate-100">Delete Shelf Photo</h3>
              <p className="text-xs text-slate-400">Permanently remove this shelf from inventory</p>
            </div>
          </div>
          <button
            onClick={onClose}
            disabled={isDeleting}
            className="p-1 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors disabled:opacity-50"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Info */}
        <div className="text-xs text-slate-400 bg-slate-800/50 p-3 rounded-xl border border-slate-700/60 space-y-1">
          <p className="font-mono text-slate-300 truncate">
            <span className="text-slate-500">Delete:</span> {photo.original_name}
          </p>
          <p className="text-[11px] text-red-400">
            Warning: This will permanently delete this shelf photo and all {photo.bins.length} indexed bins from search.
          </p>
        </div>

        {/* Buttons */}
        <div className="flex items-center justify-end space-x-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={isDeleting}
            className="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isDeleting}
            className="px-4 py-2 rounded-xl bg-red-600 hover:bg-red-500 text-white text-xs font-medium transition-colors flex items-center space-x-2 shadow-lg shadow-red-950/50 disabled:opacity-50"
          >
            {isDeleting ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                <span>Deleting...</span>
              </>
            ) : (
              <span>Confirm & Delete</span>
            )}
          </button>
        </div>
      </div>
    </div>
  );
};
