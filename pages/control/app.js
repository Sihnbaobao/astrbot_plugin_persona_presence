// Persona Presence — 插件页控制台（分组 tab 分页版）
const PLUGIN_NAME = "astrbot_plugin_persona_presence";
const BRIDGE_SDK = "/api/plugin/page/bridge-sdk.js";

let bridge = window.AstrBotPluginPage || null;

/** 面板没注入 bridge 时自己加载一次 SDK。 */
async function ensureBridge() {
  if (bridge) return bridge;
  await new Promise((resolve) => {
    const tag = document.createElement("script");
    tag.src = BRIDGE_SDK;
    tag.onload = resolve;
    tag.onerror = resolve;
    document.head.appendChild(tag);
  });
  bridge = window.AstrBotPluginPage || null;
  return bridge;
}

/** 直连插件 Web API（没有 bridge 时的退路）。 */
async function httpCall(method, path, body) {
  const token = localStorage.getItem("token") || "";
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  const res = await fetch("/api/plug/" + PLUGIN_NAME + "/" + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch (e) {
    return { status: "error", message: text || ("HTTP " + res.status) };
  }
}

const api = {
  async get(path, params) {
    const query = params ? "?" + new URLSearchParams(params).toString() : "";
    if (bridge) return bridge.apiGet(path, params);
    return httpCall("GET", path + query, null);
  },
  async post(path, body) {
    if (bridge) return bridge.apiPost(path, body);
    return httpCall("POST", path, body);
  },
};
const state = { values: {}, runtime: {}, version: "", groups: [], loadError: "" };
let activeIdx = 0;       // 当前显示的分组下标
let configDirty = false; // 当前面板是否有未保存改动

const $ = (sel) => document.querySelector(sel);
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function stripEmoji(s) {
  const m = String(s ?? "").match(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}][\u{FE0F}]?/u);
  return (m ? String(s).replace(m[0], "") : String(s ?? "")).trim();
}

/* ============ 分组主开关（卡片徽章判定） ============ */
const MASTERS = {
  presence_basic: "enable_group_chat",
  presence_private: "enable_private_chat",
  presence_decision: null,
  presence_trigger: null,
  presence_reply: null,
  presence_manage: { any: ["enable_user_blacklist", "enable_command_filter"] },
  presence_concurrent: null,
  presence_enhance: { any: ["enable_image_processing", "enable_memory_injection", "enable_emoji_filter"] },
  presence_cross_group: "enable_cross_group_sight",
  dlc_notebook: "enable_notebook",
  dlc_curiosity: "enable_curiosity",
  dlc_proactive: "enable_proactive",
};
function isMasterOn(master) {
  if (!master) return true;
  if (typeof master === "string") return !!state.values[master];
  if (master.any) return master.any.some((k) => !!state.values[k]);
  if (master.neq) return state.values[master.key] !== master.neq;
  return true;
}

/* ============ 分组摘要 ============ */
function groupSummary(gid) {
  const v = state.values;
  switch (gid) {
    case "presence_basic": return `总开关${v.enable_group_chat ? "开" : "关"} · ${(v.enabled_groups || []).length} 个群`;
    case "presence_private": return `${v.enable_private_chat ? "私聊已启用" : "私聊关闭"} · ${v.private_reply_mode ?? "direct"}`;
    case "presence_decision": return `随机${v.enable_random_probability_filter ? "开" : "AI主导"} · 倾向 ${v.decision_ai_reply_tendency ?? "persona"}`;
    case "presence_trigger": return `触发词 ${(v.trigger_keywords || []).length} 条`;
    case "presence_reply": return `上下文 ${v.max_context_messages ?? "-"} 条 · 防复读${v.enable_duplicate_filter ? "开" : "关"}`;
    case "presence_manage": return `黑名单 ${(v.blacklist_user_ids || []).length} 人`;
    case "presence_concurrent": return `${v.concurrent_mode ?? "legacy"} 模式`;
    case "presence_enhance": return "图片 / 记忆 / 表情 / 戳一戳";
    case "presence_cross_group": return v.enable_cross_group_sight ? "跨群见闻 · 开" : "跨群见闻 · 关";
    case "dlc_notebook": return v.enable_notebook ? "资料片已装载" : "资料片未装载";
    case "dlc_curiosity": return v.enable_notebook
      ? (v.enable_curiosity ? "资料片已装载 · 依赖记忆本" : "资料片未装载 · 需记忆本")
      : "需先开启记忆本";
    case "dlc_proactive": return v.enable_proactive ? "资料片已装载" : "资料片未装载";
    default: return "";
  }
}

