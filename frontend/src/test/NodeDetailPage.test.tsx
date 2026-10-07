import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NodeDetailPage } from '@/pages/NodeDetailPage';

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

function renderAt(nodeId: string) {
  return render(
    <MemoryRouter initialEntries={[`/nodes/${nodeId}`]}>
      <Routes>
        <Route path="/nodes/:nodeId" element={<NodeDetailPage />} />
        <Route path="/nodes" element={<div>Nodes page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const SUCCESS_BODY = {
  node_id: 'power-001',
  current_state: {
    node_id: 'power-001',
    subsystem: 'power',
    status: 'ONLINE',
    state: 'FAULT_DETECTED',
    last_telemetry_timestamp: '2026-01-01T00:00:00+00:00',
    last_state_update: '2026-01-01T00:00:01+00:00',
    reason: 'Field load is within the overload band.',
    fault_type: 'NODE_OVERLOAD',
    fault_severity: 'MEDIUM',
  },
  anomalies: { count: 0, items: [] },
  recoveries: { count: 0, items: [] },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('NodeDetailPage (Step 44 — honest, no demo-data fallback)', () => {
  it('shows a loading state before the fetch resolves', () => {
    mockFetch(200, SUCCESS_BODY);
    renderAt('power-001');
    expect(screen.getByText('Loading node summary…')).toBeInTheDocument();
  });

  it('renders live node data and a "Live" indicator on success', async () => {
    mockFetch(200, SUCCESS_BODY);
    renderAt('power-001');

    expect(await screen.findByText('Live')).toBeInTheDocument();
    expect(screen.getAllByText('power-001').length).toBeGreaterThan(0);
    expect(screen.getByText('Node Overload')).toBeInTheDocument();
  });

  it('shows a distinct "Node not found" state for a 404, with no fabricated node data', async () => {
    mockFetch(404, { error: 'NODE_NOT_FOUND', message: 'No current state exists for the requested node.' });
    renderAt('does-not-exist');

    expect(await screen.findByText('Node not found')).toBeInTheDocument();
    // No fabricated identity card, no "Demo data" tag -- this must look
    // nothing like a successful load.
    expect(screen.queryByText('Node Identity & Current State')).not.toBeInTheDocument();
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument();
    expect(screen.queryByText('Live')).not.toBeInTheDocument();
    expect(screen.getByText('Not Connected')).toBeInTheDocument();
  });

  it('shows a distinct ErrorState (not "Node not found") for a genuine API/network failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    renderAt('power-001');

    expect(await screen.findByText('Could not load this node')).toBeInTheDocument();
    expect(screen.queryByText('Node not found')).not.toBeInTheDocument();
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument();
  });

  it('the ErrorState Retry button re-issues the request', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(SUCCESS_BODY) });
    vi.stubGlobal('fetch', fetchMock);

    renderAt('power-001');
    await screen.findByText('Could not load this node');

    screen.getByRole('button', { name: 'Retry' }).click();

    await waitFor(() => expect(screen.getByText('Live')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('has a working "Back to Nodes" link', async () => {
    mockFetch(200, SUCCESS_BODY);
    renderAt('power-001');

    await screen.findByText('Live');
    expect(screen.getByRole('link', { name: /Back to Nodes/i })).toHaveAttribute('href', '/nodes');
  });

  it('renders an honest empty state for anomaly/recovery history when both are empty', async () => {
    mockFetch(200, SUCCESS_BODY);
    renderAt('power-001');

    await screen.findByText('Live');
    expect(screen.getByText('No anomalies recorded')).toBeInTheDocument();
    expect(screen.getByText('No recovery events recorded')).toBeInTheDocument();
  });
});
