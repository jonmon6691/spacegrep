import React, { useState, useRef } from 'react';
import { X, UploadCloud, AlertCircle, Loader2, Sparkles, Layers } from 'lucide-react';
import type { Photo, IngestJobState } from '../types';
import { getStoredToken, clearStoredToken } from '../utils/auth';

interface UploadModalProps {
  isOpen: boolean;
  onClose: () => void;
  onUploadSuccess: (newPhoto: Photo) => void;
  onAuthExpired: () => void;
}

export const UploadModal: React.FC<UploadModalProps> = ({
  isOpen,
  onClose,
  onUploadSuccess,
  onAuthExpired,
}) => {
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<number>(0);
  const [uploadStage, setUploadStage] = useState<string>('preparing');
  const [uploadMessage, setUploadMessage] = useState<string>('');
  const [uploadEncouragement, setUploadEncouragement] = useState<string>('');
  const [uploadBinsCount, setUploadBinsCount] = useState<number>(0);
  const [uploadTiles, setUploadTiles] = useState<{ current: number; total: number }>({ current: 0, total: 1 });
  const [errorMessage, setErrorMessage] = useState<string>('');

  const fileInputRef = useRef<HTMLInputElement>(null);

  if (!isOpen) return null;

  const executeUpload = async (file: File, token: string) => {
    setIsUploading(true);
    setUploadProgress(5);
    setUploadStage('preparing');
    setUploadMessage('Uploading shelf image...');
    setUploadEncouragement('Sending your shelf image to the workshop server...');
    setUploadBinsCount(0);
    setUploadTiles({ current: 0, total: 1 });

    try {
      const formData = new FormData();
      formData.append('file', file);

      const res = await fetch('/api/photos', {
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
          onAuthExpired();
          return;
        }
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Upload and vision ingestion failed');
      }

      const resData = await res.json();

      // Synchronous fallback handling (e.g. if sync=true or direct return)
      if (res.status === 201 && resData.photo) {
        setUploadProgress(100);
        setUploadStage('complete');
        setUploadEncouragement(`Success! ${resData.photo.bins.length} bins indexed!`);
        onUploadSuccess(resData.photo);
        setTimeout(() => {
          setIsUploading(false);
        }, 1200);
        return;
      }

      // Asynchronous ingestion handling (HTTP 202)
      const jobId = resData.job_id;
      if (!jobId) {
        throw new Error('No job ID returned by server');
      }

      // Connect to SSE stream for live progress updates
      await new Promise<void>((resolve, reject) => {
        let isResolved = false;
        const es = new EventSource(`/api/ingest/jobs/${jobId}/stream`);

        const cleanup = () => {
          if (!isResolved) {
            isResolved = true;
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
            if (job.total_tiles) {
              setUploadTiles({ current: job.completed_tiles, total: job.total_tiles });
            }

            if (job.status === 'completed' && job.photo_record) {
              cleanup();
              onUploadSuccess(job.photo_record);
              setTimeout(() => {
                setIsUploading(false);
                resolve();
              }, 1200);
            } else if (job.status === 'failed') {
              cleanup();
              reject(new Error(job.error || 'Ingestion failed on the server'));
            }
          } catch (e) {
            console.error('Error parsing SSE message:', e);
          }
        };

        es.onerror = () => {
          // Fallback to polling if SSE is interrupted
          cleanup();
          const pollTimer = setInterval(async () => {
            try {
              const pollRes = await fetch(`/api/ingest/jobs/${jobId}`);
              if (!pollRes.ok) return;
              const job: IngestJobState = await pollRes.json();
              setUploadProgress(job.progress);
              setUploadStage(job.stage);
              setUploadMessage(job.message);
              setUploadEncouragement(job.encouragement);
              setUploadBinsCount(job.bins_count);

              if (job.status === 'completed' && job.photo_record) {
                clearInterval(pollTimer);
                onUploadSuccess(job.photo_record);
                setTimeout(() => {
                  setIsUploading(false);
                  resolve();
                }, 1200);
              } else if (job.status === 'failed') {
                clearInterval(pollTimer);
                reject(new Error(job.error || 'Ingestion failed'));
              }
            } catch (err) {
              clearInterval(pollTimer);
              reject(err);
            }
          }, 1500);
        };
      });
    } catch (err: any) {
      console.error('Upload error:', err);
      setErrorMessage(err.message || 'An error occurred during ingestion.');
      setIsUploading(false);
    }
  };

  const selectFileForUpload = (file: File) => {
    if (!file.type.startsWith('image/')) {
      setErrorMessage('Please select a valid image file (JPEG, PNG, or WebP).');
      return;
    }
    setErrorMessage('');

    const token = getStoredToken();
    if (token) {
      executeUpload(file, token);
    } else {
      onAuthExpired();
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-md">
      <div className="relative w-full max-w-xl bg-slate-900 border border-slate-700/80 rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[85vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-800">
          <div>
            <h2 className="text-lg font-bold text-slate-100 flex items-center space-x-2">
              <span>Add Shelf Photo</span>
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Upload a shelf photo for automated bin detection
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 overflow-y-auto space-y-4">
          {/* Drag & Drop Area */}
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setIsDragging(true);
            }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setIsDragging(false);
              if (e.dataTransfer.files?.[0]) {
                selectFileForUpload(e.dataTransfer.files[0]);
              }
            }}
            onClick={() => fileInputRef.current?.click()}
            className={`border-2 border-dashed rounded-xl p-8 flex flex-col items-center justify-center text-center cursor-pointer transition-all duration-200 ${
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
                  selectFileForUpload(e.target.files[0]);
                  e.target.value = '';
                }
              }}
            />

            {isUploading ? (
              <div className="flex flex-col items-center w-full max-w-md py-4 space-y-4">
                {/* Stage Badge & Animated Loader */}
                <div className="flex items-center space-x-2">
                  <Loader2 className="w-5 h-5 text-cyan-400 animate-spin" />
                  <span className="text-xs font-mono font-semibold uppercase tracking-wider text-cyan-300 bg-cyan-950/80 border border-cyan-800/80 px-2.5 py-1 rounded-full">
                    {uploadStage === 'preparing' && 'Step 1/4: Preparing Image'}
                    {uploadStage === 'vision_ai' && 'Step 2/4: Gemini Vision AI'}
                    {uploadStage === 'deduplication' && 'Step 3/4: Spatial Fusion'}
                    {uploadStage === 'saving' && 'Step 4/4: Indexing Database'}
                    {uploadStage === 'complete' && 'Ingestion Complete!'}
                    {uploadStage === 'queued' && 'Queued in Ingestion Worker'}
                  </span>
                </div>

                {/* Progress Bar & Percentage */}
                <div className="w-full space-y-1.5">
                  <div className="flex justify-between items-center text-xs font-mono">
                    <span className="text-slate-300 truncate max-w-[280px]">{uploadMessage}</span>
                    <span className="text-cyan-300 font-bold">{uploadProgress}%</span>
                  </div>
                  <div className="w-full bg-slate-950 rounded-full h-2.5 overflow-hidden border border-slate-700/80">
                    <div
                      className="bg-gradient-to-r from-cyan-500 via-teal-400 to-emerald-400 h-full rounded-full transition-all duration-300 ease-out"
                      style={{ width: `${Math.max(5, uploadProgress)}%` }}
                    />
                  </div>
                </div>

                {/* Encouraging Feedback Card */}
                <div className="w-full bg-gradient-to-br from-cyan-950/40 via-slate-900 to-slate-950 border border-cyan-800/50 rounded-xl p-3.5 flex items-start space-x-3 shadow-inner text-left">
                  <div className="p-2 rounded-lg bg-cyan-900/50 border border-cyan-700/60 text-cyan-300 flex-shrink-0 mt-0.5">
                    <Sparkles className="w-4 h-4 animate-pulse" />
                  </div>
                  <div className="min-w-0">
                    <p className="text-xs font-semibold text-cyan-200">AI Workshop Assistant</p>
                    <p className="text-xs text-slate-300 mt-0.5 leading-relaxed font-sans">
                      {uploadEncouragement || 'Carefully inspecting your workshop storage bins...'}
                    </p>
                    {uploadBinsCount > 0 && (
                      <div className="flex items-center space-x-2 mt-2 pt-2 border-t border-cyan-900/40 text-[11px] font-mono text-cyan-400">
                        <Layers className="w-3.5 h-3.5" />
                        <span>{uploadBinsCount} bins detected so far</span>
                        {uploadTiles.total > 1 && (
                          <span className="text-slate-400">
                            &bull; Tile {uploadTiles.current} of {uploadTiles.total}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <>
                <UploadCloud className="w-10 h-10 text-slate-400 mb-3" />
                <p className="text-sm font-medium text-slate-200">
                  Drag and drop a new shelf photo here, or <span className="text-cyan-400 underline">browse</span>
                </p>
                <p className="text-xs text-slate-400 mt-1">
                  Supports high-resolution JPEG, PNG, or WebP. Bins and labels are auto-detected.
                </p>
              </>
            )}
          </div>

          {errorMessage && (
            <div className="flex items-center space-x-2 p-3 rounded-lg bg-red-950/50 border border-red-800 text-red-300 text-xs">
              <AlertCircle className="w-4 h-4 flex-shrink-0" />
              <span>{errorMessage}</span>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end px-6 py-3 border-t border-slate-800 bg-slate-900/50">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
};
