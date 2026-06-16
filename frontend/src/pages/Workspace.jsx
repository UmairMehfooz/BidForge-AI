import { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { CheckCircle2, ChevronLeft, ChevronRight, ArrowRight, AlertTriangle } from 'lucide-react';

// Small ring used in the header score card (the 200px ScoreGauge is too big here)
function MiniGauge({ pct, color }) {
  const r = 19;
  const c = 2 * Math.PI * r;
  return (
    <svg width="50" height="50" className="-rotate-90 shrink-0">
      <circle cx="25" cy="25" r={r} stroke="#E9EEF5" strokeWidth="6" fill="none" />
      <circle
        cx="25" cy="25" r={r}
        stroke={color} strokeWidth="6" fill="none" strokeLinecap="round"
        strokeDasharray={c} strokeDashoffset={c - (Math.min(pct, 100) / 100) * c}
        style={{ transition: 'stroke-dashoffset 800ms ease-out' }}
      />
    </svg>
  );
}

export default function Workspace() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const id = pathname.split('/').pop();

  const [ws, setWs] = useState(null);
  const [reqs, setReqs] = useState([]);
  const [comp, setComp] = useState([]);
  const [sections, setSections] = useState([]);
  const [score, setScore] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [competitor, setCompetitor] = useState('unknown');
  const [isRescoring, setIsRescoring] = useState(false);
  const [effort, setEffort] = useState(null);

  const formatMinutes = (mins) => {
    if (mins == null) return null;
    const m = Math.floor(mins);
    const s = Math.round((mins - m) * 60);
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
  };

  // The saved bid_scores row stores raw_scores inside score_breakdown;
  // a fresh POST /score response has breakdown/overall_score_pct directly.
  const normalizeScore = (s) => {
    if (!s) return null;
    return {
      ...s,
      overall_score_pct: s.overall_score_pct ?? Math.round((s.overall_score ?? s.score ?? 0) * 1000) / 10,
      breakdown: s.breakdown || s.score_breakdown?.raw_scores || s.reasons?.raw_scores || {},
    };
  };

  useEffect(() => {
    if (!id) return;

    setIsLoading(true);
    setLoadError('');

    fetch(`/api/workspaces/${id}/overview`)
      .then(res => {
        if(!res.ok) throw new Error('Failed to fetch workspace overview');
        return res.json();
      })
      .then(data => {
        setWs(data.workspace);
        setReqs((data.requirements || []).map(r => ({
          id: r.id,
          ref: r.section_ref || 'General',
          text: r.requirement,
          type: String(r.type || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
          chip: r.deadline || r.budget_ref || '',
          taxonomy: r.taxonomy_name || null,
          note: r.note || '',
          isDone: !!r.is_done
        })));
        setComp((data.compliance_items || []).map(ci => ({
          reqId: ci.requirement_id,
          status: String(ci.status || '').toUpperCase(),
          note: ci.status === 'pass' ? (ci.gap_note || 'Matched') : (ci.gap_note || ''),
          gap: ci.status !== 'pass' ? (ci.gap_note || '') : '',
          confidence: typeof ci.confidence === 'number' ? ci.confidence : null,
          capId: ci.matched_capability_id || null
        })));
        setSections(data.proposal_sections || []);
        setScore(normalizeScore(data.latest_score));
        setCompetitor(data.workspace?.competitor_presence || 'unknown');
        setEffort(data.effort_metrics || null);
      })
      .catch(err => {
        console.error('Failed to fetch workspace overview:', err);
        setLoadError('Unable to load live workspace data.');
        setWs(null);
        setReqs([]);
        setComp([]);
        setScore(null);
      })
      .finally(() => setIsLoading(false));
  }, [id]);

  const [activeTab, setActiveTab] = useState('All');
  const [selectedReq, setSelectedReq] = useState(null);

  // Fix 7: save the competitor-presence input, then re-score so the gauge
  // and breakdown reflect the new 6th factor immediately.
  const handleCompetitorChange = async (value) => {
    const previous = competitor;
    setCompetitor(value);
    setIsRescoring(true);
    try {
      const patchRes = await fetch(`/api/workspaces/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ competitor_presence: value }),
      });
      if (!patchRes.ok) throw new Error('Failed to save competitor presence');
      const scoreRes = await fetch(`/api/workspaces/${id}/score`, { method: 'POST' });
      if (!scoreRes.ok) throw new Error('Failed to re-score workspace');
      setScore(normalizeScore(await scoreRes.json()));
    } catch (err) {
      console.error('Competitor presence update failed:', err);
      setCompetitor(previous);
    } finally {
      setIsRescoring(false);
    }
  };

  // Bid-manager note + done mark on the selected requirement
  const [noteDraft, setNoteDraft] = useState('');
  const [noteSaving, setNoteSaving] = useState(false);
  const [noteMsg, setNoteMsg] = useState('');

  const patchRequirement = async (reqId, payload) => {
    const res = await fetch(`/api/workspaces/${id}/requirements/${reqId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let detail = `Save failed (${res.status})`;
      try { detail = (await res.json())?.detail || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    return res.json();
  };

  const handleToggleDone = async (req) => {
    const next = !req.isDone;
    setReqs(prev => prev.map(r => r.id === req.id ? { ...r, isDone: next } : r));
    try {
      await patchRequirement(req.id, { is_done: next });
    } catch (err) {
      setReqs(prev => prev.map(r => r.id === req.id ? { ...r, isDone: !next } : r));
      setNoteMsg(err.message);
    }
  };

  const handleSaveNote = async (req) => {
    setNoteSaving(true);
    setNoteMsg('');
    try {
      await patchRequirement(req.id, { note: noteDraft });
      setReqs(prev => prev.map(r => r.id === req.id ? { ...r, note: noteDraft } : r));
      setNoteMsg('Saved ✓');
    } catch (err) {
      setNoteMsg(err.message);
    } finally {
      setNoteSaving(false);
    }
  };

  // Tab labels are shorthand for the full type names ("Evaluation" matches
  // "Evaluation Criteria", "Deadline" matches "Submission Deadline").
  const filteredReqs = reqs.filter(r => activeTab === 'All' || r.type.includes(activeTab));

  // Master-detail: always have a selection — default to the first visible
  // requirement, and re-select when the filter hides the current one.
  useEffect(() => {
    if (filteredReqs.length === 0) return;
    if (!selectedReq || !filteredReqs.some(r => r.id === selectedReq)) {
      setSelectedReq(filteredReqs[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reqs, activeTab]);

  const selected = reqs.find(r => r.id === selectedReq) || null;

  // Load the saved note into the editor whenever the selection changes
  useEffect(() => {
    setNoteDraft(selected?.note || '');
    setNoteMsg('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedReq, selected?.note]);
  const selectedComp = selected ? comp.find(c => c.reqId === selected.id) || null : null;
  const selectedSection = selected
    ? sections.find(s => s.requirement_id === selected.id) || null
    : null;
  const selectedIndex = selected ? filteredReqs.findIndex(r => r.id === selected.id) : -1;

  const riskOf = (c, req) => {
    if (!c) return { label: 'Not assessed', color: 'text-brand-muted' };
    const mandatory = (req?.type || '').includes('Mandatory');
    if (c.status === 'FAIL') return mandatory
      ? { label: 'Critical', color: 'text-brand-danger' }
      : { label: 'High', color: 'text-brand-danger' };
    if (c.status === 'PARTIAL') return { label: 'Medium', color: 'text-brand-warning' };
    return { label: 'Low', color: 'text-brand-success' };
  };

  const actionOf = (c) => {
    if (!c) return 'Run compliance matching to assess this requirement.';
    if (c.status === 'FAIL') return 'No matching capability — gather evidence, consider a partner, or treat as a no-bid risk.';
    if (c.status === 'PARTIAL') return 'Strengthen the narrative with adjacent experience and address the gap explicitly.';
    return 'Evidence matched — review the drafted section and approve it.';
  };

  const statusTone = (status) => {
    if (status === 'PASS') return 'text-brand-success';
    if (status === 'FAIL') return 'text-brand-danger';
    if (status === 'PARTIAL') return 'text-brand-warning';
    return 'text-brand-muted';
  };

  const statusBar = (status) => {
    if (status === 'PASS') return 'bg-brand-success';
    if (status === 'FAIL') return 'bg-brand-danger';
    return 'bg-brand-warning';
  };

  // Short uppercase type label for the table's TYPE column and detail chips
  const typeLabel = (type) => {
    if (type === 'Submission Deadline') return 'DEADLINE';
    if (type === 'Evaluation Criteria') return 'EVALUATION';
    return type.toUpperCase();
  };

  const decision = score?.decision || ws?.decision || 'PENDING';
  const decisionColor =
    decision === 'GO' ? '#059669' :
    decision === 'NO-GO' ? '#E11D48' :
    decision === 'CONDITIONAL' ? '#D97706' : '#64748B';
  const scorePct = score?.overall_score_pct ?? ws?.score ?? 0;

  const passCount = comp.filter(c => c.status === 'PASS').length;
  const failCount = comp.filter(c => c.status === 'FAIL').length;
  const partialCount = comp.filter(c => c.status === 'PARTIAL').length;

  const compByReq = Object.fromEntries(comp.map(c => [c.reqId, c]));

  if (isLoading) {
    return (
      <div className="h-full flex items-center justify-center text-brand-muted">
        Loading workspace…
      </div>
    );
  }

  if (loadError || !ws) {
    return (
      <div className="h-full flex items-center justify-center text-brand-danger">
        {loadError || 'Workspace not found.'}
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col overflow-hidden animate-page-mount bg-brand-bg">

      {/* ===== HEADER BAR ===== */}
      <div className="bg-brand-surface border-b border-brand-border px-6 py-4 flex items-center gap-4 flex-wrap">
        <div className="flex-1 min-w-[240px]">
          <div className="font-mono text-[10px] uppercase tracking-widest mb-1">
            <span className="text-brand-primary font-bold">RFP Analysis</span>
            <span className="text-brand-muted"> / Compliance Review</span>
          </div>
          <h1 className="text-2xl font-bold text-brand-heading truncate">{ws.name}</h1>
          {effort?.reduction_percent != null && (
            <p className="font-mono text-[11px] text-brand-muted mt-1">
              Pipeline {formatMinutes(effort.automated_minutes)} · Manual baseline {effort.manual_baseline_hours}h · {Math.round(effort.reduction_percent)}% faster
            </p>
          )}
        </div>

        {/* Score card */}
        <div className="flex items-center gap-3 bg-brand-surface border border-brand-border rounded-xl px-4 py-2.5 shadow-sm">
          <MiniGauge pct={scorePct} color={decisionColor} />
          <div>
            <div className="text-2xl font-bold font-mono text-brand-heading leading-tight">
              {Number(scorePct).toFixed(1)}%
            </div>
            <div className="font-mono text-[10px] uppercase tracking-wider font-bold" style={{ color: decisionColor }}>
              Bid Score · {decision}
            </div>
          </div>
        </div>

        {/* Pass / Fail / Partial chips */}
        <div className="flex items-stretch divide-x divide-brand-border bg-brand-surface border border-brand-border rounded-xl shadow-sm">
          {[
            { n: passCount, label: 'PASS', tone: 'text-brand-success' },
            { n: failCount, label: 'FAIL', tone: 'text-brand-danger' },
            { n: partialCount, label: 'PARTIAL', tone: 'text-brand-warning' },
          ].map(c => (
            <div key={c.label} className="px-4 py-2 text-center">
              <div className={`text-xl font-bold font-mono leading-tight ${c.tone}`}>{c.n}</div>
              <div className={`font-mono text-[9px] uppercase tracking-wider ${c.tone}`}>{c.label}</div>
            </div>
          ))}
        </div>

        <button
          onClick={() => navigate(`/workspace/${ws.id}/proposal`)}
          className="flex items-center gap-2 bg-brand-primary hover:bg-brand-primary/90 text-white px-5 py-3 rounded-xl font-medium shadow-sm transition-all hover:scale-[0.98] active:scale-[0.96]"
        >
          Generate Proposal Draft <ArrowRight className="w-4 h-4" />
        </button>
      </div>

      {/* ===== BODY ===== */}
      <div className="flex-1 flex overflow-hidden">

        {/* LEFT: Requirements table */}
        <div className="flex-1 flex flex-col min-w-0 bg-brand-surface">
          <div className="px-6 py-3 border-b border-brand-border flex items-center justify-between gap-4 flex-wrap">
            <div className="flex items-center gap-2">
              <h2 className="text-brand-heading font-bold">Requirements</h2>
              <span className="bg-brand-elevated font-mono text-xs px-2 py-0.5 rounded text-brand-body">{reqs.length}</span>
            </div>
            <div className="flex gap-2 overflow-x-auto">
              {['All', 'Mandatory', 'Evaluation', 'Deadline'].map(tab => (
                <button
                  key={tab}
                  onClick={() => setActiveTab(tab)}
                  className={`font-mono text-[10px] uppercase tracking-wider px-3 py-1.5 rounded-full whitespace-nowrap transition-colors ${
                    activeTab === tab
                      ? 'bg-brand-primary text-white font-bold'
                      : 'bg-brand-elevated text-brand-muted hover:text-brand-body'
                  }`}
                >
                  {tab}
                </button>
              ))}
            </div>
          </div>

          {/* Table header */}
          <div className="grid grid-cols-[90px_1fr_130px_100px] gap-3 px-6 py-2.5 border-b border-brand-border font-mono text-[10px] uppercase tracking-wider text-brand-muted">
            <span>Status</span>
            <span>Requirement</span>
            <span>Category</span>
            <span className="text-right">Type</span>
          </div>

          {/* Rows */}
          <div className="flex-1 overflow-y-auto">
            {filteredReqs.length === 0 ? (
              <div className="h-full flex items-center justify-center text-brand-muted text-sm">
                {reqs.length === 0 ? 'No requirements yet — run Parse on this RFP.' : 'No requirements match this filter.'}
              </div>
            ) : filteredReqs.map(req => {
              const c = compByReq[req.id];
              const isSel = selectedReq === req.id;
              return (
                <div
                  key={req.id}
                  onClick={() => setSelectedReq(req.id)}
                  className={`grid grid-cols-[90px_1fr_130px_100px] gap-3 px-6 py-3.5 border-b border-brand-border/60 cursor-pointer transition-colors items-start ${
                    isSel ? 'bg-brand-primary/5 shadow-[inset_3px_0_0_#2563EB]' : 'hover:bg-brand-bg'
                  }`}
                >
                  <span className={`font-mono text-[11px] font-bold pt-0.5 ${statusTone(c?.status)}`}>
                    <span className="mr-1">●</span>{c?.status || '—'}
                  </span>
                  <div className="min-w-0">
                    <p className={`text-sm font-medium leading-snug ${req.isDone ? 'text-brand-muted line-through' : 'text-brand-heading'}`}>
                      {req.text}
                      {req.isDone && <CheckCircle2 className="inline w-3.5 h-3.5 text-brand-success ml-1.5 -mt-0.5" title="Marked done" />}
                    </p>
                    {req.note && (
                      <p className="mt-1 text-[11px] text-brand-warning line-clamp-1 italic">📝 {req.note}</p>
                    )}
                  </div>
                  <span className="font-mono text-xs text-brand-primary pt-0.5 truncate" title={req.taxonomy || req.ref}>
                    {req.ref}
                  </span>
                  <span className="font-mono text-[10px] uppercase tracking-wider text-brand-muted text-right pt-1">
                    {typeLabel(req.type)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {/* RIGHT: Detail panel */}
        <div className="w-[400px] shrink-0 border-l border-brand-border bg-brand-bg overflow-y-auto">
          <div className="p-4 space-y-4">

            {/* Panel header: position + prev/next */}
            <div className="flex items-center justify-between">
              <span className="font-mono text-[11px] uppercase tracking-wider text-brand-muted font-bold">
                Requirement {selectedIndex + 1} / {filteredReqs.length}
              </span>
              <div className="flex gap-1.5">
                <button
                  disabled={selectedIndex <= 0}
                  onClick={() => setSelectedReq(filteredReqs[selectedIndex - 1].id)}
                  className="w-7 h-7 flex items-center justify-center rounded-lg border border-brand-border bg-brand-surface text-brand-muted hover:text-brand-heading disabled:opacity-30 transition-colors"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
                <button
                  disabled={selectedIndex >= filteredReqs.length - 1}
                  onClick={() => setSelectedReq(filteredReqs[selectedIndex + 1].id)}
                  className="w-7 h-7 flex items-center justify-center rounded-lg border border-brand-border bg-brand-surface text-brand-muted hover:text-brand-heading disabled:opacity-30 transition-colors"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>

            {!selected ? (
              <div className="bg-brand-surface border border-brand-border rounded-xl p-6 text-center text-sm text-brand-muted">
                {reqs.length === 0 ? 'No requirements yet — run Parse on this RFP.' : 'Select a requirement from the list.'}
              </div>
            ) : (
              <div key={selected.id} className="space-y-4 animate-page-mount">

                {/* Requirement card */}
                <div className="bg-brand-surface border border-brand-border rounded-xl p-4 shadow-sm">
                  <div className="flex items-center gap-1.5 flex-wrap mb-2">
                    <span className="font-mono text-[10px] font-bold text-brand-primary bg-brand-primary/10 px-2 py-0.5 rounded">{selected.ref}</span>
                    <span className="font-mono text-[10px] font-bold uppercase text-brand-body bg-brand-elevated px-2 py-0.5 rounded">{typeLabel(selected.type)}</span>
                    {selected.taxonomy && (
                      <span className="font-mono text-[10px] text-brand-primary border border-brand-primary/30 px-2 py-0.5 rounded-full">{selected.taxonomy}</span>
                    )}
                  </div>
                  <p className="text-base font-semibold text-brand-heading leading-snug">{selected.text}</p>
                  {selected.chip && (
                    <div className="mt-2.5 font-mono text-xs text-brand-body flex items-center gap-1.5">
                      <span className="w-1.5 h-1.5 rounded-full bg-brand-warning shrink-0"></span> {selected.chip}
                    </div>
                  )}
                </div>

                {/* Compliance analysis card */}
                <div className="bg-brand-surface border border-brand-border rounded-xl p-4 shadow-sm">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-sm font-bold text-brand-heading">Compliance Analysis</h3>
                    {selectedComp && (
                      <span className={`font-mono text-[10px] font-bold px-2 py-1 rounded-full ${
                        selectedComp.status === 'PASS' ? 'bg-brand-success/10 text-brand-success' :
                        selectedComp.status === 'FAIL' ? 'bg-brand-danger/10 text-brand-danger' :
                        'bg-brand-warning/10 text-brand-warning'
                      }`}>
                        ● {selectedComp.status}
                      </span>
                    )}
                  </div>

                  {!selectedComp ? (
                    <p className="text-sm text-brand-muted">Not matched yet — run compliance matching.</p>
                  ) : (
                    <div className="space-y-3">
                      <div>
                        <div className="flex justify-between font-mono text-[10px] uppercase tracking-wider text-brand-muted mb-1.5">
                          <span>Confidence</span>
                          <span className="text-brand-body">{selectedComp.confidence != null ? `${Math.round(selectedComp.confidence * 100)}%` : '—'}</span>
                        </div>
                        <div className="w-full h-1.5 bg-brand-elevated rounded-full overflow-hidden">
                          <div
                            className={`h-full rounded-full ${statusBar(selectedComp.status)}`}
                            style={{ width: `${Math.round((selectedComp.confidence ?? 0) * 100)}%` }}
                          ></div>
                        </div>
                      </div>

                      {selectedComp.capId && (
                        <div className="font-mono text-[11px] text-brand-muted">
                          Matched capability: <span className="text-brand-primary">{selectedComp.capId}</span>
                        </div>
                      )}

                      {selectedComp.gap && (
                        <div className="bg-brand-danger/5 border border-brand-danger/20 rounded-lg p-3">
                          <p className="font-mono text-[10px] font-bold uppercase tracking-wider text-brand-danger mb-1">Missing Evidence</p>
                          <p className="text-sm text-brand-body leading-snug">{selectedComp.gap}</p>
                        </div>
                      )}
                      {selectedComp.status === 'PASS' && selectedComp.note && selectedComp.note !== 'Matched' && (
                        <p className="text-sm text-brand-muted italic">"{selectedComp.note}"</p>
                      )}

                      <div className="bg-brand-primary/5 border border-brand-primary/20 rounded-lg p-3">
                        <p className="font-mono text-[10px] font-bold uppercase tracking-wider text-brand-primary mb-1">Recommended Action</p>
                        <p className="text-sm text-brand-body leading-snug">{actionOf(selectedComp)}</p>
                      </div>
                    </div>
                  )}

                  <div className="mt-3 pt-3 border-t border-brand-border grid grid-cols-2 gap-3 text-sm">
                    <div className="flex items-center justify-between">
                      <span className="text-brand-muted">Risk</span>
                      <span className={`font-bold ${riskOf(selectedComp, selected).color}`}>{riskOf(selectedComp, selected).label}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-brand-muted">Drafted</span>
                      {selectedSection ? (
                        <button onClick={() => navigate(`/workspace/${ws.id}/proposal`)} className="text-brand-primary font-medium hover:underline">
                          Yes →
                        </button>
                      ) : (
                        <span className="text-brand-muted">Not yet</span>
                      )}
                    </div>
                  </div>
                </div>

                {/* My note card */}
                <div className="bg-brand-surface border border-brand-border rounded-xl p-4 shadow-sm">
                  <div className="flex items-center justify-between mb-2">
                    <label className="font-mono text-[10px] font-bold uppercase tracking-wider text-brand-muted">My Note</label>
                    <button
                      onClick={() => handleToggleDone(selected)}
                      className={`text-xs font-medium px-3 py-1 border rounded-full flex items-center gap-1.5 transition-colors ${
                        selected.isDone
                          ? 'bg-brand-success/10 text-brand-success border-brand-success/30'
                          : 'text-brand-muted border-brand-border hover:text-brand-success hover:border-brand-success/50'
                      }`}
                    >
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      {selected.isDone ? 'Done' : 'Mark as done'}
                    </button>
                  </div>
                  <textarea
                    value={noteDraft}
                    onChange={(e) => setNoteDraft(e.target.value)}
                    rows={2}
                    placeholder="Add a short note about this requirement…"
                    className="w-full bg-brand-bg border border-brand-border rounded-lg p-3 text-sm text-brand-body focus:outline-none focus:border-brand-primary resize-y"
                  />
                  <div className="flex items-center justify-between mt-2">
                    <span className={`text-xs ${noteMsg.startsWith('Saved') ? 'text-brand-success' : 'text-brand-danger'}`}>
                      {noteMsg}
                    </span>
                    <button
                      onClick={() => handleSaveNote(selected)}
                      disabled={noteSaving || noteDraft === (selected.note || '')}
                      className="text-xs font-medium px-4 py-1.5 bg-brand-heading text-white rounded-lg hover:bg-brand-heading/90 disabled:opacity-40 transition-colors"
                    >
                      {noteSaving ? 'Saving…' : 'Save note'}
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* Known competitors */}
            <div>
              <label className="block font-mono text-[10px] font-bold uppercase tracking-wider text-brand-muted mb-1.5">Known Competitors</label>
              <select
                value={competitor}
                disabled={isRescoring}
                onChange={(e) => handleCompetitorChange(e.target.value)}
                className="w-full bg-brand-surface border border-brand-border rounded-lg px-3 py-2 text-sm text-brand-body focus:outline-none focus:border-brand-primary disabled:opacity-50"
              >
                <option value="unknown">Unknown</option>
                <option value="low">Low (0–1 competitors)</option>
                <option value="medium">Medium (2–3 competitors)</option>
                <option value="high">High (4+ competitors)</option>
              </select>
              {isRescoring && (
                <p className="text-xs text-brand-muted mt-1.5">Re-scoring…</p>
              )}
            </div>

            {/* Score breakdown (win-probability factors) */}
            <div className="bg-brand-surface border border-brand-border rounded-xl p-4 shadow-sm">
              <h3 className="font-mono text-[10px] font-bold uppercase tracking-wider text-brand-muted mb-3">Score Breakdown</h3>
              <div className="space-y-3">
                {[
                  { label: 'Compliance Rate', val: Math.round((score?.breakdown?.compliance_rate || 0) * 100) },
                  { label: 'Domain Match', val: Math.round((score?.breakdown?.domain_match || 0) * 100) },
                  { label: 'Budget Alignment', val: Math.round((score?.breakdown?.budget_alignment || 0) * 100) },
                  { label: 'Past Win Rate', val: Math.round((score?.breakdown?.past_win_rate || 0) * 100) },
                  { label: 'Capability Depth', val: Math.round((score?.breakdown?.capability_depth || 0) * 100) },
                  { label: 'Competitor Presence', val: Math.round((score?.breakdown?.competitor_presence || 0) * 100) },
                ].map((metric) => (
                  <div key={metric.label}>
                    <div className="flex justify-between text-xs mb-1">
                      <span className="text-brand-muted">{metric.label}</span>
                      <span className="font-mono text-brand-body">{metric.val}%</span>
                    </div>
                    <div className="w-full h-1.5 bg-brand-elevated rounded-full overflow-hidden">
                      <div className="h-full bg-brand-primary rounded-full animate-dash" style={{ width: `${metric.val}%` }}></div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Top gaps */}
            {comp.some(c => c.status === 'FAIL') && (
              <div className="bg-brand-danger/5 border border-brand-danger/20 p-3 rounded-xl flex items-start gap-2">
                <AlertTriangle className="w-4 h-4 text-brand-danger shrink-0 mt-0.5" />
                <div>
                  <p className="font-mono text-[10px] font-bold uppercase tracking-wider text-brand-danger mb-1">Top Gap</p>
                  <p className="text-xs text-brand-body font-medium leading-relaxed">
                    {(score?.gaps && score.gaps[0]?.gap_note) || 'Review the failed requirements before submission.'}
                  </p>
                </div>
              </div>
            )}

          </div>
        </div>
      </div>
    </div>
  );
}
