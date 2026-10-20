import React, { useEffect, useState, useCallback } from 'react';
import { Search, SlidersHorizontal, Music2, RefreshCw, Sparkles } from 'lucide-react';
import { TrackCard } from '../components/catalog/TrackCard';
import { SongStudioModal } from '../components/studio/SongStudioModal';
import { BulkResearchModal } from '../components/bulk/BulkResearchModal';
import { useAudioPlayer } from '../context/AudioPlayerContext';
import { apiFetch, UserProfile, canSubmitIngestion } from '../lib/auth';
import { TrackItem, TracksResponse } from '../types/music';

interface CatalogViewProps {
  profile?: UserProfile | null;
}

export const CatalogView: React.FC<CatalogViewProps> = ({ profile }) => {
  const { updateTrackData } = useAudioPlayer();
  const canEdit = canSubmitIngestion(profile ?? null);
  const [tracks, setTracks] = useState<TrackItem[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(true);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [debouncedQuery, setDebouncedQuery] = useState<string>('');
  const [sortBy, setSortBy] = useState<string>('recent');
  const [page, setPage] = useState<number>(1);
  const [editingTrack, setEditingTrack] = useState<TrackItem | null>(null);

  // Multi-selection & Bulk check states
  const [isSelectMode, setIsSelectMode] = useState<boolean>(false);
  const [selectedTrackIds, setSelectedTrackIds] = useState<Set<string>>(new Set());
  const [isBulkModalOpen, setIsBulkModalOpen] = useState<boolean>(false);

  const perPage = 30;

  // Debounce search query by 300ms
  useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedQuery(searchQuery);
      setPage(1);
    }, 300);
    return () => clearTimeout(handler);
  }, [searchQuery]);

  const loadTracks = useCallback(() => {
    setLoading(true);
    const params = new URLSearchParams({
      page: page.toString(),
      per_page: perPage.toString(),
      sort_by: sortBy,
    });
    if (debouncedQuery.trim()) {
      params.set('query', debouncedQuery.trim());
    }

    apiFetch(`/api/v1/app/tracks?${params.toString()}`)
      .then((res) => res.json())
      .then((data: TracksResponse) => {
        setTracks(data.items || []);
        setTotal(data.total || 0);
        setLoading(false);
      })
      .catch((err) => {
        console.error('Failed to load library tracks:', err);
        setLoading(false);
      });
  }, [debouncedQuery, sortBy, page]);

  useEffect(() => {
    loadTracks();
  }, [loadTracks]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
      {/* Search Bar & Sort Row */}
      <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
        <div
          style={{
            flex: 1,
            minWidth: 0,
            display: 'flex',
            alignItems: 'center',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            borderRadius: '10px',
            padding: '8px 10px',
            gap: '6px',
          }}
        >
          <Search size={18} color="var(--tg-theme-subtitle-text-color, #8e8e93)" style={{ flexShrink: 0 }} />
          <input
            type="text"
            placeholder="Search title, artist, album..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              flex: 1,
              width: '100%',
              minWidth: 0,
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: 'var(--tg-theme-text-color, #fff)',
              fontSize: '14px',
              textOverflow: 'ellipsis',
            }}
          />
        </div>

        {/* Sort selector: protected flex width preventing viewport overflow */}
        <div
          style={{
            flexShrink: 0,
            minWidth: 'max-content',
            display: 'flex',
            alignItems: 'center',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            borderRadius: '10px',
            padding: '0 10px',
            height: '38px',
          }}
        >
          <SlidersHorizontal size={15} color="var(--tg-theme-subtitle-text-color, #8e8e93)" style={{ marginRight: '6px', flexShrink: 0 }} />
          <select
            value={sortBy}
            onChange={(e) => {
              setSortBy(e.target.value);
              setPage(1);
            }}
            style={{
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: 'var(--tg-theme-text-color, #fff)',
              fontSize: '13px',
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            <option value="recent" style={{ background: '#2c2c2e' }}>Recent</option>
            <option value="artist" style={{ background: '#2c2c2e' }}>Artist A-Z</option>
            <option value="title" style={{ background: '#2c2c2e' }}>Title A-Z</option>
            <option value="album" style={{ background: '#2c2c2e' }}>Album A-Z</option>
            <option value="year" style={{ background: '#2c2c2e' }}>Year</option>
          </select>
        </div>
      </div>

      {/* Multi-Selection Control Bar */}
      {isSelectMode && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            borderRadius: '12px',
            padding: '10px 14px',
            border: '1px solid var(--tg-theme-button-color, #0a84ff)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ fontSize: '13px', fontWeight: 700, color: 'var(--tg-theme-text-color, #fff)' }}>
              {selectedTrackIds.size} Selected
            </span>
            <button
              onClick={() => {
                if (selectedTrackIds.size === tracks.length) {
                  setSelectedTrackIds(new Set());
                } else {
                  setSelectedTrackIds(new Set(tracks.map((t) => t.id)));
                }
              }}
              style={{
                background: 'transparent',
                border: 'none',
                color: 'var(--tg-theme-button-color, #0a84ff)',
                fontSize: '12px',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              {selectedTrackIds.size === tracks.length ? 'Deselect All' : 'Select Page'}
            </button>
          </div>

          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              disabled={selectedTrackIds.size === 0}
              onClick={() => setIsBulkModalOpen(true)}
              style={{
                padding: '6px 12px',
                borderRadius: '8px',
                border: 'none',
                backgroundColor: selectedTrackIds.size === 0 ? 'rgba(255,255,255,0.1)' : 'var(--tg-theme-button-color, #0a84ff)',
                color: selectedTrackIds.size === 0 ? '#666' : '#fff',
                fontSize: '12px',
                fontWeight: 700,
                cursor: selectedTrackIds.size === 0 ? 'not-allowed' : 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
              }}
            >
              <Sparkles size={14} />
              <span>Bulk Check ({selectedTrackIds.size})</span>
            </button>

            <button
              onClick={() => {
                setIsSelectMode(false);
                setSelectedTrackIds(new Set());
              }}
              style={{
                padding: '6px 10px',
                borderRadius: '8px',
                border: '1px solid rgba(255, 255, 255, 0.1)',
                backgroundColor: 'transparent',
                color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
                fontSize: '12px',
                cursor: 'pointer',
              }}
            >
              Exit
            </button>
          </div>
        </div>
      )}

      {/* Track count indicator */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          fontSize: '12px',
          color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
          padding: '0 4px',
        }}
      >
        <span>
          {loading ? 'Searching catalog...' : `${total} track${total === 1 ? '' : 's'} available`}
        </span>
        {total > perPage && (
          <span>
            Page {page} of {Math.ceil(total / perPage)}
          </span>
        )}
      </div>

      {/* Tracks list */}
      {loading && tracks.length === 0 && (
        <div style={{ display: 'flex', justifyContent: 'center', padding: '40px 0' }}>
          <RefreshCw size={24} className="animate-spin" color="var(--tg-theme-button-color, #0a84ff)" />
        </div>
      )}

      {!loading && tracks.length === 0 && (
        <div
          style={{
            textAlign: 'center',
            padding: '60px 20px',
            color: 'var(--tg-theme-subtitle-text-color, #8e8e93)',
          }}
        >
          <Music2 size={42} style={{ marginBottom: '12px', opacity: 0.5 }} />
          <div style={{ fontSize: '16px', fontWeight: 600, color: 'var(--tg-theme-text-color, #fff)' }}>
            No tracks found
          </div>
          <div style={{ fontSize: '13px', marginTop: '4px' }}>
            {searchQuery ? 'Try adjusting your search query.' : 'Send audio tracks or archives to the Telegram bot to ingest them.'}
          </div>
        </div>
      )}

      {tracks.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {tracks.map((track) => (
            <TrackCard
              key={track.id}
              track={track}
              allTracks={tracks}
              onEdit={canEdit ? (t) => setEditingTrack(t) : undefined}
              isSelectMode={isSelectMode}
              isSelected={selectedTrackIds.has(track.id)}
              onToggleSelect={(trackId) => {
                setSelectedTrackIds((prev) => {
                  const next = new Set(prev);
                  if (next.has(trackId)) {
                    next.delete(trackId);
                  } else {
                    next.add(trackId);
                  }
                  return next;
                });
              }}
              onLongPress={canEdit ? (t) => {
                setIsSelectMode(true);
                setSelectedTrackIds(new Set([t.id]));
              } : undefined}
            />
          ))}
        </div>
      )}

      {/* Pagination controls */}
      {total > perPage && (
        <div
          style={{
            display: 'flex',
            justifyContent: 'center',
            gap: '12px',
            padding: '16px 0 80px 0',
          }}
        >
          <button
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            style={{
              padding: '6px 16px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              color: page <= 1 ? '#555' : 'var(--tg-theme-text-color, #fff)',
              cursor: page <= 1 ? 'not-allowed' : 'pointer',
              fontSize: '13px',
              fontWeight: 600,
            }}
          >
            Previous
          </button>
          <button
            disabled={page >= Math.ceil(total / perPage)}
            onClick={() => setPage((p) => p + 1)}
            style={{
              padding: '6px 16px',
              borderRadius: '8px',
              border: 'none',
              backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
              color: page >= Math.ceil(total / perPage) ? '#555' : 'var(--tg-theme-text-color, #fff)',
              cursor: page >= Math.ceil(total / perPage) ? 'not-allowed' : 'pointer',
              fontSize: '13px',
              fontWeight: 600,
            }}
          >
            Next
          </button>
        </div>
      )}

      {/* Song Studio Modal */}
      <SongStudioModal
        isOpen={Boolean(editingTrack)}
        track={editingTrack}
        onClose={() => setEditingTrack(null)}
        onTrackUpdated={(updatedTrack) => {
          setTracks((prev) =>
            prev.map((t) => (t.id === updatedTrack.id ? updatedTrack : t))
          );
          updateTrackData(updatedTrack);
        }}
      />

      {/* Bulk Operations Modal */}
      <BulkResearchModal
        isOpen={isBulkModalOpen}
        onClose={() => setIsBulkModalOpen(false)}
        selectedTrackIds={Array.from(selectedTrackIds)}
        searchQuery={debouncedQuery}
        totalCatalogTracks={total}
        onBulkCompleted={() => {
          setSelectedTrackIds(new Set());
          setIsSelectMode(false);
          loadTracks();
        }}
      />
    </div>
  );
};

