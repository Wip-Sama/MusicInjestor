import React from 'react';
import { X, Sliders, Music, CheckCircle2, Shield } from 'lucide-react';
import { useAudioPlayer } from '../../context/AudioPlayerContext';
import { UserProfile } from '../../lib/auth';

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  profile?: UserProfile | null;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({ isOpen, onClose, profile }) => {
  const {
    streamFormat,
    streamBitrate,
    autoLyricsSearch,
    updateUserSettings,
  } = useAudioPlayer();

  if (!isOpen) return null;

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.75)',
        zIndex: 1000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '16px',
        backdropFilter: 'blur(6px)',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: '100%',
          maxWidth: '440px',
          maxHeight: '90vh',
          backgroundColor: 'var(--tg-theme-bg-color, #1c1c1e)',
          borderRadius: '20px',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          boxShadow: '0 20px 50px rgba(0, 0, 0, 0.6)',
          border: '1px solid rgba(255, 255, 255, 0.1)',
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: '16px 20px',
            borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Sliders size={20} color="var(--tg-theme-button-color, #0a84ff)" />
            <span style={{ fontSize: '17px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)' }}>
              Settings &amp; Preferences
            </span>
          </div>
          <button
            onClick={onClose}
            style={{
              background: 'rgba(255, 255, 255, 0.1)',
              border: 'none',
              borderRadius: '50%',
              width: '32px',
              height: '32px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--tg-theme-text-color, #fff)',
              cursor: 'pointer',
            }}
          >
            <X size={18} />
          </button>
        </div>

        {/* Scrollable Content */}
        <div
          style={{
            padding: '20px',
            overflowY: 'auto',
            display: 'flex',
            flexDirection: 'column',
            gap: '20px',
          }}
        >
          {/* Section 1: Streaming Transcode Format */}
          <div>
            <div
              style={{
                fontSize: '13px',
                fontWeight: 700,
                textTransform: 'uppercase',
                color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                letterSpacing: '0.5px',
                marginBottom: '8px',
              }}
            >
              Streaming Format
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
              {[
                { id: 'auto', label: 'Auto (Smart)', desc: 'Opus 192k / Direct' },
                { id: 'original', label: 'Direct / Raw', desc: 'No transcoding (0% CPU)' },
                { id: 'opus', label: 'Opus', desc: 'High fidelity, compact' },
                { id: 'mp3', label: 'MP3', desc: 'Universal compatibility' },
              ].map((opt) => {
                const isSelected = streamFormat === opt.id;
                return (
                  <button
                    key={opt.id}
                    onClick={() => updateUserSettings({ stream_format: opt.id })}
                    style={{
                      padding: '12px',
                      borderRadius: '12px',
                      border: isSelected
                        ? '1.5px solid var(--tg-theme-button-color, #0a84ff)'
                        : '1px solid rgba(255, 255, 255, 0.08)',
                      backgroundColor: isSelected
                        ? 'rgba(10, 132, 255, 0.15)'
                        : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                      color: isSelected
                        ? 'var(--tg-theme-button-color, #0a84ff)'
                        : 'var(--tg-theme-text-color, #fff)',
                      cursor: 'pointer',
                      textAlign: 'left',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '4px',
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: '14px', fontWeight: 700 }}>{opt.label}</span>
                      {isSelected && <CheckCircle2 size={16} color="var(--tg-theme-button-color, #0a84ff)" />}
                    </div>
                    <span style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                      {opt.desc}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Section 2: Transcoding Bitrate */}
          <div>
            <div
              style={{
                fontSize: '13px',
                fontWeight: 700,
                textTransform: 'uppercase',
                color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                letterSpacing: '0.5px',
                marginBottom: '8px',
                opacity: streamFormat === 'original' ? 0.4 : 1,
              }}
            >
              Transcoding Bitrate
            </div>
            <div style={{ display: 'flex', gap: '8px' }}>
              {[128, 192, 320].map((rate) => {
                const isSelected = streamBitrate === rate;
                const isDisabled = streamFormat === 'original';
                return (
                  <button
                    key={rate}
                    disabled={isDisabled}
                    onClick={() => updateUserSettings({ stream_bitrate: rate })}
                    style={{
                      flex: 1,
                      padding: '10px 8px',
                      borderRadius: '10px',
                      border: isSelected && !isDisabled
                        ? '1.5px solid var(--tg-theme-button-color, #0a84ff)'
                        : '1px solid rgba(255, 255, 255, 0.08)',
                      backgroundColor: isSelected && !isDisabled
                        ? 'rgba(10, 132, 255, 0.15)'
                        : 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                      color: isSelected && !isDisabled
                        ? 'var(--tg-theme-button-color, #0a84ff)'
                        : 'var(--tg-theme-text-color, #fff)',
                      cursor: isDisabled ? 'not-allowed' : 'pointer',
                      fontSize: '13px',
                      fontWeight: 700,
                      opacity: isDisabled ? 0.35 : 1,
                    }}
                  >
                    {rate} kbps
                  </button>
                );
              })}
            </div>
            {streamFormat === 'original' && (
              <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)', marginTop: '6px' }}>
                Bitrate selection is disabled when Direct / Original streaming is active.
              </div>
            )}
          </div>

          {/* Section 3: Ingestion & Lyrics */}
          <div>
            <div
              style={{
                fontSize: '13px',
                fontWeight: 700,
                textTransform: 'uppercase',
                color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                letterSpacing: '0.5px',
                marginBottom: '8px',
              }}
            >
              Library &amp; Metadata
            </div>
            <div
              onClick={() => updateUserSettings({ auto_lyrics_search: !autoLyricsSearch })}
              style={{
                padding: '12px 14px',
                borderRadius: '12px',
                backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                border: '1px solid rgba(255, 255, 255, 0.08)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                cursor: 'pointer',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <Music size={18} color="var(--tg-theme-button-color, #0a84ff)" />
                <div>
                  <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--tg-theme-text-color, #fff)' }}>
                    Auto-Search Online Lyrics
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                    Automatically query LRCLIB for synced lyrics when inspecting or editing tracks.
                  </div>
                </div>
              </div>
              <input
                type="checkbox"
                checked={autoLyricsSearch}
                readOnly
                style={{
                  width: '18px',
                  height: '18px',
                  accentColor: 'var(--tg-theme-button-color, #0a84ff)',
                  cursor: 'pointer',
                }}
              />
            </div>
          </div>

          {/* Section 4: Account Information */}
          {profile && (
            <div>
              <div
                style={{
                  fontSize: '13px',
                  fontWeight: 700,
                  textTransform: 'uppercase',
                  color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                  letterSpacing: '0.5px',
                  marginBottom: '8px',
                }}
              >
                Account Session
              </div>
              <div
                style={{
                  padding: '12px 14px',
                  borderRadius: '12px',
                  backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
                  border: '1px solid rgba(255, 255, 255, 0.08)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <Shield size={18} color="#30d158" />
                  <div>
                    <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--tg-theme-text-color, #fff)' }}>
                      {profile.display_name}
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--tg-theme-subtitle-text-color, #8e8e93)' }}>
                      ID: {profile.telegram_id} • Role: {profile.role}
                    </div>
                  </div>
                </div>
                {profile.authentik_linked && (
                  <span
                    style={{
                      fontSize: '11px',
                      padding: '4px 8px',
                      borderRadius: '6px',
                      backgroundColor: 'rgba(48, 209, 88, 0.15)',
                      color: '#30d158',
                      fontWeight: 700,
                    }}
                  >
                    SSO Linked
                  </span>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div
          style={{
            padding: '12px 20px',
            borderTop: '1px solid rgba(255, 255, 255, 0.08)',
            display: 'flex',
            justifyContent: 'flex-end',
          }}
        >
          <button
            onClick={onClose}
            style={{
              padding: '8px 20px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: 'var(--tg-theme-button-color, #0a84ff)',
              color: 'var(--tg-theme-button-text-color, #fff)',
              fontSize: '14px',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
};

