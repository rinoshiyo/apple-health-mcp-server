/* ============================================================
   Hero scenario rotator for apple-health-mcp-server LP

   Behavior:
   - Reuses the three usecases (card1 / card2 / card3) from i18n
     and rotates them through the Hero conversation bubble pair.
   - Starts only after the Hero visual area is at least 30% in view
     (IntersectionObserver). Stops once the area leaves the viewport
     and resumes when it returns.
   - Honors prefers-reduced-motion: skips the rotation entirely and
     leaves scenario 1 (card1) on display as a static state.
   - Re-syncs when the language is toggled at runtime
     (listens for the "ahmcp:i18n-applied" event dispatched by i18n.js).
   - All literal timings come from tokens.css (--d-rotate-hold,
     --d-rotate-fade); JS reads them via getComputedStyle.
   ============================================================ */

(function () {
  "use strict";

  const SCENARIO_KEYS = [
    { q: "usecases.card1_q", a: "usecases.card1_a_html", meta: "usecases.card1_meta" },
    { q: "usecases.card2_q", a: "usecases.card2_a_html", meta: "usecases.card2_meta" },
    { q: "usecases.card3_q", a: "usecases.card3_a_html", meta: "usecases.card3_meta" },
  ];

  const rotator = document.querySelector("[data-hero-rotator]");
  if (!rotator) return;

  const qEl = rotator.querySelector("[data-rotate-q]");
  const aEl = rotator.querySelector("[data-rotate-a]");
  const metaEl = rotator.querySelector("[data-rotate-meta]");
  if (!qEl || !aEl || !metaEl) return;

  const prefersReduced = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;

  // Read durations from CSS custom properties so JS and CSS stay in sync.
  function readMs(varName, fallbackMs) {
    const raw = getComputedStyle(document.documentElement)
      .getPropertyValue(varName)
      .trim();
    if (!raw) return fallbackMs;
    if (raw.endsWith("ms")) return parseFloat(raw);
    if (raw.endsWith("s")) return parseFloat(raw) * 1000;
    const n = parseFloat(raw);
    return Number.isFinite(n) ? n : fallbackMs;
  }
  const fadeMs = readMs("--d-rotate-fade", 400);
  const holdMs = readMs("--d-rotate-hold", 6000);

  // Always default to scenario index 0 (card1) — what's rendered initially
  // via data-i18n attributes is also card1, so the first apply is a no-op.
  let currentIdx = 0;
  let timerId = null;
  let isVisible = false;

  function resolve(path) {
    const fn = window.AHMCPi18n && window.AHMCPi18n.resolveKey;
    if (typeof fn !== "function") return undefined;
    return fn(path);
  }

  function applyScenario(idx) {
    const k = SCENARIO_KEYS[idx];
    const q = resolve(k.q);
    const a = resolve(k.a);
    const meta = resolve(k.meta);
    if (q !== undefined) qEl.textContent = q;
    if (a !== undefined) aEl.innerHTML = a;
    if (meta !== undefined) metaEl.textContent = meta;
  }

  function fadeSwap(nextIdx) {
    rotator.classList.add("is-fading");
    window.setTimeout(() => {
      applyScenario(nextIdx);
      currentIdx = nextIdx;
      // Force reflow so the removal animates from the faded state.
      // eslint-disable-next-line no-unused-expressions
      rotator.offsetHeight;
      rotator.classList.remove("is-fading");
    }, fadeMs);
  }

  function scheduleNext() {
    if (timerId !== null) return;
    timerId = window.setInterval(() => {
      const next = (currentIdx + 1) % SCENARIO_KEYS.length;
      fadeSwap(next);
    }, holdMs);
  }

  function stop() {
    if (timerId !== null) {
      window.clearInterval(timerId);
      timerId = null;
    }
  }

  // Re-sync on language toggle: refresh the current scenario in the new lang.
  document.addEventListener("ahmcp:i18n-applied", () => {
    applyScenario(currentIdx);
  });

  if (prefersReduced) {
    // Static display: scenario 1 (card1) is already in the DOM via i18n. Done.
    return;
  }

  if (typeof IntersectionObserver === "undefined") {
    // Older browsers: just rotate unconditionally.
    scheduleNext();
    return;
  }

  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting && entry.intersectionRatio >= 0.3) {
          if (!isVisible) {
            isVisible = true;
            scheduleNext();
          }
        } else if (isVisible) {
          isVisible = false;
          stop();
        }
      });
    },
    { threshold: [0, 0.3, 0.6, 1] }
  );
  io.observe(rotator);
})();