/* ============ 字段元数据 ============ */
const META = {};
function buildMeta(groups) {
  Object.keys(META).forEach((k) => delete META[k]);
  (groups || []).forEach((g) => {
    Object.entries(g.items || {}).forEach(([k, m]) => {
      META[k] = { label: m.description || k, type: m.type || "string", options: m.options || null, hint: m.hint || "" };
    });
  });
}


/* ============ 记忆网络 ============ */
const NET = { nodes: [], edges: [], data: null, active: null, hover: null, raf: 0 };
function kindColor(kind) {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const light = {
    like: "#e887ab", dislike: "#d4575f", doing: "#3f8fd4",
    person: "#d9a521", curious: "#8a63d2", knows: "#2f9e64",
  };
  const dimmed = {
    like: "#f0a3c0", dislike: "#e57b82", doing: "#6aaae0",
    person: "#e8bd4d", curious: "#a583e3", knows: "#5cc48c",
  };
  const palette = dark ? dimmed : light;
  return palette[kind] || "#8a94a6";
}

async function loadNotebook() {
  const svg = document.getElementById("netSvg");
  if (!svg) return;
  try {
    const res = await api.get("notebook");
    NET.data = res && res.data !== undefined ? res.data : res;
  } catch (err) {
    svg.innerHTML = '<text x="450" y="260" text-anchor="middle" class="net-hint">读取失败</text>';
    return;
  }
  drawNetwork();
}

/* 记忆网络：持续运行的力导向图，不是一次性布局 */
function netLayoutSeed(data) {
  const CX = 450, CY = 250, RING = 200;
  const byKind = {};
  data.nodes.forEach((n) => {
    (byKind[n.kind] = byKind[n.kind] || []).push(n);
  });
  const kinds = Object.keys(byKind);
  const nodes = [];
  kinds.forEach((kind, ki) => {
    const group = byKind[kind];
    const base = (Math.PI * 2 * ki) / kinds.length - Math.PI / 2;
    group.forEach((node, ni) => {
      const spread = group.length > 1 ? (ni - (group.length - 1) / 2) * 0.45 : 0;
      const angle = base + spread;
      const radius = RING + (ni % 2 ? 22 : 0);
      nodes.push({
        id: node.id,
        data: node,
        x: CX + Math.cos(angle) * radius,
        y: CY + Math.sin(angle) * radius * 0.86,
        vx: 0,
        vy: 0,
        r: 11 + Math.min(16, (node.weight || 0.5) * 18),
      });
    });
  });
  return { nodes, kinds, byKind, CX, CY };
}

