// Builds docs/vision/Colloid_Architecture_and_Vision.docx.
//   node docs/vision/build_architecture_doc.js
// Every number in this document traces to a file in the repository (see Appendix B).
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, Header, Footer, AlignmentType,
  LevelFormat, HeadingLevel, BorderStyle, WidthType, ShadingType, PageNumber, PageBreak,
} = require("docx");

const W = 9026; // A4 content width with 1" margins (DXA)
const ACCENT = "1F3A5F";
const MUTED = "5A6472";
const FILL = "EEF2F7";

// ---------- helpers
function runs(text, base = {}) {
  // **bold** and `code` markup
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), ...base }));
    const t = m[0];
    if (t.startsWith("**")) out.push(new TextRun({ text: t.slice(2, -2), bold: true, ...base }));
    else out.push(new TextRun({ text: t.slice(1, -1), font: "Consolas", size: 20, ...base }));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), ...base }));
  return out;
}
const p = (text, opts = {}) => new Paragraph({ children: runs(text, opts.run || {}), spacing: { after: 120, line: 276 }, ...opts.para });
const h1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)], pageBreakBefore: true });
const h2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });
const h3 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_3, children: [new TextRun(t)] });
const bullet = (t, level = 0) => new Paragraph({ numbering: { reference: "bullets", level }, children: runs(t), spacing: { after: 60, line: 264 } });
const num = (t, ref = "steps", level = 0) => new Paragraph({ numbering: { reference: ref, level }, children: runs(t), spacing: { after: 60, line: 264 } });

const border = { style: BorderStyle.SINGLE, size: 4, color: "C9D1DC" };
const borders = { top: border, bottom: border, left: border, right: border };
function table(headers, rows, widths) {
  const sum = widths.reduce((a, b) => a + b, 0);
  if (sum !== W) widths = widths.map((w) => Math.floor((w * W) / sum)), widths[widths.length - 1] += W - widths.reduce((a, b) => a + b, 0);
  const cell = (text, i, head) => new TableCell({
    borders, width: { size: widths[i], type: WidthType.DXA },
    shading: head ? { fill: ACCENT, type: ShadingType.CLEAR, color: "auto" } : undefined,
    margins: { top: 70, bottom: 70, left: 110, right: 110 },
    children: String(text).split("\n").map((line) => new Paragraph({
      children: runs(line, head ? { bold: true, color: "FFFFFF", size: 19 } : { size: 19 }), spacing: { after: 40 } })),
  });
  return new Table({
    width: { size: W, type: WidthType.DXA }, columnWidths: widths,
    rows: [new TableRow({ tableHeader: true, cantSplit: true, children: headers.map((h, i) => cell(h, i, true)) }),
      ...rows.map((r) => new TableRow({ cantSplit: true, children: r.map((c, i) => cell(c, i, false)) }))],
  });
}
function callout(title, lines) {
  return new Table({
    width: { size: W, type: WidthType.DXA }, columnWidths: [W],
    rows: [new TableRow({ children: [new TableCell({
      width: { size: W, type: WidthType.DXA },
      borders: { top: { style: BorderStyle.SINGLE, size: 4, color: ACCENT }, bottom: { style: BorderStyle.SINGLE, size: 4, color: ACCENT },
        left: { style: BorderStyle.SINGLE, size: 24, color: ACCENT }, right: { style: BorderStyle.SINGLE, size: 4, color: ACCENT } },
      shading: { fill: FILL, type: ShadingType.CLEAR, color: "auto" },
      margins: { top: 120, bottom: 120, left: 200, right: 200 },
      children: [new Paragraph({ children: [new TextRun({ text: title, bold: true, color: ACCENT })], spacing: { after: 80 } }),
        ...lines.map((l) => new Paragraph({ children: runs(l), spacing: { after: 60, line: 264 } }))],
    })] })],
  });
}
const gap = () => new Paragraph({ children: [], spacing: { after: 120 } });

// ---------- content
const title = [
  new Paragraph({ children: [], spacing: { before: 2600 } }),
  new Paragraph({ children: [new TextRun({ text: "COLLOID", bold: true, size: 72, color: ACCENT, characterSpacing: 40 })] }),
  new Paragraph({ children: [new TextRun({ text: "Architecture, evidence, and the road to a proprietary, verified full stack", size: 34, color: "333333" })], spacing: { after: 360 } }),
  new Paragraph({ border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: ACCENT, space: 4 } }, children: [] }),
  new Paragraph({ children: [new TextRun({ text: "Prepared for the owner · 3 October 2026 · version 1", color: MUTED, size: 22 })], spacing: { before: 240, after: 80 } }),
  new Paragraph({ children: [new TextRun({ text: "Describes the engine as built on branches ccr-00675f8e-w4745o (PR #2) and bench/swebench (PR #3) of muthu2201/Mutation-engine-. Every number traces to a file in the repository; Appendix B says where.", color: MUTED, size: 20 })], spacing: { after: 80 } }),
  new Paragraph({ children: [new TextRun({ text: "Confidential: describes proprietary methods and data.", color: MUTED, size: 20, italics: true })] }),
];

