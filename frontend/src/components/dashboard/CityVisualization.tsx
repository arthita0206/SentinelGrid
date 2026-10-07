import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { nodeStateTone } from '@/lib/status';
import { TONE_CLASSES } from '@/lib/status';
import { humanize } from '@/lib/utils';
import type { NodeCurrentState } from '@/types/api';

interface CityVisualizationProps {
  nodes: NodeCurrentState[];
}

interface PlacedNode {
  node: NodeCurrentState;
  x: number;
  y: number;
}

/** Deterministically lays out nodes on an abstract grid, grouped by
 * subsystem into three loose clusters — a stylized, NOT geographic,
 * arrangement. There is no real location data anywhere in this project
 * (the simulator has no coordinates), so this must never be presented
 * as a map. */
function layout(nodes: NodeCurrentState[]): PlacedNode[] {
  const clusters: Record<string, { cx: number; cy: number }> = {
    power: { cx: 25, cy: 30 },
    water: { cx: 75, cy: 30 },
    traffic: { cx: 50, cy: 72 },
  };

  const bySubsystem: Record<string, NodeCurrentState[]> = {};
  for (const node of nodes) {
    (bySubsystem[node.subsystem] ??= []).push(node);
  }

  const placed: PlacedNode[] = [];
  for (const [subsystem, group] of Object.entries(bySubsystem)) {
    const cluster = clusters[subsystem] ?? { cx: 50, cy: 50 };
    const cols = Math.ceil(Math.sqrt(group.length));
    group.forEach((node, i) => {
      const row = Math.floor(i / cols);
      const col = i % cols;
      placed.push({
        node,
        x: cluster.cx - (cols * 3.2) / 2 + col * 3.2,
        y: cluster.cy - (Math.ceil(group.length / cols) * 3.2) / 2 + row * 3.2,
      });
    });
  }
  return placed;
}

/** A polished, clearly-labeled STYLIZED visualization placeholder — not
 * a real map and not claimed to be one. Node positions are derived
 * deterministically from the node list (grouped by subsystem), not from
 * any geographic coordinate, because none exists in this project. */
export function CityVisualization({ nodes }: CityVisualizationProps) {
  const placed = useMemo(() => layout(nodes), [nodes]);

  return (
    <div className="relative overflow-hidden rounded-xl border border-surface-border bg-surface-900">
      <div
        className="absolute inset-0 opacity-[0.07]"
        style={{
          backgroundImage:
            'linear-gradient(to right, #ffffff 1px, transparent 1px), linear-gradient(to bottom, #ffffff 1px, transparent 1px)',
          backgroundSize: '24px 24px',
        }}
        aria-hidden="true"
      />
      <svg viewBox="0 0 100 100" className="relative h-80 w-full" aria-hidden="true">
        {placed.map(({ node, x, y }) => {
          const tone = nodeStateTone(node.state);
          const color = TONE_CLASSES[tone].dot;
          return (
            <g key={node.node_id}>
              <circle cx={x} cy={y} r={tone === 'critical' || tone === 'warning' ? 1.6 : 1.1} className={color} fill="currentColor" />
              {(tone === 'critical' || tone === 'warning') && (
                <circle cx={x} cy={y} r="2.6" className={color} fill="none" stroke="currentColor" strokeWidth="0.3" opacity="0.5">
                  <animate attributeName="r" values="1.6;3.2;1.6" dur="2.4s" repeatCount="indefinite" />
                  <animate attributeName="opacity" values="0.5;0;0.5" dur="2.4s" repeatCount="indefinite" />
                </circle>
              )}
            </g>
          );
        })}
      </svg>

      {/* Real HTML anchors, positioned to match the SVG dots above (same
       * 0-100 percentage coordinate space) -- kept as a separate HTML
       * layer, rather than an <a> nested inside the <svg>, so click/
       * keyboard-focus/screen-reader behavior is the same plain,
       * well-supported HTMLAnchorElement behavior used everywhere else
       * in this app (NodeTable, EventTable), not SVG's own distinct <a>
       * element semantics. The SVG above is now purely decorative
       * (aria-hidden) -- this layer is what a screen reader/keyboard
       * user actually navigates. */}
      <div className="absolute inset-0">
        {placed.map(({ node, x, y }) => (
          <Link
            key={node.node_id}
            to={`/nodes/${node.node_id}`}
            aria-label={`${node.node_id} — ${humanize(node.state)}. View node detail.`}
            title={`${node.node_id} — ${node.state}`}
            className="absolute flex h-4 w-4 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand-400"
            style={{ left: `${x}%`, top: `${y}%` }}
          />
        ))}
      </div>

      <div className="absolute bottom-3 left-3 rounded-md border border-surface-border bg-surface-950/80 px-2.5 py-1 text-[11px] text-slate-400 backdrop-blur">
        Simulated node layout — grouped by subsystem, not a geographic map. Click a node to view its detail.
      </div>
    </div>
  );
}