function drawNetwork() {
  const svg = document.getElementById("netSvg");
  const legend = document.getElementById("netLegend");
  const side = document.getElementById("netSide");
  if (!svg) return;

  const data = NET.data || { nodes: [], edges: [], total: 0 };
  if (NET.raf) {
    cancelAnimationFrame(NET.raf);
    NET.raf = 0;
  }

  if (!data.nodes || !data.nodes.length) {
    svg.innerHTML =
      '<circle cx="450" cy="250" r="34" class="net-core" opacity="0.5"/>' +
      '<text x="450" y="256" text-anchor="middle" class="net-core-label" opacity="0.6">人格</text>';
    if (side) {
      side.innerHTML =
        '<div class="empty-state"><span class="icon">◎</span>' +
        "<strong>还没有任何节点</strong>" +
        "<span>在「资料片」里开启记忆本，它会开始记下自己在意的事。</span></div>";
    }
    if (legend) legend.innerHTML = "";
    return;
  }

  const seed = netLayoutSeed(data);
  const { nodes, kinds, byKind, CX, CY } = seed;
  const index = {};
  nodes.forEach((n) => { index[n.id] = n; });
  const edges = (data.edges || []).filter((e) => index[e.from] && index[e.to]);

  // 静态骨架：连线和节点各建一次，之后只改坐标。
  const ns = "http://www.w3.org/2000/svg";
  svg.innerHTML = "";
  const edgeEls = edges.map((e) => {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke-linecap", "round");
    svg.appendChild(path);
    return { el: path, from: e.from, to: e.to };
  });

  const nodeEls = nodes.map((n) => {
    const g = document.createElementNS(ns, "g");
    g.setAttribute("class", "net-node");
    g.dataset.id = n.id;
    const halo = document.createElementNS(ns, "circle");
    halo.setAttribute("fill", kindColor(n.data.kind));
    halo.setAttribute("opacity", "0.14");
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("fill", kindColor(n.data.kind));
    const label = document.createElementNS(ns, "text");
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("font-size", "12");
    label.textContent = n.data.label;
    g.append(halo, dot, label);
    svg.appendChild(g);
    g.addEventListener("click", () => {
      NET.active = NET.active === n.id ? null : n.id;
      applyNetHighlight();
      renderNetSide();
    });
    g.addEventListener("mouseenter", () => {
      NET.hover = n.id;
      applyNetHighlight();
    });
    g.addEventListener("mouseleave", () => {
      NET.hover = null;
      applyNetHighlight();
    });
    return { g, halo, dot, label, node: n };
  });

  const core = document.createElementNS(ns, "g");
  core.innerHTML =
    '<circle class="net-core" cx="' + CX + '" cy="' + CY + '" r="34"/>' +
    '<circle class="net-core-ring" cx="' + CX + '" cy="' + CY + '" r="34"/>' +
    '<text class="net-core-label" x="' + CX + '" y="' + (CY + 5) + '" text-anchor="middle">人格</text>';
  svg.appendChild(core);

  function applyNetHighlight() {
    const focus = NET.hover || NET.active;
    const linked = new Set();
    if (focus) {
      edges.forEach((e) => {
        if (e.from === focus) linked.add(e.to);
        if (e.to === focus) linked.add(e.from);
      });
    }
    edgeEls.forEach((e) => {
      const on = focus && (e.from === focus || e.to === focus);
      e.el.setAttribute("stroke", on ? "var(--accent)" : "var(--line)");
      e.el.setAttribute("stroke-width", on ? "1.8" : "1");
      e.el.setAttribute("opacity", focus ? (on ? "0.85" : "0.18") : "0.65");
    });
    nodeEls.forEach((it) => {
      const id = it.node.id;
      const dim = focus && focus !== id && !linked.has(id);
      it.g.style.opacity = dim ? "0.42" : "1";
      it.label.style.fill = dim ? "var(--text-3)" : "var(--text-2)";
      it.label.style.fontWeight = focus === id ? "600" : "400";
    });
  }

  function renderNetSide() {
    if (!side) return;
    if (!NET.active) {
      side.innerHTML = '<div class="net-empty">选一个节点</div>';
      return;
    }
    const node = data.nodes.find((n) => n.id === NET.active);
    if (!node) return;
    side.innerHTML =
      '<div class="net-detail">' +
      '<div class="net-kind" style="color:' + kindColor(node.kind) + '">' + esc(node.kind_label) + "</div>" +
      "<h3>" + esc(node.text) + "</h3>" +
      "<dl>" +
      "<dt>权重</dt><dd>" + node.weight + "</dd>" +
      "<dt>被印证</dt><dd>" + node.evidence + " 次</dd>" +
      "<dt>来源</dt><dd>" + esc(node.source) + "</dd>" +
      "</dl>" +
      '<div class="net-preview-title">bot 会看到：</div>' +
      "<pre>" + esc(node.inject) + "</pre>" +
      '<div class="net-actions">' +
      '<button class="net-btn" id="netEdit">改写</button>' +
      '<button class="net-btn net-danger" id="netDel">删除</button>' +
      "</div>" +
      '<div class="net-status" id="netStatus"></div>' +
      "</div>";
    const statusEl = side.querySelector("#netStatus");
    const setStatus = (text, bad) => {
      if (!statusEl) return;
      statusEl.textContent = text || "";
      statusEl.classList.toggle("bad", Boolean(bad));
    };
    const editBtn = side.querySelector("#netEdit");
    const delBtn = side.querySelector("#netDel");
    if (editBtn) {
      editBtn.addEventListener("click", () => {
        const box = document.createElement("div");
        box.className = "net-editor";
        const area = document.createElement("textarea");
        area.value = node.text;
        area.rows = 4;
        const row = document.createElement("div");
        row.className = "net-actions";
        const save = document.createElement("button");
        save.className = "net-btn";
        save.textContent = "保存";
        const cancel = document.createElement("button");
        cancel.className = "net-btn";
        cancel.textContent = "取消";
        row.append(save, cancel);
        box.append(area, row);
        side.appendChild(box);
        area.focus();
        cancel.addEventListener("click", () => box.remove());
        save.addEventListener("click", async () => {
          const next = area.value.trim();
          if (!next || next === node.text) {
            setStatus("内容没有变化");
            return;
          }
          save.disabled = true;
          setStatus("保存中…");
          let res;
          try {
            res = await api.post("notebook/update", { text: node.text, new_text: next });
          } catch (err) {
            setStatus("请求失败：" + err, true);
            save.disabled = false;
            return;
          }
          if (res && res.ok !== false && (res.ok || res.status !== "error")) {
            NET.active = null;
            await loadNotebook();
          } else {
            setStatus("失败：" + ((res && res.message) || "未知错误"), true);
            save.disabled = false;
          }
        });
      });
    }
    if (delBtn) {
      delBtn.addEventListener("click", async () => {
        if (!delBtn.dataset.armed) {
          delBtn.dataset.armed = "1";
          delBtn.textContent = "确认删除？";
          delBtn.classList.add("armed");
          return;
        }
        delBtn.disabled = true;
        setStatus("删除中…");
        let res;
        try {
          res = await api.post("notebook/delete", { text: node.text });
        } catch (err) {
          setStatus("请求失败：" + err, true);
          delBtn.disabled = false;
          return;
        }
        if (res && res.ok !== false && (res.ok || res.status !== "error")) {
          NET.active = null;
          await loadNotebook();
        } else {
          setStatus("失败：" + ((res && res.message) || "未知错误"), true);
          delBtn.disabled = false;
        }
      });
    }
  }

  // 力导向：斥力 + 环形约束 + 边吸引力，每帧积分。
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let frames = 0;

  function step() {
    const alpha = frames < 90 ? 0.9 : 0.18;
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        const d2 = dx * dx + dy * dy || 0.01;
        const d = Math.sqrt(d2);
        const min = 112;
        if (d < min) {
          const push = ((min - d) / d) * 0.45 * alpha;
          dx *= push;
          dy *= push;
          a.x += dx; a.y += dy;
          b.x -= dx; b.y -= dy;
        }
      }
      // 圆心留白
      let cx = a.x - CX;
      let cy = a.y - CY;
      const cd = Math.hypot(cx, cy) || 0.01;
      if (cd < 100) {
        const push = ((100 - cd) / cd) * alpha;
        a.x += cx * push;
        a.y += cy * push;
      }
      // 环形约束
      const target = 196;
      const pull = (cd - target) * 0.004 * alpha;
      a.x -= (cx / cd) * pull * 40;
      a.y -= (cy / cd) * pull * 40;
    }

    // 边把它拉近一点，结构更紧凑
    edges.forEach((e) => {
      const a = index[e.from];
      const b = index[e.to];
      if (!a || !b) return;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 0.01;
      const want = 150;
      const force = ((d - want) / d) * 0.0016 * alpha;
      a.x += dx * force;
      a.y += dy * force;
      b.x -= dx * force;
      b.y -= dy * force;
    });

    nodeEls.forEach((it, k) => {
      const n = it.node;
      const breathe = reduceMotion ? 0 : Math.sin(frames / 42 + k) * 0.9;
      const r = n.r + breathe;
      it.dot.setAttribute("cx", n.x.toFixed(1));
      it.dot.setAttribute("cy", n.y.toFixed(1));
      it.dot.setAttribute("r", r.toFixed(1));
      it.halo.setAttribute("cx", n.x.toFixed(1));
      it.halo.setAttribute("cy", n.y.toFixed(1));
      it.halo.setAttribute("r", (r + 6).toFixed(1));
      it.label.setAttribute("x", n.x.toFixed(1));
      it.label.setAttribute("y", (n.y > CY ? n.y - r - 8 : n.y + r + 15).toFixed(1));
    });

    edgeEls.forEach((e) => {
      const a = index[e.from];
      const b = index[e.to];
      if (!a || !b) return;
      const mx = (a.x + b.x) / 2 + (a.y - b.y) * 0.07;
      const my = (a.y + b.y) / 2 + (b.x - a.x) * 0.07;
      e.el.setAttribute(
        "d",
        "M" + a.x.toFixed(1) + " " + a.y.toFixed(1) +
        " Q" + mx.toFixed(1) + " " + my.toFixed(1) +
        " " + b.x.toFixed(1) + " " + b.y.toFixed(1)
      );
    });

    frames += 1;
    if (reduceMotion && frames > 40) return;
    NET.raf = requestAnimationFrame(step);
  }

  NET.edgeEls = edgeEls;
  NET.nodeEls = nodeEls;
  applyNetHighlight();
  renderNetSide();

  if (legend) {
    legend.innerHTML = kinds
      .map((k) => {
        const node = byKind[k][0];
        return (
          '<span class="net-chip"><i style="background:' + kindColor(k) + '"></i>' +
          esc(node.kind_label) + " · " + byKind[k].length + "</span>"
        );
      })
      .join("");
  }

  NET.raf = requestAnimationFrame(step);
}

