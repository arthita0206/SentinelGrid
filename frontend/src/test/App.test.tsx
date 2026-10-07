import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

/** Mocks a successful `fetch` for GET /nodes -- NodesPage (Step 40) and
 * DashboardPage (Step 41) both have no mock-data fallback, so a
 * route-rendering smoke test needs a successful response to reach a
 * loaded state. */
function mockNodesFetchOnce(nodes: unknown[] = []) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ nodes, count: nodes.length, limit: 500, order: 'asc' }),
    }),
  );
}

const SAMPLE_NODE = {
  node_id: 'power-001',
  subsystem: 'power',
  status: 'ONLINE',
  state: 'HEALTHY',
  last_telemetry_timestamp: '2026-01-01T00:00:00+00:00',
  last_state_update: '2026-01-01T00:00:01+00:00',
  reason: 'All telemetry fields are present and within the expected operating range.',
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('App routing', () => {
  it('redirects the root path to /dashboard', () => {
    renderAt('/');
    expect(screen.getByRole('heading', { name: 'SentinelGrid' })).toBeInTheDocument();
  });

  it('renders the dashboard route once the (mocked) live fetch resolves', async () => {
    mockNodesFetchOnce([SAMPLE_NODE]);
    renderAt('/dashboard');
    expect(screen.getByText('Smart City Resilience & Operations')).toBeInTheDocument();
    expect(await screen.findByText('Total Nodes')).toBeInTheDocument();
  });

  it('renders the nodes route once the (mocked) live fetch resolves', async () => {
    mockNodesFetchOnce();
    renderAt('/nodes');
    expect(screen.getByRole('heading', { level: 1, name: 'Nodes' })).toBeInTheDocument();
    expect(await screen.findByPlaceholderText('Search node ID…')).toBeInTheDocument();
  });

  it('shows a loading state immediately for the nodes route', () => {
    mockNodesFetchOnce();
    renderAt('/nodes');
    expect(screen.getByText('Loading node fleet…')).toBeInTheDocument();
  });

  it('renders a node detail route once the (mocked) live fetch resolves', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            node_id: 'power-001',
            current_state: { ...SAMPLE_NODE },
            anomalies: { count: 0, items: [] },
            recoveries: { count: 0, items: [] },
          }),
      }),
    );
    renderAt('/nodes/power-001');
    expect(await screen.findByText('Live')).toBeInTheDocument();
    expect(screen.getAllByText(/power-001/i).length).toBeGreaterThan(0);
  });

  it('renders the events route once the (mocked) live fetch resolves', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ events: [], count: 0, limit: 50, order: 'desc' }),
      }),
    );
    renderAt('/events');
    expect(screen.getByRole('heading', { level: 1, name: 'Events' })).toBeInTheDocument();
    expect(await screen.findByText('No anomaly or recovery events found.')).toBeInTheDocument();
  });

  it('renders the analytics route once the (mocked) live fetch resolves', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({ namespace: 'SentinelGrid', start: '2026-01-01T00:00:00+00:00', end: '2026-01-01T01:00:00+00:00', period_seconds: 60, series: [] }),
      }),
    );
    renderAt('/analytics');
    expect(screen.getByRole('heading', { level: 1, name: 'Analytics' })).toBeInTheDocument();
    expect(await screen.findByText('No metric datapoints found')).toBeInTheDocument();
  });

  it('renders the architecture route', () => {
    renderAt('/architecture');
    expect(screen.getByRole('heading', { level: 1, name: 'Architecture' })).toBeInTheDocument();
    expect(screen.getByText('MAPE-K Control Loop')).toBeInTheDocument();
  });

  it('renders the 404 page for an unknown route', () => {
    renderAt('/this-route-does-not-exist');
    expect(screen.getByText('404')).toBeInTheDocument();
  });
});
