/* Board component: renders a position, handles click-click and drag moves,
 * promotion picker, last-move/check highlights, best-move arrows and badges. */
(function () {
  const FILES = "abcdefgh";
  const UNICODE = {
    wK: "♔", wQ: "♕", wR: "♖", wB: "♗", wN: "♘", wP: "♙",
    bK: "♚", bQ: "♛", bR: "♜", bB: "♝", bN: "♞", bP: "♟",
  };

  class Board {
    constructor(el, opts) {
      this.el = el;
      this.opts = opts || {};
      this.orientation = "white";
      this.pieceDir = "assets/pieces/";  // piece set (theme), switchable from settings
      this.animation = true;  // piece-move glide, toggled from settings
      this.fen = null;
      this.squares = {};
      this.selected = null;
      this.legal = [];        // [{uci, san}] for current position
      this.lastMove = null;   // uci
      this.checkSq = null;
      this.badge = null;      // {sq, cls}
      this.arrows = [];       // engine arrows [{uci, color, width, opacity}]
      this.userArrows = [];   // right-click drawn arrows
      this.userSquares = [];  // right-click square highlights
      this._rmb = null;       // pending right-click drag {from, to, color}
      this.editorMode = false;
      this.build();
      this.bind();
    }

    build() {
      this.el.innerHTML = "";
      for (let v = 0; v < 8; v++) {          // visual row 0..7 top->bottom
        for (let h = 0; h < 8; h++) {        // visual col left->right
          const file = this.orientation === "white" ? h : 7 - h;
          const rank = this.orientation === "white" ? 7 - v : v;
          const sq = FILES[file] + (rank + 1);
          const d = document.createElement("div");
          d.className = "sq " + ((file + rank) % 2 === 0 ? "light" : "dark");
          d.dataset.sq = sq;
          if (v === 7) d.insertAdjacentHTML("beforeend", `<span class="coord file">${FILES[file]}</span>`);
          if (h === 0) d.insertAdjacentHTML("beforeend", `<span class="coord rank">${rank + 1}</span>`);
          this.el.appendChild(d);
          this.squares[sq] = d;
        }
      }
    }

    /* ---- helpers ---- */
    sqToXY(sq) {
      const f = FILES.indexOf(sq[0]);
      const r = parseInt(sq[1], 10) - 1;
      let h, v;
      if (this.orientation === "white") { h = f; v = 7 - r; }
      else { h = 7 - f; v = r; }
      return { x: h * 12.5, y: v * 12.5 };
    }

    pieceFromFen(fen) {
      const map = {};
      const rows = fen.split(" ")[0].split("/");
      for (let r = 0; r < 8; r++) {
        let f = 0;
        for (const ch of rows[7 - r]) {
          if (/\d/.test(ch)) f += parseInt(ch, 10);
          else {
            const color = ch === ch.toUpperCase() ? "w" : "b";
            map[FILES[f] + (r + 1)] = color + ch.toUpperCase();
            f++;
          }
        }
      }
      return map;
    }

    clearDots() {
      this.el.querySelectorAll(".dot").forEach((d) => d.remove());
      this.el.querySelectorAll(".sq.sel").forEach((d) => d.classList.remove("sel"));
    }

    pieceEl(code, sq) {
      const div = document.createElement("div");
      div.className = "piece";
      div.dataset.sq = sq;
      div.dataset.code = code;
      const img = document.createElement("img");
      img.src = `${this.pieceDir}${code}.svg`;
      img.alt = code;
      img.draggable = false;
      img.onerror = () => {
        const s = document.createElement("span");
        s.className = "fallback";
        s.textContent = UNICODE[code];
        div.innerHTML = "";
        div.appendChild(s);
      };
      div.appendChild(img);
      const { x, y } = this.sqToXY(sq);
      div.style.left = x + "%";
      div.style.top = y + "%";
      return div;
    }

    /* ---- public render ---- */
    setPosition(fen, opts) {
      opts = opts || {};
      const prevFen = this.fen, prevLast = this.lastMove;
      this.fen = fen;
      this.lastMove = opts.lastMove || this.lastMove;
      this.checkSq = opts.checkSq || null;
      this.badge = opts.badge !== undefined ? opts.badge : this.badge;
      this.arrows = opts.arrows !== undefined ? opts.arrows : this.arrows;
      if (opts.legal) this.legal = opts.legal;
      this.selected = null;
      this.userArrows = [];   // annotations reset on position change (chess.com/lichess)
      this.userSquares = [];
      this._rmb = null;
      this.renderPieces();
      this.renderDecor();
      this._animate(prevFen, opts.lastMove, prevLast);
    }

    /* ---- animated piece movement (one move at a time, like chess.com/lichess) ----
       renderPieces() rebuilds every element, so the stylesheet transition on .piece
       never sees the change; we re-place the fresh element on its origin square and
       let the CSS transition carry it to the destination. Skipped for jumps that are
       not a single move (multi-ply navigation, flips, fresh loads).
       Both directions animate: a just-played move is matched against its own squares,
       and when replaying backwards the *previous* lastMove is the move being undone
       (needed because the rendered highlight lags one ply behind the position). */
    _animate(prevFen, movedUci, prevLast) {
      if (this._suppressAnim) { this._suppressAnim = false; return; }  // drag dropped the piece already
      if (!this.animation || this.editorMode || !prevFen) return;
      const oldMap = this.pieceFromFen(prevFen), newMap = this.pieceFromFen(this.fen);
      // only animate a plausible single move: count squares whose content changed
      // (normal move/capture/promotion = 2, en passant = 3, castling = 4, jumps >> 4)
      let diff = 0;
      for (const sq in oldMap) if (oldMap[sq] !== newMap[sq]) diff++;
      for (const sq in newMap) if (!(sq in oldMap)) diff++;
      if (diff < 1 || diff > 4) return;
      // stepped forward: the new lastMove is the move that connects the two positions
      if (movedUci && movedUci.length >= 4 && movedUci !== prevLast) {
        if (this._matchMove(oldMap, newMap, movedUci, false)) return;
      }
      // stepped backward: exactly the previous lastMove was un-made; its piece glides home.
      // (Never match "movedUci" here — it is the highlight BELOW the position change and
      // would coincidentally match and animate the wrong piece.)
      if (prevLast && prevLast.length >= 4 && prevLast !== movedUci) {
        if (this._matchMove(oldMap, newMap, prevLast, true)) return;
      }
    }

    _matchMove(oldMap, newMap, uci, undo) {
      const from = uci.slice(0, 2), to = uci.slice(2, 4);
      const CASTLE = { e1g1: ["h1", "f1"], e1c1: ["a1", "d1"], e8g8: ["h8", "f8"], e8c8: ["a8", "d8"] };
      let targetSq, startSq, code;
      if (!undo) {
        // played move: the mover's origin emptied or changed (promotion); it now sits at "to"
        if (!(oldMap[from] && newMap[to] && oldMap[from][0] === newMap[to][0])) return false;
        targetSq = to; startSq = from; code = newMap[to];
      } else {
        // un-made move: the piece is back on its origin square, glides from "to" to "from"
        if (!(oldMap[to] && newMap[from] && oldMap[to][0] === newMap[from][0])) return false;
        targetSq = from; startSq = to; code = newMap[from];
      }
      this._fly(targetSq, startSq, code);
      // castling: the king's uci implies the rook slide too (either direction)
      const rook = CASTLE[from + to];
      if (rook && code && code[1] === "K") {
        const color = code[0], r0 = rook[0], r1 = rook[1];
        if (!undo && oldMap[r0] === color + "R" && newMap[r1] === color + "R") {
          this._fly(r1, r0, color + "R");
        } else if (undo && oldMap[r1] === color + "R" && newMap[r0] === color + "R") {
          this._fly(r0, r1, color + "R");
        }
      }
      return true;
    }

    _fly(targetSq, startSq, code) {
      const el = this.el.querySelector(`.piece[data-sq="${targetSq}"][data-code="${code}"]`);
      if (!el) return;
      const fin = { x: el.style.left, y: el.style.top };
      const start = this.sqToXY(startSq);
      el.style.transition = "none";
      el.style.left = start.x + "%";
      el.style.top = start.y + "%";
      el.getBoundingClientRect();     // commit the start position before transitioning
      el.style.transition = "";       // stylesheet .12s ease takes over
      el.style.left = fin.x;          // -> the change animates the glide to destination
      el.style.top = fin.y;
    }

    setAnimation(on) {
      this.animation = on !== false;
    }

    renderPieces() {
      this.el.querySelectorAll(".piece").forEach((p) => p.remove());
      const map = this.pieceFromFen(this.fen);
      for (const [sq, code] of Object.entries(map)) {
        this.el.appendChild(this.pieceEl(code, sq));
      }
    }

    renderDecor() {
      this.el.querySelectorAll(".sq.hl, .sq.check").forEach((d) => d.classList.remove("hl", "check"));
      if (this.lastMove) {
        for (const sq of uciSquares(this.lastMove)) this.squares[sq]?.classList.add("hl");
      }
      if (this.checkSq) this.squares[this.checkSq]?.classList.add("check");
      this.renderBadge();
      this.renderArrows();
    }

    renderBadge() {
      this.el.querySelectorAll(".badge-class").forEach((b) => b.remove());
      if (!this.badge) return;
      const sq = this.badge.sq;
      const d = document.createElement("div");
      d.className = "badge-class";
      const { x, y } = this.sqToXY(sq);
      d.style.left = x + "%";
      d.style.top = y + "%";
      d.innerHTML = `<span class="cls-${this.badge.cls}">${badgeSymbol(this.badge.cls)}</span>`;
      this.el.appendChild(d);
    }

    renderArrows() {
      const svg = document.getElementById("arrows");
      if (!svg) return;
      const all = [];
      for (const a of (this.arrows || [])) all.push(a);
      for (const a of (this.userArrows || [])) all.push({ width: 0.17, ...a, opacity: a.opacity ?? 1 });
      if (this._rmb && this._rmb.to && this._rmb.to !== this._rmb.from) {
        all.push({ uci: this._rmb.from + this._rmb.to, color: this._rmb.color, width: 0.17, opacity: 0.7 });
      }
      // The SVG viewBox is 0..8 (1 unit = 1 square) but sqToXY returns PERCENTS
      // (0..87.5, used for CSS positioning). Convert: units = pct / 12.5 + 0.5.
      const cxy = (sq) => {
        const p = this.sqToXY(sq);
        return { x: p.x / 12.5 + 0.5, y: p.y / 12.5 + 0.5 };
      };
      const parts = all.map((a, i) => {
        const from = a.uci.slice(0, 2);
        const to = a.uci.slice(2, 4);
        const A = cxy(from);
        const B = cxy(to);
        const dx = B.x - A.x, dy = B.y - A.y;
        const len = Math.hypot(dx, dy) || 1;
        const ux = dx / len, uy = dy / len;      // unit vector A -> B
        const w = a.width || 0.17;
        const color = a.color || "rgba(250,214,74,.9)";
        const opacity = a.opacity ?? 1;
        // arrowhead as an explicit triangle: tip exactly ON the target center
        const headLen = Math.min(3.2 * w, len * 0.45);
        const headW = 3.2 * w;
        const baseX = B.x - ux * headLen, baseY = B.y - uy * headLen;
        const px = -uy, py = ux;                 // perpendicular
        const p1x = baseX + px * headW / 2, p1y = baseY + py * headW / 2;
        const p2x = baseX - px * headW / 2, p2y = baseY - py * headW / 2;
        const inset = Math.min(0.35, len * 0.15);
        return {
          shaft: `<line x1="${(A.x + ux * inset).toFixed(3)}" y1="${(A.y + uy * inset).toFixed(3)}"
            x2="${baseX.toFixed(3)}" y2="${baseY.toFixed(3)}" stroke="${color}"
            stroke-width="${w}" stroke-linecap="round" opacity="${opacity}"/>`,
          head: `<polygon points="${B.x.toFixed(3)},${B.y.toFixed(3)} ${p1x.toFixed(3)},${p1y.toFixed(3)} ${p2x.toFixed(3)},${p2y.toFixed(3)}" fill="${color}" opacity="${opacity}"/>`,
        };
      });
      // paint order: highlights under shafts, heads on top for clean joints
      const lines = parts.map((p) => p.shaft).join("");
      const heads = parts.map((p) => p.head).join("");
      // lichess-style square highlights (circles; r in square units, 1 unit = 1 square)
      let marks = (this.userSquares || []).map((s) => {
        const C = cxy(s.sq);
        return `<circle cx="${C.x.toFixed(3)}" cy="${C.y.toFixed(3)}" r="0.44" fill="none" stroke="${s.color}" stroke-width="0.11" opacity="0.85"/>`;
      }).join("");
      if (this._rmb && this._rmb.to && this._rmb.to === this._rmb.from) {
        const C = cxy(this._rmb.from);
        marks += `<circle cx="${C.x.toFixed(3)}" cy="${C.y.toFixed(3)}" r="0.44" fill="none" stroke="${this._rmb.color}" stroke-width="0.11" opacity="0.6"/>`;
      }
      svg.innerHTML = marks + lines + heads;
    }

    setArrows(list) {
      this.arrows = list || [];
      this.renderArrows();
    }

    /* ---- right-click arrows & highlights (chess.com / lichess style) ---- */
    _rmbColor(e) {
      if (e.shiftKey) return "rgba(224,64,32,.9)";    // red
      if (e.altKey) return "rgba(64,120,224,.9)";     // blue
      if (e.ctrlKey) return "rgba(255,164,64,.9)";    // orange
      return "rgba(250,214,74,.9)";                   // chess.com yellow
    }

    sqFromEvent(e) {
      const rect = this.el.getBoundingClientRect();
      const h = Math.floor(((e.clientX - rect.left) / rect.width) * 8);
      const v = Math.floor(((e.clientY - rect.top) / rect.height) * 8);
      if (h < 0 || h > 7 || v < 0 || v > 7) return null;
      const file = this.orientation === "white" ? h : 7 - h;
      const rank = this.orientation === "white" ? 7 - v : v;
      return FILES[file] + (rank + 1);
    }

    _rmbDown(sq, e) {
      this._rmb = { from: sq, to: sq, color: this._rmbColor(e) };
      try { this.el.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
      this.renderArrows();
    }

    _rmbUp(e) {
      const { from, color } = this._rmb;
      const to = this.sqFromEvent(e) || from;
      this._rmb = null;
      try { this.el.releasePointerCapture(e.pointerId); } catch (err) { /* noop */ }
      if (to === from) {
        const i = this.userSquares.findIndex((s) => s.sq === from && s.color === color);
        if (i >= 0) this.userSquares.splice(i, 1);
        else this.userSquares.push({ sq: from, color });
      } else {
        const key = from + to;
        const i = this.userArrows.findIndex((a) => a.uci === key && a.color === color);
        if (i >= 0) this.userArrows.splice(i, 1);
        else this.userArrows.push({ uci: key, color });
      }
      this.renderArrows();
    }

    flip() {
      this.orientation = this.orientation === "white" ? "black" : "white";
      this.build();
      if (this.fen) this.setPosition(this.fen, { lastMove: this.lastMove, badge: this.badge });
    }

    setPieceTheme(dir) {
      // switch the piece image set and re-render the position (editor tray rebuilt by app.js)
      dir = dir || "assets/pieces/";
      this.pieceDir = dir.endsWith("/") ? dir : dir + "/";
      if (this.fen) this.setPosition(this.fen, { lastMove: this.lastMove, badge: this.badge });
    }

    setEditorMode(on) {
      this.editorMode = on;
      this.clearDots();
      this.selected = null;
    }

    /* ---- interaction ---- */
    bind() {
      this.el.addEventListener("contextmenu", (e) => e.preventDefault());
      this.el.addEventListener("pointerdown", (e) => {
        const piece = e.target.closest(".piece");
        const sqEl = e.target.closest(".sq");
        const sq = piece ? piece.dataset.sq : sqEl?.dataset.sq;
        if (!sq) return;
        if (e.button === 2) {
          if (!this.editorMode) this._rmbDown(sq, e); // editor uses right-click to delete
          return;
        }
        if (e.button !== 0) return;
        this.onSquare(sq, piece ? piece.dataset.code : null);
        if (piece && !this.editorMode) this.startDrag(e, piece);
      });
      this.el.addEventListener("pointermove", (e) => {
        if (this._rmb) {
          const to = this.sqFromEvent(e);
          if (to !== this._rmb.to) { this._rmb.to = to; this.renderArrows(); }
          return;
        }
        this.dragMove(e);
      });
      this.el.addEventListener("pointerup", (e) => {
        if (this._rmb) { this._rmbUp(e); return; }
        this.dragEnd(e);
      });
    }

    onSquare(sq, code) {
      if (this.editorMode) {
        if (this.opts.onEditorSquare) this.opts.onEditorSquare(sq, code);
        return;
      }
      if (!this.opts.onMoveAttempt) return;
      const mv = this.selected && this.findMove(this.selected, sq);
      if (mv) {
        this.clearDots();
        this.opts.onMoveAttempt(mv, this.selected, sq);
        this.selected = null;
        return;
      }
      if (code && this.isOwnPiece(code)) {
        this.selected = sq;
        this.clearDots();
        this.squares[sq].classList.add("sel");
        this.showDots(sq);
      } else {
        this.selected = null;
        this.clearDots();
      }
    }

    isOwnPiece(code) {
      if (!this.fen) return false;
      const turn = this.fen.split(" ")[1] === "b" ? "b" : "w";
      return code[0] === turn;
    }

    findMove(from, to) {
      return this.legal.find((m) => m.uci.startsWith(from + to)) ||
             this.legal.find((m) => m.uci.slice(0, 4) === from + to);
    }

    showDots(sq) {
      for (const m of this.legal) {
        if (!m.uci.startsWith(sq)) continue;
        const to = m.uci.slice(2, 4);
        const occupied = this.el.querySelector(`.piece[data-sq="${to}"]`);
        const d = document.createElement("div");
        d.className = "dot" + (occupied ? " cap" : "");
        const { x, y } = this.sqToXY(to);
        d.style.left = x + "%";
        d.style.top = y + "%";
        d.dataset.to = to;
        this.el.appendChild(d);
      }
    }

    startDrag(e, piece) {
      const rect = this.el.getBoundingClientRect();
      const rel = { x: ((e.clientX - rect.left) / rect.width) * 100, y: ((e.clientY - rect.top) / rect.height) * 100 };
      piece.classList.add("dragging");
      piece.style.left = Math.min(93.75, Math.max(0, rel.x - 6.25)) + "%";
      piece.style.top = Math.min(93.75, Math.max(0, rel.y - 6.25)) + "%";
      this._drag = piece;
      try { this.el.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
    }

    dragMove(e) {
      if (!this._drag) return;
      const rect = this.el.getBoundingClientRect();
      const rel = { x: ((e.clientX - rect.left) / rect.width) * 100, y: ((e.clientY - rect.top) / rect.height) * 100 };
      this._drag.style.left = Math.min(93.75, Math.max(0, rel.x - 6.25)) + "%";
      this._drag.style.top = Math.min(93.75, Math.max(0, rel.y - 6.25)) + "%";
    }

    dragEnd(e) {
      if (!this._drag) return;
      const piece = this._drag;
      this._drag = null;
      piece.classList.remove("dragging");
      const rect = this.el.getBoundingClientRect();
      const h = Math.floor(((e.clientX - rect.left) / rect.width) * 8);
      const v = Math.floor(((e.clientY - rect.top) / rect.height) * 8);
      if (h < 0 || h > 7 || v < 0 || v > 7) { this.setPosition(this.fen); return; }
      const file = this.orientation === "white" ? h : 7 - h;
      const rank = this.orientation === "white" ? 7 - v : v;
      const to = FILES[file] + (rank + 1);
      const from = piece.dataset.sq;
      if (from === to) { this.setPosition(this.fen, { lastMove: this.lastMove }); return; }
      const mv = this.findMove(from, to);
      if (mv && this.opts.onMoveAttempt) {
        this.clearDots();
        this.selected = null;
        this._suppressAnim = true;   // piece is already visually at its destination
        this.opts.onMoveAttempt(mv, from, to);
      } else {
        this.setPosition(this.fen, { lastMove: this.lastMove });
      }
    }
  }

  function uciSquares(uci) {
    return [uci.slice(0, 2), uci.slice(2, 4)];
  }

  /* Classification icons: inline SVG (robust across fonts/OS), ASCII for "?!"/"??".
     SVGs inherit color via fill/stroke = currentColor. */
  const CLASS_SVG = {
    best: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.5l2.9 5.9 6.5.9-4.7 4.6 1.1 6.5L12 17.3l-5.8 3.1 1.1-6.5L2.6 9.3l6.5-.9z" fill="currentColor"/></svg>',
    miss: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" fill="none"/></svg>',
    book: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 2h12a1 1 0 0 1 1 1v19l-7-4.2L5 22V3a1 1 0 0 1 1-1z" fill="currentColor"/></svg>',
  };

  function badgeSymbol(cls) {
    if (CLASS_SVG[cls]) return CLASS_SVG[cls];
    return {
      brilliant: "!!", great: "!", excellent: "!", good: "",
      inaccuracy: "?!", mistake: "?", blunder: "??", forced: "",
    }[cls] || "";
  }

  window.Board = Board;
  window.uciSquares = uciSquares;
  window.badgeSymbol = badgeSymbol;
})();