const VIEW_TITLES = {
  overview: "概览",
  dlc: "资料片",
  config: "配置",
  network: "记忆网络",
  prompt: "提示词",
};

function applyTheme(mode) {
  document.documentElement.setAttribute("data-theme", mode);
  try { localStorage.setItem("pp_theme", mode); } catch (e) { /* 忽略 */ }
}

function showView(name) {
  const crumb = document.getElementById("crumb");
  if (crumb) crumb.textContent = VIEW_TITLES[name] || name;
  document.querySelectorAll(".view").forEach((el) => {
    el.classList.toggle("is-active", el.id === "view-" + name);
  });
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("is-active", el.dataset.view === name);
  });
  if (name === "network") drawNetwork();
}

/* ============ 概览 ============ */
function renderOverview() {
  const host = document.getElementById("overviewCards");
  if (!host) return;
  if (state.loadError) {
    host.innerHTML =
      '<div class="stat alert" style="grid-column:1/-1">' +
      '<div class="stat-head"><span class="stat-icon">!</span><span class="stat-label">连不上插件接口</span></div>' +
      '<div class="stat-note">' + esc(state.loadError) + "</div>" +
      '<div class="stat-note">请从 AstrBot 面板的「插件页」入口打开这个页面。</div>' +
      "</div>";
    return;
  }
  const v = state.values || {};
  const nb = NET.data || {};
  const groups = (v.enabled_groups || []).length;
  const dlcOn = [v.enable_notebook, v.enable_curiosity, v.enable_proactive].filter(Boolean).length;
  const tendency = v.decision_ai_reply_tendency === "active" ? "积极"
    : (v.decision_ai_reply_tendency === "reserved" ? "克制" : "按人格");

  const stat = (icon, label, value, note, cls) =>
    '<div class="stat ' + (cls || "") + '">' +
    '<div class="stat-head"><span class="stat-icon">' + icon + "</span>" +
    '<span class="stat-label">' + label + "</span></div>" +
    '<div class="stat-value">' + value + "</div>" +
    '<div class="stat-note">' + note + "</div></div>";

  host.innerHTML = [
    stat("◍", "群聊回复", v.enable_group_chat ? "接管中" : "已停用",
      v.enable_group_chat ? groups + " 个群在内" : "当前不接管群消息",
      v.enable_group_chat ? "ok" : "muted"),
    stat("✉", "私聊回复", v.enable_private_chat ? "接管中" : "已停用",
      "策略 " + (v.private_reply_mode || "direct"),
      v.enable_private_chat ? "ok" : "muted"),
    stat("⚙", "参与判断", tendency, "决定要不要接话"),
    stat("❖", "资料片", dlcOn + " / 3", dlcOn ? "已装载" : "全部未装载",
      dlcOn ? "ok" : "muted"),
    stat("◎", "记忆条目", nb.total || 0, (nb.pending_curiosity || 0) + " 个待查"),
    stat("☀", "真实状态", v.enable_life_state ? "跟随" : "关闭",
      v.enable_cross_group_sight ? "跨群见闻 开" : "跨群见闻 关"),
  ].join("");
}

