import { useEffect, useMemo, useRef, useState } from 'react';
import { UploadCloud, Loader2, Library, Award, Building2, Search, Layers } from 'lucide-react';
import Toast from '../components/Toast';

export default function CapabilityLibrary() {
  const [records, setRecords] = useState([]);
  const [stats, setStats] = useState(null);
  const [source, setSource] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [toast, setToast] = useState(null);
  const [search, setSearch] = useState('');
  const [domainFilter, setDomainFilter] = useState('All');
  const [expandedId, setExpandedId] = useState(null);
  const fileInputRef = useRef(null);

  const showToast = (msg, type) => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 4000);
  };

  const applyPayload = (data) => {
    setRecords(data?.records || []);
    setStats(data?.stats || null);
    setSource(data?.source || '');
  };

  useEffect(() => {
    fetch('/api/capabilities')
      .then(res => {
        if (!res.ok) throw new Error('Failed to load capability library');
        return res.json();
      })
      .then(applyPayload)
      .catch(() => showToast('Unable to load the capability library.', 'error'))
      .finally(() => setIsLoading(false));
  }, []);

  const handleUpload = async (file) => {
    if (!file) return;
    setIsUploading(true);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const res = await fetch('/api/capabilities/upload', { method: 'POST', body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || 'Upload failed');
      applyPayload(data);
      setDomainFilter('All');
      showToast(data.message || 'Library replaced — index rebuilt.', 'success');
    } catch (err) {
      showToast(String(err.message || err), 'error');
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const domains = useMemo(
    () => Object.entries(stats?.domains || {}).sort((a, b) => b[1] - a[1]),
    [stats]
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return records.filter(r => {
      if (domainFilter !== 'All' && r.domain !== domainFilter) return false;
      if (!q) return true;
      return [r.id, r.domain, r.project_title, r.summary, r.certification, r.client_type]
        .some(v => String(v || '').toLowerCase().includes(q));
    });
  }, [records, search, domainFilter]);

  if (isLoading) {
    return <div className="h-full flex items-center justify-center text-brand-muted">Loading capability library…</div>;
  }

  return (
    <div className="p-8 max-w-7xl mx-auto animate-page-mount">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-brand-heading tracking-tight">Capability Library</h1>
          <p className="text-brand-muted mt-1">
            Past-project evidence used by RAG matching to judge every RFP requirement.
          </p>
        </div>
        <label className={`flex items-center gap-2 px-5 py-2.5 rounded-full font-medium transition-all cursor-pointer ${
          isUploading
            ? 'bg-brand-border text-brand-muted cursor-wait'
            : 'bg-brand-primary hover:bg-brand-primary/90 text-white hover:scale-[0.97]'
        }`}>
          {isUploading ? <Loader2 className="w-5 h-5 animate-spin" /> : <UploadCloud className="w-5 h-5" />}
          {isUploading ? 'Rebuilding index…' : 'Upload Library (JSON / CSV)'}
          <input
            ref={fileInputRef}
            type="file"
            accept=".json,.csv"
            className="hidden"
            disabled={isUploading}
            onChange={(e) => handleUpload(e.target.files?.[0])}
          />
        </label>
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-4 gap-6 mb-8">
        {[
          { label: 'Capabilities', value: String(stats?.total ?? 0), icon: Library },
          { label: 'Domains', value: String(Object.keys(stats?.domains || {}).length), icon: Layers },
          { label: 'Certified Projects', value: `${stats?.certified_pct ?? 0}%`, icon: Award },
          { label: 'Library Source', value: source === 'enriched' ? 'Enriched' : 'Raw', icon: Building2 },
        ].map((stat, i) => (
          <div key={i} className="bg-brand-surface rounded-xl p-6 border border-brand-border signature-border">
            <div className="flex items-center gap-3 text-brand-muted mb-3">
              <stat.icon className="w-5 h-5 text-brand-primary" />
              <span className="text-sm font-medium">{stat.label}</span>
            </div>
            <div className="text-3xl font-bold text-brand-heading font-sans">{stat.value}</div>
          </div>
        ))}
      </div>

      {/* Search + domain filter */}
      <div className="flex items-center gap-3 mb-6 flex-wrap">
        <div className="relative">
          <Search className="w-4 h-4 text-brand-muted absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search capabilities…"
            className="bg-brand-surface border border-brand-border rounded-full pl-9 pr-4 py-2 text-sm text-brand-body focus:outline-none focus:border-brand-primary w-[260px]"
          />
        </div>
        <button
          onClick={() => setDomainFilter('All')}
          className={`text-sm px-4 py-1.5 rounded-full transition-colors ${
            domainFilter === 'All' ? 'bg-brand-primary text-white' : 'bg-brand-surface text-brand-muted border border-brand-border hover:text-brand-body'
          }`}
        >
          All ({records.length})
        </button>
        {domains.map(([domain, count]) => (
          <button
            key={domain}
            onClick={() => setDomainFilter(domain)}
            className={`text-sm px-4 py-1.5 rounded-full transition-colors ${
              domainFilter === domain ? 'bg-brand-primary text-white' : 'bg-brand-surface text-brand-muted border border-brand-border hover:text-brand-body'
            }`}
          >
            {domain} ({count})
          </button>
        ))}
      </div>

      {/* Capability cards */}
      {filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 bg-brand-surface rounded-xl border border-brand-border border-dashed">
          <p className="text-brand-muted">No capabilities match your search.</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-5">
          {filtered.map((cap) => {
            const isExpanded = expandedId === cap.id;
            return (
              <div
                key={cap.id}
                onClick={() => setExpandedId(isExpanded ? null : cap.id)}
                className="bg-brand-surface rounded-xl p-5 border border-brand-border hover:border-brand-primary/50 transition-all cursor-pointer"
              >
                <div className="flex items-start justify-between gap-3 mb-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="font-mono text-xs text-brand-primary bg-brand-primary/10 px-1.5 py-0.5 rounded shrink-0">{cap.id}</span>
                    <h3 className="text-sm font-semibold text-brand-heading truncate">{cap.project_title || cap.domain}</h3>
                  </div>
                  {cap.certification && (
                    <span className="text-[10px] uppercase font-bold text-brand-success border border-brand-success/30 px-1.5 py-0.5 rounded-full shrink-0">
                      {cap.certification}
                    </span>
                  )}
                </div>
                <p className={`text-sm text-brand-body leading-relaxed ${isExpanded ? '' : 'line-clamp-3'}`}>
                  {cap.summary}
                </p>
                <div className="mt-3 flex items-center gap-3 text-xs text-brand-muted flex-wrap">
                  <span className="bg-brand-elevated px-2 py-0.5 rounded-md">{cap.domain}</span>
                  {cap.year_completed && <span>{cap.year_completed}</span>}
                  {cap.contract_value && <span className="font-mono">{cap.contract_value}</span>}
                  {cap.duration_months && <span>{cap.duration_months} mo</span>}
                  {cap.client_type && <span>{cap.client_type}</span>}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <p className="text-xs text-brand-muted mt-6">
        Upload accepts JSON (native schema) or CSV in the hackathon sample-sheet layout
        (Cap ID, Domain, Project Summary, Certification, Year Completed, Contract Value,
        Duration (months), Client Type). Replacing the library rebuilds the embedding index
        immediately; the previous library is backed up and restored automatically on failure.
      </p>

      {toast && <Toast message={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
