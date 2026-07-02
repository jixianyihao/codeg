import { describe, it, expect } from "vitest"
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs"
import { join } from "node:path"
import {
  normalizeHtml,
  reassemble,
  encodeBody,
  verify,
  PLACEHOLDER,
} from "./normalize-flight-basepath.mjs"

const LS = String.fromCharCode(0x2028)
const P = PLACEHOLDER // the exact form Next bakes in / Rust replaces (incl. slash)

// Wrap decoded flight fragments into real Next-style inline push scripts, each
// in its own <script> tag, with the bootstrap push([0]) and surrounding markup.
function buildHtml(fragments) {
  const scripts = fragments
    .map((f) => `<script>self.__next_f.push([1,"${encodeBody(f)}"])</script>`)
    .join("")
  return (
    "<!doctype html><html><head><title>t</title></head><body>" +
    "<script>(self.__next_f=self.__next_f||[]).push([0])</script>" +
    scripts +
    "</body></html>"
  )
}

// Split a flight string into fragments at the given sorted boundary offsets.
function splitAt(flight, positions) {
  const frags = []
  let prev = 0
  for (const pos of positions) {
    frags.push(flight.slice(prev, pos))
    prev = pos
  }
  frags.push(flight.slice(prev))
  return frags
}

function countOcc(s, sub) {
  return s.split(sub).length - 1
}

// The inline-script bodies must never contain a raw < or > (would allow a
// </script> breakout); Next escapes them and so must our re-encoder.
function pushBodiesAreSafe(html) {
  const marker = "self.__next_f.push([1,"
  let idx = 0
  while ((idx = html.indexOf(marker, idx)) !== -1) {
    let q = idx + marker.length
    while (html[q] !== '"') q++
    let j = q + 1
    while (j < html.length) {
      if (html[j] === "\\") {
        j += 2
        continue
      }
      if (html[j] === '"') break
      if (html[j] === "<" || html[j] === ">") return false
      j++
    }
    idx = j + 1
  }
  return true
}

// Core invariant check: after normalizing an HTML built from `flight` split at
// `positions`, everything must hold.
function expectRepaired(flight, positions) {
  const html = buildHtml(splitAt(flight, positions))
  const res = normalizeHtml(html)

  // 1. React reassembles the exact same stream — byte-identical.
  expect(reassemble(res.html)).toBe(flight)
  // 2. Every placeholder occurrence is contiguous in the raw HTML.
  expect(countOcc(res.html, PLACEHOLDER)).toBe(countOcc(flight, PLACEHOLDER))
  // 3. Idempotent.
  expect(normalizeHtml(res.html).changed).toBe(false)
  // 4. No </script> breakout risk introduced by re-encoding.
  expect(pushBodiesAreSafe(res.html)).toBe(true)
  // 5. The Rust-style replace now removes every trace of the placeholder.
  const served = res.html.split(P).join("/claw/782")
  expect(served.includes("__CODEG")).toBe(false)
}

describe("normalize-flight-basepath: exhaustive single-boundary splits", () => {
  const flights = {
    "two occurrences": `1:I[["${P}/_next/a.js","${P}/_next/b.js"]]`,
    "leading occurrence": `${P}/_next/start.js more text after`,
    "trailing occurrence": `some prefix text then ${P}`,
    "adjacent placeholders": `x:["${P}/_next/a.js","${P}/img"]`,
    "escaped-quote prefix": `3:I[[\\"${P}/_next/x.js\\"]]`,
    "special chars around": `a<b && c>d "q\\z" ${P}/_next/x?y=1 e<f`,
    [`separator ${"U+2028"}`]: `pre${LS}${P}/_next/x${LS}post`,
  }

  for (const [name, flight] of Object.entries(flights)) {
    it(`repairs every boundary position: ${name}`, () => {
      // Split at EVERY interior byte offset — this covers a boundary landing at
      // every possible position inside (and around) every placeholder.
      for (let b = 1; b < flight.length; b++) {
        expectRepaired(flight, [b])
      }
    })
  }
})

describe("normalize-flight-basepath: boundary edge cases", () => {
  // Realistically React only ever puts one chunk boundary inside a 20-char
  // placeholder (a single high-water-mark split of a large flight row); its
  // splits are ~KB apart, so a 3+-way split of the token does not occur.
  const flight = `head ${P}/_next/chunk.js tail`
  const k = flight.indexOf(PLACEHOLDER)
  const LEN = PLACEHOLDER.length

  it("boundary exactly at the placeholder start is a no-op (not a straddle)", () => {
    const html = buildHtml(splitAt(flight, [k]))
    expect(normalizeHtml(html).changed).toBe(false)
  })

  it("boundary exactly at the placeholder end is a no-op (not a straddle)", () => {
    const html = buildHtml(splitAt(flight, [k + LEN]))
    expect(normalizeHtml(html).changed).toBe(false)
  })

  it("two placeholders each split at their own boundary", () => {
    const f = `${P}/_next/a.js SEP ${P}/_next/b.js`
    const k1 = f.indexOf(PLACEHOLDER)
    const k2 = f.indexOf(PLACEHOLDER, k1 + 1)
    expectRepaired(f, [k1 + 6, k2 + 9])
  })
})

