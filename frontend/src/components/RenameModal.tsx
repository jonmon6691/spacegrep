import React, { useState, useEffect, useRef } from 'react';
import { Edit2, AlertCircle, Loader2, X } from 'lucide-react';
import type { Photo } from '../types';
import { getStoredToken, clearStoredToken } from '../utils/auth';

interface RenameModalProps {
  isOpen: boolean;
  photo: Photo | null;
  onClose: () => void;
  onSuccess: (updatedPhoto: Photo) => void;
  onAuthExpired: (photo: Photo) => void;
}

export const RenameModal: React.FC<RenameModalProps> = ({
  isOpen,
  photo,
  onClose,
  onSuccess,
  onAuthExpired,
}) => {
  const [nameInput, setNameInput] = useState('');
  const [error, setError] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isOpen && photo) {
      setNameInput(photo.original_name);
      setError('');
      setIsSaving(false);
      setTimeout(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      }, 50);
    }
  }, [isOpen, photo]);

  if (!isOpen || !photo) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const cleanName = nameInput.trim();
    if (!cleanName) {
      setError('Shelf name cannot be empty.');
      return;
    }

    if (cleanName === photo.original_name) {
      onClose();
      return;
    }

    const token = getStoredToken();
    if (!token) {
      onAuthExpired(photo);
      return;
    }

    setIsSaving(true);
    setError('');

    try {
      const res = await fetch(`/api/photos/${photo.id}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ name: cleanName }),
      });

      if (!res.ok) {
        if (res.status === 401) {
          clearStoredToken();
          setIsSaving(false);
          onAuthExpired(photo);
          return;
        }
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to rename shelf photo');
      }

      const data = await res.json();
      setIsSaving(false);
      onSuccess(data.photo);
      onClose();
    } catch (err: any) {
      console.error('Rename error:', err);
      setError(err.message || 'Failed to rename shelf photo');
      setIsSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-in fade-in duration-150">
      <div className="relative w-full max-w-md bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl p-6 space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="p-2.5 rounded-xl border border-cyan-800/60 bg-cyan-950/70 text-cyan-400">
              <Edit2 className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-base font-semibold text-slate-100">Rename Shelf Photo</h3>
              <p className="text-xs text-slate-400">Update the display name of this shelf</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="space-y-4 pt-1">
          <div>
            <label className="block text-xs font-medium text-slate-300 mb-1.5 font-mono">
              Shelf Display Name
            </label>
            <input
              ref={inputRef}
              type="text"
              value={nameInput}
              onChange={(e) => {
                setNameInput(e.target.value);
                setError('');
              }}
              placeholder="e.g. VHS_Upper_Shelf_01.jpg"
              disabled={isSaving}
              className="w-full px-3.5 py-2.5 bg-slate-950 border border-slate-700 rounded-xl text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 font-mono transition-colors"
            />
            {error && (
              <div className="flex items-center space-x-1.5 mt-2 text-xs text-red-400">
                <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
                <span>{error}</span>
              </div>
            )}
          </div>

          <div className="flex items-center justify-end space-x-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={isSaving}
              className="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSaving || !nameInput.trim()}
              className="px-4 py-2 rounded-xl bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-medium transition-colors flex items-center space-x-2 shadow-lg shadow-cyan-950/50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isSaving ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span>Saving...</span>
                </>
              ) : (
                <span>Save Name</span>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
