import { ParsedLrcLine } from '../types/music';

/**
 * Parse an LRC string into sorted timestamped lines.
 */
export function parseLrc(lrcContent: string): ParsedLrcLine[] {
  if (!lrcContent || !lrcContent.trim()) return [];

  const lines = lrcContent.split('\n');
  const result: ParsedLrcLine[] = [];
  const timeRegex = /\[(\d{2}):(\d{2}(?:\.\d{1,3})?)\]/g;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;

    const timestamps: number[] = [];
    let match: RegExpExecArray | null;

    timeRegex.lastIndex = 0;
    while ((match = timeRegex.exec(line)) !== null) {
      const minutes = parseInt(match[1], 10);
      const seconds = parseFloat(match[2]);
      timestamps.push(minutes * 60 + seconds);
    }

    const cleanText = line.replace(timeRegex, '').trim();
    for (const t of timestamps) {
      result.push({ time: t, text: cleanText });
    }
  }

  return result.sort((a, b) => a.time - b.time);
}

/**
 * Find the index of the currently playing lyric line based on audio currentTime.
 */
export function findActiveLrcIndex(lines: ParsedLrcLine[], currentTime: number): number {
  if (lines.length === 0) return -1;
  if (currentTime < lines[0].time) return -1;

  for (let i = lines.length - 1; i >= 0; i--) {
    if (currentTime >= lines[i].time) {
      return i;
    }
  }

  return 0;
}

/**
 * Format duration in seconds to standard mm:ss display.
 */
export function formatTime(seconds: number): string {
  if (!seconds || isNaN(seconds) || seconds < 0) return '0:00';
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs < 10 ? '0' : ''}${secs}`;
}