/* ============ 资料片 ============ */
const DLC_DEFS = [
  {
    group: "dlc_notebook",
    key: "enable_notebook",
    name: "记忆本",
    tagline: "它在意的事",
    icon: "❖",
    desc: "它自己记着在意的事。只有话题碰到才带进上下文，不会一直往外冒。",
  },
  {
    group: "dlc_curiosity",
    key: "enable_curiosity",
    name: "好奇心",
    tagline: "不懂就去查",
    icon: "◇",
    desc: "群里出现它不懂的词，会自己上网查，查完记进本子。",
    needs: "enable_notebook",
    needsLabel: "需要先开记忆本",
  },
  {
    group: "dlc_proactive",
    key: "enable_proactive",
    name: "主动发言",
    tagline: "没人叫也可能开口",
    icon: "✦",
    desc: "没人叫它也可能插一句。受冷却、每日上限和静默时段约束。",
  },
];

function renderDlc() {
  const host = document.getElementById("dlcGrid");
  if (!host) return;
  const v = state.values || {};
  host.innerHTML = DLC_DEFS.map((d) => {
    const on = !!v[d.key];
    const blocked = d.needs && !v[d.needs];
    const pill = d.needs && blocked
      ? '<span class="pill need">' + d.needsLabel + "</span>"
      : '<span class="pill' + (on ? " on" : "") + '">' + (on ? "已装载" : "未装载") + "</span>";
    return (
      '<div class="dlc' + (on ? " on" : "") + '">' +
        '<div class="dlc-head">' +
          '<span class="dlc-icon">' + d.icon + "</span>" +
          '<div class="dlc-title"><strong>' + d.name + "</strong><span>" + d.tagline + "</span></div>" +
          '<label class="switch"><input type="checkbox" data-dlc="' + d.key + '"' +
            (on ? " checked" : "") + (blocked ? " disabled" : "") +
            ' /><span class="track"></span></label>' +
        "</div>" +
        '<div class="dlc-desc">' + d.desc + "</div>" +
        '<div class="dlc-foot">' + pill + "</div>" +
      "</div>"
    );
  }).join("");

  host.querySelectorAll("[data-dlc]").forEach((el) => {
    el.addEventListener("change", async () => {
      const key = el.dataset.dlc;
      try {
        const res = await api.post("config/save", { updates: { [key]: el.checked } });
        const data = res && res.data !== undefined ? res.data : res;
        const applied = Array.isArray(data && data.applied)
          ? data.applied.includes(key)
          : !!(data && data.applied);
        if (applied) {
          toast(el.checked ? "已装载" : "已卸载");
          await loadStatus();
          loadNotebook();
        } else {
          el.checked = !el.checked;
          toast("保存失败：" + (((data && data.skipped) || []).join(",") || "未知原因"), "err");
        }
      } catch (err) {
        el.checked = !el.checked;
        toast("保存失败：" + err.message, "err");
      }
    });
  });
}

