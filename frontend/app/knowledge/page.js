"use client";
import { useState, useEffect, useRef, useCallback } from "react";
import dynamic from "next/dynamic";
import {
  getSubjects, getTextbooks,
  kgProcessBatch, kgGetStats, kgSearch,
  kgGetConcept, kgGetGraph, kgGetNeighbors,
  kgDeleteConcept, kgDeleteAll, kgAddConcept, kgAddRelation,
  kgGetConceptTypes, kgExtractRelations, kgProcessRelations,
} from "@/lib/api";

const Graph3D = dynamic(() => import("./Graph3D"), {
  ssr: false,
  loading: () => (
    <div style={{
      width: "100%", height: 520, borderRadius: 14,
      background: "radial-gradient(circle at center, #0f0f2d 0%, #060614 100%)",
      border: "1px solid rgba(60,60,100,0.3)",
      display: "flex", alignItems: "center", justifyContent: "center",
      flexDirection: "column", gap: 14,
    }}>
      <div className="spinner" style={{ borderLeftColor: "#818cf8" }} />
      <div style={{ color: "#818cf8", fontSize: 13, fontWeight: 500 }}>Initializing 3D Mesh Engine...</div>
    </div>
  )
});
// ── Constants ────────────────────────────────────────────────────

const TYPE_COLORS = {
  disease:       { bg: "rgba(248,113,113,0.15)", color: "#f87171",   dot: "#f87171" },
  anatomy:       { bg: "rgba(96,165,250,0.15)",  color: "#60a5fa",   dot: "#60a5fa" },
  procedure:     { bg: "rgba(52,211,153,0.15)",  color: "#34d399",   dot: "#34d399" },
  drug:          { bg: "rgba(251,191,36,0.15)",  color: "#fbbf24",   dot: "#fbbf24" },
  symptom:       { bg: "rgba(167,139,250,0.15)", color: "#a78bfa",   dot: "#a78bfa" },
  investigation: { bg: "rgba(251,146,60,0.15)",  color: "#fb923c",   dot: "#fb923c" },
  pathology:     { bg: "rgba(236,72,153,0.15)",  color: "#ec4899",   dot: "#ec4899" },
  physiology:    { bg: "rgba(20,184,166,0.15)",  color: "#14b8a6",   dot: "#14b8a6" },
  organism:      { bg: "rgba(132,204,22,0.15)",  color: "#84cc16",   dot: "#84cc16" },
  syndrome:      { bg: "rgba(239,68,68,0.15)",   color: "#ef4444",   dot: "#ef4444" },
  sign:          { bg: "rgba(168,85,247,0.15)",  color: "#a855f7",   dot: "#a855f7" },
  condition:     { bg: "rgba(234,179,8,0.15)",   color: "#eab308",   dot: "#eab308" },
  concept:       { bg: "rgba(148,163,184,0.15)", color: "#94a3b8",   dot: "#94a3b8" },
  other:         { bg: "rgba(100,116,139,0.15)", color: "#64748b",   dot: "#64748b" },
};
const RELATION_LABELS = {
  is_subtype_of:    "is subtype of",
  causes:           "causes",
  presents_with:    "presents with",
  treated_by:       "treated by",
  investigated_by:  "investigated by",
  complication_of:  "complication of",
  associated_with:  "associated with",
  part_of:          "part of",
  differential_of:  "differential of",
  risk_factor_for:  "risk factor for",
  synonym_of:       "synonym of",
  precedes:         "precedes",
  managed_by:       "managed by",
  produces:         "produces",
  metabolized_by:   "metabolized by",
  innervated_by:    "innervated by",
  vascularized_by:  "vascularized by",
  contraindicated_in: "contraindicated in",
  indicated_in:     "indicated in",
  side_effect_of:   "side effect of",
};
const IMPORTANCE_COLORS = {
  must_know: { color: "#f87171", label: "🔴 Must-Know" },
  standard:  { color: "#fbbf24", label: "🟡 Standard" },
  advanced:  { color: "#34d399", label: "🟢 Advanced" },
};
const KG_STATUS_STYLE = {
  not_built:          { color: "#64748b", label: "Not Built",     bg: "rgba(100,116,139,0.12)" },
  building:           { color: "#fbbf24", label: "Building...",   bg: "rgba(251,191,36,0.12)"  },
  kg_batch_pending:   { color: "#60a5fa", label: "Batch Pending", bg: "rgba(96,165,250,0.12)"  },
  relations_pending:  { color: "#818cf8", label: "Relations Pending", bg: "rgba(129,140,248,0.12)" },
  completed:          { color: "#34d399", label: "Completed",     bg: "rgba(52,211,153,0.12)"  },
  completed_with_errors: { color: "#fb923c", label: "Completed*", bg: "rgba(251,146,60,0.12)"  },
  failed:             { color: "#f87171", label: "Failed",        bg: "rgba(248,113,113,0.12)" },
};
function getTypeStyle(type) {

  return TYPE_COLORS[type] || TYPE_COLORS.other;
}

// ── Canvas Graph Renderer (Premium) ───────────────────────────────

