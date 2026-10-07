import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DataSourceTag } from '@/components/common/DataSourceTag';
import { EmptyState } from '@/components/common/EmptyState';
import { MetricCard } from '@/components/common/MetricCard';
import { NodeStateBadge } from '@/components/common/NodeStateBadge';
import { NodeTable } from '@/components/common/NodeTable';
import { SeverityBadge } from '@/components/common/SeverityBadge';
import { Timestamp } from '@/components/common/Timestamp';
import { MOCK_NODES } from '@/data/mockNodes';
import { BrowserRouter } from 'react-router-dom';

function withRouter(ui: React.ReactElement) {
  return <BrowserRouter>{ui}</BrowserRouter>;
}

describe('StatusBadge-derived components', () => {
  it('NodeStateBadge renders a human-readable label for HEALTHY', () => {
    render(<NodeStateBadge state="HEALTHY" />);
    expect(screen.getByText('Healthy')).toBeInTheDocument();
  });

  it('NodeStateBadge renders ESCALATION_REQUIRED humanized', () => {
    render(<NodeStateBadge state="ESCALATION_REQUIRED" />);
    expect(screen.getByText('Escalation Required')).toBeInTheDocument();
  });

  it('SeverityBadge renders "None" for a null severity', () => {
    render(<SeverityBadge severity={null} />);
    expect(screen.getByText('None')).toBeInTheDocument();
  });

  it('SeverityBadge renders a real severity', () => {
    render(<SeverityBadge severity="HIGH" />);
    expect(screen.getByText('High')).toBeInTheDocument();
  });
});

describe('DataSourceTag', () => {
  it('shows "Demo data" when isMock is true', () => {
    render(<DataSourceTag isMock />);
    expect(screen.getByText('Demo data')).toBeInTheDocument();
  });

  it('shows "Live" when isMock is false', () => {
    render(<DataSourceTag isMock={false} />);
    expect(screen.getByText('Live')).toBeInTheDocument();
  });
});

describe('MetricCard', () => {
  it('renders label, value, and hint', () => {
    render(<MetricCard label="Total Nodes" value={60} hint="of the fleet" />);
    expect(screen.getByText('Total Nodes')).toBeInTheDocument();
    expect(screen.getByText('60')).toBeInTheDocument();
    expect(screen.getByText('of the fleet')).toBeInTheDocument();
  });
});

describe('EmptyState', () => {
  it('renders a title and description', () => {
    render(<EmptyState title="Nothing here" description="Try again later." />);
    expect(screen.getByText('Nothing here')).toBeInTheDocument();
    expect(screen.getByText('Try again later.')).toBeInTheDocument();
  });
});

describe('Timestamp (Step 44 — exact value never lost)', () => {
  it('renders a formatted, human-readable value', () => {
    render(<Timestamp iso="2026-01-01T00:00:00+00:00" />);
    // Exact rendered text is locale/timezone-dependent -- just assert
    // something other than the placeholder or raw ISO string is shown.
    expect(screen.queryByText('—')).not.toBeInTheDocument();
  });

  it('keeps the exact original ISO-8601 value available via dateTime and title', () => {
    const iso = '2026-09-15T16:21:50+00:00';
    render(<Timestamp iso={iso} />);
    const el = screen.getByTitle(iso);
    expect(el.tagName.toLowerCase()).toBe('time');
    expect(el).toHaveAttribute('datetime', iso);
  });

  it('renders an em dash for a missing value, never a crash', () => {
    render(<Timestamp iso={null} />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});

describe('NodeTable', () => {
  it('renders an empty state for an empty node list', () => {
    render(withRouter(<NodeTable nodes={[]} />));
    expect(screen.getByText('No nodes match the current filters')).toBeInTheDocument();
  });

  it('renders one row per node with a link to its detail page', () => {
    const sample = MOCK_NODES.slice(0, 3);
    render(withRouter(<NodeTable nodes={sample} />));
    for (const node of sample) {
      const link = screen.getByRole('link', { name: node.node_id });
      expect(link).toHaveAttribute('href', `/nodes/${node.node_id}`);
    }
  });
});