const toc = [
  new Paragraph({ children: [new PageBreak()] }),
  new Paragraph({ children: [new TextRun({ text: "Contents", bold: true, size: 32, color: ACCENT })], spacing: { after: 200 } }),
  ...["1. Summary", "2. The goal, stated precisely", "3. Architecture overview", "4. The parts, and what each does",
    "5. The self-evolution loop", "6. Verification: what is proved today, and the road to mathematical proof",
    "7. Bare metal to software, silicon-agnostic", "8. Evidence to date", "9. Resilience: how the architecture survives its own mistakes",
    "10. The proprietary data moat", "11. Roadmap, gated by evidence", "12. How we work together",
    "Appendix A. Glossary", "Appendix B. Where everything lives"].map((t) => new Paragraph({ children: [new TextRun({ text: t, size: 24 })], spacing: { after: 110 } })),
];

const s1 = [
  h1("1. Summary"),
  p("**Colloid is an evolutionary engine that changes real software and keeps a change only when an independent judge proves it.** It mutates code, configuration, database design, runtime and compiler settings, and operating-system parameters. A change counts only when a judge the engine cannot influence shows it correct and measures it faster or cheaper with calibrated statistical confidence."),
  p("Every change that survives becomes a record, with its complete evidence, in a tamper-evident, append-only **mutation lake**. Those records are the proprietary asset. They:"),
  bullet("seed later searches;"),
  bullet("shape which mutation strategies the engine favours;"),
  bullet("are distilled into rules of Colloid's own language, CRL."),
  p("**The long-term aim** is to grow our own stack from that verified evidence: language, compiler, runtime and services, machine-optimised from silicon to application, carrying proof of every claim it makes."),
  gap(),
  table(["", "Where we stand (3 October 2026)"], [
    ["Built", "A hexagonal engine with a pure core. A separate, frozen judge with seven stages (L0–L6). The hash-chained mutation lake. CRL v0. Four implementations of one reference system (Python + C, Go, TypeScript on Node and Bun). A track that repairs real open-source issues (SWE-bench Verified)."],
    ["Best verified result", "**−30.9% cost per request** on the reference stack (95% CI 27.3–34.5%), and −26.6% on a hidden holdout workload. The gain came from two database indexes. The verified stack ships as `stack/stackzero-verified` (−31.3%, CI 28.2–34.1%)."],
    ["Honest limits", "Knowledge transfer between runs (milestone M1b) has **not** yet passed its pre-registered test. Small local models resolved **2 of 30** real GitHub issues, and both issues stated their own fix. Verification is statistical and differential today, not yet formal proof."],
    ["Running now", "Kimi K3 (on NVIDIA's endpoints), driven by the same engine on the same 30 issues, under a pre-registered protocol. Its pilot resolved its first practice instance."],
  ], [2200, 6826]),
];

const s2 = [
  h1("2. The goal, stated precisely"),
  callout("In the owner's words", ["Truth and efficiency. Continuous self-mutation and evolution. Our own full stack, from bare metal and silicon to software, with no bloat and with mathematically verified, proof-based evaluation. The findings modify the core engine that builds the novel stack. The architecture must be absolutely resilient and keep pushing the boundary of what it can achieve. And it stays proprietary: the data is the moat."]),
  gap(),
  p("The owner's vision, written as eight principles. Each one maps onto a mechanism that exists today or onto a gated step on the roadmap (section 11)."),
  table(["Principle", "What it means in Colloid", "Mechanism"], [
    ["1. Truth first", "Nothing counts until an independent judge has verified it. Every claim carries its evidence, and negative results are kept and published.", "Frozen judge, pre-registration, the evidence ladder, hash-chained records."],
    ["2. Efficiency without bloat", "Every component must earn its place. A change that contributes nothing measurable is removed, even if it rode along with a winner.", "Shapley attribution, leave-one-out ablation, hitchhiker pruning, a parsimony tie-break."],
    ["3. Continuous self-mutation and evolution", "The engine never stops searching, and each run starts from everything verified before it.", "Islands, MAP-Elites, lake seeds, bandit priors."],
    ["4. Findings modify the engine that builds the stack", "A closed loop: the engine finds verified changes, the lake keeps them, rules and priors come out of them, and those reshape both the engine and the stack it produces.", "The self-improvement loop (section 5) and reviewed method changes (ADRs)."],
    ["5. Micro to macro", "Mutations at every scale: one knob value or one line, up to whole cross-layer compositions and swaps of implementation or language.", "The gene → genome → splice → implementation → rule hierarchy (section 4.3)."],
    ["6. Bare metal to software, silicon-agnostic", "Every layer is a mutation surface. Hardware is a recorded variable, never a hidden assumption, and knowledge is named by meaning, not by machine.", "Atlas layers, environment fingerprints, a language-neutral mutation format."],
    ["7. Mathematical verification", "Statistical proof of effect and differential proof of behaviour today. Formal proof of equivalence next, wherever it is decidable.", "Calibrated statistics, oracles, fuzzing, canaries; next, proof-carrying rules (section 6)."],
    ["8. A proprietary moat", "Verified, provenance-carrying data, the rules distilled from it, and the stack built from them, all kept private.", "The lake, CRL, data-only branches, data-protection rules (section 10)."],
  ], [2000, 4300, 2726]),
  gap(),
  h2("What has to be true for this to be worth billions"),
  p("Ambition is only as good as the conditions that would prove it wrong. Each condition below is measured by a gate that can fail."),
  table(["Condition", "Gate that tests it", "Status"], [
    ["Knowledge compounds: a lake-primed run beats a cold run.", "M1b: verified gain per hour at least 1.25× the cold run's (pre-registered, ADR 0008).", "**Not passed** (14.9 vs 19.9). The test was confounded (unequal request rates, a soak-rule flaw). A re-test is designed."],
    ["Rules generalise to systems they were not mined from.", "M2: a mined rule reproduces a verified gain on a held-out schema.", "Not yet run. 2 rules mined (CRL v0)."],
    ["Verified gains are large and common on third-party systems.", "Repair: SWE-bench (running). Optimisation: SWE-fficiency (ADR 0013, proposed).", "Repair 2/30 with small models; the hosted-model arm is running."],
    ["The judge stays unfoolable as the search gets stronger.", "100% canary rejection before every run; a red-team island; a frozen judge.", "Holding: 17/17 Python and 14/14 Go canaries."],
    ["The data stays proprietary.", "Local models for proprietary code; hosted providers' terms verified first.", "Policy in place; provider terms are an open research question."],
  ], [2900, 3426, 2700]),
];

