import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { UploadCloud, X, CheckCircle2, Loader2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

export default function UploadModal({ onClose }) {
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [file, setFile] = useState(null);
  const [isDragging, setIsDragging] = useState(false);
  const [nameError, setNameError] = useState('');
  
  // States: idle -> uploading -> parsing -> matching -> scoring -> complete
  const [step, setStep] = useState('idle');
  const [error, setError] = useState('');

  // Lock page scroll while the modal is open
  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, []);

  // Escape closes — but never mid-pipeline
  useEffect(() => {
    const onKeyDown = (e) => {
      if (e.key === 'Escape' && step === 'idle') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [step, onClose]);

  const handleDragOver = (e) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      setFile(e.dataTransfer.files[0]);
    }
  };

  const normalizeErrorValue = (value) => {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    if (Array.isArray(value)) {
      return value
        .map(normalizeErrorValue)
        .filter(Boolean)
        .join(', ');
    }
    if (typeof value === 'object') {
      if (typeof value.detail !== 'undefined') return normalizeErrorValue(value.detail);
      if (typeof value.message !== 'undefined') return normalizeErrorValue(value.message);
      try {
        return JSON.stringify(value);
      } catch {
        return String(value);
      }
    }
    return String(value);
  };

  const readErrorDetail = async (res, fallback) => {
    try {
      const data = await res.json();
      const detail = normalizeErrorValue(data?.detail);
      if (detail) return detail;
      const message = normalizeErrorValue(data?.message);
      if (message) return message;
      const normalized = normalizeErrorValue(data);
      if (normalized) return normalized;
    } catch {
      try {
        const text = await res.text();
        if (text) return text;
      } catch {
        // ignore parsing failures and use fallback
      }
    }
    return fallback;
  };

  const startAnalysis = async () => {
    const trimmedName = name.trim();
    if (trimmedName.length < 2) {
      setNameError('Bid name must be at least 2 characters long.');
      return;
    }
    if (!file) return;
    setError('');
    setNameError('');
    
    let wsId = '';

    try {
      // Step 1: Upload
      setStep('uploading');
      const formData = new FormData();
      formData.append('name', name);
      formData.append('file', file);
      
      const uploadPromise = fetch('/api/workspaces', { method: 'POST', body: formData });
      const [uploadRes] = await Promise.all([uploadPromise, new Promise(r => setTimeout(r, 800))]);
      if (!uploadRes.ok) throw new Error(await readErrorDetail(uploadRes, 'Upload failed'));
      const uploadData = await uploadRes.json();
      wsId = uploadData.id;

      // Step 2: Parse
      setStep('parsing');
      const parsePromise = fetch(`/api/workspaces/${wsId}/parse`, { method: 'POST' });
      const [parseRes] = await Promise.all([parsePromise, new Promise(r => setTimeout(r, 2000))]);
      if (!parseRes.ok) throw new Error(await readErrorDetail(parseRes, 'Parse failed'));

      // Step 3: Match
      setStep('matching');
      const matchPromise = fetch(`/api/workspaces/${wsId}/match`, { method: 'POST' });
      const [matchRes] = await Promise.all([matchPromise, new Promise(r => setTimeout(r, 1500))]);
      if (!matchRes.ok) throw new Error(await readErrorDetail(matchRes, 'Match failed'));

      // Step 4: Score
      setStep('scoring');
      const scorePromise = fetch(`/api/workspaces/${wsId}/score`, { method: 'POST' });
      const [scoreRes] = await Promise.all([scorePromise, new Promise(r => setTimeout(r, 1000))]);
      if (!scoreRes.ok) throw new Error(await readErrorDetail(scoreRes, 'Score failed'));

      setStep('complete');
      
      // Navigate to workspace view after a brief pause
      setTimeout(() => {
        onClose();
        navigate(`/workspace/${wsId}`);
      }, 800);
      
    } catch (err) {
      console.error('API Error in pipeline:', err);
      setError(normalizeErrorValue(err?.message) || normalizeErrorValue(err) || 'Analysis failed. Please check your connection and try again.');
      setStep('idle');
    }
  };

  const steps = [
    { id: 'uploading', label: 'Upload' },
    { id: 'parsing', label: 'Parse' },
    { id: 'matching', label: 'Match' },
    { id: 'scoring', label: 'Score' }
  ];

  const getStepStatus = (stepId) => {
    const currentIndex = steps.findIndex(s => s.id === step);
    const targetIndex = steps.findIndex(s => s.id === stepId);
    if (step === 'complete' || currentIndex > targetIndex) return 'completed';
    if (currentIndex === targetIndex) return 'active';
    return 'pending';
  };

  // Rendered through a portal: ancestor pages animate with CSS transforms,
  // and a transformed ancestor re-anchors position:fixed — which used to pin
  // this modal, half cut off, to the bottom of the scrolled page.
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm animate-backdrop"
        onClick={step === 'idle' ? onClose : undefined}
      ></div>

      {/* Modal Card */}
      <div className="relative w-full max-w-[560px] max-h-[90vh] overflow-y-auto bg-brand-surface rounded-xl shadow-2xl border border-brand-border flex flex-col animate-modal-pop">
        <div className="signature-border absolute inset-x-0 top-0 h-0"></div>
        
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-brand-border">
          <h2 className="text-xl font-bold text-brand-heading">Analyze a New RFP</h2>
          {step === 'idle' && (
            <button onClick={onClose} className="text-brand-muted hover:text-brand-heading transition-colors">
              <X className="w-5 h-5" />
            </button>
          )}
        </div>

        {/* Content */}
        <div className="p-6">
          {error && (
            <div className="mb-4 bg-brand-danger/10 border border-brand-danger/20 rounded-lg p-3 text-sm text-brand-danger font-medium flex items-center justify-center">
              {error}
            </div>
          )}
          
          <div className="mb-6">
            <label className="block text-sm font-medium text-brand-muted mb-2">Bid Name</label>
            <input 
              type="text" 
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                if (nameError) setNameError('');
              }}
              placeholder="e.g. Ministry of IT — Cloud Services RFP 2025"
              className="w-full bg-brand-bg border border-brand-border rounded-lg px-4 py-2.5 text-brand-body focus:outline-none focus:border-brand-primary focus:ring-1 focus:ring-brand-primary transition-all"
              disabled={step !== 'idle'}
            />
            {nameError && (
              <p className="mt-2 text-xs text-brand-danger font-medium">{nameError}</p>
            )}
          </div>

          <div 
            className={`border-2 border-dashed rounded-xl p-8 flex flex-col items-center justify-center text-center transition-all ${
              isDragging 
                ? 'border-brand-primary bg-brand-primary/10' 
                : 'border-brand-border hover:border-brand-muted bg-brand-bg/60'
            }`}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
            {file ? (
              <>
                <CheckCircle2 className="w-12 h-12 text-brand-success mb-3" />
                <p className="text-brand-heading font-medium text-lg">{file.name}</p>
                <p className="text-brand-muted text-sm mt-1">{(file.size / 1024 / 1024).toFixed(2)} MB</p>
                {step === 'idle' && (
                  <button onClick={() => setFile(null)} className="mt-4 text-xs text-brand-danger hover:underline">Remove file</button>
                )}
              </>
            ) : (
              <>
                <UploadCloud className={`w-12 h-12 mb-4 ${isDragging ? 'text-brand-primary' : 'text-brand-muted'}`} />
                <p className="text-lg font-medium text-brand-heading mb-1">
                  {isDragging ? 'Release to upload' : 'Drop your RFP here'}
                </p>
                <p className="text-brand-muted text-sm mb-4">PDF or DOCX up to 50MB</p>
                <label className="text-brand-primary text-sm font-medium hover:underline cursor-pointer">
                  or browse files
                  <input type="file" className="hidden" accept=".pdf,.docx" onChange={(e) => {
                    if (e.target.files?.length) setFile(e.target.files[0]);
                  }} />
                </label>
              </>
            )}
          </div>

          {/* Progress Pipeline */}
          {step !== 'idle' && (
            <div className="mt-8 mb-4">
              <div className="flex items-center justify-between relative">
                <div className="absolute left-0 top-1/2 -translate-y-1/2 w-full h-0.5 bg-brand-border -z-10"></div>
                {steps.map((s, i) => {
                  const status = getStepStatus(s.id);
                  return (
                    <div key={s.id} className="flex flex-col items-center bg-brand-surface px-2">
                      <div className={`w-6 h-6 rounded-full flex items-center justify-center border-2 mb-2 ${
                        status === 'completed' ? 'bg-brand-success border-brand-success text-white' :
                        status === 'active' ? 'bg-brand-primary border-brand-primary text-white shadow-[0_0_10px_rgba(59,130,246,0.5)] animate-pulse' :
                        'bg-brand-surface border-brand-border text-brand-muted'
                      }`}>
                        {status === 'completed' ? <CheckCircle2 className="w-3.5 h-3.5" /> : <div className="w-1.5 h-1.5 rounded-full bg-current"></div>}
                      </div>
                      <span className={`text-xs font-medium ${status === 'active' ? 'text-brand-primary' : 'text-brand-muted'}`}>
                        {s.label}
                      </span>
                    </div>
                  );
                })}
              </div>
              <p className="text-center text-sm text-brand-muted mt-6 animate-pulse">
                {step === 'uploading' && 'Uploading document to secure vault...'}
                {step === 'parsing' && 'Extracting requirements and clauses...'}
                {step === 'matching' && 'Matching capabilities via Vector Search...'}
                {step === 'scoring' && 'Running win-probability heuristics...'}
                {step === 'complete' && 'Analysis complete! Opening workspace...'}
              </p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-6 border-t border-brand-border bg-brand-bg">
          <button
            onClick={startAnalysis}
            disabled={name.trim().length < 2 || !file || step !== 'idle'}
            className="w-full flex justify-center items-center gap-2 bg-brand-primary hover:bg-brand-primary/90 disabled:bg-brand-border disabled:text-brand-muted text-white py-3 rounded-lg font-medium transition-all"
          >
            {step !== 'idle' && step !== 'complete' ? (
              <><Loader2 className="w-5 h-5 animate-spin" /> Analyzing...</>
            ) : (
              'Start Analysis'
            )}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
