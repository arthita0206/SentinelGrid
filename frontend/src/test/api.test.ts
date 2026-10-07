import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, getEvents, getMetrics, getNode, getNodeAnomalies, getNodeRecoveries, getNodes, getNodeSummary } from '@/services/api';
import type { EventListResponse, MetricsResponse, NodeCurrentStateResponse, NodeListResponse } from '@/types/api';

function mockFetchOnce(status: number, body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('services/api', () => {
  it('getNode() calls the correct path and returns the typed response', async () => {
    const body: NodeCurrentStateResponse = {
      node: {
        node_id: 'power-001',
        subsystem: 'power',
        status: 'ONLINE',
        state: 'HEALTHY',
        last_telemetry_timestamp: '2026-01-01T00:00:00+00:00',
        last_state_update: '2026-01-01T00:00:01+00:00',
        reason: 'All telemetry fields are present and within the expected operating range.',
      },
    };
    const fetchMock = mockFetchOnce(200, body);

    const result = await getNode('power-001');

    expect(result).toEqual(body);
    const calledUrl = fetchMock.mock.calls[0]![0] as string;
    expect(calledUrl.endsWith('/nodes/power-001')).toBe(true);
  });

  it('getNodeAnomalies() includes limit/order query parameters', async () => {
    const fetchMock = mockFetchOnce(200, { node_id: 'power-001', count: 0, items: [] });

    await getNodeAnomalies('power-001', { limit: 5, order: 'asc' });

    const calledUrl = fetchMock.mock.calls[0]![0] as string;
    expect(calledUrl).toContain('/nodes/power-001/anomalies?');
    expect(calledUrl).toContain('limit=5');
    expect(calledUrl).toContain('order=asc');
  });

  it('getNodeRecoveries() resolves with the typed response', async () => {
    mockFetchOnce(200, { node_id: 'power-001', count: 0, items: [] });
    const result = await getNodeRecoveries('power-001');
    expect(result.node_id).toBe('power-001');
    expect(result.items).toEqual([]);
  });

  it('getNodeSummary() resolves with the typed response', async () => {
    mockFetchOnce(200, {
      node_id: 'power-001',
      current_state: {
        node_id: 'power-001',
        subsystem: 'power',
        status: 'ONLINE',
        state: 'HEALTHY',
        last_telemetry_timestamp: '2026-01-01T00:00:00+00:00',
        last_state_update: '2026-01-01T00:00:01+00:00',
        reason: 'ok',
      },
      anomalies: { count: 0, items: [] },
      recoveries: { count: 0, items: [] },
    });

    const result = await getNodeSummary('power-001');
    expect(result.current_state.node_id).toBe('power-001');
  });

  it('throws ApiError with the code/message from a 404 response', async () => {
    mockFetchOnce(404, { error: 'NODE_NOT_FOUND', message: 'No current state exists for the requested node.' });

    await expect(getNode('does-not-exist')).rejects.toMatchObject(
      new ApiError(404, { error: 'NODE_NOT_FOUND', message: 'No current state exists for the requested node.' }),
    );
  });

  it('encodes node_id in the URL path', async () => {
    const fetchMock = mockFetchOnce(200, { node: {} });
    await getNode('power 001/../weird');
    const calledUrl = fetchMock.mock.calls[0]![0] as string;
    expect(calledUrl).not.toContain(' ');
  });

  describe('getNodes() (Step 40 fleet listing)', () => {
    it('calls GET /nodes with no query string when no options are given', async () => {
      const fetchMock = mockFetchOnce(200, { nodes: [], count: 0, limit: 50, order: 'desc' });
      await getNodes();
      const calledUrl = fetchMock.mock.calls[0]![0] as string;
      expect(calledUrl.endsWith('/nodes')).toBe(true);
    });

    it('includes limit/order/subsystem/state as query parameters when given', async () => {
      const fetchMock = mockFetchOnce(200, { nodes: [], count: 0, limit: 100, order: 'asc' });
      await getNodes({ limit: 100, order: 'asc', subsystem: 'power', state: 'HEALTHY' });
      const calledUrl = fetchMock.mock.calls[0]![0] as string;
      expect(calledUrl).toContain('/nodes?');
      expect(calledUrl).toContain('limit=100');
      expect(calledUrl).toContain('order=asc');
      expect(calledUrl).toContain('subsystem=power');
      expect(calledUrl).toContain('state=HEALTHY');
    });

    it('resolves with the typed NodeListResponse on success', async () => {
      const body: NodeListResponse = {
        nodes: [
          {
            node_id: 'power-001',
            subsystem: 'power',
            status: 'ONLINE',
            state: 'HEALTHY',
            last_telemetry_timestamp: '2026-01-01T00:00:00+00:00',
            last_state_update: '2026-01-01T00:00:01+00:00',
            reason: 'ok',
          },
        ],
        count: 1,
        limit: 50,
        order: 'desc',
      };
      mockFetchOnce(200, body);

      const result = await getNodes();

      expect(result).toEqual(body);
    });

    it('throws ApiError with the code/message from a 400 response', async () => {
      mockFetchOnce(400, { error: 'INVALID_LIMIT', message: 'limit must be between 1 and 500.' });

      await expect(getNodes({ limit: 9999 })).rejects.toMatchObject(
        new ApiError(400, { error: 'INVALID_LIMIT', message: 'limit must be between 1 and 500.' }),
      );
    });

    it('propagates a network/CORS-style fetch failure as a rejected promise', async () => {
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

      await expect(getNodes()).rejects.toThrow('Failed to fetch');
    });
  });

  describe('getEvents() (Step 42 fleet-wide event feed)', () => {
    it('calls GET /events with no query string when no options are given', async () => {
      const fetchMock = mockFetchOnce(200, { events: [], count: 0, limit: 50, order: 'desc' });
      await getEvents();
      const calledUrl = fetchMock.mock.calls[0]![0] as string;
      expect(calledUrl.endsWith('/events')).toBe(true);
    });

    it('includes limit/order/event_type/subsystem/severity/status as query parameters when given', async () => {
      const fetchMock = mockFetchOnce(200, { events: [], count: 0, limit: 20, order: 'asc' });
      await getEvents({
        limit: 20,
        order: 'asc',
        event_type: 'recovery',
        subsystem: 'water',
        severity: 'HIGH',
        status: 'SUCCESS',
      });
      const calledUrl = fetchMock.mock.calls[0]![0] as string;
      expect(calledUrl).toContain('/events?');
      expect(calledUrl).toContain('limit=20');
      expect(calledUrl).toContain('order=asc');
      expect(calledUrl).toContain('event_type=recovery');
      expect(calledUrl).toContain('subsystem=water');
      expect(calledUrl).toContain('severity=HIGH');
      expect(calledUrl).toContain('status=SUCCESS');
    });

    it('resolves with the typed EventListResponse on success', async () => {
      const body: EventListResponse = {
        events: [
          {
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
          },
        ],
        count: 1,
        limit: 50,
        order: 'desc',
      };
      mockFetchOnce(200, body);

      const result = await getEvents();

      expect(result).toEqual(body);
    });

    it('throws ApiError with the code/message from a 400 response', async () => {
      mockFetchOnce(400, {
        error: 'INVALID_STATUS',
        message: 'status only applies to recovery events; remove event_type=anomaly or drop the status filter.',
      });

      await expect(getEvents({ event_type: 'anomaly', status: 'SUCCESS' })).rejects.toMatchObject(
        new ApiError(400, {
          error: 'INVALID_STATUS',
          message: 'status only applies to recovery events; remove event_type=anomaly or drop the status filter.',
        }),
      );
    });

    it('propagates a network/CORS-style fetch failure as a rejected promise', async () => {
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

      await expect(getEvents()).rejects.toThrow('Failed to fetch');
    });
  });

  describe('getMetrics() (Step 43 CloudWatch metrics feed)', () => {
    it('calls GET /metrics with no query string when no options are given', async () => {
      const fetchMock = mockFetchOnce(200, {
        namespace: 'SentinelGrid',
        start: '2026-01-01T00:00:00+00:00',
        end: '2026-01-01T01:00:00+00:00',
        period_seconds: 60,
        series: [],
      });
      await getMetrics();
      const calledUrl = fetchMock.mock.calls[0]![0] as string;
      expect(calledUrl.endsWith('/metrics')).toBe(true);
    });

    it('includes start/end/subsystem/fault_type as query parameters when given', async () => {
      const fetchMock = mockFetchOnce(200, {
        namespace: 'SentinelGrid',
        start: '2026-01-01T00:00:00+00:00',
        end: '2026-01-01T01:00:00+00:00',
        period_seconds: 60,
        series: [],
      });
      await getMetrics({
        start: '2026-01-01T00:00:00+00:00',
        end: '2026-01-01T01:00:00+00:00',
        subsystem: 'power',
        fault_type: 'NODE_OVERLOAD',
      });
      const calledUrl = fetchMock.mock.calls[0]![0] as string;
      expect(calledUrl).toContain('/metrics?');
      expect(calledUrl).toContain('start=2026-01-01T00%3A00%3A00%2B00%3A00');
      expect(calledUrl).toContain('end=2026-01-01T01%3A00%3A00%2B00%3A00');
      expect(calledUrl).toContain('subsystem=power');
      expect(calledUrl).toContain('fault_type=NODE_OVERLOAD');
    });

    it('resolves with the typed MetricsResponse on success', async () => {
      const body: MetricsResponse = {
        namespace: 'SentinelGrid',
        start: '2026-01-01T00:00:00+00:00',
        end: '2026-01-01T01:00:00+00:00',
        period_seconds: 60,
        series: [
          {
            metric_name: 'FaultDetectionCount',
            unit: 'Count',
            dimensions: { Subsystem: 'power', FaultType: 'NODE_OVERLOAD' },
            datapoints: [{ timestamp: '2026-01-01T00:00:00+00:00', value: 3 }],
            summary: { datapoint_count: 1, sum: 3 },
          },
        ],
      };
      mockFetchOnce(200, body);

      const result = await getMetrics();

      expect(result).toEqual(body);
    });

    it('throws ApiError with the code/message from a 400 response', async () => {
      mockFetchOnce(400, { error: 'INVALID_TIME_RANGE', message: 'end must be after start.' });

      await expect(
        getMetrics({ start: '2026-01-01T01:00:00+00:00', end: '2026-01-01T00:00:00+00:00' }),
      ).rejects.toMatchObject(new ApiError(400, { error: 'INVALID_TIME_RANGE', message: 'end must be after start.' }));
    });

    it('propagates a network/CORS-style fetch failure as a rejected promise', async () => {
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

      await expect(getMetrics()).rejects.toThrow('Failed to fetch');
    });
  });
});
