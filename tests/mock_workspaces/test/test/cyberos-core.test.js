"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const core = require("../cyberos-core");
const { calculateArcadeScore } = require("../buggy_component");

describe("safeCalc", () => {
  it("adds and multiplies with precedence", () => {
    assert.equal(core.safeCalc("2 + 3 * 4"), 14);
  });

  it("handles parentheses and division", () => {
    assert.equal(core.safeCalc("(10-2)/4"), 2);
  });

  it("treats ^ as exponent", () => {
    assert.equal(core.safeCalc("2^8"), 256);
  });

  it("allows decimals and modulo", () => {
    assert.equal(core.safeCalc("10.5 % 3"), 1.5);
  });

  it("trims whitespace", () => {
    assert.equal(core.safeCalc("  9  "), 9);
  });

  it("rejects empty input", () => {
    assert.throws(() => core.safeCalc("   "), /empty expression/);
  });

  it("rejects identifiers and assignment", () => {
    assert.throws(() => core.safeCalc("alert(1)"), /illegal characters/);
    assert.throws(() => core.safeCalc("1;process"), /illegal characters/);
  });

  it("rejects division by zero as non-finite", () => {
    assert.throws(() => core.safeCalc("1/0"), /not a finite number/);
  });
});

describe("parseCommand", () => {
  it("splits command and argument", () => {
    assert.deepEqual(core.parseCommand("echo hello world"), { cmd: "echo", arg: "hello world" });
  });

  it("returns empty for blank lines", () => {
    assert.deepEqual(core.parseCommand("  "), { cmd: "", arg: "" });
  });

  it("supports commands with no args", () => {
    assert.deepEqual(core.parseCommand("help"), { cmd: "help", arg: "" });
  });
});

describe("dispatchTerm", () => {
  it("lists commands on help", () => {
    const r = core.dispatchTerm("help", "");
    assert.equal(r.type, "print");
    assert.match(r.text, /calc/);
    assert.match(r.text, /matrix/);
  });

  it("echoes text and empty echo", () => {
    assert.equal(core.dispatchTerm("echo", "neon").text, "neon");
    assert.equal(core.dispatchTerm("echo", "").text, "");
  });

  it("clears and toggles matrix", () => {
    assert.equal(core.dispatchTerm("clear", "").type, "clear");
    assert.equal(core.dispatchTerm("matrix", "").type, "matrix-toggle");
  });

  it("evaluates calc and surfaces errors", () => {
    assert.equal(core.dispatchTerm("calc", "3*7").text, "21");
    const err = core.dispatchTerm("calc", "nope");
    assert.equal(err.type, "error");
    assert.match(err.text, /calc error/);
  });

  it("whoami and date", () => {
    assert.match(core.dispatchTerm("whoami", "").text, /root/);
    const now = new Date("2077-01-01T00:00:00Z");
    assert.equal(core.dispatchTerm("date", "", { now }).text, now.toString());
  });

  it("unknown command", () => {
    const r = core.dispatchTerm("hack", "");
    assert.equal(r.type, "error");
    assert.match(r.text, /unknown command: hack/);
  });

  it("noop on empty command", () => {
    assert.equal(core.dispatchTerm("", "").type, "noop");
  });
});

describe("pixel editor", () => {
  it("paints a cell inside bounds", () => {
    const g = core.createGrid(16);
    core.paintCell(g, 2, 3, "#00f3ff");
    assert.equal(g[3][2], "#00f3ff");
  });

  it("ignores out-of-bounds paint", () => {
    const g = core.createGrid(2, null);
    core.paintCell(g, 9, 9, "#ff0055");
    assert.equal(g[0][0], null);
  });

  it("flood fills a connected region", () => {
    const g = core.createGrid(4, null);
    g[1][1] = "#fff";
    g[1][2] = "#fff";
    g[2][1] = "#fff";
    core.floodFill(g, 1, 1, "#fff", "#00f3ff");
    assert.equal(g[1][1], "#00f3ff");
    assert.equal(g[1][2], "#00f3ff");
    assert.equal(g[2][1], "#00f3ff");
    assert.equal(g[0][0], null);
  });

  it("does nothing when fill color matches target", () => {
    const g = [["a", "a"], ["a", "b"]];
    core.floodFill(g, 0, 0, "a", "a");
    assert.deepEqual(g, [["a", "a"], ["a", "b"]]);
  });

  it("eraser fill replaces with null", () => {
    const g = core.createGrid(2, "#ffb800");
    core.floodFill(g, 0, 0, "#ffb800", null);
    assert.equal(g[0][0], null);
    assert.equal(g[1][1], null);
  });
});

