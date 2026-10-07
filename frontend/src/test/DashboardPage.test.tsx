import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DashboardPage } from '@/pages/DashboardPage';
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
      <DashboardPage />
    </MemoryRouter>,
  );
}

const SAMPLE_NODES: NodeListResponse['nodes'] = [
  {
    node_id: 'power-001',
    subsystem: 'power',
    status: 'ONLINE',
    state: 'HEALTHY',
    last_telemetry_timestamp: '2026-01-01T00:00:00+00:00',
    last_state_update: '2026-01-01T00:00:01+00:00',
    reason: 'All telemetry fields are present and within the expected operating range.',
  },
  {
    node_id: 'water-002',
    subsystem: 'water',
    status: 'ONLINE',
    state: 'FAULT_DETECTED',
    fault_type: 'NODE_OVERLOAD',
    fault_severity: 'MEDIUM',
    last_telemetry_timestamp: '2026-01-01T00:00:00+00:00',
    last_state_update: '2026-01-01T00:00:01+00:00',
    reason: "Field 'pressure' value 8.1 is within the overload band.",
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('DashboardPage (Step 41 — live data)', () => {
  it('shows a loading state before the fetch resolves', () => {
    mockFetch(200, { nodes: [], count: 0, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();
    expect(screen.getByText('Loading node fleet…')).toBeInTheDocument();
  });

  it('requests GET /nodes with a bounded limit (not per-subsystem calls)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ nodes: SAMPLE_NODES, count: 2, limit: 500, order: 'asc' } satisfies NodeListResponse),
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('Total Nodes');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const calledUrl = fetchMock.mock.calls[0]![0] as string;
    expect(calledUrl).toContain('/nodes?');
    expect(calledUrl).toContain('limit=500');
  });

  it('renders live totals derived from the API response and a "Live" indicator', async () => {
    mockFetch(200, { nodes: SAMPLE_NODES, count: 2, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();

    expect((await screen.findAllByText('Live')).length).toBeGreaterThan(0);
    expect(screen.getByText('Total Nodes')).toBeInTheDocument();
    // Two sample nodes: one HEALTHY, one FAULT_DETECTED.
    const totalNodesCard = screen.getByText('Total Nodes').closest('div')!.parentElement!;
    expect(totalNodesCard).toHaveTextContent('2');
  });

  it('shows "API Connected" alongside a derived system status once loaded', async () => {
    mockFetch(200, { nodes: SAMPLE_NODES, count: 2, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();

    expect(await screen.findByText('API Connected')).toBeInTheDocument();
    // One node is FAULT_DETECTED -> overall status should be "Fault Detected".
    expect(screen.getByText('Fault Detected')).toBeInTheDocument();
  });

  it('labels the "Recovered Nodes" card honestly (current-state count, not historical events)', async () => {
    mockFetch(200, { nodes: SAMPLE_NODES, count: 2, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();

    await screen.findByText('Total Nodes');
    expect(screen.getByText('Recovered Nodes')).toBeInTheDocument();
    expect(screen.queryByText('Recovered Events')).not.toBeInTheDocument();
  });

  it('shows an honest empty state when GET /nodes returns zero nodes', async () => {
    mockFetch(200, { nodes: [], count: 0, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();

    expect(await screen.findByText('No nodes are currently reporting state')).toBeInTheDocument();
    expect(screen.queryByText('Total Nodes')).not.toBeInTheDocument();
  });

  it('shows an honest error state on failure, with no demo-data fallback', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    renderPage();

    expect(await screen.findByText('Could not load dashboard data')).toBeInTheDocument();
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument();
  });

  it('the Refresh button re-issues the request', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ nodes: SAMPLE_NODES, count: 2, limit: 500, order: 'asc' } satisfies NodeListResponse),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ nodes: [], count: 0, limit: 500, order: 'asc' } satisfies NodeListResponse),
      });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('Total Nodes');

    screen.getByRole('button', { name: 'Refresh' }).click();

    await waitFor(() => expect(screen.getByText('No nodes are currently reporting state')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not fabricate "Recent Events" -- shows an honest placeholder linking to the real Events page', async () => {
    mockFetch(200, { nodes: SAMPLE_NODES, count: 2, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();

    await screen.findByText('Total Nodes');
    expect(screen.getByText('Recent event history is not shown on this dashboard.')).toBeInTheDocument();
    // Step 44: this placeholder must not claim the Events feature is
    // unimplemented -- it has been live since Step 42 -- so it links to it.
    expect(screen.getByRole('link', { name: 'Go to Events' })).toHaveAttribute('href', '/events');
  });

  it('does not fabricate resilience metrics -- shows "See Analytics" and links to the real Analytics page', async () => {
    mockFetch(200, { nodes: SAMPLE_NODES, count: 2, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();

    await screen.findByText('Total Nodes');
    expect(screen.getAllByText('See Analytics').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Not Connected').length).toBeGreaterThan(0);
    // Step 44: this section must not claim CloudWatch metrics are
    // unimplemented -- Step 43 implemented them (see the Analytics page).
    expect(screen.getByRole('link', { name: 'Analytics page' })).toHaveAttribute('href', '/analytics');
  });

  it('the Subsystem cards link to the Nodes page pre-filtered to that subsystem', async () => {
    mockFetch(200, { nodes: SAMPLE_NODES, count: 2, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();

    await screen.findByText('Total Nodes');
    expect(screen.getByRole('link', { name: 'View Power nodes' })).toHaveAttribute('href', '/nodes?subsystem=power');
  });

  it('the City Visualization links each live node to its Node Detail page (Step 44)', async () => {
    mockFetch(200, { nodes: SAMPLE_NODES, count: 2, limit: 500, order: 'asc' } satisfies NodeListResponse);
    renderPage();

    await screen.findByText('Total Nodes');
    const link = screen.getByRole('link', { name: /power-001/i });
    expect(link).toHaveAttribute('href', '/nodes/power-001');
  });
});
