/**
 * Minimal, hand-drawn inline SVG icon set for SentinelGrid — deliberately
 * NOT an external icon library or any third-party brand asset, per Step
 * 39's "do not use copyrighted logos or external brand assets" rule.
 * Small and purpose-built rather than exhaustive.
 */
import type { SVGProps } from 'react';

type IconProps = SVGProps<SVGSVGElement>;

const base = {
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.75,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  // Every icon in this set is decorative -- always paired with visible
  // text (a nav label, a card title, an aria-label on the button that
  // hosts it) -- so it's hidden from the accessibility tree by default
  // to avoid a redundant, unlabeled announcement. A caller that genuinely
  // needs it exposed can still override this via props (spread after
  // base below).
  'aria-hidden': true as const,
};

export function DashboardIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
      <rect x="13.5" y="3.5" width="7" height="4.5" rx="1.5" />
      <rect x="13.5" y="10.5" width="7" height="10" rx="1.5" />
      <rect x="3.5" y="13" width="7" height="7.5" rx="1.5" />
    </svg>
  );
}

export function NodesIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="12" cy="5" r="2" />
      <circle cx="5" cy="17" r="2" />
      <circle cx="19" cy="17" r="2" />
      <path d="M12 7v4m0 0-6 4m6-4 6 4" />
    </svg>
  );
}

export function EventsIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M13 3 4 14h6l-1 7 9-11h-6z" />
    </svg>
  );
}

export function AnalyticsIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M4 20V10M11 20V4M18 20v-7" />
      <path d="M2.5 20h19" />
    </svg>
  );
}

export function ArchitectureIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="3.5" y="3.5" width="6" height="6" rx="1.2" />
      <rect x="14.5" y="3.5" width="6" height="6" rx="1.2" />
      <rect x="9" y="14.5" width="6" height="6" rx="1.2" />
      <path d="M6.5 9.5v2a1.5 1.5 0 0 0 1.5 1.5h1M17.5 9.5v2A1.5 1.5 0 0 1 16 13h-1" />
    </svg>
  );
}

export function PowerIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M13 2 4 14h6l-1 8 9-12h-6z" />
    </svg>
  );
}

export function WaterIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M12 3s6 6.5 6 11a6 6 0 1 1-12 0c0-4.5 6-11 6-11Z" />
    </svg>
  );
}

export function TrafficIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="8" y="2.5" width="8" height="19" rx="3" />
      <circle cx="12" cy="7" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="17" r="1.2" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function ChevronRightIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </svg>
  );
}

export function AlertIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M12 3 2 21h20L12 3Z" />
      <path d="M12 10v5" />
      <circle cx="12" cy="18" r="0.6" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function CheckCircleIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="m8.5 12.5 2.3 2.3L16 10" />
    </svg>
  );
}

export function ClockIcon(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.5 2" />
    </svg>
  );
}

export function SentinelMark(props: IconProps) {
  return (
    <svg viewBox="0 0 32 32" {...props}>
      <path d="M16 2 L28 7 V15 C28 22.5 22.8 27.8 16 30 C9.2 27.8 4 22.5 4 15 V7 Z" fill="#0b1220" stroke="#22d3ee" strokeWidth="1.4" />
      <path d="M16 8 L16 24 M9 14 L23 14 M9 19 L23 19" stroke="#22d3ee" strokeWidth="1.2" strokeLinecap="round" opacity={0.55} />
      <circle cx="16" cy="14" r="2.1" fill="#34d399" />
      <circle cx="10.5" cy="19" r="1.5" fill="#22d3ee" />
      <circle cx="21.5" cy="19" r="1.5" fill="#22d3ee" />
    </svg>
  );
}
