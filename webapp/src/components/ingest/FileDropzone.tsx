import React, { useRef, useState } from 'react';
import { UploadCloud, FileAudio, Archive, AlertCircle, CheckCircle2, Loader2 } from 'lucide-react';
import { uploadMediaFile } from '../../lib/queueApi';
import { QueueJob } from '../../types/queue';

interface FileDropzoneProps {
  onJobUploaded: (job: QueueJob) => void;
  disabled?: boolean;
}

const ACCEPTED_EXTENSIONS = ['.mp3', '.flac', '.m4a', '.ogg', '.opus', '.wav', '.zip', '.7z'];

export const FileDropzone: React.FC<FileDropzoneProps> = ({ onJobUploaded, disabled = false }) => {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const [uploading, setUploading] = useState<boolean>(false);
  const [progress, setProgress] = useState<number>(0);
  const [fileName, setFileName] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    if (disabled || uploading) return;
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    if (disabled || uploading) return;

    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleFileSelected(e.dataTransfer.files[0]);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      handleFileSelected(e.target.files[0]);
    }
  };

  const handleFileSelected = async (file: File) => {
    setErrorMsg(null);
    setSuccessMsg(null);

    const ext = '.' + file.name.split('.').pop()?.toLowerCase();
    if (!ACCEPTED_EXTENSIONS.includes(ext)) {
      setErrorMsg(`Unsupported file type: ${ext}. Supported: MP3, FLAC, M4A, OGG, OPUS, WAV, ZIP, 7Z.`);
      return;
    }

    setFileName(file.name);
    setUploading(true);
    setProgress(0);

    try {
      const result = await uploadMediaFile(file, (pct) => {
        setProgress(pct);
      });
      setSuccessMsg(`Enqueued "${file.name}" successfully!`);
      onJobUploaded(result.job);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
      setTimeout(() => {
        setSuccessMsg(null);
        setFileName(null);
        setProgress(0);
      }, 3000);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Upload failed.';
      setErrorMsg(msg);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      <input
        ref={fileInputRef}
        type="file"
        accept=".mp3,.flac,.m4a,.ogg,.opus,.wav,.zip,.7z"
        onChange={handleFileChange}
        style={{ display: 'none' }}
        disabled={disabled || uploading}
      />

      {/* Drop Area Box */}
      <div
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onClick={() => !uploading && !disabled && fileInputRef.current?.click()}
        style={{
          border: `2px dashed ${
            isDragging
              ? 'var(--tg-theme-button-color, #0a84ff)'
              : 'var(--tg-theme-secondary-bg-color, #3a3a3c)'
          }`,
          borderRadius: '12px',
          padding: '24px 16px',
          textAlign: 'center',
          backgroundColor: isDragging
            ? 'rgba(10, 132, 255, 0.08)'
            : 'var(--tg-theme-bg-color, #1c1c1e)',
          cursor: disabled || uploading ? 'not-allowed' : 'pointer',
          transition: 'all 0.2s ease',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: '8px',
        }}
      >
        <div
          style={{
            width: '48px',
            height: '48px',
            borderRadius: '50%',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--tg-theme-button-color, #0a84ff)',
          }}
        >
          {uploading ? (
            <Loader2 size={24} className="spin" />
          ) : (
            <UploadCloud size={24} />
          )}
        </div>

        <div>
          <div style={{ fontSize: '14px', fontWeight: 700, color: 'var(--tg-theme-text-color, #ffffff)' }}>
            {uploading ? 'Uploading Media...' : 'Drop Audio or Archive Here'}
          </div>
          <div style={{ fontSize: '12px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', marginTop: '2px' }}>
            {uploading
              ? `${fileName || 'File'} (${progress}%)`
              : 'Tap to browse or drop .flac, .mp3, .m4a, .zip, .7z'}
          </div>
        </div>

        {/* Upload Progress Bar */}
        {uploading && (
          <div
            style={{
              width: '100%',
              maxWidth: '280px',
              height: '6px',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #3a3a3c)',
              borderRadius: '3px',
              overflow: 'hidden',
              marginTop: '4px',
            }}
          >
            <div
              style={{
                width: `${progress}%`,
                height: '100%',
                backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
                transition: 'width 0.2s ease',
              }}
            />
          </div>
        )}

        {/* Supported Formats Tags */}
        {!uploading && (
          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', justifyContent: 'center', marginTop: '4px' }}>
            <span
              style={{
                fontSize: '10px',
                fontWeight: 600,
                padding: '2px 6px',
                borderRadius: '4px',
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                color: 'var(--tg-theme-button-color, #0a84ff)',
                display: 'flex',
                alignItems: 'center',
                gap: '3px',
              }}
            >
              <FileAudio size={11} /> FLAC / MP3 / M4A / OPUS
            </span>
            <span
              style={{
                fontSize: '10px',
                fontWeight: 600,
                padding: '2px 6px',
                borderRadius: '4px',
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                color: '#ff9f0a',
                display: 'flex',
                alignItems: 'center',
                gap: '3px',
              }}
            >
              <Archive size={11} /> ZIP / 7Z Multi-Track
            </span>
          </div>
        )}
      </div>

      {/* Success Notification */}
      {successMsg && (
        <div
          style={{
            backgroundColor: 'rgba(48, 209, 88, 0.1)',
            border: '1px solid rgba(48, 209, 88, 0.3)',
            borderRadius: '8px',
            padding: '8px 12px',
            fontSize: '12px',
            color: '#30d158',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          <CheckCircle2 size={16} />
          <span>{successMsg}</span>
        </div>
      )}

      {/* Error Notification */}
      {errorMsg && (
        <div
          style={{
            backgroundColor: 'rgba(255, 69, 58, 0.1)',
            border: '1px solid rgba(255, 69, 58, 0.3)',
            borderRadius: '8px',
            padding: '8px 12px',
            fontSize: '12px',
            color: '#ff453a',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          <AlertCircle size={16} />
          <span>{errorMsg}</span>
        </div>
      )}
    </div>
  );
};

