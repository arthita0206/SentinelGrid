import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnalyticsPage } from '@/pages/AnalyticsPage';
import type { MetricsResponse } from '@/types/api';

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
      <AnalyticsPage />
    </MemoryRouter>,
  );
}

const EMPTY_RESPONSE: MetricsResponse = {
  namespace: 'SentinelGrid',
  start: '2026-01-01T00:00:00+00:00',
  end: '2026-01-01T01:00:00+00:00',
  period_seconds: 60,
  series: [],
};

const FULL_RESPONSE: MetricsResponse = {
  namespace: 'SentinelGrid',
  start: '2026-01-01T00:00:00+00:00',
  end: '2026-01-01T01:00:00+00:00',
  period_seconds: 60,
  subsystem: 'power',
  fault_type: 'NODE_OVERLOAD',
  series: [
    {
      metric_name: 'FaultDetectionCount',
      unit: 'Count',
      dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
      datapoints: [{ timestamp: '2026-01-01T00:30:00+00:00', value: 3 }],
      summary: { datapoint_count: 1, sum: 3 },
    },
    {
      metric_name: 'DetectionLatency',
      unit: 'Milliseconds',
      dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
      datapoints: [],
    },
    {
      metric_name: 'RecoveryAttemptCount',
      unit: 'Count',
      dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
      datapoints: [],
    },
    {
      metric_name: 'RecoverySuccessCount',
      unit: 'Count',
      dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
      datapoints: [{ timestamp: '2026-01-01T00:30:00+00:00', value: 2 }],
      summary: { datapoint_count: 1, sum: 2 },
    },
    {
      metric_name: 'RecoveryFailureCount',
      unit: 'Count',
      dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
      datapoints: [{ timestamp: '2026-01-01T00:30:00+00:00', value: 1 }],
      summary: { datapoint_count: 1, sum: 1 },
    },
    {
      metric_name: 'RecoveryLatency',
      unit: 'Milliseconds',
      dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
      datapoints: [{ timestamp: '2026-01-01T00:30:00+00:00', value: 1500 }],
      summary: { datapoint_count: 1, average: 1500 },
    },
    {
      metric_name: 'UnresolvedFaultCount',
      unit: 'Count',
      dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
      datapoints: [],
    },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AnalyticsPage (Step 43 — live data)', () => {
  it('shows a loading state before the fetch resolves', () => {
    mockFetch(200, EMPTY_RESPONSE);
    renderPage();
    expect(screen.getByText('Loading metrics…')).toBeInTheDocument();
  });

  it('requests GET /metrics with a bounded default (last 1 hour) window', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(EMPTY_RESPONSE),
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('No metric datapoints found');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const calledUrl = fetchMock.mock.calls[0]![0] as string;
    expect(calledUrl).toContain('/metrics?');
    expect(calledUrl).toContain('start=');
    expect(calledUrl).toContain('end=');
  });

  it('renders live metric cards and a "Live" indicator on success', async () => {
    mockFetch(200, FULL_RESPONSE);
    renderPage();

    expect(await screen.findByText('Live')).toBeInTheDocument();
    expect(screen.getByText('FaultDetectionCount')).toBeInTheDocument();
  });

  it('shows an honest empty state when GET /metrics returns zero series', async () => {
    mockFetch(200, EMPTY_RESPONSE);
    renderPage();

    expect(await screen.findByText('No metric datapoints found')).toBeInTheDocument();
  });

  it('shows an honest error state on failure, using the "Not Connected" tag rather than "Demo data"', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    renderPage();

    expect(await screen.findByText('Could not load metrics')).toBeInTheDocument();
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument();
    expect(screen.getByText('Not Connected')).toBeInTheDocument();
  });

  it('shows a per-metric "no datapoints" note without fabricating a zero value', async () => {
    mockFetch(200, FULL_RESPONSE);
    renderPage();

    await screen.findByText('FaultDetectionCount');
    expect(screen.getAllByText('No datapoints published in the selected window.').length).toBeGreaterThan(0);
  });

  it('visually distinguishes RecoverySuccessCount from RecoveryFailureCount', async () => {
    mockFetch(200, FULL_RESPONSE);
    renderPage();

    await screen.findByText('RecoverySuccessCount');
    const successCard = screen.getByTestId('metric-card-RecoverySuccessCount');
    const failureCard = screen.getByTestId('metric-card-RecoveryFailureCount');
    const successBar = within(successCard).getByRole('img');
    const failureBar = within(failureCard).getByRole('img');
    expect(successBar.innerHTML).not.toEqual(failureBar.innerHTML);
    expect(successBar.innerHTML).toContain('bg-status-healthy');
    expect(failureBar.innerHTML).toContain('bg-status-critical');
  });

  it('labels a latency aggregate "Average," never "MTTR" or a percentile', async () => {
    mockFetch(200, FULL_RESPONSE);
    renderPage();

    await screen.findByText('RecoveryLatency');
    const card = screen.getByTestId('metric-card-RecoveryLatency');
    const summaryLabel = within(card).getByText('Average: 1500 ms');
    expect(summaryLabel).toBeInTheDocument();
    // The summary LABEL itself must say "Average," never "MTTR" or a
    // percentile -- the card's own description text is allowed to
    // mention "MTTR" while explicitly disclaiming it (real, honest
    // content), so this checks the summary element specifically, not
    // the whole card.
    expect(summaryLabel.textContent).not.toMatch(/mttr|p95|percentile/i);
  });

  it('communicates UnresolvedFaultCount is an observation count, not an active-fault gauge', async () => {
    mockFetch(200, FULL_RESPONSE);
    renderPage();

    await screen.findByText('UnresolvedFaultCount');
    const card = screen.getByTestId('metric-card-UnresolvedFaultCount');
    expect(within(card).getByText(/not a continuously-maintained active-fault gauge/i)).toBeInTheDocument();
  });

  it('re-issues a request with subsystem=power when the Subsystem filter changes', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(EMPTY_RESPONSE),
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('No metric datapoints found');

    const select = screen.getByLabelText('Subsystem') as HTMLSelectElement;
    select.value = 'power';
    select.dispatchEvent(new Event('change', { bubbles: true }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const secondUrl = fetchMock.mock.calls[1]![0] as string;
    expect(secondUrl).toContain('subsystem=power');
  });

  it('re-issues a request with fault_type=NODE_OVERLOAD when the Fault type filter changes', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(EMPTY_RESPONSE),
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('No metric datapoints found');

    const select = screen.getByLabelText('Fault type') as HTMLSelectElement;
    select.value = 'NODE_OVERLOAD';
    select.dispatchEvent(new Event('change', { bubbles: true }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const secondUrl = fetchMock.mock.calls[1]![0] as string;
    expect(secondUrl).toContain('fault_type=NODE_OVERLOAD');
  });

  it('the time-range selector re-issues a bounded request, never exceeding 24 hours', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(EMPTY_RESPONSE),
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('No metric datapoints found');

    const select = screen.getByLabelText('Time range') as HTMLSelectElement;
    select.value = '24h';
    select.dispatchEvent(new Event('change', { bubbles: true }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const secondUrl = new URL(fetchMock.mock.calls[1]![0] as string);
    const start = new Date(secondUrl.searchParams.get('start')!);
    const end = new Date(secondUrl.searchParams.get('end')!);
    expect(end.getTime() - start.getTime()).toBe(24 * 60 * 60 * 1000);
  });

  it('the Refresh button re-issues the request', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(FULL_RESPONSE) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve(EMPTY_RESPONSE) });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    await screen.findByText('FaultDetectionCount');

    screen.getByRole('button', { name: 'Refresh' }).click();

    await waitFor(() => expect(screen.getByText('No metric datapoints found')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('discloses, rather than fabricates, the absence of detection-accuracy/false-positive-rate figures', async () => {
    mockFetch(200, FULL_RESPONSE);
    renderPage();

    await screen.findByText('FaultDetectionCount');
    // The page explicitly states neither figure is computed (this project
    // has no labeled ground-truth dataset) -- it must never show either as
    // if it were a real, measured number.
    expect(screen.getByText(/No detection-accuracy or false-positive-rate figure is shown/i)).toBeInTheDocument();
  });
});