const s3 = [
  h1("3. Architecture overview"),
  p("Colloid follows one hard lesson from 2023–2026 (AlphaEvolve, FunSearch, the Darwin Gödel Machine, and public failures such as inflated KernelBench numbers): **LLM-guided evolution produces real gains only when a fast, trustworthy, automatic evaluator exists.** With a weak evaluator it produces fake gains or none. So the evaluator is the centre of the system, and the language model is a swappable part."),
  table(["Package", "Role", "Key property"], [
    ["`colloid/core`", "The pure domain: Atlas, genomes, operators, selection, islands, bandit, attribution, splicing, statistics, lake, CRL rules.", "No I/O, no SDKs, reproducible from a seed. Purity is enforced by import-linter contracts."],
    ["`colloid/ports`", "Versioned protocol contracts the core talks through (LLM, code representation, build, sandbox, store, target).", "The core knows only these contracts and value objects."],
    ["`colloid/adapters`", "Concrete implementations: local llama.cpp, OpenRouter and NVIDIA providers, Linux sandbox, stores, targets, the lake on a git branch.", "A new model, language or database is a new adapter, not a rewrite."],
    ["`colloid/services`", "The engine loop, CLI, dashboard, reports, the SWE-bench runner, lake and stack services.", "Wires adapters to the core."],
    ["`colloid_evaluator`", "The judge: the L0–L6 cascade, oracles, policy scanners, the benchmark protocol, canaries, SWE-bench judge and grader bridge.", "A separate package the search can never import or edit (ADR 0001, ADR 0006)."],
  ], [2600, 3800, 2626]),
  gap(),
  p("**Why this shape.** The code that proposes changes can never influence how they are judged. Everything around the judge can be swapped: models, languages, databases, platforms."),
];