document.addEventListener("click", (e) => {
  const item = e.target.closest && e.target.closest(".nav-item");
  if (item) showView(item.dataset.view);
});

/* ============ 入口渲染 ============ */
function renderHeader() {
  $("#ver").textContent = state.version || "…";
  const on = !!state.values.enable_group_chat;
  $("#dotMain").className = "dot" + (on ? "" : " off");
  $("#mainSwitch").textContent = "群聊回复：" + (on ? "启用" : "停用");
  $("#tendencyPill").textContent = "倾向：" + (state.values.decision_ai_reply_tendency ?? "persona");
  $("#smartPill").textContent = "并发：" + (state.values.concurrent_mode ?? "legacy");
}

const groupIdList = () => (state.groups || []).map((g) => g.id);

/* ============ 配置：tab + 分页 ============ */
function renderTabs() {
  const host = $("#tabs");
  if (!host) return;
  host.innerHTML = (state.groups || [])
    .map((g, idx) => {
      const on = isMasterOn(MASTERS[g.id]);
      const summary = groupSummary(g.id);
      // 不再用 emoji 图标：很多环境没有 emoji 字体，会显示成方块。
      const emoji = "";
      return (
        '<button class="rail-item' + (idx === activeIdx ? " is-active" : "") +
        '" data-tab="' + idx + '">' +
        (emoji ? '<span class="rail-emoji">' + emoji + "</span>" : "") +
        '<span class="rail-text">' +
        '<span class="rail-name">' + esc(stripEmoji(g.title || g.id)) + "</span>" +
        (summary ? '<span class="rail-sum">' + esc(summary) + "</span>" : "") +
        "</span>" +
        '<span class="dot ' + (on ? "" : "off") + '"></span>' +
        "</button>"
      );
    })
    .join("");
  host.querySelectorAll("[data-tab]").forEach((el) => {
    el.addEventListener("click", () => selectTab(Number(el.dataset.tab)));
  });
}

function renderGroup() {
  const groups = state.groups || [];
  if (!groups.length) return;
  activeIdx = Math.max(0, Math.min(activeIdx, groups.length - 1));
  const g = groups[activeIdx];
  renderTabs();
  const keys = Object.keys(g.items || {});
  const on = isMasterOn(MASTERS[g.id]);
  $("#configPanel").innerHTML = `
    <div class="panel-head">
      <h3>${esc(g.title || g.id)}</h3>
      <span class="badge ${on ? "on" : "off"}">${on ? "已启用" : "已关闭"}</span>
    </div>
    <div class="form-grid">${keys.map(fieldControl).join("")}</div>
    <div class="save-bar">
      <span class="save-status" id="saveStatus">${configDirty ? "⚠️ 有未保存改动" : "修改后点击保存生效"}</span>
      <button class="btn" id="saveBtn">保存本组</button>
    </div>`;
  $("#configPanel").querySelector("#saveBtn").addEventListener("click", saveGroup);
  // 改动标记
  $("#configPanel").querySelectorAll("[data-key]").forEach((el) => {
    el.addEventListener("input", () => { configDirty = true; const s = $("#saveStatus"); if (s) s.textContent = "⚠️ 有未保存改动"; });
  });
  bindHints(); // 每次重绘后重新绑定 ⓘ 说明
}

function selectTab(idx) {
  if (configDirty && !confirm("当前分组有未保存的改动，切换将丢弃。确定继续？")) return;
  configDirty = false;
  activeIdx = idx;
  renderGroup();
}

function goToGroup(gid) {
  const idx = groupIdList().indexOf(gid);
  if (idx >= 0) { configDirty = false; activeIdx = idx; renderGroup(); }
}

