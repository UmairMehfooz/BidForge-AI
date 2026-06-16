import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FileText, ArrowRight, Search, Trash2 } from 'lucide-react';
import StatusBadge from '../components/StatusBadge';

export default function ActiveBids() {
  const navigate = useNavigate();
  const [workspaces, setWorkspaces] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [search, setSearch] = useState('');

  useEffect(() => {
    fetch('/api/workspaces/dashboard')
      .then(res => {
        if (!res.ok) throw new Error('Failed to load bids');
        return res.json();
      })
      .then(data => setWorkspaces(Array.isArray(data?.workspaces) ? data.workspaces : []))
      .catch(() => setWorkspaces([]))
      .finally(() => setIsLoading(false));
  }, []);

  const [deletingId, setDeletingId] = useState(null);

  const handleDelete = async (e, ws) => {
    e.stopPropagation(); // row click navigates — keep delete separate
    if (!window.confirm(`Delete bid "${ws.name}" and all its data? This cannot be undone.`)) return;
    setDeletingId(ws.id);
    try {
      const res = await fetch(`/api/workspaces/${ws.id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`Delete failed (${res.status})`);
      setWorkspaces(prev => prev.filter(w => w.id !== ws.id));
    } catch (err) {
      console.error('Delete failed:', err);
      window.alert(`Could not delete "${ws.name}": ${err.message}`);
    } finally {
      setDeletingId(null);
    }
  };

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return workspaces;
    return workspaces.filter(ws =>
      [ws.name, ws.sector, ws.status, ws.decision].some(v => String(v || '').toLowerCase().includes(q))
    );
  }, [workspaces, search]);

  if (isLoading) {
    return <div className="h-full flex items-center justify-center text-brand-muted">Loading bids…</div>;
  }

  return (
    <div className="p-8 max-w-7xl mx-auto animate-page-mount">
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-brand-heading tracking-tight">Active Bids</h1>
          <p className="text-brand-muted mt-1">Every RFP workspace and where it sits in the pipeline.</p>
        </div>
        <div className="relative">
          <Search className="w-4 h-4 text-brand-muted absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search bids…"
            className="bg-brand-surface border border-brand-border rounded-full pl-9 pr-4 py-2 text-sm text-brand-body focus:outline-none focus:border-brand-primary w-[260px]"
          />
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 bg-brand-surface rounded-xl border border-brand-border border-dashed">
          <FileText className="w-12 h-12 text-brand-muted mb-4 opacity-50" />
          <p className="text-brand-muted">{search ? 'No bids match your search.' : 'No bids yet — analyze an RFP from the Dashboard.'}</p>
        </div>
      ) : (
        <div className="bg-brand-surface rounded-xl border border-brand-border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-brand-elevated">
              <tr>
                {['Bid', 'Sector', 'Status', 'Compliance', 'Win Score', 'Decision', ''].map(col => (
                  <th key={col} className="text-left px-5 py-3 text-xs font-semibold text-brand-muted whitespace-nowrap">{col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map(ws => (
                <tr
                  key={ws.id}
                  onClick={() => navigate(`/workspace/${ws.id}`)}
                  className="border-t border-brand-border/50 hover:bg-brand-elevated/50 transition-colors cursor-pointer group"
                >
                  <td className="px-5 py-3.5 font-medium text-brand-heading group-hover:text-brand-primary transition-colors">{ws.name}</td>
                  <td className="px-5 py-3.5 text-brand-muted whitespace-nowrap">{ws.sector || '—'}</td>
                  <td className="px-5 py-3.5"><StatusBadge status={String(ws.status || 'created').replace(/_/g, ' ')} /></td>
                  <td className="px-5 py-3.5 w-[180px]">
                    <div className="flex items-center gap-2">
                      <div className="flex-1 h-1.5 bg-brand-elevated rounded-full overflow-hidden">
                        <div className="h-full bg-brand-success rounded-full" style={{ width: `${ws.compliance_pct ?? 0}%` }}></div>
                      </div>
                      <span className="font-mono text-xs text-brand-body w-9 text-right">{ws.compliance_pct ?? 0}%</span>
                    </div>
                  </td>
                  <td className="px-5 py-3.5 font-mono font-bold text-brand-heading">
                    {ws.score_pct != null ? `${Math.round(ws.score_pct)}%` : '—'}
                  </td>
                  <td className="px-5 py-3.5"><StatusBadge status={ws.decision || 'PENDING'} type="decision" /></td>
                  <td className="px-5 py-3.5 text-right whitespace-nowrap">
                    <button
                      onClick={(e) => handleDelete(e, ws)}
                      disabled={deletingId === ws.id}
                      title="Delete this bid"
                      className="p-1.5 rounded text-brand-muted opacity-0 group-hover:opacity-100 hover:text-brand-danger hover:bg-brand-danger/10 transition-all align-middle disabled:opacity-50"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                    <ArrowRight className="w-4 h-4 text-brand-muted opacity-0 group-hover:opacity-100 group-hover:text-brand-primary transition-all inline-block ml-2 align-middle" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
