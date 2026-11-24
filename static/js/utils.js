/* CursorRouter v3.0 — Shared Safe Utilities & API Helpers */
(function (global) {
  'use strict';

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatSpend(rawSpend, rawThreshold) {
    const spendCents = Number(rawSpend || 0);
    const threshCents = Number(rawThreshold || 200);
    const spendUsd = (spendCents / 100).toFixed(2);
    const threshUsd = (threshCents / 100).toFixed(2);
    return `$${spendUsd} / $${threshUsd}`;
  }

  function getQuotaTier(account) {
    const pct = Number((account && (account.usage_percent ?? account.usagePercent)) || 0);
    const status = (account && account.status) || 'READY';
    const nowSec = Date.now() / 1000;
    const inCd = Boolean(account && account.cooldown_until && Number(account.cooldown_until) > nowSec);

    if (status === 'EXPIRED') {
      return { key: 'expired', label: 'EXPIRED', color: 'muted', barClass: 'fill-rose', textClass: 'text-muted', pillClass: 'pill' };
    }
    if (inCd || status === 'EXHAUSTED' || pct >= 100) {
      const rem = inCd ? formatCooldown(Number(account.cooldown_until) - nowSec) : '≥100%';
      return {
        key: 'cooldown',
        label: inCd ? `Cooldown ${rem}` : 'Hết Quota (≥100%)',
        color: 'rose',
        barClass: 'fill-rose',
        textClass: 'text-rose',
        pillClass: 'pill pill-rose',
      };
    }
    if (status === 'PENDING') {
      return { key: 'pending', label: 'Pending', color: 'muted', barClass: 'fill-emerald', textClass: 'text-secondary', pillClass: 'pill' };
    }
    if (pct >= 50) {
      return { key: 'tier2', label: 'Tier 2 (50-99%)', color: 'amber', barClass: 'fill-amber', textClass: 'text-amber', pillClass: 'pill pill-amber' };
    }
    return { key: 'tier1', label: 'Tier 1 (<50%)', color: 'emerald', barClass: 'fill-emerald', textClass: 'text-emerald', pillClass: 'pill pill-emerald' };
  }

  function formatCooldown(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds || 0)));
    if (s <= 0) return '0m';
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${Math.max(1, m)}m`;
  }

  function formatUptime(sec) {
    const s = Math.max(0, Math.floor(Number(sec || 0)));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const rem = s % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${rem}s`;
    return `${rem}s`;
  }

  function maskEmail(email) {
    if (!email || typeof email !== 'string') return '—';
    const at = email.indexOf('@');
    if (at <= 1) return email;
    const local = email.slice(0, at);
    const domain = email.slice(at);
    const visible = local.slice(0, Math.min(3, Math.max(1, local.length - 2)));
    return `${visible}***${domain}`;
  }

  async function copyToClipboard(text, successMsg) {
    if (!text) return false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      if (successMsg) showToast(successMsg, 'success');
      return true;
    } catch (err) {
      showToast('Không thể sao chép vào clipboard', 'error');
      return false;
    }
  }

  let _toastTimer = null;
  function showToast(message, type) {
    const toast = document.getElementById('toast-bar');
    const txt = document.getElementById('toast-text');
    const dot = document.getElementById('toast-dot');
    if (!toast || !txt) return;

    txt.textContent = message;
    if (dot) {
      let cls = 'status-dot dot-emerald';
      if (type === 'error') cls = 'status-dot dot-rose';
      else if (type === 'warn' || type === 'warning') cls = 'status-dot dot-amber';
      else if (type === 'loading' || type === 'info') cls = 'status-dot dot-emerald pulse';
      dot.className = cls;
    }

    toast.classList.remove('is-hidden');
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => {
      toast.classList.add('is-hidden');
    }, type === 'loading' ? 5000 : 3400);
  }

  async function apiFetch(url, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    if (opts.json !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    const res = await fetch(url, opts);
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = { success: res.ok, error: `HTTP ${res.status}` };
    }
    return { ok: res.ok, status: res.status, data };
  }

  function previewPastedCookies(text) {
    const trimmed = (text || '').trim();
    if (!trimmed) return { count: 0, format: 'Trống' };
    if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
      try {
        const parsed = JSON.parse(trimmed);
        const list = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.cookies) ? parsed.cookies : [parsed]);
        const hits = list.filter(x => x && (x.name === 'WorkosCursorSessionToken' || x.WorkosCursorSessionToken));
        return { count: hits.length, format: 'JSON Cookie' };
      } catch (_) {
        return { count: 0, format: 'JSON chưa hợp lệ' };
      }
    }
    const lines = trimmed.split(/\r?\n/).map(l => l.trim()).filter(l => l && !l.startsWith('#'));
    const netscapeLines = lines.filter(l => {
      const parts = l.includes('\t') ? l.split('\t') : l.split(/\s+/);
      return parts.length >= 7 && parts[5].trim() === 'WorkosCursorSessionToken';
    });
    if (netscapeLines.length > 0) {
      const uniq = new Set(netscapeLines.map(l => {
        const parts = l.includes('\t') ? l.split('\t') : l.split(/\s+/);
        return parts[6].trim();
      }));
      return { count: uniq.size, format: 'Netscape Cookie' };
    }
    let count = 0;
    const formats = new Set();
    for (const line of lines) {
      if (line.includes('WorkosCursorSessionToken=')) {
        count++;
        formats.add('Header');
      } else if (line.includes('|') && line.split('|')[0].includes('@')) {
        count++;
        formats.add('Email|Cookie');
      } else if (line.startsWith('eyJ') && line.split('.').length === 3) {
        count++;
        formats.add('JWT Token');
      } else if (line.startsWith('user_') || line.length >= 32) {
        count++;
        formats.add('Raw Token');
      }
    }
    return { count, format: formats.size ? Array.from(formats).join(' + ') : 'Tự động phân tích trên server' };
  }

  global.CRUtils = {
    escapeHtml,
    formatSpend,
    getQuotaTier,
    formatCooldown,
    formatUptime,
    maskEmail,
    copyToClipboard,
    showToast,
    apiFetch,
    previewPastedCookies,
  };
  global.escapeHtml = escapeHtml;
  global.formatSpend = formatSpend;
  global.maskEmail = maskEmail;
  global.copyToClipboard = copyToClipboard;
  global.showToast = showToast;
})(window);
