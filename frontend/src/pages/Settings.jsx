import { useEffect, useState } from 'react';
import { Settings as SettingsIcon, Database, Cpu, Trash2, Loader2, ShieldCheck, KeyRound } from 'lucide-react';
import Toast from '../components/Toast';

function Toggle({ checked, onChange, disabled }) {
  return (
    <button
      onClick={() => onChange(!checked)}
      disabled={disabled}
      className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${
        checked ? 'bg-brand-primary' : 'bg-brand-border'
      } ${disabled ? 'opacity-50' : ''}`}
    >
      <span
        className={`absolute top-0.5 w-5 h-5 bg-white rounded-full transition-transform ${
          checked ? 'translate-x-[22px]' : 'translate-x-0.5'
        }`}
      ></span>
    </button>
  );
}

export default function Settings() {
  const [data, setData] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [baseline, setBaseline] = useState('');
  const [toast, setToast] = useState(null);

  const showToast = (msg, type) => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 4000);
  };

  const apply = (payload) => {
    setData(payload);
    setBaseline(String(payload?.config?.manual_baseline_hours ?? ''));
  };

  useEffect(() => {
    fetch('/api/settings')
      .then(res => { if (!res.ok) throw new Error(); return res.json(); })
      .then(apply)
      .catch(() => showToast('Unable to load settings.', 'error'))
      .finally(() => setIsLoading(false));
  }, []);

  const patch = async (body, successMsg) => {
    setIsSaving(true);
    try {
      const res = await fetch('/api/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload?.detail || 'Save failed');
      apply(payload);
      showToast(successMsg || payload.message || 'Settings saved.', 'success');
    } catch (err) {
      showToast(String(err.message || err), 'error');
    } finally {
      setIsSaving(false);
    }
  };

  const handleClearCache = async () => {
    if (!window.confirm('Delete all cached LLM responses? The demo-day offline fallback will need re-warming.')) return;
    setIsSaving(true);
    try {
      const res = await fetch('/api/settings/clear-cache', { method: 'POST' });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload?.detail || 'Clear failed');
      apply(payload);
      showToast(payload.message, 'success');
    } catch (err) {
      showToast(String(err.message || err), 'error');
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return <div className="h-full flex items-center justify-center text-brand-muted">Loading settings…</div>;
  }

  const config = data?.config || {};
  const system = data?.system || {};
  const cache = data?.cache || {};
  const tags = Object.entries(cache.per_tag || {});

  return (
    <div className="p-8 max-w-4xl mx-auto animate-page-mount">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-brand-heading tracking-tight">Settings</h1>
        <p className="text-brand-muted mt-1">Runtime configuration and system status.</p>
      </div>

      {/* Configuration */}
      <div className="bg-brand-surface rounded-xl border border-brand-border p-6 mb-6 signature-border">
        <div className="flex items-center gap-2 mb-5">
          <SettingsIcon className="w-5 h-5 text-brand-primary" />
          <h2 className="text-sm font-semibold text-brand-heading">Configuration</h2>
          {isSaving && <Loader2 className="w-4 h-4 animate-spin text-brand-muted ml-auto" />}
        </div>

        <div className="space-y-5">
          <div className="flex items-center justify-between gap-6">
            <div>
              <p className="text-sm font-medium text-brand-body">Demo Mode</p>
              <p className="text-xs text-brand-muted mt-0.5">
                Cache-first with offline fallback — when GROQ fails, the nearest cached response is served instead of an error. Judge-day insurance.
              </p>
            </div>
            <Toggle checked={!!config.demo_mode} disabled={isSaving} onChange={(v) => patch({ demo_mode: v }, `Demo mode ${v ? 'enabled' : 'disabled'}.`)} />
          </div>

          <div className="flex items-center justify-between gap-6">
            <div>
              <p className="text-sm font-medium text-brand-body">Debug Endpoints</p>
              <p className="text-xs text-brand-muted mt-0.5">
                Enables /api/debug/match (hybrid retrieval inspector) and /api/debug/cache-stats.
              </p>
            </div>
            <Toggle checked={!!config.debug} disabled={isSaving} onChange={(v) => patch({ debug: v }, `Debug endpoints ${v ? 'enabled' : 'disabled'}.`)} />
          </div>

          <div className="flex items-center justify-between gap-6">
            <div>
              <p className="text-sm font-medium text-brand-body">Manual Baseline (hours)</p>
              <p className="text-xs text-brand-muted mt-0.5">
                Honestly-timed manual bid-prep baseline used for the "Time Saved" metric. Judges may ask how this was measured.
              </p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <input
                type="number"
                min="0.5"
                step="0.5"
                value={baseline}
                onChange={(e) => setBaseline(e.target.value)}
                className="w-20 bg-brand-bg border border-brand-border rounded-lg px-3 py-1.5 text-sm text-brand-body text-right focus:outline-none focus:border-brand-primary"
              />
              <button
                disabled={isSaving || !baseline || Number(baseline) === config.manual_baseline_hours}
                onClick={() => patch({ manual_baseline_hours: Number(baseline) }, 'Baseline updated.')}
                className="text-xs px-3 py-1.5 bg-brand-primary text-white rounded-lg disabled:opacity-40 hover:bg-brand-primary/90 transition-colors"
              >
                Save
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* System status */}
      <div className="bg-brand-surface rounded-xl border border-brand-border p-6 mb-6">
        <div className="flex items-center gap-2 mb-5">
          <Cpu className="w-5 h-5 text-brand-primary" />
          <h2 className="text-sm font-semibold text-brand-heading">System Status</h2>
        </div>
        <div className="grid grid-cols-2 gap-x-10 gap-y-3 text-sm">
          {[
            ['Capability Library', `${system.capabilities_indexed ?? 0} records (${system.capability_source || '—'})`],
            ['Bid History', `${system.bid_history_rows ?? 0} rows`],
            ['Win Model', system.win_model_trained ? `Trained (${Math.round((system.win_model_accuracy || 0) * 100)}% accuracy)` : 'Not trained'],
            ['LLM Model', config.groq_model],
          ].map(([label, value]) => (
            <div key={label} className="flex justify-between border-b border-brand-border/40 pb-2">
              <span className="text-brand-muted">{label}</span>
              <span className="text-brand-body font-medium text-right">{value}</span>
            </div>
          ))}
          <div className="flex justify-between border-b border-brand-border/40 pb-2">
            <span className="text-brand-muted flex items-center gap-1.5"><KeyRound className="w-3.5 h-3.5" /> GROQ API Key</span>
            <span className={config.groq_key_set ? 'text-brand-success font-medium' : 'text-brand-danger font-medium'}>
              {config.groq_key_set ? 'Configured' : 'Missing'}
            </span>
          </div>
          <div className="flex justify-between border-b border-brand-border/40 pb-2">
            <span className="text-brand-muted flex items-center gap-1.5"><ShieldCheck className="w-3.5 h-3.5" /> Supabase</span>
            <span className={config.supabase_configured ? 'text-brand-success font-medium' : 'text-brand-danger font-medium'}>
              {config.supabase_configured ? 'Configured' : 'Missing'}
            </span>
          </div>
          <div className="flex justify-between border-b border-brand-border/40 pb-2 col-span-2">
            <span className="text-brand-muted flex items-center gap-1.5"><KeyRound className="w-3.5 h-3.5" /> Fallback LLM (OpenRouter)</span>
            <span className={config.openrouter_configured ? 'text-brand-success font-medium' : 'text-brand-muted font-medium'}>
              {config.openrouter_configured ? config.openrouter_model : 'Not configured — set OPENROUTER_API_KEY in .env'}
            </span>
          </div>
        </div>
      </div>

      {/* Cache */}
      <div className="bg-brand-surface rounded-xl border border-brand-border p-6">
        <div className="flex items-center justify-between mb-5">
          <div className="flex items-center gap-2">
            <Database className="w-5 h-5 text-brand-primary" />
            <h2 className="text-sm font-semibold text-brand-heading">LLM Cache</h2>
            <span className="bg-brand-elevated text-xs font-mono px-2 py-0.5 rounded text-brand-body">{cache.cached_entries ?? 0} entries</span>
          </div>
          <button
            onClick={handleClearCache}
            disabled={isSaving || !(cache.cached_entries > 0)}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 border border-brand-danger/40 text-brand-danger rounded-lg hover:bg-brand-danger/10 disabled:opacity-40 transition-colors"
          >
            <Trash2 className="w-3.5 h-3.5" /> Clear cache
          </button>
        </div>
        {tags.length === 0 ? (
          <p className="text-xs text-brand-muted">No cache activity this session.</p>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-brand-muted">
                {['Call site', 'Hits', 'Misses', 'Live calls', 'Fallbacks'].map(h => (
                  <th key={h} className="text-left py-2 font-semibold">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tags.map(([tag, s]) => (
                <tr key={tag} className="border-t border-brand-border/40">
                  <td className="py-2 font-mono text-brand-primary">{tag}</td>
                  <td className="py-2 font-mono text-brand-success">{s.hits}</td>
                  <td className="py-2 font-mono text-brand-body">{s.misses}</td>
                  <td className="py-2 font-mono text-brand-body">{s.live_calls}</td>
                  <td className="py-2 font-mono text-brand-warning">{s.fallbacks}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="text-[11px] text-brand-muted mt-4">
          Before demo day: run the pipeline once on your demo PDF (or scripts/warm_cache.py) so it replays
          from cache even if GROQ is unreachable, then leave Demo Mode on.
        </p>
      </div>

      {toast && <Toast message={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
