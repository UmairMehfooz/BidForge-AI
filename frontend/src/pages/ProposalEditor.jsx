import { useState, useEffect, useCallback, useRef } from 'react';
import { Download, Edit2, FileText, Check, Square, Play, RotateCcw } from 'lucide-react';
import { useLocation } from 'react-router-dom';
import Toast from '../components/Toast';
import useBidStream from '../hooks/useBidStream';

export default function ProposalEditor() {
  const { pathname } = useLocation();
  const id = pathname.split('/')[2];

  const { sections: streamSections, isStreaming, error, progress, startStream, stopStream } = useBidStream(id);
  const [workspaceName, setWorkspaceName] = useState('Proposal Editor');
  const [localSections, setLocalSections] = useState([]);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [draftableCount, setDraftableCount] = useState(0);
  const [wsStatus, setWsStatus] = useState('');

  // Natural ascending order for section refs: numbers numerically
  // ("2" before "10"), letters after, "General" last.
  const sectionOrder = (ref) => {
    const text = String(ref || '').trim();
    if (!text || text.toLowerCase() === 'general') return [[1]];
    const tokens = text.match(/\d+|[A-Za-z]+/g) || [];
    return [[0], ...tokens.map(t => (/^\d+$/.test(t) ? [0, parseInt(t, 10)] : [1, t.toLowerCase()]))];
  };

  const compareRefs = (a, b) => {
    const ka = sectionOrder(a), kb = sectionOrder(b);
    for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
      const ta = ka[i], tb = kb[i];
      if (!ta) return -1;
      if (!tb) return 1;
      for (let j = 0; j < Math.max(ta.length, tb.length); j++) {
        const va = ta[j] ?? -1, vb = tb[j] ?? -1;
        if (va < vb) return -1;
        if (va > vb) return 1;
      }
    }
    return 0;
  };

  const loadSections = useCallback(() => {
    if (!id) return;
    fetch(`/api/workspaces/${id}/overview`)
      .then(res => {
        if (!res.ok) throw new Error('Failed to fetch proposal overview');
        return res.json();
      })
      .then(data => {
        setWorkspaceName(data?.workspace?.name || 'Proposal Editor');
        setWsStatus(String(data?.workspace?.status || '').toLowerCase());
        // How many requirements the backend will draft — used to detect a
        // partially-drafted (stopped) workspace and offer Resume.
        setDraftableCount(
          (data?.requirements || []).filter(r => ['mandatory', 'question'].includes(r.type)).length
        );
        const sections = (data?.proposal_sections || []).map((sec, idx) => ({
          id: sec.id || `sec-${idx}`,
          ref: sec.section_ref || `Section ${idx + 1}`,
          title: sec.section_title || (sec.section_ref ? `Section ${sec.section_ref}` : `Section ${idx + 1}`),
          text: sec.edited_draft || sec.ai_draft || '',
          status: sec.status || 'draft',
          source: 'Live workspace data',
        }));
        sections.sort((a, b) => compareRefs(a.ref, b.ref));
        // Never replace freshly streamed content with an empty fetch — if the
        // DB save failed (or hasn't landed yet) the streamed draft must stay
        // on screen so the user's work isn't visibly "deleted".
        setLocalSections(prev =>
          sections.length === 0 && prev.some(s => s.text) ? prev : sections
        );
        setHasLoaded(true);
      })
      .catch(() => {
        setWorkspaceName('Proposal Editor');
        setLocalSections(prev => (prev.some(s => s.text) ? prev : []));
        setHasLoaded(true);
      });
  }, [id]);

  useEffect(() => { loadSections(); }, [loadSections]);

  // Merge streamed sections into local state — sections the workspace has
  // never saved before must be APPENDED, not just merged into existing ones
  // (the old version mapped over existing sections only, so generating on a
  // fresh workspace streamed into an invisible void).
  useEffect(() => {
    const entries = Object.entries(streamSections);
    if (!entries.length) return;
    setLocalSections(prev => {
      const byRef = new Map(prev.map(s => [s.ref, s]));
      for (const [ref, streamed] of entries) {
        const existing = byRef.get(ref);
        if (existing) {
          byRef.set(ref, { ...existing, text: streamed.text, title: streamed.title || existing.title });
        } else {
          byRef.set(ref, {
            id: `stream-${ref}`,
            ref,
            title: streamed.title || `Section ${ref}`,
            text: streamed.text,
            status: 'draft',
            source: 'AI draft',
          });
        }
      }
      return Array.from(byRef.values());
    });
  }, [streamSections]);

  // When a stream finishes, re-fetch so sections get their real DB ids
  // (needed for edit/approve PATCHes) instead of the temporary stream-* ids.
  const wasStreaming = useRef(false);
  useEffect(() => {
    if (wasStreaming.current && !isStreaming) loadSections();
    wasStreaming.current = isStreaming;
  }, [isStreaming, loadSections]);

  // Surface stream errors as a toast when they actually occur
  useEffect(() => {
    if (error) showToast(error, 'error');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [error]);

  const [editingId, setEditingId] = useState(null);
  const [editValue, setEditValue] = useState('');
  const [toast, setToast] = useState(null);

  const startGenerating = (restart = false) => {
    startStream({ restart });
  };

  // Drafting progress: sections already saved before this stream + sections
  // completed during it. Drives the "X of Y" label and the Resume button.
  const savedWithText = localSections.filter(s => s.text).length;
  const streamedDone = Object.values(streamSections).filter(s => s.done).length;
  const totalToDraft = progress?.total ?? draftableCount;
  const doneSoFar = isStreaming ? (progress?.done ?? 0) + streamedDone : savedWithText;
  // Partially drafted (e.g. stopped mid-run): some sections saved, but the
  // workspace never reached 'drafted'. draftableCount can overcount slightly
  // (backend dedupes near-duplicates), so trust the status when it says done.
  const isPartial = savedWithText > 0 && wsStatus !== 'drafted' && savedWithText < draftableCount;

  const showToast = (msg, type) => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  };

  const handleEdit = (sec) => {
    setEditingId(sec.id);
    setEditValue(sec.text);
  };

  // Persist a section change to Supabase. Returns 'pending' for temporary
  // stream-* ids (the section isn't in the DB until the stream's batch save
  // lands) — the old code silently faked success here, which is why
  // approvals "reverted" on refresh.
  const patchSection = async (secId, body) => {
    if (String(secId).startsWith('stream-')) return 'pending';
    try {
      const res = await fetch(`/api/workspaces/${id}/proposal/${secId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return res.ok;
    } catch {
      return false;
    }
  };

  const reportSave = (result, okMsg) => {
    if (result === 'pending') {
      showToast('Still saving the generated draft — try again in a few seconds.', 'error');
    } else {
      showToast(result ? okMsg : 'Save failed — server unreachable.', result ? 'success' : 'error');
    }
  };

  const handleSave = async (secId) => {
    setLocalSections(prev => prev.map(s => s.id === secId ? { ...s, text: editValue, status: 'edited' } : s));
    setEditingId(null);
    const result = await patchSection(secId, { edited_draft: editValue, status: 'edited' });
    if (result === 'pending') setLocalSections(prev => prev.map(s => s.id === secId ? { ...s, status: 'draft' } : s));
    reportSave(result, 'Draft saved');
  };

  const handleApprove = async (secId) => {
    const sec = localSections.find(s => s.id === secId);
    setLocalSections(prev => prev.map(s => s.id === secId ? { ...s, status: 'approved' } : s));
    const result = await patchSection(secId, { edited_draft: sec?.text || '', status: 'approved' });
    // Roll the optimistic state back when nothing was persisted — a green
    // "Approved" that vanishes on refresh is worse than an honest error.
    if (result !== true) setLocalSections(prev => prev.map(s => s.id === secId ? { ...s, status: sec?.status || 'draft' } : s));
    reportSave(result, `Section ${sec?.ref} approved ✅`);
  };

  // 'draft' | 'full' | null — which export is currently running
  const [exporting, setExporting] = useState(null);

  const handleExport = async (kind) => {
    if (exporting) return; // one export at a time
    const endpoint = kind === 'full' ? 'export-full' : 'export';
    const filename = kind === 'full'
      ? `BidForge_Full_Proposal_${id.slice(0, 8)}.docx`
      : `BidForge_Proposal_${id.slice(0, 8)}.docx`;

    setExporting(kind);
    try {
      const res = await fetch(`/api/workspaces/${id}/${endpoint}`, { method: 'POST' });
      if (!res.ok) {
        let detail = `Export failed (${res.status})`;
        try { detail = (await res.json())?.detail || detail; } catch { /* not JSON */ }
        throw new Error(detail);
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Revoke after the click has been processed — revoking synchronously
      // can cancel the download in some browsers, making re-export "dead".
      setTimeout(() => window.URL.revokeObjectURL(url), 10000);
      showToast(kind === 'full' ? 'Full proposal exported 📄' : 'Draft exported 📄', 'success');
    } catch (err) {
      showToast(`Export failed: ${err.message}`, 'error');
    } finally {
      setExporting(null);
    }
  };

  return (
    <div className="h-full flex overflow-hidden animate-page-mount">
      
      {/* Left Sidebar */}
      <div className="w-[260px] bg-brand-surface border-r border-brand-border flex flex-col h-full shrink-0">
        <div className="p-4 border-b border-brand-border">
          <h2 className="text-brand-heading font-bold">Proposal Sections</h2>
        </div>
        <div className="flex-1 overflow-y-auto p-3 space-y-1">
          {hasLoaded && localSections.length === 0 && (
            <p className="text-xs text-brand-muted p-2.5 leading-relaxed">
              {isStreaming ? 'Sections will appear here as they are drafted…' : 'No sections yet — generate the proposal draft to get started.'}
            </p>
          )}
          {localSections.map(sec => (
            <button 
              key={sec.id}
              onClick={() => document.getElementById(`sec-${sec.id}`)?.scrollIntoView({ behavior: 'smooth' })}
              className="w-full text-left flex items-center gap-3 p-2.5 rounded-lg hover:bg-brand-elevated transition-colors group"
            >
              <div className={`w-2 h-2 rounded-full shrink-0 ${sec.status === 'approved' ? 'bg-brand-success' : sec.status === 'edited' ? 'bg-brand-warning' : 'bg-brand-muted'}`}></div>
              <div className="flex-1 min-w-0">
                <div className="text-xs font-mono text-brand-primary mb-0.5">{sec.ref}</div>
                <div className="text-sm text-brand-body truncate group-hover:text-brand-heading">{sec.title}</div>
              </div>
            </button>
          ))}
        </div>
        <div className="p-4 border-t border-brand-border space-y-2">
          <button
            onClick={() => handleExport('full')}
            disabled={!!exporting}
            className="w-full bg-brand-primary hover:bg-brand-primary/90 disabled:opacity-50 disabled:cursor-wait text-white py-2.5 rounded-lg font-medium flex items-center justify-center gap-2 transition-all shadow-[0_0_10px_rgba(59,130,246,0.2)]"
          >
            <FileText className="w-4 h-4" />
            {exporting === 'full' ? 'Building Full Proposal…' : 'Export Full Proposal'}
          </button>
          <button
            onClick={() => handleExport('draft')}
            disabled={!!exporting}
            className="w-full bg-brand-success hover:bg-brand-success/90 disabled:opacity-50 disabled:cursor-wait text-white py-2.5 rounded-lg font-medium flex items-center justify-center gap-2 transition-all shadow-[0_0_10px_rgba(16,185,129,0.2)]"
          >
            <Download className="w-4 h-4" />
            {exporting === 'draft' ? 'Exporting…' : 'Export Draft DOCX'}
          </button>
        </div>
      </div>

      {/* Main Editor */}
      <div className="flex-1 bg-brand-bg flex flex-col h-full relative scroll-smooth overflow-y-auto">
        <div className="sticky top-0 z-10 bg-brand-bg/80 backdrop-blur-md border-b border-brand-border p-6 flex justify-between items-center">
          <div>
            <h1 className="text-xl font-bold text-brand-heading">{workspaceName}</h1>
            <p className="text-sm text-brand-muted mt-1">
              {isStreaming
                ? `Drafting… ${Math.min(doneSoFar, totalToDraft)} of ${totalToDraft} sections${progress?.resumed ? ' (resumed)' : ''}`
                : localSections.length === 0
                  ? 'No sections drafted yet'
                  : isPartial
                    ? `Paused — ${savedWithText} of ${draftableCount} sections drafted`
                    : localSections.every(s => s.status === 'approved')
                      ? 'All Sections Approved'
                      : 'Drafting Mode'}
            </p>
          </div>

          <div className="flex items-center gap-2">
            {isStreaming && (
              <button
                onClick={stopStream}
                className="flex items-center gap-2 bg-brand-danger hover:bg-brand-danger/90 text-white px-5 py-2.5 rounded-lg font-medium transition-all"
              >
                <Square className="w-3.5 h-3.5 fill-current" />
                Stop
              </button>
            )}
            {!isStreaming && hasLoaded && isPartial && (
              <button
                onClick={() => startGenerating(false)}
                className="flex items-center gap-2 bg-brand-primary hover:bg-brand-primary/90 text-white px-5 py-2.5 rounded-lg font-medium shadow-[0_0_15px_rgba(37,99,235,0.25)] transition-all"
              >
                <Play className="w-4 h-4" />
                Resume Drafting ({Math.max(0, draftableCount - savedWithText)} left)
              </button>
            )}
            {!isStreaming && hasLoaded && savedWithText > 0 && (
              <button
                onClick={() => startGenerating(true)}
                title="Discard all sections and draft everything again"
                className="flex items-center gap-2 text-brand-muted hover:text-brand-body border border-brand-border hover:border-brand-muted px-4 py-2.5 rounded-lg font-medium text-sm transition-colors"
              >
                <RotateCcw className="w-4 h-4" />
                Regenerate All
              </button>
            )}
          </div>
        </div>

        {/* Empty state — no sections saved and nothing streaming yet */}
        {hasLoaded && localSections.length === 0 && !isStreaming && (
          <div className="flex-1 flex flex-col items-center justify-center text-center p-8">
            <div className="w-16 h-16 rounded-2xl bg-brand-surface border border-brand-border flex items-center justify-center mb-5">
              <FileText className="w-8 h-8 text-brand-muted" />
            </div>
            <h2 className="text-xl font-semibold text-brand-heading mb-2">No proposal sections yet</h2>
            <p className="text-brand-muted text-sm max-w-md mb-8">
              Generate the AI draft to create a section for every mandatory requirement
              and question in this RFP. You can edit and approve each section afterwards.
            </p>
            <button
              onClick={() => startGenerating(false)}
              className="bg-brand-primary hover:bg-brand-primary/90 text-white px-8 py-3 rounded-lg font-medium shadow-[0_0_15px_rgba(37,99,235,0.25)] transition-all"
            >
              Generate Proposal Draft
            </button>
          </div>
        )}

        <div className="p-8 max-w-4xl mx-auto w-full space-y-8 pb-32">
          {localSections.map(sec => {
            const isApproved = sec.status === 'approved';
            const hasText = sec.text.length > 0;
            const isCurrentlyStreaming = isStreaming && hasText && sec.text.length < 150; // naive check for demo
            
            return (
              <div 
                key={sec.id} 
                id={`sec-${sec.id}`}
                className={`bg-brand-surface rounded-xl border transition-all duration-300 relative overflow-hidden ${
                  isApproved ? 'border-brand-success' : 
                  isCurrentlyStreaming ? 'animate-shimmer-border' : 
                  'border-brand-border signature-border'
                }`}
              >
                {/* Header */}
                <div className="bg-brand-elevated border-b border-brand-border p-4 flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-xs bg-brand-primary/10 text-brand-primary px-2 py-1 rounded">{sec.ref}</span>
                    <h3 className="font-semibold text-brand-heading">{sec.title}</h3>
                  </div>
                  
                  {hasText && editingId !== sec.id && (
                    <div className="flex items-center gap-2">
                      <button 
                        onClick={() => handleEdit(sec)}
                        className="text-xs font-medium text-brand-muted hover:text-brand-body px-3 py-1.5 border border-brand-border rounded flex items-center gap-1.5 transition-colors"
                      >
                        <Edit2 className="w-3.5 h-3.5" /> Edit
                      </button>
                      <button 
                        onClick={() => handleApprove(sec.id)}
                        disabled={isApproved}
                        className={`text-xs font-medium px-3 py-1.5 border rounded flex items-center gap-1.5 transition-colors ${
                          isApproved 
                            ? 'bg-brand-success/10 text-brand-success border-brand-success/30' 
                            : 'text-brand-success border-brand-success/50 hover:bg-brand-success/10'
                        }`}
                      >
                        <Check className="w-3.5 h-3.5" /> {isApproved ? 'Approved' : 'Approve'}
                      </button>
                    </div>
                  )}
                </div>

                {/* Content */}
                <div className="p-6">
                  {!hasText ? (
                    <div className="space-y-3">
                      <div className="h-4 rounded skeleton-shimmer w-full"></div>
                      <div className="h-4 rounded skeleton-shimmer w-[90%]"></div>
                      <div className="h-4 rounded skeleton-shimmer w-[60%]"></div>
                    </div>
                  ) : editingId === sec.id ? (
                    <div>
                      <textarea 
                        value={editValue}
                        onChange={(e) => setEditValue(e.target.value)}
                        className="w-full bg-brand-bg border border-brand-primary/50 rounded-lg p-4 text-brand-body leading-relaxed min-h-[150px] focus:outline-none focus:ring-1 focus:ring-brand-primary resize-y"
                      />
                      <div className="flex justify-between items-center mt-4">
                        <span className="text-xs text-brand-muted font-mono">{editValue.length} chars</span>
                        <div className="flex gap-2">
                          <button onClick={() => setEditingId(null)} className="text-sm px-4 py-2 text-brand-muted hover:text-brand-body">Cancel</button>
                          <button onClick={() => handleSave(sec.id)} className="text-sm px-4 py-2 bg-brand-primary text-white rounded hover:bg-brand-primary/90">Save Draft</button>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div>
                      <p className="text-brand-body leading-[1.8] whitespace-pre-wrap">
                        {sec.text}
                        {isCurrentlyStreaming && <span className="inline-block w-2 h-4 bg-brand-primary ml-1 animate-blink align-middle"></span>}
                      </p>
                      
                      <div className="mt-6 pt-4 border-t border-brand-border/50">
                        <p className="text-xs text-brand-muted italic">
                          Source: {sec.source} · Confidence: 87%
                        </p>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {toast && <Toast message={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
