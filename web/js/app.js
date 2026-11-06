/* Chess Review Studio � main application logic. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    engines: [],
    review: null,          // full review payload
    ply: 0,                // current game ply (0..N)
    orientation: "white",
    explore: null,         // {fromPly, moves:[{uci,san,fen}]}
    customFen: null,       // custom position from the board editor
    playing: null,
    arrowsOn: true,
    legalCache: new Map(),
    live: { fen: null, lines: null, engine: null, depth: null, cycle: 0 },
    liveOn: localStorage.getItem("crs_live") !== "0",
    liveSuspended: false,  // true while a full review is running
    playBot: false,        // sidebar "play vs engine" session (engine answers your moves)
    playBotColor: "white", // the color YOU play in a bot session
    hintUci: null,         // pending Hint arrow during a play session (uci)
    puzzle: null,          // { data, fen, ply, wrong, lastMove, finished, solved, fail, sideToMove }
    trainerEx: null,       // current training card during a trainer session
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const liveActive = () => state.liveOn && !state.liveSuspended && !editor.active && !state.quiz && !state.repTrain && !state.puzzle && !state.trainerEx;
  const DEPTHS = {
    fast: { depth: 10, movetime: 500 },
    balanced: { depth: 12, movetime: 1000 },
    deep: { depth: 14, movetime: 2000 },
    max: { depth: 16, movetime: 3200 },
  };
  /* chess.com-style arrow ladder: best move is widest/yellow, deeper ranks thinner.
     Widths are in board units (1 = one full square). */
  const ARROW_STYLES = [
    { color: "rgba(250,214,74,.95)", width: 0.22 },
    { color: "rgba(92,157,237,.85)", width: 0.15 },
    { color: "rgba(143,184,240,.75)", width: 0.1 },
  ];
  const ICON_PLAY = '<svg viewBox="0 0 24 24" class="ic" aria-hidden="true"><path d="M7 5.5v13a1 1 0 0 0 1.5.9l11-6.5a1 1 0 0 0 0-1.8l-11-6.5A1 1 0 0 0 7 5.5z" fill="currentColor"/></svg>';
  const ICON_PAUSE = '<svg viewBox="0 0 24 24" class="ic" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1" fill="currentColor"/><rect x="14" y="5" width="4" height="14" rx="1" fill="currentColor"/></svg>';
  const TARGET_SVG = '<svg viewBox="0 0 24 24" class="ic" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="4.5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/></svg>';
  const FIX_SVG = '<svg viewBox="0 0 24 24" class="ic" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M9 9l6 6M15 9l-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>';

  /* board size customization */
  const BOARD_SIZES = { small: 420, medium: 520, large: 620, xl: 760 };
  let pieceDir = "assets/pieces/";      // current piece image set (theme)
  function applyBoardSize() {
    let v = "medium";
    try { v = localStorage.getItem("crs_board_size") || "medium"; } catch (e) { /* no storage */ }
    document.documentElement.style.setProperty("--board-max", (BOARD_SIZES[v] || 520) + "px");
    markPicker("setBoardSize", v);
  }

  /* board color theme + piece image set (chess.com-style customization, persisted) */
  function applyBoardTheme() {
    let v = "green";
    try { v = localStorage.getItem("crs_board_theme") || "green"; } catch (e) { /* no storage */ }
    document.documentElement.dataset.boardTheme = v;
    markPicker("setBoardTheme", v);
  }
  function pieceSetDir(v) {
    return v === "classic" ? "assets/pieces/" : `assets/pieces-${v}/`;
  }
  function applyPieceTheme() {
    let v = "classic";
    try { v = localStorage.getItem("crs_piece_theme") || "classic"; } catch (e) { /* no storage */ }
    document.documentElement.dataset.pieceTheme = v;
    pieceDir = pieceSetDir(v);
    if (board) board.setPieceTheme(pieceDir);   // re-renders board pieces
    if ($("edTray")) buildTray();               // editor tray shows the same set
    markPicker("setPieceTheme", v);
  }

  /* piece-move animation toggle (settings checkbox, persisted) */
  function animEnabled() {
    try { return localStorage.getItem("crs_anim") !== "0"; } catch (e) { return true; }
  }
  function applyAnimSetting() {
    const on = animEnabled();
    const el = $("setAnim");
    if (el) el.checked = on;
    if (board && board.setAnimation) board.setAnimation(on);
  }

  /* move sounds: tiny WebAudio synth, no audio files. Driven by the SAN markup
     (x = capture, + = check, # = mate, O-O = castle, =Q = promotion). */
  let _audioCtx = null;
  function audioCtx() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      if (!_audioCtx) _audioCtx = new AC();
      if (_audioCtx.state === "suspended") _audioCtx.resume().catch(() => { /* autoplay policy */ });
      return _audioCtx;
    } catch (e) { return null; }
  }
  function tone(ctx, t0, freq, dur, type, vol, glideTo) {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, t0);
    if (glideTo) o.frequency.exponentialRampToValueAtTime(glideTo, t0 + dur);
    g.gain.setValueAtTime(vol, t0);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.connect(g); g.connect(ctx.destination);
    o.start(t0); o.stop(t0 + dur + 0.02);
  }
  function soundOn() {
    try { return localStorage.getItem("crs_sound") !== "0"; } catch (e) { return true; }
  }
  function playMoveSound(san) {
    if (!soundOn() || !san) return;
    const ctx = audioCtx();
    if (!ctx) return;
    const t = ctx.currentTime + 0.01;
    try {
      const base = san.replace(/[+#!?]+$/, "");
      if (/#$/.test(san)) {                       // mate: falling chord
        tone(ctx, t, 392, 0.16, "triangle", 0.14);
        tone(ctx, t + 0.14, 311, 0.16, "triangle", 0.14);
        tone(ctx, t + 0.28, 233, 0.3, "triangle", 0.14);
      } else if (/\+$/.test(base)) {              // check: two quick beeps
        tone(ctx, t, 880, 0.07, "square", 0.045);
        tone(ctx, t + 0.09, 1046, 0.09, "square", 0.045);
      } else if (/=[NBRQ]/.test(base)) {          // promotion: rising arpeggio
        tone(ctx, t, 440, 0.05, "triangle", 0.09);
        tone(ctx, t + 0.05, 587, 0.05, "triangle", 0.09);
        tone(ctx, t + 0.10, 784, 0.09, "triangle", 0.09);
      } else if (/O-O/.test(base)) {              // castle: two wood ticks
        tone(ctx, t, 520, 0.05, "triangle", 0.11, 300);
        tone(ctx, t + 0.08, 520, 0.05, "triangle", 0.11, 300);
      } else if (/x/.test(base)) {                // capture: low thunk
        tone(ctx, t, 220, 0.09, "triangle", 0.18, 130);
        tone(ctx, t, 140, 0.05, "sine", 0.2);
      } else {                                    // normal move: soft tick
        tone(ctx, t, 480, 0.05, "triangle", 0.11, 260);
        tone(ctx, t, 850, 0.012, "sine", 0.05);
      }
    } catch (e) { /* sound must never break the app */ }
  }
  function applySoundSetting() {
    const el = $("setSound");
    if (el) el.checked = soundOn();
  }

  /* ---------------- utils ---------------- */
  async function api(path, opts) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 12000); // nothing hangs the render forever
    try {
      const r = await fetch(path, { ...(opts || {}), signal: ctrl.signal });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || r.statusText);
      return data;
    } finally { clearTimeout(t); }
  }
  function post(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }
  function fmtEval(cpWhite, mateWhite) {
    if (mateWhite != null && mateWhite !== 0) {
      const n = Math.max(1, Math.ceil(Math.abs(mateWhite) / 2));
      return (mateWhite < 0 ? "-M" : "M") + n;
    }
    if (mateWhite === 0) return "M0";
    const v = (cpWhite / 100).toFixed(1);
    return (cpWhite > 0 ? "+" : "") + v;
  }
  function toast(msg, ms) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(t._h);
    t._h = setTimeout(() => (t.hidden = true), ms || 2600);
  }
  function winShare(cp) {
    cp = Math.max(-3000, Math.min(3000, cp));
    return 50 + 50 * (2 / (1 + Math.exp(-0.00368208 * cp)) - 1);
  }
  function kingSqInCheck(fen) {
    // returns square of the king in check, or null
    const parts = fen.split(" ");
    const turn = parts[1] === "b" ? "b" : "w";
    const rows = parts[0].split("/");
    let king = null;
    const map = {};
    for (let r = 0; r < 8; r++) {
      let f = 0;
      for (const ch of rows[7 - r]) {
        if (/\d/.test(ch)) f += +ch;
        else { map["abcdefgh"[f] + (r + 1)] = (ch === ch.toUpperCase() ? "w" : "b") + ch.toUpperCase(); f++; }
      }
    }
    for (const [sq, code] of Object.entries(map)) if (code === turn + "K") king = sq;
    // detect check by attacking pattern: rely on /api/legal "check" flag instead
    return null;
  }
  const INIT_PIECES = { P: 8, N: 2, B: 2, R: 2, Q: 1 };
  function materialInfo(fen) {
    const counts = { w: { P: 0, N: 0, B: 0, R: 0, Q: 0 }, b: { P: 0, N: 0, B: 0, R: 0, Q: 0 } };
    for (const ch of fen.split(" ")[0]) {
      if (/[PNBRQ]/.test(ch)) counts.w[ch]++;
      if (/[pnbrq]/.test(ch)) counts.b[ch.toUpperCase()]++;
    }
    const VAL = { P: 1, N: 3, B: 3, R: 5, Q: 9 };
    const dw = Object.keys(VAL).reduce((s, k) => s + counts.w[k] * VAL[k], 0);
    const db = Object.keys(VAL).reduce((s, k) => s + counts.b[k] * VAL[k], 0);
    const glyphs = { P: "?", N: "?", B: "?", R: "?", Q: "?" };
    let capByWhite = "", capByBlack = "";
    for (const k of ["Q", "R", "B", "N", "P"]) {
      capByWhite += glyphs[k].repeat(Math.max(0, INIT_PIECES[k] - counts.b[k]));
      capByBlack += glyphs[k].repeat(Math.max(0, INIT_PIECES[k] - counts.w[k]));
    }
    return { dw, db, capByWhite, capByBlack };
  }

  /* ---------------- board position plumbing ---------------- */
  function currentLines() {
    const fen = currentFen();
    if (state.live.fen === fen && state.live.lines) return state.live.lines; // freshest
    if (state.explore) {
      if (state.explore.linesFen === fen && state.explore.lines) return state.explore.lines;
      return [];
    }
    if (!state.review) return [];
    const pos = state.review.positions[state.ply];
    return (pos && pos.lines) || [];
  }

  function defaultArrows() {
    if (!state.arrowsOn || state.quiz || state.repTrain) return [];
    if (state.playBot) {
      // playing: no engine arrows leaking the answer - only a requested Hint
      return state.hintUci
        ? [{ uci: state.hintUci, color: "rgba(255,168,46,.92)", width: 0.2, opacity: 0.92 }]
        : [];
    }
    return currentLines()
      .filter((ln) => ln.pv && ln.pv.length)
      .slice(0, 3)
      .map((ln, i) => ({ uci: ln.pv[0], ...ARROW_STYLES[i] }));
  }

  function renderTopMoves() {
    const box = $("topMovesBox");
    if (state.quiz || state.repTrain || state.puzzle || state.trainerEx) {
      box.innerHTML = `<div class="empty-hint small" style="padding:14px">Hidden during training - no peeking!</div>`;
      $("topMovesMeta").textContent = "";
      return;
    }
    const lines = currentLines();
    if (!lines.length) {
      box.innerHTML = `<div class="empty-hint small" style="padding:14px">No engine lines here yet.</div>`;
      $("topMovesMeta").textContent = "";
      return;
    }
    const isLive = state.live.fen === currentFen() && !!state.live.lines;
    $("topMovesMeta").textContent = isLive ? "live" : (state.explore ? "evaluating..." : "from review");
    box.innerHTML = lines.slice(0, 3).map((ln, i) => {
      const ev = lineEval(ln);
      const sans = (ln.san || []).slice(0, 8).join(" ");
      const cls = (typeof ln.cpWhite === "number" && ln.cpWhite >= 0) || ln.cpWhite === undefined ? "w" : "b";
      const playing = state.linePlay && state.linePlay.i === i && state.linePlay.playing;
      return `<div class="line rank${i + 1}" data-uci="${ln.pv[0] || ""}" data-rank="${i}">
        <span class="rankchip">${i + 1}</span>
        <span class="ev ${cls}">${ev.startsWith("-") ? ev : "+" + ev}</span>
        <span class="mv">${sans}</span>
        <button class="lineplay" data-i="${i}" type="button" title="${playing ? "Stop replaying this line" : "Watch this line move by move"}">${playing ? "\u25A0" : "\u25B6"}</button>
      </div>`;
    }).join("");
  }

  function currentFen() {
    if (state.explore) {
      if (state.explore.moves.length) return state.explore.moves[state.explore.moves.length - 1].fen;
      if (state.explore.baseFen) return state.explore.baseFen; // idle explore: analysis-ready position
    }
    if (state.puzzle) return state.puzzle.fen;       // puzzle mode paints the board
    if (state.trainerEx) return state.trainerEx.fen; // trainer mode paints the board
    if (state.customFen && !state.review) return state.customFen;
    if (!state.review) return null;
    return state.review.positions[state.ply].fen;
  }

  async function legalFor(fen) {
    if (state.legalCache.has(fen)) return state.legalCache.get(fen);
    const p = api("/api/legal?fen=" + encodeURIComponent(fen));
    state.legalCache.set(fen, p);
    p.catch(() => state.legalCache.delete(fen));
    return p;
  }

  async function renderBoard() {
    const fen = currentFen();
    if (!fen) return;
    if (state.puzzle) { puzzleRenderBoard(); return; }
    if (state.trainerEx) { trainerRenderBoard(); return; }
    let lastUci = null, badge = null;
    if (state.explore && state.explore.moves.length) {
      const entry = state.explore.moves[state.explore.moves.length - 1];
      lastUci = entry.uci;
      if (entry.cls && entry.cls !== "good" && entry.cls !== "forced") {
        badge = { sq: entry.uci.slice(2, 4), cls: entry.cls };
      }
    } else if (state.review && state.ply > 0) {
      const m = state.review.moves[state.ply - 1];
      lastUci = m.uci;
      if (m.class !== "good" && m.class !== "forced") {
        badge = { sq: m.uci.slice(2, 4), cls: m.class };
      }
    }
    // pieces render IMMEDIATELY from local FEN data - no network in the critical path
    board.setPosition(fen, {
      lastMove: lastUci, badge, arrows: defaultArrows(),
      legal: board.fen === fen ? board.legal : [],
    });
    updateEvalBar();
    updateMaterialBars(fen);
    if (!liveActive()) renderEngineLinesForPly();
    renderTopMoves();
    if (state.customFen && !editor.active) renderCustomMoves();
    scheduleExplorer();
    scheduleOpeningChip();
    if (state.quiz) { quizRenderBoard(); return; }
    if (state.repTrain) { repTrainRenderBoard(); return; }
    // legality (dots / check / mate banner) arrives async - never blocks pieces
    legalFor(fen).then((l) => {
      if (currentFen() !== fen) return;
      board.legal = l.moves;
      board.checkSq = l.check ? kingSquare(fen) : null;
      board.renderDecor();
      const msg = $("boardMsg");
      if (l.checkmate) { msg.hidden = false; msg.textContent = "Checkmate"; }
      else if (l.stalemate) { msg.hidden = false; msg.textContent = "Stalemate"; }
      else msg.hidden = true;
    }).catch(() => { /* dots are optional */ });
  }

  function kingSquare(fen) {
    const turn = fen.split(" ")[1] === "b" ? "b" : "w";
    const rows = fen.split(" ")[0].split("/");
    for (let r = 0; r < 8; r++) {
      let f = 0;
      for (const ch of rows[7 - r]) {
        if (/\d/.test(ch)) f += +ch;
        else {
          const c = ch === ch.toUpperCase() ? "w" : "b";
          if (c === turn && ch.toUpperCase() === "K") return "abcdefgh"[f] + (r + 1);
          f++;
        }
      }
    }
    return null;
  }

  function updateEvalBar() {
    if (state.quiz || state.repTrain) { // neutral bar: no spoilers during training
      $("evalBarFill").style.height = "50%";
      $("evalBarNum").textContent = "?";
      return;
    }
    let cp = 0, mate = null;
    const fen = currentFen();
    if (state.live.fen === fen && state.live.lines) {
      const l0 = state.live.lines[0];
      cp = l0.cpWhite; mate = l0.mateWhite;
    } else if (state.explore && state.explore.moves.length && state.explore.lastEval) {
      cp = state.explore.lastEval.cpWhite; mate = state.explore.lastEval.mateWhite;
    } else if (state.review) {
      const pos = state.review.positions[state.ply];
      cp = pos.cpWhite; mate = pos.mateWhite;
    }
    const share = Math.max(3, Math.min(97, winShare(cp)));
    $("evalBarFill").style.height = share + "%";
    $("evalBarNum").textContent = fmtEval(cp, mate);
  }

  function updateMaterialBars(fen) {
    const info = materialInfo(fen);
    const whiteAtBottom = state.orientation === "white";
    const topIsWhite = !whiteAtBottom;
    const top = topIsWhite ? info : { capByWhite: info.capByBlack, capByBlack: info.capByWhite, dw: info.db, db: info.dw };
    $("matTop").innerHTML = top.capByWhite ? `<span class="cap-w">${top.capByWhite}</span>` : "";
    const bottom = whiteAtBottom ? info : { capByWhite: info.capByBlack, capByBlack: info.capByWhite, dw: info.db, db: info.dw };
    let bHtml = bottom.capByWhite ? `<span class="cap-w">${bottom.capByWhite}</span>` : "";
    const diff = bottom.dw - bottom.db;
    if (diff > 0) bHtml += ` <b>+${diff}</b>`;
    $("matBottom").innerHTML = bHtml;
  }

  /* ---------------- navigation ---------------- */
  function gotoPly(p, keepExplore) {
    if (!state.review) return;
    if (!keepExplore) exitExplore(false);
    if (state.linePlay) killLinePlay();   // browsing cancels line playback; renderBoard below restores
    const n = state.review.moves.length;
    state.ply = Math.max(0, Math.min(n, p));
    renderBoard();
    graph.setCurrentPly(state.ply);
    highlightMoveList();
    updateCriticalRowActive();
    updateReviewCard();
    updateEngineTab();
    const m = state.review.moves[state.ply - 1];
    $("curMovePill").textContent = m ? `${m.moveNumber}${m.color === "white" ? "." : "..."} ${m.san}` : "Start";
    saveSessionPos();
  }

  function step(d) {
    if (state.trainerEx) return;      // arrow keys don't navigate a training card
    stopAutoplay();
    const ex = state.explore;                    // analysis mode: arrows walk the
    if (ex) {                                    // explored moves back and forward
      if (ex.moves.length) return exploreStep(d);
      if (d > 0 && ex.redo && ex.redo.length) return exploreStep(d);
      // nothing explored: ArrowRight just fell back to game navigation below
      if (!state.review && d < 0) return;        // already at the start position
    }
    gotoPly(state.ply + d);
  }

  /* undo/redo for explored moves: popping keeps the entry (cls stays), the live
     loop re-fills eval lines for the resulting position */
  function exploreStep(d) {
    const ex = state.explore;
    if (!ex) return;
    if (d < 0 && ex.moves.length) {
      (ex.redo = ex.redo || []).push(ex.moves.pop());
    } else if (d > 0 && ex.redo && ex.redo.length) {
      ex.moves.push(ex.redo.pop());
    } else { return; }
    ex.lastEval = null; ex.lines = null; ex.linesFen = null;
    saveExplore();
    updateExploreInfo();
    renderBoard();
    // in a bot game: an undo/redo that lands on the engine's turn hands the
    // move straight back to the engine, so the session keeps flowing
    if (state.playBot) {
      const mover = (currentFen().split(" ")[1] || "w") === "w" ? "white" : "black";
      if (mover !== state.playBotColor) engineReply();
    }
  }
  function startAutoplay() {
    if (state.playing || !state.review) return;
    killLinePlay();
    state.playing = setInterval(() => {
      if (state.ply >= state.review.moves.length) return stopAutoplay();
      gotoPly(state.ply + 1);
    }, 1200);
    $("btnPlay").innerHTML = ICON_PAUSE;
  }
  function stopAutoplay() {
    clearInterval(state.playing);
    state.playing = null;
    $("btnPlay").innerHTML = ICON_PLAY;
  }

  /* watch a line: replay an engine PV move-by-move on the board, then snap back */
  const LINE_MOVE_MS = 260, LINE_LINGER_MS = 600, LINE_MAX_MOVES = 24;
  function killLinePlay() {          // cancel playback without re-rendering the board
    if (!state.linePlay) return;
    state.linePlay.playing = false;
    state.linePlay = null;
    state.liveSuspended = false;
  }
  async function playLine(i) {
    if (state.quiz || editor.active) return;
    const ip = parseInt(i, 10);
    if (state.linePlay) {
      if (state.linePlay.i === ip) { stopLinePlay(); return; }   // toggle off
      killLinePlay();
    }
    const fen = currentFen();
    if (!fen) return;
    const ln = currentLines()[ip];
    if (!ln || !ln.pv || !ln.pv.length) return;
    // whole PV in one server call (instant start); crawl the old way only if
    // the bulk route is missing on an older server
    let steps = null;
    try {
      const ucis = ln.pv.slice(0, LINE_MAX_MOVES);
      const r = await post("/api/move/chain", { fen, ucis });
      steps = ucis.map((uci, k) => ({ uci, fen: r.fens[k + 1] }));
    } catch (e) { /* fall through and crawl */ }
    if (!steps) {
      steps = [];
      let f = fen;
      try {
        for (const uci of ln.pv.slice(0, LINE_MAX_MOVES)) {
          const r = await post("/api/move", { fen: f, uci });
          steps.push({ uci, fen: r.fen });
          f = r.fen;
        }
      } catch (e2) { toast("Line replay failed: " + e2.message); return; }
    }
    if (!steps.length) return;
    state.linePlay = { i: ip, base: fen, steps, playing: true };
    state.liveSuspended = true;
    renderTopMoves();
    for (let k = 0; k < state.linePlay.steps.length; k++) {
      const s = state.linePlay.steps[k];
      if (!state.linePlay || !state.linePlay.playing) return;
      playMoveSound(ln.san && ln.san[k] ? ln.san[k] : null);
      board.setPosition(s.fen, { lastMove: s.uci, arrows: [], badge: null });
      const end = Date.now() + LINE_MOVE_MS;   // abortable sleep keeps stops instant
      while (Date.now() < end) {
        if (!state.linePlay || !state.linePlay.playing) return;
        await new Promise((r) => setTimeout(r, 90));
      }
    }
    if (state.linePlay && state.linePlay.playing) {   // linger on the final position
      const end = Date.now() + LINE_LINGER_MS;
      while (Date.now() < end) {
        if (!state.linePlay || !state.linePlay.playing) return;
        await new Promise((r) => setTimeout(r, 90));
      }
      stopLinePlay();                                // snap back to the review position
    }
  }
  function stopLinePlay() {
    killLinePlay();
    renderBoard();       // restore the position you were reading
  }

  /* ---------- save the board as a PNG (reads the live board DOM) ---------- */
  async function savePositionImage() {
    const el = board.el;
    if (!el || !board.fen) { toast("Nothing on the board yet"); return; }
    const br = el.getBoundingClientRect();
    if (!br.width || !br.height) return;
    const S = 720, sq = S / 8;
    const cv = document.createElement("canvas");
    cv.width = cv.height = S;
    const g = cv.getContext("2d");
    const cs = getComputedStyle(el);
    const LIGHT = (cs.getPropertyValue("--light-sq") || "#ebecd0").trim() || "#ebecd0";
    const DARK = (cs.getPropertyValue("--dark-sq") || "#739552").trim() || "#739552";
    const sqNodes = el.querySelectorAll(".sq");
    const files = "abcdefgh";
    const whiteBottom = (state.orientation || board.orientation || "white") !== "black";
    for (let r = 0; r < 8; r++) {
      for (let c = 0; c < 8; c++) {
        const darkSq = ((r + c) % 2) === 1;
        const node = sqNodes[r * 8 + c];
        // prefer the live square color so themes/orientation always match
        g.fillStyle = node ? getComputedStyle(node).backgroundColor
                           : (darkSq ? DARK : LIGHT);
        g.fillRect(c * sq, r * sq, sq, sq);
      }
    }
    // coordinates in the corners of the edge squares
    g.font = `700 ${Math.round(sq * 0.17)}px ui-sans-serif, system-ui, sans-serif`;
    for (let i = 0; i < 8; i++) {
      let node = sqNodes[7 * 8 + i];
      g.fillStyle = node && node.classList.contains("dark") ? LIGHT : DARK;
      g.fillText(files[whiteBottom ? i : 7 - i], i * sq + 3, S - 4);   // file letters
      node = sqNodes[i * 8];
      g.fillStyle = node && node.classList.contains("dark") ? LIGHT : DARK;
      g.fillText(String(whiteBottom ? 8 - i : i + 1), 3, i * sq + sq * 0.2); // rank numbers
    }
    // pieces: sample the rendered piece nodes
    for (const node of el.querySelectorAll(".piece")) {
      const pr = node.getBoundingClientRect();
      if (!pr.width || !pr.height) continue;
      const x = (pr.left - br.left) / br.width * S;
      const y = (pr.top - br.top) / br.height * S;
      const w = pr.width / br.width * S, h = pr.height / br.height * S;
      const img = node.querySelector("img");
      try {
        if (img) {
          if (img.complete && img.naturalWidth) g.drawImage(img, x, y, w, h);
          else { await new Promise((ok, err) => { img.onload = ok; img.onerror = err; setTimeout(ok, 2500); });
                 g.drawImage(img, x, y, w, h); }
        } else {
          const fb = node.querySelector(".fallback");
          const glyph = ((fb && fb.textContent) || node.textContent || "").trim();
          if (glyph) {
            g.fillStyle = getComputedStyle(fb || node).color || "#222";
            g.font = `${Math.round(h * 0.8)}px serif`;
            g.textAlign = "center"; g.textBaseline = "middle";
            g.fillText(glyph, x + w / 2, y + h * 0.54);
          }
        }
      } catch (e) { /* skip a piece that cannot be rasterized */ }
    }
    const blob = await new Promise((ok) => cv.toBlob(ok, "image/png"));
    if (!blob) { toast("Could not export the board"); return; }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "chess-" + new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-") + ".png";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast("Board image saved");
  }

  /* ---------- share-link for the current position ---------- */
  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (e) { toast("Copy failed"); }
    ta.remove();
  }
  function copyShareLink() {
    const fen = currentFen();
    if (!fen) return;
    const url = `${location.origin}${location.pathname}#fen=${fen.replace(/ /g, "_")}`;
    const done = () => toast("Link copied - anyone opening it lands on this exact position");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done).catch(() => fallbackCopy(url, done));
    } else fallbackCopy(url, done);
  }
  // boot: accept "#fen=<FEN>" links (spaces encoded as underscores)
  function maybeLoadShareLink() {
    if (!location.hash.startsWith("#fen=")) return false;
    let raw = "";
    try { raw = decodeURIComponent(location.hash.slice(5)); } catch (e) { return false; }
    raw = raw.replace(/_/g, " ").trim().replace(/\s+/g, " ");
    const f = raw.split(" ");
    if (f.length < 4 || f[0].split("/").length !== 8 || (f[1] !== "w" && f[1] !== "b")) return false;
    try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* keep hash */ }
    state.customFen = raw;
    try { localStorage.setItem("crs_last_custom", raw); } catch (e) { /* private mode */ }
    return true;
  }

  /* ---------------- explore mode ---------------- */
  function enterExplore(firstMove) {
    state.explore = {
      fromPly: state.ply,
      // bare board (no review / custom position): analyze from whatever is on the board
      baseFen: currentFen() || board.fen || START_FEN,
      moves: firstMove ? [firstMove] : [],
      lastEval: null, lines: null, linesFen: null,
    };
    $("exploreBanner").hidden = false;
    $("exploreFrom").textContent = state.customFen ? "custom position"
      : (state.ply === 0 ? "start" : "move " + state.ply);
    saveExplore();
    updateExploreInfo();
    switchTab("engine");
  }
  /* ---------------- "home": plain start-position analysis ---------------- */
  function goHome() {
    // leave anything that owns the board, using each mode's own exit routine
    if (state.playBot) stopPlayBot();
    if (state.puzzle) puzzleQuit();
    if (state.trainerEx) trainerQuit();
    if (state.quiz) quizQuit();
    if (state.repTrain) repTrainQuit();
    if (state.linePlay) stopLinePlay();
    stopAutoplay();
    if (editor.active) toggleEditor(false);
    repStopPreview();
    exitExplore(false);
    state.review = null;      // home = the bare analysis workbench
    state.customFen = null;
    try {                     // don't auto-restore a game we walked away from
      localStorage.removeItem("crs_last_review");
      localStorage.removeItem("crs_last_pos");
      localStorage.removeItem("crs_last_custom");
    } catch (e) { /* private mode */ }
    state.live = { fen: null, lines: null, engine: null, depth: null, cycle: 0 };
    board.setPosition(START_FEN);
    enterExplore(); // analysis-ready, Engine tab active -> top moves stream in
    renderBoard();
  }

  /* ---------------- play vs engine (a keyed sidebar session, not analysis) ---------------- */
  function updatePlayBotUi() {
    const side = $("botSideSelect");
    if (side) {
      if (side.value !== state.playBotColor) side.value = state.playBotColor;
      side.hidden = !state.playBot;
    }
    if ($("btnBotHint")) $("btnBotHint").hidden = !state.playBot;
    $("btnPlayBot").classList.toggle("bot-on", !!state.playBot);
    $("btnPlayBot").title = state.playBot
      ? "Stop playing the engine"
      : "Play vs engine - the engine answers your moves";
    const info = $("exploreMoveInfo");
    if (info) info.textContent = !state.playBot
      ? "engine evaluates as you play"
      : state.playBotColor === "black"
        ? "You play Black - the engine opens as White"
        : "You play White - the engine answers. Hit Hint for a pointer";
  }
  function startPlayBot(color) {
    state.playBotColor = color === "black" ? "black" : "white";
    goHome();                 // leave every mode cleanly, land on the start position
    state.playBot = true;
    state.hintUci = null;
    if (state.playBotColor === "black") {
      board.flip();           // your pieces at the bottom
      state.orientation = board.orientation;
    }
    updatePlayBotUi();
    renderBoard();            // defaultArrows() suppresses the engine arrows
    if (state.playBotColor === "black") engineReply();    // the engine opens as White
    else toast("Playing the engine - make your first move (White)", 3500);
  }
  function stopPlayBot() {
    if (!state.playBot) return;
    state.playBot = false;
    state.hintUci = null;
    updatePlayBotUi();
  }
  /* one-tap hint: mark the engine's suggested move; lives until you move again */
  async function botHint() {
    if (!state.playBot) return;
    const fen = currentFen();
    if (!fen) return;
    if (state.hintUci) { state.hintUci = null; renderBoard(); return; }   // toggle off
    try {
      const res = await post("/api/eval", {
        fen, engine: $("engineSelect").value, depth: 10, movetime: 600, multipv: 1,
      });
      if (!state.playBot || currentFen() !== fen) return;
      const ln = res && res.lines && res.lines[0];
      if (!ln || !ln.pv || !ln.pv.length) { toast("No hint available in this position"); return; }
      state.hintUci = ln.pv[0];
      renderBoard();
      if (ln.san && ln.san[0]) toast(`Hint: ${ln.san[0]} - click Hint again to hide`, 3500);
    } catch (e) { toast("Hint failed - engine busy"); }
  }

  function exitExplore(rerender) {
    if (!state.explore) return;
    state.explore = null;
    saveExplore();
    $("exploreBanner").hidden = true;
    if (rerender !== false) renderBoard();
  }

  async function onMoveAttempt(mv, from, to) {
    // analysis is always available: on a bare board (no game loaded) playing a
    // move simply opens explore mode from the current position
    if (!currentFen() && !state.quiz && !state.repTrain) enterExplore();
    // multiple matching moves = promotion choice
    const matches = board.legal.filter((m) => m.uci.startsWith(from + to));
    let chosen = mv;
    if (matches.length > 1 && !/[nbrq]/.test(mv.uci[4] || "")) {
      chosen = await pickPromotion(from, to, matches);
      if (!chosen) { renderBoard(); return; }
    }
    const legalCount = board.legal.length;
    if (state.quiz) return quizGuess(chosen);
    if (state.repTrain) return repTrainGuess(chosen);
    if (state.trainerEx) return trainerGuess(chosen);
    if (state.puzzle) return puzzleGuess(chosen);
    state.hintUci = null;      // moving consumes the Hint arrow
    // following the game line?
    if (state.review && (!state.explore || state.explore.moves.length === 0)) {
      if (state.ply < state.review.moves.length && state.review.moves[state.ply].uci === chosen.uci) {
        playMoveSound(state.review.moves[state.ply].san);
        return gotoPly(state.ply + 1);
      }
    }
    const baseFen = currentFen();
    try {
      const res = await post("/api/move", { fen: baseFen, uci: chosen.uci });
      const entry = { uci: chosen.uci, san: res.san, fen: res.fen, legalCount, checkmate: res.checkmate };
      playMoveSound(res.san);
      if (!state.explore) { enterExplore(entry); saveExplore(); }
      else { state.explore.moves.push(entry); state.explore.redo = []; saveExplore(); }
      classifyLastExploreMove();   // don't wait for the live loop: a delivered mate
                                   // never yields eval lines, so it must run now
      renderBoard();
      if (state.playBot) await engineReply();
    } catch (e) {
      toast("Illegal move");
    }
  }

  async function engineReply() {
    const fen = currentFen();
    const preset = DEPTHS[$("depthSelect").value] || DEPTHS.balanced;
    const elo = +(($("eloSelect") && $("eloSelect").value) || 0);
    try {
      const res = await post("/api/eval", {
        fen, engine: $("engineSelect").value,
        depth: elo ? 8 : Math.min(10, preset.depth), movetime: elo ? 350 : 500,
        multipv: 3, elo,
      });
      if (!res.lines || !res.lines.length || !res.lines[0].pv.length) return;
      const uci = res.lines[0].pv[0];
      const mvRes = await post("/api/move", { fen, uci });
      if (state.explore) {
        state.explore.lastEval = res.lines[0];
        state.explore.lines = res.lines;
        state.explore.linesFen = fen;
      }
      const legalCount = await legalFor(fen).then((l) => l.moves.length).catch(() => 0);
      state.explore.moves.push({ uci, san: mvRes.san, fen: mvRes.fen, legalCount, checkmate: mvRes.checkmate });
      playMoveSound(mvRes.san);
      saveExplore();
      renderBoard();
      classifyLastExploreMove();
    } catch (e) { /* engine hiccup: ignore */ }
  }

  /* ---- live classification of explored moves (same formulas as the server) ---- */
  const EXPLORE_LABELS = {
    best: "Best", excellent: "Excellent", good: "Good", great: "Great Move",
    inaccuracy: "Inaccuracy", mistake: "Mistake", miss: "Miss", blunder: "Blunder", forced: "Forced",
  };

  function cpMoverPct(lines, moverIsWhite) {
    const ln = lines && lines[0];
    if (!ln) return null;
    if (ln.mateWhite != null && ln.mateWhite !== 0) {
      const mateForMover = moverIsWhite ? ln.mateWhite > 0 : ln.mateWhite < 0;
      return mateForMover ? 2000 : -2000;
    }
    return ln.cpWhite * (moverIsWhite ? 1 : -1);
  }

  function mateDistMover(line, moverIsWhite) {
    // raw mate distance in mover POV: >0 mover forces mate in N,
    // <0 mover is being mated in N, null no mate score
    const mw = line && line.mateWhite;
    if (mw == null || mw === 0) return null;
    return moverIsWhite ? mw : -mw;
  }

  async function evalQuick(fen) {
    const preset = DEPTHS[$("depthSelect").value] || DEPTHS.balanced;
    const res = await post("/api/eval", {
      fen, engine: $("engineSelect").value,
      depth: Math.min(10, preset.depth), movetime: 400, multipv: 3,
    });
    return res.lines || [];
  }

  async function classifyLastExploreMove() {
    const ex = state.explore;
    if (!ex || !ex.moves.length) return;
    const entry = ex.moves[ex.moves.length - 1];
    if (entry.cls) { updateExploreBadge(); return; }
    try {
      const fenBefore = ex.moves.length === 1 ? ex.baseFen : ex.moves[ex.moves.length - 2].fen;
      const moverIsWhite = fenBefore.split(" ")[1] === "w";
      // checkmate delivered = the fastest possible move, always "best". The eval
      // endpoint returns no lines for a finished game, so classify before they matter.
      if (entry.checkmate) {
        entry.cls = "best";
        entry.accuracy = 100;
        updateExploreBadge();
        updateExploreInfo();
        saveExplore();
        return;
      }
      let before;
      if (ex.linesFen === fenBefore && ex.lines) before = ex.lines;
      else if (state.review && fenBefore === state.review.positions[ex.fromPly].fen) {
        before = state.review.positions[ex.fromPly].lines;  // review lines are deeper
      } else before = await evalQuick(fenBefore);
      let after = ex.linesFen === entry.fen && ex.lines ? ex.lines : null;
      if (!after) after = await evalQuick(entry.fen);
      const cpB = cpMoverPct(before, moverIsWhite);
      const cpA = cpMoverPct(after, moverIsWhite);
      if (cpB == null || cpA == null) return;
      const winB = winShare(cpB), winA = winShare(cpA);
      const drop = Math.max(0, winB - winA);
      const loss = Math.max(0, cpB - cpA);
      entry.accuracy = Math.max(0, Math.min(100, 103.1668 * Math.exp(-0.04354 * drop) - 3.1669));
      const playedBest = before[0] && before[0].pv && before[0].pv[0] === entry.uci;
      // mate distances are compared directly: mate evals collapse the whole
      // "mate in N" spectrum to a flat value, so a slower mate must not grade
      // as "best" just because the loss looks like 0 cp
      const bestMate = before[0] ? mateDistMover(before[0], moverIsWhite) : null;
      const playedMate = after[0] ? mateDistMover(after[0], moverIsWhite) : null;
      if (entry.legalCount === 1) entry.cls = "forced";
      else if (playedMate != null && playedMate > 0) {
        // the move keeps a forced mate: the after-position distance counts the
        // REMAINING mating moves (+1 for the move itself, matching the best
        // line's distance from the start); as fast as it or the engine had seen
        // no mate at all -> best; a slower mate keeps the win but is not the
        // engine's top choice
        entry.cls = (!bestMate || bestMate <= 0 || playedMate + 1 <= bestMate) ? "best" : "excellent";
      }
      else {
        const bestCp = cpMoverPct([before[0]], moverIsWhite);
        const hadWin = winB >= 88;
        if (hadWin && winA <= 62 && drop >= 22) entry.cls = "miss";
        else if (playedBest || loss <= 12) {
          const second = before[1] ? cpMoverPct([before[1]], moverIsWhite) : null;
          if (second != null && winShare(second) <= winB - 15 && loss <= 25 && winA >= winB - 8) entry.cls = "great";
          else entry.cls = loss <= 12 ? "best" : "excellent";
        } else if (drop < 2 && loss <= 60) entry.cls = "excellent";
        else if (drop < 6) entry.cls = "good";
        else if (drop < 12) entry.cls = "inaccuracy";
        else if (drop < 20) entry.cls = "mistake";
        else entry.cls = "blunder";
      }
      updateExploreBadge();
      updateExploreInfo();
      saveExplore();   // cls/accuracy computed after the live eval - persist them
    } catch (e) { /* classification is best-effort */ }
  }

  function updateExploreBadge() {
    const ex = state.explore;
    if (!ex || !ex.moves.length) return;
    const entry = ex.moves[ex.moves.length - 1];
    if (!entry.cls || entry.cls === "good" || entry.cls === "forced") {
      board.setPosition(currentFen(), { badge: null });
      return;
    }
    board.setPosition(currentFen(), { badge: { sq: entry.uci.slice(2, 4), cls: entry.cls } });
  }

  function updateExploreInfo() {
    const el = $("exploreMoveInfo");
    if (!el) return;
    const ex = state.explore;
    if (!ex || !ex.moves.length) { el.textContent = "engine evaluates as you play"; return; }
    const entry = ex.moves[ex.moves.length - 1];
    if (!entry.cls) { el.textContent = `${entry.san} � evaluating�`; return; }
    const label = EXPLORE_LABELS[entry.cls] || entry.cls;
    const acc = entry.accuracy != null ? ` � ${entry.accuracy.toFixed(0)}%` : "";
    el.innerHTML = `last: <b>${entry.san}</b> ${smallChip(entry.cls)} ${label}${acc}`;
  }

  function pickPromotion(from, to, matches) {
    return new Promise((resolve) => {
      const ov = $("promoOverlay");
      ov.innerHTML = "";
      const box = document.createElement("div");
      box.className = "choices";
      const color = to[1] === "8" ? "w" : "b"; // the pawn reaching the last rank is the mover
      for (const m of matches) {
        const letter = m.uci[4] || "q";
        const code = color + letter.toUpperCase();
        const d = document.createElement("div");
        d.className = "choice";
        const img = document.createElement("img");
        img.src = `${pieceDir}${code}.svg`;
        img.style.width = "100%"; img.style.height = "100%";
        img.onerror = () => (img.replaceWith(Object.assign(document.createElement("span"), { textContent: { q: "?", r: "?", b: "?", n: "?" }[letter] || "?", style: "font-size:34px" })));
        d.appendChild(img);
        d.onclick = () => { ov.hidden = true; resolve(m); };
        box.appendChild(d);
      }
      ov.appendChild(box);
      ov.hidden = false;
      ov.onclick = (e) => { if (e.target === ov) { ov.hidden = true; resolve(null); } };
    });
  }

  /* ---------------- continuous live analysis ----------------
     One persistent loop. While not paused, it evaluates the current position
     with an iterative-deepening ladder (350ms -> 700ms -> 1400ms -> 2800ms per
     cycle), refreshing the eval bar, top-moves box, arrows and engine lines
     after every cycle. Navigation to a new position restarts the ladder
     automatically; the pause button stops it entirely. */
  async function liveEngineLoop() {
    for (;;) {
      if (!liveActive()) { await sleep(300); continue; }
      const fen = currentFen();
      if (!fen) { await sleep(400); continue; }
      if (state.live.fen === fen && (state.live.cycle || 0) >= 4) {
        await sleep(500);   // settled at max depth for this position; watch for changes
        continue;
      }
      const cycle = state.live.fen === fen ? (state.live.cycle || 0) : 0;
      const movetime = [350, 700, 1400, 2800][Math.min(cycle, 3)];
      const engine = $("engineSelect").value || "stockfish";
      try {
        const res = await post("/api/eval", { fen, engine, depth: 20, movetime, multipv: 3 });
        if (!liveActive() || currentFen() !== fen) continue; // stale � drop it
        if (res.lines && res.lines.length) {
          state.live = { fen, lines: res.lines, engine: res.engine, depth: res.lines[0].depth, cycle: cycle + 1 };
          if (state.explore) {
            state.explore.lastEval = res.lines[0];
            state.explore.lines = res.lines;
            state.explore.linesFen = fen;
            classifyLastExploreMove();
          }
          updateEvalBar();
          renderTopMoves();
          renderLines($("liveLines"), res.lines);
          if (state.arrowsOn) board.setArrows(defaultArrows());
          $("liveEngineName").textContent = res.engine;
          $("liveDepth").textContent = "d" + res.lines[0].depth;
        } else {
          state.live = { fen, lines: null, engine: null, depth: null, cycle: 9 }; // game-over position
        }
      } catch (e) {
        await sleep(800);
      }
    }
  }

  function updateLiveBtn() {
    const b = $("btnLive");
    b.innerHTML = state.liveOn ? ICON_PAUSE : ICON_PLAY;
    b.title = state.liveOn ? "Pause live engine analysis" : "Resume live engine analysis";
    b.classList.toggle("live-on", state.liveOn);
  }

  /* real-time depth/node readout while the engine is searching */
  async function pollEngineState() {
    if (!liveActive()) return;
    const res = await api("/api/enginestate").catch(() => null);
    if (!res || !res.engines) return;
    const st = res.engines[$("engineSelect").value];
    if (!st || !st.searching || !st.depth) return;
    const nodesTxt = st.nodes >= 1000000 ? (st.nodes / 1e6).toFixed(1) + "M"
      : st.nodes >= 1000 ? Math.round(st.nodes / 1000) + "k" : String(st.nodes);
    $("liveDepth").textContent = `d${st.depth} � ${nodesTxt}n`;
    $("topMovesMeta").textContent = `depth ${st.depth} � ${nodesTxt} nodes`;
  }
  setInterval(() => { pollEngineState().catch(() => {}); }, 300);

  /* ---------------- panels ---------------- */
  function chip(cls, big) {
    const sym = badgeSymbol(cls);
    if (!sym && cls !== "book") return `<span class="class-chip ${big ? "big" : ""} cls-good" style="background:#96af8b"></span>`;
    return `<span class="class-chip ${big ? "big" : ""} cls-${cls}">${sym}</span>`;
  }
  function smallChip(cls) {
    const sym = badgeSymbol(cls);
    if (!sym) return "";
    return `<span class="chip cls-${cls}">${sym}</span>`;
  }

  /* engine-line eval display: always white-perspective cp from the payload */
  function lineEval(ln) {
    const cp = (typeof ln.cpWhite === "number" && isFinite(ln.cpWhite)) ? ln.cpWhite : 0;
    const mate = (typeof ln.mateWhite === "number") ? ln.mateWhite : null;
    return fmtEval(cp, mate);
  }

  function renderLines(el, lines) {
    el.innerHTML = (lines || []).map((ln, i) => {
      const ev = lineEval(ln);
      const cls = (typeof ln.cpWhite === "number" && ln.cpWhite >= 0) || ln.cpWhite === undefined ? "w" : "b";
      const sans = (ln.san || []).slice(0, 12).join(" ");
      const rankCls = i < 3 ? ` rank${i + 1}` : "";
      const playing = state.linePlay && state.linePlay.i === i && state.linePlay.playing;
      return `<div class="line${rankCls}" data-uci="${(ln.pv && ln.pv[0]) || ""}" data-rank="${i}">
        <button class="lineplay" data-i="${i}" type="button" title="${playing ? "Stop replaying this line" : "Watch this line move by move"}">${playing ? "\u25A0" : "\u25B6"}</button>
        <span class="ev ${cls}">${ev.startsWith("-") ? ev : "+" + ev}</span><span class="mv">${sans}</span></div>`;
    }).join("");
  }

  function renderEngineLinesForPly() {
    if (!state.review) return;
    const pos = state.review.positions[state.ply];
    if (pos && pos.lines) renderLines($("liveLines"), pos.lines);
  }
  function updateEngineTab() {
    if (state.explore) return;
    if (liveActive()) return; // the live loop drives this tab
    if (!state.review) return;
    const pos = state.review.positions[state.ply];
    $("liveEngineName").textContent = state.review.analysis.engine;
    $("liveDepth").textContent = "d" + state.review.analysis.depth;
    renderLines($("liveLines"), pos ? pos.lines : []);
  }

  function updateReviewCard() {
    const card = $("rcCard"), empty = $("reviewEmpty");
    if (!state.review) { card.hidden = true; empty.hidden = false; return; }
    card.hidden = false; empty.hidden = true;
    const m = state.review.moves[state.ply - 1];
    if (!m) {
      card.innerHTML = `<div class="movecard"><div class="movecard-head">
        <span class="class-chip big" style="background:#8b8b8b">?</span>
        <div class="movecard-title"><div class="rc-label">Starting position</div>
        <div class="rc-move text-muted">${state.review.game.opening || ""}</div></div></div>
        <p class="coach">Step through the moves to see classifications. Press ? to advance.</p></div>`;
      renderEngineLinesForPly();
      return;
    }
    const posB = state.review.positions[state.ply - 1];
    card.innerHTML = `<div class="movecard">
      <div class="movecard-head">
        ${chip(m.class, true)}
        <div class="movecard-title">
          <div class="rc-label">${m.label}</div>
          <div class="rc-move text-muted">${m.moveNumber}${m.color === "white" ? "." : "..."} ${m.san}</div>
        </div>
        <div class="movecard-acc"><div class="v">${m.accuracy.toFixed(0)}%</div><div class="l">accuracy</div></div>
      </div>
      <div class="evalswing">
        <div class="evalswing-bar"><div class="es-after" id="esAfterFill" style="background:#ffffff"></div></div>
        <div class="evalswing-lbl"><span>before ${fmtEval(m.cpBefore, posB.mateWhite)}</span>
        <span>after ${fmtEval(m.cpAfter, state.review.positions[state.ply].mateWhite)}</span></div>
      </div>
      ${(m.epl > 0.005 || m.loss > 25) ? `<div class="quiz-answer text-small" style="margin-top:8px">Quality loss: <b>${m.epl != null ? m.epl.toFixed(2) : "?"} EP</b> � <b>${Math.round(m.loss)} cp</b></div>` : ""}
      <p class="coach">${m.comment || ""}</p>
      <div class="bestline">Best move: <b>${m.best.san}</b>
        <button class="btn btn-xs" id="rcShow" type="button">Show</button></div>
    </div>`;
    const fill = card.querySelector("#esAfterFill");
    const share = Math.max(3, Math.min(97, winShare(m.cpAfter)));
    fill.style.left = "0";
    fill.style.width = share + "%";
    card.querySelector("#rcShow").onclick = () => {
      state.arrowsOn = true;
      gotoPly(state.ply - 1);
    };
    renderEngineLinesForPly();
  }

  function buildMoveList() {
    const el = $("moveList");
    if (!state.review) { el.innerHTML = `<div class="empty-hint small">Moves will appear here.</div>`; return; }
    const rows = [];
    const moves = state.review.moves;
    for (let i = 0; i < moves.length; i += 2) {
      const w = moves[i], b = moves[i + 1];
      rows.push(`<div class="mrow" data-num="${w.moveNumber}">
        <span class="mnum">${w.moveNumber}.</span>
        <span class="mv" data-ply="${i + 1}"><span class="san">${w.san}</span>${smallChip(w.class)}</span>
        ${b ? `<span class="mv" data-ply="${i + 2}"><span class="san">${b.san}</span>${smallChip(b.class)}</span>` : "<span></span>"}
      </div>`);
    }
    el.innerHTML = rows.join("");
    el.querySelectorAll(".mv").forEach((mv) => {
      mv.onclick = () => gotoPly(+mv.dataset.ply);
    });
    highlightMoveList();
  }

  function highlightMoveList() {
    document.querySelectorAll("#moveList .mv").forEach((mv) => {
      mv.classList.toggle("current", +mv.dataset.ply === state.ply && !state.explore);
    });
    const cur = document.querySelector("#moveList .mv.current");
    if (cur) cur.scrollIntoView({ block: "nearest" });
  }

  function renderMoments() {
    const el = $("momentsList");
    const has = state.review && state.review.keyMoments.length;
    $("momentsHead").hidden = !has;
    if (!has) {
      el.innerHTML = ``;
      return;
    }
    el.innerHTML = state.review.keyMoments.map((k) => `
      <div class="moment" data-ply="${k.ply}">
        ${chip(k.kind === "turning" ? "best" : k.kind)}
        <div><div class="m-text">${k.title}</div><div class="m-sub">${k.text}</div></div>
      </div>`).join("");
    el.querySelectorAll(".moment").forEach((m) => {
      m.onclick = () => { gotoPly(+m.dataset.ply); switchTab("report"); };
    });
  }

  function renderTimeReport() {
    const el = $("timeReport");
    const tr = state.review && state.review.timeReport;
    $("timeHead").hidden = !tr;
    if (!tr) {
      el.innerHTML = ``;
      return;
    }
    const phaseName = { early: "Early game", middle: "Middle game", late: "Late game" };
    let html = `<table class="ttable"><tr><th>Phase</th><th>Moves</th><th>Avg time</th><th>Accuracy</th></tr>`;
    for (const [k, v] of Object.entries(tr.phase)) {
      html += `<tr><td>${phaseName[k]}</td><td>${v.plies}</td><td>${v.avgTime ? v.avgTime.toFixed(1) + "s" : "�"}</td><td>${v.accuracy ? v.accuracy + "%" : "�"}</td></tr>`;
    }
    html += `</table>`;
    const speedName = { fast: "< 5s / move", normal: "5�20s", slow: "> 20s" };
    html += `<h4 class="panel-sub">Accuracy by time spent</h4><table class="ttable">
      <tr><th>Speed</th><th>Moves</th><th>Accuracy</th><th>Blunders</th></tr>`;
    for (const [k, v] of Object.entries(tr.bySpeed)) {
      html += `<tr><td>${speedName[k]}</td><td>${v.moves}</td><td>${v.accuracy ? v.accuracy + "%" : "�"}</td><td>${v.blunders || 0}</td></tr>`;
    }
    html += `</table>`;
    if (tr.timeTroubleErrors) html += `<p class="coach">? ${tr.timeTroubleErrors} serious error(s) were played in time trouble.</p>`;
    el.innerHTML = html;
  }

  function renderSummary() {
    const el = $("summaryBody");
    if (!state.review) { el.innerHTML = `<div class="empty-hint small">Run a review first.</div>`; return; }
    const R = state.review;
    const countsHtml = (p) => ["brilliant", "great", "best", "excellent", "book", "inaccuracy", "mistake", "miss", "blunder"]
      .filter((k) => p.counts[k])
      .map((k) => `<span class="count-chip">${smallChip(k)} ${p.counts[k]}</span>`)
      .join("");
    const PHASE_LABELS = { opening: "Opening", middlegame: "Middlegame", endgame: "Endgame" };
    const phaseAccs = (p) => ["opening", "middlegame", "endgame"].map((ph) => {
      const v = p.phases[ph];
      const acc = v.accuracy;
      return `<div class="pa"><div class="l">${PHASE_LABELS[ph]}${v.plies ? ` (${v.plies})` : ""}</div>
        <div class="v">${acc != null ? acc + "%" : "--"}</div>
        <div class="a text-muted">${v.acpl != null ? v.acpl + " acpl" : ""}</div></div>`;
    }).join("");
    const pcard = (color, label) => {
      const p = R.players[color];
      return `<div class="pcard"><div class="row">
        <span class="avatar ${color === "white" ? "w" : "b"}">${label[0]}</span>
        <div><div class="p-name">${p.name || label}</div>
        <div class="est">played like a <b>${p.estRating}</b>-rated player � ACPL ${p.acpl}</div></div>
        <div style="margin-left:auto;text-align:right"><div class="big-acc">${p.accuracy}%</div>
        <div class="est">overall accuracy</div></div>
      </div>
      <div class="phase-accs">${phaseAccs(p)}</div>
      <div class="counts">${countsHtml(p)}</div></div>`;
    };
    el.innerHTML = pcard("white", "White") + pcard("black", "Black") +
      `<h4 class="panel-sub">Next level plan</h4>
       <ul class="bullets">${R.summary.bullets.map((b) => `<li>${b}</li>`).join("")}</ul>`;
  }

  function renderAccuracySummary() {
    const el = $("accSummary");
    if (!state.review) {
      el.innerHTML = state.customFen
        ? `<div class="accsum-empty">Custom position loaded � live analysis is running. See the Engine tab for lines, or play moves on the board to explore.</div>`
        : `<div class="accsum-empty">Load a game and run a review to see accuracies, classifications and key moments here.</div>`;
      return;
    }
    const R = state.review;
    const g = R.game;
    const COUNT_ORDER = ["brilliant", "great", "best", "excellent", "good", "book",
                         "inaccuracy", "mistake", "miss", "blunder"];
    const countRow = (p) => `<div class="as-counts">` + COUNT_ORDER
      .filter((k) => p.counts[k])
      .map((k) => `<span class="as-count">${smallChip(k)}<b>${p.counts[k]}</b></span>`)
      .join("") + `</div>`;
    const row = (color, label) => {
      const p = R.players[color];
      return `<div class="as-row">
        <span class="avatar ${color === "white" ? "w" : "b"}">${label[0]}</span>
        <span class="as-name">${p.name || label}${g[color === "white" ? "WhiteElo" : "BlackElo"] ? ` <span class="text-muted">(${g[color === "white" ? "WhiteElo" : "BlackElo"]})</span>` : ""}</span>
        <span class="as-acc">${p.accuracy}%<small>accuracy</small></span>
        ${countRow(p)}
      </div>`;
    };
    const meta = [
      g.eco ? `${g.eco} � ${g.opening}` : g.opening,
      [g.Event, g.Date].filter(Boolean).join(" � "),
      g.Result,
    ].filter(Boolean).join("  |  ");
    const nBad = (c) => R.moves.filter((m) => m.color === c && DRILL_CLASSES.includes(m.class)).length;
    const nW = nBad("white"), nB = nBad("black");
    const drillBtn = (id, n, side) => n
      ? `<button id="${id}" class="btn btn-sm" type="button">${FIX_SVG} Fix ${n} mistake${n === 1 ? "" : "s"} (${side})</button>` : "";
    const crit = criticalPlies();
    const momentsHtml = crit.length ? `<div class="cm-card">
      <div class="cm-head">Critical moments <span class="cm-hintkey"><kbd>[</kbd> <kbd>]</kbd> to jump</span></div>
      ${crit.map((i) => {
        const m = R.moves[i];
        const sw = m.color === "white" ? (m.cpBefore - m.cpAfter) : (m.cpAfter - m.cpBefore);
        const swTxt = Number.isFinite(sw) && sw > 0 ? `<span class="cm-loss">&minus;${(Math.min(sw, 1000) / 100).toFixed(1)}</span>` : "";
        return `<button class="cm-row" type="button" data-ply="${i}">
          <span class="cm-move">${m.moveNumber}${m.color === "white" ? "." : "..."} ${m.san}</span>
          ${smallChip(m.class)}${swTxt}<span class="cm-jump">&rarr;</span>
        </button>`;
      }).join("")}</div>` : "";
    el.innerHTML = row("white", "White") + row("black", "Black") +
      (meta ? `<div class="as-meta">${meta}</div>` : "") +
      `<div class="as-train">
        <button id="btnTrainW" class="btn btn-sm" type="button">${TARGET_SVG} Train as White</button>
        <button id="btnTrainB" class="btn btn-sm" type="button">${TARGET_SVG} Train as Black</button>
      </div>` +
      ((nW || nB) ? `<div class="as-train">${drillBtn("btnDrillW", nW, "as White")}${drillBtn("btnDrillB", nB, "as Black")}</div>` : "") +
      momentsHtml;
    const tw = el.querySelector("#btnTrainW");
    if (tw) tw.onclick = () => quizStart("white");
    const tb = el.querySelector("#btnTrainB");
    if (tb) tb.onclick = () => quizStart("black");
    const dw = el.querySelector("#btnDrillW");
    if (dw) dw.onclick = () => drillStart("white");
    const db = el.querySelector("#btnDrillB");
    if (db) db.onclick = () => drillStart("black");
    el.querySelectorAll(".cm-row").forEach((r) => {
      r.onclick = () => gotoPly(+r.dataset.ply + 1);
    });
    updateCriticalRowActive();
  }

  /* critical moments: the error plies, [ / ] keyboard jumps + clickable list.
     Row "ply" = index of the mistake move; jump lands on the position AFTER it. */
  function criticalPlies() {
    if (!state.review) return [];
    const out = [];
    for (let i = 0; i < state.review.moves.length; i++) {
      if (DRILL_CLASSES.includes(state.review.moves[i].class)) out.push(i);
    }
    return out;
  }
  function jumpMoment(dir) {
    const list = criticalPlies();
    if (!state.review || !list.length) { toast("No critical moves in this game"); return; }
    let target = null;
    if (dir > 0) target = list.find((i) => i + 1 > state.ply);
    else {
      for (let k = list.length - 1; k >= 0; k--) {
        if (list[k] + 1 < state.ply) { target = list[k]; break; }
      }
    }
    if (target == null) {
      toast(dir > 0 ? "No critical moves after this point" : "No critical moves before this point");
      return;
    }
    const m = state.review.moves[target];
    gotoPly(target + 1);
    toast(`Critical: ${m.moveNumber}${m.color === "white" ? "." : "..."} ${m.san} (${m.label})`);
  }
  function updateCriticalRowActive() {
    const el = $("accSummary");
    if (!el) return;
    el.querySelectorAll(".cm-row").forEach((r) => r.classList.toggle("active", +r.dataset.ply === state.ply - 1));
  }

  function renderGameHead() {
    const g = state.review.game;
    $("nameTop").textContent = state.orientation === "white" ? (g.Black || "Black") : (g.White || "White");
    $("nameBottom").textContent = state.orientation === "white" ? (g.White || "White") : (g.Black || "Black");
    $("avatarTop").textContent = state.orientation === "white" ? "B" : "W";
    $("avatarBottom").textContent = state.orientation === "white" ? "W" : "B";
    $("avatarTop").className = "avatar " + (state.orientation === "white" ? "b" : "w");
    $("avatarBottom").className = "avatar " + (state.orientation === "white" ? "w" : "b");
    const wAcc = state.review.players.white.accuracy, bAcc = state.review.players.black.accuracy;
    const topAcc = state.orientation === "white" ? bAcc : wAcc;
    const botAcc = state.orientation === "white" ? wAcc : bAcc;
    $("accTop").hidden = false; $("accTop").textContent = topAcc + "%";
    $("accBottom").hidden = false; $("accBottom").textContent = botAcc + "%";
    $("eloTop").textContent = state.orientation === "white" ? (g.BlackElo || "") : (g.WhiteElo || "");
    $("eloBottom").textContent = state.orientation === "white" ? (g.WhiteElo || "") : (g.BlackElo || "");
    $("analysisMeta").textContent = `${state.review.analysis.engine} � depth ${state.review.analysis.depth} � ${state.review.analysis.seconds || "?"}s`;
    renderAccuracySummary();
    $("btnPgn").disabled = false; $("btnCopyPgn").disabled = false;
  }

  function applyReview(res, opts) {
    opts = opts || {};
    state.review = res;
    state.customFen = null;
    state.ply = 0;
    // stable id for the library (players+date+length), kept across re-reviews
    if (!res.game.id) {
      res.game.id = `${res.game.White || "W"}-${res.game.Black || "B"}-${res.game.Date || ""}-${res.moves.length}`.replace(/[^\w.-]+/g, "_");
    }
    libAdd(res);
    exitExplore(false);
    buildMoveList();
    graph.setData(res.graph, res.moves);
    renderMoments();
    renderTimeReport();
    renderSummary();
    renderGameHead();
    switchTab("report");
    gotoPly(1);
    if (res.pgnWarnings && res.pgnWarnings.length && !(opts && opts.silent)) {
      // parse drop-outs / multi-game exports: loud, once, per fresh review
      toast(res.pgnWarnings.join("  "), 7000);
    }
    if (opts.silent) {
      toast("Previous session restored", 3000);
    } else {
      saveSession(res);
      syncTrainerFromReview(res);   // fresh review → new trainer exercises (deduped)
      perfSyncReviewed(res);        // current game → Performance Lab (best effort)
      toast(`Review complete � White ${res.players.white.accuracy}% vs Black ${res.players.black.accuracy}%`, 4000);
    }
  }

  /* session auto-save: the loaded review + position survive a refresh */
  function saveSession(res) {
    try {
      const s = JSON.stringify(res);
      if (s.length < 2500000) localStorage.setItem("crs_last_review", s);
    } catch (e) { /* quota - skip */ }
  }
  function saveSessionPos() {
    try { localStorage.setItem("crs_last_pos", JSON.stringify({ ply: state.ply })); } catch (e) { /* skip */ }
  }
  /* explored line (moves played on top of a review position / custom position) �
     persisted so a page refresh puts you back exactly where you were exploring */
  function saveExplore() {
    try {
      if (state.explore) {
        localStorage.setItem("crs_last_explore", JSON.stringify({
          fromPly: state.explore.fromPly,
          baseFen: state.explore.baseFen,
          moves: state.explore.moves,
          lastEval: state.explore.lastEval,
        }));
      } else {
        localStorage.removeItem("crs_last_explore");
      }
    } catch (e) { /* quota - skip */ }
  }
  async function restoreSession() {
    // saved explored line - must be read BEFORE applyReview(): its exitExplore()
    // would clear it from localStorage (same ordering trap as the saved ply)
    let ex = null;
    try { ex = JSON.parse(localStorage.getItem("crs_last_explore") || "null"); } catch (err) { ex = null; }
    const replayExplore = () => {
      if (!ex || !Array.isArray(ex.moves)) return false;
      if (state.review) {
        if (typeof ex.fromPly !== "number" || ex.fromPly < 0 || ex.fromPly > state.review.moves.length) return false;
      } else if (!state.customFen) {
        // bare analysis board: the line can only be restored from the start position
        if (ex.baseFen !== START_FEN) return false;
        ex.fromPly = 0;
      }
      const from = Math.max(0, Math.min(ex.fromPly | 0, state.review ? state.review.moves.length : 0));
      const baseFen = state.customFen ? state.customFen
        : (state.review ? state.review.positions[from].fen : START_FEN);
      if (!baseFen || ex.baseFen !== baseFen) return false;
      gotoPly(from, true);   // keepExplore: reuses graph/highlight/pill plumbing
      enterExplore();
      state.explore.fromPly = from;
      state.explore.moves = ex.moves.filter((m) => m && m.uci && m.fen);
      state.explore.lastEval = ex.lastEval || null;
      state.explore.lines = null; state.explore.linesFen = null;  // live loop refills these within seconds
      saveExplore();
      renderBoard();
      updateExploreInfo();
      return true;
    };
    try {
      const saved = localStorage.getItem("crs_last_review");
      if (saved) {
        const res = JSON.parse(saved);
        if (res && res.moves && res.positions) {
          // read the saved ply BEFORE applyReview(): its gotoPly(1) -> saveSessionPos()
          // would otherwise overwrite crs_last_pos with ply 1 and destroy the position
          let pos = null;
          try { pos = JSON.parse(localStorage.getItem("crs_last_pos") || "{}"); } catch (err) { pos = null; }
          applyReview(res, { silent: true });
          const explored = replayExplore();
          if (!explored && pos && pos.ply) gotoPly(pos.ply);
          // guarantee the board renders even if the async render chain hiccups
          // (keep last-move highlight + classification badge from the restored state)
          const fen = currentFen();
          if (fen) {
            let lastUci2 = null, badge2 = null;
            if (state.explore && state.explore.moves.length) {
              const entry = state.explore.moves[state.explore.moves.length - 1];
              lastUci2 = entry.uci;
              if (entry.cls && entry.cls !== "good" && entry.cls !== "forced") badge2 = { sq: entry.uci.slice(2, 4), cls: entry.cls };
            } else if (state.review && state.ply > 0) {
              const m = state.review.moves[state.ply - 1];
              lastUci2 = m.uci;
              if (m.class !== "good" && m.class !== "forced") badge2 = { sq: m.uci.slice(2, 4), cls: m.class };
            }
            board.setPosition(fen, { legal: [], badge: badge2, arrows: [], lastMove: lastUci2, checkSq: null });
            legalFor(fen).then((l) => {
              if (currentFen() === fen) { board.legal = l.moves; board.renderDecor(); }
            }).catch(() => {});
          }
          return true;
        }
      }
      const custom = localStorage.getItem("crs_last_custom");
      if (custom) {
        state.customFen = custom;
        renderAccuracySummary();
        if (!replayExplore()) enterExplore();
        renderBoard();   // paint the pieces (the live path gets this via toggleEditor; without it the board is blank)
        toast("Custom position restored", 3000);
        return true;
      }
      if (replayExplore()) return true; // bare analysis line from the start position
    } catch (e) { /* corrupt session - start fresh */ }
    return false;
  }

  /* ---------------- tabs ---------------- */
  function switchTab(name) {
    state.activeTab = name;
    document.querySelectorAll(".review-col > .tabs .tab").forEach((t) => {
      const on = t.dataset.tab === name;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on);
    });
    document.querySelectorAll(".review-col .tab-body").forEach((b) => { b.hidden = b.id !== "tab-" + name; });
    if (name === "moves") setTimeout(() => graph.resize(), 0);
    if (name === "explorer") renderExplorer();
    if (name === "puzzle" && !state.puzzle) pzNew();
    if (name === "trainer") refreshTrainer();
  }

  /* ---------------- opening explorer ---------------- */
  const explorerCache = new Map();
  let explorerTimer = null;
  let explorerReqFen = null;

  function scheduleExplorer() {
    if (state.activeTab !== "explorer") return;
    clearTimeout(explorerTimer);
    explorerTimer = setTimeout(renderExplorer, 350);
  }

  async function renderExplorer() {
    const el = $("explorerList");
    const fen = editor.active ? board.fen : currentFen();
    if (!fen) { el.innerHTML = `<div class="empty-hint small">No position loaded.</div>`; return; }
    if (explorerCache.has(fen)) { paintExplorer(fen, explorerCache.get(fen)); return; }
    el.innerHTML = `<div class="empty-hint small">Looking up master games...</div>`;
    $("explorerOpening").textContent = "�";
    $("explorerMeta").textContent = "";
    explorerReqFen = fen;
    clearTimeout(explorerTimer);
    explorerTimer = setTimeout(async () => {
      try {
        const res = await api("/api/explorer?fen=" + encodeURIComponent(fen));
        explorerCache.set(fen, res);
        if (explorerReqFen === fen) paintExplorer(fen, res);
      } catch (e) {
        if (explorerReqFen === fen) {
          el.innerHTML = /token/i.test(e.message)
            ? `<div class="empty-hint small">Lichess now requires a (free) API token for the explorer.<br>
               Open <b>? settings</b> and paste a personal token from<br>lichess.org/account/oauth/token</div>`
            : `<div class="empty-hint small">Explorer unavailable (${e.message}).</div>`;
        }
      }
    }, 250);
  }

  function paintExplorer(fen, res) {
    const op = res.opening;
    $("explorerOpening").textContent = op && op.name ? `${op.eco ? op.eco + " � " : ""}${op.name}` : "Rarely played in master games";    $("explorerMeta").textContent = res.total ? `${res.total.toLocaleString()} master games reached this position` : "";
    const sanMap = new Map(board.legal.map((m) => [m.uci.slice(0, 4), m.san]));
    $("explorerList").innerHTML = (res.moves || []).map((m) => {
      const san = sanMap.get(m.uci.slice(0, 4)) || m.san || m.uci;
      let pw = 50, pd = 50, pb = 0;
      if (m.white != null && m.games) {
        pw = (m.white / m.games) * 100;
        pd = (m.draws / m.games) * 100;
        pb = (m.black / m.games) * 100;
      } else if (m.share != null) {
        pw = Math.min(100, m.share * 100);
        pd = 0; pb = 0;
      }
      return `<div class="exp-row" data-uci="${m.uci}">
        <span class="exp-san">${san}</span>
        <span class="exp-pct">${((m.share || 0) * 100).toFixed(1)}%</span>
        <div class="wdl"><span class="w" style="width:${pw.toFixed(1)}%"></span><span class="d" style="width:${pd.toFixed(1)}%"></span><span class="b" style="width:${pb.toFixed(1)}%"></span></div>
        <span class="exp-games">${(m.games || 0).toLocaleString()}</span>
      </div>`;
    }).join("") || `<div class="empty-hint small">No recorded continuations.</div>`;
    document.querySelectorAll("#explorerList .exp-row").forEach((row) => {
      row.onclick = () => {
        const uci = row.dataset.uci;
        const match = board.legal.find((m) => m.uci.startsWith(uci.slice(0, 4)));
        if (match) onMoveAttempt(match, uci.slice(0, 2), uci.slice(2, 4));
      };
    });
  }

  /* ---------------- review flow ---------------- */
  const stageText = (s) => ({
    starting: "Starting engine�",
    book: "Checking opening theory (Lichess explorer)�",
    engine: "Analyzing positions�",
    classify: "Classifying moves & computing accuracy�",
    done: "Done",
  })[s] || s;

  async function pollJob(jobId) {
    /* progress UI loop, shared by startReview and refresh-reattachment;
       resolves with the full review result. */
    let lastStage = "";
    for (;;) {
      await new Promise((r) => setTimeout(r, 700));
      const st = await api(`/api/review/status/${jobId}`);
      const stageStr = st.stageDetail || st.stage || "";
      let m = /^(engine|book) (\d+)\/(\d+)/.exec(stageStr);
      let note = "";
      if (m) {
        const pct = +m[2] / +m[3];
        $("progressFill").style.width = (m[1] === "book" ? pct * 15 : 15 + pct * 80) + "%";
        note = `${m[2]} of ${m[3]} ${m[1] === "book" ? "book moves checked" : "positions analyzed"}`;
      } else if (st.stage === "classify") {
        $("progressFill").style.width = "96%"; note = "computing accuracy�";
      } else if (st.stage === "starting") {
        $("progressFill").style.width = "5%"; note = "engine warming up�";
      }
      if (st.stage !== lastStage) { $("progressStage").textContent = stageText(st.stage); lastStage = st.stage; }
      $("progressNote").textContent = note;
      if (st.status === "error") throw new Error(st.error || "analysis failed");
      if (st.status === "done") return api(`/api/review/result/${jobId}`);
    }
  }

  async function startReview(pgn) {
    const engine = $("optEngine").value || "stockfish";
    const preset = DEPTHS[$("optDepth").value] || DEPTHS.balanced;
    state.arrowsOn = $("optArrows").checked;
    if (state.quiz) quizQuit();
    if (state.repTrain) repTrainQuit();
    state.liveSuspended = true; // don't compete with the review for the engine
    $("modalError").hidden = true;
    $("btnStartReview").disabled = true;
    $("progressPane").hidden = false;
    $("serverStatus").classList.add("busy");
    try {
      const { jobId } = await post("/api/review", {
        pgn, engine, depth: preset.depth, movetime: preset.movetime, multipv: 3,
      });
      // remembered so a refresh mid-review can reattach to the same job
      try { localStorage.setItem("crs_job", jobId); } catch (e) { /* skip */ }
      const res = await pollJob(jobId);
      $("progressFill").style.width = "100%";
      $("progressStage").textContent = "Loading review�";
      state._pgnMoves = res.moves.length;
      applyReview(res);
      hideModal();
      try { if (localStorage.getItem("crs_job") === jobId) localStorage.removeItem("crs_job"); } catch (e) { /* skip */ }
    } catch (e) {
      $("modalError").textContent = e.message || "Review failed";
      $("modalError").hidden = false;
      try { localStorage.removeItem("crs_job"); } catch (e2) { /* skip */ }
    } finally {
      $("btnStartReview").disabled = false;
      $("serverStatus").classList.remove("busy");
      state.liveSuspended = false;
      state.live.fen = null; // force a fresh live eval on the new position
    }
  }

  /* resume a review that was running when the page was last refreshed: replay
     the progress UI and apply the finished result (server keeps the job alive) */
  async function maybeReattachJob() {
    let jobId = null;
    try { jobId = localStorage.getItem("crs_job"); } catch (e) { /* skip */ }
    if (!jobId) return false;
    let st = null;
    try { st = await api(`/api/review/status/${jobId}`); } catch (e) { st = null; }
    if (!st || st.error || st.status === "error") {
      try { localStorage.removeItem("crs_job"); } catch (e) { /* skip */ }
      return false;
    }
    state.liveSuspended = true;
    if (st.status === "done") {
      try {
        if (localStorage.getItem("crs_job") === jobId) {
          const res = await api(`/api/review/result/${jobId}`);
          state._pgnMoves = res.moves.length;
          applyReview(res);
          toast("Review finished", 3000);
        }
      } catch (e) { /* keep whatever session was restored */ }
      try { if (localStorage.getItem("crs_job") === jobId) localStorage.removeItem("crs_job"); } catch (e) { /* skip */ }
      state.liveSuspended = false;
      state.live.fen = null;
      return true;
    }
    // still running: restore whatever was on screen beneath the progress, keep polling
    showModal();
    $("progressPane").hidden = false;
    $("progressStage").textContent = "Resuming review�";
    try {
      const res = await pollJob(jobId);
      if (localStorage.getItem("crs_job") === jobId) {
        state._pgnMoves = res.moves.length;
        applyReview(res);
        hideModal();
      }
    } catch (e) { hideModal(); }
    try { if (localStorage.getItem("crs_job") === jobId) localStorage.removeItem("crs_job"); } catch (e) { /* skip */ }
    state.liveSuspended = false;
    state.live.fen = null;
    return true;
  }

  /* ---------------- modals ---------------- */
  function showModal() { $("modalNew").hidden = false; $("progressPane").hidden = true; $("progressFill").style.width = "0%"; }
  function hideModal() { $("modalNew").hidden = true; }

  function countPgnMoves(pgn) { return (pgn.match(/\d+\./g) || []).length; }

  async function loadSamples() {
    try {
      const res = await api("/api/samples");
      const el = $("sampleList");
      if (!res.samples.length) { el.innerHTML = `<p class="text-muted">No samples found.</p>`; return; }
      el.innerHTML = "";
      for (const s of res.samples) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "game-pick";
        b.innerHTML = `<div class="t">${s.name}</div>`;
        b.onclick = async () => {
          const r = await api("/api/samples/" + encodeURIComponent(s.id));
          $("pgnText").value = r.pgn;
          el.querySelectorAll(".game-pick").forEach((x) => x.classList.remove("picked"));
          b.classList.add("picked");
        };
        el.appendChild(b);
      }
    } catch (e) { $("sampleList").innerHTML = `<p class="text-muted">Could not list samples.</p>`; }
  }

  async function fetchLichess() {
    const user = $("lichessUser").value.trim();
    if (!user) return;
    const el = $("lichessGames");
    el.innerHTML = `<p class="text-muted">Fetching�</p>`;
    try {
      const res = await post("/api/lichess/fetch", { username: user, max: 6 });
      const games = res.pgn.split("\n\n[Event ").map((g, i) => i === 0 ? g : "[Event " + g)
        .filter((g) => g.includes("1.") || g.includes("1 ..."));
      if (!games.length) { el.innerHTML = `<p class="text-muted">No games found for ${user}.</p>`; return; }
      el.innerHTML = "";
      games.slice(0, 6).forEach((g, i) => {
        const white = (/^\[White "(.*)"\]/m.exec(g) || [])[1] || "?";
        const black = (/^\[Black "(.*)"\]/m.exec(g) || [])[1] || "?";
        const result = (/^\[Result "(.*)"\]/m.exec(g) || [])[1] || "";
        const date = (/^\[UTCDate "(.*)"\]/m.exec(g) || [])[1] || (/^\[Date "(.*)"\]/m.exec(g) || [])[1] || "";
        const b = document.createElement("button");
        b.type = "button";
        b.className = "game-pick";
        b.innerHTML = `<div class="t">${white} vs ${black} ${result}</div><div class="s">${date}</div>`;
        b.onclick = () => {
          $("pgnText").value = g.trim();
          el.querySelectorAll(".game-pick").forEach((x) => x.classList.remove("picked"));
          b.classList.add("picked");
        };
        el.appendChild(b);
      });
    } catch (e) {
      el.innerHTML = `<p class="text-muted">${e.message}</p>`;
    }
  }

  async function fetchChessCom() {
    const user = $("chesscomUser").value.trim();
    if (!user) { toast("Enter a chess.com username first"); return; }
    const el = $("chesscomGames");
    el.innerHTML = `<p class="text-muted">Fetching recent games�</p>`;
    try {
      const res = await post("/api/chesscom/fetch", {
        username: user, max: 8, timeClass: $("chesscomSpeed").value,
      });
      el.innerHTML = "";
      res.games.forEach((g) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "game-pick";
        b.innerHTML = `<div class="t">${g.white} vs ${g.black} ${g.result}</div>
          <div class="s">${g.timeClass} � ${g.date} � ${g.whiteRating ?? "?"} vs ${g.blackRating ?? "?"}</div>`;
        b.onclick = () => {
          $("pgnText").value = g.pgn;
          el.querySelectorAll(".game-pick").forEach((x) => x.classList.remove("picked"));
          b.classList.add("picked");
        };
        el.appendChild(b);
      });
    } catch (e) {
      el.innerHTML = `<p class="text-muted">${e.message}</p>`;
    }
  }

  /* ---------------- export ---------------- */
  function downloadPgn() {
    if (!state.review) return;
    const blob = new Blob([state.review.annotatedPgn], { type: "application/x-chess-pgn" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    const g = state.review.game;
    a.download = `${(g.White || "white")}-vs-${(g.Black || "black")}-review.pgn`.replace(/[^\w.-]+/g, "_");
    a.click();
    URL.revokeObjectURL(a.href);
  }
  async function copyPgn() {
    if (!state.review) return;
    try { await navigator.clipboard.writeText(state.review.annotatedPgn); toast("PGN copied to clipboard"); }
    catch { toast("Copy failed � use Download instead"); }
  }

  /* ---------------- engines ---------------- */
  async function loadEngines() {
    try {
      const res = await api("/api/engines");
      state.engines = res.engines;
      const opts = res.engines.map((e) => `<option value="${e.id}" title="${e.name}">${e.name}</option>`).join("");
      $("engineSelect").innerHTML = opts;
      $("optEngine").innerHTML = opts;
      const sf = res.engines.find((e) => e.id === "stockfish");
      if (sf) { $("engineSelect").value = "stockfish"; $("optEngine").value = "stockfish"; }
      $("engineInfo").innerHTML = res.engines.map((e) =>
        `<div><b>${e.name}</b>${e.author ? " � " + e.author : ""} ${e.kind === "builtin" ? "(built-in)" : ""}${(e.warnings || []).length ? ' <span class="text-muted">? limited build</span>' : ""}</div>`
      ).join("");
    } catch (e) { toast("Could not load engines: " + e.message); }
  }

  /* ---------------- board editor ---------------- */
  const editor = { active: false, position: {}, turn: "w", castling: { K: false, Q: false, k: false, q: false }, selectedPiece: null };
  const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const TRAY = ["wK", "wQ", "wR", "wB", "wN", "wP", "bK", "bQ", "bR", "bB", "bN", "bP"];
  const TRASH_SVG = '<svg viewBox="0 0 24 24" style="width:22px;height:22px"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

  function buildTray() {
    const tray = $("edTray");
    tray.innerHTML = "";
    for (const code of TRAY) {
      const b = document.createElement("button");
      b.className = "ed-piece";
      b.type = "button";
      b.dataset.code = code;
      b.title = code;
      b.innerHTML = `<img src="${pieceDir}${code}.svg" alt="">`;
      b.onclick = () => selectTray(code);
      tray.appendChild(b);
    }
    const trash = document.createElement("button");
    trash.className = "ed-piece ed-trash";
    trash.type = "button";
    trash.title = "Eraser - click squares to delete pieces";
    trash.innerHTML = TRASH_SVG;
    trash.onclick = () => selectTray("trash");
    tray.appendChild(trash);
  }

  function selectTray(code) {
    editor.selectedPiece = editor.selectedPiece === code ? null : code;
    document.querySelectorAll(".ed-piece").forEach((b) => {
      b.classList.toggle("picked", b.dataset.code === editor.selectedPiece ||
        (editor.selectedPiece === "trash" && b.classList.contains("ed-trash")));
    });
  }

  function editorToFen() {
    const rows = [];
    for (let r = 8; r >= 1; r--) {
      let row = "", empty = 0;
      for (const f of "abcdefgh") {
        const code = editor.position[f + r];
        if (!code) { empty++; continue; }
        if (empty) { row += empty; empty = 0; }
        row += code[0] === "w" ? code[1] : code[1].toLowerCase();
      }
      if (empty) row += empty;
      rows.push(row);
    }
    const p = editor.position;
    const castling = (
      (editor.castling.K && p.e1 === "wK" && p.h1 === "wR" ? "K" : "") +
      (editor.castling.Q && p.e1 === "wK" && p.a1 === "wR" ? "Q" : "") +
      (editor.castling.k && p.e8 === "bK" && p.h8 === "bR" ? "k" : "") +
      (editor.castling.q && p.e8 === "bK" && p.a8 === "bR" ? "q" : "")
    ) || "-";
    return rows.join("/") + ` ${editor.turn} ${castling} - 0 1`;
  }

  function syncEdCastling() {
    $("edCK").checked = editor.castling.K;
    $("edCQ").checked = editor.castling.Q;
    $("edCk").checked = editor.castling.k;
    $("edCq").checked = editor.castling.q;
  }

  function editorSync() {
    const fen = editorToFen();
    $("edFen").value = fen;
    syncEdCastling();
    $("edTurn").textContent = editor.turn === "w" ? "White to move" : "Black to move";
    board.lastMove = null;
    board.checkSq = null;
    board.setPosition(fen, { legal: [], badge: null, arrows: [], lastMove: null, checkSq: null });
  }

  function editorLoadFen(fen) {
    const map = board.pieceFromFen(fen);
    editor.position = map;
    const parts = fen.trim().split(/\s+/);
    editor.turn = parts[1] === "b" ? "b" : "w";
    const c = parts[2] || "-";
    editor.castling = { K: c.includes("K"), Q: c.includes("Q"), k: c.includes("k"), q: c.includes("q") };
    editor.selectedPiece = null;
    document.querySelectorAll(".ed-piece").forEach((b) => b.classList.remove("picked"));
    editorSync();
  }

  function onEditorSquare(sq, code) {
    if (editor.selectedPiece === "trash") {
      delete editor.position[sq];
      editorSync();
      return;
    }
    if (editor.selectedPiece) {
      editor.position[sq] = editor.selectedPiece;
      // sensible castling defaults when kings/rooks land on their start squares
      if (sq === "e1" && editor.selectedPiece === "wK") { editor.castling.K = true; editor.castling.Q = true; }
      if (sq === "e8" && editor.selectedPiece === "bK") { editor.castling.k = true; editor.castling.q = true; }
      if (sq === "h1" && editor.selectedPiece === "wR") editor.castling.K = true;
      if (sq === "a1" && editor.selectedPiece === "wR") editor.castling.Q = true;
      if (sq === "h8" && editor.selectedPiece === "bR") editor.castling.k = true;
      if (sq === "a8" && editor.selectedPiece === "bR") editor.castling.q = true;
      editorSync();
      return;
    }
    if (code) { // pick the piece up from the board
      editor.selectedPiece = code;
      delete editor.position[sq];
      editorSync();
      document.querySelectorAll(".ed-piece").forEach((b) => b.classList.toggle("picked", b.dataset.code === code));
    }
  }

  function editorDelete(e) {
    e.preventDefault();
    const sqEl = e.target.closest(".sq");
    if (sqEl && editor.position[sqEl.dataset.sq]) {
      delete editor.position[sqEl.dataset.sq];
      editorSync();
    }
  }

  function toggleEditor(on) {
    editor.active = on;
    killLinePlay();
    $("editorPanel").hidden = !on;
    $("btnEditor").classList.toggle("live-on", on);
    board.setEditorMode(on);
    if (on) {
      editorLoadFen(currentFen() || START_FEN);
      board.el.addEventListener("contextmenu", editorDelete);
      switchTab("report");
    } else {
      board.el.removeEventListener("contextmenu", editorDelete);
      renderBoard();
    }
  }

  async function editorAnalyze() {
    const raw = $("edFen").value.trim();
    const rows = raw.split(" ")[0].split("/");
    if (rows.length !== 8) { toast("Invalid position: board must have 8 rows"); return; }
    let fen;
    try {
      editorLoadFen(raw); // accept hand-edited FENs from the box too
      fen = editorToFen();
    } catch (e) {
      toast("Could not parse FEN: " + e.message);
      return;
    }
    const flat = rows.join("");
    const wK = (flat.match(/K/g) || []).length, bK = (flat.match(/k/g) || []).length;
    if (wK !== 1 || bK !== 1) { toast("Position needs exactly one king per side"); return; }
    try {
      await api("/api/legal?fen=" + encodeURIComponent(fen));
    } catch (e) {
      toast("Invalid position: " + e.message);
      return;
    }
    state.review = null;
    state.customFen = fen;
    state.ply = 0;
    try {
      localStorage.removeItem("crs_last_review");
      localStorage.setItem("crs_last_custom", fen);
    } catch (e) { /* skip */ }
    exitExplore(false);
    toggleEditor(false);
    buildMoveList();
    graph.setData([], []);
    renderAccuracySummary();
    $("momentsHead").hidden = true;
    $("timeHead").hidden = true;
    $("momentsList").innerHTML = "";
    $("timeReport").innerHTML = "";
    $("summaryBody").innerHTML = "";
    enterExplore();
    toast("Custom position loaded - live analysis running");
  }

  function renderCustomMoves() {
    const el = $("moveList");
    const moves = state.explore ? state.explore.moves : [];
    if (!moves.length) {
      el.innerHTML = `<div class="empty-hint small">Custom position - play moves on the board to build a line.</div>`;
      return;
    }
    const rows = [];
    for (let i = 0; i < moves.length; i += 2) {
      rows.push(`<div class="mrow"><span class="mnum">${i / 2 + 1}.</span>
        <span class="mv"><span class="san">${moves[i] ? moves[i].san : ""}</span></span>
        <span class="mv"><span class="san">${moves[i + 1] ? moves[i + 1].san : ""}</span></span></div>`);
    }
    el.innerHTML = rows.join("");
  }

  /* ---------------- Mistake Trainer (v63) ---------------- */
  const trainerUI = { queue: [], idx: 0, results: [], cardAt: 0, revealFrom: null };

  const trainerEsc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const trainerEval = (cp) => {
    if (cp == null || Number.isNaN(cp)) return "?";
    const v = cp / 100;
    return (v >= 0 ? "+" : "") + v.toFixed(2);
  };
  const trainerFmtDue = (e) => {
    if (e.status === "new") return ["New", "tr-badge-new"];
    if (e.status === "mastered") return ["Mastered", "tr-badge-ok"];
    const now = Date.now() / 1000;
    if (e.dueAt <= now) return ["Due now", "tr-badge-due"];
    return ["in " + Math.ceil((e.dueAt - now) / 86400) + "d", "tr-badge-due"];
  };

  function trainerFilters() {
    const out = {};
    const cls = $("trFilterCls").value; if (cls) out.cls = cls;
    const side = $("trFilterSide").value; if (side) out.side = side;
    const st = $("trFilterStatus").value; if (st) out.status = st;
    const src = $("trFilterSource").value; if (src) out.source = src;
    return out;
  }
  function trainerQs(f, extra) {
    const p = new URLSearchParams(f);
    if (extra) Object.keys(extra).forEach((k) => p.set(k, extra[k]));
    const s = p.toString();
    return s ? "?" + s : "";
  }
  function trainerShowView(v) {
    $("trainerDash").hidden = v !== "dash";
    $("trainerSession").hidden = v !== "session";
    $("trainerDone").hidden = v !== "done";
  }

  async function syncTrainerFromReview(res) {
    try {
      const r = await post("/api/trainer/extract", { review: res });
      if (r && r.created > 0) toast(`+${r.created} training exercise${r.created > 1 ? "s" : ""} saved`, 3500);
    } catch (e) { /* trainer is best effort - never break a review */ }
  }
  async function trainerSyncAuto() {
    // one-shot migration for games already in the local library (idempotent)
    let added = 0;
    try {
      for (const g of libLoad()) {
        try {
          const r = await post("/api/trainer/extract", { review: g.review });
          if (r && r.created) added += r.created;
        } catch (e) { /* skip that game */ }
      }
    } catch (e) { /* best effort */ }
    if (added > 0) toast(`Trainer synced: +${added} new exercise${added > 1 ? "s" : ""} from your saved games`, 4000);
    return added;
  }

  function paintTrainerStats(d) {
    const cell = (v, label, cls) =>
      `<div class="tr-stat ${cls || ""}"><b>${v}</b><span>${label}</span></div>`;
    $("trHeroStats").innerHTML =
      cell(d.due ?? 0, "due now", "hot") +
      cell(d.new ?? 0, "new", "ok") +
      cell(d.total ?? 0, "total", "") +
      cell(d.mastered ?? 0, "mastered", "ok") +
      cell(d.accuracy != null ? d.accuracy + "%" : "—", "accuracy", "") +
      cell(d.recentAccuracy != null ? d.recentAccuracy + "%" : "—", "last 50", "") +
      cell(`${d.streakDays || 0}d`, "streak", (d.streakDays || 0) > 0 ? "hot" : "") +
      cell(`${Math.round((d.studyTimeMs || 0) / 60000)}m`, "studied", "");
    // weakest category, only when reliable
    const w = (d.weakCategories || [])[0];
    const names = { blunder: "blunders", miss: "missed wins", mistake: "mistakes" };
    if (w) {
      $("trWeakCard").hidden = false;
      $("trWeakCard").innerHTML = `Weakest area so far: <b>${names[w.cls] || w.cls}</b> — ${w.misses} missed ${w.misses === 1 ? "attempt" : "attempts"} logged.`;
    } else {
      $("trWeakCard").hidden = true;
    }
  }
  function paintTrainerList(list) {
    const el = $("trList");
    if (!list.length) {
      $("trListMeta").textContent = "0";
      el.innerHTML = `<div class="empty-hint small">No exercises match. Sync a fresh review or loosen the filters.</div>`;
      return;
    }
    $("trListMeta").textContent = list.length + (list.length >= 50 ? "+" : "") + " shown";
    el.innerHTML = list.map((e) => {
      const [tagTxt, tagCls] = trainerFmtDue(e);
      const mv = `${e.moveNumber || "?"}${e.colorSide === "black" ? "..." : "."} ${e.bestSan}`;
      return `<div class="tr-row"><span class="tr-chip ${e.cls}">${trainerEsc(e.cls)}</span>
        <span><b class="mv">${trainerEsc(mv)}</b>
          <div class="meta">${trainerEsc(e.sourceName)} · ${trainerEsc(e.opening || "opening")} · missed ${trainerEsc(e.playedSan)}</div></span>
        <span class="${tagCls}">${tagTxt}</span></div>`;
    }).join("");
  }
  function paintTrainerHistory(list) {
    const el = $("trHistory");
    if (!list.length) {
      el.innerHTML = `<div class="empty-hint small">No attempts yet — start your first session!</div>`;
      return;
    }
    const now = new Date(); const y1 = new Date(); y1.setDate(y1.getDate() - 1);
    el.innerHTML = list.map((h) => {
      const d = new Date((h.attemptedAt || 0) * 1000);
      const when = d.toDateString() === now.toDateString() ? "today"
        : d.toDateString() === y1.toDateString() ? "yesterday"
        : Math.floor((Date.now() - d) / 86400000) + "d ago";
      return `<div class="tr-hist"><span class="dot ${h.solved ? "ok" : "bad"}"></span>
        <span>${trainerEsc(h.label || h.cls)} · played <b>${trainerEsc(h.playedSan || "?")}</b> / best <b>${trainerEsc(h.bestSan)}</b></span>
        <span class="when">${when}</span></div>`;
    }).join("");
  }
  function populateTrainerSources(list) {
    const sel = $("trFilterSource");
    const prev = sel.value;
    sel.innerHTML = `<option value="">All games</option>` +
      list.map((s) => `<option value="${trainerEsc(s.id)}">${trainerEsc(s.name)} · ${s.count}</option>`).join("");
    if (prev && list.some((s) => s.id === prev)) sel.value = prev;
  }
  function refreshTrainer() {
    if (state.trainerEx) { trainerShowView("session"); return Promise.resolve(); }
    return (async () => {
      try {
        const f = trainerFilters();
        const d = await api("/api/trainer/dashboard" + trainerQs(f));
        paintTrainerStats(d);
        const l = await api("/api/trainer/list" + trainerQs(f, { limit: 50 }));
        paintTrainerList((l && l.exercises) || []);
        const h = await api("/api/trainer/history?limit=8");
        paintTrainerHistory((h && h.history) || []);
        const s = await api("/api/trainer/sources");
        populateTrainerSources((s && s.sources) || []);
      } catch (e) {
        $("trHeroStats").innerHTML = `<div class="empty-hint small" style="grid-column:1/-1">Trainer unavailable — is the server awake?</div>`;
        $("trList").innerHTML = ""; $("trHistory").innerHTML = "";
      }
    })();
  }

  async function startTrainerSession() {
    if (state.quiz) quizQuit();
    if (state.repTrain) repTrainQuit();
    if (state.linePlay) stopLinePlay();
    if (state.puzzle) puzzleQuit();
    if (state.explore) exitExplore(false);
    if (state.trainerEx) trainerQuit();
    stopAutoplay();
    if (editor.active) toggleEditor(false);
    if (state.playBot) stopPlayBot();
    const f = trainerFilters();
    let queue = [];
    try {
      const r = await api("/api/trainer/queue" + trainerQs(f, { limit: 10 }));
      queue = (r && r.queue) || [];
    } catch (e) { /* handled below */ }
    if (!queue.length) {
      toast("Nothing due — you're caught up! Sync a fresh review, or loosen the filters.", 4500);
      return;
    }
    trainerUI.queue = queue; trainerUI.idx = 0; trainerUI.results = [];
    loadTrainerCard(0);
    trainerShowView("session");
  }
  function loadTrainerCard(i) {
    trainerUI.idx = i;
    trainerUI.revealFrom = null;
    const ex = trainerUI.queue[i];
    state.trainerEx = JSON.parse(JSON.stringify(ex));
    state.trainerEx.attempt = null;
    trainerUI.cardAt = Date.now();
    if (state.orientation !== ex.turn) { board.flip(); state.orientation = board.orientation; }
    const n = trainerUI.queue.length;
    $("trSessCount").textContent = (i + 1) + " / " + n;
    $("trProgressFill").style.width = (100 * i) / n + "%";
    $("trTurn").innerHTML = `<b>${ex.turn === "w" ? "White" : "Black"}</b> to move — find the best move!`;
    $("trMeta").innerHTML =
      `<span class="tag">${trainerEsc(ex.label || ex.cls)}</span>` +
      (ex.opening ? `<span class="tag">${trainerEsc(ex.opening)}</span>` : "") +
      `<span>from <b>${trainerEsc(ex.sourceName)}</b></span><span>move ${ex.moveNumber || "?"}</span>`;
    $("trFeedback").hidden = true;
    $("trRevealBtn").disabled = false;
    trainerRenderBoard();
  }
  async function trainerRenderBoard() {
    const ex = state.trainerEx;
    if (!ex) return;
    const legal = await legalFor(ex.fen).catch(() => ({ moves: [] }));
    if (state.trainerEx !== ex) return;
    board.setPosition(ex.fen, {
      legal: legal.moves, arrows: [], lastMove: "",
      badge: trainerUI.revealFrom ? { sq: trainerUI.revealFrom, cls: "hint" } : null,
      checkSq: legal && legal.check ? kingSquare(ex.fen) : null,
    });
  }
  async function trainerGuess(chosen) {
    const ex = state.trainerEx;
    if (!ex || ex.attempt) return;
    const mv = await post("/api/move", { fen: ex.fen, uci: chosen.uci });
    if (!mv) { toast("Illegal move"); return; }
    playMoveSound(mv.san);
    const lines = ex.lines || [];
    const best = lines[0] || { uci: ex.bestUci, san: ex.bestSan, cp: null };
    const match = lines.find((l) => l.uci === chosen.uci) || null;
    let verdict, quality, gapCp = 0;
    if (chosen.uci === ex.bestUci) {
      verdict = "best"; quality = 5;
    } else if (match) {
      if (best.mate != null && match.mate != null) {
        const d = Math.abs(Math.abs(best.mate) - Math.abs(match.mate));
        verdict = d <= 1 ? "good-enough" : "slightly-off";
        quality = d <= 1 ? 4 : 2;
      } else {
        gapCp = Math.max(0, (best.cp || 0) - (match.cp || 0));
        verdict = gapCp <= 20 ? "good-enough" : gapCp <= 80 ? "slightly-off" : "bad";
        quality = verdict === "good-enough" ? 4 : verdict === "slightly-off" ? 2 : 0;
      }
    } else { verdict = "bad"; quality = 0; }
    ex.attempt = { playedUci: chosen.uci, san: mv.san, verdict, quality, gapCp };
    const badgeCls = verdict === "best" ? "best" : verdict === "good-enough" ? "excellent"
      : verdict === "slightly-off" ? "inaccuracy" : "blunder";
    board.setPosition(ex.fen, {
      legal: [], lastMove: chosen.uci,
      arrows: verdict === "best" ? []
        : [{ uci: ex.bestUci, color: "rgba(255,168,46,.92)", width: 0.22, opacity: 0.95 }],
      badge: { sq: chosen.uci.slice(2, 4), cls: badgeCls },
      checkSq: null,
    });
    paintTrainerVerdict(ex, verdict, gapCp);
  }
  function paintTrainerVerdict(ex, verdict, gapCp) {
    const v = $("trVerdict"), b = $("trFeedbackBody");
    const head = { best: "✓ Exactly right!", "good-enough": "That works too!", "slightly-off": "Playable — but…", bad: "That's not it" };
    const cls = { best: "good", "good-enough": "mixed", "slightly-off": "mixed", bad: "bad" };
    v.textContent = head[verdict] || head.bad;
    v.className = "tr-verdict " + (cls[verdict] || "bad");
    let body = "";
    if (verdict === "best") {
      body = `<b>${trainerEsc(ex.attempt.san)}</b> is the engine's top choice — engine and you agree.`;
    } else if (verdict === "good-enough") {
      body = `<b>${trainerEsc(ex.attempt.san)}</b> keeps things level — the engine's top choice is <b>${trainerEsc(ex.bestSan)}</b>${gapCp ? ` (≈ ${gapCp}cp better)` : ""}.`;
    } else if (verdict === "slightly-off") {
      body = `<b>${trainerEsc(ex.attempt.san)}</b> is playable — but <b>${trainerEsc(ex.bestSan)}</b> is clearly stronger (≈ ${gapCp}cp).`;
    } else {
      body = `<b>${trainerEsc(ex.attempt.san)}</b> isn't good — <b>${trainerEsc(ex.bestSan)}</b> was needed here.`;
    }
    body += `<div class="swing" style="margin-top:4px">eval ${trainerEval(ex.cpBefore)} → ${trainerEval(ex.cpAfter)} · win ${ex.winBefore ?? "?"}% → ${ex.winAfter ?? "?"}%</div>`;
    if (verdict !== "best" && ex.pvSan && ex.pvSan.length) {
      body += `<div class="swing" style="margin-top:4px">Engine line: ${ex.pvSan.map(trainerEsc).join(" ")}</div>`;
    }
    b.innerHTML = body;
    ["trAgain", "trHard", "trGood", "trEasy"].forEach((id) => $(id).classList.remove("suggested"));
    const map = { 0: "trAgain", 2: "trHard", 4: "trGood", 5: "trEasy" };
    const pick = (ex.attempt.quality in map) ? map[ex.attempt.quality] : "trGood";
    $(pick).classList.add("suggested");
    ["trAgain", "trHard", "trGood", "trEasy"].forEach((id) => { $(id).disabled = false; });
    $("trFeedback").hidden = false;
    $("trHintBtn").hidden = false;
    $("trRevealBtn").disabled = true;
  }
  async function trainerGrade(quality) {
    const ex = state.trainerEx;
    if (!ex || !ex.attempt) return;
    ["trAgain", "trHard", "trGood", "trEasy"].forEach((id) => { $(id).disabled = true; });
    const ms = Date.now() - (trainerUI.cardAt || Date.now());
    try {
      const r = await post("/api/trainer/result", {
        exerciseId: ex.id, quality, playedUci: ex.attempt.playedUci, ms,
      });
      const days = r && (r.intervalDays != null) ? Math.round(r.intervalDays) : 1;
      toast(`Saved · next review in ~${days}d`, 2400);
      trainerUI.results.push({ cls: ex.cls, quality });
    } catch (e) {
      toast("Couldn't save that attempt — check the server", 3000);
    }
    const n = trainerUI.idx + 1;
    if (n >= trainerUI.queue.length) finishTrainerSession();
    else loadTrainerCard(n);
  }
  function advanceTrainerCard() {
    if (!state.trainerEx) return;
    const n = trainerUI.idx + 1;
    if (n >= trainerUI.queue.length) finishTrainerSession();
    else loadTrainerCard(n);
  }
  function finishTrainerSession() {
    state.trainerEx = null;
    const solved = trainerUI.results.filter((r) => r.quality > 0).length;
    const total = trainerUI.results.length;
    $("trDoneStats").innerHTML =
      `<div class="tr-done-stat"><b>${solved}</b><span>solved</span></div>
       <div class="tr-done-stat"><b>${total - solved}</b><span>missed</span></div>
       <div class="tr-done-stat"><b>${total ? Math.round(100 * solved / total) : 0}%</b><span>accuracy</span></div>`;
    trainerShowView("done");
  }
  function trainerQuit() {
    state.trainerEx = null; trainerUI.queue = []; trainerUI.idx = 0; trainerUI.results = [];
    trainerShowView("dash");
    renderBoard();
    refreshTrainer();
  }
  function trainerHint() {
    const ex = state.trainerEx;
    if (!ex || ex.attempt) return;
    trainerUI.revealFrom = ex.bestUci.slice(0, 2);
    trainerRenderBoard();
    toast("Hint: look at the piece on " + trainerUI.revealFrom, 3000);
  }
  function trainerReveal() {
    const ex = state.trainerEx;
    if (!ex) return;
    trainerUI.revealFrom = ex.bestUci.slice(0, 2);
    board.setPosition(ex.fen, {
      legal: [], lastMove: "",
      arrows: [{ uci: ex.bestUci, color: "rgba(255,168,46,.95)", width: 0.24, opacity: 0.97 }],
      badge: { sq: ex.bestUci.slice(0, 2), cls: "hint" },
      checkSq: null,
    });
    toast("Solution: " + ex.bestSan, 3500);
  }

  /* ---------------- Performance Lab (v65) — navy diagnostics ---------------- */
  const labEscape = typeof trainerEsc === "function" ? trainerEsc : ((s) =>
    String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"));

  const LAB_GLYPHS = { total: "&#9822;", winrate: "&#x2655;", accuracy: "&#9814;", blunders: "&#9873;", reviewed: "&#9822;", missed: "&#8626;" };

  function paintLabHero(d) {
    const avgAcc = (d.avgAccuracy && d.avgAccuracy.white != null && d.avgAccuracy.black != null)
      ? Math.round((d.avgAccuracy.white + d.avgAccuracy.black) / 2) : null;
    const wr = d.decidedGames ? d.winRate : null;
    const wrCls = wr == null ? "accent" : wr >= 55 ? "ok" : wr < 40 ? "hot" : "warn";
    $("labHero").innerHTML =
      `<div class="lab-hero-item"><span class="lab-hero-glyph">${LAB_GLYPHS.total}</span>`
      + `<div class="lab-hero-num accent">${d.total}</div><div class="lab-hero-label">Games imported</div>`
      + `<div class="lab-hero-sub">${d.reviewedGames || 0} reviewed</div></div>`
      + `<div class="lab-hero-item"><span class="lab-hero-glyph">${LAB_GLYPHS.winrate}</span>`
      + `<div class="lab-hero-num ${wrCls}">${wr == null ? "—" : wr + "%"}</div><div class="lab-hero-label">Win rate</div>`
      + `<div class="lab-hero-sub">${d.decidedGames || 0} decided games</div></div>`
      + `<div class="lab-hero-item"><span class="lab-hero-glyph">${LAB_GLYPHS.accuracy}</span>`
      + `<div class="lab-hero-num ok">${avgAcc != null ? avgAcc + "%" : "—"}</div><div class="lab-hero-label">Avg accuracy</div>`
      + `<div class="lab-hero-sub">ACPL ${d.avgACPL && d.avgACPL.white != null ? Math.round((d.avgACPL.white + d.avgACPL.black) / 2) : "—"}</div></div>`
      + `<div class="lab-hero-item"><span class="lab-hero-glyph">${LAB_GLYPHS.blunders}</span>`
      + `<div class="lab-hero-num ${(d.errorTotals.blunders || 0) > 0 ? "hot" : "ok"}">${d.errorTotals.blunders || 0}</div>`
      + `<div class="lab-hero-label">Blunders</div>`
      + `<div class="lab-hero-sub">${d.errorTotals.misses || 0} missed wins</div></div>`;
  }

  function paintLabPhaseBars(d) {
    const el = $("labPhaseBars");
    const meta = $("labPhaseMeta");
    const ps = d.phaseStats || {};
    const n = ps.n || 0;
    meta.textContent = n === 0 ? "no reviewed games yet" : `${n} reviewed game${n === 1 ? "" : "s"}`;
    if (!n) {
      el.innerHTML = `<div class="lab-empty">Review some games to unlock phase analysis.</div>`;
      return;
    }
    const row = (label, val, fillCls) => {
      const missing = val == null;
      const v = missing ? 0 : Math.round(val);
      return `<div class="lab-phase-row"><span class="lab-phase-name">${label}</span>`
        + `<span class="lab-phase-track"><span class="lab-phase-fill ${fillCls}" style="width:${v}%"></span></span>`
        + `<span class="lab-phase-val">${missing ? "—" : v}</span></div>`;
    };
    // navy-reference gradient lanes: green → amber → red (open → end)
    el.innerHTML = row("Opening", ps.opening, "")
      + row("Middlegame", ps.middlegame, "mid")
      + row("Endgame", ps.endgame, "late");
  }

  function paintLabWeaknesses(w) {
    const el = $("labWeaknessCards");
    const meta = $("labWeaknessMeta");
    const total = w.weaknesses.length;
    const shown = Math.min(4, total);
    meta.textContent = shown
      ? `${w.reviewedGames || 0} reviewed · top ${shown} of ${total} pattern${total === 1 ? "" : "s"}`
      : "no weaknesses found";
    if (!w.weaknesses.length) {
      el.innerHTML = `<div class="lab-empty">${labEscape(w.note || "Nothing to see yet — review more games or import them to surface patterns here.")}</div>`;
      return;
    }
    el.innerHTML = w.weaknesses.slice(0, 4).map((f) => `
      <div class="lab-weak">
        <span class="lab-weak-stripe sev-${f.severity === 'high' ? 'high' : f.severity === 'medium' ? 'medium' : 'low'}"></span>
        <div>
          <div class="lab-weak-title">${labEscape(f.label)}</div>
          <div class="lab-weak-desc">${labEscape(f.description)}</div>
        </div>
        <div class="lab-weak-right">
          <span class="lab-weak-count sev-${f.severity === 'high' ? 'high' : f.severity === 'medium' ? 'medium' : 'low'}">${f.count}</span>
          <span class="lab-weak-conf">${f.confidence} conf.</span>
        </div>
      </div>`).join("");
  }

  function paintLabOpenings(o) {
    const el = $("labOpeningRows");
    const fams = o.families || [];
    if (!fams.length) { el.innerHTML = `<div class="lab-empty">No games classified — import a PGN to populate this.</div>`; return; }
    el.innerHTML = fams.slice(0, 10).map((f) => {
      const decided = f.wins + f.losses + f.draws;
      const wr = decided ? Math.round(100 * f.wins / decided) : null;
      const wrCls = wr == null ? "" : wr >= 60 ? "ok" : wr >= 40 ? "mid" : "bad";
      const confNote = f.confidence === "high" ? "" : ` · small sample (N=${f.games})`;
      const wPct = f.games ? Math.round(100 * f.wins / f.games) : 0;
      const dPct = f.games ? Math.round(100 * f.draws / f.games) : 0;
      const lPct = f.games ? Math.max(0, 100 - wPct - dPct) : 0;
      return `<div class="lab-op">
        <span class="lab-op-eco">${labEscape(f.eco || "?")}</span>
        <span>
          <div class="lab-op-name">${labEscape(f.opening)}</div>
          <div class="lab-op-meta">${f.games} games · ${f.wins}W ${f.losses}L ${f.draws}D${confNote}</div>
          <div class="lab-wd"><span class="lab-wd-w" style="width:${wPct}%"></span><span class="lab-wd-d" style="width:${dPct}%"></span><span class="lab-wd-l" style="width:${lPct}%"></span></div>
        </span>
        <span class="lab-op-wr ${wrCls}">${wr == null ? "—" : wr + "%"}<small>WR</small></span>
      </div>`;
    }).join("");
  }

  /* ---- aimchess-style behavior layer (v69): radar + chips + daily drills ---- */
  let labColor = null;
  function getLabColor() {
    if (labColor) return labColor;
    try { labColor = localStorage.getItem("crs_lab_color") === "black" ? "black" : "white"; }
    catch (e) { labColor = "white"; }
    return labColor;
  }
  function setLabColor(c) {
    labColor = c;
    try { localStorage.setItem("crs_lab_color", c); } catch (e) { /* private mode */ }
    document.querySelectorAll("#labColorToggle .lab-seg").forEach((b) => {
      b.classList.toggle("active", b.dataset.color === c);
    });
    loadLabMetrics();
  }
  const mcls = (s) => (s == null ? "" : s >= 70 ? "ok" : s >= 40 ? "mid" : "bad");

  function paintLabRadar(m) {
    const svg = $("labRadar");
    if (!m || !m.radar || !m.radar.length) { svg.innerHTML = ""; return; }
    const cx = 150, cy = 124, R = 86;
    const ang = (i, n) => (Math.PI * 2 * i) / n - Math.PI / 2;
    const pt = (i, n, r) => [cx + r * Math.cos(ang(i, n)), cy + r * Math.sin(ang(i, n))];
    const n = m.radar.length;
    const rings = [0.25, 0.5, 0.75, 1].map((f) =>
      `<polygon class="lab-radar-grid" points="${
        m.radar.map((_, i) => pt(i, n, R * f).map((v) => v.toFixed(1)).join(",")).join(" ")}"/>`).join("");
    const spokes = m.radar.map((_, i) => {
      const [x2, y2] = pt(i, n, R);
      return `<line class="lab-radar-spoke" x1="${cx}" y1="${cy}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>`;
    }).join("");
    let opt = "";
    let dots = "", labels = "";
    m.radar.forEach((axis, i) => {
      const r = axis.v == null ? 0 : R * (Math.max(5, axis.v) / 100);
      const [x, y] = pt(i, n, r);
      opt += (i ? "" : "") + `${x.toFixed(1)},${y.toFixed(1)} `;
      dots += `<circle class="lab-radar-dot ${axis.v == null ? "dim" : ""}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3"/>`;
      const [lx, ly] = pt(i, n, R + 17);
      const anchor = Math.abs(lx - cx) < 8 ? "middle" : lx > cx ? "start" : "end";
      const dy = ly < cy ? -2 : 8;
      labels += `<text class="lab-radar-label" x="${lx.toFixed(1)}" y="${(ly + dy).toFixed(1)}" text-anchor="${anchor}">${axis.axis}</text>`
        + `<text class="lab-radar-val" x="${lx.toFixed(1)}" y="${(ly + dy + 11).toFixed(1)}" text-anchor="${anchor}">${axis.v == null ? "—" : axis.v}</text>`;
    });
    svg.innerHTML = `<g>${rings}${spokes}<polygon class="lab-radar-shape" points="${opt.trim()}"/></g>${dots}${labels}`;
  }

  function paintLabMetricChips(m) {
    const el = $("labMetricChips");
    if (!m) { el.innerHTML = ""; return; }
    const chip = (score, name, sub, extra) => `
      <div class="lab-mchip">
        <span class="lab-mchip-num ${mcls(score)}">${score == null ? "—" : score}</span>
        <span><div class="lab-mchip-name">${name}</div><div class="lab-mchip-sub">${sub}</div>
          <div class="lab-mchip-track"><div class="lab-mchip-fill ${mcls(score)}" style="width:${score == null ? 0 : score}%"></div></div></span>
        <span class="lab-mchip-val">${extra || ""}</span>
      </div>`;
    const dash = (a) => (a == null ? "—" : a);
    el.innerHTML =
      chip(m.speed, "Speed · impulsivity",
        m.tpm == null ? "needs clocked games" : `${m.tpm}s / move average`,
        m.tpm == null ? "—" : `${m.speed === null ? "" : m.speed >= 70 ? "quick play" : m.speed < 40 ? "slow play" : "balanced"}`) +
      chip(m.aggression, "Aggression",
        `${m.captureCount || 0} captures · ${m.checkCount || 0} checks`,
        m.attackShare == null ? "—" : `${m.attackShare}%`) +
      chip(m.timeMgmt, "Time management",
        m.tpm == null ? "needs clocked games" : `${m.tpm}s / move`,
        m.tpm == null ? "—" : m.timeMgmt >= 70 ? "healthy tempo" : m.timeMgmt < 40 ? "rushed or stuck" : "steady") +
      chip(m.blunderless, "Error sanity",
        `${m.blunders || 0} blunders · ${m.mistakes || 0} mistakes`,
        m.blunderRate == null ? "—" : `${m.blunderRate}/100 moves`) +
      chip(m.consistency, "Consistency",
        m.accuracyStdDev == null ? "needs 2+ reviewed games" : `±${m.accuracyStdDev} accuracy swing`,
        "") +
      chip(dash(m.conversion), "Conversion",
        `${m.wins || 0}W ${m.losses || 0}L ${m.draws || 0}D`,
        m.conversion == null ? "—" : `${m.conversion}%`);
  }

  function paintLabRadarNote(m) {
    const el = $("labRadarNote");
    if (!m) { el.textContent = ""; return; }
    if (!m.movesCount) {
      el.textContent = "Behavior metrics start once reviewed games reach the Lab.";
      return;
    }
    const axes = (m.radar || []).filter((a) => a.v != null).sort((a, b) => b.v - a.v);
    const top = axes[0], low = axes[axes.length - 1];
    let txt = `${m.movesCount} own moves analyzed across ${m.gamesCount} reviewed game(s).`;
    if (top && low && top.axis !== low.axis) txt += ` Strongest axis: ${top.axis} (${top.v}). Weakest: ${low.axis} (${low.v}).`;
    if (m.fastAccuracy != null && m.avgAccuracy != null) {
      txt += ` Fast moves (=<3s): ${m.fastAccuracy}% vs ${m.avgAccuracy}% overall.`;
    }
    el.textContent = txt;
  }

  async function loadLabMetrics() {
    try {
      const m = await api("/api/perf/metrics?color=" + getLabColor());
      paintLabRadar(m); paintLabMetricChips(m); paintLabRadarNote(m);
    } catch (e) {
      $("labRadar").innerHTML = "";
      $("labMetricChips").innerHTML = `<div class="lab-empty">Behavior metrics unavailable — server still waking up.</div>`;
      $("labRadarNote").textContent = "";
    }
  }

  /* ---- daily workout: three drills picked from your weakest patterns ---- */
  function labDayKey() {
    try { return new Date().toISOString().slice(0, 10); } catch (e) { return "day"; }
  }
  function labPlanLoad() {
    try {
      const raw = JSON.parse(localStorage.getItem("crs_lab_plan") || "{}");
      if (raw.day !== labDayKey()) return { day: labDayKey(), done: [], streak: raw.streak || 0, lastFull: raw.lastFull || "" };
      return { day: raw.day, done: raw.done || [], streak: raw.streak || 0, lastFull: raw.lastFull || "" };
    } catch (e) { return { day: labDayKey(), done: [], streak: 0, lastFull: "" }; }
  }
  function labPlanSave(p) {
    try { localStorage.setItem("crs_lab_plan", JSON.stringify(p)); } catch (e) { /* full */ }
  }
  function paintLabDaily(recs) {
    const el = $("labDailyList");
    const meta = $("labDailyMeta");
    const fill = $("labDayFill");
    const note = $("labDailyNote");
    const tasks = (recs || []).slice(0, 3);
    if (!tasks.length) {
      el.innerHTML = `<div class="lab-empty">No drills yet — review more games and your plan appears here.</div>`;
      meta.textContent = ""; fill.style.width = "0%"; note.textContent = "";
      return;
    }
    const p = labPlanLoad();
    const doneCount = tasks.filter((_, i) => p.done.includes(i)).length;
    meta.textContent = `${doneCount}/${tasks.length} done · ${p.streak} day streak`;
    el.innerHTML = tasks.map((rec, i) => `
      <div class="lab-day ${p.done.includes(i) ? "lit" : ""}" data-i="${i}" title="Click to mark done">
        <span class="lab-dayCheck">&#10003;</span>
        <span><div class="lab-day-title">${labEscape(rec.title)}</div>
          <div class="lab-day-body">${labEscape(rec.exerciseFocus)}</div></span>
        <span class="lab-day-streak">&#9832; ${rec.priority}</span>
      </div>`).join("");
    el.querySelectorAll(".lab-day").forEach((row) => {
      row.onclick = () => {
        const pp = labPlanLoad();
        const i = +row.dataset.i;
        pp.done = pp.done.includes(i) ? pp.done.filter((x) => x !== i) : pp.done.concat([i]);
        const allDone = tasks.every((_, x) => pp.done.includes(x));
        if (allDone && pp.lastFull !== pp.day) {
          const yest = (function () {
            const d = new Date(); d.setDate(d.getDate() - 1);
            try { return d.toISOString().slice(0, 10); } catch (e) { return ""; }
          })();
          pp.streak = pp.lastFull === yest ? pp.streak + 1 : 1;
          pp.lastFull = pp.day;
        }
        labPlanSave(pp);
        paintLabDaily(recs);
      };
    });
    fill.style.width = `${Math.round(100 * doneCount / tasks.length)}%`;
    note.textContent = "Drills come from your top recommendations and rotate as your data changes.";
  }

  function paintLabRecommendations(r) {
    const el = $("labRecList");
    const recs = r.recommendations || [];
    if (!recs.length) { el.innerHTML = `<div class="lab-empty">Not enough data yet — review more games.</div>`; return; }
    el.innerHTML = recs.map((rec, i) => `
      <div class="lab-rec prio-${rec.priority}">
        <span class="lab-rec-num">${i + 1}</span>
        <span>
          <div class="lab-rec-title">${labEscape(rec.title)}<span class="lab-rec-category">${rec.category}</span></div>
          <div class="lab-rec-body">${labEscape(rec.exerciseFocus)}</div>
          <div class="lab-rec-why">${labEscape(rec.why)}</div>
        </span>
      </div>`).join("");
  }

  function paintLabGames(list, total) {
    const el = $("labGameList");
    const tot = Number(total) || 0;
    $("labGameMeta").textContent = tot ? `${list.length} of ${tot} shown` : "0 games";
    if (!list.length) {
      el.innerHTML = `<div class="lab-empty">No games imported yet. Use "Import file" or "Paste PGN" above — duplicates are skipped automatically.</div>`;
      return;
    }
    el.innerHTML = list.slice(0, 20).map((g) => {
      const cls = g.result === "1-0" ? "win" : g.result === "0-1" ? "loss" : "draw";
      const label = g.result === "1-0" ? "1-0" : g.result === "0-1" ? "0-1" : "½-½";
      return `<div class="lab-game">
        <span class="lab-game-res ${cls}">${label}</span>
        <span><div class="lab-game-name">${labEscape(g.white)} vs ${labEscape(g.black)}</div>
          <div class="lab-game-meta">${labEscape(g.opening)} · ${labEscape(g.date || "?")} · ${labEscape(g.tcBucket)}</div></span>
        <button class="lab-game-del" data-id="${g.id}" type="button" aria-label="Delete">&#10005;</button>
      </div>`;
    }).join("");
    el.querySelectorAll(".lab-game-del").forEach((b) => {
      b.onclick = async () => {
        await post("/api/perf/games/delete", { id: b.dataset.id });
        refreshLab();
      };
    });
  }

  async function refreshLab() {
    try {
      await perfSyncLibrary();   // v66: Library is the source of truth - re-attach missing reviewed games first
      const [d, w, o, r, g, m] = await Promise.all([
        api("/api/perf/overall"), api("/api/perf/weaknesses"),
        api("/api/perf/openings"), api("/api/perf/recommendations"),
        api("/api/perf/games?limit=30"), api("/api/perf/metrics?color=" + getLabColor()),
      ]);
      paintLabHero(d); paintLabPhaseBars(d); paintLabWeaknesses(w); paintLabOpenings(o);
      paintLabRecommendations(r); paintLabDaily(r.recommendations); paintLabGames(g.games || [], g.total);
      paintLabRadar(m); paintLabMetricChips(m); paintLabRadarNote(m);
    } catch (e) {
      $("labHero").innerHTML = `<div class="lab-hero-item" style="grid-column:1/-1"><div class="lab-hero-label">Lab unavailable — is the server awake?</div></div>`;
      $("labPhaseBars").innerHTML = ""; $("labWeaknessCards").innerHTML = "";
      $("labOpeningRows").innerHTML = ""; $("labRecList").innerHTML = ""; $("labGameList").innerHTML = "";
      $("labRadar").innerHTML = ""; $("labMetricChips").innerHTML = "";
      $("labRadarNote").textContent = ""; $("labDailyList").innerHTML = "";
    }
  }
  async function perfSyncReviewed(res) {
    try { await post("/api/perf/import_review", { review: res }); } catch (e) { /* best effort */ }
  }

  /* v66: the Lab always uses every already-reviewed game the browser holds (library is
     source of truth). Container wipes erase the server DB, so instead of a one-shot flag
     we re-attach the whole library whenever the server shows fewer reviewed games than
     the library holds. attach_review upserts by id / position+players, so re-sending
     known reviews is idempotent and cheap. */
  async function perfSyncLibrary() {
    const lib = libLoad();
    if (!lib.length) return 0;
    let d = null;
    try { d = await api("/api/perf/overall"); } catch (e) { return 0; }
    if (((d && d.reviewedGames) || 0) >= lib.length) return 0;
    await Promise.all(lib.filter((g) => g && g.review).map((g) => perfSyncReviewed(g.review)));
    return lib.length;
  }
  async function perfImportBulk(text) {
    try {
      const r = await post("/api/perf/import_pgn", { pgn: text });
      if (r && r.ok) {
        toast(`Imported ${r.inserted} new game(s) · ${r.duplicates} duplicate(s) skipped`, 4500);
        refreshLab();
      } else if (r && r.error) {
        showLabImportError(r.error);
      }
    } catch (e) {
      showLabImportError("Import failed — check the PGN and try again.");
    }
  }
  function showLabImportError(msg) {
    const el = $("labImportError");
    el.textContent = msg;
    el.hidden = false;
  }
  $("labImportPgnBtn").onclick = () => { $("labImportModal").hidden = false; $("labImportText").focus(); };
  $("labImportClose").onclick = () => { $("labImportModal").hidden = true; $("labImportError").hidden = true; };
  $("labImportCancel").onclick = $("labImportClose").onclick;
  $("labImportGo").onclick = () => {
    const text = $("labImportText").value.trim();
    if (!text) { showLabImportError("Paste a PGN first."); return; }
    $("labImportModal").hidden = true;
    $("labImportError").hidden = true;
    perfImportBulk(text);
    $("labImportText").value = "";
  };
  $("labImportBtn").onclick = () => $("labImportFile").click();
  $("labImportFile").onchange = async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const text = await file.text();
    await perfImportBulk(text);
    e.target.value = "";
  };
  $("labRefreshBtn").onclick = refreshLab;
  document.querySelectorAll("#labColorToggle .lab-seg").forEach((b) => {
    b.classList.toggle("active", b.dataset.color === getLabColor());
    b.onclick = () => setLabColor(b.dataset.color);
  });

  /* Lab is a full sidebar page (v66): btnLab opens it; Board button / Esc / btnLab closes it */
  let labOpen = false;
  function showLab(open) {
    labOpen = !!open;
    $("viewLab").hidden = !labOpen;
    const ma = document.querySelector(".main-area");
    if (ma) ma.classList.toggle("lab-open", labOpen);
    $("btnLab").classList.toggle("lab-on", labOpen);
    $("btnLab").title = labOpen
      ? "Back to the board"
      : "Performance Lab - performance analytics across all your games";
    if (labOpen) {
      $("viewLab").scrollTop = 0;
      refreshLab();
    }
  }
  $("btnLab").onclick = () => showLab(!labOpen);
  $("labBackBtn").onclick = () => showLab(false);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && labOpen && !document.querySelector(".modal-backdrop:not([hidden])")) {
      showLab(false);
    }
  });

  /* ---------------- Lichess puzzles (v60) ---------------- */
  let pzStats = { solved: 0, streak: 0, best: 0, rating: 1200 };
  try {
    const raw = localStorage.getItem("crs_puzzle_stats");
    if (raw) pzStats = Object.assign(pzStats, JSON.parse(raw));
  } catch (e) { /* private mode */ }
  function pzSaveStats() {
    try { localStorage.setItem("crs_puzzle_stats", JSON.stringify(pzStats)); } catch (e) { /* full */ }
  }
  const pzExpected = (my, their) => 1 / (1 + Math.pow(10, (their - my) / 400));

  function paintPuzzleStats() {
    $("pzStats").innerHTML =
      `Solved <b>${pzStats.solved}</b> &middot; Streak <b class="${pzStats.streak > 0 ? "on" : ""}">${pzStats.streak}</b> &middot; Best <b>${pzStats.best}</b> &middot; Your rating <b>${Math.round(pzStats.rating)}</b>`;
  }

  function puzzleRenderBoard() {
    const pz = state.puzzle;
    if (!pz) return;
    legalFor(pz.fen).then((l) => {
      if (state.puzzle !== pz) return;               // user jumped to another puzzle meanwhile
      board.setPosition(pz.fen, {
        legal: l.moves, arrows: [], lastMove: pz.lastMove || "",
        badge: pz._revealFrom ? { sq: pz._revealFrom, cls: "hint" } : null,
        checkSq: l.check ? kingSquare(pz.fen) : null,
      });
    }).catch(() => {
      board.setPosition(pz.fen, { legal: [], arrows: [], lastMove: pz.lastMove || "" });
    });
  }

  function paintPzPanel() {
    const pz = state.puzzle;
    if (!pz) { paintPuzzleStats(); return; }
    $("pzRating").textContent = "Rating " + (pz.data.rating || "?");
    $("pzThemes").innerHTML = (pz.data.themes || []).slice(0, 3)
      .map((t) => `<span class="pz-theme-chip">${String(t).replace(/[<>&]/g, "")}</span>`).join("");
    const link = $("pzLink");
    link.hidden = false;
    link.href = pz.data.url || ("https://lichess.org/training/" + pz.data.id);
    link.textContent = "lichess #" + pz.data.id;
    const side = pz.fen.split(" ")[1] === "b" ? "Black" : "White";
    $("pzTurn").innerHTML = pz.finished
      ? (pz.solved ? "Solved!"
                     : "Failed � press Retry to take another run at it.")
      : `Find the best move for <b>${side}</b>${pz.wrong ? " � wrong move #" + pz.wrong : ""}`;
    paintPuzzleStats();
  }

  function puzzleQuit() {
    if (!state.puzzle) return;
    state.puzzle = null;
    state.liveSuspended = false;
    $("pzSolved").hidden = true;
    paintPuzzleStats();
  }

  async function pzNew() {
    // leave every other board-mode cleanly; the puzzle owns the board now
    if (state.quiz) quizQuit();
    if (state.repTrain) repTrainQuit();
    if (state.linePlay) stopLinePlay();
    stopAutoplay();
    if (editor.active) toggleEditor(false);
    state.hintUci = null;
    const theme = $("pzTheme").value;
    try { localStorage.setItem("crs_puzzle_theme", theme); } catch (e) { /* private mode */ }
    const btn = $("pzNew");
    btn.disabled = true;
    const label = btn.innerHTML;
    btn.textContent = "Loading�";
    try {
      let data = await api("/api/puzzle?theme=" + encodeURIComponent(theme));
      // lichess can occasionally serve the same puzzle twice - one retry for variety
      if (state.puzzle && data && data.id === state.puzzle.data.id) {
        try {
          const again = await api("/api/puzzle?theme=" + encodeURIComponent(theme));
          if (again && again.id !== data.id) data = again;
        } catch (e) { /* keep the repeat: it was a fair fetch */ }
      }
      state.puzzle = {
        data, fen: data.fen, ply: 0, wrong: 0, finished: false, solved: false,
        lastMove: data.lastMove || "", _revealFrom: null,
      };
      state.liveSuspended = true;             // no spoilers from live analysis
      const solver = data.fen.split(" ")[1] === "b" ? "black" : "white";
      if (state.orientation !== solver) { board.flip(); state.orientation = board.orientation; }
      $("pzSolved").hidden = true;
      puzzleRenderBoard();
      paintPzPanel();
    } catch (e) {
      state.puzzle = null;
      $("pzTurn").innerHTML = "No puzzle loaded � lichess.org unreachable?";
      toast("Could not fetch a puzzle");
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
    }
  }

  async function puzzleGuess(chosen) {
    const pz = state.puzzle;
    if (!pz || pz.finished) return;
    if (chosen.uci === pz.data.solution[pz.ply]) {
      const mv = await post("/api/move", { fen: pz.fen, uci: chosen.uci });
      pz.fen = mv.fen; pz.ply++; pz.lastMove = chosen.uci;
      playMoveSound(mv.san);
      if (pz.ply < pz.data.solution.length) {   // forced opponent reply
        await sleep(200);
        const opp = pz.data.solution[pz.ply];
        try {
          const om = await post("/api/move", { fen: pz.fen, uci: opp });
          pz.fen = om.fen; pz.ply++; pz.lastMove = opp;
          playMoveSound(om.san);
        } catch (e) { /* opponent reply failed - just stop */ }
      }
      if (pz.ply >= pz.data.solution.length) {
        pz.finished = true; pz.solved = true;
        const gain = Math.max(6, Math.round(24 * (1 - pzExpected(pzStats.rating, pz.data.rating || 1500))));
        pzStats.rating = pzStats.rating + gain;
        pzStats.solved++; pzStats.streak++; pzStats.best = Math.max(pzStats.best, pzStats.streak);
        pzSaveStats();
        $("pzSolved").hidden = false;
        $("pzSolvedText").textContent = "Solved! +" + gain + " rating � streak " + pzStats.streak;
        toast("Solved!", 2600);
      }
      puzzleRenderBoard();
      paintPzPanel();
      return;
    }
    // wrong move: single strike fails the puzzle, Lichess-style
    pz.wrong++;
    pzStats.streak = 0;
    board.setPosition(pz.fen, { arrows: [], badge: { sq: chosen.uci.slice(2, 4), cls: "blunder" } });
    pz.finished = true; pz.fail = true;
    const loss = Math.max(6, Math.round(24 * pzExpected(pzStats.rating, pz.data.rating || 1500)));
    pzStats.rating = Math.max(400, pzStats.rating - loss);
    pzSaveStats();
    pz._revealFrom = pz.data.solution[pz.ply].slice(0, 2);
    puzzleRenderBoard();
    $("pzSolved").hidden = false;
    $("pzSolvedText").textContent = "Failed � -" + loss + " rating � streak reset";
    toast("That's not it � Retry to try again");
    paintPzPanel();
  }

  function pzRetry() {
    const pz = state.puzzle;
    if (!pz) return;
    state.puzzle = {
      data: pz.data, fen: pz.data.fen, ply: 0, wrong: 0, finished: false, solved: false,
      lastMove: pz.data.lastMove || "", _revealFrom: null,
    };
    $("pzSolved").hidden = true;
    puzzleRenderBoard();
    paintPzPanel();
  }

  function pzHint() {
    const pz = state.puzzle;
    if (!pz || pz.finished) { toast("No hint needed here"); return; }
    pz._revealFrom = pz.data.solution[pz.ply].slice(0, 2);
    puzzleRenderBoard();
    toast("Hint: move the highlighted piece", 2500);
  }

  /* ---------------- best-move trainer (find the best move) ---------------- */
  const TRAIN_GRADES = [
    { max: 30, cls: "best", label: "Best", pts: 10 },
    { max: 90, cls: "excellent", label: "Excellent", pts: 8 },
    { max: 180, cls: "good", label: "Good", pts: 5 },
    { max: 350, cls: "inaccuracy", label: "Inaccuracy", pts: 3 },
    { max: 700, cls: "mistake", label: "Mistake", pts: 1 },
    { max: Infinity, cls: "blunder", label: "Blunder", pts: 0 },
  ];
  const gradeForLoss = (loss) => TRAIN_GRADES.find((g) => loss <= g.max);

  function quizCur() {
    const q = state.quiz;
    const idx = q.plies[q.k];
    return { idx, mv: state.review.moves[idx], pos: state.review.positions[idx] };
  }

  function quizStart(side) {
    if (!state.review) { toast("Run a review first"); return; }
    if (editor.active) toggleEditor(false);
    if (state.repTrain) repTrainQuit();
    killLinePlay();
    let plies = state.review.moves
      .map((m, i) => ({ i, m }))
      .filter((x) => x.m.color === side && x.m.class !== "book");
    if (!plies.length) {
      plies = state.review.moves.map((m, i) => ({ i, m })).filter((x) => x.m.color === side);
    }
    if (!plies.length) { toast("No positions for that side"); return; }
    state.quiz = {
      side, plies: plies.map((x) => x.i), k: 0, score: 0, max: 0,
      results: [], phase: "question", last: null,
    };
    state.liveSuspended = true; // no spoilers from live analysis
    quizRender();
    quizRenderBoard();
    toast(`Trainer started - find the best moves for ${side}. No peeking at the Engine tab.`);
  }

  /* mistake drill: a training playlist of ONLY the positions where the reviewed
     game went wrong (inaccuracy / mistake / missed win / blunder), played as the
     side that erred. Guess what you should have played instead; graded by eval
     loss vs the engine's best, exactly like the full trainer. */
  const DRILL_CLASSES = ["inaccuracy", "mistake", "miss", "blunder"];
  function drillStart(side) {
    if (!state.review) { toast("Run a review first"); return; }
    killLinePlay();
    const plies = state.review.moves
      .map((m, i) => ({ i, m }))
      .filter((x) => x.m.color === side && DRILL_CLASSES.includes(x.m.class));
    if (!plies.length) {
      toast(`${side === "white" ? "White" : "Black"} played clean - no mistakes to fix!`);
      return;
    }
    if (editor.active) toggleEditor(false);
    if (state.repTrain) repTrainQuit();
    state.quiz = {
      side, plies: plies.map((x) => x.i), k: 0, score: 0, max: 0,
      results: [], phase: "question", last: null, drill: true, drillTotal: plies.length,
    };
    state.liveSuspended = true; // no spoilers from live analysis
    quizRender();
    quizRenderBoard();
    toast(`Mistake drill - fix all ${plies.length} of ${side === "white" ? "White's" : "Black's"} errors`);
  }

  async function quizRenderBoard() {
    const q = state.quiz;
    if (!q) return;
    const { idx, mv } = quizCur();
    const fen = state.review.positions[idx].fen;
    const legal = await legalFor(fen);
    // a revealed hint stays on the board even across async re-renders
    const badge = q.hintUsed && mv.best && mv.best.uci
      ? { sq: mv.best.uci.slice(0, 2), cls: "hint" } : null;
    board.setPosition(fen, { legal: legal.moves, badge, arrows: [], lastMove: null, checkSq: null });
  }

  function quizQuit() {
    if (!state.quiz) return;
    state.quiz = null;
    state.liveSuspended = false;
    $("quizPanel").hidden = true;
    renderBoard();
  }

  function quizNext() {
    const q = state.quiz;
    if (!q) return;
    q.k++;
    q.hintUsed = false;                 // hint restores its value for the next question
    if (q.k >= q.plies.length) { q.phase = "done"; }
    else { q.phase = "question"; quizRenderBoard(); }
    quizRender();
  }

  function quizSkip() {
    const q = state.quiz;
    if (!q || q.phase !== "question") return;
    const { mv } = quizCur();
    q.max += q.hintUsed ? 5 : 10;
    q.results.push({ idx: q.plies[q.k], grade: "forced", label: "Skipped", pts: 0, guessSan: null, hint: !!q.hintUsed });
    q.last = { skipped: true, bestSan: mv.best.san, gameSan: mv.san, gameLabel: mv.label, grade: null, hint: !!q.hintUsed };
    q.phase = "answered";
    quizRender();
  }

  /* hint: highlight the square the best move starts from; halves that question's value */
  function quizHint() {
    const q = state.quiz;
    if (!q || q.phase !== "question") return;
    if (q.hintUsed) return;
    const { mv } = quizCur();
    if (!mv.best || !mv.best.uci) { toast("No hint available for this position"); return; }
    q.hintUsed = true;
    const fen = state.review.positions[q.plies[q.k]].fen;
    board.setPosition(fen, { badge: { sq: mv.best.uci.slice(0, 2), cls: "hint" } });
    quizRender();
    toast("Hint: the highlighted piece makes the best move - question now worth 5 points max");
  }

  async function quizGuess(chosen) {
    const q = state.quiz;
    if (!q || q.phase !== "question") return;
    q.phase = "grading";
    const { idx, mv, pos } = quizCur();
    const bestUci = mv.best && mv.best.uci;
    let loss = 0, mateAfter = false, chosenMate = null;
    if (chosen.uci !== bestUci) {
      const ln = (pos.lines || []).find((l) => l.pv && l.pv[0] && l.pv[0].slice(0, 4) === chosen.uci.slice(0, 4));
      if (ln) {
        loss = Math.max(0, pos.lines[0].cp - ln.cp);
        if (ln.mate != null) chosenMate = ln.mate;
      } else {
        try {
          const mvRes = await post("/api/move", { fen: pos.fen, uci: chosen.uci });
          mateAfter = mvRes.checkmate;
          if (!mateAfter) {
            const ev = await post("/api/eval", {
              fen: mvRes.fen, engine: $("engineSelect").value, depth: 12, movetime: 500, multipv: 1,
            });
            if (ev.lines && ev.lines.length) {
              const moverCp = q.side === "white" ? ev.lines[0].cpWhite : -ev.lines[0].cpWhite;
              loss = Math.max(0, pos.lines[0].cp - moverCp);
              if (ev.lines[0].mateWhite != null) {
                const rem = q.side === "white" ? ev.lines[0].mateWhite : -ev.lines[0].mateWhite;
                chosenMate = rem < 0 ? null : rem + 1;  // +1: the guess itself is one of the mating moves
              }
            }
          }
        } catch (e) { loss = 0; }
      }
    }
    // a forced mate slower than the engine's fastest mate is not the best move:
    // mate scores collapse the distance to ~0 cp, so grade the distance itself
    if (chosenMate != null && chosenMate > 0) {
      const bestMate = pos.lines[0] && pos.lines[0].mate != null ? pos.lines[0].mate : null;
      if (bestMate != null && bestMate > 0 && chosenMate > bestMate) {
        loss = Math.max(loss, 30 + 45 * (chosenMate - bestMate));
      }
    }
    const exact = chosen.uci === bestUci || mateAfter;
    const grade = exact ? TRAIN_GRADES[0] : gradeForLoss(loss);
    const hintCap = !!q.hintUsed;                    // hint: question worth max 5
    const pts = hintCap ? Math.min(grade.pts, 5) : grade.pts;
    q.score += pts;
    q.max += hintCap ? 5 : 10;
    const guessSan = (board.legal.find((m) => m.uci === chosen.uci) || {}).san || chosen.uci;
    playMoveSound(guessSan);
    q.results.push({ idx: q.plies[q.k], grade: grade.cls, label: grade.label, pts, guessSan, hint: hintCap });
    q.last = { grade, guessSan, loss, bestSan: mv.best.san, gameSan: mv.san, gameLabel: mv.label, skipped: false, hint: hintCap };
    q.phase = "answered";
    // show the guess on the board + reveal the best move with a green arrow
    try {
      const mvRes = await post("/api/move", { fen: pos.fen, uci: chosen.uci });
      const legal2 = await legalFor(mvRes.fen);
      board.setPosition(mvRes.fen, { legal: legal2.moves, badge: null, arrows: [], lastMove: chosen.uci, checkSq: null });
    } catch (e) { /* keep current view */ }
    board.setArrows(bestUci && bestUci !== chosen.uci
      ? [{ uci: bestUci, color: "rgba(250,214,74,.95)", width: 0.22 }]
      : []);
    quizRender();
  }

  function quizRender() {
    const q = state.quiz;
    const panel = $("quizPanel");
    if (!q) { panel.hidden = true; return; }
    panel.hidden = false;
    const { idx, mv } = quizCur();
    const Side = q.side === "white" ? "White" : "Black";
    let html = "";
    if (q.phase === "done") {
      const pct = q.max ? Math.round((q.score / q.max) * 100) : 0;
      const title = q.drill ? "Drill complete" : "Training complete";
      const sub = q.drill ? `${q.plies.length} mistake${q.plies.length === 1 ? "" : "s"} attempted` : `${q.plies.length} positions`;
      html = `<div class="quiz-top"><b>${title}</b><span class="text-muted">${sub}</span></div>
        <div class="quiz-score">Score <b>${q.score}</b> / ${q.max} <span class="text-muted">(${pct}%)</span></div>
        <div class="quiz-results">${q.results.map((r, n) => {
          const m = state.review.moves[r.idx];
          return `<div class="quiz-res-row">${m.moveNumber}${m.color === "white" ? "." : "..."} ${m.san}
            ${q.drill ? `${smallChip(m.class)} ` : ""}<span class="chip cls-${r.grade}">${badgeSymbol(r.grade) || "?"}</span> ${r.label}${r.hint ? ` <span class="q-hint-mark">(hint)</span>` : ""}</div>`;
        }).join("")}</div>
        <div class="ed-btns"><button class="btn btn-sm" id="qClose" type="button">Back to review</button>
          ${q.drill ? `<button class="btn btn-primary btn-sm" id="qRetry" type="button">Retry drill</button>` : ""}</div>`;
    } else {
      const qnum = q.drill
        ? `<div class="quiz-top"><b>${TARGET_SVG} Fix your ${mv.label.toLowerCase()}</b>
            <span class="text-muted">Mistake ${q.k + 1}/${q.plies.length}</span></div>
          <div class="quiz-score">Game played: <b>${mv.san}</b> ${smallChip(mv.class)} � Score <b>${q.score}</b> / ${q.max}</div>`
        : `<div class="quiz-top"><b>${TARGET_SVG} Find the best move for ${Side}</b>
        <span class="text-muted">Question ${q.k + 1}/${q.plies.length}</span></div>
        <div class="quiz-score">Score <b>${q.score}</b> / ${q.max}</div>`;
      if (q.phase === "question") {
        const hNote = q.hintUsed
          ? `<div class="quiz-note">Hint shown � the piece marked <b>?</b> on the board makes the best move. This question is now worth 5 points max.</div>` : "";
        html = qnum + hNote + (q.drill
          ? `<div class="quiz-hint text-small text-muted">In the game, ${Side} went wrong here. Find and play the improving move instead - graded by eval loss vs the engine's best. No peeking at the Engine tab.</div>`
          : `<div class="quiz-hint text-small text-muted">Play your guess on the board. You are graded by eval loss, like the review. No peeking at the Engine tab.</div>`) + `
          <div class="ed-btns"><button class="btn btn-xs" id="qHint" type="button"${q.hintUsed ? " disabled" : ""}>${q.hintUsed ? "Hint used" : "Hint (H)"}</button>
          <button class="btn btn-xs" id="qSkip" type="button">Skip</button>
          <button class="btn btn-xs" id="qQuit" type="button">Quit</button></div>`;
      } else {
        const L = q.last;
        if (L.skipped) {
          html = qnum + `<div class="quiz-result"><span class="chip cls-forced">?</span>
            <div><b>Skipped.</b> Best was <b>${L.bestSan}</b> � game played ${L.gameSan} (${L.gameLabel})</div></div>`;
        } else {
          const lossTxt = L.grade.cls !== "best" && L.loss > 0 ? ` <span class="text-muted">(&minus;${(L.loss / 100).toFixed(1)})</span>` : "";
          const hintTxt = L.hint ? ` <span class="q-hint-mark">(hint)</span>` : "";
          html = qnum + `<div class="quiz-result">${chip(L.grade.cls, true)}
            <div><b>${L.grade.label}!</b> you played <b>${L.guessSan}</b>${lossTxt}${hintTxt}</div></div>
            <div class="quiz-answer text-small">Best: <b>${L.bestSan}</b> � game played: ${L.gameSan} (${L.gameLabel})</div>`;
        }
        html += `<div class="ed-btns"><button class="btn btn-primary btn-sm" id="qNext" type="button">${q.k + 1 >= q.plies.length ? (q.drill ? "Finish drill" : "Finish") : (q.drill ? "Next mistake" : "Next")}</button>
          <button class="btn btn-xs" id="qQuit" type="button">Quit</button></div>`;
      }
    }
    panel.innerHTML = html;
    const next = panel.querySelector("#qNext");
    if (next) next.onclick = quizNext;
    const skip = panel.querySelector("#qSkip");
    if (skip) skip.onclick = quizSkip;
    const hint = panel.querySelector("#qHint");
    if (hint) hint.onclick = quizHint;
    const quit = panel.querySelector("#qQuit");
    if (quit) quit.onclick = quizQuit;
    const close = panel.querySelector("#qClose");
    if (close) close.onclick = quizQuit;
    const retry = panel.querySelector("#qRetry");
    if (retry) retry.onclick = () => drillStart(q.side);
  }

  /* ---------------- game library ---------------- */
  const LIB_KEY = "crs_library";
  const LIB_MAX = 8;

  function libLoad() {
    try { return JSON.parse(localStorage.getItem(LIB_KEY) || "[]"); } catch (e) { return []; }
  }
  function libSave(list) {
    try { localStorage.setItem(LIB_KEY, JSON.stringify(list.slice(0, LIB_MAX))); }
    catch (e) { toast("Library too large to save - delete a game first"); }
  }
  function libAdd(res) {
    const list = libLoad().filter((g) => g.id !== res.game.id);
    list.unshift({ id: res.game.id, savedAt: Date.now(), review: res });
    libSave(list);
  }
  function libGameName(g) {
    const r = g.review;
    return `${r.game.White || "White"} vs ${r.game.Black || "Black"}`;
  }

  function renderLibrary() {
    const list = libFilter(libLoad());
    const el = $("libList");
    const chart = $("libChart");
    $("btnLibSave").disabled = !state.review;

    // accuracy-over-time chart (per saved game, oldest first; 40-100 range)
    const pts = list.slice().reverse().map((g) => ({
      w: g.review.players.white.accuracy,
      b: g.review.players.black.accuracy,
    }));
    if (pts.length >= 2) {
      const W = 300, H = 80, n = pts.length;
      const x = (i) => 14 + (i * (W - 28)) / (n - 1);
      const clampY = (v) => H - 10 - ((Math.max(40, Math.min(100, v)) - 40) / 60) * (H - 20);
      const line = (key) => pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${clampY(p[key]).toFixed(1)}`).join(" ");
      chart.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Accuracy trend">
        <text x="14" y="12" font-size="9" fill="#98948f">accuracy % (saved games, oldest to newest)</text>
        <path d="${line("w")}" fill="none" stroke="#ece5d8" stroke-width="2"/>
        <path d="${line("b")}" fill="none" stroke="#81b64c" stroke-width="2"/>
        ${pts.map((p, i) => `<circle cx="${x(i).toFixed(1)}" cy="${clampY(p.w).toFixed(1)}" r="2.5" fill="#ece5d8"/><circle cx="${x(i).toFixed(1)}" cy="${clampY(p.b).toFixed(1)}" r="2.5" fill="#81b64c"/>`).join("")}
      </svg>`;
      chart.hidden = false;
    } else {
      chart.innerHTML = "";
      chart.hidden = true;
    }

    if (!list.length) {
      const q = (($("libSearch") && $("libSearch").value) || "").trim();
      el.innerHTML = `<div class="empty-hint small">${q
        ? `No saved games match "${q.replace(/[<>&"]/g, "")}".`
        : `No saved games yet. Analyze a game, then click "Save current game".`}</div>`;
      return;
    }
    el.innerHTML = list.map((g) => {
      const r = g.review;
      const acc = `${r.players.white.accuracy}% / ${r.players.black.accuracy}%`;
      const meta = [r.game.opening, r.game.Result, new Date(g.savedAt).toLocaleDateString()]
        .filter(Boolean).join(" � ");
      return `<div class="lib-row" data-id="${g.id}">
        <div><div class="lib-name">${libGameName(g)}</div><div class="lib-sub">${meta}</div></div>
        <span class="lib-acc">${acc}<br><small>accuracy</small></span>
        <span class="lib-actions">
          <button class="btn btn-xs lib-open" type="button">Open</button>
          <button class="btn btn-xs lib-del" type="button" aria-label="Delete saved game">?</button>
        </span>
      </div>`;
    }).join("");
    el.querySelectorAll(".lib-row").forEach((row) => {
      row.querySelector(".lib-open").onclick = () => {
        const g = libLoad().find((x) => x.id === row.dataset.id);
        if (!g) return;
        $("modalLibrary").hidden = true;
        applyReview(g.review);
        toast(`Opened: ${libGameName(g)}`, 2500);
      };
      row.querySelector(".lib-del").onclick = () => {
        const id = row.dataset.id;
        libSave(libLoad().filter((x) => x.id !== id));
        // drop trainer exercises that were sourced exclusively from this game
        post("/api/trainer/detach", { sourceId: id }).catch(() => {});
        renderLibrary();
      };
    });
  }

  /* pieces watchdog: if the FEN has pieces but none rendered, re-render once */
  function piecesWatchdog() {
    if (!board.fen) return;
    const hasPieces = board.fen.split(" ")[0].replace(/\d/g, "").length > 0;
    const rendered = board.el.querySelectorAll(".piece").length;
    if (hasPieces && rendered === 0) board.renderPieces();
  }
  setInterval(piecesWatchdog, 1500);

  /* ---------- topbar opening-name chip (v56) ----------
     Reuses the shared GET /api/explorer route (cached server-side), so the name
     costs its lookup only on positions the user actually reaches. The chip
     stays hidden while there is no name (custom/endgame positions) or the
     explorer API is unreachable. */
  const openingChipState = { fen: "", timer: 0 };
  function scheduleOpeningChip() {
    clearTimeout(openingChipState.timer);
    const chip = $("openingChip");
    if (!chip) return;
    const fen = currentFen();
    if (!fen) { chip.hidden = true; return; }
    if (openingChipState.fen === fen) return;        // same position, nothing to do
    chip.hidden = true;
    openingChipState.timer = setTimeout(async () => {
      const f = currentFen();
      if (!f) return;
      openingChipState.fen = f;
      let name = "";
      try {
        const res = await api("/api/explorer?fen=" + encodeURIComponent(f));
        name = typeof res.opening === "string" ? res.opening : "";
      } catch (e) { /* explorer offline or unauthenticated: keep the chip hidden */ }
      if (currentFen() !== f || openingChipState.fen !== f) return;   // user moved on
      if (name) { chip.textContent = name; chip.hidden = false; }
    }, 500);
  }

  /* ---------- Game Library instant search (v56) ---------- */
  function libFilter(list) {
    const q = (($("libSearch") && $("libSearch").value) || "").trim().toLowerCase();
    if (!q) return list;
    return list.filter((g) =>
      libGameName(g).toLowerCase().includes(q) ||
      `${g.review.game.opening || ""} ${g.review.game.Result || ""}`.toLowerCase().includes(q));
  }

  function repPaint(res, opts) {
    opts = opts || {};
    $("repResults").hidden = false;
    repPgn = res.pgn || "";
    state.repCurrent = res;
    if (res.rootFen) $("repRoot").value = "current";
    else $("repRoot").value = "start";
    $("repSummary").innerHTML = `<b>${res.lines.length}</b> lines for <b>${res.side}</b> (${res.mode}) �
      ${res.stats.queries} database positions checked � ${res.stats.evals ? res.stats.evals + " engine checks" : "explorer stats only"}`;
    if (!res.lines.length) {
      $("repLines").innerHTML = `<div class="empty-hint small">No lines could be built for these settings.
        If builds keep failing, add a free Lichess API token in ? settings � the explorer needs it for fresh positions.</div>`;
      repStopPreview();
      return;
    }
    $("repLines").innerHTML = res.lines.slice(0, 60).map((ln, i) =>
      `<div class="rep-line" data-li="${i}">
        <span class="rep-num">${i + 1}.</span>
        <span class="rep-moves">${ln.moves.map((m, j) =>
          `<span class="rep-mv" data-li="${i}" data-mi="${j}" title="Click to preview up to here">${m.san}</span>`).join(" ")}</span>
        <span class="rep-eval">${ln.evalCp != null ? fmtEval(ln.evalCp, null) : (ln.own != null ? ln.own.toFixed(1) + "%" : "")}</span>
        <span class="rep-games">${(ln.games || 0).toLocaleString()} games</span>
        <button class="btn btn-xs rep-copy" type="button" title="Copy line">?</button>
      </div>`).join("");
    document.querySelectorAll("#repLines .rep-line").forEach((row) => {
      const li = +row.dataset.li;
      // clicking anywhere on the row previews the FULL line (tokens preview up to that move)
      row.onclick = (e) => {
        if (e.target.closest(".rep-copy")) return;
        repPreviewLine(li, res.lines[li].moves.length - 1);
      };
      row.querySelector(".rep-copy").onclick = (e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(res.lines[li].san).then(() => toast("Line copied"), () => {});
      };
      row.querySelectorAll(".rep-mv").forEach((tok) => {
        tok.onclick = (e) => {
          e.stopPropagation();
          repPreviewLine(li, +tok.dataset.mi);
        };
      });
    });
    repStopPreview();
    // persist locally so the tab survives refreshes, and auto-save permanently
    try {
      const dump = JSON.stringify(res);
      if (dump.length < 2500000) localStorage.setItem("crs_last_rep", dump);
    } catch (e) { /* quota - skip */ }
    if (!opts.noSave) repAutoSave(res);
    repPrefetchFens(); // warm FEN caches in the background so line clicks are instant
  }

  function repRestoreLast() {
    try {
      const raw = localStorage.getItem("crs_last_rep");
      if (!raw) return false;
      const res = JSON.parse(raw);
      if (res && res.lines && res.lines.length) {
        repPaint(res, { noSave: true });
        return true;
      }
    } catch (e) { /* corrupt - start clean */ }
    return false;
  }

  /* ---- clicked-line preview: fast replay of the line up to the clicked move ---- */
  const REP_HL = "rgba(38,194,163,.9)"; // teal, clearly distinct from engine/user arrows
  const REP_STEP_MS = 120, REP_END_MS = 200; // replay pace: one move every ~120 ms
  let repPreviewSeq = 0;

  function repStopPreview() {
    repPreviewSeq++;
    state.repPreview = null;
    document.querySelectorAll("#repLines .rep-line.selected").forEach((r) => r.classList.remove("selected"));
    document.querySelectorAll("#repLines .rep-mv.seen").forEach((m) => m.classList.remove("seen"));
    if (!$("quizPanel").hidden || state.repTrain) return; // training owns the board
    renderBoard(); // snap back to the game/position you were on
  }

  // one bulk call gets the whole line's positions; cached on the line object so
  // repeat clicks (and prefetching) are pure client-side, zero server work
  async function repLineFens(line) {
    if (line._fens) return line._fens;
    const res = state.repCurrent;
    let fens = null;
    try {
      const r = await post("/api/move/chain", {
        fen: res.rootFen || START_FEN, ucis: line.moves.map((m) => m.uci),
      });
      fens = r.fens;
    } catch (e) { /* older server without the bulk route: crawl the old way */ }
    if (!fens || fens.length !== line.moves.length + 1) {
      fens = [res.rootFen || START_FEN];
      for (const m of line.moves) {
        try {
          const r = await post("/api/move", { fen: fens[fens.length - 1], uci: m.uci });
          fens.push(r.fen);
        } catch (e) { toast("Preview failed: " + e.message); throw e; }
      }
    }
    line._fens = fens;
    return fens;
  }

  async function repPreviewLine(li, mi) {
    const res = state.repCurrent;
    if (!res || !res.lines || !res.lines[li]) return;
    const line = res.lines[li];
    const seq = ++repPreviewSeq;
    state.repPreview = { seq, li, mi };
    document.querySelectorAll("#repLines .rep-line").forEach((r, n) => r.classList.toggle("selected", n === li));
    document.querySelectorAll("#repLines .rep-mv").forEach((m) =>
      m.classList.toggle("seen", +m.dataset.li === li && +m.dataset.mi <= mi));
    let fens;
    try { fens = await repLineFens(line); } catch (e) { return; }
    if (seq !== repPreviewSeq) return;            // clicked something else meanwhile
    if (!$("quizPanel").hidden || state.repTrain) return; // training took over
    // walk the line move by move with the board's glide animation, just faster
    for (let k = 0; k <= mi; k++) {
      const uci = line.moves[k].uci;
      board.setPosition(fens[k + 1], { lastMove: uci, badge: null, arrows: [], legal: [] });
      board.userSquares = [{ sq: uci.slice(0, 2), color: REP_HL }, { sq: uci.slice(2, 4), color: REP_HL }];
      board.renderArrows();
      const end = Date.now() + (k === mi ? REP_END_MS : REP_STEP_MS);
      while (Date.now() < end) {
        if (seq !== repPreviewSeq) return;
        await sleep(25);
      }
    }
  }

  // background precompute: fill every line's FEN cache right after a build/restore
  let repPrefetchJob = 0;
  async function repPrefetchFens() {
    const res = state.repCurrent;
    if (!res || !res.lines) return;
    const job = ++repPrefetchJob;
    for (const line of res.lines.slice(0, 60)) {
      if (job !== repPrefetchJob || line._fens) continue;
      await repLineFens(line).catch(() => {});
      await sleep(40); // stay gentle on the server
    }
  }

  /* ---- permanent storage: auto-save + saved list ---- */
  function repAutoId(res) {
    // stable per configuration: rebuilding updates the same entry (no duplicates)
    let h = 5381;
    const s = `${res.side}|${res.mode}|${res.rootFen || ""}`;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
    return "r" + h.toString(36);
  }
  async function repAutoSave(res) {
    try {
      const body = {
        id: repAutoId(res),
        name: `${res.side === "white" ? "White" : "Black"} � ${res.mode} � ${res.lines.length} lines`,
        side: res.side, mode: res.mode, rootFen: res.rootFen || "",
        data: { lines: res.lines, pgn: res.pgn, stats: res.stats },
      };
      const saved = await post("/api/repertoire/save", body);
      res.game = res.game || {};
      res.game.repId = saved.id;
      repRefreshSaved();
    } catch (e) { toast("Auto-save failed: " + e.message, 4000); }
  }
  async function repRefreshSaved() {
    const el = $("repSavedList");
    try {
      const res = await api("/api/repertoire/list");
      state.repSaved = res.repertoires || [];
    } catch (e) {
      el.innerHTML = `<div class="empty-hint small">Could not load saved repertoires: ${e.message}</div>`;
      return;
    }
    if (!state.repSaved.length) {
      el.innerHTML = `<div class="empty-hint small">Nothing saved yet � build a repertoire above and it is saved here automatically.</div>`;
      return;
    }
    el.innerHTML = state.repSaved.map((g) => `
      <div class="lib-row" data-id="${g.id}">
        <div><div class="lib-name rep-name" data-id="${g.id}">${g.name}</div>
          <div class="lib-sub">${g.side} � ${g.mode} � ${g.lines} lines � ${new Date(g.updatedAt * 1000).toLocaleDateString()}</div></div>
        <span class="lib-actions">
          <button class="btn btn-xs rep-open" type="button">Open</button>
          <button class="btn btn-xs rep-train" type="button">?? Train</button>
          <button class="btn btn-xs rep-rename" type="button" title="Rename">?</button>
          <button class="btn btn-xs rep-del" type="button" title="Delete">?</button>
        </span>
      </div>`).join("");
    el.querySelectorAll(".lib-row").forEach((row) => {
      const g = state.repSaved.find((x) => x.id === row.dataset.id);
      row.querySelector(".rep-open").onclick = async () => {
        try {
          const full = await api("/api/repertoire/get/" + g.id);
          switchTab("repertoire");
          repPaint({ lines: full.data.lines, pgn: full.data.pgn, stats: full.data.stats || { queries: 0 },
                     side: full.side, mode: full.mode, rootFen: full.rootFen }, { noSave: true });
        } catch (e) { toast("Open failed: " + e.message); }
      };
      row.querySelector(".rep-train").onclick = async () => {
        try {
          const full = await api("/api/repertoire/get/" + g.id);
          repTrainStart(full);
        } catch (e) { toast("Could not start training: " + e.message); }
      };
      row.querySelector(".rep-del").onclick = async () => {
        try {
          await post("/api/repertoire/delete", { id: g.id });
          toast("Repertoire deleted");
          repRefreshSaved();
        } catch (e) { toast("Delete failed: " + e.message); }
      };
      row.querySelector(".rep-rename").onclick = () => {
        const nameEl = row.querySelector(".rep-name");
        if (nameEl.querySelector("input")) return;
        const old = nameEl.textContent;
        nameEl.innerHTML = `<input class="input rep-name-input" value="${old.replace(/"/g, "&quot;")}">`;
        const input = nameEl.querySelector("input");
        input.focus(); input.select();
        const commit = async () => {
          const v = input.value.trim();
          if (v && v !== old) {
            try { await post("/api/repertoire/rename", { id: g.id, name: v }); g.name = v; } catch (e) { toast("Rename failed"); }
          }
          repRefreshSaved();
        };
        input.onblur = commit;
        input.onkeydown = (ev) => {
          if (ev.key === "Enter") { input.onblur(); }
          if (ev.key === "Escape") { input.value = old; input.onblur(); }
          ev.stopPropagation();
        };
      };
    });
  }

  /* ---------------- repertoire builder (background job) ---------------- */
  async function repBuild() {
    if ($("repBuild").disabled) return;
    const fen = repRootFen();
    $("repStatus").hidden = false;
    $("repStatus").textContent = "Building repertoire...";
    $("repBuild").disabled = true;
    try {
      const { jobId } = await post("/api/repertoire/build", {
        fen,
        side: $("repSide").value,
        mode: $("repMode").value,
        yourMoves: +$("repDepth").value || 5,
        breadth: +$("repBreadth").value || 2,
        useEngine: $("repMode").value === "strongest",
      });
      let done = false, last = "";
      while (!done) {
        await sleep(600);
        const st = await api(`/api/repertoire/status/${jobId}`);
        if (st.stage && st.stage !== last) { $("repStatus").textContent = "Building: " + st.stage; last = st.stage; }
        if (st.status === "error") throw new Error(st.error || "build failed");
        if (st.status === "done") done = true;
      }
      const res = await api(`/api/repertoire/result/${jobId}`);
      repPaint(res);
      if (res.lines.length) {
        $("repStatus").hidden = false;
        $("repStatus").textContent = `? Built and auto-saved to your library (${res.lines.length} lines) � it will still be here after refreshing.`;
      } else {
        $("repStatus").hidden = false;
        $("repStatus").textContent = "0 lines built � see the note above the results.";
      }
    } catch (e) {
      $("repStatus").hidden = false;
      if (/token/i.test(e.message)) {
        $("repStatus").textContent = "Build failed: the Lichess explorer needs a free API token � open ? settings and paste one from lichess.org/account/oauth/token";
      } else {
        $("repStatus").textContent = "Build failed: " + e.message;
      }
    } finally {
      $("repBuild").disabled = false;
    }
  }

  /* ---------------- best-move trainer on a saved repertoire ---------------- */
  /* Trie over the saved lines (branching preserved); training DFS:
     - at USER nodes: wait for the guess; correct = present in the trie
     - at OPPONENT nodes: auto-play each unvisited reply (branching covered once)
     - leaf reached = line complete; back up to the nearest unexplored branch */
  function repTrieNode(fen) {
    return { children: new Map(), fen, path: [] };
  }
  function repTrieBuild(lines, rootFen) {
    const root = repTrieNode(rootFen);
    for (const line of lines) {
      let node = root;
      for (const m of line.moves) {
        let child = node.children.get(m.uci);
        if (!child) {
          child = repTrieNode(null);
          child.uci = m.uci;
          child.san = m.san;
          child.path = [...node.path, m.uci];
          node.children.set(m.uci, child);
        }
        node = child;
      }
    }
    return root;
  }
  function repTrieNodeAt(t) {
    let node = t.root;
    for (const uci of t.path) {
      node = node.children.get(uci);
      if (!node) return null;
    }
    return node;
  }
  function repSideAt(t) {
    // side to move at the CURRENT path (root turn alternates by ply parity)
    const rootTurn = t.rootFen.split(" ")[1] === "b" ? "black" : "white";
    return t.path.length % 2 === 0 ? rootTurn : (rootTurn === "white" ? "black" : "white");
  }

  function repTrainStart(full) {
    const data = full.data;
    if (!data || !data.lines || !data.lines.length) { toast("This repertoire has no lines"); return; }
    if (state.quiz) quizQuit();
    if (editor.active) toggleEditor(false);
    repStopPreview();
    const rootFen = full.rootFen || START_FEN;
    const root = repTrieBuild(data.lines, rootFen);
    if (!root.children.size) { toast("This repertoire has no moves"); return; }
    state.repTrain = {
      repId: full.id, name: full.name, side: full.side, rootFen, root, data,
      path: [], curFen: rootFen, phase: "user", linesDone: new Set(),
      stats: { correct: 0, wrong: 0, lines: 0, total: 0 },
      feedback: { kind: "info", text: `Training as ${full.side}. Play the repertoire move.` },
      visited: new Set(),
    };
    state.liveSuspended = true;
    if ($("quizPanel").hidden) $("quizPanel").hidden = false;
    board.setPosition(rootFen, { legal: [], badge: null, arrows: [], lastMove: null, checkSq: null });
    repTrainRender();
    legalFor(rootFen).then((l) => {
      if (state.repTrain && state.repTrain.curFen === rootFen) { board.legal = l.moves; board.renderDecor(); }
    }).catch(() => {});
  }

  function repTrainQuit() {
    if (!state.repTrain) return;
    state.repTrain = null;
    state.liveSuspended = false;
    $("quizPanel").hidden = true;
    renderBoard();
  }

  async function repTrainRenderBoard(lastUci) {
    const t = state.repTrain;
    if (!t) return;
    const legal = await legalFor(t.curFen).catch(() => ({ moves: [] }));
    if (!state.repTrain || state.repTrain.curFen !== t.curFen) return;
    board.setPosition(t.curFen, {
      legal: legal.moves, badge: null, arrows: [], lastMove: lastUci || null, checkSq: null,
    });
  }

  async function repTrainPlayOpponent(uci, child) {
    const t = state.repTrain;
    try {
      const r = await post("/api/move", { fen: t.curFen, uci });
      child.fen = r.fen;
      t.path.push(uci);
      t.curFen = r.fen;
      await repTrainRenderBoard(uci);
      t.feedback = { kind: "info", text: `Opponent plays ${child.san}` };
      repTrainRender();
      await sleep(600);
      return repTrainAdvance();
    } catch (e) {
      t.feedback = { kind: "wrong", text: "Could not play the opponent move: " + e.message };
      repTrainRender();
    }
  }

  async function repTrainAdvance() {
    const t = state.repTrain;
    if (!t) return;
    const node = repTrieNodeAt(t);
    if (!node || !node.children.size) {
      // leaf: the line is fully trained
      const key = t.path.join(" ");
      if (!t.linesDone) t.linesDone = new Set();
      const before = t.linesDone.size;
      t.linesDone.add(key);
      if (t.linesDone.size > before) t.stats.lines++;
      t.feedback = { kind: "correct", text: "Line complete!" };
      return repTrainBackup();
    }
    if (repSideAt(t) === t.side) {
      t.phase = "user";
      await repTrainRenderBoard();
      t.feedback = t.feedback.kind === "info" && t.path.length
        ? t.feedback : { kind: "info", text: "Your move � play the repertoire move." };
      repTrainRender();
      return;
    }
    // opponent node: play the first unvisited reply (branches covered once each)
    for (const [uci, child] of node.children) {
      const key = t.path.join(" ") + ">" + uci;
      if (t.visited.has(key)) continue;
      t.visited.add(key);
      return repTrainPlayOpponent(uci, child);
    }
    return repTrainBackup(); // all replies visited
  }

  function repTrainBackup() {
    const t = state.repTrain;
    while (t.path.length) {
      t.path.pop();
      const node = repTrieNodeAt(t);
      if (!node) continue;
      t.curFen = node.fen || t.curFen;
      const mover = repSideAt(t);
      if (mover !== t.side) {
        for (const [uci, child] of node.children) {
          const key = t.path.join(" ") + ">" + uci;
          if (!t.visited.has(key)) {
            t.visited.add(key);
            return repTrainPlayOpponent(uci, child);
          }
        }
      }
    }
    // fully trained
    t.phase = "done";
    repTrainRender();
  }

  async function repTrainGuess(chosen) {
    const t = state.repTrain;
    if (!t || t.phase !== "user") return;
    const node = repTrieNodeAt(t);
    if (!node) return;
    const base = chosen.uci.slice(0, 4);
    let child = null;
    for (const [uci, c] of node.children) {
      if (uci.slice(0, 4) === base) { child = c; break; }
    }
    t.stats.total++;
    if (!child) {
      t.stats.wrong++;
      const expected = node.children.values().next().value;
      t.feedback = { kind: "wrong", text: `Not in your repertoire � the move is ${expected ? expected.san : "?"}. Try again.` };
      board.setArrows(expected ? [{ uci: expected.uci, color: "rgba(129,182,76,.95)", width: 0.22 }] : []);
      repTrainRender();
      return;
    }
    t.stats.correct++;
    t.feedback = { kind: "correct", text: `Correct � ${child.san}` };
    try {
      const r = await post("/api/move", { fen: t.curFen, uci: child.uci });
      child.fen = r.fen;
      t.path.push(child.uci);
      t.curFen = r.fen;
      await repTrainRenderBoard(child.uci);
    } catch (e) { /* keep going - the trie tracks the position regardless */ }
    repTrainRender();
    setTimeout(() => { if (state.repTrain && state.repTrain.phase === "user") repTrainAdvance(); }, 500);
  }

  function repTrainRender() {
    const t = state.repTrain;
    const panel = $("quizPanel");
    if (!t) { panel.hidden = true; return; }
    panel.hidden = false;
    const acc = t.stats.total ? Math.round((t.stats.correct / t.stats.total) * 100) : 100;
    const feedbackCls = t.feedback.kind === "correct" ? "cls-best" : t.feedback.kind === "wrong" ? "cls-blunder" : "cls-forced";
    const feedbackSym = t.feedback.kind === "correct" ? "?" : t.feedback.kind === "wrong" ? "?" : "?";
    let html = `<div class="quiz-top"><b>Train on repertoire</b><span class="text-muted">${t.name} � as ${t.side}</span></div>
      <div class="quiz-score">? ${t.stats.correct} � ? ${t.stats.wrong} � accuracy <b>${acc}%</b>
        � lines <b>${t.stats.lines}</b> � moves <b>${t.stats.total}</b></div>
      <div class="quiz-result"><span class="chip ${feedbackCls}">${feedbackSym}</span>
        <div>${t.feedback.text}</div></div>
      <div class="ed-btns"><button class="btn btn-xs" id="qQuit" type="button">Quit training</button></div>`;
    if (t.phase === "done") {
      const acc2 = t.stats.total ? Math.round((t.stats.correct / t.stats.total) * 100) : 100;
      html = `<div class="quiz-top"><b>Training complete</b><span class="text-muted">${t.name}</span></div>
        <div class="quiz-score">Score: <b>${t.stats.correct}/${t.stats.total}</b> (${acc2}% accuracy) �
          ${t.stats.lines} line${t.stats.lines === 1 ? "" : "s"} completed</div>
        <div class="ed-btns"><button class="btn btn-primary btn-sm" id="qAgain" type="button">Train again</button>
          <button class="btn btn-xs" id="qQuit" type="button">Close</button></div>`;
    }
    panel.innerHTML = html;
    const quit = panel.querySelector("#qQuit");
    if (quit) quit.onclick = repTrainQuit;
    const again = panel.querySelector("#qAgain");
    if (again) again.onclick = () => repTrainStart({ id: t.repId, name: t.name, side: t.side, rootFen: t.rootFen, data: t.data });
  }

  /* ---------------- wiring ---------------- */
  const board = new Board($("board"), { onMoveAttempt, onEditorSquare });
  const graph = new Graph($("graph"), $("graphTip"), {
    onSeek: (ply) => { gotoPly(ply); stopAutoplay(); },
  });
  window._crs = { board, graph, state };

  document.querySelectorAll(".review-col > .tabs .tab").forEach((t) => {
    t.onclick = () => switchTab(t.dataset.tab);
  });
  document.querySelectorAll("#modalNew .tabs .tab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll("#modalNew .tabs .tab").forEach((x) => {
        const on = x === t;
        x.classList.toggle("active", on);
        x.setAttribute("aria-selected", on);
      });
      document.querySelectorAll("#modalNew .src-pane").forEach((p) => (p.hidden = p.id !== "src-" + t.dataset.src));
    };
  });

  $("btnNew").onclick = showModal;
  $("btnNew2").onclick = showModal;
  $("modalClose").onclick = hideModal;
  $("btnFlip").onclick = () => { board.flip(); state.orientation = board.orientation; if (state.review) renderGameHead(); renderBoard(); };
  $("btnStart").onclick = () => { stopAutoplay(); gotoPly(0); };
  $("btnEnd").onclick = () => { stopAutoplay(); gotoPly(state.review ? state.review.moves.length : 0); };
  $("btnShot").onclick = savePositionImage;
  $("btnShare").onclick = copyShareLink;
  $("libSearch").oninput = () => renderLibrary();
  $("btnPrev").onclick = () => step(-1);
  $("btnNext").onclick = () => step(1);
  $("btnPlay").onclick = () => (state.playing ? stopAutoplay() : startAutoplay());
  $("pzNew").onclick = () => pzNew();
  $("pzNextBtn").onclick = () => pzNew();
  $("pzSkipBtn").onclick = () => pzNew();
  $("pzRetryBtn").onclick = pzRetry;
  $("pzHintBtn").onclick = pzHint;
  $("btnTrainerStart").onclick = startTrainerSession;
  $("btnTrainerQuit").onclick = trainerQuit;
  $("btnTrainerSkip").onclick = advanceTrainerCard;
  $("trHintBtn").onclick = trainerHint;
  $("trRevealBtn").onclick = trainerReveal;
  $("trAgain").onclick = () => trainerGrade(0);
  $("trHard").onclick = () => trainerGrade(2);
  $("trGood").onclick = () => trainerGrade(4);
  $("trEasy").onclick = () => trainerGrade(5);
  $("btnTrainerDoneBack").onclick = trainerQuit;
  $("btnTrainerSync").onclick = async () => {
    $("btnTrainerSync").disabled = true;
    const n = await trainerSyncAuto();
    $("btnTrainerSync").disabled = false;
    refreshTrainer();
    if (!n) toast("Already in sync — nothing new to add", 2500);
  };
  [$("trFilterCls"), $("trFilterSide"), $("trFilterStatus"), $("trFilterSource")].forEach((el) => {
    el.addEventListener("change", refreshTrainer);
  });
  // one-time migration: re-wrap the existing local library into the trainer DB
  try {
    if (!localStorage.getItem("crs_train_synced_63")) {
      localStorage.setItem("crs_train_synced_63", "1");
      trainerSyncAuto().then(refreshTrainer);
    }
  } catch (e) { /* private mode */ }
  try {
    const saved = localStorage.getItem("crs_puzzle_theme");
    if (saved) $("pzTheme").value = saved;
  } catch (e) { /* private mode */ }
  paintPuzzleStats();
  $("btnExitExplore").onclick = () => { if (state.playBot) stopPlayBot(); exitExplore(); switchTab("report"); };
  $("btnPlayBot").onclick = () => {
    if (state.playBot) return stopPlayBot();
    startPlayBot(($("botSideSelect") && $("botSideSelect").value) || "white");
  };
  $("botSideSelect").onchange = () => {
    if (!state.playBot) return;
    const c = $("botSideSelect").value === "black" ? "black" : "white";
    if (c !== state.playBotColor) { stopPlayBot(); startPlayBot(c); toast(`New game - now playing as ${c === "black" ? "Black" : "White"}`, 3000); }
  };
  $("btnBotHint").onclick = botHint;
  $("btnLichessFetch").onclick = fetchLichess;
  $("btnChesscomFetch").onclick = fetchChessCom;
  $("chesscomUser").addEventListener("keydown", (e) => { if (e.key === "Enter") fetchChessCom(); });
  $("lichessUser").addEventListener("keydown", (e) => { if (e.key === "Enter") fetchLichess(); });

  // hover/focus a top-moves line -> show its arrow emphasized (chess.com-style)
  function focusLineArrow(line) {
    const uci = line.dataset.uci;
    if (!uci || uci.length < 4) return;
    const rank = +line.dataset.rank || 0;
    const st = ARROW_STYLES[rank] || ARROW_STYLES[0];
    board.setArrows([{ uci, color: st.color, width: Math.min(0.3, st.width * 1.6), opacity: 1 }]);
  }
  document.querySelectorAll("#tab-engine .lines, #liveLines").forEach((box) => {
    box.addEventListener("mouseover", (e) => {
      const line = e.target.closest(".line");
      if (line && line.dataset.uci) {
        box.querySelectorAll(".line.hover").forEach((l) => l.classList.remove("hover"));
        line.classList.add("hover");
        focusLineArrow(line);
      }
    });
    box.addEventListener("click", (e) => {
      const b = e.target.closest(".lineplay");
      if (!b) return;
      e.stopPropagation();
      playLine(b.dataset.i);
    });
    box.addEventListener("mouseleave", () => {
      box.querySelectorAll(".line.hover").forEach((l) => l.classList.remove("hover"));
      board.setArrows(defaultArrows());
    });
  });
  $("btnPgn").onclick = downloadPgn;
  $("btnCopyPgn").onclick = copyPgn;
  $("btnStartReview").onclick = () => {
    const pgn = $("pgnText").value.trim();
    if (!pgn) { $("modalError").textContent = "Paste a PGN, pick a file, or choose a sample first."; $("modalError").hidden = false; return; }
    state._pgnMoves = countPgnMoves(pgn);
    startReview(pgn);
  };
  $("pgnFile").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const text = await f.text();
    $("pgnText").value = text;
    $("pgnFilePreview").textContent = text.slice(0, 400) + (text.length > 400 ? "�" : "");
  };
  $("btnSettings").onclick = () => { $("modalSettings").hidden = false; };
  $("setClose").onclick = () => ($("modalSettings").hidden = true);
  $("modalSettings").onclick = (e) => { if (e.target === $("modalSettings")) $("modalSettings").hidden = true; };
  function openKeysModal() {
    $("modalSettings").hidden = true;
    $("modalKeys").hidden = false;
  }
  $("btnKeysLink").onclick = openKeysModal;
  $("keysClose").onclick = () => ($("modalKeys").hidden = true);
  $("modalKeys").onclick = (e) => { if (e.target === $("modalKeys")) $("modalKeys").hidden = true; };
  $("modalNew").onclick = (e) => {
    // don't abandon a running review by accident
    if (e.target === $("modalNew") && $("progressPane").hidden) hideModal();
  };
  $("setSave").onclick = async () => {
    try {
      await post("/api/settings", { threads: +$("setThreads").value || 1, hash: +$("setHash").value || 128 });
      try {   // persist engine settings in the browser like the Lichess token -
              // the server only keeps them in RAM (hosted disks wipe)
        localStorage.setItem("crs_threads", +$("setThreads").value || 1);
        localStorage.setItem("crs_hash", +$("setHash").value || 128);
      } catch (e) { /* private mode */ }
      const tok = $("setLichessToken").value.trim();
      if (tok) {
        await post("/api/lichess_token", { token: tok });
        lichessTokenSave(tok);
        $("setLichessToken").value = "";
        explorerCache.clear();
        updateTokenStatus();
        toast("Lichess token saved - explorer enabled");
      }
      $("modalSettings").hidden = true;
      toast("Settings saved");
    } catch (e) { toast("Failed: " + e.message); }
  };
  /* the Lichess token is owned by the browser (localStorage) and re-seeded into
     the server on every boot. Hosted copies (Render etc.) wipe their container
     disk on restart, so the server alone is not durable enough for it. */
  function lichessTokenLocal() {
    try { return (localStorage.getItem("crs_lichess_token") || "").trim(); } catch (e) { return ""; }
  }
  function lichessTokenSave(tok) {
    try {
      if (tok) localStorage.setItem("crs_lichess_token", tok);
      else localStorage.removeItem("crs_lichess_token");
    } catch (e) { /* no storage */ }
  }
  async function updateTokenStatus() {
    const local = lichessTokenLocal();
    let set = false;
    try {
      const res = await api("/api/lichess_token");
      set = !!res.set;
      // a freshly restarted server lost its token: re-seed it from the browser
      if (local && !set) {
        try {
          await post("/api/lichess_token", { token: local });
          set = true;
        } catch (e) { /* keep going with the local copy */ }
      }
    } catch { set = false; }
    $("tokenStatus").textContent = (set || local) ? "A token is set." : "No token set.";
  }
  /* engine threads/RAM: same treatment as the token - the server holds them
     only in memory, so the browser owns the values and re-seeds on every boot */
  function restoreSettings() {
    let th = 0, hs = 0;
    try {
      th = +localStorage.getItem("crs_threads") || 0;
      hs = +localStorage.getItem("crs_hash") || 0;
    } catch (e) { /* no storage */ }
    if (th) $("setThreads").value = th;   // the settings modal shows them too
    if (hs) $("setHash").value = hs;
    if (th || hs) post("/api/settings", { threads: th || 1, hash: hs || 128 }).catch(() => {});
  }
  // picker helper: mark the active option and apply instantly on click
  function bindPicker(id, onPick) {
    const el = $(id);
    if (!el) return;
    el.querySelectorAll("button[data-value]").forEach((b) => {
      b.onclick = () => {
        el.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
        onPick(b.dataset.value);
      };
    });
  }
  function pickerValue(id, fallback) {
    const active = $(id) && $(id).querySelector("button.active");
    return active ? active.dataset.value : fallback;
  }
  function markPicker(id, value) {
    const el = $(id);
    if (el) el.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.value === value));
  }
  bindPicker("setBoardSize", (v) => {
    try { localStorage.setItem("crs_board_size", v); } catch (e) { /* no storage */ }
    applyBoardSize();
  });
  bindPicker("setBoardTheme", (v) => {
    try { localStorage.setItem("crs_board_theme", v); } catch (e) { /* no storage */ }
    applyBoardTheme();
  });
  bindPicker("setPieceTheme", (v) => {
    try { localStorage.setItem("crs_piece_theme", v); } catch (e) { /* no storage */ }
    applyPieceTheme();
    toast("Piece set updated");
  });
  $("setAnim").onchange = () => {
    try { localStorage.setItem("crs_anim", $("setAnim").checked ? "1" : "0"); } catch (e) { /* no storage */ }
    applyAnimSetting();
    toast($("setAnim").checked ? "Piece animations on" : "Piece animations off");
  };
  $("setSound").onchange = () => {
    try { localStorage.setItem("crs_sound", $("setSound").checked ? "1" : "0"); } catch (e) { /* no storage */ }
    applySoundSetting();
    if (soundOn()) playMoveSound("Nf3");   // sample the move sound when enabling
    toast($("setSound").checked ? "Move sounds on" : "Move sounds off");
  };
  $("threadsMinus").onclick = () => {
    $("setThreads").value = Math.max(1, (+$("setThreads").value || 1) - 1);
  };
  $("threadsPlus").onclick = () => {
    $("setThreads").value = Math.min(8, (+$("setThreads").value || 1) + 1);
  };
  /* light / dark appearance */
  const THEME_ICONS = {
    dark: '<svg viewBox="0 0 24 24" class="ic" aria-hidden="true"><circle cx="12" cy="12" r="4.5" fill="currentColor"/><g stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M4.8 4.8l1.8 1.8M17.4 17.4l1.8 1.8M19.2 4.8l-1.8 1.8M6.6 17.4l-1.8 1.8"/></g></svg>',
    light: '<svg viewBox="0 0 24 24" class="ic" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z" fill="currentColor"/></svg>',
  };
  function applyUiTheme() {
    let v = "dark";
    try { v = localStorage.getItem("crs_ui_theme") || "dark"; } catch (e) { /* no storage */ }
    document.documentElement.dataset.uiTheme = v;
    const b = $("btnUiTheme");
    if (b) {
      b.innerHTML = THEME_ICONS[v] || THEME_ICONS.dark;
      b.title = v === "dark" ? "Switch to light appearance" : "Switch to dark appearance";
    }
    graph.resize(); // canvas colors are theme-aware on redraw
  }
  $("btnUiTheme").onclick = () => {
    const next = document.documentElement.dataset.uiTheme === "light" ? "dark" : "light";
    try { localStorage.setItem("crs_ui_theme", next); } catch (e) { /* no storage */ }
    document.documentElement.dataset.uiTheme = next;
    applyUiTheme();
  };
  $("btnLive").onclick = () => {
    state.liveOn = !state.liveOn;
    localStorage.setItem("crs_live", state.liveOn ? "1" : "0");
    if (state.liveOn) state.live.fen = null; // resume with a fresh eval
    updateLiveBtn();
  };
  $("engineSelect").onchange = () => { state.live.fen = null; };
  $("depthSelect").onchange = () => { state.live.fen = null; };

  /* board editor wiring */
  $("btnLibrary").onclick = () => { renderLibrary(); $("modalLibrary").hidden = false; };
  $("libClose").onclick = () => ($("modalLibrary").hidden = true);
  $("btnLibSave").onclick = () => {
    if (state.review) { libAdd(state.review); renderLibrary(); toast("Game saved to library"); }
  };
  $("modalLibrary").onclick = (e) => { if (e.target === $("modalLibrary")) $("modalLibrary").hidden = true; };
  $("btnEditor").onclick = () => toggleEditor(!editor.active);
  $("edCancel").onclick = () => toggleEditor(false);
  $("edStart").onclick = () => editorLoadFen(START_FEN);
  $("edClear").onclick = () => { editor.position = {}; editor.castling = { K: false, Q: false, k: false, q: false }; editorSync(); };
  $("edFlip").onclick = () => { board.flip(); editorSync(); };
  $("edTurn").onclick = () => { editor.turn = editor.turn === "w" ? "b" : "w"; editorSync(); };
  $("edLoad").onclick = () => {
    try { editorLoadFen($("edFen").value.trim()); }
    catch (e) { toast("Could not parse FEN"); }
  };
  $("edCopy").onclick = async () => {
    try { await navigator.clipboard.writeText($("edFen").value); toast("FEN copied"); }
    catch { toast("Copy failed"); }
  };
  $("edAnalyze").onclick = editorAnalyze;
  for (const [id, key] of [["edCK", "K"], ["edCQ", "Q"], ["edCk", "k"], ["edCq", "q"]]) {
    $(id).onchange = (e) => { editor.castling[key] = e.target.checked; editorSync(); };
  }
  window.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (state.quiz) {
      if (e.key === "Enter") { quizNext(); e.preventDefault(); }
      else if (e.key === "Escape") { quizQuit(); e.preventDefault(); }
      else if (e.key === "h" || e.key === "H") { quizHint(); }
      return;
    }
    if (state.repTrain) {
      if (e.key === "Escape") { repTrainQuit(); e.preventDefault(); }
      return;
    }
    if (e.key === "Escape") {
      if (state.linePlay) { stopLinePlay(); return; }
      if (!$("modalKeys").hidden) { $("modalKeys").hidden = true; return; }
      if (!$("modalLibrary").hidden) { $("modalLibrary").hidden = true; return; }
      if (editor.active) { toggleEditor(false); return; }
      if (!$("modalSettings").hidden) { $("modalSettings").hidden = true; return; }
      if (!$("modalNew").hidden && $("progressPane").hidden) { hideModal(); return; }
      if (!$("promoOverlay").hidden) { $("promoOverlay").hidden = true; return; }
    }
    if (e.key === "ArrowLeft") { step(-1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { step(1); e.preventDefault(); }
    else if (e.key === "Home") { gotoPly(0); }
    else if (e.key === "End") { gotoPly(state.review ? state.review.moves.length : 0); }
    else if (e.key === "f" || e.key === "F") { $("btnFlip").click(); }
    else if (e.key === "[") { jumpMoment(-1); e.preventDefault(); }
    else if (e.key === "]") { jumpMoment(1); e.preventDefault(); }
    else if (e.key === "?") { openKeysModal(); e.preventDefault(); }
    else if (e.key === " ") { $("btnPlay").click(); e.preventDefault(); }
  });
  window.addEventListener("resize", () => graph.resize());

  /* ---------------- repertoire wiring ---------------- */
  let repPgn = "";
  function repRootFen() {
    if ($("repRoot").value !== "current") return START_FEN;
    return (editor.active ? board.fen : currentFen()) || START_FEN;
  }
  $("repBuild").onclick = repBuild;
  $("repCopyPgn").onclick = async () => {
    try { await navigator.clipboard.writeText(repPgn); toast("Repertoire PGN copied"); }
    catch { toast("Copy failed"); }
  };
  $("repDlPgn").onclick = () => {
    if (!repPgn) return;
    const blob = new Blob([repPgn], { type: "application/x-chess-pgn" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `repertoire-${$("repSide").value}-${$("repMode").value}.pgn`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  /* ---------------- boot ---------------- */
  // surface uncaught errors visibly instead of failing silently
  window.addEventListener("error", (e) => toast("Error: " + e.message, 5000));
  window.addEventListener("unhandledrejection", (e) => toast("Error: " + (e.reason && e.reason.message || e.reason), 5000));
  $("btnPlay").innerHTML = ICON_PLAY;
  $("brandHome").onclick = goHome;
  applyUiTheme();
  repRefreshSaved();
  applyBoardSize();
  applyBoardTheme();
  applyAnimSetting();
  applyPieceTheme();
  applySoundSetting();
  buildTray();
  updateLiveBtn();
  restoreSettings();
  updateTokenStatus();
  liveEngineLoop();
  loadEngines()
    .catch((e) => toast("Engine list failed: " + e.message, 5000))
    .then(async () => {
      if (maybeLoadShareLink()) {
        // #fen= link: land straight in analysis mode on the shared position
        renderAccuracySummary();
        enterExplore();
        renderBoard();
        toast("Loaded shared position", 3000);
      } else if (!(await restoreSession())) {
        // first visit / nothing saved: open straight into analysis mode so the
        // board is playable immediately and the engine's top moves stream in
        board.setPosition("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
        enterExplore();
        renderBoard();
      }
      repRestoreLast(); // reopen the last built/opened repertoire (tab + preview)
      // v66: keep the Lab fed with every locally-saved review at boot
      // (was a one-shot v64 flag; now a self-healing count-diff sync so container
      // wipes on Render re-attach the whole library automatically)
      perfSyncLibrary().catch(() => {});
      loadSamples();
      maybeReattachJob(); // a review that was running at refresh keeps going
    });
})();
