"use strict";

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}

function voiceId(id) {
  return String(id);
}

function safeCalc(expr) {
  const src = String(expr).trim();
  if (!src) throw new Error("empty expression");
  if (!/^[0-9+\-*/().%\s^eE]+$/.test(src)) throw new Error("illegal characters");
  const js = src.replace(/\^/g, "**");
  const val = Function('"use strict"; return (' + js + ")")();
  if (typeof val !== "number" || !Number.isFinite(val)) throw new Error("not a finite number");
  return val;
}

function parseCommand(line) {
  const trimmed = String(line).trim();
  if (!trimmed) return { cmd: "", arg: "" };
  const cmd = trimmed.split(/\s+/)[0];
  const arg = trimmed.slice(cmd.length).trim();
  return { cmd, arg };
}

function createGrid(size, fill) {
  const v = fill === undefined ? null : fill;
  return Array.from({ length: size }, () => Array(size).fill(v));
}

function floodFill(grid, x, y, target, repl) {
  const h = grid.length;
  const w = grid[0] ? grid[0].length : 0;
  if (target === repl) return grid;
  const st = [[x, y]];
  while (st.length) {
    const [cx, cy] = st.pop();
    if (cx < 0 || cy < 0 || cx >= w || cy >= h) continue;
    if (grid[cy][cx] !== target) continue;
    grid[cy][cx] = repl;
    st.push([cx + 1, cy], [cx - 1, cy], [cx, cy + 1], [cx, cy - 1]);
  }
  return grid;
}

function paintCell(grid, x, y, color) {
  if (y < 0 || x < 0 || y >= grid.length || x >= grid[0].length) return grid;
  grid[y][x] = color;
  return grid;
}

function circlesCollide(x1, y1, x2, y2, radius) {
  return Math.hypot(x1 - x2, y1 - y2) < radius;
}

function killScore(wave) {
  return 50 * wave;
}

function spawnEnemyCount(wave) {
  return 4 + wave * 2;
}

function spawnRockCount(wave) {
  return 2 + wave;
}

function formatUptime(ms) {
  return Math.floor(ms / 1000) + "s";
}

function masterGain(muted, volume) {
  return muted ? 0 : volume;
}

function nextZ(current) {
  return current + 1;
}

function applyPowerup(player, kind) {
  const next = {
    shield: player.shield,
    multishot: player.multishot
  };
  if (kind === "shield") next.shield = Math.min(5, player.shield + 2);
  else if (kind === "multi") next.multishot = 420;
  return next;
}

function hitPlayer(player) {
  if (player.shield > 0) {
    return { shield: player.shield - 1, over: false };
  }
  return { shield: player.shield, over: true };
}

function dispatchTerm(cmd, arg, ctx) {
  const c = ctx || {};
  switch (cmd) {
    case "help":
      return { type: "print", text: "help, matrix, calc <expr>, echo <text>, sysinfo, clear, date, whoami" };
    case "echo":
      return { type: "print", text: arg || "" };
    case "clear":
      return { type: "clear" };
    case "matrix":
      return { type: "matrix-toggle" };
    case "calc":
      try {
        return { type: "print", text: String(safeCalc(arg)) };
      } catch (e) {
        return { type: "error", text: "calc error: " + e.message };
      }
    case "whoami":
      return { type: "print", text: "root  ·  CYBER-OS 2077" };
    case "date":
      return { type: "print", text: (c.now || new Date()).toString() };
    case "sysinfo":
      return { type: "sysinfo" };
    case "":
      return { type: "noop" };
    default:
      return { type: "error", text: "unknown command: " + cmd + "  (try help)" };
  }
}

function WindowRegistry() {
  this.map = new Map();
  this.z = 20;
  this.focused = null;
}

WindowRegistry.prototype.open = function (appId, meta) {
  for (const [id, w] of this.map) {
    if (w.appId === appId) {
      this.bringToFront(id);
      return { id, reused: true };
    }
  }
  const id = appId + "-" + (meta && meta.now ? meta.now : Date.now());
  this.z += 1;
  this.map.set(id, { id, appId, z: this.z, minimized: false, ram: (meta && meta.ram) || 32 });
  this.focused = id;
  return { id, reused: false };
};

WindowRegistry.prototype.bringToFront = function (id) {
  const w = this.map.get(id);
  if (!w) return null;
  this.z += 1;
  w.z = this.z;
  w.minimized = false;
  this.focused = id;
  return w;
};

WindowRegistry.prototype.minimize = function (id) {
  const w = this.map.get(id);
  if (!w) return;
  w.minimized = true;
  if (this.focused === id) this.focused = null;
};

WindowRegistry.prototype.close = function (id) {
  this.map.delete(id);
  if (this.focused === id) this.focused = null;
};

WindowRegistry.prototype.list = function () {
  return [...this.map.values()];
};

module.exports = {
  clamp,
  voiceId,
  safeCalc,
  parseCommand,
  createGrid,
  floodFill,
  paintCell,
  circlesCollide,
  killScore,
  spawnEnemyCount,
  spawnRockCount,
  formatUptime,
  masterGain,
  nextZ,
  applyPowerup,
  hitPlayer,
  dispatchTerm,
  WindowRegistry
};
