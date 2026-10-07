import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NodesPage } from '@/pages/NodesPage';
import type { NodeListResponse } from '@/types/api';

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(body),
    }),
  );
}

function renderPage() {
  return render(
    <MemoryRouter>
      <NodesPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('NodesPage (Step 40 — live data)', () => {
  it('shows a loading state before the fetch resolves', () => {
    mockFetch(200, { nodes: [], count: 0, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();
    expect(screen.getByText('Loading node fleet…')).toBeInTheDocument();
  });

  it('renders live node data and a "Live" indicator on success', async () => {
    const body: NodeListResponse = {
      nodes: [
        {
          node_id: 'power-001',
          subsystem: 'power',
          status: 'ONLINE',
          state: 'HEALTHY',
          last_telemetry_timestamp: '2026-01-01T00:00:00+00:00',
          last_state_update: '2026-01-01T00:00:01+00:00',
          reason: 'All telemetry fields are present and within the expected operating range.',
        },
      ],
      count: 1,
      limit: 500,
      order: 'asc',
    };
    mockFetch(200, body);

    renderPage();

    expect(await screen.findByText('Live')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'power-001' })).toBeInTheDocument();
  });

  it('does NOT show a "Demo data" indicator when the live call succeeds', async () => {
    mockFetch(200, { nodes: [], count: 0, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();

    await screen.findByText('Live');
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument();
  });

  it('shows an honest error state when the API call fails, with no demo-data fallback', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    renderPage();

    expect(await screen.findByText('Could not load the node fleet')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('shows an error state for a non-2xx API response', async () => {
    mockFetch(500, { error: 'INTERNAL_ERROR', message: 'An internal error occurred while processing the request.' });

    renderPage();

    expect(await screen.findByText('Could not load the node fleet')).toBeInTheDocument();
  });

  it('retry button re-issues the request', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 500, json: () => Promise.resolve({ error: 'INTERNAL_ERROR', message: 'x' }) })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ nodes: [], count: 0, limit: 500, order: 'asc' } satisfies NodeListResponse),
      });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();

    const retryButton = await screen.findByRole('button', { name: 'Retry' });
    retryButton.click();

    await waitFor(() => expect(screen.getByText('Live')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
