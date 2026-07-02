// Post-build normalizer for the static export (`out/`).
//
// The production build bakes `basePath: "/__CODEG_BASE_PATH__"` into every
// asset/route URL. At serve time the Rust server swaps that placeholder for the
// runtime base path via a plain string `.replace()`. That only works while the
// placeholder is a single contiguous run of bytes.
//
// React serializes the RSC "flight" payload into a sequence of inline scripts:
//
//   <script>self.__next_f.push([1,"...first chunk..."])</script>
//   <script>self.__next_f.push([1,"...next chunk..."])</script>
//
// The browser reassembles the stream by concatenating the type-1 payloads in
// order. When the payload is large, React splits it at a size high-water mark,
// and that split can land *inside* `/__CODEG_BASE_PATH__`:
//
//   ...push([1,"...\"/__CODEG_BA"])</script><script>...push([1,"SE_PATH__/..."])...
//
// The reassembled stream is still correct, but the raw HTML no longer contains
// a contiguous placeholder, so the Rust `.replace()` misses it and the browser
// ends up requesting the literal `/__CODEG_BASE_PATH__/...` path.
//
// This script rebalances adjacent flight chunks so the placeholder is never
// split: when a placeholder straddles a boundary, it moves just enough leading
// characters of the second chunk onto the end of the first. The concatenation
// React reassembles is byte-identical; only the raw-HTML chunk boundary moves.
import { readFileSync, writeFileSync, readdirSync, statSync } from "node:fs"
import { join } from "node:path"

// The exact string the Rust server replaces (SERVER_BASE_PATH_PLACEHOLDER in
// src-tauri/src/web/router.rs) — including the leading slash. Guarding this
// form means a boundary between the "/" and "__CODEG..." is handled too.
export const PLACEHOLDER = "/__CODEG_BASE_PATH__"

// Next/React escape < > & U+2028 U+2029 in inline scripts (htmlEscapeJsonString).
// Build the table without literal separator chars in the source.
const LS = String.fromCharCode(0x2028)
const PS = String.fromCharCode(0x2029)
const HTML_ESCAPE = { "&": "\\u0026", ">": "\\u003e", "<": "\\u003c" }
HTML_ESCAPE[LS] = "\\u2028"
HTML_ESCAPE[PS] = "\\u2029"
const HTML_ESCAPE_RE = new RegExp("[&><" + LS + PS + "]", "g")

// Decoded flight fragment -> safe inline-script string body (no surrounding
// quotes). JSON string escaping, then the inline-script HTML escapes above.
export function encodeBody(decoded) {
  const json = JSON.stringify(decoded)
  const body = json.slice(1, -1)
  return body.replace(HTML_ESCAPE_RE, (c) => HTML_ESCAPE[c])
}

// Index of the closing quote of a string literal opening at `q`, honoring
// backslash escapes.
function findStringEnd(src, q) {
  let i = q + 1
  while (i < src.length) {
    const c = src[i]
    if (c === "\\") {
      i += 2
      continue
    }
    if (c === '"') return i
    i++
  }
  throw new Error("unterminated flight string literal at " + q)
}

// The inline-script prefix React uses for a type-1 flight chunk. If a future
// Next changes this shape, collectPushes stops finding chunks — `verify` turns
// that silent drift into a loud build failure.
const PUSH_MARKER = "self.__next_f.push([1,"

// Every `self.__next_f.push([1,"..."])` literal, in source order.
function collectPushes(html) {
  const marker = PUSH_MARKER
  const lits = []
  let idx = 0
  while ((idx = html.indexOf(marker, idx)) !== -1) {
    // A real type-1 chunk is `push([1,"..."])` — only whitespace may sit
    // between the marker and the opening quote. Anything else means this is
    // not a string chunk (e.g. `push([1,{...}])`); skip it rather than scanning
    // to some far-off, unrelated quote.
    let q = idx + marker.length
    while (q < html.length && (html[q] === " " || html[q] === "\t")) q++
    if (html[q] !== '"') {
      idx = idx + marker.length
      continue
    }
    const end = findStringEnd(html, q)
    const body = html.slice(q + 1, end)
    lits.push({ q, end, decoded: JSON.parse('"' + body + '"') })
    idx = end + 1
  }
  return lits
}

// Concatenated type-1 payloads == the flight stream React reassembles.
export function reassemble(html) {
  return collectPushes(html)
    .map((l) => l.decoded)
    .join("")
}

