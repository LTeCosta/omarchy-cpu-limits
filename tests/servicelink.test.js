// Tests for the CPU Limits plugin's ServiceLink.js, the registry through
// which widgets reach the service under any bar.
// Run from the plugin directory: node --test
"use strict"
const assert = require("node:assert")
const { test } = require("node:test")
const fs = require("fs")
const path = require("path")
const vm = require("vm")

const source = fs.readFileSync(path.join(__dirname, "../ServiceLink.js"), "utf8").replace(/^\.pragma library$/m, "")

function load() {
  const warnings = []
  const link = { console: { warn: message => warnings.push(message) } }
  vm.runInNewContext(source, link)
  return { link, warnings }
}

test("register, reload and unregister notify every listener", () => {
  const { link } = load()
  const seen = []
  const listener = service => seen.push(service ? service.name : null)
  link.subscribe(listener)
  link.subscribe(listener)
  assert.strictEqual(link.current(), null)

  const first = { name: "first" }
  const second = { name: "second" }
  link.register(first)
  link.register(first)
  assert.strictEqual(link.current(), first)
  link.register(second)
  link.unregister(first)
  assert.strictEqual(link.current(), second, "an old service going away leaves its replacement")
  link.unregister(second)
  assert.strictEqual(link.current(), null)
  link.unregister(second)
  link.register(null)
  assert.deepStrictEqual(seen, ["first", "second", null])
})

test("unsubscribed and failing listeners", () => {
  const { link, warnings } = load()
  const seen = []
  const listener = service => seen.push(service)
  link.subscribe(() => { throw new Error("gone") })
  link.subscribe(listener)
  link.subscribe("not a function")
  const service = {}
  link.register(service)
  assert.deepStrictEqual(seen, [service], "a failing listener does not stop the others")
  assert.strictEqual(warnings.length, 1)
  link.unsubscribe(listener)
  link.unsubscribe(listener)
  link.unregister(service)
  assert.deepStrictEqual(seen, [service])
})
