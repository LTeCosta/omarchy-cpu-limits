// Pure state and formatting rules for the CPU Limits panel. QML files bind to
// these functions so tests/model.test.js can exercise them in node.
.pragma library

var STATUS_SCHEMA = 1
// Used only when the status does not carry its own `limits`.
var DEFAULT_LIMITS = { step_mhz: 100, power_step_w: 5, stale_after_s: 10, min_w: 15, max_w: 215 }
var REQUIRED_FIELDS = ["schema", "updated", "profile", "profiles", "groups", "limits", "thermal"]
var PROFILE_LABELS = { quiet: "Quiet", balanced: "Balanced", performance: "Performance",
  "low-power": "Low power", "balanced-performance": "Balanced performance", "default": "Default" }

function isObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value) }

function present(value) { return value !== null && value !== undefined }

function unavailable(error) { return { status: null, stale: true, error: error, warnings: [] } }

function limit(status, key) {
  var value = status && isObject(status.limits) ? status.limits[key] : undefined
  return typeof value === "number" ? value : DEFAULT_LIMITS[key]
}

function parseStatus(text, nowSeconds) {
  if (!text) return unavailable("cpu-frequency-limits.service has not published status")
  var status
  try {
    status = JSON.parse(text)
  } catch (error) {
    return unavailable("Unreadable CPU status")
  }
  if (!isObject(status)) return unavailable("Unreadable CPU status")
  // A status without a schema comes from an older service: a version mismatch
  // rather than a merely incomplete file.
  if (status.schema !== STATUS_SCHEMA)
    return unavailable("The CPU Limits panel and service versions differ; restart the shell (omarchy-restart-shell) or the service")
  for (var i = 0; i < REQUIRED_FIELDS.length; i++)
    if (!present(status[REQUIRED_FIELDS[i]]))
      // A service that failed hardware discovery publishes only its error; once
      // that status is stale, the service is gone and the error is history.
      return unavailable(typeof status.updated === "number"
          && !(status.updated > nowSeconds - limit(status, "stale_after_s"))
        ? "cpu-frequency-limits.service is not running"
        : (typeof status.error === "string" && status.error !== ""
          ? status.error : "Incomplete CPU status (no " + REQUIRED_FIELDS[i] + ")"))
  if (typeof status.updated !== "number" || !isObject(status.profiles) || !Array.isArray(status.groups)
      || !isObject(status.limits) || !isObject(status.thermal))
    return unavailable("Incomplete CPU status")
  var warnings = Array.isArray(status.warnings)
    ? status.warnings.filter(function(w) { return typeof w === "string" && w !== "" }) : []
  var stale = !(status.updated > nowSeconds - limit(status, "stale_after_s"))
  return { status: status, stale: stale, warnings: warnings,
    error: stale ? "cpu-frequency-limits.service is not running" : (status.error || "") }
}

function profileLabel(name) {
  var label = PROFILE_LABELS[name]
  return label ? label : String(name || "").replace(/(^|-)([a-z])/g, function(_m, dash, c) { return (dash ? " " : "") + c.toUpperCase() })
}

function settingsOf(profile) {
  if (!profile) return null
  var ceilings = {}
  for (var key in profile.ceilings_mhz) ceilings[key] = profile.ceilings_mhz[key]
  return { ceilings_mhz: ceilings, turbo: profile.turbo === true,
    pl1_w: profile.pl1_w === undefined ? null : profile.pl1_w,
    pl2_w: profile.pl2_w === undefined ? null : profile.pl2_w }
}

function sameSettings(a, b) {
  if (!a || !b) return a === b
  if (a.turbo !== b.turbo || a.pl1_w !== b.pl1_w || a.pl2_w !== b.pl2_w) return false
  var keys = Object.keys(a.ceilings_mhz)
  if (keys.length !== Object.keys(b.ceilings_mhz).length) return false
  for (var i = 0; i < keys.length; i++)
    if (a.ceilings_mhz[keys[i]] !== b.ceilings_mhz[keys[i]]) return false
  return true
}

// ---------- Drafts: { profile: settings } of unsaved edits ----------

function copyDrafts(drafts, skip) {
  var copy = {}
  for (var key in drafts) if (key !== skip) copy[key] = drafts[key]
  return copy
}

// An edit back to the saved value is no longer a pending change.
function withDraft(drafts, profile, settings, saved) {
  var copy = copyDrafts(drafts, profile)
  if (!sameSettings(settings, saved)) copy[profile] = settings
  return copy
}

function withoutDraft(drafts, profile) { return copyDrafts(drafts, profile) }

// The helper saved `target`; "all" saved the same settings to every profile.
function afterSave(drafts, target) { return target === "all" ? {} : withoutDraft(drafts, target) }

function otherDraftCount(drafts, profile) { return Object.keys(copyDrafts(drafts, profile)).length }