export function normalizeHtml(html) {
  const lits = collectPushes(html)
  if (lits.length < 2) return { html, changed: false, moves: 0 }

  const dec = lits.map((l) => l.decoded)
  const flight = dec.join("")
  const LEN = PLACEHOLDER.length

  // Placeholder occurrences in the reassembled stream.
  const occ = []
  for (let p = 0; (p = flight.indexOf(PLACEHOLDER, p)) !== -1; p += LEN) {
    occ.push([p, p + LEN])
  }
  if (occ.length === 0) return { html, changed: false, moves: 0 }

  // Chunk boundary offsets in the reassembled stream: bounds[i] is where chunk
  // i ends / chunk i+1 begins.
  const bounds = []
  let acc = 0
  for (let i = 0; i < dec.length - 1; i++) {
    acc += dec[i].length
    bounds.push(acc)
  }

  // Snap any boundary that lands strictly inside a placeholder to that
  // placeholder's end, so the whole token falls into a single chunk. In
  // practice React only ever splits a 20-char placeholder in two (its chunk
  // boundaries are ~KB apart); snapping every boundary inside [s, e) to e is
  // simply the general form and keeps `bounds` non-decreasing.
  let moves = 0
  for (let i = 0; i < bounds.length; i++) {
    for (const [s, e] of occ) {
      if (bounds[i] > s && bounds[i] < e) {
        bounds[i] = e
        moves++
        break
      }
    }
  }
  if (moves === 0) return { html, changed: false, moves: 0 }

  // Re-slice the stream at the snapped boundaries (chunk count is unchanged;
  // some chunks may become empty, which is harmless).
  const next = []
  let prev = 0
  for (let i = 0; i < bounds.length; i++) {
    next.push(flight.slice(prev, bounds[i]))
    prev = bounds[i]
  }
  next.push(flight.slice(prev))

  // Rewrite only the changed literal bodies; splice right-to-left so the
  // original byte offsets stay valid.
  let out = html
  for (let i = lits.length - 1; i >= 0; i--) {
    if (next[i] === dec[i]) continue
    out =
      out.slice(0, lits[i].q + 1) + encodeBody(next[i]) + out.slice(lits[i].end)
  }
  return { html: out, changed: true, moves }
}

// Build-time postcondition. Returns a failure reason, or null if the HTML is
// provably safe to serve. Two independent guards:
//  (a) format drift — the flight marker is present but no chunk parsed, so our
//      assumptions about Next's output no longer hold;
//  (b) a placeholder still straddles a chunk boundary (normalize left work
//      undone) — detected by re-running the normalizer: a clean file needs 0
//      further moves.
export function verify(html) {
  if (html.includes(PUSH_MARKER) && collectPushes(html).length === 0) {
    return "flight marker present but no chunk parsed (Next output format changed?)"
  }
  const again = normalizeHtml(html)
  if (again.changed) {
    return `${again.moves} placeholder(s) still split across a flight boundary`
  }
  return null
}

// ---- CLI: normalize every out/**/*.html in place ----
function* walkHtml(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) yield* walkHtml(p)
    else if (name.endsWith(".html")) yield p
  }
}

function main() {
  const outDir = process.argv[2] || "out"
  let files = 0
  let touched = 0
  let totalMoves = 0
  for (const file of walkHtml(outDir)) {
    files++
    const html = readFileSync(file, "utf8")
    const res = normalizeHtml(html)
    if (res.changed) {
      // Safety net: the reassembled flight stream must be byte-identical.
      if (reassemble(res.html) !== reassemble(html)) {
        console.error(`[flight-basepath] ABORT: reassembly mismatch in ${file}`)
        process.exit(2)
      }
      writeFileSync(file, res.html)
      touched++
      totalMoves += res.moves
      console.log(`[flight-basepath] fixed ${res.moves} split(s) in ${file}`)
    }
    // Postcondition: a split (or unrecognized flight format) must never ship
    // silently — fail the build instead.
    const reason = verify(res.changed ? res.html : html)
    if (reason) {
      console.error(`[flight-basepath] ABORT: ${reason} in ${file}`)
      process.exit(2)
    }
  }
  console.log(
    `[flight-basepath] scanned ${files} html files; normalized ${touched} (${totalMoves} move(s)); all verified contiguous`
  )
}

if (
  process.argv[1] &&
  process.argv[1].endsWith("normalize-flight-basepath.mjs")
) {
  main()
}