const s4 = [
  h1("4. The parts, and what each does"),
  h2("4.1 The Stack Atlas: dissecting a system"),
  p("The Atlas is a multi-resolution graph of the target system. Its levels run from layer to component, module, function and region, and knobs hang off the components they configure. It answers three questions."),
  bullet("**Where can we mutate?** A mutable unit exposes **loci**: a unit plus a surface such as a code region, a knob, an index set or compiler flags. Tests, oracles and frozen knobs such as `fsync` are not loci, so editing tests or trading away durability is impossible by construction."),
  bullet("**Where is the leverage?** The Atlas decorates units with hotness, latency share, dollar share and **causal leverage**: if this unit were x% faster, how much faster would the whole workload be? Search effort follows leverage, not raw hotness."),
  bullet("**What interacts with what?** Call, query and configuration edges, plus request paths that cross layers, predict which changes are likely to interact."),
  p("On the reference stack the Atlas holds 172 units, 7 request paths crossing up to 3 layers, and 106 loci."),
  h2("4.2 Genes and genomes"),
  p("A **gene** is one change at one site: a knob value or a source replacement. A **genome** is a sparse set of genes on top of a fixed baseline. Genomes from different layers compose by set union when their loci do not overlap, which makes \"all layers at once\" tractable. Everything is content-addressed: identical proposals share one id, and every program can be rebuilt from its id and stored payloads."),
  h2("4.3 Micro, meso, macro and meta mutations"),
  table(["Scale", "What changes", "How Colloid does it today"], [
    ["Micro", "One value, one line, one function, one index.", "Knob operators (sample, perturb, reset). Peephole AST rewrites. Genetic-improvement line edits. LLM rewrite of one function or block. One database index."],
    ["Meso", "Several genes in one region of the system.", "Genomes evolved on a region's island; crossover between parents."],
    ["Macro", "Whole-system compositions and structural choices.", "Cross-layer **splicing** on the Composition Island: a fractional-factorial screen and an epistasis-aware surrogate assemble winners from different layers. Implementation-level choices: allocator, runtime, and a whole-language port measured in the bake-off."],
    ["Rule", "A pattern that holds across systems.", "CRL rules mined from verified genes propose changes on any implementation that matches."],
    ["Meta", "The searcher itself.", "Bandit priors, transfer seeds and rule arms come automatically from the lake. Method lessons become reviewed code, recorded in an ADR."],
  ], [1300, 2700, 5026]),
  h2("4.4 Islands, MAP-Elites and escaping local optima"),
  p("Each region of the system (OS, allocator, compiler, runtime, database configuration, indexes, service code, native code) has its own island. Each island keeps a **MAP-Elites** grid, which holds the best program per behaviour cell, not one global best. The search escapes local optima with:"),
  bullet("simulated-annealing acceptance, reheated on stagnation;"),
  bullet("neutral drift across plateaus;"),
  bullet("tabu basins around converged solutions;"),
  bullet("NSGA-II stepping-stone preservation;"),
  bullet("ring migration between islands."),
  p("A **red-team island** has the inverted goal of fooling the judge. Anything that survives it becomes a new canary."),
  h2("4.5 Operators and models"),
  p("Operators are pure functions from a parent to proposals; only the LLM call happens outside the core. An LLM rewrite gets a localised prompt:"),
  bullet("the unit's source and its causal leverage;"),
  bullet("the request paths it sits on;"),
  bullet("archive neighbours, with their measured gains;"),
  bullet("summaries of failed attempts."),
  p("The response must be a drop-in replacement, or it is rejected before any compute is spent. Models are bandit arms, so swapping one is a configuration change. In use: local Qwen2.5-Coder 1.5B, 3B and 7B; hosted Kimi K3 (NVIDIA); Qwen3.8 (OpenRouter)."),
  h2("4.6 Bandit and budget"),
  p("A cost-aware **Thompson-sampling bandit** picks the (operator, model, prompt) arm for each locus. A **budget** spreads proposals across islands in proportion to measured leverage and recent progress, with an exploration floor."),
  p("One finding from the SWE-bench run: cost-awareness starved the strongest local model. The next protocol routes first proposals to the largest model."),
  h2("4.7 The judge: a seven-stage cascade"),
  table(["Stage", "What it checks", "Cost"], [
    ["L0", "Static policy: locus validity, knob ranges, diff caps, and an AST/semantic scan blocking forbidden imports, introspection, caching, background work, timer patching and paths into the judge.", "milliseconds"],
    ["L1", "Hermetic, sandboxed build.", "< 1 s cached"],
    ["L2", "Unit tests plus a **differential oracle**: baseline and candidate run side by side on throwaway database clones, and every response must match. Native differential fuzzing with leak checks.", "~10 s"],
    ["L3", "A learned surrogate ranks candidates, and is distrusted automatically when its audited rank correlation drops.", "milliseconds"],
    ["L4", "Micro-benchmark on the touched paths, paired against the parent.", "~25 s"],
    ["L5", "Macro-benchmark of the full workload against baseline and parent.", "~60 s"],
    ["L6", "Deep assurance: sanitizer fuzzing, a **hidden holdout workload**, and a soak test for leaks.", "~2 min"],
  ], [900, 6726, 1400]),
  gap(),
  p("**Verdicts.** PASS. FAIL: the candidate's fault, never retried. ERROR: an infrastructure fault, which may be retried. SUSPICIOUS: a gain too large to be plausible, which forces deep review."),
  h2("4.8 Defences against reward hacking"),
  p("The genome is treated as an adversary. The defences:"),
  bullet("The judge is physically separate from the search."),
  bullet("Inputs are fresh, hidden and randomised for every evaluation."),
  bullet("CPU is timed across the whole process tree."),
  bullet("The L0 policy scanner rejects forbidden patterns."),
  bullet("A canary suite of known reward hacks must be rejected 100% before any run: 17/17 for Python, 14/14 for Go."),
  bullet("Gains must persist on the holdout."),
  bullet("Suspicion triggers force deep review."),
  bullet("The red-team island tries to fool the judge."),
  h2("4.9 Measurement you can trust"),
  p("The load generator is pinned away from the target. Warm-up runs until steady state, arms are interleaved ABAB in random order, and layout is randomised (link order, environment padding, hash seed). Every comparison is paired, chunk by chunk."),
  p("An **A/A test** (the program against itself) measures the between-run noise. That noise widens every later confidence interval, and an out-of-sample check of the false-positive rate gates promotion. Multiple comparisons are corrected (Holm–Bonferroni)."),
  h2("4.10 Attribution and anti-bloat"),
  p("Each child is measured against its parent. For elite programs, exact **Shapley values** with confidence intervals assign the gain to each gene, and pairwise epistasis (synergy or interference) falls out of the same measurements. A gene whose contribution is not distinguishable from zero is a hitchhiker, and it is pruned."),
  h2("4.11 Sandbox and provenance"),
  p("Untrusted candidates run under seccomp-BPF, an empty network namespace, a filesystem jail, cgroups and an unprivileged user. Each evaluation records an environment fingerprint: kernel, CPU, microcode, governor, compilers, package and binary hashes, and commit. Each LLM call logs its model, parameters and the hashes of its prompt and response."),
  h2("4.12 The mutation lake"),
  p("An append-only, **hash-chained ledger** on a data-only git branch (`colloid/datalake`). A record's id is the sha256 of its canonical JSON. Any edit, reordering or deletion breaks verification. There are three kinds of record:"),
  bullet("**gene records**: one change, with its language-neutral locus and its provenance;"),
  bullet("**program records**: a verified combination of genes, with effects, CIs, holdout, attribution, noise floor and platform;"),
  bullet("**rule records**: CRL rules, with the evidence behind them."),
  p("Today it holds **46 entries**: 18 genes, 26 programs and 2 rules. That includes the two SWE-bench repairs, each carrying its official grade and memorisation-probe verdict."),
  h2("4.13 One contract, many implementations"),
  p("One reference system, StackZero, is implemented in Python + C, Go, and TypeScript (Node and Bun). Every implementation has the same HTTP API, database and deliberate first-version inefficiencies, and is judged by the same oracle and protocol."),
  bullet("A port's conformance is decided by the judge."),
  bullet("Language comparisons are fair."),
  bullet("Knowledge can transfer: a database index is the same locus on every implementation."),
  h2("4.14 CRL: the first piece of Colloid's own language"),
  p("Declarative optimisation rules, mined from verified evidence. A rule without evidence, or whose evidence interval includes zero, is refused by the parser. Rules read facts extracted conservatively from the queries a service issues, so a rule may fail to fire but never fires wrongly. Example:"),
  p("`rule equality-filter-sorted-index v1: when a query filters table.column = ? and orders by table.sort, propose index (column, sort), unless covered; evidence: lake record, gain 7.48%, CI [1.11%, 13.44%].`"),
  h2("4.15 Real repositories: the SWE-bench track"),
  p("The engine repairs real GitHub issues in eight projects, including Django, SymPy, Sphinx, Matplotlib and pytest:"),
  bullet("The search sees only the issue text and the repository; the gold tests stay hidden."),
  bullet("A judge runs in a network-off container: patch policy, compile, regression tests and validated reproduction scripts."),
  bullet("The official grader is the holdout."),
  bullet("Everything was pre-registered before any instance ran, including a post-hoc **memorisation probe** that checks whether a model recalls a fix instead of finding it."),
];

