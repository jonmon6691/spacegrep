import React, { useState, useRef } from 'react';
import { RefreshCw, UploadCloud, AlertCircle, Loader2, X, Sparkles, Layers } from 'lucide-react';
import type { Photo, IngestJobState } from '../types';
import { getStoredToken, clearStoredToken } from '../utils/auth';

interface ReplaceModalProps {
  isOpen: boolean;
  photo: Photo | null;
  onClose: () => void;
  onSuccess: (updatedPhoto: Photo) => void;
  onAuthExpired: (photo: Photo) => void;
}

export const ReplaceModal: React.FC<ReplaceModalProps> = ({
  isOpen,
  photo,
  onClose,
  onSuccess,
  onAuthExpired,
}) => {
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadStage, setUploadStage] = useState('preparing');
  const [uploadMessage, setUploadMessage] = useState('');
  const [uploadEncouragement, setUploadEncouragement] = useState('');
  const [uploadBinsCount, setUploadBinsCount] = useState(0);
  const [error, setError] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  if (!isOpen || !photo) return null;

  const handleFile = async (file: File) => {
    if (!file.type.startsWith('image/')) {
      setError('Please select a valid image file (JPEG, PNG, or WebP).');
      return;
    }

    const token = getStoredToken();
    if (!token) {
      onAuthExpired(photo);
      return;
    }

    setIsUploading(true);
    setError('');
    setUploadProgress(5);
    setUploadStage('preparing');
    setUploadMessage('Uploading replacement image...');
    setUploadEncouragement('Uploading new shelf photo to the server...');
    setUploadBinsCount(0);

    try {
      const formData = new FormData();
      formData.append('file', file);

      const res = await fetch(`/api/photos/${photo.id}/replace`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
        },
        body: formData,
      });

      if (!res.ok) {
        if (res.status === 401) {
          clearStoredToken();
          setIsUploading(false);
          onAuthExpired(photo);
          return;
        }
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to replace shelf photo');
      }

      const resData = await res.json();

      // Synchronous return
      if (res.status === 200 && resData.photo) {
        setUploadProgress(100);
        setUploadStage('complete');
        setUploadEncouragement(`Success! ${resData.photo.bins.length} bins indexed!`);
        onSuccess(resData.photo);
        setTimeout(() => {
          setIsUploading(false);
          onClose();
        }, 1000);
        return;
      }

      // Asynchronous SSE stream
      const jobId = resData.job_id;
      if (!jobId) {
        // Even if no SSE jobId, close and let background banner handle it
        onClose();
        return;
      }

      await new Promise<void>((resolve, reject) => {
        let isDone = false;
        const es = new EventSource(`/api/ingest/jobs/${jobId}/stream`);

        const cleanup = () => {
          if (!isDone) {
            isDone = true;
            es.close();
          }
        };

        es.onmessage = (event) => {
          try {
            const job: IngestJobState = JSON.parse(event.data);
            setUploadProgress(job.progress);
            setUploadStage(job.stage);
            setUploadMessage(job.message);
            setUploadEncouragement(job.encouragement);
            setUploadBinsCount(job.bins_count);

            if (job.status === 'completed' && job.photo_record) {
              cleanup();
              onSuccess(job.photo_record);
              setTimeout(() => {
                setIsUploading(false);
                onClose();
                resolve();
              }, 1000);
            } else if (job.status === 'failed') {
              cleanup();
              reject(new Error(job.error || 'Ingestion failed on the server'));
            }
          } catch (e) {
            console.error('Error parsing SSE message:', e);
          }
        };

        es.onerror = () => {
          cleanup();
          // SSE disconnected, fallback to letting user close while IngestBanner tracks progress
          setIsUploading(false);
          onClose();
          resolve();
        };
      });
    } catch (err: any) {
      console.error('Replace photo error:', err);
      setError(err.message || 'An error occurred while replacing the shelf photo.');
      setIsUploading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-in fade-in duration-150">
      <div className="relative w-full max-w-lg bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl p-6 space-y-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="p-2.5 rounded-xl border border-cyan-800/60 bg-cyan-950/70 text-cyan-400">
              <RefreshCw className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-base font-semibold text-slate-100">Replace Shelf Photo</h3>
              <p className="text-xs text-slate-400">Upload a new photo for this shelf slot</p>
            </div>
          </div>
          <button
            onClick={onClose}
            disabled={isUploading}
            className="p-1 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors disabled:opacity-50"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Current Photo Info */}
        <div className="flex items-center space-x-3 p-3 rounded-xl bg-slate-800/50 border border-slate-700/60">
          <img
            src={`/images/thumb_${photo.filename}?v=${photo.image_hash?.slice(0, 8) || photo.created_at}`}
            alt={photo.original_name}
            className="w-14 h-10 object-cover rounded bg-slate-950 border border-slate-700 flex-shrink-0"
            onError={(e) => {
              (e.target as HTMLImageElement).src = `/images/${photo.filename}?v=${photo.image_hash?.slice(0, 8) || photo.created_at}`;
            }}
          />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-semibold text-slate-200 truncate">{photo.original_name}</p>
            <p className="text-[11px] text-slate-400 font-mono">
              Currently {photo.bins.length} bins indexed &bull; {photo.width}&times;{photo.height}px
            </p>
          </div>
        </div>

        {/* Drag & Drop Area */}
        <div
          onDragOver={(e) => {
            e.preventDefault();
            if (!isUploading) setIsDragging(true);
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setIsDragging(false);
            if (!isUploading && e.dataTransfer.files?.[0]) {
              handleFile(e.dataTransfer.files[0]);
            }
          }}
          onClick={() => {
            if (!isUploading) fileInputRef.current?.click();
          }}
          className={`border-2 border-dashed rounded-xl p-6 flex flex-col items-center justify-center text-center cursor-pointer transition-all duration-200 ${
            isDragging
              ? 'border-cyan-400 bg-cyan-950/20'
              : 'border-slate-700 hover:border-cyan-500/50 bg-slate-800/40 hover:bg-slate-800/70'
          }`}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            className="hidden"
            onChange={(e) => {
              if (e.target.files?.[0]) {
                handleFile(e.target.files[0]);
                e.target.value = '';
              }
            }}
          />

          {isUploading ? (
            <div className="flex flex-col items-center w-full max-w-sm py-2 space-y-3">
              <div className="flex items-center space-x-2">
                <Loader2 className="w-4 h-4 text-cyan-400 animate-spin" />
                <span className="text-xs font-mono font-semibold uppercase text-cyan-300">
                  {uploadStage}
                </span>
              </div>

              <div className="w-full space-y-1">
                <div className="flex justify-between items-center text-xs font-mono">
                  <span className="text-slate-300 truncate max-w-[240px]">{uploadMessage}</span>
                  <span className="text-cyan-300 font-bold">{uploadProgress}%</span>
                </div>
                <div className="w-full bg-slate-950 rounded-full h-2 overflow-hidden border border-slate-700">
                  <div
                    className="bg-gradient-to-r from-cyan-500 to-emerald-400 h-full rounded-full transition-all duration-300 ease-out"
                    style={{ width: `${Math.max(5, uploadProgress)}%` }}
                  />
                </div>
              </div>

              {uploadEncouragement && (
                <div className="w-full bg-slate-950/60 border border-cyan-800/40 rounded-lg p-2.5 flex items-start space-x-2 text-left">
                  <Sparkles className="w-3.5 h-3.5 text-cyan-400 flex-shrink-0 mt-0.5" />
                  <p className="text-xs text-slate-300 leading-snug">{uploadEncouragement}</p>
                </div>
              )}

              {uploadBinsCount > 0 && (
                <div className="flex items-center space-x-1.5 text-xs font-mono text-cyan-400">
                  <Layers className="w-3.5 h-3.5" />
                  <span>{uploadBinsCount} bins detected</span>
                </div>
              )}
            </div>
          ) : (
            <>
              <UploadCloud className="w-8 h-8 text-slate-400 mb-2" />
              <p className="text-xs font-medium text-slate-200">
                Drag and drop replacement photo here, or <span className="text-cyan-400 underline">browse</span>
              </p>
              <p className="text-[11px] text-slate-400 mt-1">
                Supports high-res JPEG, PNG, or WebP. AI will re-index bins for this slot.
              </p>
            </>
          )}
        </div>

        {error && (
          <div className="flex items-center space-x-2 p-2.5 rounded-lg bg-red-950/50 border border-red-800 text-red-300 text-xs">
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <div className="flex items-center justify-end space-x-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={isUploading}
            className="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
};