function GraphCanvas({ nodes, edges, onSelectNode, selectedNodeId, height = 520 }) {
  const canvasRef = useRef(null);
  const posRef = useRef({});
  const dragRef = useRef(null);
  const animRef = useRef(null);
  const hoverRef = useRef(null);
  const transformRef = useRef({ x: 0, y: 0, scale: 1 });
  const panStartRef = useRef(null);
  const [zoomLevel, setZoomLevel] = useState(100);
  const [showLabels, setShowLabels] = useState(true);

  // Radial layout — center node in middle, neighbors in rings
  const layoutNodes = useCallback((ns, es) => {
    const p = {};
    if (!ns?.length) return p;
    const W = 1400, H = 700;
    const cx = W / 2, cy = H / 2;
    const centerNode = ns.find(n => n.is_center) || ns.find(n => n.id === selectedNodeId) || ns[0];
    if (!centerNode) return p;
    p[centerNode.id] = { x: cx, y: cy };
    const connected = new Set();
    es.forEach(e => {
      if (e.source === centerNode.id) connected.add(e.target);
      if (e.target === centerNode.id) connected.add(e.source);
    });
    const connectedNodes = ns.filter(n => n.id !== centerNode.id && connected.has(n.id));
    const otherNodes = ns.filter(n => n.id !== centerNode.id && !connected.has(n.id));
    // Ring 1: directly connected
    const r1 = Math.min(260, 120 + connectedNodes.length * 6);
    connectedNodes.forEach((n, i) => {
      const angle = (i / Math.max(connectedNodes.length, 1)) * Math.PI * 2 - Math.PI / 2;
      p[n.id] = { x: cx + r1 * Math.cos(angle), y: cy + r1 * Math.sin(angle) };
    });
    // Ring 2: others (clustered by concept_type)
    if (otherNodes.length > 0) {
      const types = {};
      otherNodes.forEach(n => {
        if (!types[n.concept_type]) types[n.concept_type] = [];
        types[n.concept_type].push(n);
      });
      const typeArr = Object.entries(types);
      const r2 = r1 + 140;
      let globalIdx = 0;
      typeArr.forEach(([, group], typeIdx) => {
        const typeAngleStart = (typeIdx / typeArr.length) * Math.PI * 2;
        const typeAngleSpan = Math.PI * 2 / typeArr.length * 0.8;
        group.forEach((n, gi) => {
          const angle = typeAngleStart + (gi / Math.max(group.length, 1)) * typeAngleSpan - Math.PI / 4;
          const rr = r2 + Math.floor(gi / 12) * 60;
          p[n.id] = { x: cx + rr * Math.cos(angle), y: cy + rr * Math.sin(angle) };
          globalIdx++;
        });
      });
    }
    return p;
  }, [selectedNodeId]);

  // Redraw canvas at proper resolution on every change
  useEffect(() => {
    if (!nodes?.length) return;
    posRef.current = layoutNodes(nodes, edges);
    transformRef.current = { x: 0, y: 0, scale: 1 };
    setZoomLevel(100);
    const canvas = canvasRef.current;
    if (!canvas) return;

    function draw() {
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      const W = rect.width, H = rect.height;
      // Always render at full resolution for crisp text
      const t = transformRef.current;
      const renderScale = dpr * Math.max(t.scale, 1);
      canvas.width = W * renderScale;
      canvas.height = H * renderScale;
      const ctx = canvas.getContext("2d");
      ctx.setTransform(renderScale, 0, 0, renderScale, 0, 0);
      const pos = posRef.current;
      const hovId = hoverRef.current;

      // Background
      ctx.fillStyle = "#0b0b1a";
      ctx.fillRect(0, 0, W, H);
      // Subtle grid
      ctx.strokeStyle = "rgba(40,40,70,0.25)";
      ctx.lineWidth = 0.5;
      const gs = 50 * t.scale;
      const ox = ((W / 2 + t.x * t.scale) % gs + gs) % gs;
      const oy = ((H / 2 + t.y * t.scale) % gs + gs) % gs;
      for (let gx = ox; gx < W; gx += gs) {
        ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, H); ctx.stroke();
      }
      for (let gy = oy; gy < H; gy += gs) {
        ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke();
      }
      // Apply transform
      ctx.save();
      ctx.translate(W / 2 + t.x * t.scale, H / 2 + t.y * t.scale);
      ctx.scale(t.scale, t.scale);
      ctx.translate(-700, -350);
      // Draw edges
      edges.forEach(e => {
        if (!pos[e.source] || !pos[e.target]) return;
        const sx = pos[e.source].x, sy = pos[e.source].y;
        const tx = pos[e.target].x, ty = pos[e.target].y;
        const isHi = e.source === selectedNodeId || e.target === selectedNodeId
                   || e.source === hovId || e.target === hovId;
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        // Curved edges for better aesthetics
        const mx = (sx + tx) / 2, my = (sy + ty) / 2;
        const dx = tx - sx, dy = ty - sy;
        const off = Math.min(20, Math.sqrt(dx*dx + dy*dy) * 0.1);
        ctx.quadraticCurveTo(mx + dy * 0.05, my - dx * 0.05, tx, ty);
        ctx.strokeStyle = isHi ? "rgba(99,140,255,0.7)" : "rgba(80,90,130,0.15)";
        ctx.lineWidth = isHi ? 2.5 : 0.8;
        ctx.stroke();
        // Arrow
        const angle = Math.atan2(ty - sy, tx - sx);
        const ar = 18;
        const ax = tx - ar * Math.cos(angle), ay = ty - ar * Math.sin(angle);
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(ax - 9 * Math.cos(angle - 0.3), ay - 9 * Math.sin(angle - 0.3));
        ctx.lineTo(ax - 9 * Math.cos(angle + 0.3), ay - 9 * Math.sin(angle + 0.3));
        ctx.closePath();
        ctx.fillStyle = isHi ? "rgba(99,140,255,0.6)" : "rgba(80,90,130,0.15)";
        ctx.fill();
        // Edge label
        if (isHi && RELATION_LABELS[e.relation_type]) {
          ctx.font = "bold 10px 'Inter', 'Segoe UI', sans-serif";
          const labelText = `${RELATION_LABELS[e.relation_type]} (${Math.round((e.confidence || 0.9) * 100)}%)`;
          ctx.fillStyle = "rgba(140,160,255,0.95)";
          ctx.textAlign = "center";
          const lx = (sx + tx) / 2, ly = (sy + ty) / 2 - 8;
          // Label background
          const tw = ctx.measureText(labelText).width;
          ctx.fillStyle = "rgba(8,8,20,0.9)";
          ctx.fillRect(lx - tw / 2 - 6, ly - 8, tw + 12, 16);
          ctx.strokeStyle = "rgba(99,140,255,0.3)";
          ctx.lineWidth = 1;
          ctx.strokeRect(lx - tw / 2 - 6, ly - 8, tw + 12, 16);
          ctx.fillStyle = "rgba(140,170,255,0.95)";
          ctx.fillText(labelText, lx, ly + 4);
        }
      });
      // Draw nodes
      nodes.forEach(n => {
        if (!pos[n.id]) return;
        const x = pos[n.id].x, y = pos[n.id].y;
        const style = getTypeStyle(n.concept_type);
        const isSel = n.id === selectedNodeId;
        const isHov = n.id === hovId;
        const isCtr = n.is_center;
        const impR = { must_know: 18, standard: 13, advanced: 10 };
        const r = isCtr ? 24 : (impR[n.importance] || 13);
        // Outer glow for selected/hovered
        if (isSel || isHov) {
          const glow = ctx.createRadialGradient(x, y, r, x, y, r + 20);
          glow.addColorStop(0, (isSel ? "rgba(77,127,255,0.25)" : "rgba(167,139,250,0.15)"));
          glow.addColorStop(1, "transparent");
          ctx.beginPath();
          ctx.arc(x, y, r + 20, 0, Math.PI * 2);
          ctx.fillStyle = glow;
          ctx.fill();
        }
        // Shadow
        ctx.shadowColor = style.dot + "44";
        ctx.shadowBlur = isSel ? 16 : (isHov ? 10 : 4);
        ctx.shadowOffsetX = 0;
        ctx.shadowOffsetY = 2;
        // Node circle with gradient
        const grad = ctx.createRadialGradient(x - r * 0.25, y - r * 0.25, 0, x, y, r);
        grad.addColorStop(0, style.color + "ff");
        grad.addColorStop(0.7, style.dot + "cc");
        grad.addColorStop(1, style.dot + "88");
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fillStyle = grad;
        ctx.globalAlpha = (isSel || isHov || isCtr) ? 1.0 : 0.65;
        ctx.fill();
        ctx.globalAlpha = 1.0;
        ctx.shadowBlur = 0;
        ctx.shadowColor = "transparent";
        // Border ring
        if (isSel || isCtr) {
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 3;
          ctx.stroke();
        } else if (isHov) {
          ctx.strokeStyle = style.color;
          ctx.lineWidth = 2;
          ctx.stroke();
        }
        // Frequency indicator (small number badge)
        if ((n.frequency || 0) > 2 && (isSel || isHov || isCtr)) {
          const bx = x + r * 0.7, by = y - r * 0.7;
          ctx.beginPath();
          ctx.arc(bx, by, 9, 0, Math.PI * 2);
          ctx.fillStyle = "#1e1e3a";
          ctx.fill();
          ctx.strokeStyle = style.color;
          ctx.lineWidth = 1.5;
          ctx.stroke();
          ctx.font = "bold 8px 'Inter', sans-serif";
          ctx.fillStyle = "#fff";
          ctx.textAlign = "center";
          ctx.fillText(n.frequency, bx, by + 3);
        }
        // Label
        if (!showLabels && !isSel && !isHov && !isCtr) return;
        const showFull = isSel || isHov || isCtr;
        const maxLen = showFull ? 28 : 16;
        const label = n.name.length > maxLen ? n.name.substring(0, maxLen - 1) + "…" : n.name;
        const fs = showFull ? 13 : 10;
        ctx.font = showFull ? `600 ${fs}px 'Inter', 'Segoe UI', sans-serif` : `${fs}px 'Inter', 'Segoe UI', sans-serif`;
        if (showFull) {
          const tw = ctx.measureText(label).width;
          // Pill-shaped label background
          const lx = x - tw / 2 - 8, ly = y + r + 4;
          const lw = tw + 16, lh = fs + 10;
          ctx.beginPath();
          ctx.roundRect(lx, ly, lw, lh, 6);
          ctx.fillStyle = "rgba(12,12,28,0.92)";
          ctx.fill();
          ctx.strokeStyle = style.color + "44";
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.fillStyle = "#e8e8ff";
          ctx.textAlign = "center";
          ctx.fillText(label, x, y + r + fs + 6);
          // Type badge
          ctx.font = "500 9px 'Inter', sans-serif";
          ctx.fillStyle = style.color;
          ctx.fillText(n.concept_type.toUpperCase(), x, y + r + fs + 19);
        } else {
          ctx.fillStyle = "rgba(170,170,200,0.55)";
          ctx.textAlign = "center";
          ctx.fillText(label, x, y + r + fs + 4);
        }
      });
      ctx.restore();
      // HUD overlay (rendered in screen space, not transformed)
      // Zoom indicator
      const zoomPct = Math.round(t.scale * 100);
      ctx.font = "600 11px 'Inter', 'Segoe UI', sans-serif";
      ctx.fillStyle = "rgba(120,130,170,0.6)";
      ctx.textAlign = "left";
      ctx.fillText(`${zoomPct}%`, 12, H - 12);
      // Node count
      ctx.textAlign = "right";
      ctx.fillText(`${nodes.length} nodes · ${edges.length} edges`, W - 12, H - 12);
    }
    function loop() {
      draw();
      animRef.current = requestAnimationFrame(loop);
    }
    animRef.current = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(animRef.current);
  }, [nodes, edges, selectedNodeId, layoutNodes, showLabels]);

  // Transform helpers
  function screenToCanvas(mx, my) {
    const rect = canvasRef.current.getBoundingClientRect();
    const t = transformRef.current;
    const W = rect.width, H = rect.height;
    return {
      x: (mx - rect.left - W / 2) / t.scale - t.x + 700,
      y: (my - rect.top - H / 2) / t.scale - t.y + 350,
    };
  }
  function findNodeAt(mx, my) {
    const { x, y } = screenToCanvas(mx, my);
    const pos = posRef.current;
    for (const n of (nodes || [])) {
      if (!pos[n.id]) continue;
      const dx = pos[n.id].x - x, dy = pos[n.id].y - y;
      if (Math.sqrt(dx * dx + dy * dy) < 30) return n;
    }
    return null;
  }
  function setZoom(newScale) {
    transformRef.current.scale = Math.max(0.2, Math.min(4, newScale));
    setZoomLevel(Math.round(transformRef.current.scale * 100));
  }

  // Event handlers
  function handleClick(e) {
    const node = findNodeAt(e.clientX, e.clientY);
    onSelectNode(node ? node.id : null);
  }
  function handleMouseDown(e) {
    e.preventDefault();
    const node = findNodeAt(e.clientX, e.clientY);
    if (node) { dragRef.current = node.id; }
    else { panStartRef.current = { x: e.clientX, y: e.clientY, tx: transformRef.current.x, ty: transformRef.current.y }; }
  }
  function handleMouseMove(e) {
    const node = findNodeAt(e.clientX, e.clientY);
    hoverRef.current = node ? node.id : null;
    if (canvasRef.current) canvasRef.current.style.cursor = node ? "pointer" : (panStartRef.current ? "grabbing" : "grab");
    if (dragRef.current) {
      const { x, y } = screenToCanvas(e.clientX, e.clientY);
      posRef.current[dragRef.current] = { x, y };
      return;
    }
    if (panStartRef.current) {
      const t = transformRef.current;
      t.x = panStartRef.current.tx + (e.clientX - panStartRef.current.x) / t.scale;
      t.y = panStartRef.current.ty + (e.clientY - panStartRef.current.y) / t.scale;
    }
  }
  function handleMouseUp() { dragRef.current = null; panStartRef.current = null; }
  function handleWheel(e) {
    e.preventDefault();
    const factor = e.deltaY > 0 ? 0.92 : 1.08;
    setZoom(transformRef.current.scale * factor);
  }

  if (!nodes?.length) {
    return (
      <div style={{
        width: "100%", height: height, borderRadius: 14,
        background: "linear-gradient(180deg, #0d0d1f 0%, #080814 100%)",
        border: "1px solid rgba(60,60,100,0.3)",
        display: "flex", alignItems: "center", justifyContent: "center",
        flexDirection: "column", gap: 14,
      }}>
        <div style={{ fontSize: 48, opacity: 0.4 }}>🕸️</div>
        <div style={{ color: "#55567a", fontSize: 15, fontWeight: 500 }}>No graph data to display</div>
        <div style={{ color: "#44455a", fontSize: 12 }}>Select a subject or search for a concept to explore</div>
      </div>
    );
  }

  return (
    <div style={{ position: "relative", height: height }}>
      {/* Toolbar */}
      <div style={{
        position: "absolute", top: 10, left: 10, zIndex: 10,
        display: "flex", gap: 4, background: "rgba(12,12,28,0.85)",
        borderRadius: 10, padding: "4px 6px", backdropFilter: "blur(8px)",
        border: "1px solid rgba(60,60,100,0.3)", boxShadow: "0 4px 20px rgba(0,0,0,0.4)",
      }}>
        <button onClick={() => setZoom(transformRef.current.scale * 1.25)}
          style={toolBtnStyle} title="Zoom In">＋</button>
        <button onClick={() => setZoom(transformRef.current.scale * 0.8)}
          style={toolBtnStyle} title="Zoom Out">−</button>
        <div style={{ width: 1, background: "rgba(80,80,120,0.3)", margin: "2px 4px" }} />
        <button onClick={() => { transformRef.current = { x: 0, y: 0, scale: 1 }; setZoomLevel(100); }}
          style={toolBtnStyle} title="Reset View">⟳</button>
        <button onClick={() => setShowLabels(!showLabels)}
          style={{ ...toolBtnStyle, color: showLabels ? "#818cf8" : "#555" }}
          title={showLabels ? "Hide Labels" : "Show Labels"}>Aa</button>
        <div style={{
          padding: "4px 10px", fontSize: 10, color: "#818cf8",
          fontFamily: "var(--mono)", fontWeight: 600, alignSelf: "center",
        }}>{zoomLevel}%</div>
      </div>
      {/* Legend */}
      <div style={{
        position: "absolute", top: 10, right: 10, zIndex: 10,
        display: "flex", flexWrap: "wrap", gap: 6,
        background: "rgba(12,12,28,0.85)", borderRadius: 10, padding: "6px 10px",
        backdropFilter: "blur(8px)", border: "1px solid rgba(60,60,100,0.3)",
        maxWidth: 320,
      }}>
        {Object.entries(TYPE_COLORS).slice(0, 10).map(([type, s]) => (
          <div key={type} style={{ display: "flex", alignItems: "center", gap: 3, fontSize: 9,
            padding: "2px 6px", borderRadius: 4, background: s.bg, color: s.color,
            fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px" }}>
            <div style={{ width: 6, height: 6, borderRadius: "50%", background: s.dot }} />
            {type}
          </div>
        ))}
      </div>
      {/* Canvas */}
      <canvas
        ref={canvasRef}
        style={{
          width: "100%", height: "100%", borderRadius: 14,
          border: "1px solid rgba(60,60,100,0.3)", cursor: "grab", display: "block",
          background: "#0b0b1a",
        }}
        onClick={handleClick}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        onWheel={handleWheel}
      />
    </div>
  );
}

const toolBtnStyle = {
  background: "transparent", border: "1px solid rgba(80,80,120,0.3)",
  color: "#aab", borderRadius: 6, width: 30, height: 28, cursor: "pointer",
  fontSize: 15, display: "flex", alignItems: "center", justifyContent: "center",
  transition: "all 0.15s",
};


// ── Concept Detail Panel ──────────────────────────────────────────

function ConceptDetailPanel({ concept, onClose, onExplore, onDelete }) {

  if (!concept) return null;
  const style = getTypeStyle(concept.concept_type);
  const impStyle = IMPORTANCE_COLORS[concept.importance] || IMPORTANCE_COLORS.standard;
  const aliases = Array.isArray(concept.aliases) ? concept.aliases : [];
  return (
    <div style={{
      position: "absolute", top: 8, right: 8, width: 340,
      background: "var(--surface)", border: "1px solid var(--border-hi)",
      borderRadius: 12, padding: 16, maxHeight: "calc(100% - 16px)",
      overflowY: "auto", zIndex: 10, boxShadow: "0 12px 40px rgba(0,0,0,0.5)",
    }}>
      {/* Header */}

      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{
            padding: "2px 8px", borderRadius: 6, fontSize: 10,
            background: style.bg, color: style.color, fontFamily: "var(--mono)",
          }}>{concept.concept_type}</span>
          <span style={{ fontSize: 10, color: impStyle.color }}>{impStyle.label}</span>
        </div>
        <button className="btn btn-sm btn-secondary" onClick={onClose} style={{ fontSize: 10 }}>✕</button>
      </div>
      {/* Name */}

      <div style={{ fontSize: 16, fontWeight: 700, color: "var(--text-bright)", marginBottom: 8 }}>
        {concept.name}

      </div>
      {/* Aliases */}

      {aliases.length > 0 && (
        <div style={{ marginBottom: 10, display: "flex", flexWrap: "wrap", gap: 4 }}>
          {aliases.map((a, i) => (
            <span key={i} style={{
              fontSize: 10, padding: "2px 7px", borderRadius: 10,
              background: "rgba(77,127,255,0.1)", color: "var(--accent)",
              border: "1px solid rgba(77,127,255,0.2)",
            }}>{a}</span>
          ))}

        </div>
      )}

      {/* Definition */}

      {concept.definition && (
        <div style={{
          fontSize: 12, color: "var(--text)", lineHeight: 1.5,
          padding: "8px 10px", background: "rgba(77,127,255,0.05)",
          borderLeft: "3px solid var(--accent)", borderRadius: "0 6px 6px 0",
          marginBottom: 12,
        }}>
          {concept.definition}

        </div>
      )}

      {/* Frequency */}

      <div style={{ fontSize: 11, color: "var(--text-dim)", marginBottom: 12, fontFamily: "var(--mono)" }}>
        📊 Frequency: {concept.frequency || 1} · Relations: {
          (concept.outgoing_relations?.length || 0) + (concept.incoming_relations?.length || 0)
        }

      </div>
      {/* Relations */}

      {concept.outgoing_relations?.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-dim)", marginBottom: 6,
                        textTransform: "uppercase", letterSpacing: 1 }}>
            Outgoing Relations
          </div>
          {concept.outgoing_relations.slice(0, 8).map((rel, i) => (
            <div key={i} style={{
              display: "flex", alignItems: "center", gap: 6, marginBottom: 4,
              fontSize: 11, flexWrap: "wrap",
            }}>
              <span style={{ color: "var(--accent)", fontFamily: "var(--mono)", fontSize: 9 }}>
                {RELATION_LABELS[rel.relation_type] || rel.relation_type}

              </span>
              <span style={{
                cursor: "pointer", padding: "1px 6px", borderRadius: 4,
                background: getTypeStyle(rel.other_concept?.concept_type).bg,
                color: getTypeStyle(rel.other_concept?.concept_type).color,
                fontSize: 11,
              }}

                onClick={() => rel.other_concept?.id && onExplore(rel.other_concept.id)}>
                {rel.other_concept?.name || "Unknown"}

              </span>
              <span style={{ fontSize: 9, color: "var(--text-dim)" }}>
                {(rel.confidence * 100).toFixed(0)}%
              </span>
            </div>
          ))}

        </div>
      )}

      {concept.incoming_relations?.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-dim)", marginBottom: 6,
                        textTransform: "uppercase", letterSpacing: 1 }}>
            Incoming Relations
          </div>
          {concept.incoming_relations.slice(0, 8).map((rel, i) => (
            <div key={i} style={{
              display: "flex", alignItems: "center", gap: 6, marginBottom: 4,
              fontSize: 11, flexWrap: "wrap",
            }}>
              <span style={{
                cursor: "pointer", padding: "1px 6px", borderRadius: 4,
                background: getTypeStyle(rel.other_concept?.concept_type).bg,
                color: getTypeStyle(rel.other_concept?.concept_type).color,
                fontSize: 11,
              }}

                onClick={() => rel.other_concept?.id && onExplore(rel.other_concept.id)}>
                {rel.other_concept?.name || "Unknown"}

              </span>
              <span style={{ color: "var(--accent)", fontFamily: "var(--mono)", fontSize: 9 }}>
                {RELATION_LABELS[rel.relation_type] || rel.relation_type}

              </span>
              <span style={{ fontSize: 9, color: "var(--text-dim)" }}>
                {(rel.confidence * 100).toFixed(0)}%
              </span>
            </div>
          ))}

        </div>
      )}

      {/* Sources */}

      {concept.sources?.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-dim)", marginBottom: 6,
                        textTransform: "uppercase", letterSpacing: 1 }}>
            📚 Sources
          </div>
          {concept.sources.map((s, i) => (
            <div key={i} style={{
              fontSize: 11, color: "var(--text)", marginBottom: 4, lineHeight: 1.4,
            }}>
              📖 <span style={{ color: "var(--text-bright)" }}>{s.textbook_name}</span>
              {s.chapter_name && <> → {s.chapter_name}</>}

              {s.page_numbers && (() => {
                try {
                  const pages = JSON.parse(s.page_numbers);
                  if (pages.length > 0) {
                    const display = pages.slice(0, 3).map(p => p + 1).join(", ");
                    return <span style={{ color: "var(--text-dim)" }}> · p.{display}</span>;
                  }

                } catch { return null; }

                return null;
              })()}

            </div>
          ))}

        </div>
      )}

      {/* Actions */}

      <div style={{ display: "flex", gap: 6, paddingTop: 8, borderTop: "1px solid var(--border)" }}>
        <button className="btn btn-primary btn-sm" style={{ fontSize: 11 }}

          onClick={() => onExplore(concept.id)}>
          🔭 Explore
        </button>
        <button className="btn btn-sm"
          style={{ background: "var(--danger-bg)", color: "var(--danger)",
                   border: "1px solid rgba(248,113,113,0.3)", fontSize: 11 }}

          onClick={() => onDelete(concept.id)}>
          🗑 Delete
        </button>
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────