// Drops drafts that now match what the service saved, or whose profile is gone.
function pruneDrafts(drafts, statusProfiles) {
  var copy = {}
  for (var key in drafts) {
    var profile = statusProfiles ? statusProfiles[key] : null
    if (profile && !sameSettings(drafts[key], settingsOf(profile))) copy[key] = drafts[key]
  }
  return copy
}

// pkexec: 126 = the authentication dialog was dismissed, 127 = not authorized
// or authentication failed (both also write to stderr); 1 = the helper refused.
// stdoutText is the helper's JSON answer; `superseded` means a later save
// replaced this one before the service applied it.
function helperOutcome(exitCode, stderrText, target, allCount, stdoutText) {
  if (exitCode === 0) {
    var answer = null
    try { answer = JSON.parse(String(stdoutText || "")) } catch (error) { answer = null }
    if (isObject(answer) && answer.superseded === true)
      return { ok: true, message: "Saved; a later change replaced it before it was applied" }
    if (target !== "all") return { ok: true, message: "Applied to " + profileLabel(target) }
    var discarded = allCount > 0
      ? "; unsaved changes in " + allCount + (allCount === 1 ? " other profile were" : " other profiles were") + " discarded"
      : ""
    return { ok: true, message: "Applied to every profile" + discarded }
  }
  if (exitCode === 126) return { ok: false, message: "Authentication was cancelled" }
  if (exitCode === 127) return { ok: false, message: "Not authorized to change CPU limits" }
  var lines = String(stderrText || "").split("\n").map(function(l) { return l.trim() })
    .filter(function(l) { return l !== "" })
  return { ok: false, message: lines.length ? lines[lines.length - 1] : "The change could not be applied" }
}

// ---------- Editing ----------

function clamp(value, low, high) { return Math.max(low, Math.min(high, value)) }

function stepCeiling(settings, group, direction, status) {
  var next = settingsOf(settings)
  var value = next.ceilings_mhz[group.id] + direction * limit(status, "step_mhz")
  next.ceilings_mhz[group.id] = clamp(value, group.min_mhz, group.max_mhz)
  return next
}

// The firmware value the service observed for `profile`, or null.
function firmwareFor(status, profile, key) {
  var entry = status && status.profiles ? status.profiles[profile] : null
  var value = entry ? entry["firmware_" + key] : null
  return present(value) ? value : null
}

// What a power limit row shows: the explicit value, else that profile's
// firmware value, else the value now in the MSR.
function startValue(status, profile, settings, key) {
  if (settings && present(settings[key])) return settings[key]
  var firmware = firmwareFor(status, profile, key)
  if (firmware !== null) return firmware
  return status && status.power && present(status.power[key]) ? status.power[key] : null
}

function otherPowerKey(key) { return key === "pl1_w" ? "pl2_w" : "pl1_w" }

// A firmware-controlled limit starts stepping from the firmware value.
// PL2 never drops below PL1: moving one past the other carries the other along.
function stepPower(settings, key, direction, status, profile) {
  var next = settingsOf(settings)
  var step = limit(status, "power_step_w")
  var low = limit(status, "min_w")
  var current = startValue(status, profile, next, key)
  if (current === null) current = low
  var value = clamp(Math.round((current + direction * step) / step) * step, low, limit(status, "max_w"))
  next[key] = value
  var otherKey = otherPowerKey(key)
  var other = startValue(status, profile, next, otherKey)
  if (other !== null && (key === "pl1_w" ? other < value : other > value)) next[otherKey] = value
  return next
}

// Returns `key` to firmware control. When the firmware value and the other,
// still explicit, limit would leave PL2 below PL1, the other limit returns to
// firmware control as well.
function useFirmware(settings, key, status, profile) {
  var next = settingsOf(settings)
  next[key] = null
  var otherKey = otherPowerKey(key)
  if (!present(next[otherKey])) return next
  var firmware = firmwareFor(status, profile, key)
  if (firmware === null && status && status.power && present(status.power[key])) firmware = status.power[key]
  if (firmware === null) return next
  var pl1 = key === "pl1_w" ? firmware : next.pl1_w
  var pl2 = key === "pl2_w" ? firmware : next.pl2_w
  if (pl2 < pl1) next[otherKey] = null
  return next
}

// ---------- Formatting ----------

function ghz(mhz) {
  if (mhz === null || mhz === undefined) return "—"
  return (mhz / 1000).toFixed(2).replace(/0$/, "") + " GHz"
}

// Thousands separators without relying on the engine's locale support.
function grouped(value) { return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ",") }

function mhzText(mhz) { return mhz === null || mhz === undefined ? "—" : mhz + " MHz" }

function wattsText(watts) {
  if (watts === null || watts === undefined) return "—"
  return (watts >= 100 ? Math.round(watts) : Math.round(watts * 10) / 10) + " W"
}