const s5 = [
  h1("5. The self-evolution loop"),
  p("This is how \"findings modify the core engine that builds the stack\" works in practice, and where the limits sit."),
  num("**Search.** Operators propose mutations where causal leverage is highest."),
  num("**Judge.** The frozen cascade accepts a change only if it is correct and measurably better, with calibrated confidence."),
  num("**Record.** Survivors enter the hash-chained lake with their complete evidence."),
  num("**Distil.** Attribution separates the genes that carry a gain from hitchhikers. Recurring carriers become CRL rules, which need evidence to exist."),
  num("**Feed back.** The next run starts from lake seeds, bandit priors and rule arms. The search is shaped by everything verified before it."),
  num("**Build.** Verified records materialise into deployable stacks (`stack/<target>-verified`), with a manifest tying every change to its evidence."),
  num("**Learn about the method.** When a run exposes a flaw in the engine itself, the fix becomes reviewed code with an ADR, and the next pre-registration uses it."),
  gap(),
  table(["What changes", "How", "Who decides"], [
    ["Candidate programs", "Every generation.", "The judge."],
    ["Seeds and bandit priors", "Automatically, from the lake.", "Measured evidence."],
    ["CRL rules", "Mined from carrying genes; the evidence CI must exclude zero.", "The rule parser, then a held-out gate."],
    ["Engine code and method", "A reviewed change with an ADR.", "The owner, through a reviewed PR."],
    ["The judge", "Only by a reviewed change, never by the engine, never during an experiment.", "The owner."],
  ], [2400, 4126, 2500]),
  gap(),
  callout("Why the judge is frozen", [
    "An optimiser that can edit its grader will. The judge's paths (the evaluator, tests, statistics, the lake code, the sandbox) are listed in `policy.JUDGE_PATHS`. L0 rejects any gene that touches them, and the engine refuses to start on a target that exposes one. Self-improvement changes the searcher; it never changes the referee.",
  ]),
  gap(),
  h2("Examples where findings changed the engine"),
  table(["Finding", "Change to the engine"], [
    ["The best program carried genes that contributed nothing.", "`verify --ablate` and carrying-only stack materialisation."],
    ["The soak test flagged 21 of 57 honest runs as leaking, because it measured database warm-up.", "ADR 0010: per-service memory sampling. Now 0 of 57 false alarms, and all 6 real leaks still caught."],
    ["A promotion happened while the A/A gate was closed.", "The gate is enforced in the engine; such a program is held as `verified`, never promoted."],
    ["The transfer metric counted verification passes made after the run.", "Only in-run passes count, with a regression test."],
    ["SWE-bench: regression tests missed real breakages, and the bandit starved the best model.", "Next protocol: transitive regression tests, largest-model-first routing."],
  ], [4300, 4726]),
];

