/* Frontend SPA logic for Patent TM Suitability & Savings Estimator */
(() => {
  "use strict";

  let activeJobId = null;
  let activeJobData = null;
  let activeEventSource = null;
  let growthChart = null;
  let bandsChart = null;

  const $ = (id) => document.getElementById(id);

  const fmtInt = (n) => Number(n || 0).toLocaleString("en-US");
  const fmtPct = (p) => `${(Number(p || 0) * 100).toFixed(2)}%`;

  // =========================================================================
  // Initialization
  // =========================================================================
  document.addEventListener("DOMContentLoaded", async () => {
    bindFormEvents();
    bindSettingsModal();
    await refreshSettingsIndicator();
    await refreshJobHistory();
  });

  function closeActiveStream() {
    if (activeEventSource) {
      activeEventSource.close();
      activeEventSource = null;
    }
  }

  // =========================================================================
  // Sidebar & Settings
  // =========================================================================
  async function refreshSettingsIndicator() {
    try {
      const resp = await fetch("/api/settings");
      const data = await resp.json();
      const dot = $("epo-status-dot");
      const lbl = $("epo-status-label");
      const cachedCount = Number(data.cached_patents || 0);
      if (data.has_epo_credentials) {
        dot.classList.remove("fallback");
        lbl.textContent = cachedCount > 0 ? `EPO OPS v3.2 • ${fmtInt(cachedCount)} cached` : "EPO OPS v3.2 Ready";
      } else {
        dot.classList.add("fallback");
        lbl.textContent = "Add EPO Key in ⚙ (Google Ready)";
      }
      const cacheStatsLbl = $("cache-stats-label");
      if (cacheStatsLbl) {
        cacheStatsLbl.textContent = `${fmtInt(cachedCount)} full-text patents & ${fmtInt(data.cached_searches || 0)} searches cached locally`;
      }
      // Populate modal defaults
      $("set-epo-key").value = data.epo_ops_key || "";
      $("set-epo-secret").value = "";
      $("set-secret-hint").textContent = data.epo_ops_secret_masked
        ? `Saved secret: ${data.epo_ops_secret_masked} (leave blank to keep)`
        : "No EPO secret saved yet — paste Consumer Secret above.";
      const testStatusEl = $("epo-test-status");
      if (testStatusEl) testStatusEl.textContent = "";
      $("w-rep").value = data.weight_repetitions;
      $("w-100").value = data.weight_100;
      $("w-95").value = data.weight_95_99;
      $("w-85").value = data.weight_85_94;
    } catch (err) {
      console.error("Failed to load settings:", err);
    }
  }

  function bindSettingsModal() {
    const modal = $("settings-modal");
    $("btn-open-settings").addEventListener("click", () => {
      refreshSettingsIndicator();
      modal.classList.remove("hidden");
    });
    $("btn-close-settings").addEventListener("click", () => modal.classList.add("hidden"));
    $("btn-cancel-settings").addEventListener("click", () => modal.classList.add("hidden"));

    const testEpoBtn = $("btn-test-epo");
    if (testEpoBtn) {
      testEpoBtn.addEventListener("click", async () => {
        const statusEl = $("epo-test-status");
        testEpoBtn.disabled = true;
        testEpoBtn.textContent = "Testing...";
        statusEl.style.color = "#4A475C";
        statusEl.textContent = "Verifying OAuth2 credentials with ops.epo.org...";
        try {
          const resp = await fetch("/api/settings/test-epo", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              epo_ops_key: $("set-epo-key").value.trim(),
              epo_ops_secret: $("set-epo-secret").value.trim(),
            }),
          });
          const data = await resp.json();
          if (resp.ok && data.ok) {
            statusEl.style.color = "#059669";
            statusEl.textContent = `✓ ${data.message}`;
          } else {
            statusEl.style.color = "#DC2626";
            statusEl.textContent = `✗ ${data.error || "Authentication failed."}`;
          }
        } catch (err) {
          statusEl.style.color = "#DC2626";
          statusEl.textContent = `✗ Connection error: ${err}`;
        } finally {
          testEpoBtn.disabled = false;
          testEpoBtn.textContent = "Test EPO Connection";
        }
      });
    }

    const clearCacheBtn = $("btn-clear-cache");
    if (clearCacheBtn) {
      clearCacheBtn.addEventListener("click", async () => {
        clearCacheBtn.disabled = true;
        clearCacheBtn.textContent = "Clearing...";
        try {
          await fetch("/api/cache/clear", { method: "POST" });
          await refreshSettingsIndicator();
        } finally {
          clearCacheBtn.disabled = false;
          clearCacheBtn.textContent = "Clear Cache";
        }
      });
    }

    $("form-settings").addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = {
        epo_ops_key: $("set-epo-key").value.trim(),
        epo_ops_secret: $("set-epo-secret").value.trim(),
        weight_repetitions: parseFloat($("w-rep").value),
        weight_100: parseFloat($("w-100").value),
        weight_95_99: parseFloat($("w-95").value),
        weight_85_94: parseFloat($("w-85").value),
      };
      await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      modal.classList.add("hidden");
      await refreshSettingsIndicator();
    });
  }

  async function refreshJobHistory() {
    try {
      const resp = await fetch("/api/jobs");
      const { jobs } = await resp.json();
      const listEl = $("job-history-list");

      if (!jobs || jobs.length === 0) {
        listEl.innerHTML = `<div class="empty-history">No assessments run yet.</div>`;
        return;
      }

      listEl.innerHTML = jobs
        .map((j) => {
          const isActive = j.id === activeJobId ? "active" : "";
          let pillHtml = "";
          if (j.status === "completed") {
            pillHtml = `<span class="history-saving-pill">${fmtPct(j.projected_tm_saving_pct)}</span>`;
          } else if (j.status === "failed") {
            pillHtml = `<span class="history-saving-pill failed">Failed</span>`;
          } else {
            pillHtml = `<span class="history-saving-pill running">Running</span>`;
          }
          const dtStr = (j.created_at || "").slice(0, 10);
          const countLbl = j.total_patents || j.sample_size;
          return `
            <div class="history-item ${isActive}" data-job-id="${j.id}">
              <div class="history-item-top">
                <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(j.client_name)}</span>
                ${pillHtml}
              </div>
              <div class="history-item-sub">${j.language} &bull; ${countLbl} patents &bull; ${dtStr}</div>
            </div>
          `;
        })
        .join("");

      listEl.querySelectorAll(".history-item").forEach((el) => {
        el.addEventListener("click", () => openJobView(el.dataset.jobId));
      });
    } catch (err) {
      console.error("Error loading history:", err);
    }
  }

  // =========================================================================
  // New Assessment Form & Applicant Verification
  // =========================================================================
  function bindFormEvents() {
    $("btn-new-assessment").addEventListener("click", () => {
      closeActiveStream();
      activeJobId = null;
      $("view-results").classList.add("hidden");
      $("view-new").classList.remove("hidden");
      refreshJobHistory();
    });

    // Mode tabs (Database Search vs File Upload)
    document.querySelectorAll(".mode-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".mode-tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        const isSearch = tab.dataset.tab === "search-tab";
        $("form-create-job").classList.toggle("hidden", !isSearch);
        $("form-upload-job").classList.toggle("hidden", isSearch);
      });
    });

    // Sample size slider + quick chips
    const slider = $("input-sample-size");
    const valLbl = $("sample-size-val");
    slider.addEventListener("input", () => {
      valLbl.textContent = slider.value;
      document.querySelectorAll(".chip-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.count === slider.value);
      });
    });
    document.querySelectorAll(".chip-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        slider.value = btn.dataset.count;
        valLbl.textContent = btn.dataset.count;
        document.querySelectorAll(".chip-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
      });
    });

    // Show demo sector dropdown when demo is selected
    $("select-data-source").addEventListener("change", (e) => {
      $("demo-profile-wrap").classList.toggle("hidden", e.target.value !== "demo");
    });

    // Verify Applicant Entities button
    $("btn-verify-applicants").addEventListener("click", verifyApplicants);
    $("btn-clear-variants").addEventListener("click", () => {
      $("variants-checkboxes").innerHTML = "";
      $("applicant-variants-panel").classList.add("hidden");
    });

    // Submit Search Job
    $("form-create-job").addEventListener("submit", async (e) => {
      e.preventDefault();
      const clientName = $("input-client-name").value.trim();
      if (!clientName) return;

      const selectedVariants = Array.from(
        document.querySelectorAll('input[name="applicant_variant"]:checked')
      ).map((cb) => cb.value);

      const payload = {
        client_name: clientName,
        applicant_variants: selectedVariants,
        sample_size: parseInt($("input-sample-size").value, 10),
        language: $("select-language").value,
        data_source: $("select-data-source").value,
        demo_profile: $("select-demo-profile").value,
        include_rte: $("check-include-rte").checked,
      };

      const btn = $("btn-submit-job");
      btn.disabled = true;
      btn.textContent = "Starting Background Job...";
      try {
        const resp = await fetch("/api/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok) {
          alert(data.error || "Failed to start job.");
          return;
        }
        await refreshJobHistory();
        await openJobView(data.id);
      } finally {
        btn.disabled = false;
        btn.textContent = "Run Rolling TM Simulation";
      }
    });

    // Submit Upload Job
    $("form-upload-job").addEventListener("submit", async (e) => {
      e.preventDefault();
      const files = $("upload-files-input").files;
      if (!files || files.length === 0) return;

      const fd = new FormData();
      fd.append("client_name", $("upload-client-name").value.trim());
      fd.append("include_rte", $("upload-include-rte").checked ? "true" : "false");
      for (const f of files) {
        fd.append("files", f);
      }

      const resp = await fetch("/api/jobs/upload", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok) {
        alert(data.error || "Upload failed.");
        return;
      }
      await refreshJobHistory();
      await openJobView(data.id);
    });

    // Live RTE Toggle on Results Screen
    $("res-rte-toggle").addEventListener("change", async (e) => {
      if (!activeJobId) return;
      const includeRte = e.target.checked;
      const resp = await fetch(`/api/jobs/${activeJobId}/rte`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ include_rte: includeRte }),
      });
      if (resp.ok) {
        activeJobData = await resp.json();
        renderJobResults(activeJobData);
      }
    });

    // Delete Job button
    $("btn-delete-job").addEventListener("click", async () => {
      if (!activeJobId) return;
      if (!confirm("Delete this client assessment from history?")) return;
      closeActiveStream();
      await fetch(`/api/jobs/${activeJobId}`, { method: "DELETE" });
      activeJobId = null;
      $("view-results").classList.add("hidden");
      $("view-new").classList.remove("hidden");
      await refreshJobHistory();
    });
  }

  async function verifyApplicants() {
    const q = $("input-client-name").value.trim();
    if (q.length < 2) {
      alert("Enter at least 2 characters of the client name first.");
      return;
    }
    const lang = $("select-language").value;
    const btn = $("btn-verify-applicants");
    btn.disabled = true;
    btn.textContent = "Searching...";

    try {
      const resp = await fetch(
        `/api/applicants/search?q=${encodeURIComponent(q)}&language=${encodeURIComponent(lang)}`
      );
      const data = await resp.json();
      const panel = $("applicant-variants-panel");
      const grid = $("variants-checkboxes");
      panel.classList.remove("hidden");

      if (!resp.ok || !data.variants || data.variants.length === 0) {
        $("variants-summary-text").textContent = `No distinct applicant variants returned (${data.error || "will search exact name"}).`;
        grid.innerHTML = "";
        return;
      }

      $("variants-summary-text").textContent = `Found ${fmtInt(data.total_found)} publications via ${data.source_used} — select applicant entities to include:`;
      grid.innerHTML = data.variants
        .map(
          (v, idx) => `
          <label class="variant-check">
            <input type="checkbox" name="applicant_variant" value="${escapeHtml(v.name)}" ${idx < 4 ? "checked" : ""} />
            <span>${escapeHtml(v.name)} <small style="color:#64748b;">(${v.hit_count})</small></span>
          </label>
        `
        )
        .join("");
    } catch (err) {
      alert(`Entity lookup error: ${err}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Verify Entities";
    }
  }

  // =========================================================================
  // Results Dashboard & Live SSE Stream
  // =========================================================================
  async function openJobView(jobId) {
    closeActiveStream();
    activeJobId = jobId;
    $("view-new").classList.add("hidden");
    $("view-results").classList.remove("hidden");
    await refreshJobHistory();

    const resp = await fetch(`/api/jobs/${jobId}`);
    if (!resp.ok) return;
    const job = await resp.json();
    activeJobData = job;
    renderJobResults(job);

    if (job.status !== "completed" && job.status !== "failed") {
      activeEventSource = new EventSource(`/api/jobs/${jobId}/events`);
      activeEventSource.onmessage = async (ev) => {
        try {
          const updated = JSON.parse(ev.data);
          activeJobData = updated;
          renderJobResults(updated);
          if (updated.status === "completed" || updated.status === "failed") {
            closeActiveStream();
            await refreshJobHistory();
            await refreshSettingsIndicator();
          }
        } catch (err) {
          console.error("SSE parse error:", err);
        }
      };
      activeEventSource.onerror = () => {
        closeActiveStream();
      };
    }
  }

  function renderJobResults(job) {
    const s = job.summary || {};
    $("res-client-title").textContent = job.client_name;
    const srcLabel = job.data_source_used || job.data_source_requested;
    $("res-meta-line").textContent = `${srcLabel} • ${job.language} • ${job.rows.length || job.sample_size} Patents (1 per Family)`;

    $("res-rte-toggle").checked = Boolean(job.include_rte);
    $("btn-download-report").href = `/api/jobs/${job.id}/report.pdf?include_rte=${Boolean(job.include_rte)}`;

    // Progress vs Error vs Completed banners
    const isRunning = ["queued", "fetching", "analyzing"].includes(job.status);
    $("progress-banner").classList.toggle("hidden", !isRunning);
    if (isRunning) {
      const stageMap = {
        queued: "Queued in background...",
        fetching: "Stage 1/2: Downloading Client Patent Full-Text & Tables...",
        analyzing: "Stage 2/2: Sequential Rolling Trados TM Simulation (Oldest → Newest)...",
      };
      $("progress-stage-title").textContent = stageMap[job.status] || "Processing...";
      $("progress-detail-msg").textContent = job.progress_message || "";
      const pct = job.progress_total > 0 ? Math.min(100, Math.round((job.progress_current / job.progress_total) * 100)) : 5;
      $("progress-pct-label").textContent = `${pct}%`;
      $("progress-bar-fill").style.width = `${Math.max(4, pct)}%`;
    }

    const errBanner = $("error-banner");
    if (job.status === "failed") {
      errBanner.classList.remove("hidden");
      errBanner.textContent = job.error_message || "Analysis failed.";
    } else {
      errBanner.classList.add("hidden");
    }

    // KPI Cards
    $("kpi-cum-tm-saving").textContent = fmtPct(s.projected_cumulative_tm_saving_pct);
    const savedWords = Math.max(0, (s.total_words_studio || 0) - (s.reduced_tm_total || 0));
    $("kpi-cum-tm-sub").textContent = `${fmtInt(Math.round(savedWords))} weighted words saved across ${s.total_patents || 0} patents`;

    $("kpi-mature-saving").textContent = fmtPct(s.mature_tm_saving_pct);
    $("kpi-ramp-sub").textContent = `Starts at ${fmtPct(s.first_patent_saving_pct)} (#1) → reaches ${fmtPct(s.last_patent_saving_pct)} (#${s.total_patents || 0})`;

    if (job.include_rte) {
      $("kpi-rte-saving").textContent = fmtPct(s.projected_cumulative_rte_saving_pct);
      $("kpi-rte-sub").textContent = `ACTIVE: ${fmtInt(s.total_rte_table_numbers)} table ints + ${fmtInt(s.total_rte_nmr_words)} NMR words deducted`;
    } else {
      $("kpi-rte-saving").textContent = "0.00%";
      $("kpi-rte-sub").textContent =
        s.total_rte_words > 0
          ? `Toggle ON to deduct ${fmtInt(s.total_rte_words)} RTE words (${fmtPct(s.potential_rte_saving_pct)})`
          : "No table integers or NMR spans detected";
    }

    const lastTmSegs = job.rows.length > 0 ? job.rows[job.rows.length - 1].tm_size_segments_after : 0;
    $("kpi-total-words").textContent = fmtInt(s.total_words_studio);
    $("kpi-words-sub").textContent = `Studio words (${fmtInt(s.total_words_word)} Word) • ${fmtInt(lastTmSegs)} unique TM segments`;

    renderCharts(job.rows || []);
    renderDataTable(job);
  }

  // =========================================================================
  // Interactive Charts (Chart.js)
  // =========================================================================
  function renderCharts(rows) {
    const labels = rows.map((r) => `#${r.sequence_index} ${r.file_ref}`);
    const perPatentPct = rows.map((r) => Number((r.pct_saving * 100).toFixed(2)));
    const cumulPct = rows.map((r) => Number((r.cumulative_pct_saving * 100).toFixed(2)));

    // 1. Growth Curve Chart (RWS Brand Palette: Hillier Pink curve + Hidcote Blue bars)
    const ctxGrowth = $("chart-growth-curve").getContext("2d");
    if (growthChart) growthChart.destroy();
    growthChart = new Chart(ctxGrowth, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            type: "line",
            label: "Rolling Cumulative TM Saving %",
            data: cumulPct,
            borderColor: "#E60050",
            backgroundColor: "rgba(230, 0, 80, 0.10)",
            pointBackgroundColor: "#38006B",
            pointBorderColor: "#FFFFFF",
            pointBorderWidth: 1.5,
            borderWidth: 3,
            pointRadius: 3.5,
            tension: 0.32,
            yAxisID: "y",
          },
          {
            type: "bar",
            label: "Individual Patent TM Saving %",
            data: perPatentPct,
            backgroundColor: "rgba(123, 92, 245, 0.72)",
            borderColor: "#7B5CF5",
            borderWidth: 1,
            borderRadius: 5,
            yAxisID: "y",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", labels: { font: { family: "'Inter', sans-serif", weight: "600" } } },
          tooltip: {
            backgroundColor: "#240047",
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)}%`,
            },
          },
        },
        scales: {
          x: { ticks: { maxRotation: 45, minRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
          y: {
            beginAtZero: true,
            title: { display: true, text: "% TM Saving", color: "#38006B", font: { weight: "700" } },
            ticks: { callback: (v) => `${v}%` },
          },
        },
      },
    });

    // 2. Stacked Match-Band Share Chart (RWS Spectrum: Galaxy Purple, Hillier Pink, Hidcote Blue, Lavender, Neutral)
    const toShare = (r, val) => (r.words_studio > 0 ? Number(((val / r.words_studio) * 100).toFixed(1)) : 0);

    const ctxBands = $("chart-match-bands").getContext("2d");
    if (bandsChart) bandsChart.destroy();
    bandsChart = new Chart(ctxBands, {
      type: "bar",
      data: {
        labels,
        datasets: [
          { label: "100% TM Match", data: rows.map((r) => toShare(r, r.match_100)), backgroundColor: "#38006B" },
          { label: "Repetitions", data: rows.map((r) => toShare(r, r.repetitions)), backgroundColor: "#E60050" },
          { label: "95%-99%", data: rows.map((r) => toShare(r, r.match_95_99)), backgroundColor: "#7B5CF5" },
          { label: "85%-94%", data: rows.map((r) => toShare(r, r.match_85_94)), backgroundColor: "#B79EFF" },
          {
            label: "50%-84% (Full Rate)",
            data: rows.map((r) => toShare(r, r.match_75_84 + r.match_50_74)),
            backgroundColor: "#9E9BB0",
          },
          { label: "No Match (<50%)", data: rows.map((r) => toShare(r, r.no_match)), backgroundColor: "#DCDAE6" },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", labels: { boxWidth: 11, font: { family: "'Inter', sans-serif", size: 11, weight: "600" } } },
          tooltip: { backgroundColor: "#240047" },
        },
        scales: {
          x: { stacked: true, ticks: { autoSkip: true, maxTicksLimit: 12 } },
          y: {
            stacked: true,
            max: 100,
            beginAtZero: true,
            title: { display: true, text: "% of Patent Words", color: "#38006B", font: { weight: "700" } },
            ticks: { callback: (v) => `${v}%` },
          },
        },
      },
    });
  }

  // =========================================================================
  // Sequential Patent Breakdown Table
  // =========================================================================
  function renderDataTable(job) {
    const rows = job.rows || [];
    const s = job.summary || {};
    const tbody = $("results-table-body");
    const tfoot = $("results-table-foot");

    tbody.innerHTML = rows
      .map((r) => {
        const refLink = r.source_url
          ? `<a href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener" title="${escapeHtml(r.title)}">${escapeHtml(r.file_ref)}</a>`
          : escapeHtml(r.file_ref);

        const rteWords = r.rte ? r.rte.total_rte_words : 0;
        let rteHtml = `<span style="color:#94a3b8;">0</span>`;
        if (rteWords > 0) {
          const tip = `Table whole numbers: ${r.rte.table_whole_numbers} | NMR words: ${r.rte.nmr_words}`;
          rteHtml = `<span class="rte-chip" title="${tip}">${fmtInt(rteWords)}</span>`;
        }

        return `
          <tr>
            <td>${r.sequence_index}</td>
            <td><strong>${refLink}</strong></td>
            <td>${escapeHtml(r.pub_date || "—")}</td>
            <td class="num">${fmtInt(r.words_word)}</td>
            <td class="num">${fmtInt(r.words_studio)}</td>
            <td class="num">${fmtInt(r.repetitions)}</td>
            <td class="num">${fmtInt(r.match_100)}</td>
            <td class="num">${fmtInt(r.match_95_99)}</td>
            <td class="num">${fmtInt(r.match_85_94)}</td>
            <td class="num">${fmtInt(r.match_75_84)}</td>
            <td class="num">${fmtInt(r.match_50_74)}</td>
            <td class="num">${fmtInt(r.no_match)}</td>
            <td class="num">${fmtInt(Math.round(r.tm_words))}</td>
            <td class="num"><span class="saving-badge">${fmtPct(r.pct_saving)}</span></td>
            <td class="num"><strong>${fmtPct(r.cumulative_pct_saving)}</strong></td>
            <td class="num">${rteHtml}</td>
          </tr>
        `;
      })
      .join("");

    if (rows.length > 0) {
      tfoot.innerHTML = `
        <tr>
          <td colspan="3">TOTAL (${rows.length} Patents)</td>
          <td class="num">${fmtInt(s.total_words_word)}</td>
          <td class="num">${fmtInt(s.total_words_studio)}</td>
          <td class="num">${fmtInt(s.total_repetitions)}</td>
          <td class="num">${fmtInt(s.total_match_100)}</td>
          <td class="num">${fmtInt(s.total_match_95_99)}</td>
          <td class="num">${fmtInt(s.total_match_85_94)}</td>
          <td class="num">${fmtInt(s.total_match_75_84)}</td>
          <td class="num">${fmtInt(s.total_match_50_74)}</td>
          <td class="num">${fmtInt(s.total_no_match)}</td>
          <td class="num">${fmtInt(Math.round(s.reduced_tm_total))}</td>
          <td class="num"><span class="saving-badge">${fmtPct(s.projected_cumulative_tm_saving_pct)}</span></td>
          <td class="num">${fmtPct(s.projected_cumulative_tm_saving_pct)}</td>
          <td class="num">${fmtInt(s.total_rte_words)}</td>
        </tr>
      `;
    } else {
      tfoot.innerHTML = "";
    }
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
})();