function groupCaption(group) {
  var parts = []
  if (group.avg_mhz) parts.push("now " + ghz(group.avg_mhz) + " avg")
  if (group.peak_mhz) parts.push(ghz(group.peak_mhz) + " peak")
  return parts.join(" · ")
}

function groupDetail(group) {
  var count = group.cpus ? group.cpus.length : 0
  return count + (count === 1 ? " thread" : " threads") + " · max " + ghz(group.max_mhz)
}

// The panel edits the MSR limit; thermald may hold a lower MMIO one.
function powerCaption(status, key) {
  var power = status ? status.power : null
  if (!power || !power.mmio || !present(power.mmio[key])) return ""
  return "effective " + wattsText(power["effective_" + key]) + " · MMIO " + wattsText(power.mmio[key]) + " (thermald)"
}

function effectivePower(power, key) {
  if (!power) return null
  var effective = power["effective_" + key]
  return present(effective) ? effective : power[key]
}

function barKind(parsed) {
  if (!parsed.status || parsed.stale || parsed.error) return "error"
  if (parsed.status.thermal && parsed.status.thermal.throttling) return "throttling"
  return "ok"
}

function heroMeta(parsed) {
  if (!parsed.status) return "Unavailable"
  if (parsed.stale) return "Service stopped"
  var status = parsed.status
  var active = status.profiles && status.profiles[status.profile]
  return profileLabel(status.profile) + " · " + (status.error ? "divergent" : (active && active.custom ? "custom limits" : "defaults"))
}

function throttleText(thermal) {
  if (!thermal) return ""
  var count = thermal.throttle_count
  var since = present(count) ? " · " + grouped(count) + " events since boot" : ""
  if (!thermal.throttling) return "No sustained thermal throttling" + since
  var pct = thermal.throttle_pct
  return (present(pct) ? "Thermal throttling " + Math.round(pct * 10) / 10 + "% of the time" : "Thermal throttling now") + since
}

function atPowerLimit(power) {
  var pl1 = effectivePower(power, "pl1_w")
  return !!power && present(power.package_w) && pl1 > 0 && power.package_w >= pl1 * 0.95
}

// The four live reading tiles; `alert` is "", "accent" or "urgent".
function liveTiles(status) {
  var power = status ? status.power : null
  var thermal = status ? status.thermal : null
  var fan = status && status.fans && status.fans.length ? status.fans[0] : null
  var hot = !!thermal && thermal.crit_c > 0 && present(thermal.package_c) && thermal.package_c >= thermal.crit_c - 5
  return [
    { value: wattsText(power ? power.package_w : null), label: "Package", alert: atPowerLimit(power) ? "accent" : "" },
    { value: thermal && present(thermal.package_c) ? thermal.package_c + " °C" : "—", label: "Temp", alert: hot ? "urgent" : "" },
    { value: status && present(status.load_pct) ? status.load_pct + "%" : "—", label: "Load", alert: "" },
    { value: fan && present(fan.rpm) ? grouped(fan.rpm) : "—", label: fan ? fan.label + " rpm" : "Fan", alert: "" }
  ]
}

function tooltip(parsed) {
  if (!parsed.status) return "CPU Limits\n" + parsed.error
  var status = parsed.status
  var lines = ["CPU Limits · " + heroMeta(parsed)]
  var live = []
  if (status.thermal && present(status.thermal.package_c)) live.push(status.thermal.package_c + " °C")
  if (status.power && present(status.power.package_w)) live.push(wattsText(status.power.package_w))
  if (present(status.load_pct)) live.push(status.load_pct + "% load")
  if (live.length) lines.push(live.join(" · "))
  var ceilings = []
  for (var i = 0; i < (status.groups || []).length; i++) {
    var group = status.groups[i]
    ceilings.push(group.label + " " + (group.ceiling_mhz || []).join("/") + " MHz")
  }
  if (ceilings.length) lines.push(ceilings.join("\n"))
  if (status.power) lines.push("PL1 " + wattsText(effectivePower(status.power, "pl1_w"))
    + " · PL2 " + wattsText(effectivePower(status.power, "pl2_w"))
    + (status.power.mmio ? " effective" : "") + (status.turbo ? "" : " · turbo off"))
  if (parsed.error) lines.push(parsed.error)
  return lines.join("\n")
}

function barLabel(parsed, mode) {
  if (!parsed.status || parsed.stale) return ""
  if (mode === "temperature" && parsed.status.thermal && present(parsed.status.thermal.package_c))
    return parsed.status.thermal.package_c + "°"
  if (mode === "power" && parsed.status.power && present(parsed.status.power.package_w))
    return Math.round(parsed.status.power.package_w) + "W"
  return ""
}

function helperArgs(profile, settings) {
  return ["--set", profile, JSON.stringify(settingsOf(settings))]
}
