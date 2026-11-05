/* Evaluation graph: clickable/scrubbable area chart of the eval over plies,
 * with classification markers on notable moves. Canvas-based. */
(function () {
  class Graph {
    constructor(canvas, tipEl, opts) {
      this.cv = canvas;
      this.tip = tipEl;
      this.ctx = canvas.getContext("2d");
      this.opts = opts || {};
      this.data = [];       // [{ply, y (white cp), mate, cls, san, num}]
      this.hoverPly = null;
      this.bind();
    }

    setData(graph, moves) {
      this.data = graph.map((g, i) => {
        const mv = moves && moves[i - 1];
        return {
          ply: g.ply,
          y: Math.max(-1200, Math.min(1200, g.mate != null ? Math.sign(g.mate) * 1100 : g.y)),
          mate: g.mate,
          cls: mv ? mv.class : null,
          san: mv ? mv.san : "",
          num: mv ? mv.moveNumber : "",
          color: mv ? mv.color : null,
        };
      });
      this.draw();
    }

    resize() { this.draw(); }

    xFor(ply) {
      const w = this.cv.width / devicePixelRatio;
      const n = Math.max(this.data.length - 1, 1);
      const pad = 4;
      return pad + (w - 2 * pad) * (ply / n);
    }

    plyFor(clientX) {
      const rect = this.cv.getBoundingClientRect();
      const x = clientX - rect.left;
      const n = Math.max(this.data.length - 1, 1);
      const pad = 4;
      const t = (x - pad) / (rect.width - 2 * pad);
      return Math.max(0, Math.min(this.data.length - 1, Math.round(t * n)));
    }

    draw() {
      const dpr = window.devicePixelRatio || 1;
      const rect = this.cv.getBoundingClientRect();
      if (rect.width < 10) return;
      this.cv.width = rect.width * dpr;
      this.cv.height = rect.height * dpr;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const W = rect.width, H = rect.height;
      const ctx = this.ctx;
      ctx.clearRect(0, 0, W, H);
      if (!this.data.length) {
        ctx.fillStyle = "#9c9a96";
        ctx.font = "13px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("No game loaded", W / 2, H / 2);
        return;
      }
      const pad = 4;
      const mid = H / 2;
      const scaleY = (v) => mid - (v / 1200) * (H / 2 - 6);

      // neutral background (chess.com look: bands filled from the midline)
      ctx.fillStyle = "#7d7a77";
      ctx.fillRect(0, 0, W, H);

      // chess.com-style filled bands: white advantage fills white above the
      // midline, black advantage fills dark below it, per segment
      for (let i = 1; i < this.data.length; i++) {
        const x1 = this.xFor(this.data[i - 1].ply);
        const x2 = this.xFor(this.data[i].ply);
        const y1 = scaleY(this.data[i - 1].y);
        const y2 = scaleY(this.data[i].y);
        ctx.beginPath();
        ctx.moveTo(x1, mid);
        ctx.lineTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.lineTo(x2, mid);
        ctx.closePath();
        ctx.fillStyle = ((y1 + y2) / 2) < mid ? "rgba(255,255,255,.93)" : "rgba(32,30,28,.93)";
        ctx.fill();
      }

      // midline
      ctx.strokeStyle = "rgba(50,48,45,.8)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, mid);
      ctx.lineTo(W, mid);
      ctx.stroke();

      // classification markers
      const colors = {
        brilliant: "#26c2a3", great: "#5c9ded", best: "#81b64c", excellent: "#95bb4a",
        book: "#a88865", inaccuracy: "#f7c631", mistake: "#ffa459", miss: "#ff7769",
        blunder: "#fa412d", forced: "#8b8b8b", good: null,
      };
      for (const d of this.data) {
        if (!d.cls || !colors[d.cls]) continue;
        ctx.fillStyle = colors[d.cls];
        ctx.beginPath();
        ctx.arc(this.xFor(d.ply), scaleY(d.y), 3, 0, Math.PI * 2);
        ctx.fill();
      }

      // current ply indicator
      if (this.opts.currentPly != null && this.data.length) {
        const x = this.xFor(this.opts.currentPly);
        ctx.strokeStyle = "rgba(38,194,163,.95)";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, H);
        ctx.stroke();
      }
    }

    bind() {
      const move = (e) => {
        if (!this.data.length) return;
        const ply = this.plyFor(e.clientX);
        this.hoverPly = ply;
        const d = this.data[ply];
        if (!d) return;
        const ev = d.mate != null
          ? `M${Math.max(1, Math.ceil(Math.abs(d.mate) / 2))}` + (d.mate < 0 ? " for black" : " for white")
          : (d.y / 100).toFixed(1);
        this.tip.hidden = false;
        this.tip.textContent = d.ply === 0 ? `Start · ${ev}` : `${d.num}${d.color === "black" ? "…" : "."} ${d.san} · ${ev}`;
        const wrap = this.cv.parentElement.getBoundingClientRect();
        const x = Math.min(Math.max(e.clientX - wrap.left + 10, 4), wrap.width - 140);
        this.tip.style.left = x + "px";
        this.tip.style.top = "12px";
      };
      this.cv.addEventListener("pointermove", move);
      this.cv.addEventListener("pointerleave", () => { this.tip.hidden = true; this.hoverPly = null; });
      this.cv.addEventListener("pointerdown", (e) => {
        if (this.opts.onSeek && this.data.length) this.opts.onSeek(this.plyFor(e.clientX));
      });
      this.cv.addEventListener("pointermove", (e) => {
        if (e.buttons === 1 && this.opts.onSeek && this.data.length) this.opts.onSeek(this.plyFor(e.clientX));
      });
    }

    setCurrentPly(ply) {
      this.opts.currentPly = ply;
      this.draw();
    }
  }

  window.Graph = Graph;
})();
