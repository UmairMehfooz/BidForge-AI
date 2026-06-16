import { useEffect, useRef, useState } from 'react';
import { UploadCloud, Loader2, TrendingUp, Trophy, Activity, Database } from 'lucide-react';
import Toast from '../components/Toast';

export default function BidHistory() {
  const [rows, setRows] = useState([]);
  const [stats, setStats] = useState(null);
  const [model, setModel] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [toast, setToast] = useState(null);
  const fileInputRef = useRef(null);

  const showToast = (msg, type) => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 4000);
  };

  const applyPayload = (data) => {
    setRows(data?.rows || []);
    setStats(data?.stats || null);
    setModel(data?.model || null);
  };

  useEffect(() => {
    fetch('/api/bid-history')
      .then(res => {
        if (!res.ok) throw new Error('Failed to load bid history');
        return res.json();
      })
      .then(applyPayload)
      .catch(() => showToast('Unable to load bid history.', 'error'))
      .finally(() => setIsLoading(false));
  }, []);

  const handleUpload = async (file) => {
    if (!file) return;
    setIsUploading(true);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const res = await fetch('/api/bid-history/upload', { method: 'POST', body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || 'Upload failed');
      applyPayload(data);
      showToast(data.message || 'Dataset replaced — win model retrained.', 'success');
    } catch (err) {
      showToast(String(err.message || err), 'error');
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const winRatePct = stats ? Math.round((stats.overall_win_rate || 0) * 100) : 0;
  const sectorEntries = Object.entries(stats?.win_rate_by_sector || {}).sort((a, b) => b[1] - a[1]);

  if (isLoading) {
    return <div className="h-full flex items-center justify-center text-brand-muted">Loading bid history…</div>;
  }

  return (
    <div className="p-8 max-w-7xl mx-auto animate-page-mount">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-brand-heading tracking-tight">Bidding History</h1>
          <p className="text-brand-muted mt-1">
            Historical bid outcomes powering sector win rates and the win-probability model.
          </p>
        </div>
        <label className={`flex items-center gap-2 px-5 py-2.5 rounded-full font-medium transition-all cursor-pointer ${
          isUploading
            ? 'bg-brand-border text-brand-muted cursor-wait'
            : 'bg-brand-primary hover:bg-brand-primary/90 text-white hover:scale-[0.97]'
        }`}>
          {isUploading ? <Loader2 className="w-5 h-5 animate-spin" /> : <UploadCloud className="w-5 h-5" />}
          {isUploading ? 'Retraining model…' : 'Upload New CSV'}
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv"
            className="hidden"
            disabled={isUploading}
            onChange={(e) => handleUpload(e.target.files?.[0])}
          />
        </label>
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-4 gap-6 mb-8">
        {[
          { label: 'Total Bids', value: String(stats?.total_bids ?? 0), icon: Database },
          { label: 'Overall Win Rate', value: `${winRatePct}%`, icon: Trophy },
          { label: 'Avg Score (Wins)', value: stats?.avg_score_wins != null ? `${Math.round(stats.avg_score_wins)}%` : '—', icon: TrendingUp },
          { label: 'Model Accuracy', value: model?.train_accuracy != null ? `${Math.round(model.train_accuracy * 100)}%` : '—', icon: Activity },
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

      {/* Sector win rates */}
      {sectorEntries.length > 0 && (
        <div className="bg-brand-surface rounded-xl p-6 border border-brand-border mb-8">
          <h3 className="text-sm font-semibold text-brand-heading border-b border-brand-border pb-3 mb-4">
            Win Rate by Sector
          </h3>
          <div className="grid grid-cols-2 gap-x-10 gap-y-4">
            {sectorEntries.map(([sector, rate]) => (
              <div key={sector}>
                <div className="flex justify-between text-xs mb-1.5">
                  <span className="text-brand-muted">{sector}</span>
                  <span className="font-mono text-brand-body">{Math.round(rate * 100)}%</span>
                </div>
                <div className="w-full h-1.5 bg-brand-elevated rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full ${rate >= 0.6 ? 'bg-brand-success' : rate >= 0.5 ? 'bg-brand-primary' : 'bg-brand-warning'}`}
                    style={{ width: `${Math.round(rate * 100)}%` }}
                  ></div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Data table */}
      <div className="bg-brand-surface rounded-xl border border-brand-border overflow-hidden">
        <div className="flex items-center justify-between p-4 border-b border-brand-border">
          <h3 className="text-sm font-semibold text-brand-heading">Bid Records</h3>
          <span className="bg-brand-elevated text-xs font-mono px-2 py-1 rounded text-brand-body">{rows.length} rows</span>
        </div>
        <div className="overflow-x-auto max-h-[480px] overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-brand-elevated z-10">
              <tr>
                {['Bid ID', 'Client', 'Sector', 'Budget', 'Score (%)', 'Outcome', 'Compliance %', 'Gaps Found', 'Bid Manager', 'Submission Date'].map(col => (
                  <th key={col} className="text-left px-4 py-3 text-xs font-semibold text-brand-muted whitespace-nowrap">{col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={row['Bid ID'] || i} className="border-t border-brand-border/50 hover:bg-brand-elevated/50 transition-colors">
                  <td className="px-4 py-2.5 font-mono text-xs text-brand-primary whitespace-nowrap">{row['Bid ID']}</td>
                  <td className="px-4 py-2.5 text-brand-body whitespace-nowrap">{row['Client']}</td>
                  <td className="px-4 py-2.5 text-brand-muted whitespace-nowrap">{row['Sector']}</td>
                  <td className="px-4 py-2.5 font-mono text-brand-body whitespace-nowrap">{row['Budget']}</td>
                  <td className="px-4 py-2.5 font-mono text-brand-body">{row['Score (%)']}</td>
                  <td className="px-4 py-2.5">
                    <span className={`text-[11px] uppercase font-bold px-2 py-0.5 rounded-full ${
                      String(row['Outcome']).toLowerCase() === 'win'
                        ? 'bg-brand-success/10 text-brand-success'
                        : 'bg-brand-danger/10 text-brand-danger'
                    }`}>
                      {row['Outcome']}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 font-mono text-brand-body">{row['Compliance %']}</td>
                  <td className="px-4 py-2.5 font-mono text-brand-body">{row['Gaps Found']}</td>
                  <td className="px-4 py-2.5 text-brand-muted whitespace-nowrap">{row['Bid Manager']}</td>
                  <td className="px-4 py-2.5 font-mono text-xs text-brand-muted whitespace-nowrap">{row['Submission Date']}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <p className="text-xs text-brand-muted mt-4">
        Required CSV columns: Sector, Budget, Score (%), Outcome (Win/Loss), Compliance %, Gaps Found.
        Uploading replaces the dataset and retrains the win-probability model immediately — the previous
        file is kept as a backup and restored automatically if the new one fails validation.
      </p>

      {toast && <Toast message={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
