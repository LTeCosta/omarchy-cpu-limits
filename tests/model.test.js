// Tests for the CPU Limits shell plugin's Model.js.
// Run from the plugin directory: node --test
"use strict"
const assert = require("node:assert")
const { test } = require("node:test")
const fs = require("fs")
const path = require("path")
const vm = require("vm")

const source = fs.readFileSync(path.join(__dirname,
  "../Model.js"), "utf8")
const Model = {}
vm.runInNewContext(source.replace(/^\.pragma library$/m, ""), Model)

// The status schema 1 contract the service publishes.
const fixtureText = fs.readFileSync(path.join(__dirname, "fixtures/status.json"), "utf8")
const fixture = () => JSON.parse(fixtureText)
const NOW = 1794680002 // two seconds after the fixture's `updated`
const parse = (status, now = NOW) => Model.parseStatus(JSON.stringify(status), now)
// vm-context objects have a foreign prototype; compare them as plain JSON.
const plain = value => JSON.parse(JSON.stringify(value))

const base = { ceilings_mhz: { preferred: 5400, p: 5200, e: 3800 }, turbo: true, pl1_w: null, pl2_w: null }
const group = { id: "p", min_mhz: 800, max_mhz: 5600 }
const tests = {
  "helper outcome reports a superseded save"() {
    const answer = JSON.stringify({ profile: "performance", applied: true, revision: 4, superseded: true })
    assert.match(Model.helperOutcome(0, "", "quiet", 0, answer).message, /later change replaced it/)
    assert.strictEqual(Model.helperOutcome(0, "", "quiet", 0, "{\"superseded\":false}").message, "Applied to Quiet")
    assert.strictEqual(Model.helperOutcome(0, "", "quiet", 0, "not json").message, "Applied to Quiet")
  },
  "status parsing marks stale and service errors"() {
    assert.strictEqual(Model.parseStatus("", 100).stale, true)
    assert.strictEqual(Model.parseStatus("{", 100).error, "Unreadable CPU status")
    const fresh = parse(fixture())
    assert.deepStrictEqual([fresh.stale, fresh.error, fresh.status.profile], [false, "", "performance"])
    assert.deepStrictEqual(plain(fresh.warnings), [])
    assert.match(parse(fixture(), NOW + 20).error, /not running/)
    assert.strictEqual(parse({ ...fixture(), error: "Readback" }).error, "Readback")
  },
  "status parsing rejects non-objects, missing fields and other schemas"() {
    for (const text of ["null", "[]", "42", "\"text\""]) {
      const parsed = Model.parseStatus(text, NOW)
      assert.deepStrictEqual([parsed.status, parsed.stale, parsed.error], [null, true, "Unreadable CPU status"], text)
    }
    for (const field of ["updated", "profile", "profiles", "groups", "limits", "thermal"]) {
      const status = fixture()
      delete status[field]
      const parsed = parse(status)
      assert.strictEqual(parsed.status, null, field)
      assert.match(parsed.error, new RegExp("Incomplete CPU status.*" + field))
    }
    assert.strictEqual(parse({ ...fixture(), groups: {} }).status, null)
    for (const schema of [undefined, 0, 2, "1"]) {
      const parsed = parse({ ...fixture(), schema })
      assert.strictEqual(parsed.status, null)
      assert.match(parsed.error, /versions differ; restart the shell \(omarchy-restart-shell\) or the service/)
    }
    // A service whose hardware discovery failed publishes a minimal status:
    // the panel shows its error rather than "incomplete".
    const failed = parse({ ...fixture(), profile: null, profiles: {}, groups: [], power: null,
      applied: false, error: "No default ceiling for policy3" })
    assert.deepStrictEqual([failed.status, failed.error], [null, "No default ceiling for policy3"])
    const leftBehind = Model.parseStatus(JSON.stringify({ ...fixture(), profile: null, profiles: {}, groups: [],
      applied: false, error: "No default ceiling for policy3" }), fixture().updated + 60)
    assert.deepStrictEqual([leftBehind.status, leftBehind.error], [null, "cpu-frequency-limits.service is not running"])
    // An object without the schema 1 fields is rejected.
    assert.strictEqual(Model.parseStatus(JSON.stringify({ updated: NOW }), NOW).status, null)
  },
  "staleness follows limits.stale_after_s"() {
    const status = fixture()
    status.limits.stale_after_s = 30
    assert.strictEqual(parse(status, status.updated + 25).stale, false)
    assert.strictEqual(parse(status, status.updated + 31).stale, true)
    assert.strictEqual(parse(fixture(), fixture().updated + 11).stale, true)
    delete status.limits.stale_after_s
    assert.strictEqual(parse(status, status.updated + 9).stale, false, "defaults to 10 s")
    assert.strictEqual(parse(status, status.updated + 11).stale, true)
  },
  "warnings are passed through as strings"() {
    const parsed = parse({ ...fixture(), warnings: ["MMIO limit managed by thermald", 3, ""] })
    assert.deepStrictEqual(plain(parsed.warnings), ["MMIO limit managed by thermald"])
    assert.deepStrictEqual(plain(Model.parseStatus("null", NOW).warnings), [])
  },
  "ceilings step by limits.step_mhz and clamp to the hardware range"() {
    assert.strictEqual(Model.stepCeiling(base, group, 1).ceilings_mhz.p, 5300, "defaults to 100 MHz")
    assert.strictEqual(Model.stepCeiling(base, group, 1, fixture()).ceilings_mhz.p, 5300)
    const coarse = fixture()
    coarse.limits.step_mhz = 250
    assert.strictEqual(Model.stepCeiling(base, group, -1, coarse).ceilings_mhz.p, 4950)
    const top = Model.stepCeiling({ ...base, ceilings_mhz: { ...base.ceilings_mhz, p: 5600 } }, group, 1, fixture())
    assert.strictEqual(top.ceilings_mhz.p, 5600)
    assert.strictEqual(base.ceilings_mhz.p, 5200, "input is not mutated")
  },
  "firmware and start values come from the edited profile"() {
    const status = fixture()
    assert.strictEqual(Model.firmwareFor(status, "balanced", "pl1_w"), 160)
    assert.strictEqual(Model.firmwareFor(status, "quiet", "pl1_w"), null)
    assert.strictEqual(Model.firmwareFor(status, "missing", "pl2_w"), null)
    assert.strictEqual(Model.firmwareFor(null, "quiet", "pl2_w"), null)
    assert.strictEqual(Model.startValue(status, "quiet", { ...base, pl1_w: 45 }, "pl1_w"), 45)
    assert.strictEqual(Model.startValue(status, "balanced", base, "pl2_w"), 175)
    // Never observed: fall back to the MSR value.
    assert.strictEqual(Model.startValue(status, "quiet", base, "pl2_w"), 175)
    status.profiles.balanced.firmware_pl1_w = 120
    assert.strictEqual(Model.startValue(status, "balanced", base, "pl1_w"), 120)
    assert.strictEqual(Model.startValue({ ...status, power: null }, "quiet", base, "pl1_w"), null)
  },
  "power limits start from the firmware value and keep PL2 >= PL1"() {
    const status = fixture()
    const step = (settings, key, direction) => Model.stepPower(settings, key, direction, status, "performance")
    const lowered = step(base, "pl1_w", -1)
    assert.deepStrictEqual([lowered.pl1_w, lowered.pl2_w], [155, null])
    const raised = step({ ...base, pl1_w: 175, pl2_w: 175 }, "pl1_w", 1)
    assert.deepStrictEqual([raised.pl1_w, raised.pl2_w], [180, 180])
    const firmwarePl2 = step({ ...base, pl1_w: 175 }, "pl1_w", 1)
    assert.deepStrictEqual([firmwarePl2.pl1_w, firmwarePl2.pl2_w], [180, 180])
    const pl2Down = step({ ...base, pl1_w: 120, pl2_w: 120 }, "pl2_w", -1)
    assert.deepStrictEqual([pl2Down.pl1_w, pl2Down.pl2_w], [115, 115])
    const pl2BelowFirmwarePl1 = step({ ...base, pl2_w: 160 }, "pl2_w", -1)
    assert.deepStrictEqual([pl2BelowFirmwarePl1.pl1_w, pl2BelowFirmwarePl1.pl2_w], [155, 155])
    assert.strictEqual(step({ ...base, pl1_w: 15 }, "pl1_w", -1).pl1_w, 15)
    assert.strictEqual(step({ ...base, pl2_w: 215 }, "pl2_w", 1).pl2_w, 215)
  },
  "power steps use limits and the selected inactive profile's firmware"() {
    const status = fixture()
    status.profiles.balanced.firmware_pl1_w = 100
    status.profiles.balanced.firmware_pl2_w = 110
    const balanced = Model.stepPower(base, "pl1_w", 1, status, "balanced")
    assert.deepStrictEqual([balanced.pl1_w, balanced.pl2_w], [105, null], "not the active profile's 160 W")
    const carried = Model.stepPower({ ...base, pl1_w: 110 }, "pl1_w", 1, status, "balanced")
    assert.deepStrictEqual([carried.pl1_w, carried.pl2_w], [115, 115])
    // Quiet never reported firmware values: it starts from the MSR value.
    assert.strictEqual(Model.stepPower(base, "pl1_w", -1, status, "quiet").pl1_w, 155)
    status.limits.power_step_w = 10
    status.limits.min_w = 50
    status.limits.max_w = 120
    assert.strictEqual(Model.stepPower(base, "pl2_w", 1, status, "balanced").pl2_w, 120)
    assert.strictEqual(Model.stepPower({ ...base, pl1_w: 55 }, "pl1_w", -1, status, "balanced").pl1_w, 50)
  },
  "returning a limit to firmware never leaves PL2 below PL1"() {
    const status = fixture()
    assert.strictEqual(Model.useFirmware({ ...base, pl1_w: 100 }, "pl1_w", status, "performance").pl1_w, null)
    const inverted = Model.useFirmware({ ...base, pl1_w: 100, pl2_w: 120 }, "pl1_w", status, "performance")
    assert.deepStrictEqual([inverted.pl1_w, inverted.pl2_w], [null, null], "firmware PL1 160 > PL2 120")
    const kept = Model.useFirmware({ ...base, pl1_w: 100, pl2_w: 170 }, "pl1_w", status, "performance")
    assert.deepStrictEqual([kept.pl1_w, kept.pl2_w], [null, 170])
    const pl2 = Model.useFirmware({ ...base, pl1_w: 180, pl2_w: 200 }, "pl2_w", status, "performance")
    assert.deepStrictEqual([pl2.pl1_w, pl2.pl2_w], [null, null], "firmware PL2 175 < PL1 180")
    // Quiet has no observed firmware value: the MSR value stands in for it.
    const quiet = Model.useFirmware({ ...base, pl1_w: 45, pl2_w: 65 }, "pl1_w", status, "quiet")
    assert.deepStrictEqual([quiet.pl1_w, quiet.pl2_w], [null, null])
  },
  "draft reducers return new maps and drop drafts equal to the saved value"() {
    const saved = Model.settingsOf(base)
    const edited = { ...saved, turbo: false }
    const empty = {}
    const one = Model.withDraft(empty, "quiet", edited, saved)
    assert.deepStrictEqual(Object.keys(one), ["quiet"])
    assert.deepStrictEqual(Object.keys(empty), [], "input is not mutated")
    const back = Model.withDraft(one, "quiet", Model.settingsOf(base), saved)
    assert.deepStrictEqual(Object.keys(back), [], "an edit back to the saved value is not pending")
    assert.deepStrictEqual(Object.keys(one), ["quiet"])
    const two = Model.withDraft(one, "balanced", edited, saved)
    assert.deepStrictEqual(Object.keys(two).sort(), ["balanced", "quiet"])
    const removed = Model.withoutDraft(two, "quiet")
    assert.deepStrictEqual(Object.keys(removed), ["balanced"])
    assert.notStrictEqual(Model.withoutDraft(two, "missing"), two)
    assert.deepStrictEqual(Object.keys(Model.afterSave(two, "quiet")), ["balanced"])
    assert.deepStrictEqual(Object.keys(Model.afterSave(two, "all")), [])
    assert.deepStrictEqual(Object.keys(two).sort(), ["balanced", "quiet"])
    assert.strictEqual(Model.otherDraftCount(two, "quiet"), 1)
    assert.strictEqual(Model.otherDraftCount(two, "performance"), 2)
  },
  "pruning drops drafts that match the saved settings or a vanished profile"() {
    const status = fixture()
    const quietSaved = Model.settingsOf(status.profiles.quiet)
    const drafts = {
      quiet: quietSaved,
      balanced: { ...Model.settingsOf(status.profiles.balanced), turbo: false },
      gone: quietSaved,
    }
    const pruned = Model.pruneDrafts(drafts, status.profiles)
    assert.deepStrictEqual(Object.keys(pruned), ["balanced"])
    assert.deepStrictEqual(Object.keys(drafts).length, 3, "input is not mutated")
    assert.deepStrictEqual(Object.keys(Model.pruneDrafts(drafts, null)), [])
  },
  "helper outcomes"() {
    assert.deepStrictEqual(plain(Model.helperOutcome(0, "", "performance", 0)), { ok: true, message: "Applied to Performance" })
    assert.deepStrictEqual(plain(Model.helperOutcome(0, "", "all", 0)), { ok: true, message: "Applied to every profile" })
    assert.strictEqual(Model.helperOutcome(0, "", "all", 1).message,
      "Applied to every profile; unsaved changes in 1 other profile were discarded")
    assert.strictEqual(Model.helperOutcome(0, "", "all", 2).message,
      "Applied to every profile; unsaved changes in 2 other profiles were discarded")
    const pkexecText = "Error executing command as another user: Request dismissed\n"
    assert.deepStrictEqual(plain(Model.helperOutcome(126, pkexecText, "quiet", 0)),
      { ok: false, message: "Authentication was cancelled" })
    assert.deepStrictEqual(plain(Model.helperOutcome(127, "Error executing command as another user: Not authorized\n", "quiet", 0)),
      { ok: false, message: "Not authorized to change CPU limits" })
    assert.deepStrictEqual(plain(Model.helperOutcome(1, "Traceback\nPL2 must not be below PL1\n\n", "quiet", 0)),
      { ok: false, message: "PL2 must not be below PL1" })
    assert.deepStrictEqual(plain(Model.helperOutcome(2, "  \n", "quiet", 0)),
      { ok: false, message: "The change could not be applied" })
    assert.strictEqual(Model.helperOutcome(-1, null, "quiet", 0).message, "The change could not be applied")
  },
  "settings comparison covers every field"() {
    assert.ok(Model.sameSettings(base, Model.settingsOf(base)))
    assert.ok(!Model.sameSettings(base, { ...base, turbo: false }))
    assert.ok(!Model.sameSettings(base, Model.stepCeiling(base, { id: "e", min_mhz: 800, max_mhz: 4100 }, -1)))
  },
  "formatting"() {
    assert.strictEqual(Model.ghz(1517), "1.52 GHz")
    assert.strictEqual(Model.ghz(5200), "5.2 GHz")
    assert.strictEqual(Model.wattsText(42.34), "42.3 W")
    assert.strictEqual(Model.wattsText(160), "160 W")
    assert.strictEqual(Model.profileLabel("performance"), "Performance")
    assert.strictEqual(Model.profileLabel("custom-silent"), "Custom Silent")
    assert.strictEqual(Model.throttleText({ throttling: true, throttle_count: 5699 }),
      "Thermal throttling now · 5,699 events since boot")
  },
  "throttle text reports the share of time throttled"() {
    const thermal = fixture().thermal
    assert.strictEqual(Model.throttleText(thermal), "Thermal throttling 12.5% of the time · 7,635 events since boot")
    assert.strictEqual(Model.throttleText({ ...thermal, throttle_pct: 1.2, throttling: false }),
      "No sustained thermal throttling · 7,635 events since boot")
    assert.strictEqual(Model.throttleText({ ...thermal, throttle_count: null, throttle_pct: 33.333 }),
      "Thermal throttling 33.3% of the time")
    assert.strictEqual(Model.throttleText(null), "")
  },
  "power captions show the effective MMIO limit"() {
    const status = fixture()
    assert.strictEqual(Model.powerCaption(status, "pl1_w"), "effective 55 W · MMIO 55 W (thermald)")
    assert.strictEqual(Model.powerCaption(status, "pl2_w"), "effective 125 W · MMIO 125 W (thermald)")
    status.power.mmio = null
    assert.strictEqual(Model.powerCaption(status, "pl1_w"), "")
    assert.strictEqual(Model.powerCaption({ ...status, power: null }, "pl1_w"), "")
    assert.strictEqual(Model.powerCaption(null, "pl1_w"), "")
  },
  "bar state, label and tooltip from the schema 1 fixture"() {
    const parsed = parse(fixture())
    assert.strictEqual(Model.barKind(parsed), "throttling")
    assert.strictEqual(Model.heroMeta(parsed), "Performance · custom limits")
    assert.strictEqual(Model.barLabel(parsed, "temperature"), "77°")
    assert.strictEqual(Model.barLabel(parsed, "power"), "44W")
    assert.strictEqual(Model.barLabel(parsed, "none"), "")
    const tooltip = Model.tooltip(parsed)
    assert.match(tooltip, /^CPU Limits · Performance · custom limits\n77 °C · 43.8 W · 11% load\n/)
    assert.match(tooltip, /Preferred P-cores 5400 MHz\nP-cores 5200 MHz\nE-cores 3800 MHz/)
    assert.match(tooltip, /PL1 55 W · PL2 125 W effective$/, "the MMIO zone, not the MSR's 160 W")
    const quiet = fixture()
    quiet.profile = "balanced"
    quiet.thermal.throttling = false
    quiet.power.mmio = null
    quiet.power.effective_pl1_w = null
    quiet.power.effective_pl2_w = null
    quiet.turbo = false
    const calm = parse(quiet)
    assert.strictEqual(Model.barKind(calm), "ok")
    assert.strictEqual(Model.heroMeta(calm), "Balanced · defaults")
    assert.match(Model.tooltip(calm), /PL1 160 W · PL2 175 W · turbo off$/)
    const stale = parse(fixture(), NOW + 60)
    assert.strictEqual(Model.barKind(stale), "error")
    assert.strictEqual(Model.heroMeta(stale), "Service stopped")
    assert.strictEqual(Model.barLabel(stale, "power"), "")
    const missing = Model.parseStatus("", NOW)
    assert.strictEqual(Model.heroMeta(missing), "Unavailable")
    assert.strictEqual(Model.tooltip(missing), "CPU Limits\ncpu-frequency-limits.service has not published status")
  },
  "power limit detection uses the effective PL1"() {
    const power = fixture().power
    assert.ok(!Model.atPowerLimit(power), "43.8 W is below 95% of the effective 55 W")
    assert.ok(Model.atPowerLimit({ ...power, package_w: 53 }))
    assert.ok(Model.atPowerLimit({ ...power, effective_pl1_w: null, mmio: null, package_w: 155 }))
    assert.ok(!Model.atPowerLimit(null))
  },
  "live tiles from the schema 1 fixture"() {
    const status = fixture()
    assert.deepStrictEqual(plain(Model.liveTiles(status)), [
      { value: "43.8 W", label: "Package", alert: "" },
      { value: "77 °C", label: "Temp", alert: "" },
      { value: "11%", label: "Load", alert: "" },
      { value: "5,000", label: "CPU rpm", alert: "" },
    ])
    status.thermal.package_c = 96
    status.power.package_w = 54
    assert.deepStrictEqual(plain(Model.liveTiles(status).map(tile => tile.alert)), ["accent", "urgent", "", ""])
    assert.deepStrictEqual(plain(Model.liveTiles(null).map(tile => tile.value)), ["—", "—", "—", "—"])
    assert.strictEqual(Model.liveTiles({ ...status, fans: [] })[3].label, "Fan")
    assert.strictEqual(Model.grouped(1234567), "1,234,567")
    assert.strictEqual(Model.grouped(999), "999")
  },
  "helper arguments"() {
    const args = Model.helperArgs("quiet", { ...base, custom: true })
    assert.strictEqual(JSON.stringify(args.slice(0, 2)), JSON.stringify(["--set", "quiet"]))
    assert.deepStrictEqual(Object.keys(JSON.parse(args[2])).sort(), ["ceilings_mhz", "pl1_w", "pl2_w", "turbo"])
  },
}
for (const [name, body] of Object.entries(tests)) test(name, body)