const s6 = [
  h1("6. Verification: what is proved today, and the road to mathematical proof"),
  p("\"Verified\" in Colloid means more than tested, but today it does **not** mean formally proved. Being exact about that is part of the truth-first principle."),
  table(["Claim", "How it is verified today", "Strength", "Next step"], [
    ["The change preserves behaviour.", "Differential oracle against the baseline on fresh hidden inputs; unit tests; sanitizer fuzzing for C and Go; official tests for SWE-bench.", "Empirical, adversarially tested, not exhaustive.", "Translation validation and SMT equivalence for small pure functions and SQL rewrites."],
    ["The change is faster or cheaper.", "Paired ABAB benchmarks, A/A-calibrated CIs, a hidden holdout, replicates, Holm–Bonferroni correction.", "Statistical, with a measured false-positive rate.", "Cross-machine replay across CPU families."],
    ["The rule is sound.", "Evidence lines whose confidence interval excludes zero.", "Empirical.", "**Proof-carrying rules**: SMT-checked soundness in the style of Alive2; e-graph equality saturation."],
    ["The record is authentic.", "sha256 content addressing, a hash chain, git.", "Cryptographic.", "Signed ledger entries."],
    ["The judge cannot be gamed.", "100% canary rejection, the red-team island, frozen judge paths.", "Adversarially tested.", "A growing canary corpus from every breach found."],
  ], [1900, 3000, 1800, 2326]),
  gap(),
  p("**The design rule going forward.** Use proof where it is decidable and cheap: rewrite rules, small pure functions, SQL transformations with known semantics. Keep measurement where proof is out of reach, such as end-to-end performance on real hardware. Every gene then carries **proof and measurement**, never one pretending to be the other."),
];

const s7 = [
  h1("7. Bare metal to software, silicon-agnostic"),
  table(["Layer", "What Colloid can mutate today"], [
    ["Operating system", "Transparent huge pages, scheduler, socket and CPU-placement settings."],
    ["Memory allocator", "glibc, jemalloc, tcmalloc or mimalloc, each with its own tunables."],
    ["Compiler", "Optimisation level, `-march`, LTO, unrolling and other flags for native code."],
    ["Native code", "C functions (rewritten by LLMs and checked by fuzzing with sanitizers)."],
    ["Runtime", "Event loop, GC, connection pools; the Go runtime and build settings."],
    ["Database", "Indexes and PostgreSQL settings; durability settings are frozen."],
    ["Service code", "Python and Go functions; TypeScript is measurable, not yet mutable."],
  ], [2300, 6726]),
  gap(),
  p("**How hardware enters.**"),
  bullet("Every evaluation records an environment fingerprint: CPU, microcode, governor, kernel, compilers."),
  bullet("The platform layer probes what the host can guarantee. Linux gets full fidelity. macOS and Windows get the same through a container. A fallback mode refuses untrusted code."),
  bullet("Results from different backends are never compared, and the noise floor is measured where the judge runs."),
  p("**What silicon-agnostic means here.** Knowledge is named by meaning, never by machine: a locus is a symbol path, a surface, a layer and a language. A gene proven on one CPU family is a **prior** on another and must be re-verified there. The target-architecture compiler flag is itself a per-silicon locus."),
  p("**Not yet.** Kernel-level mutation needs full VMs (ADR 0004). There are no GPU or ARM bench hosts yet. A multi-architecture bench pool (x86 and ARM) with cross-machine replay is on the roadmap."),
];

const s8 = [
  h1("8. Evidence to date"),
  table(["Experiment", "Result", "What it shows"], [
    ["1. Optimise one stack (Python + C)", "4 programs promoted. Best: −30.9% cost per request (CI 27.3–34.5%); holdout −26.6%; p50 latency −46.9%.", "The engine finds real, verified gains. Ablation shows they came from two database indexes. The LLM-rewritten code genes contributed nothing distinguishable from zero."],
    ["2a. Language neutrality (M1a)", "Passed: the same judge certifies, measures and optimises Python + C and Go.", "The architecture is language-neutral in practice."],
    ["2b. Knowledge transfer (M1b)", "Not passed: primed 14.9 vs cold 19.9 verified gain per hour. Best gains 23.2% vs 29.98%.", "Transfer is not yet shown. Confounds: the arms ran at different request rates (30 vs 45 req/s), and the soak rule was flawed. A re-test is designed."],
    ["Language bake-off", "Cost vs Python: Go −1.7% (CI −3.6 to +0.1), Node −3.1%, Bun −4.9% (negative = costs more). Capacity knee 90 req/s for all.", "With the same algorithms, language choice matters less than data access."],
    ["Judge integrity", "Canaries 17/17 (Python), 14/14 (Go). Soak calibration: 0/57 false alarms, 6/6 leaks caught.", "The judge holds against known reward hacks."],
    ["3. Real GitHub issues (local models)", "2/30 resolved (6.7%, CI 1.8–21.3%), graded by the official harness.", "Both issues stated their own fix. Probes found no memorisation. Selection, not localisation, is the bottleneck."],
    ["3b. Real issues, Kimi K3", "Running under a pre-registered protocol (ADR 0012).", "Will show what a frontier-scale model adds to the same engine."],
  ], [2300, 3400, 3326]),
  gap(),
  h2("What the evidence says, without decoration"),
  bullet("The biggest verified wins so far are in **data access**: indexes and N+1 queries. Code rewrites by today's models have not yet carried a verified gain on their own."),
  bullet("Transfer of knowledge, the heart of the compounding-data thesis, is **unproven**. It is the most important open question."),
  bullet("Small local models do not infer fixes for real issues. They copy fixes the issue states. Stronger models are being measured now."),
  bullet("The judge's own gaps get found and fixed: the soak rule, the gate, regression-test selection. Each is recorded, not hidden."),
];

