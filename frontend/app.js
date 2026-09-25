(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    ollamaStatus: $("ollamaStatus"),
    ollamaStatusText: $("ollamaStatusText"),
    mTotalTps: $("mTotalTps"),
    mTotalSub: $("mTotalSub"),
    mAgentTps: $("mAgentTps"),
    mAgentSub: $("mAgentSub"),
    mTokens: $("mTokens"),
    mTokensSub: $("mTokensSub"),
    mClients: $("mClients"),
    mClientsSub: $("mClientsSub"),
    mGpu: $("mGpu"),
    mGpuSub: $("mGpuSub"),
    agentTpsBars: $("agentTpsBars"),
    chartWindowSec: $("chartWindowSec"),
    modelSelect: $("modelSelect"),
    promptInput: $("promptInput"),
    numPredict: $("numPredict"),
    temperature: $("temperature"),
    runBtn: $("runBtn"),
    refreshBtn: $("refreshBtn"),
    streamMeta: $("streamMeta"),
    streamTokens: $("streamTokens"),
    streamOut: $("streamOut"),
    modelCount: $("modelCount"),
    runningCount: $("runningCount"),
    modelsBody: $("modelsBody"),
    runningBody: $("runningBody"),
    historyBody: $("historyBody"),
    observedGrid: $("observedGrid"),
    clientsBody: $("clientsBody"),
    wsState: $("wsState"),
  };

  let tpsChart;
  let ttftChart;
  let liveChart;
  let selectedModel = localStorage.getItem("llmperf.model") || "";
  let running = false;
  let lastHistory = [];

  const AGENT_PALETTE = [
    "#c45c26", "#3d7ea6", "#7a5cbf", "#b45309", "#0e7490", "#a16207",
  ];
  /** @type {{ t: number, total: number, agents: Record<string, number> }[]} */
  const liveSamples = [];
  const LIVE_KEEP_MAX_SEC = 1800; // bellekte en fazla 30 dk

  function getWindowSec() {
    const raw = Number(els.chartWindowSec?.value);
    if (!Number.isFinite(raw)) return 60;
    return Math.min(900, Math.max(10, Math.round(raw)));
  }

  function setWindowSec(sec) {
    const v = Math.min(900, Math.max(10, Math.round(sec)));
    if (els.chartWindowSec) els.chartWindowSec.value = String(v);
    localStorage.setItem("llmperf.chartWindowSec", String(v));
  }

  function fmtBytes(n) {
    if (n == null || Number.isNaN(n)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n);
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i += 1;
    }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function fmtNum(v, digits = 1) {
    if (v == null || Number.isNaN(v)) return "—";
    return Number(v).toFixed(digits);
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleTimeString();
  }

  function setOllamaStatus(ollama) {
    if (!ollama) {
      els.ollamaStatus.dataset.state = "unknown";
      els.ollamaStatusText.textContent = "bilinmiyor";
      return;
    }
    if (ollama.ok) {
      els.ollamaStatus.dataset.state = "ok";
      els.ollamaStatusText.textContent = `ollama · ${ollama.url || "ok"}`;
    } else {
      els.ollamaStatus.dataset.state = "bad";
      els.ollamaStatusText.textContent = ollama.error || "ulaşılamıyor";
    }
  }

  function fillModels(models) {
    const names = (models || []).map((m) => m.name || m.model).filter(Boolean);
    const current = selectedModel && names.includes(selectedModel)
      ? selectedModel
      : names[0] || "";

    els.modelSelect.innerHTML = names
      .map((n) => `<option value="${n}">${n}</option>`)
      .join("");
    if (current) {
      els.modelSelect.value = current;
      selectedModel = current;
    }
    els.modelCount.textContent = `${names.length} model`;

    els.modelsBody.innerHTML = (models || [])
      .map((m) => {
        const d = m.details || {};
        return `<tr>
          <td>${m.name || m.model}</td>
          <td class="mono">${fmtBytes(m.size)}</td>
          <td>${d.family || "—"}</td>
          <td class="mono">${d.quantization_level || "—"}</td>
        </tr>`;
      })
      .join("") || `<tr><td colspan="4">Model yok</td></tr>`;
  }

  function fillRunning(runningModels) {
    const list = runningModels || [];
    els.runningCount.textContent = `${list.length} yüklü`;
    els.runningBody.innerHTML = list
      .map((m) => {
        const name = m.name || m.model || "—";
        const size = m.size_vram ?? m.size;
        const expires = m.expires_at
          ? new Date(m.expires_at).toLocaleTimeString()
          : "—";
        return `<tr>
          <td>${name}</td>
          <td class="mono">${fmtBytes(size)}</td>
          <td class="mono">${expires}</td>
        </tr>`;
      })
      .join("") || `<tr><td colspan="3">Şu an bellekde model yok</td></tr>`;
  }

  function fillThroughput(throughput, live) {
    const total = throughput?.total_tps ?? live?.total_tps;
    const agentMap = throughput?.agent_tps || live?.agent_tps || {};
    const entries = Object.entries(agentMap).sort((a, b) => b[1] - a[1]);
    const primary = entries[0];

    els.mTotalTps.textContent = fmtNum(total);
    els.mTotalSub.textContent = entries.length
      ? `${entries.length} ajan · Σ`
      : "tüm ajanlar";

    els.mAgentTps.textContent = primary ? fmtNum(primary[1]) : "—";
    els.mAgentSub.textContent = primary ? primary[0] : "birincil ajan";

    els.mTokens.textContent = String(throughput?.active_tokens ?? live?.tokens ?? 0);
    els.mTokensSub.textContent = live?.model ? live.model : "bu tur";

    pushLiveSample(throughput, live);

    if (!entries.length) {
      els.agentTpsBars.innerHTML =
        `<div class="agent-tps-empty">Aktif çıkarım yok — ajan üretince tok/s burada toplanır.</div>`;
      return;
    }
    const max = Math.max(...entries.map(([, v]) => v), 1);
    els.agentTpsBars.innerHTML = entries
      .filter(([, tps]) => tps > 0)
      .map(([name, tps]) => {
        const pct = Math.max(4, Math.round((tps / max) * 100));
        return `<div class="agent-tps-row">
          <div class="name">${name}</div>
          <div class="bar"><span style="width:${pct}%"></span></div>
          <div class="val">${fmtNum(tps)} t/s</div>
        </div>`;
      })
      .join("");
  }

  function ensureLiveChart() {
    if (liveChart || typeof Chart === "undefined") return;
    const canvas = $("liveTotalChart");
    if (!canvas) return;

    liveChart = new Chart(canvas, {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          label: "toplam",
          data: [],
          borderColor: "#0f7a5f",
          backgroundColor: "rgba(15,122,95,0.14)",
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
          order: 0,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: true,
            position: "top",
            align: "end",
            labels: {
              color: "#5a6554",
              boxWidth: 10,
              boxHeight: 10,
              font: { size: 11, family: "'IBM Plex Mono', monospace" },
            },
          },
          tooltip: {
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${Number(ctx.parsed.y).toFixed(1)} t/s`,
            },
          },
        },
        scales: {
          x: {
            ticks: {
              color: "#5a6554",
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: 6,
              font: { size: 10 },
            },
            grid: { color: "rgba(26,33,24,0.05)" },
          },
          y: {
            beginAtZero: true,
            suggestedMax: 10,
            ticks: {
              color: "#5a6554",
              font: { size: 10 },
              callback: (v) => `${v}`,
            },
            grid: { color: "rgba(26,33,24,0.08)" },
            title: {
              display: true,
              text: "tok/s",
              color: "#5a6554",
              font: { size: 11 },
            },
          },
        },
      },
    });
  }

  function agentColor(name, index) {
    let hash = 0;
    for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) | 0;
    return AGENT_PALETTE[Math.abs(hash + index) % AGENT_PALETTE.length];
  }

  function syncLiveDatasets() {
    if (!liveChart) return;
    const windowSec = getWindowSec();
    const cutoff = Date.now() / 1000 - windowSec;
    const view = liveSamples.filter((s) => s.t >= cutoff);
    const labels = view.map((s) => new Date(s.t * 1000).toLocaleTimeString());
    const totals = view.map((s) => s.total);
    const agentNames = new Set();
    view.forEach((s) => Object.keys(s.agents || {}).forEach((n) => agentNames.add(n)));

    const datasets = [{
      label: "toplam",
      data: totals,
      borderColor: "#0f7a5f",
      backgroundColor: "rgba(15,122,95,0.14)",
      fill: true,
      tension: 0.3,
      pointRadius: 0,
      borderWidth: 2.5,
      order: 0,
    }];

    [...agentNames].sort().forEach((name, i) => {
      const series = view.map((s) => Number(s.agents?.[name]) || 0);
      if (!series.some((v) => v > 0)) return;
      datasets.push({
        label: name,
        data: series,
        borderColor: agentColor(name, i),
        backgroundColor: "transparent",
        fill: false,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 1.5,
        borderDash: [4, 3],
        order: 1,
      });
    });

    liveChart.data.labels = labels;
    liveChart.data.datasets = datasets;
    const peak = Math.max(10, ...totals, 0);
    liveChart.options.scales.y.suggestedMax = Math.ceil(peak * 1.15);
    liveChart.update("none");
  }

  function pushLiveSample(throughput, live) {
    ensureLiveChart();
    if (!liveChart) return;

    const total = Number(throughput?.total_tps ?? live?.total_tps ?? 0) || 0;
    const agentMap = throughput?.agent_tps || live?.agent_tps || {};
    const now = Date.now() / 1000;

    liveSamples.push({
      t: now,
      total,
      agents: { ...agentMap },
    });

    const keepCutoff = now - LIVE_KEEP_MAX_SEC;
    while (liveSamples.length && liveSamples[0].t < keepCutoff) {
      liveSamples.shift();
    }

    syncLiveDatasets();
  }

  function fillObserved(observed) {
    const list = observed || [];
    if (!list.length) {
      els.observedGrid.innerHTML =
        `<div class="session-card empty">Gözlenen runner yok — bir ajan Ollama kullanınca burada görünür.</div>`;
      return;
    }

    els.observedGrid.innerHTML = list
      .map((o) => {
        const status = o.inferring ? "inferring" : o.status || "loaded";
        const tps = o.avg_tps ?? o.live_tps;
        const ttl = o.expires_in_sec != null ? ` · ttl ${fmtNum(o.expires_in_sec, 0)}s` : "";
        const pid = o.pid != null ? `pid ${o.pid}` : "resident";
        return `<article class="session-card" data-status="${status}">
          <div class="session-top">
            <span class="session-agent">${o.agent ? `${o.agent} · ` : ""}${o.model || "?"}</span>
            <span class="session-tps">${o.inferring ? fmtNum(tps) + " t/s" : "—"}</span>
          </div>
          <div class="session-meta">${status} · ${o.tokens || 0} tok · ${pid}${ttl}</div>
          <div class="session-partial">cpu ${fmtNum(o.cpu_pct, 1)}% · ${fmtBytes(o.size_vram || o.rss_bytes)}</div>
        </article>`;
      })
      .join("");
  }

  function fillGpu(gpus) {
    const list = gpus || [];
    if (!list.length) {
      els.mGpu.textContent = "—";
      els.mGpuSub.textContent = "gpu yok";
      return;
    }
    // Prefer GPU with VRAM (discrete) that has highest util, else first
    const ranked = [...list].sort(
      (a, b) => (b.util_pct || 0) - (a.util_pct || 0) || (b.mem_total || 0) - (a.mem_total || 0)
    );
    const g = ranked[0];
    els.mGpu.textContent = `${fmtNum(g.util_pct, 0)}%`;
    const mem =
      g.mem_used != null && g.mem_total
        ? ` · ${fmtBytes(g.mem_used)}/${fmtBytes(g.mem_total)}`
        : "";
    els.mGpuSub.textContent = `${g.name || g.vendor || "gpu"}${mem}`;
  }

  function fillClients(clients) {
    const list = clients || [];
    els.mClients.textContent = String(list.length);
    els.mClientsSub.textContent = list.map((c) => c.agent).filter(Boolean).slice(0, 3).join(", ") || "tcp :11434";
    els.clientsBody.innerHTML = list
      .map((c) => `<tr>
        <td>${c.agent || "—"}</td>
        <td class="mono">${c.pid ?? "—"}</td>
        <td class="mono">${c.comm || "—"}</td>
        <td class="mono">${(c.cmdline || "").slice(0, 60)}</td>
      </tr>`)
      .join("") || `<tr><td colspan="4">Bağlı istemci yok</td></tr>`;
  }

  function fillActivity(activity) {
    // Activity tags removed from Bağlı istemciler panel
  }

  function fillHistory(history) {
    const rows = history || [];
    els.historyBody.innerHTML = rows
      .map((h) => `<tr>
        <td class="mono">${fmtTime(h.timestamp)}</td>
        <td><span class="badge">${h.agent || "—"}</span></td>
        <td>${h.model}</td>
        <td class="mono">${h.prompt_tokens ?? "—"}</td>
        <td class="mono">${h.completion_tokens ?? "—"}</td>
        <td class="mono">${fmtNum(h.completion_tps)}</td>
        <td class="mono">${h.source || "—"}</td>
        <td class="mono">${fmtNum(h.wall_ms, 0)} ms</td>
      </tr>`)
      .join("") || `<tr><td colspan="8">Henüz ölçüm yok</td></tr>`;
  }

  function updateHero(snapshot) {
    const live = snapshot.live || {};
    const bench = (snapshot.sessions || []).find((s) => s.source === "benchmark");
    if (bench) {
      els.streamOut.textContent = bench.partial || "";
      els.streamTokens.textContent = `${bench.tokens || 0} tok`;
      els.streamMeta.textContent = `ölçüm · ${bench.model || ""} · ${fmtNum(bench.live_tps)} t/s`;
    } else if (live.status === "running") {
      els.streamMeta.textContent = `canlı · ${live.agent || ""} · ${live.model || ""} · ${fmtNum(live.live_tps)} t/s`;
      els.streamTokens.textContent = `${live.tokens || 0} tok`;
    }
  }

  function ensureCharts() {
    if (tpsChart || typeof Chart === "undefined") return;
    const common = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: {
          ticks: { color: "#5a6554", maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
          grid: { color: "rgba(26,33,24,0.06)" },
        },
        y: {
          beginAtZero: true,
          ticks: { color: "#5a6554" },
          grid: { color: "rgba(26,33,24,0.08)" },
        },
      },
    };

    tpsChart = new Chart($("tpsChart"), {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          data: [],
          borderColor: "#0f7a5f",
          backgroundColor: "rgba(15,122,95,0.12)",
          fill: true,
          tension: 0.35,
          pointRadius: 3,
          pointBackgroundColor: "#0f7a5f",
        }],
      },
      options: { ...common, scales: { ...common.scales, y: { ...common.scales.y, title: { display: true, text: "tok/s", color: "#5a6554" } } } },
    });

    ttftChart = new Chart($("ttftChart"), {
      type: "bar",
      data: {
        labels: [],
        datasets: [{
          data: [],
          backgroundColor: "rgba(196,92,38,0.55)",
          borderRadius: 6,
        }],
      },
      options: { ...common, scales: { ...common.scales, y: { ...common.scales.y, title: { display: true, text: "tokens", color: "#5a6554" } } } },
    });
  }

  function updateCharts(history) {
    ensureCharts();
    if (!tpsChart) return;
    lastHistory = history || [];
    const windowSec = getWindowSec();
    const cutoff = Date.now() / 1000 - windowSec;
    let rows = [...lastHistory].reverse();
    // Zaman penceresine giren koşular; yoksa son kayıtlara düş
    const inWindow = rows.filter((h) => (h.timestamp || 0) >= cutoff);
    rows = (inWindow.length ? inWindow : rows).slice(-40);
    const labels = rows.map((h) => fmtTime(h.timestamp));
    tpsChart.data.labels = labels;
    tpsChart.data.datasets[0].data = rows.map((h) => h.completion_tps ?? null);
    tpsChart.update("none");
    ttftChart.data.labels = labels;
    ttftChart.data.datasets[0].data = rows.map((h) => h.completion_tokens ?? null);
    ttftChart.update("none");
  }

  function applySnapshot(snapshot) {
    setOllamaStatus(snapshot.ollama);
    fillModels(snapshot.models);
    fillRunning(snapshot.running);
    fillThroughput(snapshot.throughput, snapshot.live);
    fillObserved(snapshot.observed);
    fillGpu(snapshot.gpus);
    fillClients(snapshot.clients);
    fillActivity(snapshot.activity);
    fillHistory(snapshot.history);
    updateHero(snapshot);
    updateCharts(snapshot.history);
  }

  async function refreshOnce() {
    const res = await fetch("/api/snapshot");
    if (!res.ok) throw new Error(await res.text());
    applySnapshot(await res.json());
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    els.wsState.textContent = "ws: bağlanıyor";

    ws.onopen = () => {
      els.wsState.textContent = "ws: bağlı";
    };
    ws.onclose = () => {
      els.wsState.textContent = "ws: koptu · yeniden…";
      setTimeout(connectWs, 2000);
    };
    ws.onerror = () => {
      els.wsState.textContent = "ws: hata";
    };
    ws.onmessage = (ev) => {
      try {
        applySnapshot(JSON.parse(ev.data));
      } catch {
        /* ignore */
      }
    };

    setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, 20000);
  }

  async function runBenchmark() {
    if (running) return;
    const model = els.modelSelect.value;
    if (!model) return;

    running = true;
    els.runBtn.disabled = true;
    els.streamOut.textContent = "";
    els.streamMeta.textContent = `ölçülüyor · ${model}`;
    els.streamTokens.textContent = "0 tok";

    try {
      const res = await fetch("/api/benchmark", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model,
          prompt: els.promptInput.value,
          num_predict: Number(els.numPredict.value) || 128,
          temperature: Number(els.temperature.value) || 0.2,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      els.streamOut.textContent = data.response || "";
      els.streamMeta.textContent = `bitti · ${model}`;
      els.streamTokens.textContent = `${data.metrics?.completion_tokens ?? 0} tok`;
    } catch (err) {
      els.streamMeta.textContent = "hata";
      els.streamOut.textContent = String(err.message || err);
    } finally {
      running = false;
      els.runBtn.disabled = false;
    }
  }

  els.modelSelect.addEventListener("change", () => {
    selectedModel = els.modelSelect.value;
    localStorage.setItem("llmperf.model", selectedModel);
  });
  els.runBtn.addEventListener("click", runBenchmark);
  els.refreshBtn.addEventListener("click", () => {
    refreshOnce().catch((e) => {
      els.streamOut.textContent = String(e.message || e);
    });
  });

  const savedWindow = Number(localStorage.getItem("llmperf.chartWindowSec"));
  setWindowSec(Number.isFinite(savedWindow) ? savedWindow : 60);
  els.chartWindowSec?.addEventListener("change", () => {
    setWindowSec(getWindowSec());
    syncLiveDatasets();
    updateCharts(lastHistory);
  });
  els.chartWindowSec?.addEventListener("input", () => {
    // Anlık önizleme; aşırı sık clamp etmeden
    const v = Number(els.chartWindowSec.value);
    if (Number.isFinite(v) && v >= 10 && v <= 900) {
      localStorage.setItem("llmperf.chartWindowSec", String(Math.round(v)));
      syncLiveDatasets();
      updateCharts(lastHistory);
    }
  });

  refreshOnce().catch(() => {});
  connectWs();
  setTimeout(() => {
    ensureCharts();
    ensureLiveChart();
  }, 300);
})();
