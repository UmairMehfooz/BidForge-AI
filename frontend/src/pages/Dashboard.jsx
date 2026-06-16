import { useState, useEffect } from 'react';
import { UploadCloud, ArrowRight, Activity, CheckCircle, FileText, Download, Timer, Trash2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import StatusBadge from '../components/StatusBadge';
import UploadModal from './UploadModal';

export default function Dashboard() {
  const navigate = useNavigate();
  const [isUploadModalOpen, setUploadModalOpen] = useState(false);
  const [workspaces, setWorkspaces] = useState([]);
  const [stats, setStats] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState('All');

  // Pipeline stages grouped for the filter buttons
  const STATUS_GROUPS = {
    'Pending'     : ['uploaded', 'created'],
    'In Progress' : ['parsed', 'matched', 'drafting', 'drafted', 'scored'],
    'Done'        : ['exported'],
  };

  const groupOf = (ws) => {
    const s = String(ws.status || '').toLowerCase();
    for (const [group, statuses] of Object.entries(STATUS_GROUPS)) {
      if (statuses.includes(s)) return group;
    }
    return 'Pending';
  };

  const filteredWorkspaces = statusFilter === 'All'
    ? workspaces
    : workspaces.filter((ws) => groupOf(ws) === statusFilter);

  const [deletingId, setDeletingId] = useState(null);

  const handleDelete = async (e, ws) => {
    e.stopPropagation(); // don't navigate into the workspace
    if (!window.confirm(`Delete bid "${ws.name}" and all its data? This cannot be undone.`)) return;
    setDeletingId(ws.id);
    try {
      const res = await fetch(`/api/workspaces/${ws.id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`Delete failed (${res.status})`);
      setWorkspaces(prev => prev.filter(w => w.id !== ws.id));
      setStats(prev => prev.map(s =>
        s.label === 'Total Bids' ? { ...s, value: String(Math.max(0, Number(s.value) - 1)) } : s
      ));
    } catch (err) {
      console.error('Delete failed:', err);
      window.alert(`Could not delete "${ws.name}": ${err.message}`);
    } finally {
      setDeletingId(null);
    }
  };

  useEffect(() => {
    let cancelled = false;

    const loadDashboard = async () => {
      try {
        // One bulk endpoint (3 server-side queries) instead of an /overview
        // fan-out per workspace — the old approach was ~60 DB round trips.
        const res = await fetch('/api/workspaces/dashboard');
        if (!res.ok) throw new Error('API failed');

        const data = await res.json();
        const list = Array.isArray(data?.workspaces) ? data.workspaces : [];

        const enrichedWorkspaces = list.map((ws) => ({
          id: ws.id,
          name: ws.name,
          type: ws.sector || 'IT Services',
          status: ws.status ? ws.status.charAt(0).toUpperCase() + ws.status.slice(1) : 'Created',
          compliance: ws.compliance_pct ?? 0,
          score: Math.round(ws.score_pct ?? 0),
          decision: ws.decision || 'PENDING',
          reductionPercent: ws.reduction_percent ?? null,
        }));

        if (cancelled) return;

        setWorkspaces(enrichedWorkspaces);

        const totalBids = enrichedWorkspaces.length;
        const goDecisions = enrichedWorkspaces.filter((ws) => ws.decision === 'GO').length;
        const avgWinScore = totalBids
          ? Math.round(enrichedWorkspaces.reduce((sum, ws) => sum + (Number(ws.score) || 0), 0) / totalBids)
          : 0;
        const proposalsExported = enrichedWorkspaces.filter((ws) => String(ws.status || '').toLowerCase() === 'exported').length;

        const timedWorkspaces = enrichedWorkspaces.filter((ws) => ws.reductionPercent != null);
        const avgTimeSaved = timedWorkspaces.length
          ? Math.round(timedWorkspaces.reduce((sum, ws) => sum + ws.reductionPercent, 0) / timedWorkspaces.length)
          : null;

        setStats([
          { label: 'Total Bids', value: String(totalBids), icon: FileText },
          { label: 'GO Decisions', value: String(goDecisions), icon: CheckCircle },
          { label: 'Avg Win Score', value: `${avgWinScore}%`, icon: Activity },
          { label: 'Proposals Exported', value: String(proposalsExported), icon: Download },
          { label: 'Avg Time Saved', value: avgTimeSaved != null ? `${avgTimeSaved}% vs manual` : '—', icon: Timer },
        ]);
      } catch (err) {
        console.error('Failed to fetch workspaces:', err);
        if (!cancelled) {
          setWorkspaces([]);
          setStats([
            { label: 'Total Bids', value: '0', icon: FileText },
            { label: 'GO Decisions', value: '0', icon: CheckCircle },
            { label: 'Avg Win Score', value: '0%', icon: Activity },
            { label: 'Proposals Exported', value: '0', icon: Download },
            { label: 'Avg Time Saved', value: '—', icon: Timer },
          ]);
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    loadDashboard();

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="p-8 max-w-7xl mx-auto animate-page-mount">
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-brand-heading tracking-tight">Active Bids</h1>
          <p className="text-brand-muted mt-1">Manage and track your RFP analysis pipeline.</p>
        </div>
        <button 
          onClick={() => setUploadModalOpen(true)}
          className="flex items-center gap-2 bg-brand-primary hover:bg-brand-primary/90 text-white px-5 py-2.5 rounded-full font-medium transition-all hover:scale-[0.97] active:scale-[0.95]"
        >
          <UploadCloud className="w-5 h-5" />
          Analyze New RFP
        </button>
      </div>

      {/* Loading skeleton — shown instead of a flash of empty state */}
      {isLoading && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-6 mb-10">
            {[...Array(5)].map((_, i) => (
              <div key={i} className="bg-brand-surface rounded-xl p-6 border border-brand-border">
                <div className="h-4 w-24 rounded skeleton-shimmer mb-4"></div>
                <div className="h-8 w-16 rounded skeleton-shimmer"></div>
              </div>
            ))}
          </div>
          <div className="flex items-center gap-2 mb-6">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="h-8 w-24 rounded-full skeleton-shimmer"></div>
            ))}
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="bg-brand-surface rounded-xl p-6 border border-brand-border">
                <div className="h-5 w-48 rounded skeleton-shimmer mb-3"></div>
                <div className="h-4 w-32 rounded skeleton-shimmer mb-8"></div>
                <div className="h-2 w-full rounded skeleton-shimmer mb-3"></div>
                <div className="flex justify-between">
                  <div className="h-7 w-20 rounded skeleton-shimmer"></div>
                  <div className="h-7 w-16 rounded-full skeleton-shimmer"></div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {!isLoading && (
      <>
      {/* Stats Bar */}
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-6 mb-10">
        {stats.map((stat, i) => (
          <div key={i} className="bg-brand-surface rounded-xl p-6 border border-brand-border signature-border">
            <div className="flex items-center gap-3 text-brand-muted mb-3">
              <stat.icon className="w-5 h-5 text-brand-primary" />
              <span className="text-sm font-medium">{stat.label}</span>
            </div>
            <div className="text-3xl font-bold text-brand-heading font-sans">
              {stat.value}
            </div>
          </div>
        ))}
      </div>

      {/* Status filter */}
      <div className="flex items-center gap-2 mb-6">
        {['All', 'Pending', 'In Progress', 'Done'].map((group) => {
          const count = group === 'All'
            ? workspaces.length
            : workspaces.filter((ws) => groupOf(ws) === group).length;
          return (
            <button
              key={group}
              onClick={() => setStatusFilter(group)}
              className={`text-sm px-4 py-1.5 rounded-full transition-colors flex items-center gap-2 ${
                statusFilter === group
                  ? 'bg-brand-primary text-white'
                  : 'bg-brand-surface text-brand-muted border border-brand-border hover:text-brand-body'
              }`}
            >
              {group}
              <span className={`text-xs font-mono px-1.5 py-0.5 rounded-full ${
                statusFilter === group ? 'bg-white/20' : 'bg-brand-elevated'
              }`}>
                {count}
              </span>
            </button>
          );
        })}
      </div>

      {/* Bid Cards Grid */}
      {workspaces.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 bg-brand-surface rounded-xl border border-brand-border border-dashed">
          <UploadCloud className="w-12 h-12 text-brand-muted mb-4 opacity-50" />
          <h3 className="text-xl font-medium text-brand-heading mb-2">No bids analyzed yet</h3>
          <p className="text-brand-muted mb-6">Upload your first RFP to get started</p>
          <button 
            onClick={() => setUploadModalOpen(true)}
            className="bg-brand-primary text-white px-6 py-2.5 rounded-full font-medium transition-all hover:scale-[0.97]"
          >
            Analyze New RFP
          </button>
        </div>
      ) : filteredWorkspaces.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 bg-brand-surface rounded-xl border border-brand-border border-dashed">
          <p className="text-brand-muted">No bids in “{statusFilter}”.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {filteredWorkspaces.map((ws) => (
            <div 
              key={ws.id} 
              className="bg-brand-surface rounded-xl p-6 border border-brand-border signature-border hover:-translate-y-0.5 hover:border-brand-primary/50 transition-all duration-200 group cursor-pointer"
              onClick={() => navigate(`/workspace/${ws.id}`)}
            >
              <div className="flex justify-between items-start mb-4">
                <div>
                  <h3 className="text-lg font-bold text-brand-heading mb-2 line-clamp-1 group-hover:text-brand-primary transition-colors">{ws.name}</h3>
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium text-brand-muted bg-brand-elevated px-2 py-1 rounded-md">{ws.type}</span>
                    <StatusBadge status={ws.status} />
                  </div>
                </div>
                <button
                  onClick={(e) => handleDelete(e, ws)}
                  disabled={deletingId === ws.id}
                  title="Delete this bid"
                  className="p-2 rounded-lg text-brand-muted opacity-0 group-hover:opacity-100 hover:text-brand-danger hover:bg-brand-danger/10 transition-all disabled:opacity-50"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>

              <div className="flex items-center justify-between mt-6 pt-6 border-t border-brand-border/50">
                <div className="flex-1 mr-6">
                  <div className="flex justify-between text-xs mb-1.5">
                    <span className="text-brand-muted">Compliance</span>
                    <span className="font-mono text-brand-body">{ws.compliance}%</span>
                  </div>
                  <div className="w-full h-1.5 bg-brand-elevated rounded-full overflow-hidden">
                    <div className="h-full bg-brand-success rounded-full" style={{ width: `${ws.compliance}%` }}></div>
                  </div>
                </div>
                
                <div className="flex items-center gap-4">
                  <div className="text-right">
                    <div className="text-xs text-brand-muted mb-0.5">Win Score</div>
                    <div className="font-mono text-xl font-bold text-brand-heading">{ws.score}%</div>
                  </div>
                  <StatusBadge status={ws.decision} type="decision" />
                </div>
              </div>

              <div className="mt-4 flex justify-end">
                <span className="text-sm font-medium text-brand-primary flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  Open Workspace <ArrowRight className="w-4 h-4" />
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
      </>
      )}

      {isUploadModalOpen && <UploadModal onClose={() => setUploadModalOpen(false)} />}
    </div>
  );
}
