import { CheckCircle2, Info, XCircle } from 'lucide-react';

export default function Toast({ message, type = 'success', onClose }) {
  const icons = {
    success: <CheckCircle2 className="w-5 h-5 text-brand-success" />,
    info: <Info className="w-5 h-5 text-brand-primary" />,
    error: <XCircle className="w-5 h-5 text-brand-danger" />
  };

  const borders = {
    success: 'border-brand-success/30',
    info: 'border-brand-primary/30',
    error: 'border-brand-danger/30'
  };

  return (
    <div className={`fixed bottom-6 right-6 flex items-center gap-3 bg-brand-surface border ${borders[type]} shadow-lg shadow-black/50 px-4 py-3 rounded-lg animate-page-mount z-50`}>
      {icons[type]}
      <span className="text-sm font-medium text-brand-heading">{message}</span>
      <button onClick={onClose} className="ml-2 text-brand-muted hover:text-brand-body">
        &times;
      </button>
    </div>
  );
}