const s9 = [
  h1("9. Resilience: how the architecture survives its own mistakes"),
  table(["Failure found", "What it would have caused", "Fix"], [
    ["The sanitizer's leak checker needs ptrace, which the sandbox forbids.", "Every native-code deep check lost.", "Leak checks inside the process, from heap accounting: exact, 0 bytes for clean code."],
    ["Promotion with the A/A gate closed.", "Noise promoted as a win.", "The gate is enforced in the engine."],
    ["Soak false positives (21/57).", "Real wins rejected.", "Service-only memory rule (ADR 0010)."],
    ["The transfer metric counted post-run passes.", "Inflated transfer score.", "In-run window, with a regression test."],
    ["A container restart killed a run mid-arm.", "Lost or corrupted results.", "Clean rerun; every run is resumable per instance."],
    ["Docker Hub rate limits; a stale proxy after a restart.", "No images, no runs.", "A GHCR mirror; the setup script detects and repairs a stale proxy."],
    ["An invisible character in a pasted API key.", "Every hosted call refused.", "Keys are cleaned of invisible characters on read."],
  ], [3000, 2500, 3526]),
  gap(),
  h2("Structural principles"),
  bullet("**Separation:** the judge, the search, the data and the code live apart. Data branches cannot be written over code branches."),
  bullet("**Content addressing and hash chains:** nothing is silently overwritten."),
  bullet("**Pre-registration:** decision rules are fixed before results exist. Amendments are dated and made before the runs they govern."),
  bullet("**Fail closed:** an unknown verdict is ERROR, never PASS. Untrusted code without a sandbox is refused."),
  bullet("**Resumability:** every long run continues where it stopped."),
  bullet("**Evidence ladder:** the next rung is built only when the gate below it passes."),
];

const s10 = [
  h1("10. The proprietary data moat"),
  p("**What one lake record holds.**"),
  bullet("A change, named in a language- and machine-neutral way."),
  bullet("The exact source it applies to."),
  bullet("Who proposed it: model, prompt hash, response hash."),
  bullet("How it was judged: oracle, holdout, statistics with confidence intervals, the A/A noise floor, attribution, and the platform fingerprint."),
  bullet("Its place in a tamper-evident chain."),
  p("This is rarer than code. It is a **verified, causal, reproducible record of what makes software better**, and of what does not."),
  p("**Why it compounds, once transfer is proven.** Each record can:"),
  bullet("seed a future search;"),
  bullet("bias the choice of strategy;"),
  bullet("support or refute a rule;"),
  bullet("become part of a deployable stack."),
  p("Negative results count too: they stop the engine from repeating failures."),
  gap(),
  callout("Protection rules (in force now)", [
    "• Proprietary code is optimised with **local models only**, until hosted providers' data-retention and training terms are verified. Hosted models are used only on public benchmark code.",
    "• The lake and stack branches stay in this private repository and are never pushed elsewhere.",
    "• API keys live only in environment settings, never in files, logs, commits or chat.",
    "• The hash chain makes tampering detectable. Memorisation probes check whether a model's \"discovery\" is really recall of public data.",
  ]),
];

const s11 = [
  h1("11. Roadmap, gated by evidence"),
  table(["Horizon", "Step", "Gate (what must be shown)"], [
    ["Now", "Kimi K3 arm on the 30 SWE-bench issues; paired comparison with the local arm.", "Pre-registered exact McNemar test, on all instances and on instances clean of memorisation."],
    ["Weeks", "SWE-bench protocol v2: transitive regression tests, largest-model-first routing, one reproduction script per behaviour, sampling stratified on issues that state their fix.", "A fresh pre-registration; issues created after the model's release date."],
    ["Weeks", "M1 re-test: at least 3 seeds per arm, the fixed judge, equal request rates.", "Primed run ≥ 1.25× the cold run's verified gain per hour."],
    ["Weeks", "Model acceptance gate: executed code tests before any model is used.", "Every model passes on this host's backend."],
    ["Months", "SWE-fficiency track: optimising real third-party repositories (ADR 0013).", "Image access, disk and A/A checks; then speedups verified under our stricter judge."],
    ["Months", "M2: rules generalise to a held-out schema.", "A mined rule reproduces a verified gain where it was not mined."],
    ["Months", "Proof-carrying CRL rules.", "Each rule's soundness checked by an SMT solver before it is applied."],
    ["Year+", "M3: rule backends compiled for several languages.", "Rules work across languages under the same judge."],
    ["Year+", "Our own language and compiler, built on CRL and an existing compiler toolchain chosen by research (e.g. MLIR, LLVM or Cranelift).", "Programs built with it beat their conventional equivalents under the judge."],
    ["Year+", "Silicon-agnostic bench pool (x86, ARM; later accelerators) with cross-machine replay.", "Gains replicate across CPU families."],
    ["Year+", "A full proprietary stack materialised from verified records.", "Every component traced to evidence; the whole stack beats its baseline end to end."],
  ], [1100, 4500, 3426]),
];