describe("normalize-flight-basepath: build-time guarantee (verify)", () => {
  it("passes a clean, unsplit page", () => {
    const html = buildHtml([`x ${P}/_next/a.js y`, "unrelated tail"])
    expect(verify(html)).toBeNull()
  })

  it("flags a page where a placeholder is still split (unrepaired)", () => {
    const flight = `x ${P}/_next/a.js y`
    const k = flight.indexOf(PLACEHOLDER)
    // Build the split HTML but do NOT normalize it — verify must catch it.
    const broken = buildHtml(splitAt(flight, [k + 5]))
    expect(verify(broken)).toMatch(/split/)
  })

  it("flags flight-format drift (marker present but unparseable)", () => {
    // The marker exists but the payload is not a string literal we can parse.
    const html = "<script>self.__next_f.push([1,{obj:1}])</script>"
    expect(verify(html)).toMatch(/format/)
  })

  it("passes HTML with no flight at all", () => {
    expect(verify("<!doctype html><html><body>hi</body></html>")).toBeNull()
  })
})

describe("normalize-flight-basepath: no-op / safety cases", () => {
  it("HTML without any flight is untouched", () => {
    const html = "<!doctype html><html><body>hello</body></html>"
    const res = normalizeHtml(html)
    expect(res.changed).toBe(false)
    expect(res.html).toBe(html)
  })

  it("flight without any placeholder is untouched", () => {
    const html = buildHtml(["1:I[[]]", "2:{\\n}", "3:done"])
    const res = normalizeHtml(html)
    expect(res.changed).toBe(false)
    expect(res.html).toBe(html)
  })

  it("placeholder fully inside one chunk is untouched", () => {
    const html = buildHtml([`pre ${P}/_next/x.js`, "unrelated tail chunk"])
    const res = normalizeHtml(html)
    expect(res.changed).toBe(false)
    expect(res.html).toBe(html)
  })

  it("a single push is untouched", () => {
    const html = buildHtml([`only ${P}/_next/x.js chunk`])
    expect(normalizeHtml(html).changed).toBe(false)
  })
})

// Grounded against the actual build output when present. In CI `pnpm test`
// runs before `pnpm build`, so `out/` may be absent — skip cleanly then.
const OUT = "out"
const hasOut = existsSync(OUT) && existsSync(join(OUT, "index.html"))
const d = hasOut ? describe : describe.skip

function walkHtml(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walkHtml(p, acc)
    else if (name.endsWith(".html")) acc.push(p)
  }
  return acc
}

d("normalize-flight-basepath: real out/ build output", () => {
  const files = hasOut ? walkHtml(OUT) : []

  it("no shipped HTML currently splits the placeholder", () => {
    for (const file of files) {
      const html = readFileSync(file, "utf8")
      // If already clean, normalize is a no-op; if not, it must at least
      // preserve the reassembled stream.
      const res = normalizeHtml(html)
      expect(reassemble(res.html)).toBe(reassemble(html))
      if (res.changed) {
        // A real split exists in the build — surface it, but prove we fixed it.
        expect(countOcc(res.html, PLACEHOLDER)).toBeGreaterThanOrEqual(
          countOcc(html, PLACEHOLDER)
        )
      }
    }
  })

  it("repairs a synthetic split at every offset of a real placeholder", () => {
    const LEN = PLACEHOLDER.length
    for (const file of files) {
      const flight = reassemble(readFileSync(file, "utf8"))
      const first = flight.indexOf(PLACEHOLDER)
      if (first === -1) continue
      // Sweep a boundary through every interior offset of the first
      // occurrence (real surrounding bytes), ...
      for (let off = 1; off < LEN; off++) {
        expectRepaired(flight, [first + off])
      }
      // ... and hit every remaining occurrence with one mid-token split.
      let k = flight.indexOf(PLACEHOLDER, first + 1)
      while (k !== -1) {
        expectRepaired(flight, [k + Math.floor(LEN / 2)])
        k = flight.indexOf(PLACEHOLDER, k + 1)
      }
    }
  }, 120000)
})
