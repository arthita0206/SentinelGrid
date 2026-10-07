import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { EventsPage } from '@/pages/EventsPage';
import type { EventListResponse, FleetEvent } from '@/types/api';

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
      <EventsPage />
    </MemoryRouter>,
  );
}

const ANOMALY_EVENT: FleetEvent = {
  event_type: 'anomaly',
  node_id: 'power-001',
  timestamp: '2026-01-01T00:00:05+00:00',
  subsystem: 'power',
  fault_type: 'NODE_OVERLOAD',
  severity: 'MEDIUM',
  reason: "Field 'load' value 97.8 is within the overload band.",
  detected: true,
  evidence: { field: 'load', value: 97.8 },
  action: null,
  automatic: null,
  status: null,
  success: null,
  recovery_started_at: null,
  recorded_at: null,
};

const RECOVERY_EVENT: FleetEvent = {
  event_type: 'recovery',
  node_id: 'water-002',
  timestamp: '2026-01-01T00:01:00+00:00',
  subsystem: 'water',
  fault_type: 'NODE_OVERLOAD',
  severity: 'MEDIUM',
  reason: 'Simulated execution of THROTTLE_FLOW succeeded.',
  detected: null,
  evidence: null,
  action: 'THROTTLE_FLOW',
  automatic: true,
  status: 'SUCCESS',
  success: true,
  recovery_started_at: '2026-01-01T00:01:00+00:00',
  recorded_at: '2026-01-01T00:01:02+00:00',
};

const ESCALATION_EVENT: FleetEvent = {
  ...RECOVERY_EVENT,
  node_id: 'traffic-003',
  status: 'ESCALATION_REQUIRED',
  success: false,
  action: 'NONE',
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('EventsPage (Step 42 — live data)', () => {
  it('shows a loading state before the fetch resolves', () => {
    mockFetch(200, { events: [], count: 0, limit: 50, order: 'desc' } satisfies EventListResponse);
    renderPage();
    expect(screen.getByText('Loading events…')).toBeInTheDocument();
  });

  it('requests GET /events with the default bounded limit', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ events: [ANOMALY_EVENT], count: 1, limit: 50, order: 'desc' } satisfies EventListResponse),
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('power-001');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const calledUrl = fetchMock.mock.calls[0]![0] as string;
    expect(calledUrl).toContain('/events?');
    expect(calledUrl).toContain('limit=50');
  });

  it('renders live event data and a "Live" indicator on success', async () => {
    mockFetch(200, { events: [ANOMALY_EVENT], count: 1, limit: 50, order: 'desc' } satisfies EventListResponse);
    renderPage();

    expect(await screen.findByText('Live')).toBeInTheDocument();
    expect(screen.getByText('power-001')).toBeInTheDocument();
  });

  it('does not show "Demo data" when the live call succeeds', async () => {
    mockFetch(200, { events: [], count: 0, limit: 50, order: 'desc' } satisfies EventListResponse);
    renderPage();

    await screen.findByText('No anomaly or recovery events found.');
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument();
  });

  it('shows an honest empty state when GET /events returns zero events', async () => {
    mockFetch(200, { events: [], count: 0, limit: 50, order: 'desc' } satisfies EventListResponse);
    renderPage();

    expect(await screen.findByText('No anomaly or recovery events found.')).toBeInTheDocument();
  });

  it('shows an honest error state on failure, with no demo-data fallback', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    renderPage();

    expect(await screen.findByText('Could not load events')).toBeInTheDocument();
    // The error state shows no fabricated data, so it must use the
    // 'pending' variant ("Not Connected"), never the 'demo' variant
    // ("Demo data") which would (falsely) imply a fabricated value is
    // on screen.
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument();
    expect(screen.getByText('Not Connected')).toBeInTheDocument();
  });

  it('renders an anomaly row distinguishably from a recovery row', async () => {
    mockFetch(200, {
      events: [ANOMALY_EVENT, RECOVERY_EVENT],
      count: 2,
      limit: 50,
      order: 'desc',
    } satisfies EventListResponse);
    renderPage();

    await screen.findByText('power-001');
    const table = screen.getByRole('table');
    expect(within(table).getByText('Anomaly')).toBeInTheDocument();
    expect(within(table).getByText('Recovery')).toBeInTheDocument();
    expect(within(table).getByText('Detected')).toBeInTheDocument();
    expect(within(table).getByText('Success')).toBeInTheDocument();
  });

  it('renders an escalation row (recovery event with ESCALATION_REQUIRED status) distinctly', async () => {
    mockFetch(200, { events: [ESCALATION_EVENT], count: 1, limit: 50, order: 'desc' } satisfies EventListResponse);
    renderPage();

    await screen.findByText('traffic-003');
    const table = screen.getByRole('table');
    expect(within(table).getByText('Escalation')).toBeInTheDocument();
    expect(within(table).getByText('Escalation Required')).toBeInTheDocument();
  });

  it('re-issues a request with event_type=anomaly when the Event type filter changes', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ events: [], count: 0, limit: 50, order: 'desc' } satisfies EventListResponse),
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('No anomaly or recovery events found.');

    const select = screen.getByLabelText('Event type') as HTMLSelectElement;
    select.value = 'anomaly';
    select.dispatchEvent(new Event('change', { bubbles: true }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const secondUrl = fetchMock.mock.calls[1]![0] as string;
    expect(secondUrl).toContain('event_type=anomaly');
  });

  it('the Refresh button re-issues the request', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ events: [ANOMALY_EVENT], count: 1, limit: 50, order: 'desc' } satisfies EventListResponse),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ events: [], count: 0, limit: 50, order: 'desc' } satisfies EventListResponse),
      });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('power-001');

    screen.getByRole('button', { name: 'Refresh' }).click();

    await waitFor(() => expect(screen.getByText('No anomaly or recovery events found.')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('communicates the feed is bounded, not the complete historical database', async () => {
    mockFetch(200, { events: [ANOMALY_EVENT], count: 1, limit: 50, order: 'desc' } satisfies EventListResponse);
    renderPage();

    await screen.findByText('power-001');
    expect(screen.getByText(/Showing \d+ event\(s\) — a bounded, recent feed/i)).toBeInTheDocument();
  });

  it('the node ID in each event row links to that node\'s detail page (Step 44)', async () => {
    mockFetch(200, { events: [ANOMALY_EVENT], count: 1, limit: 50, order: 'desc' } satisfies EventListResponse);
    renderPage();

    const link = await screen.findByRole('link', { name: 'power-001' });
    expect(link).toHaveAttribute('href', '/nodes/power-001');
  });
});
