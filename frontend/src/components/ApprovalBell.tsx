import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Archive,
  Bell,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  Clock,
  Mail,
  Trash2,
  XCircle,
} from 'lucide-react';
import { approveAction, denyAction, fetchPendingApprovals } from '../lib/api';
import type { PendingApproval } from '../lib/api';

const TIER_STYLES: Record<string, { label: string; color: string; bg: string }> = {
  trivial: {
    label: 'Trivial',
    color: 'var(--color-text-secondary)',
    bg: 'color-mix(in srgb, var(--color-text-secondary) 10%, transparent)',
  },
  low: {
    label: 'Baixo',
    color: '#3b82f6',
    bg: 'rgba(59,130,246,0.12)',
  },
  medium: {
    label: 'Médio',
    color: 'var(--color-warning)',
    bg: 'color-mix(in srgb, var(--color-warning) 12%, transparent)',
  },
  high: {
    label: 'Alto',
    color: 'var(--color-error)',
    bg: 'color-mix(in srgb, var(--color-error) 12%, transparent)',
  },
};

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return 'agora';
  if (m < 60) return 'há ' + m + ' min';
  const h = Math.floor(m / 60);
  if (h < 24) return 'há ' + h + ' h';
  const d = Math.floor(h / 24);
  return d === 1 ? 'há 1 dia' : 'há ' + d + ' dias';
}

