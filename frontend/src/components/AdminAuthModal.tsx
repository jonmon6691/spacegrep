import React, { useState, useEffect } from 'react';
import { Lock, AlertCircle, Loader2, Trash2, ArrowUpDown, Edit2, RefreshCw, UploadCloud } from 'lucide-react';
import { setStoredToken } from '../utils/auth';

export type AdminActionType = 'delete' | 'replace' | 'rename' | 'move_up' | 'move_down' | 'upload';

interface AdminAuthModalProps {
  isOpen: boolean;
  actionType: AdminActionType;
  photoName?: string;
  binsCount?: number;
  initialError?: string;
  onSuccess: (token: string) => void;
  onClose: () => void;
}

export const AdminAuthModal: React.FC<AdminAuthModalProps> = ({
  isOpen,
  actionType,
  photoName,
  binsCount,
  initialError = '',
  onSuccess,
  onClose,
}) => {
  const [passwordInput, setPasswordInput] = useState('');
  const [passwordError, setPasswordError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (isOpen) {
      setPasswordInput('');
      setPasswordError(initialError);
      setIsSubmitting(false);
    }
  }, [isOpen, initialError]);

  if (!isOpen) return null;

  const isDelete = actionType === 'delete';

  const getSubtitle = () => {
    switch (actionType) {
      case 'delete':
        return 'Enter admin password to delete shelf photo';
      case 'replace':
        return 'Enter admin password to replace shelf photo';
      case 'rename':
        return 'Enter admin password to rename shelf photo';
      case 'move_up':
      case 'move_down':
        return 'Enter admin password to reorder shelves';
      case 'upload':
        return 'Enter admin password to upload new shelf photo';
      default:
        return 'Enter admin password to continue';
    }
  };

  const getActionIcon = () => {
    switch (actionType) {
      case 'delete':
        return <Trash2 className="w-5 h-5 text-red-400" />;
      case 'replace':
        return <RefreshCw className="w-5 h-5 text-cyan-400" />;
      case 'rename':
        return <Edit2 className="w-5 h-5 text-cyan-400" />;
      case 'move_up':
      case 'move_down':
        return <ArrowUpDown className="w-5 h-5 text-cyan-400" />;
      case 'upload':
        return <UploadCloud className="w-5 h-5 text-cyan-400" />;
      default:
        return <Lock className="w-5 h-5 text-cyan-400" />;
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!passwordInput.trim() || isSubmitting) return;

    setIsSubmitting(true);
    setPasswordError('');

    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ password: passwordInput }),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        if (res.status === 429) {
          setPasswordError(errorData.detail || 'Too many failed attempts. Please wait 5 minutes.');
        } else {
          setPasswordError(errorData.detail || 'Invalid admin password. Please try again.');
        }
        setIsSubmitting(false);
        return;
      }

      const data = await res.json();
      const token = data.token;
      setStoredToken(token);
      setIsSubmitting(false);
      onSuccess(token);
    } catch (err: any) {
      console.error('Authentication error:', err);
      setPasswordError(err.message || 'Authentication failed. Please try again.');
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-in fade-in duration-150">
      <div className="relative w-full max-w-md bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl p-6 space-y-4">
        {/* Header */}
        <div className="flex items-center space-x-3">
          <div
            className={`p-2.5 rounded-xl border flex-shrink-0 ${
              isDelete
                ? 'bg-red-950/70 border-red-800/60'
                : 'bg-cyan-950/70 border-cyan-800/60'
            }`}
          >
            {getActionIcon()}
          </div>
          <div>
            <h3 className="text-base font-semibold text-slate-100">Admin Authentication</h3>
            <p className="text-xs text-slate-400">{getSubtitle()}</p>
          </div>
        </div>

        {/* Target Details Box */}
        {photoName && (
          <div className="text-xs text-slate-400 bg-slate-800/50 p-3 rounded-xl border border-slate-700/60 space-y-1">
            <p className="font-mono text-slate-300 truncate">
              <span className="text-slate-500">Target Shelf:</span> {photoName}
            </p>
            {isDelete && binsCount !== undefined && (
              <p className="text-[11px] text-red-400 font-sans">
                Warning: This will permanently delete this shelf photo and all {binsCount} indexed bins.
              </p>
            )}
            {actionType === 'replace' && (
              <p className="text-[11px] text-amber-300/90 font-sans">
                Note: Replacing will re-scan the shelf with AI and update all bin annotations.
              </p>
            )}
            <p className="text-[11px] text-slate-500 font-sans pt-0.5">
              Password grants a 1-hour Bearer token session.
            </p>
          </div>
        )}

        {/* Password Form */}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-slate-300 mb-1.5">
              Admin Password
            </label>
            <input
              type="password"
              value={passwordInput}
              onChange={(e) => {
                setPasswordInput(e.target.value);
                setPasswordError('');
              }}
              placeholder="Enter admin password..."
              autoFocus
              disabled={isSubmitting}
              className="w-full px-3.5 py-2.5 bg-slate-950 border border-slate-700 rounded-xl text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 font-mono transition-colors"
            />
            {passwordError && (
              <div className="flex items-center space-x-1.5 mt-2 text-xs text-red-400">
                <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
                <span>{passwordError}</span>
              </div>
            )}
          </div>

          <div className="flex items-center justify-end space-x-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              disabled={isSubmitting}
              className="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSubmitting || !passwordInput.trim()}
              className={`px-4 py-2 rounded-xl text-white text-xs font-medium transition-colors flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed shadow-lg ${
                isDelete
                  ? 'bg-red-600 hover:bg-red-500 shadow-red-950/50'
                  : 'bg-cyan-600 hover:bg-cyan-500 shadow-cyan-950/50'
              }`}
            >
              {isSubmitting ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span>Verifying...</span>
                </>
              ) : (
                <span>
                  {isDelete
                    ? 'Confirm & Delete'
                    : actionType === 'upload'
                    ? 'Authenticate & Upload'
                    : 'Authenticate'}
                </span>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