/* ============ 字段控件（长说明收进 ⓘ） ============ */
function fieldControl(key) {
  const def = META[key] || { label: key, type: "string", hint: "" };
  const val = state.values[key];
  const hint = def.hint || "";
  const showInline = hint && hint.length <= 36; // 短说明直接显示，长说明才折叠进 ⓘ
  const label = `<div class="field-label-row"><span class="field-label">${esc(def.label)}</span>` +
    (hint && !showInline ? `<span class="hint-ico" data-hint="${esc(hint)}" title="${esc(hint)}">ⓘ</span>` : "") +
    "</div>";
  const hintLine = showInline ? `<div class="field-hint">${esc(hint)}</div>` : "";
  let ctrl = "";
  if (def.type === "bool") {
    ctrl = `<label class="switch"><input type="checkbox" data-key="${key}" ${val ? "checked" : ""} /><span class="track"></span></label>`;
  } else if (def.type === "select" || (def.options && def.options.length)) {
    const opts = (def.options || []).map((o) => `<option value="${esc(o)}" ${String(val) === String(o) ? "selected" : ""}>${esc(o)}</option>`).join("");
    ctrl = `<select data-key="${key}">${opts}</select>`;
  } else if (def.type === "list") {
    ctrl = `<textarea data-key="${key}" rows="4" placeholder="每行一项">${esc(Array.isArray(val) ? val.join("\n") : val || "")}</textarea>`;
  } else if (def.type === "int" || def.type === "float") {
    const step = def.step ?? (def.type === "int" ? 1 : 0.01);
    ctrl = `<input type="number" data-key="${key}" value="${esc(val)}" min="${def.min ?? ""}" max="${def.max ?? ""}" step="${step}" />`;
  } else if (def.type === "text" || (def.type === "string" && String(def.label).length > 6 && typeof val === "string" && val && val.includes("\n"))) {
    ctrl = `<textarea data-key="${key}" rows="5">${esc(val ?? "")}</textarea>`;
  } else {
    ctrl = `<input type="text" data-key="${key}" value="${esc(val ?? "")}" />`;
  }
  return `<div class="field">${label}${ctrl}${hintLine}</div>`;
}

/* ============ 保存 ============ */
async function saveGroup() {
  const btn = $("#saveBtn");
  btn.disabled = true;
  const updates = {};
  $("#configPanel").querySelectorAll("[data-key]").forEach((el) => {
    const key = el.dataset.key;
    const def = META[key] || { type: "string" };
    if (def.type === "bool") updates[key] = el.checked;
    else if (def.type === "int") updates[key] = parseInt(el.value, 10);
    else if (def.type === "float") updates[key] = parseFloat(el.value);
    else if (def.type === "list") updates[key] = el.value.split("\n").map((s) => s.trim()).filter(Boolean);
    else updates[key] = el.value;
  });
  try {
    const res = await api.post("config/save", { updates });
    const data = res && res.data !== undefined ? res.data : res;
    if (data && data.applied) {
      toast(`已保存 ${data.applied.length} 项${data.skipped && data.skipped.length ? "，忽略 " + data.skipped.length + " 项" : ""}`, "ok");
      Object.assign(state.values, updates);
      configDirty = false;
      renderGroup();
    } else {
      toast("保存失败：" + ((res && res.message) || "未知错误"), "err");
    }
  } catch (err) {
    toast("保存失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
  }
}

/* ============ ⓘ 说明小框 ============ */
function bindHints() {
  const root = $("#configPanel");
  root.querySelectorAll(".hint-ico").forEach((ico) => {
    ico.addEventListener("click", (e) => {
      e.stopPropagation();
      const text = ico.dataset.hint || "";
      const box = $("#tipBox");
      box.textContent = text;
      box.classList.toggle("show");
      if (box.classList.contains("show")) {
        const r = ico.getBoundingClientRect();
        box.style.top = (r.bottom + 6 + window.scrollY) + "px";
        box.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 320)) + "px";
      }
    });
  });
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".hint-ico")) $("#tipBox").classList.remove("show");
});