function payloadText(action: PendingApproval, ...keys: string[]): string {
  for (const key of keys) {
    const value = action.payload?.[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function humanActionType(actionType: string): string {
  const labels: Record<string, string> = {
    email_archive: 'Arquivar e-mail',
    email_delete: 'Mover e-mail para a lixeira',
  };
  return labels[actionType] ?? actionType.split('_').join(' ');
}

function EmailApprovalSummary({ action }: { action: PendingApproval }) {
  const sender = payloadText(action, 'sender', 'de');
  const subject = payloadText(action, 'subject', 'assunto');
  const snippet = payloadText(action, 'snippet');
  const isArchive = action.action_type === 'email_archive';
  const Icon = isArchive ? Archive : Trash2;

  return (
    <div
      className="rounded-lg p-3 mb-3"
      style={{
        background: 'var(--color-bg-tertiary)',
        border: '1px solid var(--color-border)',
      }}
    >
      <div className="flex items-start gap-2.5">
        <div
          className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
          style={{
            background: 'color-mix(in srgb, var(--color-accent) 10%, transparent)',
            color: 'var(--color-accent)',
          }}
        >
          <Mail size={15} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 mb-1">
            <Icon size={12} style={{ color: 'var(--color-text-secondary)' }} />
            <span
              className="text-[11px] font-semibold uppercase tracking-wide"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {isArchive ? 'Arquivamento solicitado' : 'Envio à lixeira solicitado'}
            </span>
          </div>

          {sender && (
            <div className="text-xs mb-1" style={{ color: 'var(--color-text-secondary)' }}>
              <span className="font-medium" style={{ color: 'var(--color-text)' }}>
                De:
              </span>{' '}
              {sender}
            </div>
          )}

          <div
            className="text-[13px] font-medium leading-snug"
            style={{ color: 'var(--color-text)' }}
          >
            {subject || 'E-mail sem assunto disponível'}
          </div>

          {snippet && (
            <p
              className="text-[11px] mt-1.5 leading-relaxed line-clamp-3"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {snippet}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

export function ApprovalBell() {
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [processing, setProcessing] = useState<Record<string, boolean>>({});
  const containerRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      setApprovals(await fetchPendingApprovals());
    } catch {
      // O backend pode ainda não estar disponível.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [load]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const handleApprove = async (id: string) => {
    setProcessing((p) => ({ ...p, [id]: true }));
    try {
      await approveAction(id);
      setApprovals((prev) => prev.filter((a) => a.id !== id));
    } finally {
      setProcessing((p) => ({ ...p, [id]: false }));
    }
  };

  const handleDeny = async (id: string) => {
    setProcessing((p) => ({ ...p, [id]: true }));
    try {
      await denyAction(id);
      setApprovals((prev) => prev.filter((a) => a.id !== id));
    } finally {
      setProcessing((p) => ({ ...p, [id]: false }));
    }
  };

  const count = approvals.length;
  const pendingLabel = count === 1 ? '1 pendente' : String(count) + ' pendentes';

  return (
    <div ref={containerRef} className="fixed top-2 right-3 z-40">
      <button
        onClick={() => setOpen((o) => !o)}
        className="relative p-2 rounded-lg transition-colors cursor-pointer"
        title="Aprovações do agente"
        aria-label="Aprovações do agente"
        style={{
          color: count > 0 ? 'var(--color-text)' : 'var(--color-text-secondary)',
          background: open
            ? 'var(--color-bg-tertiary)'
            : count > 0
              ? 'color-mix(in srgb, var(--color-error) 8%, transparent)'
              : 'transparent',
        }}
      >
        <Bell size={17} />
        {count > 0 && (
          <span
            className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 flex items-center justify-center rounded-full text-[10px] font-bold px-1 leading-none"
            style={{ background: 'var(--color-error)', color: '#fff' }}
          >
            {count > 99 ? '99+' : count}
          </span>
        )}
      </button>

      {open && (
        <div
          className="absolute right-0 top-full mt-1 rounded-xl shadow-2xl overflow-hidden flex flex-col"
          style={{
            width: 'min(430px, calc(100vw - 24px))',
            maxHeight: '620px',
            background: 'var(--color-bg-secondary)',
            border: '1px solid var(--color-border)',
          }}
        >
          <div
            className="flex items-center justify-between px-4 py-3 shrink-0"
            style={{ borderBottom: '1px solid var(--color-border)' }}
          >
            <div className="flex items-center gap-2">
              <Bell size={13} style={{ color: 'var(--color-accent)' }} />
              <span className="text-sm font-semibold" style={{ color: 'var(--color-text)' }}>
                Aprovações do agente
              </span>
            </div>
            {count > 0 && (
              <span
                className="text-[11px] font-medium px-2 py-0.5 rounded-full"
                style={{
                  background: 'color-mix(in srgb, var(--color-error) 12%, transparent)',
                  color: 'var(--color-error)',
                }}
              >
                {pendingLabel}
              </span>
            )}
          </div>

          <div className="overflow-y-auto flex-1">
            {count === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 gap-2">
                <CheckCircle
                  size={26}
                  style={{ color: 'var(--color-text-secondary)', opacity: 0.35 }}
                />
                <span className="text-sm" style={{ color: 'var(--color-text-secondary)' }}>
                  Nenhuma aprovação pendente
                </span>
              </div>
            ) : (
              approvals.map((action, idx) => {
                const tier = TIER_STYLES[action.tier] ?? TIER_STYLES.medium;
                const isExpanded = !!expanded[action.id];
                const isLoading = !!processing[action.id];
                const isEmail =
                  action.action_type === 'email_archive' || action.action_type === 'email_delete';

                const technicalPayload = {
                  id_da_acao: action.id,
                  tipo_interno: action.action_type,
                  ...action.payload,
                };

                return (
                  <div
                    key={action.id}
                    className="px-4 py-3"
                    style={{
                      borderBottom:
                        idx < count - 1 ? '1px solid var(--color-border)' : 'none',
                    }}
                  >
                    <div className="flex items-center justify-between mb-2">
                      <span
                        className="text-[12px] font-semibold"
                        style={{ color: 'var(--color-accent)' }}
                      >
                        {humanActionType(action.action_type)}
                      </span>
                      <div className="flex items-center gap-2">
                        <span
                          className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded"
                          style={{ background: tier.bg, color: tier.color }}
                          title="Nível de risco"
                        >
                          {tier.label}
                        </span>
                        <span
                          className="text-[10px] flex items-center gap-0.5"
                          style={{ color: 'var(--color-text-secondary)' }}
                        >
                          <Clock size={9} />
                          {timeAgo(action.created_at)}
                        </span>
                      </div>
                    </div>

                    {isEmail ? (
                      <EmailApprovalSummary action={action} />
                    ) : (
                      <p
                        className="text-[13px] mb-2.5 leading-snug"
                        style={{ color: 'var(--color-text)' }}
                      >
                        {action.description}
                      </p>
                    )}

                    {isEmail && action.description && (
                      <p
                        className="text-[11px] mb-2.5 leading-relaxed"
                        style={{ color: 'var(--color-text-secondary)' }}
                      >
                        Motivo informado pelo agente: {action.description}
                      </p>
                    )}

                    <button
                      className="flex items-center gap-1 text-[11px] mb-2 cursor-pointer"
                      style={{ color: 'var(--color-text-secondary)' }}
                      onClick={() =>
                        setExpanded((e) => ({ ...e, [action.id]: !e[action.id] }))
                      }
                    >
                      {isExpanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
                      {isExpanded ? 'Ocultar detalhes técnicos' : 'Mostrar detalhes técnicos'}
                    </button>

                    {isExpanded && (
                      <pre
                        className="text-[10px] rounded-lg p-2.5 mb-2.5 overflow-x-auto"
                        style={{
                          background: 'var(--color-bg-tertiary)',
                          color: 'var(--color-text-secondary)',
                          fontFamily: 'monospace',
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                          lineHeight: '1.5',
                        }}
                      >
                        {JSON.stringify(technicalPayload, null, 2)}
                      </pre>
                    )}

                    <div className="flex gap-2">
                      <button
                        onClick={() => handleApprove(action.id)}
                        disabled={isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-semibold transition-opacity cursor-pointer disabled:opacity-40"
                        style={{
                          background:
                            'color-mix(in srgb, var(--color-success) 12%, transparent)',
                          color: 'var(--color-success)',
                          border:
                            '1px solid color-mix(in srgb, var(--color-success) 22%, transparent)',
                        }}
                      >
                        <CheckCircle size={12} />
                        Aprovar
                      </button>
                      <button
                        onClick={() => handleDeny(action.id)}
                        disabled={isLoading}
                        className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-semibold transition-opacity cursor-pointer disabled:opacity-40"
                        style={{
                          background:
                            'color-mix(in srgb, var(--color-error) 12%, transparent)',
                          color: 'var(--color-error)',
                          border:
                            '1px solid color-mix(in srgb, var(--color-error) 22%, transparent)',
                        }}
                      >
                        <XCircle size={12} />
                        Negar
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
