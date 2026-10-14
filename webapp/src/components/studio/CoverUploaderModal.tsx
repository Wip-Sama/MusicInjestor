import React, { useRef, useState } from 'react';
import { Image, Upload, Check, AlertCircle, ArrowRight, Loader2 } from 'lucide-react';
import { apiFetch } from '../../lib/auth';
import { TrackItem } from '../../types/music';
import { ProposalResponse } from '../../types/studio';

interface CoverUploaderModalProps {
  track: TrackItem;
  onPropose: (proposal: ProposalResponse) => void;
  isProposing: boolean;
  onCancel: () => void;
}

export const CoverUploaderModal: React.FC<CoverUploaderModalProps> = ({
  track,
  onPropose,
  isProposing,
  onCancel,
}) => {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState<boolean>(false);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    if (!file.type.startsWith('image/')) {
      setError('Please select an image file (JPEG, PNG, WEBP).');
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setError('Image file must be under 10MB.');
      return;
    }

    setError(null);
    setSelectedFile(file);
    const objectUrl = URL.createObjectURL(file);
    setPreviewUrl(objectUrl);
  };

  const handleReviewCover = async () => {
    if (!selectedFile) {
      setError('Please choose an image file first.');
      return;
    }

    setUploading(true);
    setError(null);
    try {
      const formData = new FormData();
      formData.append('file', selectedFile);

      // Upload with apply=false to get cover_hex for dry-run diff
      const res = await apiFetch(`/api/v1/app/tracks/${track.id}/cover?apply=false`, {
        method: 'POST',
        body: formData,
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `Upload failed (${res.status})`);
      }

      const coverData = await res.json();
      const coverHex = coverData.cover_hex;
      const coverMime = coverData.mime_type || 'image/jpeg';

      // Now propose edit with cover hex attached
      const propRes = await apiFetch(`/api/v1/app/tracks/${track.id}/propose-edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action_type: 'cover',
          cover_hex: coverHex,
          cover_mime: coverMime,
        }),
      });

      if (!propRes.ok) {
        throw new Error(`Proposal failed (${propRes.status})`);
      }

      const proposal: ProposalResponse = await propRes.json();
      onPropose(proposal);
    } catch (err: unknown) {
      setError((err as Error).message || 'Failed to prepare cover preview.');
    } finally {
      setUploading(false);
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* Before / After Preview Images */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-around',
          backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
          borderRadius: '12px',
          padding: '16px',
        }}
      >
        {/* Current Cover */}
        <div style={{ textAlign: 'center', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '6px' }}>
          <div
            style={{
              width: '100px',
              height: '100px',
              borderRadius: '10px',
              overflow: 'hidden',
              backgroundColor: '#1c1c1e',
              border: '1px solid #3a3a3c',
            }}
          >
            <img
              src={track.cover_url}
              alt="Current Cover"
              style={{ width: '100%', height: '100%', objectFit: 'cover' }}
              onError={(e) => {
                (e.currentTarget as HTMLImageElement).src =
                  'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" fill="%233a3a3c"><rect width="100" height="100"/></svg>';
              }}
            />
          </div>
          <span style={{ fontSize: '11px', color: '#8e8e93', fontWeight: 600 }}>Current Artwork</span>
        </div>

        <ArrowRight size={20} color="var(--tg-theme-button-color, #0a84ff)" />

        {/* New Cover Preview */}
        <div style={{ textAlign: 'center', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '6px' }}>
          <div
            onClick={() => fileInputRef.current?.click()}
            style={{
              width: '100px',
              height: '100px',
              borderRadius: '10px',
              overflow: 'hidden',
              backgroundColor: '#1c1c1e',
              border: previewUrl ? '2px solid #30d158' : '2px dashed #48484a',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
            }}
          >
            {previewUrl ? (
              <img src={previewUrl} alt="New Cover" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px', color: '#8e8e93' }}>
                <Upload size={22} />
                <span style={{ fontSize: '10px', fontWeight: 600 }}>Tap to Select</span>
              </div>
            )}
          </div>
          <span style={{ fontSize: '11px', color: previewUrl ? '#30d158' : '#8e8e93', fontWeight: 600 }}>
            {previewUrl ? 'New Selected' : 'Not Chosen'}
          </span>
        </div>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        style={{ display: 'none' }}
        onChange={handleFileChange}
      />

      {/* Select file button */}
      <button
        onClick={() => fileInputRef.current?.click()}
        style={{
          width: '100%',
          backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
          border: '1px solid #3a3a3c',
          borderRadius: '10px',
          padding: '10px 14px',
          color: 'var(--tg-theme-text-color, #fff)',
          fontSize: '13px',
          fontWeight: 600,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '8px',
          cursor: 'pointer',
        }}
      >
        <Image size={16} color="var(--tg-theme-button-color, #0a84ff)" />
        <span>{selectedFile ? `Selected: ${selectedFile.name}` : 'Choose Image File from Device'}</span>
      </button>

      {error && (
        <div style={{ color: '#ff453a', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
          <AlertCircle size={14} />
          <span>{error}</span>
        </div>
      )}

      {/* Review Actions */}
      <div style={{ display: 'flex', gap: '10px', marginTop: '8px' }}>
        <button
          onClick={onCancel}
          disabled={uploading || isProposing}
          style={{
            flex: 1,
            padding: '12px',
            borderRadius: '10px',
            border: 'none',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            color: 'var(--tg-theme-text-color, #fff)',
            fontSize: '14px',
            fontWeight: 600,
            cursor: 'pointer',
          }}
        >
          Cancel
        </button>
        <button
          onClick={handleReviewCover}
          disabled={!selectedFile || uploading || isProposing}
          style={{
            flex: 2,
            padding: '12px',
            borderRadius: '10px',
            border: 'none',
            backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
            color: '#fff',
            fontSize: '14px',
            fontWeight: 700,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '6px',
            cursor: !selectedFile || uploading || isProposing ? 'not-allowed' : 'pointer',
            opacity: !selectedFile ? 0.6 : 1,
          }}
        >
          {uploading || isProposing ? (
            <>
              <Loader2 size={16} className="animate-spin" />
              <span>Preparing Preview...</span>
            </>
          ) : (
            <>
              <Check size={16} />
              <span>Review Cover Change</span>
            </>
          )}
        </button>
      </div>
    </div>
  );
};

