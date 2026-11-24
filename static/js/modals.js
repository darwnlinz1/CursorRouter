/* CursorRouter v3.0 — Modals Controller (Anti-Detect, 8-Category Settings, Diagnostics, Cleanup, Updater) */
(function (global) {
  'use strict';

  const U = global.CRUtils;

  /* ---------- UNIFIED MODAL CLOSE ---------- */
  function closeAllModals() {
    ['antidetect-modal', 'debug-modal', 'cleanup-modal', 'update-modal', 'cursor-settings-modal'].forEach(id => {
      const m = document.getElementById(id);
      if (m) m.classList.add('hidden');
    });
  }

  /* ================= 1. ANTI-DETECT MODAL ================= */
  async function openAntidetectModal(preselectAccountId) {
    const m = document.getElementById('antidetect-modal');
    if (m) m.classList.remove('hidden');
    await loadAntidetectConfig();
    populateAntidetectAccountsDropdown(preselectAccountId);
  }

  function closeAntidetectModal() {
    const m = document.getElementById('antidetect-modal');
    if (m) m.classList.add('hidden');
  }

  async function loadAntidetectConfig() {
    const { ok, data } = await U.apiFetch('/api/antidetect/config');
    if (!ok || !data || !data.success) return;
    const mode = data.antidetect_mode || 'account_locked';
    document.querySelectorAll('.antidetect-mode-card').forEach(card => {
      card.classList.toggle('selected', card.dataset.mode === mode);
    });
    const fp = data.current_fingerprint || {};
    const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v || '—'; };
    setTxt('ad-cur-machine-id', fp.machineId);
    setTxt('ad-cur-mac-id', fp.macMachineId);
    setTxt('ad-cur-dev-id', fp.devDeviceId);
    setTxt('ad-cur-sqm-id', fp.sqmId);
  }

  async function setAntidetectMode(mode) {
    const { ok, data } = await U.apiFetch('/api/antidetect/config', {
      method: 'POST',
      json: { antidetect_mode: mode },
    });
    if (ok && data && data.success) {
      U.showToast(`Chế độ Anti-Detect: ${mode}`, 'success');
      await loadAntidetectConfig();
    } else {
      U.showToast('Lỗi cập nhật chế độ Anti-Detect', 'error');
    }
  }

  async function spoofMachineIDs() {
    U.showToast('Đang tạo Hardware Machine ID mới...', 'loading');
    const { ok, data } = await U.apiFetch('/api/cursor/storage-ids/spoof', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast('Đã Randomize toàn bộ 4 Hardware IDs!', 'success');
      await Promise.allSettled([loadAntidetectConfig(), loadHardwareData()]);
    } else {
      U.showToast((data && data.error) || 'Không thể spoof', 'warn');
    }
  }

  async function restoreMachineIDs() {
    const { ok, data } = await U.apiFetch('/api/cursor/storage-ids/restore', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast('Đã khôi phục Hardware IDs từ bản sao lưu!', 'success');
      await Promise.allSettled([loadAntidetectConfig(), loadHardwareData()]);
    } else {
      U.showToast((data && data.error) || 'Không tìm thấy file backup', 'warn');
    }
  }

  function populateAntidetectAccountsDropdown(preselectId) {
    const sel = document.getElementById('ad-account-select');
    if (!sel) return;
    const accs = (global.CRState && global.CRState.allAccounts) || [];
    sel.innerHTML = '<option value="">-- Chọn tài khoản cần kiểm tra vân tay --</option>' +
      accs.map(a => `<option value="${a.id}">${U.escapeHtml(a.email || a.file_name)} (FP:${U.escapeHtml(a.fingerprint_id || a.id)})</option>`).join('');
    if (preselectId) {
      sel.value = String(preselectId);
      onSelectAccountFingerprint(preselectId);
    }
  }

  async function onSelectAccountFingerprint(accId) {
    const box = document.getElementById('ad-selected-fp-box');
    if (!box) return;
    if (!accId) {
      box.innerHTML = '<div class="text-muted mono" style="font-size:11px;">Chọn một tài khoản ở trên để xem / đổi vân tay phần cứng riêng biệt.</div>';
      return;
    }
    const { ok, data } = await U.apiFetch(`/api/antidetect/account/${Number(accId)}`);
    if (!ok || !data || !data.fingerprint) {
      box.innerHTML = '<div class="text-rose mono" style="font-size:11px;">Không tải được fingerprint.</div>';
      return;
    }
    const fp = data.fingerprint;
    box.innerHTML = `
      <div class="form-grid-2" style="font-size:11px;">
        <div><div class="text-muted">machineId</div><div class="code-field">${U.escapeHtml(fp.machineId || '—')}</div></div>
        <div><div class="text-muted">macMachineId</div><div class="code-field">${U.escapeHtml(fp.macMachineId || '—')}</div></div>
        <div><div class="text-muted">devDeviceId</div><div class="code-field">${U.escapeHtml(fp.devDeviceId || '—')}</div></div>
        <div><div class="text-muted">sqmId</div><div class="code-field">${U.escapeHtml(fp.sqmId || '—')}</div></div>
      </div>
      <div style="display:flex;gap:8px;margin-top:10px;">
        <button class="btn btn-xs" onclick="regenerateSelectedFingerprint()">Tạo Vân Tay Mới</button>
        <button class="btn btn-xs btn-primary" onclick="applySelectedFingerprint()">Áp Ngay Vào Cursor</button>
      </div>
    `;
  }

  async function regenerateSelectedFingerprint() {
    const id = document.getElementById('ad-account-select')?.value;
    if (!id) return;
    const { ok, data } = await U.apiFetch(`/api/antidetect/regenerate/${Number(id)}`, { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast(`Đã cấp mới Fingerprint cho #${id}`, 'success');
      await onSelectAccountFingerprint(id);
      await global.loadAccounts();
    }
  }

  async function applySelectedFingerprint() {
    const id = document.getElementById('ad-account-select')?.value;
    if (!id) return;
    const { ok, data } = await U.apiFetch(`/api/antidetect/apply/${Number(id)}`, { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast(`Đã áp Fingerprint #${id} vào storage.json`, 'success');
      await loadAntidetectConfig();
    }
  }

  function inspectAccountFingerprint(accId) {
    openAntidetectModal(accId);
  }

  /* ================= 2. SYSTEM DIAGNOSTICS MODAL ================= */
  async function openDebugModal() {
    const m = document.getElementById('debug-modal');
    if (m) m.classList.remove('hidden');
    await refreshDebugInfo();
  }

  function closeDebugModal() {
    const m = document.getElementById('debug-modal');
    if (m) m.classList.add('hidden');
  }

  async function refreshDebugInfo() {
    const { ok, data } = await U.apiFetch('/api/debug/system-info');
    if (!ok || !data || !data.success) return;

    const kb = data.keyboard || {};
    const mods = kb.modifiers || {};
    const setPill = (id, pressed) => {
      const e = document.getElementById(id);
      if (!e) return;
      e.className = pressed ? 'pill pill-rose' : 'pill pill-emerald';
      e.textContent = `${e.dataset.keyLabel}: ${pressed ? 'ĐANG ĐÈ' : 'OK'}`;
    };
    setPill('dbg-key-alt', mods.alt);
    setPill('dbg-key-ctrl', mods.ctrl);
    setPill('dbg-key-shift', mods.shift);
    setPill('dbg-key-win', mods.win);

    const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    const cur = data.cursor || {};
    setTxt('dbg-cursor-running', cur.is_running ? 'Running' : 'Stopped');
    setTxt('dbg-cursor-pid', cur.pid ?? '—');
    setTxt('dbg-cursor-ram', cur.memory_mb ? `${cur.memory_mb} MB` : '—');
    setTxt('dbg-cursor-hwnd', cur.hwnd ?? '—');
    setTxt('dbg-cursor-title', cur.window_title || '—');
    setTxt('dbg-user-idle', `${kb.user_idle_seconds ?? 0}s`);

    const eventsBox = document.getElementById('dbg-events-log');
    if (eventsBox) {
      const evs = data.recent_events || [];
      eventsBox.innerHTML = evs.length === 0
        ? '<div class="text-muted">Chưa có sự kiện nào</div>'
        : evs.slice().reverse().map(ev =>
            `<div style="padding:4px 0;border-bottom:1px solid var(--border-subtle);font-size:11px;" class="mono">
              <span class="text-muted">[${U.escapeHtml(ev.time_iso)}]</span>
              <span class="text-emerald">${U.escapeHtml(ev.type)}</span>
              <span>${U.escapeHtml(ev.details)}</span>
            </div>`
          ).join('');
    }
  }

  async function unstickKeys() {
    const { ok, data } = await U.apiFetch('/api/unstick-keys', { method: 'POST' });
    if (ok && data) {
      U.showToast(data.message || 'Đã giải phóng toàn bộ modifier keys!', 'success');
      await refreshDebugInfo();
    }
  }

  /* ================= 3. ACCOUNT CLEANUP MODAL ================= */
  function openCleanupModal() {
    const m = document.getElementById('cleanup-modal');
    if (m) m.classList.remove('hidden');
    const accs = (global.CRState && global.CRState.allAccounts) || [];
    const exh = accs.filter(a => U.getQuotaTier(a).key === 'cooldown').length;
    const inv = accs.filter(a => a.status === 'EXPIRED').length;
    const elExh = document.getElementById('cleanup-exhausted-count');
    const elInv = document.getElementById('cleanup-invalid-count');
    if (elExh) elExh.textContent = exh;
    if (elInv) elInv.textContent = inv;
  }

  function closeCleanupModal() {
    const m = document.getElementById('cleanup-modal');
    if (m) m.classList.add('hidden');
  }

  async function cleanHighUsageAccounts() {
    if (!confirm('Xóa toàn bộ phiên tài khoản cạn quota (≥100%) khỏi pool?')) return;
    const { ok, data } = await U.apiFetch('/api/accounts/clean-high-usage', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast(`Đã dọn dẹp ${data.deleted || 0} tài khoản hết quota`, 'success');
      closeCleanupModal();
      await Promise.allSettled([global.loadAccounts(), global.loadPoolSummary()]);
    }
  }

  async function cleanInvalidAccounts() {
    if (!confirm('Xóa toàn bộ phiên OAuth EXPIRED khỏi pool?')) return;
    const { ok, data } = await U.apiFetch('/api/accounts/clean-invalid', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast(`Đã dọn dẹp ${data.deleted || 0} phiên hết hạn`, 'success');
      closeCleanupModal();
      await Promise.allSettled([global.loadAccounts(), global.loadPoolSummary()]);
    }
  }

  /* ================= 4. AUTO-UPDATER MODAL ================= */
  function openUpdateModal() {
    const m = document.getElementById('update-modal');
    if (m) m.classList.remove('hidden');
    fetchUpdateStatus();
  }

  function closeUpdateModal() {
    const m = document.getElementById('update-modal');
    if (m) m.classList.add('hidden');
  }

  function renderUpdaterUI(data) {
    if (!data) return;
    const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    setTxt('updater-cur-version', `v${data.current_version || '3.0.0'}`);
    setTxt('updater-state-label', data.state || 'IDLE');

    const info = data.update_info;
    const card = document.getElementById('updater-info-card');
    const btnDl = document.getElementById('btn-updater-download');
    const btnApply = document.getElementById('btn-updater-apply');
    const badge = document.getElementById('update-badge');

    if (info && info.update_available) {
      if (card) card.classList.remove('hidden');
      if (badge) badge.classList.remove('hidden');
      setTxt('updater-latest-version', `v${info.latest_version}`);
      setTxt('updater-changelog', info.changelog || 'Bản cập nhật mới đã sẵn sàng.');
      if (btnDl) btnDl.classList.toggle('hidden', data.state === 'DOWNLOADED');
      if (btnApply) btnApply.classList.toggle('hidden', data.state !== 'DOWNLOADED');
    } else {
      if (card) card.classList.add('hidden');
      if (badge) badge.classList.add('hidden');
      if (btnDl) btnDl.classList.add('hidden');
      if (btnApply) btnApply.classList.add('hidden');
    }
    if (data.last_error) {
      setTxt('updater-error-text', data.last_error);
    } else {
      setTxt('updater-error-text', '');
    }
  }

  async function fetchUpdateStatus() {
    const { ok, data } = await U.apiFetch('/api/updater/status');
    if (ok && data) renderUpdaterUI(data);
  }

  async function checkUpdates(userInitiated) {
    if (userInitiated) U.showToast('Đang kiểm tra bản phát hành mới trên GitHub...', 'loading');
    const { ok, data } = await U.apiFetch('/api/updater/check', { method: 'POST' });
    if (ok && data) {
      renderUpdaterUI(data);
      if (userInitiated) {
        U.showToast(data.has_update ? `Có bản mới v${data.update_info.latest_version}!` : 'Bạn đang dùng phiên bản mới nhất!', 'info');
      }
    }
  }

  async function downloadUpdate() {
    U.showToast('Đang tải bản cập nhật & xác thực SHA-256...', 'loading');
    const { ok, data } = await U.apiFetch('/api/updater/download', { method: 'POST' });
    if (ok && data && data.success) {
      renderUpdaterUI(data);
      U.showToast('Tải xong & xác thực SHA-256 thành công!', 'success');
    } else {
      U.showToast((data && data.error) || 'Tải bản cập nhật thất bại', 'error');
    }
  }

  async function applyUpdate() {
    const { ok, data } = await U.apiFetch('/api/updater/apply', { method: 'POST' });
    if (ok && data && data.success) {
      U.showToast('Đang khởi chạy trình cài đặt nền & khởi động lại...', 'success');
    } else {
      U.showToast((data && data.error) || 'Không thể áp dụng bản cập nhật', 'error');
    }
  }

  /* ================= 5. 8-CATEGORY CURSOR SETTINGS MODAL ================= */
  let _cachedProfiles = {};

  function openCursorSettingsModal(initialTab) {
    const m = document.getElementById('cursor-settings-modal');
    if (m) m.classList.remove('hidden');
    switchSettingsTab(initialTab || 'general');
    Promise.allSettled([loadSettingsData(), loadAutoSwitchConfig(), loadHardwareData(), loadProfilesData(), loadCursorRulesData()]);
  }

  function closeCursorSettingsModal() {
    const m = document.getElementById('cursor-settings-modal');
    if (m) m.classList.add('hidden');
  }

  function switchSettingsTab(tabName) {
    document.querySelectorAll('.settings-tab-content').forEach(p => p.classList.add('hidden'));
    const activePane = document.getElementById(`settings-tab-${tabName}`);
    if (activePane) activePane.classList.remove('hidden');
    document.querySelectorAll('.settings-nav-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
  }

  function filterSettings(query) {
    const q = (query || '').trim().toLowerCase();
    const rows = document.querySelectorAll('#cursor-settings-modal .setting-row, #cursor-settings-modal .setting-group');
    rows.forEach(r => r.classList.remove('search-match'));
    if (!q) return;

    const tabs = document.querySelectorAll('.settings-tab-content');
    let firstTab = null;
    tabs.forEach(tab => {
      if (tab.textContent.toLowerCase().includes(q)) {
        if (!firstTab) firstTab = tab.id.replace('settings-tab-', '');
        tab.querySelectorAll('.setting-row').forEach(row => {
          if (row.textContent.toLowerCase().includes(q)) row.classList.add('search-match');
        });
      }
    });
    if (firstTab) switchSettingsTab(firstTab);
  }

  function setEditorFontPreset(fontCss) {
    const el = document.getElementById('input-editor-font-family');
    if (el) el.value = fontCss;
  }

  async function loadSettingsData() {
    const { ok, data } = await U.apiFetch('/api/cursor/settings');
    if (!ok || !data) return;

    const setVal = (id, v) => { const e = document.getElementById(id); if (e && v !== undefined && v !== null) e.value = v; };
    const setChk = (id, v) => { const e = document.getElementById(id); if (e) e.checked = Boolean(v); };

    // General
    setVal('select-startup-editor', data.startup_editor || 'none');
    setVal('select-restore-windows', data.restore_windows || 'all');
    setChk('toggle-dnd-mode', data.dnd_mode);
    setChk('toggle-silent-notif', data.silent_notifications);
    setChk('toggle-privacy-mode', data.privacy_mode);
    setChk('toggle-ghost-mode', data.ghost_mode);
    setChk('toggle-index-local', data.index_locally_only);

    // Models & Agent
    const selModel = document.getElementById('select-default-model');
    if (selModel && Array.isArray(data.available_models)) {
      selModel.innerHTML = data.available_models.map(m => `<option value="${U.escapeHtml(m.id)}">${U.escapeHtml(m.name)} (${U.escapeHtml(m.badge || '')})</option>`).join('');
    }
    setVal('select-default-model', data.default_model || 'default');
    setVal('select-thinking-effort', data.thinking_effort || 'medium');
    setChk('toggle-fast-mode', data.fast_mode);
    setChk('toggle-composer-agent', data.composer_agent_mode);
    setChk('toggle-composer-auto-apply', data.composer_auto_apply);
    setChk('toggle-composer-terminal', data.composer_auto_execute_terminal);
    setChk('toggle-inline-diffs', data.use_inline_diffs);

    // Tab (CPP)
    setChk('toggle-cpp-enable', data.cpp_enable);
    setChk('toggle-cpp-partial', data.cpp_partial_accepts);
    setChk('toggle-cpp-autosuggest', data.cpp_auto_suggest);
    setVal('input-cpp-delay', data.cpp_trigger_delay ?? 50);

    // Appearance
    setVal('select-color-theme', data.color_theme || 'Default Dark Modern');
    setVal('input-editor-font-family', data.font_family || "'JetBrains Mono', Consolas, monospace");
    setVal('input-editor-font-size', data.font_size ?? 14);
    setVal('input-tab-size', data.tab_size ?? 4);
    setChk('toggle-minimap', data.minimap);
    setChk('toggle-format-save', data.format_on_save);

    // Code Intelligence
    const ci = (data.sections && data.sections.code_intelligence) || {};
    setChk('toggle-indexing-enable', data.indexing_enable);
    setChk('toggle-semantic-search', data.semantic_search);
    setChk('toggle-precise-symbols', data.precise_symbols);
    setVal('select-indexing-engine', ci.indexing_engine || 'turbo_graph');

    // Browser & Network
    setChk('toggle-route-proxy', data.is_proxy_routed);
    setChk('toggle-proxy-ssl', data.proxy_strict_ssl);
    setVal('input-proxy-bypass', data.proxy_bypass || 'localhost, 127.0.0.1');

    // Git & Worktrees (FIXED: now properly loaded & saved!)
    const git = (data.sections && data.sections.git_prs) || {};
    const wt = (data.sections && data.sections.worktrees) || {};
    setChk('toggle-git-integration', git.git_integration ?? data.enable_pull_requests);
    setChk('toggle-git-pr-review', git.pr_review_assistant ?? data.enable_pull_requests);
    setChk('toggle-git-commit-msg', git.commit_message_generator ?? true);
    setChk('toggle-worktree-isolation', wt.multi_worktree_isolation ?? data.worktrees_enabled);
    setChk('toggle-worktree-cleanup', wt.auto_cleanup ?? data.prune_merged);
  }

  async function saveSettingsPatch(patch, label) {
    const { ok, data } = await U.apiFetch('/api/cursor/settings', { method: 'POST', json: patch });
    if (ok && data && data.success) {
      U.showToast(`Đã lưu cài đặt ${label}!`, 'success');
      await global.loadActiveStatus();
    } else {
      U.showToast('Lỗi lưu cài đặt: ' + ((data && data.error) || ''), 'error');
    }
  }

  function saveGeneralSettings() {
    return saveSettingsPatch({
      startup_editor: document.getElementById('select-startup-editor')?.value,
      restore_windows: document.getElementById('select-restore-windows')?.value,
      dnd_mode: document.getElementById('toggle-dnd-mode')?.checked,
      silent_notifications: document.getElementById('toggle-silent-notif')?.checked,
      privacy_mode: document.getElementById('toggle-privacy-mode')?.checked,
      ghost_mode: document.getElementById('toggle-ghost-mode')?.checked,
      index_locally_only: document.getElementById('toggle-index-local')?.checked,
    }, 'General & Privacy');
  }

  function saveModelSettings() {
    return saveSettingsPatch({
      default_model: document.getElementById('select-default-model')?.value,
      thinking_effort: document.getElementById('select-thinking-effort')?.value,
      fast_mode: document.getElementById('toggle-fast-mode')?.checked,
      composer_agent_mode: document.getElementById('toggle-composer-agent')?.checked,
      composer_auto_apply: document.getElementById('toggle-composer-auto-apply')?.checked,
      composer_auto_execute_terminal: document.getElementById('toggle-composer-terminal')?.checked,
      use_inline_diffs: document.getElementById('toggle-inline-diffs')?.checked,
    }, 'Models & Agent');
  }

  function saveFeatureSettings() {
    return saveSettingsPatch({
      cpp_enable: document.getElementById('toggle-cpp-enable')?.checked,
      cpp_partial_accepts: document.getElementById('toggle-cpp-partial')?.checked,
      cpp_auto_suggest: document.getElementById('toggle-cpp-autosuggest')?.checked,
      cpp_trigger_delay: Number(document.getElementById('input-cpp-delay')?.value || 50),
    }, 'Cursor Tab (CPP)');
  }

  function saveEditorSettings() {
    return saveSettingsPatch({
      color_theme: document.getElementById('select-color-theme')?.value,
      font_family: document.getElementById('input-editor-font-family')?.value,
      font_size: Number(document.getElementById('input-editor-font-size')?.value || 14),
      tab_size: Number(document.getElementById('input-tab-size')?.value || 4),
      minimap: document.getElementById('toggle-minimap')?.checked,
      format_on_save: document.getElementById('toggle-format-save')?.checked,
    }, 'Appearance');
  }

  function saveIntelligenceSettings() {
    return saveSettingsPatch({
      indexing_enable: document.getElementById('toggle-indexing-enable')?.checked,
      semantic_search: document.getElementById('toggle-semantic-search')?.checked,
      precise_symbols: document.getElementById('toggle-precise-symbols')?.checked,
      sections: { code_intelligence: { indexing_engine: document.getElementById('select-indexing-engine')?.value || 'turbo_graph' } },
    }, 'Code Intelligence');
  }

  function saveProxyRoutingSettings() {
    return saveSettingsPatch({
      route_via_proxy: document.getElementById('toggle-route-proxy')?.checked,
      proxy_strict_ssl: document.getElementById('toggle-proxy-ssl')?.checked,
      proxy_bypass: document.getElementById('input-proxy-bypass')?.value || 'localhost, 127.0.0.1',
    }, 'Browser & Network');
  }

  function saveGitWorktreesSettings() {
    return saveSettingsPatch({
      sections: {
        git_prs: {
          git_integration: Boolean(document.getElementById('toggle-git-integration')?.checked),
          pr_review_assistant: Boolean(document.getElementById('toggle-git-pr-review')?.checked),
          commit_message_generator: Boolean(document.getElementById('toggle-git-commit-msg')?.checked),
        },
        worktrees: {
          multi_worktree_isolation: Boolean(document.getElementById('toggle-worktree-isolation')?.checked),
          auto_cleanup: Boolean(document.getElementById('toggle-worktree-cleanup')?.checked),
        },
      },
    }, 'Git & Worktrees');
  }

  async function testCursorNetwork() {
    const out = document.getElementById('network-test-result');
    if (out) out.textContent = 'Đang kiểm tra kết nối tới api2.cursor.sh...';
    const { ok, data } = await U.apiFetch('/api/cursor/network-test');
    if (ok && data && out) {
      out.textContent = data.upstream_ok
        ? `OK · api2.cursor.sh (HTTP ${data.upstream_status}) · ${data.latency_ms}ms · Proxy :${data.proxy_port} ${data.proxy_running ? 'ONLINE' : 'OFFLINE'}`
        : `Lỗi kết nối: ${data.error || 'Unknown'} (${data.latency_ms}ms)`;
    }
  }

  /* Auto-Switch Config */
  async function loadAutoSwitchConfig() {
    const { ok, data } = await U.apiFetch('/api/cursor/auto-switch-config');
    if (!ok || !data || !data.config) return;
    const c = data.config;
    const setVal = (id, v) => { const e = document.getElementById(id); if (e && v !== undefined) e.value = v; };
    const setChk = (id, v) => { const e = document.getElementById(id); if (e) e.checked = Boolean(v); };
    setVal('select-reset-mode', c.reset_mode || 'hard_restart');
    setVal('select-auto-resend', c.auto_resend_action || 'manual');
    setVal('input-continue-prompt', c.continue_prompt || 'Tiếp tục');
    setVal('select-target-mode', c.target_mode || 'composer');
    setChk('toggle-apply-profile-switch', c.apply_profile_on_switch);
  }

  async function saveAutoSwitchConfig() {
    const payload = {
      reset_mode: document.getElementById('select-reset-mode')?.value || 'hard_restart',
      auto_resend_action: document.getElementById('select-auto-resend')?.value || 'manual',
      continue_prompt: document.getElementById('input-continue-prompt')?.value || 'Tiếp tục',
      target_mode: document.getElementById('select-target-mode')?.value || 'composer',
      apply_profile_on_switch: Boolean(document.getElementById('toggle-apply-profile-switch')?.checked),
    };
    const { ok, data } = await U.apiFetch('/api/cursor/auto-switch-config', { method: 'POST', json: payload });
    if (ok && data && data.success) {
      U.showToast('Đã lưu cấu hình Auto-Switch & Tiếp Tục!', 'success');
      await global.loadActiveStatus();
    } else {
      U.showToast('Lỗi lưu cấu hình Auto-Switch', 'error');
    }
  }

  async function testSendContinuePrompt() {
    const prompt = document.getElementById('input-continue-prompt')?.value || 'Tiếp tục';
    const target_mode = document.getElementById('select-target-mode')?.value || 'composer';
    const { ok, data } = await U.apiFetch('/api/cursor/send-continue', { method: 'POST', json: { prompt, target_mode } });
    if (ok && data && data.success) U.showToast('Đã gửi thử prompt tiếp tục an toàn!', 'success');
    else U.showToast((data && data.error) || 'Không thể gửi prompt', 'warn');
  }

  /* Profiles */
  async function loadProfilesData() {
    const { ok, data } = await U.apiFetch('/api/cursor/profiles');
    if (!ok || !data || !data.success) return;
    _cachedProfiles = data.profiles || {};
    const sel = document.getElementById('select-config-profile');
    if (sel) {
      sel.innerHTML = Object.values(_cachedProfiles).map(p =>
        `<option value="${U.escapeHtml(p.id)}" ${p.id === data.active_profile ? 'selected' : ''}>${U.escapeHtml(p.name)} [${U.escapeHtml(p.badge || 'Custom')}]</option>`
      ).join('');
    }
    const countEl = document.getElementById('profiles-total-accounts');
    if (countEl) countEl.textContent = `${data.total_accounts ?? 0} acc`;
    onSelectConfigProfile(sel ? sel.value : 'default');
  }

  function onSelectConfigProfile(profileId) {
    const p = _cachedProfiles[profileId];
    const desc = document.getElementById('profile-selected-desc');
    if (desc) desc.textContent = p ? (p.description || '') : '';
  }

  async function applyProfileToCurrentCursor() {
    const id = document.getElementById('select-config-profile')?.value || 'default';
    const { ok, data } = await U.apiFetch('/api/cursor/profiles/apply-current', { method: 'POST', json: { profile_id: id } });
    if (ok && data && data.success) {
      U.showToast(data.message || `Đã áp dụng hồ sơ ${id}`, 'success');
      await loadSettingsData();
    }
  }

  async function applyProfileToAllAccounts() {
    const id = document.getElementById('select-config-profile')?.value || 'default';
    const { ok, data } = await U.apiFetch('/api/cursor/profiles/apply-all', { method: 'POST', json: { profile_id: id } });
    if (ok && data && data.success) {
      U.showToast(data.message || `Đã gán hồ sơ ${id} cho toàn bộ pool`, 'success');
      await Promise.allSettled([global.loadAccounts(), loadProfilesData()]);
    }
  }

  /* Hardware IDs tab */
  async function loadHardwareData() {
    const { ok, data } = await U.apiFetch('/api/cursor/storage-ids');
    if (!ok || !data) return;
    const setTxt = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v || '—'; };
    setTxt('hw-machine-id', data.machineId);
    setTxt('hw-mac-machine-id', data.macMachineId);
    setTxt('hw-dev-device-id', data.devDeviceId);
    setTxt('hw-sqm-id', data.sqmId);
  }

  /* .cursorrules tab */
  async function loadCursorRulesData() {
    const { ok, data } = await U.apiFetch('/api/cursor/cursorrules');
    if (!ok || !data) return;
    const ta = document.getElementById('cursorrules-textarea');
    if (ta) ta.value = data.content || '';
  }

  async function saveCursorRules() {
    const content = document.getElementById('cursorrules-textarea')?.value || '';
    const { ok, data } = await U.apiFetch('/api/cursor/cursorrules', { method: 'POST', json: { content } });
    if (ok && data && data.success) U.showToast('Đã lưu file .cursorrules!', 'success');
    else U.showToast('Lỗi lưu .cursorrules', 'error');
  }

  Object.assign(global, {
    closeAllModals,
    openAntidetectModal,
    closeAntidetectModal,
    loadAntidetectConfig,
    setAntidetectMode,
    spoofMachineIDs,
    restoreMachineIDs,
    onSelectAccountFingerprint,
    regenerateSelectedFingerprint,
    applySelectedFingerprint,
    inspectAccountFingerprint,
    openDebugModal,
    closeDebugModal,
    refreshDebugInfo,
    unstickKeys,
    openCleanupModal,
    closeCleanupModal,
    cleanHighUsageAccounts,
    cleanInvalidAccounts,
    openUpdateModal,
    closeUpdateModal,
    fetchUpdateStatus,
    checkUpdates,
    downloadUpdate,
    applyUpdate,
    openCursorSettingsModal,
    closeCursorSettingsModal,
    switchSettingsTab,
    filterSettings,
    setEditorFontPreset,
    loadSettingsData,
    saveGeneralSettings,
    saveModelSettings,
    saveFeatureSettings,
    saveEditorSettings,
    saveIntelligenceSettings,
    saveProxyRoutingSettings,
    saveGitWorktreesSettings,
    testCursorNetwork,
    loadAutoSwitchConfig,
    saveAutoSwitchConfig,
    testSendContinuePrompt,
    loadProfilesData,
    onSelectConfigProfile,
    applyProfileToCurrentCursor,
    applyProfileToAllAccounts,
    loadHardwareData,
    loadCursorRulesData,
    saveCursorRules,
  });
})(window);