describe("cyber-shooter rules", () => {
  it("scores 50 per wave on kill", () => {
    assert.equal(core.killScore(1), 50);
    assert.equal(core.killScore(4), 200);
  });

  it("scales enemy and asteroid counts with wave", () => {
    assert.equal(core.spawnEnemyCount(1), 6);
    assert.equal(core.spawnRockCount(3), 5);
  });

  it("detects circle collisions", () => {
    assert.equal(core.circlesCollide(0, 0, 3, 4, 6), true);
    assert.equal(core.circlesCollide(0, 0, 3, 4, 4), false);
  });

  it("clamps player position", () => {
    assert.equal(core.clamp(1.2, 0.04, 0.96), 0.96);
    assert.equal(core.clamp(-1, 0.04, 0.96), 0.04);
    assert.equal(core.clamp(0.5, 0.04, 0.96), 0.5);
  });

  it("applies shield and multishot drops", () => {
    const s = core.applyPowerup({ shield: 1, multishot: 0 }, "shield");
    assert.equal(s.shield, 3);
    const capped = core.applyPowerup({ shield: 4, multishot: 0 }, "shield");
    assert.equal(capped.shield, 5);
    const m = core.applyPowerup({ shield: 0, multishot: 0 }, "multi");
    assert.equal(m.multishot, 420);
  });

  it("consumes shield before game over", () => {
    assert.deepEqual(core.hitPlayer({ shield: 2 }), { shield: 1, over: false });
    assert.deepEqual(core.hitPlayer({ shield: 0 }), { shield: 0, over: true });
  });
});

describe("audio and window manager", () => {
  it("stringifies voice ids so mouse and keyboard share a map key", () => {
    assert.equal(core.voiceId(261.63), core.voiceId("261.63"));
  });

  it("mutes master gain", () => {
    assert.equal(core.masterGain(true, 0.7), 0);
    assert.equal(core.masterGain(false, 0.7), 0.7);
  });

  it("increments z-index", () => {
    assert.equal(core.nextZ(20), 21);
  });

  it("formats uptime in whole seconds", () => {
    assert.equal(core.formatUptime(2500), "2s");
    assert.equal(core.formatUptime(0), "0s");
  });

  it("reuses a single instance per app and focuses it", () => {
    const wm = new core.WindowRegistry();
    const a = wm.open("shooter", { now: 1, ram: 40 });
    const b = wm.open("shooter", { now: 2 });
    assert.equal(a.reused, false);
    assert.equal(b.reused, true);
    assert.equal(a.id, b.id);
    assert.equal(wm.map.size, 1);
  });

  it("stacks z-order and restore from minimize", () => {
    const wm = new core.WindowRegistry();
    const s = wm.open("synth", { now: 1 });
    const t = wm.open("term", { now: 2 });
    assert.ok(wm.map.get(t.id).z > wm.map.get(s.id).z);
    wm.minimize(t.id);
    assert.equal(wm.focused, null);
    wm.bringToFront(t.id);
    assert.equal(wm.focused, t.id);
    assert.equal(wm.map.get(t.id).minimized, false);
  });

  it("kill process removes the window", () => {
    const wm = new core.WindowRegistry();
    const w = wm.open("tasks", { now: 9 });
    wm.close(w.id);
    assert.equal(wm.map.size, 0);
    assert.equal(wm.focused, null);
    assert.equal(wm.list().length, 0);
  });
});

describe("calculateArcadeScore (legacy arcade helper)", () => {
  it("returns product when total is at most 1000", () => {
    assert.equal(calculateArcadeScore(100, 10), 1000);
    assert.equal(calculateArcadeScore(0, 99), 0);
  });

  it("applies 1.5x bonus above 1000", () => {
    assert.equal(calculateArcadeScore(100, 11), 1650);
  });
});
