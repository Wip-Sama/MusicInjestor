import React, { useEffect, useState } from 'react';
import { Search, SlidersHorizontal, Music2, RefreshCw } from 'lucide-react';
import { TrackCard } from '../components/catalog/TrackCard';
import { SongStudioModal } from '../components/studio/SongStudioModal';
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
  const perPage = 30;

  // Debounce search query by 300ms
  useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedQuery(searchQuery);
      setPage(1);
    }, 300);
    return () => clearTimeout(handler);
  }, [searchQuery]);

  // Fetch catalog tracks
  useEffect(() => {
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

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
      {/* Search Bar & Sort Row */}
      <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
        <div
          style={{
            flex: 1,
            display: 'flex',
            alignItems: 'center',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            borderRadius: '10px',
            padding: '8px 12px',
            gap: '8px',
          }}
        >
          <Search size={18} color="var(--tg-theme-subtitle-text-color, #8e8e93)" />
          <input
            type="text"
            placeholder="Search by title, artist, or album..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              flex: 1,
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: 'var(--tg-theme-text-color, #fff)',
              fontSize: '14px',
            }}
          />
        </div>

        {/* Sort selector */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            backgroundColor: 'var(--tg-theme-secondary-bg-color, #2c2c2e)',
            borderRadius: '10px',
            padding: '0 8px',
            height: '38px',
          }}
        >
          <SlidersHorizontal size={16} color="var(--tg-theme-subtitle-text-color, #8e8e93)" style={{ marginRight: '4px' }} />
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
    </div>
  );
};

