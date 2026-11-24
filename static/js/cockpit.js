/* CursorRouter v3.0 — Cockpit State, Pool Explorer, Cookie Workbench & Telemetry */
(function (global) {
  'use strict';

  const U = global.CRUtils;

  const state = {
    allAccounts: [],
    statusFilter: 'ALL',
    searchQuery: '',
    currentPage: 1,
    pageSize: 50,
    viewMode: localStorage.getItem('cursor_view_mode') || 'table',
    globalMaskEmails: localStorage.getItem('cursor_mask_emails') === 'true',
    unmaskedIds: new Set(),
    lastVersion: 0,
    previousActiveEmail: null,
    previousCursorRunning: null,
    offlineNotificationDismissed: false,
    startupBannerDismissed: false,
    batchPollTimer: null,
    convertPollTimer: null,
    sseConnected: false,
    workbenchCollapsed: localStorage.getItem('cursor_workbench_collapsed') === 'true',
  };

  global.CRState = state;

  /* ---------- SECTION NAVIGATION ---------- */
  function navigateSection(sectionId) {
    document.querySelectorAll('.rail-nav-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.nav === sectionId);
    });
    const el = document.getElementById(sectionId);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  /* ---------- WORKBENCH COLLAPSE & TABS ---------- */
  function toggleWorkbench(forceState) {
    state.workbenchCollapsed = typeof forceState === 'boolean' ? forceState : !state.workbenchCollapsed;
    localStorage.setItem('cursor_workbench_collapsed', String(state.workbenchCollapsed));
    const body = document.getElementById('cookie-workbench-body');
    const chev = document.getElementById('workbench-chevron');
    if (body) body.classList.toggle('hidden', state.workbenchCollapsed);
    if (chev) chev.style.transform = state.workbenchCollapsed ? 'rotate(-90deg)' : 'rotate(0deg)';
  }

  function switchIngestTab(tab) {
    const isPaste = tab === 'paste';
    const pastePane = document.getElementById('ingest-pane-paste');
    const dropPane = document.getElementById('ingest-pane-drop');
    const btnPaste = document.getElementById('ingest-tab-paste');
    const btnDrop = document.getElementById('ingest-tab-drop');
    if (pastePane) pastePane.classList.toggle('hidden', !isPaste);
    if (dropPane) dropPane.classList.toggle('hidden', isPaste);
    if (btnPaste) btnPaste.classList.toggle('active', isPaste);
    if (btnDrop) btnDrop.classList.toggle('active', !isPaste);
  }

  function onPasteInputChanged() {
    const ta = document.getElementById('cookie-paste-input');
    const badge = document.getElementById('paste-detect-badge');
    const btn = document.getElementById('btn-submit-paste');
    if (!ta || !badge) return;
    const info = U.previewPastedCookies(ta.value);
    if (info.count > 0) {
      badge.textContent = `Phát hiện ${info.count} cookie (${info.format})`;
      badge.className = 'pill pill-emerald';
      if (btn) btn.disabled = false;
    } else if (ta.value.trim().length > 0) {
      badge.textContent = info.format;
      badge.className = 'pill pill-amber';
      if (btn) btn.disabled = false;
    } else {
      badge.textContent = 'Sẵn sàng dán (Raw / Netscape / JSON / JWT)';
      badge.className = 'pill';
      if (btn) btn.disabled = true;
    }
  }

  function _revealNewlyImportedAccounts() {
    state.statusFilter = 'ALL';
    state.searchQuery = '';
    state.currentPage = 1;
    const searchInp = document.getElementById('search-input');
    if (searchInp) searchInp.value = '';
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.status === 'ALL'));
    document.querySelectorAll('.kpi-card').forEach(c => c.classList.toggle('active-filter', c.dataset.filter === 'ALL'));
  }

  async function submitPastedCookies() {
    const ta = document.getElementById('cookie-paste-input');
    const btn = document.getElementById('btn-submit-paste');
    if (!ta || !ta.value.trim()) {
      U.showToast('Vui lòng dán ít nhất 1 cookie', 'warn');
      return;
    }
    if (btn) btn.disabled = true;
    U.showToast('Đang nạp & đồng bộ cookie qua HTTP/2...', 'loading');
    try {
      const { ok, data } = await U.apiFetch('/api/cookies/paste', {
        method: 'POST',
        json: { content: ta.value, auto_convert: true },
      });
      if (ok && data && data.success) {
        U.showToast(data.message || `Đã nạp ${data.parsed} cookie!`, 'success');
        ta.value = '';
        onPasteInputChanged();
        _revealNewlyImportedAccounts();
        startConvertProgressPolling();
        await Promise.allSettled([loadCookiesInfo(), loadAccounts()]);
      } else {
        U.showToast('Lỗi: ' + ((data && data.error) || 'Không thể nạp cookie'), 'error');
      }
    } catch (err) {
      U.showToast('Lỗi kết nối server khi dán cookie', 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  let _pkcePollTimer = null;
  let _pkceSessionId = null;
  let _pkceLoginUrl = '';

  async function startInteractiveOAuthLogin(openBrowser) {
    cancelInteractiveOAuthLogin(true);
    U.showToast('Đang tạo link đăng nhập Cursor OAuth PKCE chính thức...', 'loading');
    try {
      const { ok, data } = await U.apiFetch('/api/auth/pkce/start', {
        method: 'POST',
        json: { open_browser: openBrowser !== false },
      });
      if (!ok || !data || !data.success) {
        U.showToast((data && data.error) || 'Không thể khởi tạo OAuth PKCE', 'error');
        return;
      }
      _pkceSessionId = data.session_id;
      _pkceLoginUrl = data.login_url;

      const box = document.getElementById('oauth-pkce-banner');
      const urlEl = document.getElementById('oauth-pkce-url');
      const stEl = document.getElementById('oauth-pkce-status');
      if (urlEl) urlEl.value = _pkceLoginUrl;
      if (stEl) stEl.textContent = 'Đang chờ bạn xác nhận đăng nhập trên trình duyệt (tự động nhận IDE Session 60 ngày)...';
      if (box) box.classList.remove('hidden');

      U.showToast('Đã tạo phiên OAuth! Hãy bấm "Yes, Log In" trên trình duyệt hoặc copy link mở tab Ẩn danh.', 'info');

      _pkcePollTimer = setInterval(async () => {
        if (!_pkceSessionId) return;
        const r = await U.apiFetch(`/api/auth/pkce/status/${encodeURIComponent(_pkceSessionId)}`);
        if (!r.ok || !r.data) return;
        const st = r.data.status;
        if (st === 'completed') {
          clearInterval(_pkcePollTimer);
          _pkcePollTimer = null;
          _pkceSessionId = null;
          if (box) box.classList.add('hidden');
          const acc = r.data.account || {};
          U.showToast(`Đã thêm IDE Session thành công: ${acc.email || 'OK'}!`, 'success');
          _revealNewlyImportedAccounts();
          await Promise.allSettled([loadAccounts(), loadCookiesInfo()]);
        } else if (st === 'error' || st === 'expired' || st === 'cancelled') {
          clearInterval(_pkcePollTimer);
          _pkcePollTimer = null;
          _pkceSessionId = null;
          if (stEl) stEl.textContent = r.data.error || 'Phiên OAuth đã kết thúc.';
          if (st !== 'cancelled') U.showToast(r.data.error || 'Phiên OAuth hết hạn', 'warn');
        }
      }, 1600);
    } catch (_) {
      U.showToast('Lỗi kết nối khi khởi tạo OAuth PKCE', 'error');
    }
  }

  function copyOAuthLoginUrl() {
    if (_pkceLoginUrl) {
      U.copyToClipboard(_pkceLoginUrl, 'Đã sao chép link OAuth! Dán vào cửa sổ Ẩn danh để đăng nhập tài khoản khác.');
    }
  }

  async function cancelInteractiveOAuthLogin(silent) {
    if (_pkcePollTimer) {
      clearInterval(_pkcePollTimer);
      _pkcePollTimer = null;
    }
    const sid = _pkceSessionId;
    _pkceSessionId = null;
    const box = document.getElementById('oauth-pkce-banner');
    if (box) box.classList.add('hidden');
    if (sid) {
      try { await U.apiFetch(`/api/auth/pkce/cancel/${encodeURIComponent(sid)}`, { method: 'POST' }); } catch (_) {}
      if (!silent) U.showToast('Đã hủy phiên chờ đăng nhập OAuth', 'info');
    }
  }

  async function captureActiveCursorAccount() {
    U.showToast('Đang đọc & giải mã phiên Cursor IDE hiện tại (DPAPI safeStorage)...', 'loading');
    try {
      const { ok, data } = await U.apiFetch('/api/accounts/capture-active', { method: 'POST' });
      if (ok && data && data.success) {
        U.showToast(data.message || `Đã lưu ${data.email} vào pool!`, 'success');
        _revealNewlyImportedAccounts();
        await Promise.allSettled([loadAccounts(), loadCookiesInfo(), global.loadActiveStatus()]);
      } else {
        U.showToast((data && data.error) || 'Không thể đọc phiên Cursor hiện tại', 'warn');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối khi chụp phiên Cursor', 'error');
    }
  }

  /* ---------- COOKIE FOLDER / DROPZONE / CONVERT ---------- */
  async function openCookieFolder() {
    try {
      const { ok, data } = await U.apiFetch('/api/cookies/open-folder', { method: 'POST' });
      if (ok && data && data.success) {
        U.showToast('Đã mở thư mục Cookies trong Explorer', 'success');
      } else {
        U.showToast('Không thể mở thư mục Cookies', 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối khi mở thư mục', 'error');
    }
  }

  function copyCookieFolderPath() {
    const el = document.getElementById('cookie-folder-path');
    if (el) U.copyToClipboard(el.textContent.trim(), 'Đã sao chép đường dẫn thư mục Cookies');
  }

  async function loadCookiesInfo() {
    try {
      const { ok, data } = await U.apiFetch('/api/cookies/info');
      if (!ok || !data || !data.success) return;
      const setTxt = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
      setTxt('cookie-folder-path', data.path || 'Cookies/');
      setTxt('cookie-files-count', data.count ?? 0);
      setTxt('cookie-total-accounts', data.total_accounts ?? 0);
      setTxt('cookie-valid-tokens', data.valid_tokens ?? 0);
      setTxt('cookie-tier1-count', `${data.tier1_count ?? 0} T1`);
      setTxt('cookie-tier2-count', `${data.tier2_count ?? 0} T2`);
      setTxt('cookie-exhausted-count', `${data.exhausted_count ?? 0} Cạn`);
      const archWrap = document.getElementById('cookie-archived-container');
      if (archWrap) {
        if ((data.archived_count || 0) > 0) {
          archWrap.classList.remove('hidden');
          setTxt('cookie-archived-count', data.archived_count);
        } else {
          archWrap.classList.add('hidden');
        }
      }
    } catch (_) {}
  }

  async function quarantineCorruptedCookies() {
    U.showToast('Đang quét & cách ly file cookie hỏng...', 'loading');
    try {
      const { ok, data } = await U.apiFetch('/api/cookies/quarantine', { method: 'POST' });
      if (ok && data && data.success) {
        U.showToast(data.message || `Đã cách ly ${data.quarantined || 0} file hỏng`, 'success');
        await Promise.allSettled([loadCookiesInfo(), loadAccounts()]);
      } else {
        U.showToast('Lỗi cách ly: ' + ((data && data.error) || ''), 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối server', 'error');
    }
  }

  function renderLiveChips(containerId, items) {
    const el = document.getElementById(containerId);
    if (!el || !Array.isArray(items) || items.length === 0) return;
    el.innerHTML = items.slice(-10).map(item => {
      const st = item.status || (item.ok ? 'READY' : (item.expired ? 'EXPIRED' : 'ERR'));
      const cls = st === 'READY' ? 'pill pill-emerald' : (st === 'HIGH_USAGE' ? 'pill pill-amber' : 'pill pill-rose');
      const msTxt = item.ms ? ` ${item.ms}ms` : '';
      return `<span class="${cls}">${U.escapeHtml(item.email)} · ${U.escapeHtml(st)}${msTxt}</span>`;
    }).join('');
  }

  function startConvertProgressPolling() {
    const wrap = document.getElementById('cookie-convert-progress');
    if (wrap) wrap.classList.remove('hidden');
    if (state.convertPollTimer) clearInterval(state.convertPollTimer);
    let ticks = 0;
    state.convertPollTimer = setInterval(async () => {
      ticks++;
      try {
        const { ok, data } = await U.apiFetch('/api/startup-sync/status');
        if (!ok || !data || !data.startup_sync) return;
        const ss = data.startup_sync;
        const total = ss.total || 1;
        const pct = Math.min(100, Math.round(((ss.current || 0) / total) * 100));
        const bar = document.getElementById('cookie-convert-bar');
        const ratio = document.getElementById('cookie-convert-ratio');
        const statusEl = document.getElementById('cookie-convert-status');
        if (bar) bar.style.width = `${pct}%`;
        if (ratio) ratio.textContent = `${ss.current || 0}/${ss.total || 0} (${pct}%)`;
        if (statusEl) {
          statusEl.textContent = ss.running
            ? `Đang đồng bộ (${ss.transport || 'HTTP/2'}): ${ss.current_account || '...'}`
            : `Hoàn tất trong ${ss.duration_seconds || 0}s · Ready: ${ss.ready_count || 0} · Expired: ${ss.expired_count || 0}`;
        }
        renderLiveChips('cookie-live-chips', ss.recent_results);
        if (!ss.running || ticks > 150) {
          clearInterval(state.convertPollTimer);
          state.convertPollTimer = null;
          await Promise.allSettled([loadAccounts(), loadCookiesInfo()]);
          setTimeout(() => { if (wrap) wrap.classList.add('hidden'); }, 4500);
        }
      } catch (_) {}
    }, 350);
  }

  async function convertCookiesToTokens() {
    U.showToast('Đang quét thư mục Cookies & chuyển đổi song song...', 'loading');
    try {
      const { ok, data } = await U.apiFetch('/api/cookies/convert', { method: 'POST' });
      if (ok && data && data.success) {
        U.showToast(data.message || 'Đang đồng bộ HTTP/2...', 'success');
        startConvertProgressPolling();
        await loadCookiesInfo();
      } else {
        U.showToast('Lỗi chuyển đổi: ' + ((data && data.error) || ''), 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối server', 'error');
    }
  }

  function handleCookieDragOver(event) {
    event.preventDefault();
    const dz = document.getElementById('cookie-drop-area');
    if (dz) dz.classList.add('drag-over');
  }

  function handleCookieDragLeave(event) {
    event.preventDefault();
    const dz = document.getElementById('cookie-drop-area');
    if (dz) dz.classList.remove('drag-over');
  }

  function handleCookieDrop(event) {
    event.preventDefault();
    const dz = document.getElementById('cookie-drop-area');
    if (dz) dz.classList.remove('drag-over');
    if (event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length > 0) {
      uploadCookieFiles(event.dataTransfer.files);
    }
  }

  function handleCookieFileSelect(event) {
    if (event.target && event.target.files && event.target.files.length > 0) {
      uploadCookieFiles(event.target.files);
      event.target.value = '';
    }
  }

  async function uploadCookieFiles(files) {
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    U.showToast(`Đang tải lên ${files.length} file cookie...`, 'loading');
    try {
      const res = await fetch('/api/cookies/upload', { method: 'POST', body: fd });
      const data = await res.json();
      if (res.ok && data.success) {
        U.showToast(data.message || 'Đã tải lên và bắt đầu đồng bộ!', 'success');
        _revealNewlyImportedAccounts();
        startConvertProgressPolling();
        await Promise.allSettled([loadCookiesInfo(), loadAccounts()]);
      } else {
        U.showToast('Lỗi tải lên: ' + (data.error || ''), 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối khi tải file', 'error');
    }
  }

  /* ---------- ACCOUNT POOL FILTERING, SORTING & RENDERING ---------- */
  function getFilteredAccounts() {
    const q = (state.searchQuery || '').trim().toLowerCase();
    const nowSec = Date.now() / 1000;

    const list = state.allAccounts.filter(a => {
      if (q) {
        const hay = `${a.email || ''} ${a.file_name || ''} ${a.display_name || ''} ${a.fingerprint_id || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      const tier = U.getQuotaTier(a).key;
      if (state.statusFilter === 'ALL') return true;
      if (state.statusFilter === 'TIER1') return tier === 'tier1';
      if (state.statusFilter === 'TIER2') return tier === 'tier2';
      if (state.statusFilter === 'READY') return tier === 'tier1' || tier === 'tier2';
      if (state.statusFilter === 'COOLDOWN' || state.statusFilter === 'HIGH') return tier === 'cooldown';
      if (state.statusFilter === 'EXPIRED') return tier === 'expired';
      if (state.statusFilter === 'PENDING') return tier === 'pending';
      return true;
    });

    // Strict sort: Active #1 -> Tier 1 -> Tier 2 -> Pending -> Cooldown -> Expired
    const rankOf = (a) => {
      if (a.is_active) return 0;
      const t = U.getQuotaTier(a).key;
      if (t === 'tier1') return 1;
      if (t === 'tier2') return 2;
      if (t === 'pending') return 3;
      if (t === 'cooldown') return 4;
      return 5;
    };
    list.sort((a, b) => {
      const ra = rankOf(a);
      const rb = rankOf(b);
      if (ra !== rb) return ra - rb;
      const ua = Number(a.usage_percent || 0);
      const ub = Number(b.usage_percent || 0);
      if (ua !== ub) return ua - ub;
      return (a.id || 0) - (b.id || 0);
    });
    return list;
  }

  function updateStats(serverStats) {
    let total, tier1, tier2, cooldown, ready, pending, expired;
    if (serverStats && typeof serverStats.total === 'number' && state.allAccounts.length === 0) {
      total = serverStats.total;
      tier1 = serverStats.tier1 || 0;
      tier2 = serverStats.tier2 || 0;
      cooldown = serverStats.blacklist || 0;
      ready = serverStats.ready || (tier1 + tier2);
      pending = serverStats.pending || 0;
      expired = serverStats.expired || 0;
    } else {
      total = state.allAccounts.length;
      tier1 = 0; tier2 = 0; cooldown = 0; pending = 0; expired = 0;
      for (const a of state.allAccounts) {
        const k = U.getQuotaTier(a).key;
        if (k === 'tier1') tier1++;
        else if (k === 'tier2') tier2++;
        else if (k === 'cooldown') cooldown++;
        else if (k === 'pending') pending++;
        else if (k === 'expired') expired++;
      }
      ready = tier1 + tier2;
    }

    const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    setTxt('stat-total', total);
    setTxt('stat-ready', tier1);
    setTxt('stat-tier2', tier2);
    setTxt('stat-high', cooldown);
    setTxt('stat-pending', pending);
    setTxt('account-count-badge', `${total} acc`);
    setTxt('rail-pool-count', total);

    setTxt('filter-count-all', total);
    setTxt('filter-count-tier1', tier1);
    setTxt('filter-count-tier2', tier2);
    setTxt('filter-count-cooldown', cooldown);
    setTxt('filter-count-ready', ready);
    setTxt('filter-count-high', cooldown);
    setTxt('filter-count-pending', pending);
    setTxt('filter-count-expired', expired);

    setTxt('cookie-tier1-count', `${tier1} T1`);
    setTxt('cookie-tier2-count', `${tier2} T2`);
    setTxt('cookie-exhausted-count', `${cooldown} Cạn`);

    // Auto-expand cookie workbench when pool is completely empty
    if (total === 0 && state.workbenchCollapsed) {
      toggleWorkbench(false);
    }
  }

  function getDisplayedEmail(acc) {
    const raw = acc.email || acc.file_name || `Account #${acc.id}`;
    const shouldMask = state.globalMaskEmails && !state.unmaskedIds.has(acc.id);
    return shouldMask ? U.maskEmail(raw) : raw;
  }

  function renderAccounts() {
    const tbody = document.getElementById('accounts-tbody');
    const cardsGrid = document.getElementById('accounts-cards-grid');
    const tableWrap = document.getElementById('table-view-container');

    const isCards = state.viewMode === 'cards';
    if (tableWrap) tableWrap.classList.toggle('hidden', isCards);
    if (cardsGrid) cardsGrid.classList.toggle('hidden', !isCards);

    const btnTbl = document.getElementById('btn-view-table');
    const btnCrd = document.getElementById('btn-view-cards');
    if (btnTbl) btnTbl.classList.toggle('active', !isCards);
    if (btnCrd) btnCrd.classList.toggle('active', isCards);

    const filtered = getFilteredAccounts();
    const totalCount = filtered.length;
    const totalPages = state.pageSize === 'all' ? 1 : Math.max(1, Math.ceil(totalCount / state.pageSize));
    if (state.currentPage > totalPages) state.currentPage = totalPages;
    if (state.currentPage < 1) state.currentPage = 1;

    let displayList = filtered;
    let startIndex = 0;
    if (state.pageSize !== 'all') {
      startIndex = (state.currentPage - 1) * state.pageSize;
      displayList = filtered.slice(startIndex, startIndex + state.pageSize);
    }

    const pageSummary = document.getElementById('page-summary');
    const pageIndicator = document.getElementById('page-indicator');
    const btnPrev = document.getElementById('btn-prev-page');
    const btnNext = document.getElementById('btn-next-page');
    if (pageSummary) {
      if (totalCount === 0) pageSummary.textContent = '0 tài khoản';
      else if (state.pageSize === 'all') pageSummary.textContent = `Hiển thị toàn bộ ${totalCount} tài khoản`;
      else pageSummary.textContent = `Hiển thị ${startIndex + 1} – ${Math.min(startIndex + state.pageSize, totalCount)} / ${totalCount} tài khoản`;
    }
    if (pageIndicator) pageIndicator.textContent = `Trang ${state.currentPage} / ${totalPages}`;
    if (btnPrev) btnPrev.disabled = state.currentPage <= 1;
    if (btnNext) btnNext.disabled = state.currentPage >= totalPages;

    if (displayList.length === 0) {
      const emptyMsg = state.allAccounts.length === 0
        ? 'Chưa có tài khoản nào trong pool. Dán cookie hoặc kéo thả file ở khung phía trên để bắt đầu!'
        : 'Không tìm thấy tài khoản phù hợp với bộ lọc hiện tại.';
      if (tbody) tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:36px;color:var(--text-muted);">${U.escapeHtml(emptyMsg)}</td></tr>`;
      if (cardsGrid) cardsGrid.innerHTML = `<div style="grid-column:1/-1;text-align:center;padding:36px;color:var(--text-muted);">${U.escapeHtml(emptyMsg)}</div>`;
      return;
    }

    if (tbody) {
      tbody.innerHTML = displayList.map((a, idx) => {
        const gIdx = startIndex + idx + 1;
        const pct = Math.round(Number(a.usage_percent || 0));
        const tier = U.getQuotaTier(a);
        const emailShown = U.escapeHtml(getDisplayedEmail(a));
        const fileShown = U.escapeHtml(a.file_name || '');
        const nameShown = U.escapeHtml(a.display_name || '—');
        const fpId = U.escapeHtml(a.fingerprint_id || `FP-${a.id}`);
        const spendTxt = U.formatSpend(a.total_spend, a.display_threshold || 200);
        const prof = a.config_profile || 'default';
        const isCustomProf = prof && !['default', 'agent_turbo', 'cost_saver', 'privacy_stealth'].includes(prof);
        const rowCls = a.is_active ? 'row-active' : (tier.key === 'cooldown' || tier.key === 'expired' ? 'row-cooldown' : '');

        const tokType = (a.token_type || '').toLowerCase();
        const tokBadge = tokType === 'session'
          ? '<span class="pill pill-emerald" title="Desktop IDE Session Token — hoạt động đầy đủ AI Chat & Quota">IDE Session</span>'
          : (tokType === 'web'
            ? '<span class="pill pill-amber" title="Web Cookie Only (Cursor chặn PKCE với FRESH_SIGN_IN_REQUIRED). Chỉ xem được Quota — hãy dùng cookie vừa login mới hoặc bấm Lưu Acc Từ Cursor.">Web Only</span>'
            : '');

        return `
          <tr class="${rowCls}" data-account-id="${a.id}">
            <td class="mono text-muted" style="text-align:center;width:44px;">${gIdx}</td>
            <td>
              <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                <span class="mono" style="font-weight:600;color:#fff;font-size:12.5px;">${emailShown}</span>
                ${a.is_active ? '<span class="pill pill-emerald">ACTIVE</span>' : ''}
                ${tokBadge}
                ${state.globalMaskEmails ? `<button class="btn btn-ghost btn-xs" data-action="toggle-mask" data-id="${a.id}" title="Hiện/Ẩn email">👁</button>` : ''}
              </div>
              <div style="display:flex;align-items:center;gap:6px;margin-top:3px;">
                <span class="mono text-muted truncate" style="font-size:10.5px;max-width:170px;">${fileShown}</span>
                <button class="pill pill-clickable" data-action="inspect-fp" data-id="${a.id}" title="Xem Hardware Fingerprint">FP:${fpId}</button>
              </div>
            </td>
            <td style="font-size:12.5px;color:var(--text-secondary);">${nameShown}</td>
            <td><span class="${tier.pillClass}">${U.escapeHtml(tier.label)}</span></td>
            <td>
              <select class="select-compact" data-action="assign-profile" data-id="${a.id}">
                <option value="default" ${prof === 'default' ? 'selected' : ''}>Standard</option>
                <option value="agent_turbo" ${prof === 'agent_turbo' ? 'selected' : ''}>Turbo</option>
                <option value="cost_saver" ${prof === 'cost_saver' ? 'selected' : ''}>Saver</option>
                <option value="privacy_stealth" ${prof === 'privacy_stealth' ? 'selected' : ''}>Stealth</option>
                ${isCustomProf ? `<option value="${U.escapeHtml(prof)}" selected>${U.escapeHtml(prof)}</option>` : ''}
              </select>
            </td>
            <td style="min-width:190px;">
              <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:5px;" class="mono">
                <span class="${tier.textClass}" style="font-weight:700;">${pct}%</span>
                <span class="text-muted">${spendTxt}</span>
              </div>
              <div class="progress-track">
                <div class="progress-fill ${tier.barClass}" style="width:${Math.min(100, pct)}%"></div>
              </div>
            </td>
            <td class="sticky-actions">
              <div style="display:inline-flex;align-items:center;gap:5px;">
                <button class="btn btn-xs" data-action="copy-token" data-id="${a.id}" title="Sao chép Access Token">Token</button>
                <button class="btn btn-xs" data-action="refresh" data-id="${a.id}" title="Làm mới Quota">↻</button>
                <button class="btn btn-xs ${a.is_active ? 'btn-emerald' : 'btn-primary'}" data-action="switch" data-id="${a.id}" title="Nạp tài khoản vào Cursor">
                  ${a.is_active ? 'Đang Dùng' : 'Dùng'}
                </button>
                <button class="btn btn-xs btn-rose" data-action="delete" data-id="${a.id}" title="Xóa tài khoản">✕</button>
              </div>
            </td>
          </tr>
        `;
      }).join('');
    }

    if (cardsGrid) {
      cardsGrid.innerHTML = displayList.map(a => {
        const pct = Math.round(Number(a.usage_percent || 0));
        const tier = U.getQuotaTier(a);
        const emailShown = U.escapeHtml(getDisplayedEmail(a));
        const nameShown = U.escapeHtml(a.display_name || '—');
        const fpId = U.escapeHtml(a.fingerprint_id || `FP-${a.id}`);
        const spendTxt = U.formatSpend(a.total_spend, a.display_threshold || 200);

        return `
          <div class="acc-card ${a.is_active ? 'is-active' : ''}" data-account-id="${a.id}">
            <div class="acc-card-top">
              <div style="min-width:0;flex:1;">
                <div style="display:flex;align-items:center;gap:6px;">
                  <span class="mono truncate" style="font-weight:600;color:#fff;font-size:12.5px;">${emailShown}</span>
                  ${a.is_active ? '<span class="pill pill-emerald">ACTIVE</span>' : ''}
                </div>
                <div style="display:flex;align-items:center;gap:6px;margin-top:4px;font-size:11px;color:var(--text-muted);">
                  <span class="truncate">${nameShown}</span>
                  <button class="pill pill-clickable" data-action="inspect-fp" data-id="${a.id}">FP:${fpId}</button>
                </div>
              </div>
              <span class="${tier.pillClass}">${U.escapeHtml(tier.label)}</span>
            </div>
            <div>
              <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:5px;" class="mono">
                <span class="${tier.textClass}" style="font-weight:700;">${pct}%</span>
                <span class="text-muted">${spendTxt}</span>
              </div>
              <div class="progress-track">
                <div class="progress-fill ${tier.barClass}" style="width:${Math.min(100, pct)}%"></div>
              </div>
            </div>
            <div class="acc-card-footer">
              <div style="display:flex;gap:5px;">
                <button class="btn btn-xs" data-action="copy-token" data-id="${a.id}">Token</button>
                <button class="btn btn-xs" data-action="refresh" data-id="${a.id}">↻</button>
                ${state.globalMaskEmails ? `<button class="btn btn-xs" data-action="toggle-mask" data-id="${a.id}">👁</button>` : ''}
              </div>
              <div style="display:flex;gap:5px;">
                <button class="btn btn-xs ${a.is_active ? 'btn-emerald' : 'btn-primary'}" data-action="switch" data-id="${a.id}">
                  ${a.is_active ? 'Đang Dùng' : 'Dùng'}
                </button>
                <button class="btn btn-xs btn-rose" data-action="delete" data-id="${a.id}">✕</button>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }
  }

  /* ---------- DELEGATED POOL EVENT HANDLERS (ZERO XSS) ---------- */
  function bindPoolDelegation() {
    const handler = async (e) => {
      const target = e.target.closest('[data-action]');
      if (!target) return;
      const action = target.dataset.action;
      const id = Number(target.dataset.id);
      if (!id) return;

      if (action === 'switch') await switchToAccount(id);
      else if (action === 'refresh') await refreshQuota(id);
      else if (action === 'copy-token') await copyAccountToken(id);
      else if (action === 'inspect-fp' && typeof global.inspectAccountFingerprint === 'function') {
        global.inspectAccountFingerprint(id);
      } else if (action === 'toggle-mask') {
        toggleCardEmailMask(id);
      } else if (action === 'delete') {
        const acc = state.allAccounts.find(x => x.id === id);
        await deleteSingleAccount(id, (acc && (acc.email || acc.file_name)) || `#${id}`);
      }
    };

    const changeHandler = async (e) => {
      const sel = e.target.closest('select[data-action="assign-profile"]');
      if (!sel) return;
      await assignAccountProfile(Number(sel.dataset.id), sel.value);
    };

    const tbody = document.getElementById('accounts-tbody');
    const grid = document.getElementById('accounts-cards-grid');
    if (tbody) {
      tbody.addEventListener('click', handler);
      tbody.addEventListener('change', changeHandler);
    }
    if (grid) {
      grid.addEventListener('click', handler);
    }
  }

  function setViewMode(mode) {
    state.viewMode = mode === 'cards' ? 'cards' : 'table';
    localStorage.setItem('cursor_view_mode', state.viewMode);
    renderAccounts();
  }

  function toggleGlobalEmailMask() {
    state.globalMaskEmails = !state.globalMaskEmails;
    state.unmaskedIds.clear();
    localStorage.setItem('cursor_mask_emails', String(state.globalMaskEmails));
    const btn = document.getElementById('btn-toggle-mask');
    if (btn) btn.classList.toggle('btn-emerald', state.globalMaskEmails);
    renderAccounts();
  }

  function toggleCardEmailMask(id) {
    if (state.unmaskedIds.has(id)) state.unmaskedIds.delete(id);
    else state.unmaskedIds.add(id);
    renderAccounts();
  }

  function setStatusFilter(status) {
    state.statusFilter = status;
    state.currentPage = 1;
    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.status === status);
    });
    document.querySelectorAll('.kpi-card').forEach(card => {
      card.classList.toggle('active-filter', card.dataset.filter === status);
    });
    renderAccounts();
  }

  function filterAccounts() {
    const inp = document.getElementById('search-input');
    state.searchQuery = inp ? inp.value : '';
    state.currentPage = 1;
    renderAccounts();
  }

  function changePageSize(val) {
    state.pageSize = val === 'all' ? 'all' : Number(val || 50);
    state.currentPage = 1;
    renderAccounts();
  }

  function prevPage() {
    if (state.currentPage > 1) { state.currentPage--; renderAccounts(); }
  }

  function nextPage() {
    const total = getFilteredAccounts().length;
    const pages = state.pageSize === 'all' ? 1 : Math.max(1, Math.ceil(total / state.pageSize));
    if (state.currentPage < pages) { state.currentPage++; renderAccounts(); }
  }

  /* ---------- ACCOUNT ACTIONS ---------- */
  async function loadAccounts() {
    try {
      const { ok, data } = await U.apiFetch('/api/accounts');
      if (!ok || !Array.isArray(data)) throw new Error((data && data.error) || 'Failed');
      state.allAccounts = data;
      renderAccounts();
      updateStats();
    } catch (err) {
      const tbody = document.getElementById('accounts-tbody');
      if (tbody && state.allAccounts.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:30px;color:var(--rose-text);">Không thể tải danh sách tài khoản (${U.escapeHtml(err.message)})</td></tr>`;
      }
    }
  }

  async function switchToAccount(id) {
    U.showToast('Đang nạp tài khoản, áp Fingerprint & cập nhật Cursor...', 'loading');
    try {
      const { ok, data } = await U.apiFetch(`/api/switch/${id}?reload=true`, { method: 'POST' });
      if (ok && data && data.success) {
        if (data.warning) {
          U.showToast(data.warning, 'warn');
        } else {
          U.showToast('Đã chuyển tài khoản thành công!', 'success');
        }
        await Promise.allSettled([global.loadActiveStatus(), loadAccounts()]);
      } else {
        U.showToast('Lỗi chuyển đổi: ' + ((data && data.error) || ''), 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối máy chủ', 'error');
    }
  }

  async function autoSwitchBest() {
    U.showToast('Đang tìm tài khoản Tier 1 / Tier 2 tối ưu nhất...', 'loading');
    try {
      const { ok, data } = await U.apiFetch('/api/auto-switch', { method: 'POST' });
      if (ok && data && data.success) {
        U.showToast(`Đã chuyển sang ${data.account.email} (${Math.round(data.account.usage_percent || 0)}%)`, 'success');
        await Promise.allSettled([global.loadActiveStatus(), loadAccounts()]);
      } else {
        U.showToast((data && data.error) || 'Không còn tài khoản khả dụng', 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối máy chủ', 'error');
    }
  }

  async function forceRotateAccount() {
    U.showToast('Đang xoay tua sang tài khoản tiếp theo...', 'loading');
    try {
      const { ok, data } = await U.apiFetch('/api/rotate', { method: 'POST', json: { mark_current_exhausted: true } });
      if (ok && data && data.success) {
        U.showToast(`Đã xoay tua sang ${data.account.email}!`, 'success');
        await Promise.allSettled([global.loadActiveStatus(), loadAccounts()]);
      } else {
        U.showToast('Không thể xoay tua: ' + ((data && data.error) || ''), 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối máy chủ', 'error');
    }
  }

  async function refreshQuota(id) {
    U.showToast('Đang đồng bộ Quota từ Cursor API...', 'loading');
    try {
      const { ok, data } = await U.apiFetch(`/api/refresh/${id}`, { method: 'POST' });
      if (ok && data && data.success) {
        const pct = Math.round((data.profile && data.profile.usagePercent) || 0);
        U.showToast(`Cập nhật thành công (${pct}%)`, 'success');
        await Promise.allSettled([loadAccounts(), global.loadActiveStatus()]);
      } else {
        U.showToast((data && data.error) || 'Không thể cập nhật quota', 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối máy chủ', 'error');
    }
  }

  async function copyAccountToken(id) {
    try {
      const { ok, data } = await U.apiFetch(`/api/accounts/${id}/token`);
      if (ok && data && data.success && data.token) {
        await U.copyToClipboard(data.token, `Đã sao chép Access Token (${data.email || '#' + id})`);
      } else {
        U.showToast((data && data.error) || 'Tài khoản chưa có token', 'warn');
      }
    } catch (_) {
      U.showToast('Lỗi khi lấy token', 'error');
    }
  }

  async function deleteSingleAccount(id, label) {
    if (!confirm(`Xóa tài khoản "${label}" và file cookie tương ứng?`)) return;
    try {
      const { ok, data } = await U.apiFetch(`/api/accounts/${id}`, { method: 'DELETE' });
      if (ok && data && data.success) {
        U.showToast(`Đã xóa ${label}`, 'success');
        await Promise.allSettled([loadAccounts(), loadCookiesInfo()]);
      } else {
        U.showToast('Không thể xóa tài khoản', 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối khi xóa', 'error');
    }
  }

  async function assignAccountProfile(accountId, profileId) {
    try {
      const { ok, data } = await U.apiFetch('/api/cursor/profiles/assign-account', {
        method: 'POST',
        json: { account_id: accountId, profile_id: profileId },
      });
      if (ok && data && data.success) {
        U.showToast(`Đã gán hồ sơ "${profileId}" cho tài khoản #${accountId}`, 'success');
      } else {
        U.showToast('Không thể gán hồ sơ', 'error');
      }
    } catch (_) {
      U.showToast('Lỗi kết nối server', 'error');
    }
  }

  /* ---------- BATCH CHECK ALL ---------- */
  async function toggleCheckAll() {
    const { ok, data } = await U.apiFetch('/api/check-all', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast('Đã khởi chạy kiểm tra toàn pool (HTTP/2)...', 'info');
      startPollingProgress();
    } else if (data && data.throttled) {
      U.showToast(data.message || 'Vui lòng chờ giây lát', 'warn');
    } else {
      startPollingProgress();
    }
  }

  async function stopCheckAll() {
    await U.apiFetch('/api/check-all/stop', { method: 'POST' });
    U.showToast('Đã gửi yêu cầu dừng kiểm tra', 'warn');
  }

  function startPollingProgress() {
    const banner = document.getElementById('batch-progress-banner');
    if (banner) banner.classList.remove('hidden');
    if (state.batchPollTimer) clearInterval(state.batchPollTimer);
    state.batchPollTimer = setInterval(async () => {
      try {
        const { ok, data } = await U.apiFetch('/api/check-all/progress');
        if (!ok || !data) return;
        const pct = data.total > 0 ? Math.round((data.current / data.total) * 100) : 0;
        const ratio = document.getElementById('batch-progress-ratio');
        const cur = document.getElementById('batch-current-acc');
        const bar = document.getElementById('batch-bar');
        if (ratio) ratio.textContent = `(${data.current}/${data.total})`;
        if (cur) cur.textContent = data.current_email || 'Đang đồng bộ...';
        if (bar) bar.style.width = `${pct}%`;
        if (!data.running) {
          clearInterval(state.batchPollTimer);
          state.batchPollTimer = null;
          U.showToast(`Hoàn tất check pool: ${data.success_count} OK, ${data.failed_count} lỗi`, 'success');
          await loadAccounts();
          setTimeout(() => { if (banner) banner.classList.add('hidden'); }, 2200);
        }
      } catch (_) {}
    }, 500);
  }

  /* ---------- CURSOR APP CONTROLS ---------- */
  async function launchCursorApp() {
    U.showToast('Đang khởi động Cursor IDE...', 'loading');
    const { ok, data } = await U.apiFetch('/api/cursor/launch', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast(data.message || 'Đã mở Cursor IDE!', 'success');
      setTimeout(() => global.loadActiveStatus(), 1500);
    } else {
      U.showToast((data && data.error) || 'Không tìm thấy Cursor.exe', 'error');
    }
  }

  async function hardRestartCursorApp() {
    U.showToast('Đang tắt hoàn toàn và khởi động lại Cursor (Hard Reset)...', 'loading');
    const { ok, data } = await U.apiFetch('/api/cursor/hard-restart', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast(data.message || 'Đã Hard Reset Cursor!', 'success');
      setTimeout(() => global.loadActiveStatus(), 1500);
    } else {
      U.showToast((data && data.error) || 'Reset thất bại', 'error');
    }
  }

  async function reloadCursorWindow() {
    U.showToast('Đang gửi lệnh Reload Window tới Cursor...', 'loading');
    const { ok, data } = await U.apiFetch('/api/reload-window', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast(data.message || 'Đã reload cửa sổ Cursor', 'success');
    } else {
      U.showToast((data && data.error) || 'Không thể reload Cursor', 'warn');
    }
  }

  async function toggleAutoRotate() {
    const { ok, data } = await U.apiFetch('/api/auto-rotate/toggle', { method: 'POST' });
    if (ok && data) {
      updateAutoRotateUI(Boolean(data.enabled));
      U.showToast(data.enabled ? 'Đã BẬT Auto-Rotate 5s' : 'Đã TẮT Auto-Rotate', 'info');
    }
  }

  function updateAutoRotateUI(enabled) {
    const btn = document.getElementById('btn-auto-rotate');
    const dot = document.getElementById('auto-rotate-dot');
    const txt = document.getElementById('auto-rotate-text');
    if (btn) btn.classList.toggle('is-on', Boolean(enabled));
    if (dot) dot.className = enabled ? 'status-dot dot-emerald pulse' : 'status-dot dot-zinc';
    if (txt) txt.textContent = enabled ? 'Auto: BẬT' : 'Auto: TẮT';
  }

  async function toggleProxy() {
    U.showToast('Đang chuyển trạng thái Rotating Proxy...', 'loading');
    const { ok, data } = await U.apiFetch('/api/proxy/toggle', { method: 'POST' });
    if (ok && data && data.success) {
      global.updateProxyUI(Boolean(data.running), data.port || 8999);
      U.showToast(data.message || 'Đã cập nhật Proxy', 'success');
      loadProxyTelemetry();
    } else {
      U.showToast((data && data.error) || 'Lỗi proxy', 'error');
    }
  }

  /* ---------- LIVE PROXY TRAFFIC & AI BURN TELEMETRY ---------- */
  async function loadProxyTelemetry() {
    if (document.visibilityState === 'hidden') return;
    try {
      const [stRes, mRes, predRes] = await Promise.allSettled([
        U.apiFetch('/api/proxy/status'),
        U.apiFetch('/api/proxy/metrics'),
        U.apiFetch('/api/ai/predict-quota'),
      ]);

      if (stRes.status === 'fulfilled' && stRes.value.ok && stRes.value.data) {
        const s = stRes.value.data;
        global.updateProxyUI(Boolean(s.running), s.port || 8999);
        const setVal = (id, v) => { const e = document.getElementById(id); if (e) e.value = v; };
        const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
        setVal('proxy-lookup-select', s.lookup_strategy || 'cache');
        setVal('proxy-midstream-select', s.midstream_strategy || 'passthrough');
        setTxt('tele-cached-tokens', `${(s.pool_stats && s.pool_stats.ready_count) || 0} ready`);
      }

      if (mRes.status === 'fulfilled' && mRes.value.ok && mRes.value.data) {
        const metrics = mRes.value.data.metrics || [];
        const totalReq = metrics.length;
        const swaps = metrics.filter(m => m.swap_occurred).length;
        const avgSwap = swaps > 0
          ? (metrics.filter(m => m.swap_occurred).reduce((acc, m) => acc + Number(m.swap_latency_ms || 0), 0) / swaps).toFixed(2)
          : '0.00';
        const avgTtft = totalReq > 0
          ? Math.round(metrics.reduce((acc, m) => acc + Number(m.ttft_ms || 0), 0) / totalReq)
          : 0;

        const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
        setTxt('tele-req-count', totalReq);
        setTxt('tele-swap-count', swaps);
        setTxt('tele-avg-swap', `${avgSwap}ms`);
        setTxt('tele-avg-ttft', `${avgTtft}ms`);

        const tbody = document.getElementById('proxy-metrics-tbody');
        if (tbody) {
          if (metrics.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:18px;color:var(--text-muted);">Chưa có request nào đi qua Rotating Proxy (:8999). Bật định tuyến Proxy trong Cài Đặt để theo dõi live.</td></tr>';
          } else {
            const recent = metrics.slice(-15).reverse();
            tbody.innerHTML = recent.map(m => {
              const ttft = Math.round(Number(m.ttft_ms || 0));
              const barPct = Math.min(100, Math.round((ttft / 1500) * 100));
              const stCls = (m.final_status || 200) < 400 ? 'pill pill-emerald' : 'pill pill-rose';
              return `
                <tr>
                  <td class="mono truncate" style="max-width:220px;font-size:11px;">/${U.escapeHtml(m.path || '')}</td>
                  <td><span class="${stCls}">${U.escapeHtml(m.first_status ?? '—')} → ${U.escapeHtml(m.final_status ?? '—')}</span></td>
                  <td class="mono" style="font-size:11px;">${m.swap_occurred ? `<span class="pill pill-amber">${m.swap_hops} hop · ${Number(m.swap_latency_ms || 0).toFixed(2)}ms</span>` : '<span class="text-muted">Direct</span>'}</td>
                  <td class="mono" style="font-size:11px;">
                    <span class="latency-bar-wrap"><span class="latency-bar-fill" style="width:${barPct}%"></span></span>${ttft}ms
                  </td>
                  <td class="mono text-muted" style="font-size:11px;">${U.escapeHtml(m.final_token || '—')}</td>
                  <td class="mono text-muted" style="font-size:11px;">${Math.round(Number(m.total_ms || 0))}ms</td>
                </tr>
              `;
            }).join('');
          }
        }
      }

      if (predRes.status === 'fulfilled' && predRes.value.ok && predRes.value.data && predRes.value.data.prediction) {
        const p = predRes.value.data.prediction;
        const badge = document.getElementById('active-burn-badge');
        if (badge) {
          const rate = Number(p.burn_rate_percent_per_min || 0);
          if (rate > 0.01) {
            const mins = Math.round(Number(p.tte_seconds || 0) / 60);
            badge.textContent = `Burn: ${rate.toFixed(1)}%/m · ~${mins}m`;
            badge.className = p.is_critical ? 'pill pill-rose' : 'pill pill-amber';
            badge.classList.remove('hidden');
          } else {
            badge.classList.add('hidden');
          }
        }
      }
    } catch (_) {}
  }

  async function updateProxyConfig() {
    const lookup = document.getElementById('proxy-lookup-select')?.value || 'cache';
    const mid = document.getElementById('proxy-midstream-select')?.value || 'passthrough';
    const { ok, data } = await U.apiFetch('/api/proxy/config', {
      method: 'POST',
      json: { lookup_strategy: lookup, midstream_strategy: mid },
    });
    if (ok && data && data.success) U.showToast(`Proxy config: ${lookup} / ${mid}`, 'success');
  }

  async function reloadProxyCache() {
    const { ok, data } = await U.apiFetch('/api/proxy/reload', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast(`Đã tải lại ${data.cached_accounts} token vào RAM Proxy (<0.05ms)`, 'success');
      loadProxyTelemetry();
    }
  }

  async function pingUpstreamGateway() {
    const badge = document.getElementById('gateway-ping-badge');
    if (badge) badge.textContent = 'Pinging...';
    const { ok, data } = await U.apiFetch('/api/cursor/network-test');
    if (ok && data) {
      if (badge) {
        badge.textContent = data.upstream_ok ? `api2.cursor.sh · ${data.latency_ms}ms` : `Offline (${data.error || 'err'})`;
        badge.className = data.upstream_ok ? 'pill pill-emerald' : 'pill pill-rose';
      }
    }
  }

  /* ---------- UNIFIED SNAPSHOT + SSE LIVE STREAM ---------- */
  function connectSSE() {
    if (typeof EventSource === 'undefined') return;
    try {
      const es = new EventSource('/api/events');
      const dot = document.getElementById('sse-dot');
      const txt = document.getElementById('sse-text');
      es.addEventListener('connected', () => {
        state.sseConnected = true;
        if (dot) dot.className = 'status-dot dot-emerald pulse';
        if (txt) txt.textContent = 'SSE Live';
      });
      const onDelta = async () => {
        await Promise.allSettled([global.loadActiveStatus(), loadAccounts(), loadCookiesInfo()]);
      };
      ['state_changed', 'cookies_scanned', 'account_refreshed', 'account_switched', 'account_captured', 'accounts_deleted', 'account_deleted', 'startup_sync_done', 'batch_check_done', 'proxy_exhausted'].forEach(ev => {
        es.addEventListener(ev, onDelta);
      });
      es.addEventListener('keyboard_alert', () => {
        U.showToast('Watchdog vừa tự động giải phóng phím modifier bị kẹt!', 'warn');
      });
      es.onerror = () => {
        state.sseConnected = false;
        if (dot) dot.className = 'status-dot dot-amber';
        if (txt) txt.textContent = 'Polling';
      };
    } catch (_) {}
  }

  function updateKeyboardStatusBadge(kb) {
    const dot = document.getElementById('keyboard-status-dot');
    const txt = document.getElementById('keyboard-status-text');
    const isStuck = Boolean(kb && (kb.is_stuck || kb.any_stuck));
    if (dot) dot.className = isStuck ? 'status-dot dot-rose pulse' : 'status-dot dot-emerald';
    if (txt) txt.textContent = isStuck ? 'KẸT PHÍM!' : 'OK';
  }

  function dismissOfflineNotification() {
    state.offlineNotificationDismissed = true;
    const b = document.getElementById('cursor-offline-banner');
    if (b) b.classList.add('hidden');
  }

  function copyActiveEmail() {
    const el = document.getElementById('active-email');
    if (el) U.copyToClipboard(el.textContent.trim(), 'Đã sao chép email active');
  }

  /* Export globally for inline triggers & bootstrap */
  Object.assign(global, {
    navigateSection,
    toggleWorkbench,
    switchIngestTab,
    onPasteInputChanged,
    submitPastedCookies,
    startInteractiveOAuthLogin,
    copyOAuthLoginUrl,
    cancelInteractiveOAuthLogin,
    captureActiveCursorAccount,
    openCookieFolder,
    copyCookieFolderPath,
    loadCookiesInfo,
    quarantineCorruptedCookies,
    convertCookiesToTokens,
    handleCookieDragOver,
    handleCookieDragLeave,
    handleCookieDrop,
    handleCookieFileSelect,
    uploadCookieFiles,
    loadAccounts,
    renderAccounts,
    updateStats,
    setViewMode,
    toggleGlobalEmailMask,
    toggleCardEmailMask,
    setStatusFilter,
    filterAccounts,
    changePageSize,
    prevPage,
    nextPage,
    switchToAccount,
    autoSwitchBest,
    forceRotateAccount,
    refreshQuota,
    copyAccountToken,
    deleteSingleAccount,
    assignAccountProfile,
    toggleCheckAll,
    stopCheckAll,
    startPollingProgress,
    launchCursorApp,
    hardRestartCursorApp,
    reloadCursorWindow,
    toggleAutoRotate,
    updateAutoRotateUI,
    toggleProxy,
    loadProxyTelemetry,
    updateProxyConfig,
    reloadProxyCache,
    pingUpstreamGateway,
    updateKeyboardStatusBadge,
    dismissOfflineNotification,
    copyActiveEmail,
    bindPoolDelegation,
    connectSSE,
  });
})(window);