/* ============ 提示词预览 ============ */
async function renderPrompts() {
  try {
    const res = await api.get("prompts");
    const data = res && res.data !== undefined ? res.data : res;
    if (!data) return;
    const modeText = (m) => (m === "override" ? "override · 自定义覆盖" : "append · 默认拼接");
    const modeDesc = (m) =>
      m === "override" ? "留空 = 使用默认提示词；填写后完全替换默认提示词" : "留空 = 使用默认提示词；填写后追加在默认提示词之后";
    const statusDesc = (p) => {
      if (p.backend_not_reloaded) return "后端尚未重载，当前仅显示诊断提示";
      if (p.fallback_to_shared) {
        return p.shared_has_custom ? "未单独配置，当前沿用通用补充提示词" : "未单独配置，当前使用内置私聊提示词";
      }
      return p.has_custom ? "已配置私聊自定义提示词" : "使用内置私聊提示词";
    };
    const card = (title, icon, p, cfgKey) => `
      <div class="prompt-card">
        <h3>${icon} ${title}</h3>
        <div class="meta">模式：${modeText(p.mode)} · ${p.backend_not_reloaded ? "等待后端重载" : (p.fallback_to_shared ? (p.shared_has_custom ? "沿用通用提示词" : "使用内置私聊提示词") : (p.has_custom ? "已配置自定义提示词" : "使用默认提示词"))}</div>
        <textarea data-prompt-edit="${cfgKey}" rows="4" placeholder="在这里输入你的自定义提示词…">${esc(p.extra)}</textarea>
        <div class="row">
          <span class="save-status" data-prompt-status="${cfgKey}">${statusDesc(p)}；${modeDesc(p.mode)}${p.time_awareness_source ? (p.time_awareness === false ? "；AstrBot 全局时间感知已关闭" : "；跟随 AstrBot 全局时间感知") : (p.time_awareness === false ? "；当前时间感知已关闭" : "")}</span>
          <button class="btn" data-prompt-save="${cfgKey}">保存</button>
        </div>
        <details class="prompt-preview">
          <summary>生效提示词预览（只读）</summary>
          <pre>${esc(p.text)}</pre>
        </details>
      </div>`;
    const privateDecision = data.private_decision || {
      mode: "append",
      has_custom: false,
      extra: "",
      text: "当前插件后端尚未加载私聊提示词预览接口。请先重载 Persona Presence 插件，再刷新此页面。",
      backend_not_reloaded: true,
    };
    $("#promptGrid").innerHTML =
      card("参与判断提示词", "⚙", data.decision || { mode: "append", has_custom: false, extra: "", text: "" }, "decision_ai_extra_prompt") +
      card("私聊参与判断提示词", "✉", privateDecision, "private_decision_ai_extra_prompt") +
      card("回复生成提示词", "▤", data.reply || { mode: "append", has_custom: false, extra: "", text: "" }, "reply_ai_extra_prompt");
    $("#promptGrid").querySelectorAll("[data-prompt-save]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const key = btn.dataset.promptSave;
        const ta = document.querySelector(`textarea[data-prompt-edit="${key}"]`);
        const status = document.querySelector(`[data-prompt-status="${key}"]`);
        if (!ta || !status) return;
        btn.disabled = true;
        status.textContent = "保存中…";
        try {
          const res = await api.post("config/save", { updates: { [key]: ta.value } });
          const data = res && res.data !== undefined ? res.data : res;
          if (data && data.applied && data.applied.includes(key)) {
            status.textContent = "✅ 已保存";
            toast("提示词已保存", "ok");
            await renderPrompts();
          } else {
            status.textContent = "保存失败：" + ((res && res.message) || "未知错误");
          }
        } catch (err) {
          status.textContent = "保存失败：" + err.message;
        } finally {
          btn.disabled = false;
        }
      });
    });
  } catch (err) {
    $("#promptGrid").innerHTML = `<div class="prompt-card">提示词加载失败：${esc(err.message)}</div>`;
  }
}

/* ============ toast ============ */
let toastTimer = null;
function toast(msg, type) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "show " + (type || "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2800);
}

/* ============ 启动 ============ */
function renderAll() {
  renderHeader();
  renderTabs();
  renderGroup();
}

async function loadStatus() {
  try {
    const res = await api.get("status");
    const data = res && res.data !== undefined ? res.data : res;
    if (!data) throw new Error("status 返回为空");
    state.version = data.version || "";
    state.values = data.values || {};
    state.runtime = data.runtime || {};
    state.groups = data.groups || [];
    buildMeta(state.groups);
    renderAll();
  } catch (err) {
    state.loadError = err.message || String(err);
    state.values = {};
    $("#mainSwitch").textContent = "状态加载失败";
    toast("状态加载失败：" + state.loadError, "err");
    renderOverview();
  }
}

async function init() {
  let savedTheme = "";
  try { savedTheme = localStorage.getItem("pp_theme") || ""; } catch (e) { /* 忽略 */ }
  applyTheme(savedTheme || "light");
  const themeBtn = document.getElementById("themeBtn");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      applyTheme(next);
      drawNetwork();
    });
  }

  const refresh = document.getElementById("refreshBtn");
  if (refresh) {
    refresh.addEventListener("click", async () => {
      await loadStatus();
      loadNotebook();
      toast("已刷新");
    });
  }
  try { await ensureBridge(); if (bridge && bridge.ready) await bridge.ready(); } catch (e) { /* 直接打开也能用 */ }
  await ensureBridge();
  const wanted = (location.hash || "").replace("#", "");
  if (wanted && VIEW_TITLES[wanted]) showView(wanted);
  await loadStatus();
  renderOverview();
  renderDlc();
  loadNotebook();
  renderPrompts();
}

init();