const s12 = [
  h1("12. How we work together"),
  table(["The owner", "The engineering session (Claude Code)"], [
    ["Brings ideas, connections, patterns and questions, including the unusual ones; that is where new directions come from.", "Turns an idea into a testable hypothesis, an ADR and a pre-registered experiment."],
    ["Runs deep-research prompts in the Claude web app and returns the reports.", "Writes those prompts when a decision needs outside knowledge, and checks every returned claim against the original paper or code before it enters the docs."],
    ["Decides on direction, spending, and changes to the judge.", "Implements, runs, measures, and reports failures as plainly as successes."],
  ], [4513, 4513]),
  gap(),
  h2("Research prompts queued for the owner"),
  num("**Proven-sound program rewriting (2025–2026):** Alive2-style SMT checking, e-graphs, translation validation, verified compilers, and LLM-proposed rewrites with formal equivalence checks. Which of these can certify rules mined from measured mutations, in Python, Go and C, and at what cost?", "research"),
  num("**Superoptimisation and learned compilers:** the state of the art in LLM-guided superoptimisation and compiler-pass ordering with verified correctness. Which open toolchain (MLIR, LLVM, Cranelift) should a new language target first?", "research"),
  num("**Keeping proprietary data proprietary:** the data-retention and training terms of NVIDIA's hosted endpoints and OpenRouter's providers. What can be sent for public benchmark code, and what must stay on local models?", "research"),
];

const appA = [
  h1("Appendix A. Glossary"),
  table(["Term", "Meaning"], [
    ["A/A test", "The program benchmarked against itself, to measure noise."],
    ["ABAB", "Interleaving the two arms in random order, so drift cancels out."],
    ["ADR", "Architecture decision record: a dated, reviewed design decision."],
    ["Atlas", "Colloid's multi-resolution graph of the target system."],
    ["Canary", "A known reward hack the judge must reject."],
    ["CRL", "Colloid Rule Language: declarative optimisation rules backed by evidence."],
    ["Epistasis", "Interaction between genes (synergy or interference)."],
    ["Gene / genome", "One change at one site / a sparse set of changes on a baseline."],
    ["Hitchhiker", "A gene that rides along with a winner but contributes nothing."],
    ["Holdout", "A workload or test set the search never sees, used for the final judgement."],
    ["Lake", "The append-only, hash-chained store of verified mutations."],
    ["Locus", "A mutable site: a unit plus a surface (code region, knob, index set, …)."],
    ["MAP-Elites", "An archive keeping the best solution per behaviour cell."],
    ["Pre-registration", "Fixing an experiment's decision rule before results exist."],
    ["Shapley value", "A gene's average marginal contribution over all orderings."],
    ["VGPH", "Verified gain per hour: the transfer-test metric."],
  ], [2200, 6826]),
  h1("Appendix B. Where everything lives"),
  table(["What", "Where"], [
    ["Architecture (detailed)", "`docs/ARCHITECTURE.md`"],
    ["Decisions", "`docs/adr/0001`–`0013`"],
    ["Experiment 1 results", "`docs/INITIAL_RESULTS.md`"],
    ["Experiment 2 results (polyglot, M1, bake-off)", "`docs/POLYGLOT_RESULTS.md` (PR #2)"],
    ["SWE-bench results and findings", "`docs/SWEBENCH_RESULTS.md`, `docs/results/swebench/` (PR #3)"],
    ["Related research, verified", "`docs/RELATED_WORK.md`"],
    ["Runbook and working agreement", "`docs/HANDOFF.md`"],
    ["The mutation lake", "branch `colloid/datalake`"],
    ["The verified reference stack", "branch `stack/stackzero-verified`"],
    ["This document's source", "`docs/vision/build_architecture_doc.js`"],
  ], [3600, 5426]),
];

const doc = new Document({
  creator: "Colloid", title: "Colloid: architecture, evidence and roadmap",
  styles: {
    default: { document: { run: { font: "Calibri", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 34, bold: true, font: "Calibri", color: ACCENT }, paragraph: { spacing: { before: 120, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, font: "Calibri", color: ACCENT }, paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 23, bold: true, font: "Calibri", color: "333333" }, paragraph: { spacing: { before: 180, after: 80 }, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 270 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "–", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 1080, hanging: 270 } } } }] },
      { reference: "steps", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 300 } } } }] },
      { reference: "research", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 300 } } } }] },
    ],
  },
  sections: [{
    properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } }, titlePage: true },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT,
      children: [new TextRun({ text: "Colloid: architecture, evidence and roadmap", color: MUTED, size: 16 })] })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
      children: [new TextRun({ text: "Confidential · page ", color: MUTED, size: 16 }), new TextRun({ children: [PageNumber.CURRENT], color: MUTED, size: 16 })] })] }) },
    children: [...title, ...toc, ...s1, ...s2, ...s3, ...s4, ...s5, ...s6, ...s7, ...s8, ...s9, ...s10, ...s11, ...s12, ...appA],
  }],
});

const out = path.join(__dirname, "Colloid_Architecture_and_Vision.docx");
Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(out, buf); console.log("wrote", out); });