export default function KnowledgeGraphPage() {

  const [subjects, setSubjects] = useState([]);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [textbooks, setTextbooks] = useState([]);
  const [stats, setStats] = useState(null);
  const [graphLimit, setGraphLimit] = useState(10000);
  // State

  const [selSubject, setSelSubject] = useState("");
  const [selTextbook, setSelTextbook] = useState("");
  const [selTypeFilter, setSelTypeFilter] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState([]);
  const [tab, setTab] = useState("overview"); // overview | graph | search | build | manual
  // Graph viz

  const [graphData, setGraphData] = useState({ nodes: [], edges: [] });

  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [selectedConcept, setSelectedConcept] = useState(null);
  const [graphViewMode, setGraphViewMode] = useState("3d");
  // Advanced filters (Feature 5)
  const [importanceFilter, setImportanceFilter] = useState(""); // "" = all, "must_know", "standard", "advanced"
  const [activeRelationTypes, setActiveRelationTypes] = useState(new Set(Object.keys(RELATION_LABELS))); // all on by default
  const [showRelFilters, setShowRelFilters] = useState(false);

  // Client-side filtered graph data
  const filteredGraphData = (() => {
    let nodes = graphData.nodes || [];
    let edges = graphData.edges || [];
    // Filter by importance
    if (importanceFilter) {
      nodes = nodes.filter(n => n.importance === importanceFilter);
    }
    // Filter edges by relation type
    if (activeRelationTypes.size < Object.keys(RELATION_LABELS).length) {
      edges = edges.filter(e => activeRelationTypes.has(e.relation_type));
    }
    // Only keep edges whose source/target are in the filtered nodes set
    const nodeIds = new Set(nodes.map(n => n.id));
    edges = edges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target));
    return { ...graphData, nodes, edges, total_nodes: nodes.length, total_edges: edges.length };
  })();

  function toggleRelationType(type) {
    setActiveRelationTypes(prev => {
      const next = new Set(prev);
      if (next.has(type)) {
        if (next.size > 1) next.delete(type); // Prevent deselecting all
      } else {
        next.add(type);
      }
      return next;
    });
  }

  function resetRelationFilters() {
    setActiveRelationTypes(new Set(Object.keys(RELATION_LABELS)));
  }
  // Build state

  const [batchProcessing, setBatchProcessing] = useState(false);
  // Manual add

  const [manualName, setManualName] = useState("");
  const [manualType, setManualType] = useState("disease");
  const [manualDef, setManualDef] = useState("");
  const [manualAliases, setManualAliases] = useState("");
  const [manualImportance, setManualImportance] = useState("standard");
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState(null);
  useEffect(() => { loadData(); }, []);
  
  useEffect(() => {
    setGraphLimit(10000);
  }, [selSubject, selTypeFilter]);

  useEffect(() => {
    if (tab === "graph") loadGraph();
  }, [selSubject, selTypeFilter, tab, graphLimit]);

  // Escape key handler for fullscreen mode
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === "Escape") {
        setIsFullscreen(false);
      }
    };
    if (isFullscreen) {
      window.addEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "";
    };
  }, [isFullscreen]);
  async function loadData() {

    setLoading(true);
    try {
      const [subs, tbs, st] = await Promise.all([
        getSubjects(), getTextbooks(), kgGetStats(),
      ]);
      setSubjects(subs);
      setTextbooks(tbs);
      setStats(st);
      if (subs.length > 0 && !selSubject) setSelSubject(subs[0].subject);
      // Auto-select the first textbook that has a pending batch

      const pending = tbs.find(t => t.kg_status === "kg_batch_pending");
      if (pending && !selTextbook) {
        setSelTextbook(pending.id);
        setSelSubject(pending.subject);
      }

    } catch (err) { showToast("Load failed: " + err.message, "error"); }

    setLoading(false);
  }

  async function loadGraph() {

    try {
      const data = await kgGetGraph(selSubject || null, selTypeFilter || null, graphLimit);
      setGraphData(data);
      setSelectedNodeId(null);
      setSelectedConcept(null);
    } catch (err) { showToast("Graph load failed: " + err.message, "error"); }

  }

  async function handleSelectNode(nodeId) {

    setSelectedNodeId(nodeId);
    if (!nodeId) { setSelectedConcept(null); return; }

    try {
      const concept = await kgGetConcept(nodeId);
      setSelectedConcept(concept);
    } catch { setSelectedConcept(null); }

  }

  async function handleExplore(conceptId) {

    try {
      const data = await kgGetNeighbors(conceptId);
      setGraphData({ nodes: data.nodes, edges: data.edges });

      setSelectedNodeId(conceptId);
      const concept = await kgGetConcept(conceptId);
      setSelectedConcept(concept);
      setTab("graph");
    } catch (err) { showToast("Explore failed: " + err.message, "error"); }

  }

  async function handleSearch(q) {

    setSearchQuery(q);
    if (q.length < 2) { setSearchResults([]); return; }

    try {
      const res = await kgSearch(q, selSubject || null, selTypeFilter || null, null, 40);
      setSearchResults(res.concepts || []);
    } catch { setSearchResults([]); }

  }

  // textbookId param allows calling directly with an ID (bypasses stale state)

  async function handleProcessBatch(textbookId) {

    const rawId = textbookId || selTextbook;
    // Coerce to string — guard against accidentally passing a full object

    const tbId = rawId && typeof rawId === "object" ? rawId.id : String(rawId || "");
    if (!tbId || tbId === "[object Object]") {
      showToast("Invalid textbook ID. Please select a textbook from the dropdown and retry.", "error");
      console.error("[handleProcessBatch] received bad textbookId:", rawId);
      return;
    }

    // Sync dropdown so user sees what's being processed

    if (tbId !== selTextbook) setSelTextbook(tbId);
    setBatchProcessing(true);
    try {
      const res = await kgProcessBatch(tbId);
      if (res.status === "batch_pending" || res.status?.includes("processing")) {
        showToast("Batch still processing in Claude's queue. Try again in a few minutes.", "info");
      } else if (res.concepts_new !== undefined) {
        showToast(
          `✅ Graph built: ${res.concepts_new} new concepts, ${res.relations_new} relations, ${res.chapters_processed} chapters`,
          "success"
        );
        loadData();
        if (tab === "graph") loadGraph();
      } else if (res.message) {
        showToast(res.message, res.status === "completed" ? "success" : "info");
        loadData();
      } else {
        showToast("Batch processed", "info");
        loadData();
      }

    } catch (err) {
      showToast("Batch processing failed: " + err.message, "error");
    }

    setBatchProcessing(false);
  }

  async function handleExtractRelations(textbookId) {

    const tbId = textbookId && typeof textbookId === "object" ? textbookId.id : String(textbookId || "");
    if (!tbId) { showToast("Select a textbook first", "error"); return; }

    setBatchProcessing(true);
    try {
      const res = await kgExtractRelations(tbId);
      if (res.status === "batch_pending") {
        showToast(`Relations batch submitted (${res.chapters_queued} chapters). Wait for Claude, then click "Process Relations".`, "success");
      } else {
        showToast(res.message || "Relations extraction started", "info");
      }

      loadData();
    } catch (err) {
      showToast("Relations extraction failed: " + err.message, "error");
    }

    setBatchProcessing(false);
  }

  async function handleProcessRelations(textbookId) {

    const tbId = textbookId && typeof textbookId === "object" ? textbookId.id : String(textbookId || "");
    if (!tbId) { showToast("Select a textbook first", "error"); return; }

    setBatchProcessing(true);
    try {
      const res = await kgProcessRelations(tbId);
      if (res.status === "processing" || res.status === "in_progress") {
        showToast("Relations batch still processing in Claude. Try again in a few minutes.", "info");
      } else if (res.relations_new !== undefined) {
        showToast(`✅ ${res.relations_new} new relations extracted from ${res.chapters_ok} chapters!`, "success");
        loadData();
        if (tab === "graph") loadGraph();
      } else {
        showToast(res.message || "Relations processed", "info");
        loadData();
      }

    } catch (err) {
      showToast("Relations processing failed: " + err.message, "error");
    }

    setBatchProcessing(false);
  }

  async function handleDeleteConcept(conceptId) {

    if (!confirm("Delete this concept and all its relations?")) return;
    try {
      await kgDeleteConcept(conceptId);
      showToast("Concept deleted", "success");
      setSelectedConcept(null);
      setSelectedNodeId(null);
      loadData();
      if (tab === "graph") loadGraph();
    } catch (err) { showToast("Delete failed: " + err.message, "error"); }

  }

  async function handleClearAll() {

    if (!confirm(`Delete ALL knowledge graph data${selSubject ? " for " + selSubject : ""}? This cannot be undone.`)) return;
    try {
      await kgDeleteAll(selSubject || null);
      showToast("Knowledge graph cleared", "success");
      setGraphData({ nodes: [], edges: [] });

      setSelectedConcept(null);
      loadData();
    } catch (err) { showToast("Clear failed: " + err.message, "error"); }

  }

  async function handleManualAdd(e) {

    e.preventDefault();
    if (!manualName || !manualType || !selSubject) return;
    try {
      const aliases = manualAliases.split(",").map(a => a.trim()).filter(Boolean);
      const res = await kgAddConcept({
        name: manualName, concept_type: manualType,
        definition: manualDef, aliases, importance: manualImportance,
        subject: selSubject,
      });

      showToast(res.message, "success");
      setManualName(""); setManualDef(""); setManualAliases("");
      loadData();
    } catch (err) { showToast("Add failed: " + err.message, "error"); }

  }

  function showToast(msg, type = "info") {

    setToast({ msg, type });

    setTimeout(() => setToast(null), 4500);
  }

  const subjectStats = stats?.by_subject || {};
  const totalConcepts = stats?.total_concepts || 0;
  const totalRelations = stats?.total_relations || 0;
  const byType = stats?.by_type || {};
  const byImportance = stats?.by_importance || {};
  const textbooksForSubject = textbooks.filter(t => !selSubject || t.subject === selSubject);
  if (loading) return <div className="loading-overlay"><div className="spinner" /> Loading...</div>;
  return (
    <div>
      {/* Header */}

      <div className="page-header">
        <div>
          <h1 className="page-title">🕸️ Knowledge Graph</h1>
          <p className="page-subtitle">
            Medical concept mesh — explore relationships, build GraphRAG context
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <div style={{ fontSize: 11, fontFamily: "var(--mono)", color: "var(--text-dim)" }}>
            {totalConcepts.toLocaleString()} concepts · {totalRelations.toLocaleString()} relations
          </div>
          <button className="btn btn-secondary btn-sm" onClick={loadData}>↻ Refresh</button>
        </div>
      </div>
      {/* Subject Selector + Controls */}

      <div style={{ display: "flex", gap: 10, marginBottom: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
        <div className="input-group" style={{ margin: 0, width: 200 }}>
          <label className="input-label">Subject</label>
          <select className="select" value={selSubject}

            onChange={e => { setSelSubject(e.target.value); setSelTextbook(""); }}>
            <option value="">All Subjects</option>
            {subjects.map(s => <option key={s.subject} value={s.subject}>{s.subject}</option>)}

          </select>
        </div>
        <div className="input-group" style={{ margin: 0, width: 120 }}>
          <label className="input-label">Type Filter</label>
          <select className="select" value={selTypeFilter} onChange={e => setSelTypeFilter(e.target.value)}>
            <option value="">All Types</option>
            {Object.keys(TYPE_COLORS).map(t => (
              <option key={t} value={t}>{t}</option>
            ))}

          </select>
        </div>
        <div style={{ flex: 1 }} />
        <button className="btn btn-sm"
          style={{ background: "rgba(248,113,113,0.12)", color: "var(--danger)",
                   border: "1px solid rgba(248,113,113,0.2)" }}

          onClick={handleClearAll}>
          🗑 Clear{selSubject ? " Subject" : " All"}

        </button>
      </div>
      {/* Tabs */}

      <div style={{ display: "flex", gap: 4, marginBottom: 16 }}>
        {[
          { key: "overview", label: "📊 Overview" },
          { key: "graph",    label: "🕸️ Graph View" },
          { key: "search",   label: "🔍 Search" },
          { key: "manage",   label: "📋 Manage" },
          { key: "manual",   label: "✏️ Manual" },
        ].map(t => (
          <button key={t.key}

            className={`btn btn-sm ${tab === t.key ? "btn-primary" : "btn-secondary"}`}

            onClick={() => { setTab(t.key); if (t.key === "graph" && !graphData.nodes.length) loadGraph(); }}

            style={{ fontSize: 12 }}>
            {t.label}

          </button>
        ))}

      </div>
      {/* ── Overview Tab ── */}

      {tab === "overview" && (
        <div>
          {/* Stat Cards */}

          <div className="grid-4" style={{ marginBottom: 20 }}>
            <div className="stat-card">
              <div className="stat-icon">🧠</div>
              <div className="stat-value">{totalConcepts.toLocaleString()}</div>
              <div className="stat-label">Total Concepts</div>
            </div>
            <div className="stat-card">
              <div className="stat-icon">🔗</div>
              <div className="stat-value">{totalRelations.toLocaleString()}</div>
              <div className="stat-label">Relations (Edges)</div>
            </div>
            <div className="stat-card">
              <div className="stat-icon">🔴</div>
              <div className="stat-value" style={{ color: "#f87171" }}>{byImportance.must_know || 0}</div>
              <div className="stat-label">Must-Know Concepts</div>
            </div>
            <div className="stat-card">
              <div className="stat-icon">📚</div>
              <div className="stat-value">{stats?.total_sources || 0}</div>
              <div className="stat-label">Source Records</div>
            </div>
          </div>
          <div className="grid-2" style={{ marginBottom: 20 }}>
            {/* By Type */}

            <div className="card">
              <div className="card-title" style={{ marginBottom: 14 }}>Concepts by Type</div>
              {Object.keys(byType).length === 0 ? (
                <div style={{ color: "var(--text-dim)", fontSize: 13 }}>No concepts yet. Build the graph first.</div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {Object.entries(byType)
                    .sort((a, b) => b[1] - a[1])
                    .map(([type, count]) => {
                      const s = getTypeStyle(type);
                      const pct = totalConcepts > 0 ? (count / totalConcepts * 100) : 0;
                      return (
                        <div key={type} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <div style={{ width: 10, height: 10, borderRadius: "50%", background: s.dot, flexShrink: 0 }} />
                          <span style={{ fontSize: 12, color: "var(--text)", width: 110, textTransform: "capitalize" }}>
                            {type}

                          </span>
                          <div style={{ flex: 1, height: 5, background: "var(--surface)", borderRadius: 3 }}>
                            <div style={{ height: "100%", width: `${pct}%`, background: s.dot,
                                          borderRadius: 3, transition: "width 0.4s" }} />
                          </div>
                          <span style={{ fontSize: 11, color: "var(--text-dim)", fontFamily: "var(--mono)", width: 32, textAlign: "right" }}>
                            {count}

                          </span>
                        </div>
                      );
                    })}

                </div>
              )}

            </div>
            {/* Textbook KG Status */}

            <div className="card">
              <div className="card-title" style={{ marginBottom: 14 }}>Textbook KG Status</div>
              {(stats?.textbooks || []).length === 0 ? (
                <div style={{ color: "var(--text-dim)", fontSize: 13 }}>No textbooks uploaded yet.</div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {(stats?.textbooks || []).map(tb => {
                    const kgStyle = KG_STATUS_STYLE[tb.kg_status] || KG_STATUS_STYLE.not_built;
                    return (
                      <div key={tb.id} style={{
                        display: "flex", alignItems: "center", gap: 8,
                        padding: "8px 10px", borderRadius: 8,
                        background: "var(--surface)", border: "1px solid var(--border)",
                      }}>
                        <div style={{ flex: 1 }}>
                          <div style={{ fontSize: 13, color: "var(--text-bright)", fontWeight: 500 }}>
                            {tb.name}

                          </div>
                          <div style={{ fontSize: 11, color: "var(--text-dim)" }}>{tb.subject}</div>
                        </div>
                        <span style={{
                          fontSize: 10, padding: "2px 8px", borderRadius: 20,
                          background: kgStyle.bg, color: kgStyle.color,
                          fontFamily: "var(--mono)",
                        }}>{kgStyle.label}</span>
                        {tb.kg_status === "kg_batch_pending" && (
                          <button className="btn btn-sm btn-secondary"
                            style={{ fontSize: 10, background: "rgba(96,165,250,0.15)",
                                     color: "#60a5fa", border: "1px solid rgba(96,165,250,0.3)" }}

                            disabled={batchProcessing}

                            onClick={() => handleProcessBatch(tb.id)}>
                            {batchProcessing ? "Processing..." : "📥 Process Batch"}

                          </button>
                        )}

                        {(tb.kg_status === "not_built" || !tb.kg_status || tb.kg_status === "failed") && (
                          <span style={{ fontSize: 10, color: "var(--text-dim)", fontStyle: "italic" }}>
                            Auto-built on textbook upload
                          </span>
                        )}

                      </div>
                    );
                  })}

                </div>
              )}

            </div>
          </div>
          {/* Importance Distribution */}

          {totalConcepts > 0 && (
            <div className="card">
              <div className="card-title" style={{ marginBottom: 14 }}>Importance Distribution</div>
              <div style={{ display: "flex", gap: 20 }}>
                {Object.entries(byImportance).map(([imp, count]) => {
                  const s = IMPORTANCE_COLORS[imp] || IMPORTANCE_COLORS.standard;
                  return (
                    <div key={imp} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <div style={{ width: 8, height: 8, borderRadius: "50%", background: s.color }} />
                      <span style={{ fontSize: 13, color: "var(--text)" }}>{s.label}</span>
                      <span style={{ fontSize: 13, fontWeight: 700, color: "var(--text-bright)",
                                     fontFamily: "var(--mono)" }}>{count}</span>
                    </div>
                  );
                })}

              </div>
            </div>
          )}

        </div>
      )}

      {/* ── Graph View Tab ── */}

      {tab === "graph" && (
        <div>
          {/* Graph Toolbar */}
          <div style={{ display: "flex", gap: 12, marginBottom: 8, alignItems: "center", flexWrap: "wrap" }}>
            <div style={{ fontSize: 12, color: "var(--text-dim)", display: "flex", alignItems: "center", gap: 8 }}>
              <span>
                Showing <strong>{filteredGraphData.total_nodes || 0}</strong> of{" "}
                <strong>{graphData.total_db_nodes || filteredGraphData.total_nodes || 0}</strong> concepts
              </span>
              <span style={{ opacity: 0.35 }}>|</span>
              <span>{filteredGraphData.total_edges || 0} relations</span>
              {selSubject && <span style={{ color: "var(--accent)", fontWeight: 500 }}>· {selSubject}</span>}
            </div>

            {graphData.total_db_nodes > (graphData.nodes || []).length && (
              <button
                className="btn btn-xs btn-primary"
                style={{ height: "auto", padding: "4px 8px", fontSize: 10, borderRadius: 6, display: "flex", alignItems: "center", gap: 4 }}
                onClick={() => setGraphLimit(prev => prev + 5000)}
              >
                <span>➕</span> Load More
              </button>
            )}
            <div style={{ flex: 1 }} />

            {/* View Mode Toggle */}
            <div style={{
              display: "flex", gap: 2, background: "var(--surface)",
              padding: 2, borderRadius: 8, border: "1px solid var(--border-hi)"
            }}>
              <button className={`btn btn-xs ${graphViewMode === "3d" ? "btn-primary" : "btn-secondary"}`}
                style={{ padding: "4px 10px", fontSize: 11, borderRadius: 6, height: "auto" }}
                onClick={() => setGraphViewMode("3d")}>3D Space</button>
              <button className={`btn btn-xs ${graphViewMode === "2d" ? "btn-primary" : "btn-secondary"}`}
                style={{ padding: "4px 10px", fontSize: 11, borderRadius: 6, height: "auto" }}
                onClick={() => setGraphViewMode("2d")}>2D Radial</button>
            </div>
            <button className="btn btn-sm btn-secondary" onClick={loadGraph}>↻ Reload</button>
            <button className="btn btn-sm btn-primary"
              style={{ display: "flex", alignItems: "center", gap: 6 }}
              onClick={() => setIsFullscreen(true)}>⛶ Fullscreen</button>
          </div>

          {/* ── Importance + Relation Filters ── */}
          <div style={{
            display: "flex", gap: 8, marginBottom: 12, alignItems: "center",
            padding: "8px 12px", borderRadius: 8,
            background: "var(--surface)", border: "1px solid var(--border)",
            flexWrap: "wrap",
          }}>
            {/* Importance Filters */}
            <span style={{ fontSize: 10, fontWeight: 700, color: "var(--text-dim)", textTransform: "uppercase", letterSpacing: 1 }}>Importance</span>
            <div style={{ display: "flex", gap: 3 }}>
              {["", "must_know", "standard", "advanced"].map(imp => {
                const label = imp ? IMPORTANCE_COLORS[imp]?.label : "All";
                const color = imp ? IMPORTANCE_COLORS[imp]?.color : "var(--text-dim)";
                return (
                  <button key={imp}
                    className={`btn btn-xs ${importanceFilter === imp ? "btn-primary" : "btn-secondary"}`}
                    style={{
                      fontSize: 10, padding: "3px 10px", borderRadius: 6, height: "auto",
                      border: importanceFilter === imp ? `1px solid ${color}` : "1px solid transparent",
                      background: importanceFilter === imp ? `${color}22` : "transparent",
                      color: importanceFilter === imp ? color : "var(--text-dim)",
                      fontWeight: importanceFilter === imp ? 700 : 400,
                    }}
                    onClick={() => setImportanceFilter(imp)}>
                    {label}
                  </button>
                );
              })}
            </div>

            <div style={{ width: 1, height: 20, background: "var(--border)" }} />

            {/* Relation Type Filters */}
            <span style={{ fontSize: 10, fontWeight: 700, color: "var(--text-dim)", textTransform: "uppercase", letterSpacing: 1 }}>Relations</span>
            <button className="btn btn-xs btn-secondary"
              style={{ fontSize: 10, padding: "3px 8px", borderRadius: 6, height: "auto" }}
              onClick={() => setShowRelFilters(!showRelFilters)}>
              {activeRelationTypes.size}/{Object.keys(RELATION_LABELS).length} active {showRelFilters ? "▲" : "▼"}
            </button>
            {activeRelationTypes.size < Object.keys(RELATION_LABELS).length && (
              <button className="btn btn-xs" style={{ fontSize: 9, color: "var(--accent)", background: "transparent", padding: "2px 6px" }}
                onClick={resetRelationFilters}>Reset</button>
            )}
          </div>

          {/* Relation type dropdown */}
          {showRelFilters && (
            <div style={{
              display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 12,
              padding: "8px 12px", borderRadius: 8,
              background: "var(--surface)", border: "1px solid var(--border)",
            }}>
              {Object.entries(RELATION_LABELS).map(([key, label]) => (
                <button key={key}
                  style={{
                    fontSize: 10, padding: "3px 8px", borderRadius: 5, height: "auto",
                    border: "1px solid var(--border)", cursor: "pointer",
                    background: activeRelationTypes.has(key) ? "rgba(129,140,248,0.15)" : "transparent",
                    color: activeRelationTypes.has(key) ? "#818cf8" : "var(--text-dim)",
                    fontWeight: activeRelationTypes.has(key) ? 600 : 400,
                    transition: "all 0.15s",
                  }}
                  onClick={() => toggleRelationType(key)}>
                  {activeRelationTypes.has(key) ? "✓ " : ""}{label}
                </button>
              ))}
            </div>
          )}

          <div style={{ position: "relative" }}>
            {graphViewMode === "3d" ? (
              <Graph3D
                nodes={filteredGraphData.nodes}
                edges={filteredGraphData.edges}
                onSelectNode={handleSelectNode}
                selectedNodeId={selectedNodeId}
                height={520}
              />
            ) : (
              <GraphCanvas
                nodes={filteredGraphData.nodes}
                edges={filteredGraphData.edges}
                onSelectNode={handleSelectNode}
                selectedNodeId={selectedNodeId}
                height={520}
              />
            )}
            {selectedConcept && (
              <ConceptDetailPanel
                concept={selectedConcept}
                onClose={() => { setSelectedConcept(null); setSelectedNodeId(null); }}
                onExplore={handleExplore}
                onDelete={handleDeleteConcept}
              />
            )}
          </div>
          <div style={{ marginTop: 8, fontSize: 11, color: "var(--text-dim)" }}>
            💡 {graphViewMode === "3d" ? "Left-click + drag to rotate · Right-click + drag to pan · Scroll to zoom · Click node to focus" : "Click a node to see details · Drag nodes to rearrange · Scroll to zoom"}
          </div>
        </div>
      )}

      {/* ── Search Tab ── */}

      {tab === "search" && (
        <div>
          <div className="card" style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
              <div className="input-group" style={{ flex: 1, minWidth: 200, margin: 0 }}>
                <label className="input-label">Search Concepts</label>
                <input
                  id="kg-search-input"
                  className="input"
                  placeholder="e.g. Anemia, Fracture, Appendix..."
                  value={searchQuery}

                  onChange={e => handleSearch(e.target.value)}

                  autoFocus
                />
              </div>
              <div className="input-group" style={{ width: 130, margin: 0 }}>
                <label className="input-label">Type</label>
                <select className="select" value={selTypeFilter} onChange={e => { setSelTypeFilter(e.target.value); handleSearch(searchQuery); }}>
                  <option value="">All Types</option>
                  {Object.keys(TYPE_COLORS).map(t => <option key={t} value={t}>{t}</option>)}

                </select>
              </div>
            </div>
          </div>
          {searchQuery.length >= 2 && (
            <div>
              {searchResults.length === 0 ? (
                <div className="empty-state">
                  <div className="empty-state-icon">🔍</div>
                  <div className="empty-state-title">No concepts found</div>
                  <div className="empty-state-text">
                    Try a different search term or build the knowledge graph first.
                  </div>
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {searchResults.map(c => {
                    const s = getTypeStyle(c.concept_type);
                    const imp = IMPORTANCE_COLORS[c.importance] || IMPORTANCE_COLORS.standard;
                    const aliases = Array.isArray(c.aliases) ? c.aliases : [];
                    return (
                      <div key={c.id} style={{
                        padding: "12px 14px", borderRadius: 8,
                        background: "var(--card)", border: "1px solid var(--border)",
                        cursor: "pointer", transition: "all 0.15s",
                        display: "flex", alignItems: "flex-start", gap: 12,
                      }}

                        onClick={() => handleExplore(c.id)}>
                        <div style={{ width: 10, height: 10, borderRadius: "50%",
                                      background: s.dot, marginTop: 4, flexShrink: 0 }} />
                        <div style={{ flex: 1 }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                            <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-bright)" }}>
                              {c.name}

                            </span>
                            <span style={{
                              fontSize: 10, padding: "1px 7px", borderRadius: 4,
                              background: s.bg, color: s.color, fontFamily: "var(--mono)",
                            }}>{c.concept_type}</span>
                            <span style={{ fontSize: 10, color: imp.color }}>{imp.label}</span>
                          </div>
                          {c.definition && (
                            <div style={{ fontSize: 12, color: "var(--text)", marginTop: 3, lineHeight: 1.4 }}>
                              {c.definition.substring(0, 120)}{c.definition.length > 120 ? "…" : ""}

                            </div>
                          )}

                          <div style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 4, display: "flex", gap: 10 }}>
                            <span>🔗 {c.relation_count} relations</span>
                            <span>📚 {c.source_count} sources</span>
                            <span>📊 freq: {c.frequency}</span>
                            {aliases.length > 0 && (
                              <span>{aliases.slice(0, 2).join(", ")}</span>
                            )}

                          </div>
                        </div>
                      </div>
                    );
                  })}

                </div>
              )}

            </div>
          )}

          {searchQuery.length < 2 && (
            <div className="empty-state">
              <div className="empty-state-icon">🧠</div>
              <div className="empty-state-title">Search Medical Concepts</div>
              <div className="empty-state-text">
                Type at least 2 characters to search. Click any result to explore in graph view.
              </div>
            </div>
          )}

        </div>
      )}

      {/* ── Manage Tab ── */}

      {tab === "manage" && (
        <div>
          {/* Info box */}

          <div className="card" style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 12, color: "var(--text)", lineHeight: 1.6 }}>
              <strong style={{ color: "var(--accent)" }}>How Knowledge Graph works:</strong><br />
              1. When you upload a textbook, the KG extraction batch is <strong>automatically</strong> submitted alongside the KB summary batch<br />
              2. Claude processes both batches in background (~5-15 min)<br />
              3. Use <strong>Check Batch</strong> on the <a href="/textbooks" style={{ color: "var(--accent)" }}>Textbooks page</a> to retrieve both KB + KG results<br />
              4. Click <strong>📥 Process KG Batch</strong> below for pending KG results<br />
              5. Click <strong>🔗 Extract Relations</strong> to add concept relationships after KG is built
            </div>
          </div>
          {/* Textbook KG status table */}

          <div className="card">
            <div className="card-title" style={{ marginBottom: 12 }}>Textbooks — Knowledge Graph Status</div>
            <div className="table-container">
              <table>
                <thead>
                  <tr>
                    <th>Textbook</th>
                    <th>Subject</th>
                    <th>KG Status</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {textbooks.map(tb => {
                    const kgStyle = KG_STATUS_STYLE[tb.kg_status] || KG_STATUS_STYLE.not_built;
                    return (
                      <tr key={tb.id}>
                        <td style={{ fontWeight: 500 }}>{tb.name}</td>
                        <td style={{ color: "var(--text-dim)" }}>{tb.subject}</td>
                        <td>
                          <span style={{
                            fontSize: 10, padding: "2px 8px", borderRadius: 20,
                            background: kgStyle.bg, color: kgStyle.color, fontFamily: "var(--mono)",
                          }}>{kgStyle.label}</span>
                        </td>
                        <td>
                          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                            {tb.kg_status === "kg_batch_pending" && (
                              <button className="btn btn-sm btn-secondary"
                                style={{ fontSize: 10, background: "rgba(96,165,250,0.15)",
                                         color: "#60a5fa", border: "1px solid rgba(96,165,250,0.3)" }}

                                disabled={batchProcessing}

                                onClick={() => handleProcessBatch(tb.id)}>
                                {batchProcessing ? "Processing..." : "📥 Process KG Batch"}

                              </button>
                            )}

                            {tb.kg_status === "relations_pending" && (
                              <button className="btn btn-sm btn-secondary"
                                style={{ fontSize: 10, background: "rgba(129,140,248,0.15)",
                                         color: "#818cf8", border: "1px solid rgba(129,140,248,0.3)" }}

                                disabled={batchProcessing}

                                onClick={() => handleProcessRelations(tb.id)}>
                                {batchProcessing ? "Processing..." : "📥 Process Relations"}

                              </button>
                            )}

                            {(tb.kg_status === "completed" || tb.kg_status === "completed_with_errors") && (
                              <>
                                <button className="btn btn-sm btn-secondary"
                                  style={{ fontSize: 10, background: "rgba(129,140,248,0.15)",
                                           color: "#818cf8", border: "1px solid rgba(129,140,248,0.3)" }}

                                  disabled={batchProcessing}

                                  onClick={() => handleExtractRelations(tb.id)}>
                                  {batchProcessing ? "Submitting..." : "🔗 Extract Relations"}

                                </button>
                                <button className="btn btn-sm btn-secondary" style={{ fontSize: 10 }}

                                  onClick={() => { setSelSubject(tb.subject); setTab("graph"); loadGraph(); }}>
                                  🕸️ View Graph
                                </button>
                              </>
                            )}

                            {(tb.kg_status === "not_built" || !tb.kg_status || tb.kg_status === "failed") && (
                              <span style={{ fontSize: 10, color: "var(--text-dim)", fontStyle: "italic" }}>
                                Builds automatically on textbook upload
                              </span>
                            )}

                          </div>
                        </td>
                      </tr>
                    );
                  })}

                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* ── Manual Tab ── */}

      {tab === "manual" && (
        <div className="grid-2" style={{ alignItems: "start" }}>
          {/* Add Concept */}

          <div className="card">
            <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-bright)", marginBottom: 16 }}>
              Add Concept Manually
            </div>
            <form onSubmit={handleManualAdd}>
              <div className="input-group">
                <label className="input-label">Concept Name *</label>
                <input className="input" value={manualName}

                  onChange={e => setManualName(e.target.value)} required
                  placeholder="e.g. Colles' Fracture" />
              </div>
              <div style={{ display: "flex", gap: 10 }}>
                <div className="input-group" style={{ flex: 1 }}>
                  <label className="input-label">Type *</label>
                  <select className="select" value={manualType} onChange={e => setManualType(e.target.value)}>
                    {Object.keys(TYPE_COLORS).map(t => <option key={t} value={t}>{t}</option>)}

                  </select>
                </div>
                <div className="input-group" style={{ flex: 1 }}>
                  <label className="input-label">Importance</label>
                  <select className="select" value={manualImportance} onChange={e => setManualImportance(e.target.value)}>
                    <option value="must_know">🔴 Must-Know</option>
                    <option value="standard">🟡 Standard</option>
                    <option value="advanced">🟢 Advanced</option>
                  </select>
                </div>
              </div>
              <div className="input-group">
                <label className="input-label">Definition</label>
                <textarea className="input" rows={3} value={manualDef}

                  onChange={e => setManualDef(e.target.value)}

                  style={{ resize: "vertical" }}

                  placeholder="Brief 1-sentence textbook definition..." />
              </div>
              <div className="input-group">
                <label className="input-label">Aliases (comma-separated)</label>
                <input className="input" value={manualAliases}

                  onChange={e => setManualAliases(e.target.value)}

                  placeholder="SCA, HbSS disease, Sickle cell disease" />
                <div className="input-hint">Abbreviations and synonyms</div>
              </div>
              <div className="input-group">
                <label className="input-label">Subject *</label>
                <select className="select" value={selSubject} onChange={e => setSelSubject(e.target.value)} required>
                  <option value="">Select subject...</option>
                  {subjects.map(s => <option key={s.subject} value={s.subject}>{s.subject}</option>)}

                </select>
              </div>
              <button className="btn btn-primary" type="submit" disabled={!manualName || !selSubject}>
                ➕ Add Concept
              </button>
            </form>
          </div>
          {/* Add Relation (only if concepts exist) */}

          {totalConcepts > 0 && (
            <div className="card">
              <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-bright)", marginBottom: 8 }}>
                Add Relation Manually
              </div>
              <p style={{ fontSize: 12, color: "var(--text-dim)", marginBottom: 16 }}>
                Use the Search tab to find concept IDs, then add a custom relation between two concepts.
              </p>
              <ManualRelationForm
                subjects={subjects}

                onSubmit={async (data) => {
                  try {
                    const res = await kgAddRelation(data);
                    showToast(res.message, res.status === "exists" ? "info" : "success");
                    loadData();
                  } catch (err) { showToast("Failed: " + err.message, "error"); }

                }}

              />
            </div>
          )}

        </div>
      )}

      {toast && <div className={`toast toast-${toast.type}`}>{toast.msg}</div>}

      {/* Fullscreen Graph Modal */}
      {isFullscreen && (
        <div style={{
          position: "fixed",
          inset: 0,
          zIndex: 99999,
          background: "#060614",
          display: "flex",
          flexDirection: "column",
          fontFamily: "'Inter', sans-serif",
        }}>
          {/* Top Control Bar */}
          <div style={{
            height: 60,
            background: "rgba(12,12,28,0.96)",
            borderBottom: "1px solid rgba(80,80,120,0.35)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "0 24px",
            gap: 16,
            backdropFilter: "blur(12px)",
            boxShadow: "0 4px 20px rgba(0,0,0,0.3)",
          }}>
            {/* Left: Title & Concept Counts */}
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span style={{ fontSize: 18, fontWeight: 700, color: "#fff", display: "flex", alignItems: "center", gap: 8 }}>
                <span>🕸️</span> Concept Mesh Explorer
              </span>
              <span style={{
                fontSize: 10,
                background: "rgba(129, 140, 248, 0.15)",
                color: "#818cf8",
                padding: "3px 10px",
                borderRadius: 20,
                fontFamily: "var(--mono)",
                fontWeight: 600,
                border: "1px solid rgba(129,140,248,0.25)"
              }}>
                {filteredGraphData.total_nodes} / {graphData.total_db_nodes || filteredGraphData.total_nodes} concepts · {filteredGraphData.total_edges} relations
              </span>
              {graphData.total_db_nodes > (graphData.nodes || []).length && (graphData.nodes || []).length < 1000 && (
                <button
                  style={{
                    background: "rgba(129, 140, 248, 0.25)",
                    border: "1px solid rgba(129, 140, 248, 0.4)",
                    color: "#fff",
                    padding: "4px 10px",
                    borderRadius: 6,
                    fontSize: 10,
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                  onClick={() => setGraphLimit(prev => Math.min(prev + 150, 1000))}
                >
                  <span>➕</span> Load More
                </button>
              )}
            </div>

            {/* Middle: Filters & View Toggles */}
            <div style={{ display: "flex", alignItems: "center", gap: 16, flex: 1, justifyContent: "center", maxWidth: 700 }}>
              {/* Subject Dropdown */}
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 11, color: "#aab", fontWeight: 500 }}>Subject</span>
                <select
                  value={selSubject}
                  onChange={e => { setSelSubject(e.target.value); setSelTextbook(""); }}
                  style={{
                    background: "#0d0d27",
                    border: "1px solid rgba(80,80,120,0.5)",
                    borderRadius: 6,
                    color: "#fff",
                    padding: "4px 10px",
                    fontSize: 12,
                    outline: "none",
                    cursor: "pointer",
                  }}
                >
                  <option value="">All Subjects</option>
                  {subjects.map(s => <option key={s.subject} value={s.subject}>{s.subject}</option>)}
                </select>
              </div>

              {/* Type Filter */}
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 11, color: "#aab", fontWeight: 500 }}>Type</span>
                <select
                  value={selTypeFilter}
                  onChange={e => setSelTypeFilter(e.target.value)}
                  style={{
                    background: "#0d0d27",
                    border: "1px solid rgba(80,80,120,0.5)",
                    borderRadius: 6,
                    color: "#fff",
                    padding: "4px 10px",
                    fontSize: 12,
                    outline: "none",
                    cursor: "pointer",
                  }}
                >
                  <option value="">All Types</option>
                  {Object.keys(TYPE_COLORS).map(t => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </div>

              {/* Importance Filters in Fullscreen */}
              <div style={{ display: "flex", gap: 3 }}>
                {["", "must_know", "standard", "advanced"].map(imp => {
                  const label = imp ? (IMPORTANCE_COLORS[imp]?.label || imp) : "All";
                  const color = imp ? IMPORTANCE_COLORS[imp]?.color : "#94a3b8";
                  return (
                    <button key={imp}
                      style={{
                        fontSize: 10, padding: "4px 10px", borderRadius: 5, height: "auto",
                        background: importanceFilter === imp ? `${color}22` : "transparent",
                        border: importanceFilter === imp ? `1px solid ${color}55` : "1px solid rgba(80,80,120,0.3)",
                        color: importanceFilter === imp ? color : "#8888aa",
                        fontWeight: importanceFilter === imp ? 700 : 400,
                        cursor: "pointer",
                        transition: "all 0.15s",
                      }}
                      onClick={() => setImportanceFilter(imp)}>
                      {label}
                    </button>
                  );
                })}
              </div>

              {/* Relation Filter Button */}
              <button
                style={{
                  fontSize: 10, padding: "4px 10px", borderRadius: 5, height: "auto",
                  background: showRelFilters ? "rgba(129,140,248,0.15)" : "transparent",
                  border: "1px solid rgba(80,80,120,0.4)",
                  color: showRelFilters ? "#818cf8" : "#8888aa",
                  cursor: "pointer",
                }}
                onClick={() => setShowRelFilters(!showRelFilters)}>
                🔗 Relations {activeRelationTypes.size}/{Object.keys(RELATION_LABELS).length}
              </button>

              {/* View Mode (3D/2D) Toggle */}
              <div style={{
                display: "flex", gap: 2, background: "#0d0d27",
                padding: 2, borderRadius: 6, border: "1px solid rgba(80,80,120,0.5)"
              }}>
                <button className={`btn btn-xs ${graphViewMode === "3d" ? "btn-primary" : "btn-secondary"}`}
                  style={{ padding: "4px 10px", fontSize: 10, borderRadius: 4, height: "auto", border: "none" }}
                  onClick={() => setGraphViewMode("3d")}>3D Space</button>
                <button className={`btn btn-xs ${graphViewMode === "2d" ? "btn-primary" : "btn-secondary"}`}
                  style={{ padding: "4px 10px", fontSize: 10, borderRadius: 4, height: "auto", border: "none" }}
                  onClick={() => setGraphViewMode("2d")}>2D Radial</button>
              </div>

              {/* Reload Button */}
              <button className="btn btn-xs btn-secondary"
                style={{ height: "auto", padding: "5px 10px", fontSize: 11, borderRadius: 6, border: "1px solid rgba(80,80,120,0.4)" }}
                onClick={loadGraph}>↻ Reload</button>
            </div>

            {/* Right: Search Input & Exit Fullscreen */}
            <div style={{ display: "flex", alignItems: "center", gap: 14, position: "relative" }}>
              {/* Search Box */}
              <div style={{ width: 220, position: "relative" }}>
                <input
                  type="text"
                  placeholder="🔍 Search concepts..."
                  value={searchQuery}
                  onChange={e => handleSearch(e.target.value)}
                  style={{
                    width: "100%",
                    background: "#0d0d27",
                    border: "1px solid rgba(80,80,120,0.5)",
                    borderRadius: 6,
                    color: "#fff",
                    padding: "5px 10px 5px 28px",
                    fontSize: 12,
                    outline: "none",
                  }}
                />
                <span style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)", fontSize: 11, opacity: 0.5 }}>🔍</span>
                {searchQuery.length > 0 && (
                  <button
                    onClick={() => { setSearchQuery(""); setSearchResults([]); }}
                    style={{
                      position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)",
                      background: "transparent", border: "none", color: "#818cf8", cursor: "pointer", fontSize: 12
                    }}
                  >
                    ✕
                  </button>
                )}

                {/* Floating Search Results Dropdown */}
                {searchQuery.length >= 2 && (
                  <div style={{
                    position: "absolute",
                    top: 36,
                    right: 0,
                    width: 320,
                    maxHeight: 350,
                    overflowY: "auto",
                    background: "rgba(10, 10, 24, 0.98)",
                    border: "1px solid rgba(80, 80, 120, 0.6)",
                    borderRadius: 8,
                    boxShadow: "0 10px 30px rgba(0,0,0,0.7)",
                    zIndex: 100000,
                    padding: 6,
                  }}>
                    {searchResults.length === 0 ? (
                      <div style={{ color: "var(--text-dim)", fontSize: 11, textAlign: "center", padding: 12 }}>
                        No concepts found
                      </div>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                        {searchResults.map(c => {
                          const s = getTypeStyle(c.concept_type);
                          return (
                            <div
                              key={c.id}
                              onClick={() => {
                                handleExplore(c.id);
                                setSearchQuery("");
                                setSearchResults([]);
                              }}
                              style={{
                                padding: "6px 10px",
                                borderRadius: 6,
                                background: "rgba(255,255,255,0.03)",
                                cursor: "pointer",
                                transition: "all 0.15s",
                                display: "flex",
                                alignItems: "center",
                                gap: 8,
                                border: "1px solid transparent",
                              }}
                              onMouseEnter={e => {
                                e.currentTarget.style.border = "1px solid rgba(129, 140, 248, 0.4)";
                                e.currentTarget.style.background = "rgba(129, 140, 248, 0.08)";
                              }}
                              onMouseLeave={e => {
                                e.currentTarget.style.border = "1px solid transparent";
                                e.currentTarget.style.background = "rgba(255,255,255,0.03)";
                              }}
                            >
                              <div style={{ width: 6, height: 6, borderRadius: "50%", background: s.dot, flexShrink: 0 }} />
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontSize: 12, fontWeight: 600, color: "#fff", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                  {c.name}
                                </div>
                                <div style={{ fontSize: 9, color: "var(--text-dim)" }}>
                                  {c.concept_type} · 🔗 {c.relation_count} relations
                                </div>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* Exit Button */}
              <button
                className="btn btn-sm btn-secondary"
                style={{
                  background: "rgba(248,113,113,0.12)",
                  color: "#f87171",
                  border: "1px solid rgba(248,113,113,0.25)",
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  fontSize: 12,
                  fontWeight: 600,
                  height: 32,
                }}
                onClick={() => setIsFullscreen(false)}
              >
                ✕ Exit <span style={{ fontSize: 9, opacity: 0.6, fontFamily: "var(--mono)" }}>[ESC]</span>
              </button>
            </div>
          </div>

          {/* Relation type filter panel (fullscreen) */}
          {showRelFilters && (
            <div style={{
              background: "rgba(12,12,28,0.96)",
              borderBottom: "1px solid rgba(80,80,120,0.35)",
              padding: "8px 24px",
              display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center",
            }}>
              <span style={{ fontSize: 10, color: "#8888aa", fontWeight: 600, marginRight: 8 }}>FILTER EDGES:</span>
              {Object.entries(RELATION_LABELS).map(([key, label]) => (
                <button key={key}
                  style={{
                    fontSize: 10, padding: "3px 8px", borderRadius: 5, height: "auto",
                    border: "1px solid rgba(80,80,120,0.4)", cursor: "pointer",
                    background: activeRelationTypes.has(key) ? "rgba(129,140,248,0.15)" : "transparent",
                    color: activeRelationTypes.has(key) ? "#818cf8" : "#666",
                    fontWeight: activeRelationTypes.has(key) ? 600 : 400,
                    transition: "all 0.15s",
                  }}
                  onClick={() => toggleRelationType(key)}>
                  {activeRelationTypes.has(key) ? "✓ " : ""}{label}
                </button>
              ))}
              <button style={{ fontSize: 9, color: "#818cf8", background: "transparent", border: "none", cursor: "pointer", marginLeft: 8 }}
                onClick={resetRelationFilters}>Reset All</button>
            </div>
          )}

          {/* Fullscreen Viewport Area */}
          <div style={{ flex: 1, position: "relative", width: "100%", height: showRelFilters ? "calc(100vh - 100px)" : "calc(100vh - 60px)", background: "#060614" }}>
            {graphViewMode === "3d" ? (
              <Graph3D
                nodes={filteredGraphData.nodes}
                edges={filteredGraphData.edges}
                onSelectNode={handleSelectNode}
                selectedNodeId={selectedNodeId}
                height="100%"
              />
            ) : (
              <GraphCanvas
                nodes={filteredGraphData.nodes}
                edges={filteredGraphData.edges}
                onSelectNode={handleSelectNode}
                selectedNodeId={selectedNodeId}
                height="100%"
              />
            )}

            {/* Floating HUD Overlay (bottom-left) */}
            <div style={{
              position: "absolute", bottom: 16, left: 16,
              background: "rgba(10,10,24,0.85)", backdropFilter: "blur(8px)",
              border: "1px solid rgba(80,80,120,0.3)",
              borderRadius: 10, padding: "8px 14px",
              fontSize: 10, color: "#aab", fontFamily: "var(--mono)",
              display: "flex", flexDirection: "column", gap: 4,
              maxWidth: 300,
            }}>
              <div style={{ fontWeight: 700, color: "#fff", fontSize: 11 }}>
                📊 {filteredGraphData.total_nodes} / {graphData.total_db_nodes || (graphData.nodes || []).length} concepts
                · {filteredGraphData.total_edges} relations
              </div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {importanceFilter ? (
                  <span style={{ color: IMPORTANCE_COLORS[importanceFilter]?.color }}>▪ {IMPORTANCE_COLORS[importanceFilter]?.label}</span>
                ) : (
                  <span>▪ All importance</span>
                )}
                <span>▪ {activeRelationTypes.size}/{Object.keys(RELATION_LABELS).length} relation types</span>
              </div>
            </div>

            {/* Float Detail Overlay in Fullscreen */}
            {selectedConcept && (
              <ConceptDetailPanel
                concept={selectedConcept}
                onClose={() => { setSelectedConcept(null); setSelectedNodeId(null); }}
                onExplore={handleExplore}
                onDelete={handleDeleteConcept}
              />
            )}
          </div>
        </div>
      )}

    </div>
  );
}

function ManualRelationForm({ subjects, onSubmit }) {

  const [sourceId, setSourceId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [relType, setRelType] = useState("is_subtype_of");
  const [confidence, setConfidence] = useState(0.9);
  function handleSubmit(e) {

    e.preventDefault();
    if (!sourceId || !targetId || !relType) return;
    onSubmit({ source_id: sourceId, target_id: targetId, relation_type: relType, confidence });

    setSourceId(""); setTargetId("");
  }

  return (
    <form onSubmit={handleSubmit}>
      <div className="input-group">
        <label className="input-label">Source Concept ID</label>
        <input className="input" value={sourceId} onChange={e => setSourceId(e.target.value)}

          required placeholder="c_abc123..." />
        <div className="input-hint">Find IDs in the Search tab or graph panel</div>
      </div>
      <div className="input-group">
        <label className="input-label">Relation Type</label>
        <select className="select" value={relType} onChange={e => setRelType(e.target.value)}>
          {Object.entries(RELATION_LABELS).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}

        </select>
      </div>
      <div className="input-group">
        <label className="input-label">Target Concept ID</label>
        <input className="input" value={targetId} onChange={e => setTargetId(e.target.value)}

          required placeholder="c_def456..." />
      </div>
      <div className="input-group">
        <label className="input-label">Confidence (0–1)</label>
        <input className="input" type="number" min={0} max={1} step={0.05}

          value={confidence} onChange={e => setConfidence(parseFloat(e.target.value))} />
      </div>
      <button className="btn btn-primary" type="submit" disabled={!sourceId || !targetId}>
        🔗 Add Relation
      </button>
    </form>
  );
}
