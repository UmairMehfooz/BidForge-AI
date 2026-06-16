export default function StatusBadge({ status, type = 'status' }) {
  let colors = 'bg-brand-elevated text-brand-muted border-brand-border';
  let pulse = false;

  if (type === 'decision') {
    if (status === 'GO') colors = 'bg-brand-success/10 text-brand-success border-brand-success/30 shadow-[0_0_10px_rgba(16,185,129,0.2)]';
    if (status === 'NO-GO') colors = 'bg-brand-danger/10 text-brand-danger border-brand-danger/30 shadow-[0_0_10px_rgba(239,68,68,0.2)]';
    if (status === 'CONDITIONAL') colors = 'bg-brand-warning/10 text-brand-warning border-brand-warning/30';
  } else {
    if (status === 'Parsed') colors = 'bg-blue-500/10 text-blue-600 border-blue-500/20';
    if (status === 'Matched') colors = 'bg-indigo-500/10 text-indigo-600 border-indigo-500/20';
    if (status === 'Drafting') { colors = 'bg-brand-warning/10 text-brand-warning border-brand-warning/30'; pulse = true; }
    if (status === 'Draft Ready') colors = 'bg-brand-success/10 text-brand-success border-brand-success/20';
    if (status === 'Exported') colors = 'bg-brand-elevated text-brand-body border-brand-border';
    if (status.includes('Processing')) pulse = true;
  }

  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border ${colors} ${pulse ? 'animate-pulse' : ''}`}>
      {status}
    </span>
  );
}
